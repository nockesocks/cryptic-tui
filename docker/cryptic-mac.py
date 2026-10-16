#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""cryptic on macOS.

Runs the Linux cryptic in a container inside a QEMU VM (Colima profile `cryptic`), plugs every external disk
into that VM through QEMU's monitor socket (QMP) while cryptic runs, and mounts each stick cryptic unlocks
at <mount folder>/<name> over SMB. Standard library only: runs on the Mac's /usr/bin/python3.

Layout (install.sh): ~/.local/share/cryptic/{cryptic.py,docker/}; settings, mac.json and ctl/ in
~/.config/cryptic/tui/ — only that subfolder goes into the container (the phone app's signing key lives
one level up on this Mac).
"""
import hashlib
import json
import os
import plistlib
import pwd
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PROFILE = "cryptic"
LINKS = ("https://github.com/nockesocks/cryptic-tui/releases/latest",  # the only pages cryptic may open
         "https://nockesocks.github.io/tip/?")
APP_DIR = Path(__file__).resolve().parent.parent
TERM_RESET = "\033[?1000l\033[?1003l\033[?1015l\033[?1006l\033[?1004l\033[?2004l\033[?1049l\033[?25h"


def say(msg):
    print(f"\033[1;35m::\033[0m {msg}", flush=True)


# ── running things as the Mac user (Colima and Docker refuse root) ──
USER = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
PW = pwd.getpwnam(USER)
HOME = Path(PW.pw_dir)
CONFIG = HOME / ".config" / "cryptic" / "tui"
CTL = CONFIG / "ctl"
DOCKER = ["docker", "--context", f"colima-{PROFILE}"]  # never DOCKER_CONTEXT: `colima stop` would then reset
USER_ENV = {"HOME": str(HOME), "USER": USER,                # the user's active context to `default`
            "PATH": os.environ.get("CRYPTIC_PATH") or "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}


def as_user(cmd, check=True, **kw):
    env = {**USER_ENV, **kw.pop("env", {})}
    full = (["sudo", "-u", USER] if os.geteuid() == 0 else []) + ["/usr/bin/env", *[f"{k}={v}" for k, v in env.items()], *cmd]
    return subprocess.run(full, check=check, **kw)


def mount_folder() -> Path:
    try:
        folder = json.loads((CONFIG / "mac.json").read_text())["mount_folder"]
        return HOME / folder[2:] if folder.startswith("~/") else Path(folder)  # HOME is root's under sudo
    except (OSError, ValueError, KeyError):
        return HOME / "cryptic"


def display_folder() -> str:
    folder = str(mount_folder())
    return "~" + folder[len(str(HOME)):] if folder.startswith(str(HOME)) else folder


# ── QEMU monitor ──
class QMP:
    def __init__(self):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.connect(str(HOME / ".colima" / "_lima" / f"colima-{PROFILE}" / "qmp.sock"))
        self.f = self.s.makefile("rb")
        self._reply()  # greeting
        self.cmd("qmp_capabilities")

    def _reply(self):
        while True:
            line = self.f.readline()
            if not line:
                raise RuntimeError("QEMU closed the monitor connection")
            msg = json.loads(line)
            if "event" not in msg:
                return msg

    def cmd(self, name, **args):
        self.s.sendall(json.dumps({"execute": name, "arguments": args}).encode())
        r = self._reply()
        if "error" in r:
            raise RuntimeError(f"{name}: {r['error']['desc']}")
        return r["return"]

    def close(self):
        self.s.close()


def usb_identity(disk: str) -> dict:
    """Name, size and vendor/product/serial: inside the VM the stick is a plain SCSI disk, so the wrapper passes
    its identity in. `id` (the USB serial, else a hash of name and size) is how drives.json remembers it."""
    info = plistlib.loads(subprocess.run(["/usr/sbin/diskutil", "info", "-plist", disk], capture_output=True).stdout or b"<plist/>")
    name = (info.get("MediaName") or "").strip() or "USB drive"
    vendor, _, product = name.partition(" ")
    tree = subprocess.run(["/usr/sbin/ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-w0", "-a"],
                          capture_output=True).stdout

    def walk(node, usb=None):  # the USB device (serial, vendor) whose subtree holds this disk
        if isinstance(node, dict):
            if node.get("USB Serial Number") or node.get("kUSBSerialNumberString"):
                usb = (node.get("USB Serial Number") or node.get("kUSBSerialNumberString"),
                       node.get("USB Vendor Name") or node.get("kUSBVendorString"))
            if node.get("BSD Name") == disk:
                return usb
            node = list(node.values())
        if isinstance(node, list):
            for x in node:
                if (found := walk(x, usb)):
                    return found
        return None
    try:
        serial, usb_vendor = walk(plistlib.loads(tree)) if tree else (None, None)
    except (ValueError, TypeError):
        serial, usb_vendor = None, None
    if usb_vendor:  # macOS's media name often leaves the maker out ("USB Flash Drive")
        vendor, product = usb_vendor, name
        name = name if name.startswith(usb_vendor) else f"{usb_vendor} {name}"
    size = int(info.get("TotalSize") or info.get("Size") or 0)
    # no serial → name+size, so two identical no-name sticks count as one drive
    ident = re.sub(r"[^\w.-]", "", str(serial or ""))[:36] or "x" + hashlib.sha1(f"{name}{size}".encode()).hexdigest()[:16]
    return {"id": ident, "name": name, "size": size,
            "qemu": {"vendor": vendor[:8] or "USB", "product": (product or name)[:16], "serial": ident,
                     "rotation_rate": 1}}  # solid-state, so details don't say HDD


def external_disks() -> set:
    p = subprocess.run(["/usr/sbin/diskutil", "list", "-plist", "external", "physical"], capture_output=True)
    return set(plistlib.loads(p.stdout).get("WholeDisks", [])) if p.returncode == 0 and p.stdout else set()


def log(msg: str):
    """Wrapper errors go to a file: printing would draw over the TUI."""
    with open(CONFIG / "cryptic-mac.log", "a") as f:
        f.write(f"{time.strftime('%F %T')} {msg}\n")


# ── the bridge: disks into the VM, shares onto the Mac ──
class Bridge(threading.Thread):
    def __init__(self, smb_pw: str):
        super().__init__(daemon=True)
        self.smb_pw = smb_pw
        self.info: dict = {}          # diskN → usb_identity(), for every external drive plugged in
        self.disks: set = set()       # diskN plugged into the VM
        self.mounted: set = set()     # share names mounted on the Mac
        self.stop = threading.Event()
        self.base = mount_folder()
        self.prefs_file = CONFIG / "drives.json"  # {id: {"auto": bool, "name": str}}; new drives auto-load
        try:
            self.prefs = json.loads(self.prefs_file.read_text())
        except (OSError, ValueError):
            self.prefs = {}

    def run(self):
        last_scan = 0.0
        while not self.stop.is_set():
            try:
                if time.monotonic() - last_scan > 2:
                    last_scan = time.monotonic()
                    self.sync_disks()
                self.handle_requests()
                self.sync_shares()
            except PermissionError as e:  # macOS privacy settings, not file permissions
                log(f"bridge: {e!r} — give Full Disk Access to your terminal and any multiplexer in it "
                    "(System Settings ▸ Privacy & Security), then restart them")
            except Exception as e:  # keep the TUI alive; the next round retries
                log(f"bridge: {e!r}")
            self.stop.wait(0.25)

    def auto(self, d: str) -> bool:
        return self.prefs.get(self.info[d]["id"], {}).get("auto", True)

    def sync_disks(self):
        now = external_disks()
        changed = False
        for d in sorted(now - set(self.info)):
            self.info[d] = usb_identity(d)
            self.prefs.setdefault(self.info[d]["id"], {"auto": True})["name"] = self.info[d]["name"]
            if self.auto(d):
                self.plug(d)
            changed = True
        for d in sorted(set(self.info) - now):  # pulled out
            if d in self.disks:
                self.unplug(d)
            del self.info[d]
            changed = True
        if changed:
            self.push_state()

    def handle_requests(self):
        """The TUI writes ctl/requests/<n>.json: {"op": "load" | "release" | "auto", "id": …, "value": bool}."""
        reqs = sorted((CTL / "requests").glob("*.json"))
        for r in reqs:
            try:
                req = json.loads(r.read_text())
            except ValueError:
                continue  # still being written; next round
            r.unlink()
            d = next((d for d, i in self.info.items() if i["id"] == req.get("id")), None)
            if req.get("op") == "auto":
                self.prefs.setdefault(req["id"], {})["auto"] = bool(req.get("value"))
            elif d and req.get("op") == "load" and d not in self.disks:
                self.plug(d)
            elif d and req.get("op") == "release" and d in self.disks:
                self.unplug(d)
                subprocess.run(["/usr/sbin/diskutil", "mountDisk", d], capture_output=True)
            elif req.get("op") == "url" and str(req.get("id")).startswith(LINKS):
                as_user(["/usr/bin/open", req["id"]], check=False)  # updates and tips, in the user's browser
            elif req.get("op") in ("finder", "terminal", "copy") and req.get("id") in self.mounted:
                self.open_share(req["op"], self.base / req["id"])
        if reqs:
            self.push_state()

    def open_share(self, op: str, path: Path):
        """Finder, a new window of the terminal cryptic runs in, or the clipboard — as the user, on the Mac."""
        if op == "finder":
            as_user(["/usr/bin/open", str(path)], check=False)
        elif op == "terminal":  # the app that started cryptic; Terminal.app if macOS didn't say
            app = os.environ.get("__CFBundleIdentifier") or "com.apple.Terminal"
            as_user(["/usr/bin/open", "-b", app, str(path)], check=False)
        else:
            as_user(["/usr/bin/pbcopy"], input=str(path).encode(), check=False)

    def push_state(self):
        self.prefs_file.write_text(json.dumps(self.prefs, indent=2) + "\n")
        if os.geteuid() == 0:
            os.chown(self.prefs_file, PW.pw_uid, PW.pw_gid)
        state = [{**{k: i[k] for k in ("id", "name", "size")}, "auto": self.auto(d),
                  "where": "cryptic" if d in self.disks else "mac"} for d, i in sorted(self.info.items())]
        as_user([*DOCKER, "exec", "-i", "cryptic", "sh", "-c",
                 "mkdir -p /run/cryptic && cat > /run/cryptic/drives.new && mv /run/cryptic/drives.new /run/cryptic/drives.json"],
                input=json.dumps(state).encode(), check=False, capture_output=True)

    def plug(self, d: str):
        subprocess.run(["/usr/sbin/diskutil", "unmountDisk", d], capture_output=True)  # it's cryptic's now
        os.chown(f"/dev/r{d}", PW.pw_uid, -1)  # QEMU runs as the user; reset on unplug or replug
        q = QMP()
        try:
            q.cmd("blockdev-add", driver="raw", **{"node-name": f"c_{d}"},
                  file={"driver": "host_device", "filename": f"/dev/r{d}"})
            q.cmd("device_add", driver="scsi-hd", bus="scsi0.0", drive=f"c_{d}", id=f"c_{d}", **self.info[d]["qemu"])
        finally:
            q.close()
        self.disks.add(d)

    def unplug(self, d: str):
        q = QMP()
        try:
            q.cmd("device_del", id=f"c_{d}")
            for _ in range(50):  # device_del finishes asynchronously
                if not any((b.get("qdev") or "").endswith(f"c_{d}") for b in q.cmd("query-block")):
                    break
                time.sleep(0.1)
            q.cmd("blockdev-del", **{"node-name": f"c_{d}"})
        except RuntimeError:
            pass
        finally:
            q.close()
        if os.geteuid() == 0 and os.path.exists(f"/dev/r{d}"):
            os.chown(f"/dev/r{d}", 0, -1)
        self.disks.discard(d)

    def sync_shares(self):
        wanted = {p.name for p in (CTL / "shares").glob("*")}
        for name in sorted(wanted - self.mounted):
            target = self.base / name
            as_user(["mkdir", "-p", str(target)])
            url = f"//cryptic:{self.smb_pw}@127.0.0.1:4450/{name}"
            if as_user(["/sbin/mount_smbfs", "-N", url, str(target)], check=False, capture_output=True).returncode == 0:
                self.mounted.add(name)
        for name in sorted(self.mounted - wanted):
            self.release(name)

    def release(self, name: str, force=False) -> bool:
        target = self.base / name
        cmd = ["/sbin/umount", "-f", str(target)] if force else ["/sbin/umount", str(target)]
        if os.path.ismount(target) and as_user(cmd, check=False, capture_output=True).returncode != 0:
            return False  # busy on the Mac: cryptic tells the user to close the files
        try:
            target.rmdir()
        except OSError:
            pass
        self.mounted.discard(name)
        return True


def close_all_in_vm():
    """Only matters if the TUI died: a mapper left open outlives the container in the VM's kernel."""
    as_user([*DOCKER, "exec", "cryptic", "sh", "-c",
             'for m in /media/*; do mountpoint -q "$m" && umount "$m"; done; '
             'for d in /dev/mapper/cryptic_*; do [ -e "$d" ] && cryptsetup close "${d##*/}"; done; true'],
            check=False, capture_output=True)


def compose(*args, **kw):
    env = {"CRYPTIC_CONFIG": str(CONFIG), **kw.pop("env", {})}
    return as_user([*DOCKER, "compose", "-f", str(APP_DIR / "docker" / "compose.yml"), *args], env=env, **kw)


def start_vm():
    say("starting the cryptic VM")
    as_user(["colima", "start", PROFILE, "--vm-type", "qemu", "--cpus", "2", "--memory", "4", "--disk", "10",
             "--activate=false"], stdout=subprocess.DEVNULL)


def build():
    """install.sh: build the image once, so the first `cryptic` starts quickly."""
    start_vm()
    say("building the cryptic image")
    compose("build")
    as_user(["colima", "stop", PROFILE], check=False, capture_output=True)


def main():
    if sys.argv[1:] == ["--build"]:
        return build()
    if os.geteuid() != 0:  # sudo first, as on Linux; the raw disks are root-only
        env = [f"{k}={os.environ[k]}" for k in ("TERM", "COLORTERM", "LANG", "__CFBundleIdentifier") if k in os.environ]
        os.execvp("sudo", ["sudo", "/usr/bin/env", *env, f"CRYPTIC_PATH={os.environ.get('PATH', '')}",
                           sys.executable, os.path.abspath(__file__), *sys.argv[1:]])
    if USER == "root":
        sys.exit("run cryptic as yourself, not as root")
    for d in (CONFIG, CTL, CTL / "shares", CTL / "requests"):
        as_user(["mkdir", "-p", str(d)])
    for p in [*(CTL / "shares").glob("*"), *(CTL / "requests").glob("*")]:
        p.unlink()
    start_vm()
    smb_pw = secrets.token_urlsafe(24)
    compose("up", "-d", "--force-recreate", env={"CRYPTIC_SMB_PW": smb_pw},
            stdout=subprocess.DEVNULL)
    bridge = Bridge(smb_pw)
    bridge.start()
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C belongs to the TUI
    try:
        as_user([*DOCKER, "exec", "-it", "-e", f"TERM={os.environ.get('TERM', 'xterm-256color')}",
                 "-e", f"COLORTERM={os.environ.get('COLORTERM', 'truecolor')}", "-e", "CRYPTIC_HOST=macos", "-e", f"CRYPTIC_MAC_FOLDER={display_folder()}",
                 "-e", "SUDO_USER=cryptic", "cryptic", "python3", "/app/cryptic.py"], check=False)
    finally:
        if sys.stdout.isatty():
            print(TERM_RESET, end="", flush=True)
            subprocess.run(["stty", "sane"])
        say("closing up")
        bridge.stop.set()
        bridge.join(timeout=5)
        for name in sorted(bridge.mounted):  # only left over if the TUI died
            bridge.release(name, force=True)
        close_all_in_vm()
        for d in sorted(bridge.disks):  # eject: left attached, macOS would ask what to do with an unreadable disk
            bridge.unplug(d)
            subprocess.run(["/usr/sbin/diskutil", "eject", d], capture_output=True)
        compose("down", check=False, capture_output=True)
        as_user(["colima", "stop", PROFILE], check=False, capture_output=True)
        say("done")


if __name__ == "__main__":
    main()
