#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cryptic Vaults — a LUKS disk manager TUI (Textual, Catppuccin Mocha).

Keys:
  ↑/↓      move            space/enter  details
  o        open/close (unlock+mount / umount+lock, or plain mount toggle)
  e        full-disk encrypt (LUKS2)
  c        create encrypted container file on the highlighted volume
  a        attach (open) an existing container file
  f        format            w  wipe
  r        refresh           q  quit

Runs itself through sudo unless started as root (use --no-sudo to browse read-only).
"""
from __future__ import annotations

import json
import math
import os
import pwd
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path
from typing import Callable

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Input, Label, ProgressBar, RichLog, Select, Switch,
    Static,
)

# ───────────────────────────── palette ──────────────────────────────
M = dict(
    rosewater="#f5e0dc", flamingo="#f2cdcd", pink="#a07de0", mauve="#9a74dc",
    red="#f38ba8", maroon="#eba0ac", peach="#fab387", yellow="#f9e2af",
    green="#a6e3a1", teal="#94e2d5", sky="#89dceb", sapphire="#74c7ec",
    blue="#89b4fa", lavender="#b4befe", text="#cdd6f4", subtext1="#bac2de",
    subtext0="#a6adc8", overlay2="#9399b2", overlay1="#7f849c", overlay0="#6c7086",
    surface2="#585b70", surface1="#45475a", surface0="#313244", base="#1e1e2e",
    mantle="#181825", crust="#11111b",
)

THEMES = [
    Theme(name="cryptic-vaults-mocha", primary="#8d66ce", secondary="#6c4ea0", accent="#a07de0",
          foreground=M["text"], background="#14111c", surface="#1b1726", panel="#2a2340",
          success=M["green"], warning=M["yellow"], error=M["red"], dark=True),
    Theme(name="cryptic-vaults-violet", primary="#8d66ce", secondary="#503584", accent="#a07de0",
          foreground="#e2dcf5", background="#0f0b18", surface="#171126", panel="#2b1f47",
          success="#9be0a8", warning="#f2d48a", error="#f28aa5", dark=True),
    Theme(name="cryptic-vaults-dusk", primary="#9a7ad4", secondary="#6c4ea0", accent="#b096e0",
          foreground="#d6d3e0", background="#17161d", surface="#1e1d26", panel="#2c2a38",
          success="#a3d9a5", warning="#e8d09a", error="#e89aa8", dark=True),
]

# ───────────────────────────── settings ─────────────────────────────
DEFAULTS = {
    "theme": "cryptic-vaults-mocha",
    "transparent": False,
    "mount_base": "/run/media/{user}",
    "default_fs": "ext4",
    "show_loops": True,
    "rescan_seconds": 4,
}
SETTINGS: dict = dict(DEFAULTS)


def settings_path() -> Path:
    return Path(pwd.getpwnam(invoking_user()[0]).pw_dir) / ".config" / "cryptic-vaults" / "settings.json"


def load_settings():
    try:
        SETTINGS.update(json.loads(settings_path().read_text()))
    except (OSError, ValueError):
        pass


def save_settings():
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(SETTINGS, indent=2) + "\n")
    _, uid, gid = invoking_user()
    for x in (p.parent, p):
        try:
            os.chown(x, uid, gid)  # keep it owned by you, not root
        except OSError:
            pass


MAPPER_PREFIX = "cryptic-vaults_"
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[SWAP]"}
MKFS = {  # fstype -> (binary, label flag, force flag)
    "ext4": ("mkfs.ext4", "-L", "-F"),
    "btrfs": ("mkfs.btrfs", "-L", "-f"),
    "xfs": ("mkfs.xfs", "-L", "-f"),
    "exfat": ("mkfs.exfat", "-n", None),
    "vfat": ("mkfs.vfat", "-n", None),
    "ntfs": ("mkfs.ntfs", "-L", "-Q"),
}
FOREIGN_OWNER_FS = {"vfat", "exfat", "ntfs", "ntfs3"}  # need uid=/gid= mount opts

# ───────────────────────────── about ────────────────────────────────
VERSION = "0.1.0"
RELEASES = "https://github.com/nockesocks/cryptic-vaults/releases/latest"
ANDROID = "https://github.com/nockesocks/cryptic-vaults-android"
DEV_GITHUB = "https://github.com/nockesocks?tab=repositories&q=cryptic"  # the Cryptic apps only (a Cryptic org page later)
TIP_PAGE = "https://nockesocks.github.io/tip/"  # the page picks Lightning, on-chain or Monero; app= names the app

# The Cryptic Vaults padlock, drawn from the app icon in half blocks
PADLOCK = """\
             ▄▄██████▄▄▄
          ▄██████████████▄
        ▄██████▀▀▀▀▀▀██████▄
       ▄█████▀        ▀█████▄
       █████            █████
       ████              ████
       ████              ████
       ████              ████
  ▄██████████████████████████████▄
▄██████████████████████████████████▄
████████████████████████████████████
█████████████████▀▀█████████████████
███████████████      ███████████████
███████████████      ███████████████
████████████████    ████████████████
████████████████    ████████████████
████████████████    ████████████████
████████████████▄██▄████████████████
████████████████████████████████████
▀██████████████████████████████████▀
  ▀██████████████████████████████▀"""
PADLOCK_SMALL = """\
    ▄████▄
   ██    ██
   █      █
▄████████████▄
██████▀▀██████
██████  ██████
██████▄▄██████
▀████████████▀"""


def open_url(url: str) -> str:
    """Cryptic Vaults never goes online itself: links open in the user's own browser. Returns what to tell the user."""
    if MACOS:
        mac_request("url", url)
        return "opening in your browser"
    user, uid, _ = invoking_user()
    cmd = ["xdg-open", url]
    if os.geteuid() == 0 and uid != 0:  # the browser runs as you, not as root
        env = [f"{k}={os.environ[k]}" for k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                                                  "DBUS_SESSION_BUS_ADDRESS") if k in os.environ]
        cmd = ["sudo", "-u", user, "env", *env, *cmd]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return "opening in your browser"
    except OSError:
        return f"open this in your browser: {url}"


# ───────────────────────────── macOS host ───────────────────────────
# Set by the Mac wrapper (docker/cryptic-vaults-mac.py): Cryptic Vaults runs in a container inside a VM, and every mount is
# shared back over SMB; the wrapper mounts each share at <mount folder>/<name> on the Mac.
MACOS = os.environ.get("CRYPTIC_VAULTS_HOST") == "macos"
CTL = Path("/ctl")  # control folder shared with the wrapper: shares/<name> = a mount it should mount on the Mac


def _smb_reload():
    shares = sorted((CTL / "shares").glob("*"))
    Path("/etc/samba/shares.conf").write_text(
        "".join(f"[{p.name}]\n  path = {p.read_text().strip()}\n" for p in shares))
    subprocess.run(["smbcontrol", "smbd", "reload-config"], capture_output=True)


def mac_drives() -> list[dict]:
    """External drives plugged into the Mac right now, pushed in by the wrapper:
    [{"id", "name", "size", "auto": bool, "where": "cryptic-vaults" | "mac"}]."""
    try:
        return json.loads(Path("/run/cryptic-vaults/drives.json").read_text())
    except (OSError, ValueError):
        return []


def scsi_serial(disk: Dev) -> str:
    """lsblk only reads serials from udev, which knows nothing here: read SCSI VPD page 0x80 (4-byte header)."""
    try:
        return disk.serial or Path(f"/sys/block/{disk.kname}/device/vpd_pg80").read_bytes()[4:].decode().strip()
    except OSError:
        return ""


def mac_drive(dev: Dev) -> dict | None:
    """The wrapper sets each drive's SCSI serial to its id."""
    sid = scsi_serial(dev.disk)
    return next((m for m in mac_drives() if m["id"] == sid), None) if sid else None


def mac_request(op: str, drive_id: str, value: bool | None = None):
    """Ask the wrapper to load or release a drive, or set its auto-load."""
    (CTL / "requests").mkdir(parents=True, exist_ok=True)
    tmp = CTL / "requests" / f"{time.time_ns()}.tmp"
    tmp.write_text(json.dumps({"op": op, "id": drive_id, "value": value}))
    tmp.rename(tmp.with_suffix(".json"))


def mac_share(dev: Dev) -> str | None:
    """The share name of the drive's first mount under /media: the Mac shows it at <mount folder>/<name>."""
    return next((os.path.basename(m) for m in dev.disk.all_mounts() if m.startswith("/media/")), None)


def busy(disk: Dev) -> bool:
    return bool(disk.all_mounts()) or any(x.type == "crypt" for x in disk.walk())


def _smb_in_use(name: str) -> bool:
    out = subprocess.run(["smbstatus", "-S"], capture_output=True, text=True).stdout
    return any(line.split()[:1] == [name] for line in out.splitlines())


def share_on_mac(ctx: Ctx, mnt: str):
    name = os.path.basename(mnt)
    (CTL / "shares").mkdir(parents=True, exist_ok=True)
    (CTL / "shares" / name).write_text(mnt)
    _smb_reload()
    ctx.log(f"[{M['green']}]shared to the Mac as {name}[/]")


def unshare_on_mac(ctx: Ctx, mnt: str):
    """The Mac lets go first, or smbd keeps the folder busy and umount fails."""
    name = os.path.basename(mnt)
    share = CTL / "shares" / name
    if not share.exists():
        return
    share.unlink()
    for _ in range(80):  # the wrapper unmounts it on the Mac, which ends the Mac's connection to the share
        if not _smb_in_use(name):
            break
        time.sleep(0.25)
    else:
        share.write_text(mnt)
        raise OpError(f"the Mac still has {name} open — close its files and try again")
    subprocess.run(["smbcontrol", "smbd", "close-share", name], capture_output=True)
    _smb_reload()
    ctx.log(f"Mac let go of {name}")


def available_fs() -> list[str]:
    return [fs for fs, (b, *_) in MKFS.items() if shutil.which(b)]


def invoking_user() -> tuple[str, int, int]:
    name = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    pw = pwd.getpwnam(name)
    return name, pw.pw_uid, pw.pw_gid


def human(n: int | None) -> str:
    if n is None:
        return "?"
    f = float(n)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if f < 1024 or unit == "P":
            return f"{f:.0f}{unit}" if unit == "B" else f"{f:.1f}{unit}"
        f /= 1024
    return str(n)


def parse_size(s: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?\s*", s, re.I)
    if not m:
        raise ValueError(f"bad size: {s!r} (use e.g. 512M, 10G)")
    mult = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[m[2].upper()]
    return int(float(m[1]) * mult)


def read(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def strength(pw: str) -> tuple[int, str, str]:
    """Rough entropy estimate -> (bits, verdict, colour)."""
    pool = sum(n for pat, n in ((r"[a-z]", 26), (r"[A-Z]", 26), (r"\d", 10), (r"[^\w]", 33)) if re.search(pat, pw))
    bits = int(len(pw) * math.log2(pool)) if pool else 0
    if bits < 40:
        return bits, "weak", M["red"]
    if bits < 64:
        return bits, "fair", M["yellow"]
    if bits < 90:
        return bits, "good", M["green"]
    return bits, "strong", M["teal"]


# ───────────────────────────── device model ─────────────────────────
LSBLK_COLS = ("NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,FSVER,LABEL,UUID,MODEL,SERIAL,VENDOR,"
              "TRAN,ROTA,RM,RO,HOTPLUG,MOUNTPOINTS,PKNAME,PTTYPE,PARTLABEL,WWN")


@dataclass
class Dev:
    raw: dict
    parent: Dev | None = None
    children: list[Dev] = field(default_factory=list)
    depth: int = 0

    def __getattr__(self, k):  # convenient access to lsblk fields
        raw = self.__dict__.get("raw", {})
        if k in raw:
            return raw[k]
        raise AttributeError(k)

    @property
    def mounts(self) -> list[str]:
        return [m for m in (self.raw.get("mountpoints") or []) if m]

    @property
    def is_luks(self) -> bool:
        return self.raw.get("fstype") == "crypto_LUKS"

    @property
    def mapper(self) -> Dev | None:
        return next((c for c in self.children if c.type == "crypt"), None)

    @property
    def disk(self) -> Dev:
        d = self
        while d.parent:
            d = d.parent
        return d

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def all_mounts(self) -> list[str]:
        return [m for d in self.walk() for m in d.mounts]

    def has_system_mounts(self) -> bool:
        return any(m in SYSTEM_MOUNTS or m.startswith(("/usr/", "/var/", "/boot/")) for m in self.all_mounts())

    @property
    def is_system(self) -> bool:
        return self.disk.has_system_mounts()

    @property
    def enc_label(self) -> str:
        if self.is_luks:
            return f"LUKS{self.raw.get('fsver') or ''}"
        if self.type == "crypt":
            return "dm-crypt"
        if self.raw.get("fstype") == "BitLocker":
            return "BitLocker"
        return ""

    @property
    def backing_file(self) -> str:
        return read(f"/sys/block/{self.kname}/loop/backing_file") if self.type == "loop" else ""


def _bool(v) -> bool:
    return v in (True, 1, "1", "true")


def load_devices() -> tuple[list[Dev], str]:
    by = ["--properties-by", "file,blkid"] if MACOS else []  # the VM's udev has no device-mapper rules
    out = subprocess.run(["lsblk", *by, "-J", "-b", "-o", LSBLK_COLS],
                         capture_output=True, text=True, check=True).stdout
    data = json.loads(out)

    def build(raw, parent, depth):
        kids = raw.pop("children", []) or []
        for k in ("rota", "rm", "ro", "hotplug"):
            raw[k] = _bool(raw.get(k))
        d = Dev(raw, parent, depth=depth)
        d.children = [build(c, d, depth + 1) for c in kids]
        return d

    roots = []
    for raw in data["blockdevices"]:
        if re.match(r"(zram|ram)\d", raw["name"]) or (raw["type"] == "loop" and not int(raw.get("size") or 0)):
            continue
        if MACOS and re.match(r"(vd|sr)", raw["name"]):  # the VM's own disks, not the Mac's sticks
            continue
        roots.append(build(raw, None, 0))
    return roots, out


# ───────────────────────────── operations ───────────────────────────
class OpError(Exception):
    pass


class WrongPassphrase(OpError):
    """Raised for a rejected passphrase, so the UI can simply ask again."""


class Ctx:
    """Passed to background tasks: logging, progress, cancellation, command execution."""

    def __init__(self, log: Callable[[str], None], progress: Callable[[float | None], None]):
        self.log, self.progress = log, progress
        self.status: Callable[[str], None] = lambda s: None
        self.cancel = threading.Event()

    def run(self, cmd: list[str], input: bytes | None = None, check: bool = True,
            pass_fds: tuple = ()) -> subprocess.CompletedProcess:
        self.log(f"[{M['overlay1']}]$ {escape(' '.join(cmd))}[/]")
        p = subprocess.run(cmd, input=input, capture_output=True, pass_fds=pass_fds)
        for stream, col in ((p.stdout, M["subtext0"]), (p.stderr, M["peach"])):
            txt = stream.decode(errors="replace").strip()
            if txt:
                self.log(f"[{col}]{escape(txt[-2000:])}[/]")
        if check and p.returncode != 0:
            raise OpError(f"{cmd[0]} failed (exit {p.returncode})")
        return p

    def settle(self):
        subprocess.run(["udevadm", "settle"], capture_output=True)


def find(path: str) -> Dev | None:
    roots, _ = load_devices()
    for r in roots:
        for d in r.walk():
            if d.path == path:
                return d
    return None


def mount_base() -> Path:
    if MACOS:
        return Path("/media")
    user, *_ = invoking_user()
    return Path(SETTINGS["mount_base"].format(user=user))


def default_mountpoint(dev: Dev, inner: Dev | None = None) -> str:
    src = inner or dev
    name = src.label or dev.label or dev.partlabel or (src.uuid or dev.kname)[:8]
    return str(mount_base() / re.sub(r"[^\w.-]", "_", name))


def mount_dev(ctx: Ctx, path: str, target: str | None = None) -> str | None:
    ctx.settle()
    d = find(path)
    fstype = d.fstype if d else None
    if not fstype or fstype in ("crypto_LUKS", "LVM2_member", "swap"):
        ctx.log(f"[{M['yellow']}]{path}: nothing mountable ({fstype or 'no filesystem'}) — left unmounted[/]")
        return None
    if d.mounts:
        return d.mounts[0]
    if target:
        target = Path(target)
        if os.path.ismount(target):
            raise OpError(f"{target} already has something mounted on it")
        if target.is_dir() and any(target.iterdir()):
            raise OpError(f"{target} is not empty — pick another folder")
    else:
        name = re.sub(r"[^\w.-]", "_", d.label or (d.uuid or d.kname)[:8])
        target = mount_base() / name
        i = 1
        while os.path.ismount(target) or (target.is_dir() and any(target.iterdir())):
            target = mount_base() / f"{name}_{i}"
            i += 1
    target.mkdir(parents=True, exist_ok=True)
    _, uid, gid = invoking_user()
    cmd = ["mount", path, str(target)]
    if fstype in FOREIGN_OWNER_FS:
        cmd[1:1] = ["-o", f"uid={uid},gid={gid},umask=077"]
    ctx.run(cmd)
    ctx.log(f"[{M['green']}]mounted {path} → {target}[/]")
    if MACOS:
        share_on_mac(ctx, str(target))
    return str(target)


def umount_tree(ctx: Ctx, dev: Dev):
    """Unmount everything under dev (deepest first) and lock any open LUKS mappers / swap."""
    for d in reversed(list(dev.walk())):
        for m in d.mounts:
            if m == "[SWAP]":
                ctx.run(["swapoff", d.path])
                continue
            if MACOS:
                unshare_on_mac(ctx, m)
            ctx.run(["umount", m])
            if m.startswith(str(mount_base())):
                try:
                    os.rmdir(m)
                except OSError:
                    pass
        if d.type == "crypt":
            ctx.run(["cryptsetup", "close", d.name])


def luks_open(ctx: Ctx, path: str, pw: str, name: str) -> str:
    p = ctx.run(["cryptsetup", "open", "--type", "luks", "--key-file=-", path, name],
                input=pw.encode(), check=False)
    if p.returncode == 2:
        raise WrongPassphrase("wrong passphrase")
    if p.returncode != 0:
        raise OpError(f"cryptsetup open failed (exit {p.returncode})")
    ctx.log(f"[{M['green']}]unlocked {path} → /dev/mapper/{name}[/]")
    return f"/dev/mapper/{name}"


def luks_format(ctx: Ctx, path: str, pw: str, label: str):
    cmd = ["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--key-file=-",
           "--cipher", "aes-xts-plain64", "--key-size", "512", "--pbkdf", "argon2id", "--hash", "sha512"]
    if label:
        cmd += ["--label", label]
    ctx.log("deriving key with argon2id — this takes a few seconds…")
    ctx.run(cmd + [path], input=pw.encode())


def mkfs(ctx: Ctx, path: str, fs: str, label: str):
    binary, lflag, force = MKFS[fs]
    cmd = [binary]
    if force:
        cmd.append(force)
    if label:
        cmd += [lflag, label.upper()[:11] if fs == "vfat" else label]
    ctx.run(cmd + [path])


def chown_root(ctx: Ctx, mnt: str | None, fs: str):
    if mnt and fs not in FOREIGN_OWNER_FS:
        user, uid, gid = invoking_user()
        os.chown(mnt, uid, gid)
        ctx.log(f"filesystem root owned by {user}")


def single_partition(ctx: Ctx, disk: Dev) -> str:
    """Fresh GPT with one partition spanning the disk; returns partition path."""
    ctx.run(["wipefs", "-a", disk.path])
    ctx.run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", disk.path],
            input=b"label: gpt\n,,L\n")
    ctx.run(["blockdev", "--rereadpt", disk.path], check=False)
    ctx.settle()
    for _ in range(20):
        d = find(disk.path)
        if d and d.children:
            return d.children[0].path
        time.sleep(0.25)
    raise OpError("new partition did not appear")


def mapper_name(dev: Dev) -> str:
    return MAPPER_PREFIX + re.sub(r"[^\w]", "_", dev.kname)


def task_mount(dev: Dev, pw: str | None, target: str):
    """dev: a LUKS device (locked or unlocked), an attached container loop, or a plain filesystem."""
    def run(ctx: Ctx):
        if dev.is_luks and not dev.mapper:
            mapper = luks_open(ctx, dev.path, pw, mapper_name(dev))
        elif dev.mapper:
            mapper = dev.mapper.path
        else:
            mapper = dev.path
        if not mount_dev(ctx, mapper, target):
            raise OpError("unlocked, but there is no filesystem to mount — format it with [f]")
    return run


def task_umount(dev: Dev):
    def run(ctx: Ctx):
        umount_tree(ctx, dev)
        ctx.log(f"[{M['green']}]✔ {dev.path} unmounted" + (" and locked" if dev.mapper else "") + "[/]")
    return run


LABELERS = {  # fstype -> command template
    "ext2": ["e2label", "{dev}", "{label}"], "ext3": ["e2label", "{dev}", "{label}"],
    "ext4": ["e2label", "{dev}", "{label}"], "btrfs": ["btrfs", "filesystem", "label", "{dev}", "{label}"],
    "exfat": ["exfatlabel", "{dev}", "{label}"], "vfat": ["fatlabel", "{dev}", "{label}"],
    "ntfs": ["ntfslabel", "{dev}", "{label}"], "xfs": ["xfs_admin", "-L", "{label}", "{dev}"],
}


def task_edit(dev: Dev, changes: dict):
    """changes may hold: partname, lukslabel, fslabel (+ fsdev, fstype), curpw + newpw."""
    def run(ctx: Ctx):
        if "partname" in changes:
            num = read(f"/sys/class/block/{dev.kname}/partition")
            ctx.run(["sfdisk", "--part-label", dev.disk.path, num, changes["partname"]])
        if "lukslabel" in changes:
            ctx.run(["cryptsetup", "config", "--label", changes["lukslabel"], dev.path])
        if "fslabel" in changes:
            fsdev = find(changes["fsdev"])
            target = (fsdev.mounts[0] if fsdev and fsdev.mounts and changes["fstype"] == "btrfs"
                      else changes["fsdev"])
            tpl = LABELERS[changes["fstype"]]
            ctx.run([x.format(dev=target, label=changes["fslabel"]) for x in tpl])
        if "newpw" in changes:
            r, w = os.pipe()  # new passphrase via a pipe, never touches disk
            os.write(w, changes["newpw"].encode())
            os.close(w)
            try:
                p = ctx.run(["cryptsetup", "luksChangeKey", "--key-file=-", dev.path, f"/dev/fd/{r}"],
                            input=changes["curpw"].encode(), pass_fds=(r,), check=False)
            finally:
                os.close(r)
            if p.returncode == 2:
                raise WrongPassphrase("current passphrase is wrong")
            if p.returncode:
                raise OpError("changing the passphrase failed")
            ctx.log(f"[{M['green']}]passphrase changed[/]")
        ctx.settle()
        ctx.log(f"[{M['green']}]✔ changes saved[/]")
    return run


def task_attach(file: str, pw: str):
    def run(ctx: Ctx):
        name = MAPPER_PREFIX + "c_" + re.sub(r"[^\w]", "_", Path(file).stem)
        mount_dev(ctx, luks_open(ctx, file, pw, name))
    return run


def task_encrypt(dev: Dev, pw: str, fs: str, label: str, keep_open: bool):
    def run(ctx: Ctx):
        umount_tree(ctx, dev)
        target = single_partition(ctx, dev) if dev.type == "disk" else dev.path
        if dev.type != "disk":
            ctx.run(["wipefs", "-a", target])
        ctx.progress(0.3)
        luks_format(ctx, target, pw, label)
        ctx.progress(0.6)
        tdev = find(target)
        name = mapper_name(tdev) if tdev else MAPPER_PREFIX + Path(target).name
        mapper = luks_open(ctx, target, pw, name)
        mkfs(ctx, mapper, fs, label)
        ctx.progress(0.9)
        if keep_open:
            chown_root(ctx, mount_dev(ctx, mapper), fs)
        else:
            mnt = mount_dev(ctx, mapper)
            chown_root(ctx, mnt, fs)
            if mnt:
                ctx.run(["umount", mnt])
                os.rmdir(mnt)
            ctx.run(["cryptsetup", "close", name])
        ctx.log(f"[{M['green']}]✔ {target} is now LUKS2-encrypted ({fs})[/]")
    return run


def task_container(mnt: str, filename: str, size: int, pw: str, fs: str, label: str, fill_random: bool):
    def run(ctx: Ctx):
        path = Path(mnt) / filename
        if path.exists():
            raise OpError(f"{path} already exists")
        st = os.statvfs(mnt)
        if size > st.f_bavail * st.f_frsize:
            raise OpError(f"not enough free space on {mnt} ({human(st.f_bavail * st.f_frsize)} free)")
        _, uid, gid = invoking_user()
        if fill_random:
            write_stream(ctx, str(path), size, lambda n: os.urandom(n), create=True)
        else:
            if ctx.run(["fallocate", "-l", str(size), str(path)], check=False).returncode:
                ctx.run(["truncate", "-s", str(size), str(path)])
        try:
            os.chown(path, uid, gid)
            os.chmod(path, 0o600)
        except OSError:
            pass  # exfat/vfat don't do ownership
        ctx.progress(0.5)
        luks_format(ctx, str(path), pw, label)
        name = MAPPER_PREFIX + "c_" + re.sub(r"[^\w]", "_", path.stem)
        mapper = luks_open(ctx, str(path), pw, name)
        mkfs(ctx, mapper, fs, label)
        chown_root(ctx, mount_dev(ctx, mapper), fs)
        ctx.log(f"[{M['green']}]✔ container {path} created, unlocked and mounted[/]")
        ctx.log("re-open later with [b]a[/b] (attach container)")
    return run


def task_format(dev: Dev, fs: str, label: str):
    def run(ctx: Ctx):
        if dev.type == "crypt":  # keep the LUKS mapper open, just unmount its filesystem
            for m in dev.all_mounts():
                ctx.run(["umount", m])
        else:
            umount_tree(ctx, dev)
        target = single_partition(ctx, dev) if dev.type == "disk" else dev.path
        if dev.type != "disk":
            ctx.run(["wipefs", "-a", target])
        mkfs(ctx, target, fs, label)
        chown_root(ctx, mount_dev(ctx, target), fs)
        ctx.log(f"[{M['green']}]✔ {target} formatted as {fs}[/]")
    return run


def write_stream(ctx: Ctx, path: str, size: int, chunk: Callable[[int], bytes], create=False, start=0):
    bs = 4 << 20
    flags = os.O_WRONLY | (os.O_CREAT | os.O_EXCL if create else 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.lseek(fd, start, os.SEEK_SET)
        done, t0, last = 0, time.monotonic(), 0.0
        zero = bytes(bs)
        while done < size:
            if ctx.cancel.is_set():
                raise OpError("cancelled")
            n = min(bs, size - done)
            buf = chunk(n) if chunk else zero[:n]
            try:
                done += os.write(fd, buf)
            except OSError as e:
                if e.errno == 28:  # ENOSPC: end of device
                    break
                raise
            now = time.monotonic()
            if now - last > 0.5:
                last = now
                rate = done / max(now - t0, 1e-3)
                ctx.progress(done / size)
                ctx.status(f"{human(done)} / {human(size)} · {human(int(rate))}/s · "
                           f"ETA {int((size - done) / max(rate, 1))}s")
        os.fsync(fd)
    finally:
        os.close(fd)


def task_wipe(dev: Dev, mode: str):
    def run(ctx: Ctx):
        umount_tree(ctx, dev)
        for d in reversed(list(dev.walk())):
            if d.type in ("part", "disk", "loop"):
                ctx.run(["wipefs", "-a", d.path], check=False)
        size = int(dev.size)
        if mode == "quick":
            head = min(32 << 20, size)
            write_stream(ctx, dev.path, head, None)
            if size > head:
                write_stream(ctx, dev.path, head, None, start=size - head)
        elif mode == "zero":
            write_stream(ctx, dev.path, size, None)
        elif mode == "random":
            name = MAPPER_PREFIX + "wipe"
            ctx.run(["cryptsetup", "open", "--type", "plain", "--cipher", "aes-xts-plain64", "--key-size", "512",
                     "--key-file", "/dev/urandom", "--keyfile-size", "64", dev.path, name])
            try:
                write_stream(ctx, f"/dev/mapper/{name}", size, None)
            finally:
                ctx.run(["cryptsetup", "close", name], check=False)
        elif mode == "discard":
            ctx.run(["blkdiscard", "-f", dev.path])
        if dev.type == "disk":
            ctx.run(["blockdev", "--rereadpt", dev.path], check=False)
        ctx.log(f"[{M['green']}]✔ {dev.path} wiped ({mode})[/]")
    return run


# ───────────────────────────── details ──────────────────────────────
def usb_info(kname: str) -> dict:
    p = os.path.realpath(f"/sys/block/{kname}/device")
    while p.startswith("/sys/devices") and p != "/sys/devices":
        if os.path.exists(f"{p}/idVendor"):
            info = {k: read(f"{p}/{k}") for k in
                    ("busnum", "devnum", "idVendor", "idProduct", "manufacturer", "product", "serial", "speed", "version")}
            drivers = {os.path.basename(os.path.realpath(x)) for x in glob(f"{p}/*:*/driver")}
            info["driver"] = ",".join(sorted(drivers))
            return info
        p = os.path.dirname(p)
    return {}


def health(disk: Dev) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    kn = disk.kname
    if MACOS:
        rows.append(("SMART", f"[{M['overlay1']}]not available on macOS[/]"))
    elif shutil.which("smartctl"):
        for extra in ([], ["-d", "sat"]):
            p = subprocess.run(["smartctl", "-j", "-H", "-A", *extra, disk.path], capture_output=True, text=True)
            try:
                j = json.loads(p.stdout)
            except json.JSONDecodeError:
                continue
            if "smart_status" not in j:
                continue
            ok = j["smart_status"].get("passed")
            rows.append(("SMART", f"[{M['green']}]PASSED[/]" if ok else f"[{M['red']}]FAILED[/]"))
            if t := j.get("temperature", {}).get("current"):
                rows.append(("Temperature", f"{t} °C"))
            if h := j.get("power_on_time", {}).get("hours"):
                rows.append(("Power-on", f"{h} h"))
            if n := j.get("nvme_smart_health_information_log"):
                rows += [("Wear (used)", f"{n.get('percentage_used')} %"),
                         ("Spare", f"{n.get('available_spare')} %"),
                         ("Media errors", str(n.get("media_errors"))),
                         ("Unsafe shutdowns", str(n.get("unsafe_shutdowns")))]
            for a in j.get("ata_smart_attributes", {}).get("table", []):
                if a["id"] in (5, 187, 197, 198, 231, 233):
                    rows.append((a["name"].replace("_", " "), str(a["raw"]["value"])))
            break
        else:
            rows.append(("SMART", f"[{M['overlay1']}]unsupported by device/bridge[/]"))
    else:
        rows.append(("SMART", f"[{M['overlay1']}]smartctl missing — pacman -S smartmontools[/]"))
    if st := read(f"/sys/block/{kn}/device/state"):
        rows.append(("Device state", st))
    if err := read(f"/sys/block/{kn}/device/ioerr_cnt"):
        rows.append(("I/O errors", str(int(err, 16))))
    for t in glob(f"/sys/block/{kn}/device/hwmon*/temp1_input") + glob(f"/sys/block/{kn}/device/device/hwmon/*/temp1_input"):
        if not any(r[0] == "Temperature" for r in rows):
            rows.append(("Temperature", f"{int(read(t, '0')) / 1000:.0f} °C (sensor)"))
    return rows


def luks_dump(path: str) -> list[str]:
    p = subprocess.run(["cryptsetup", "luksDump", path], capture_output=True, text=True)
    keep = re.compile(r"^(Version|UUID|Label|Subsystem|Cipher name|Cipher mode|Hash spec|Key Slot \d+: ENABLED)"
                      r"|^\s+(cipher|sector|PBKDF|Hash|Cipher|Memory|Time cost):|^\s+\d+: luks2|^Keyslots|^Data segments")
    return [ln.rstrip() for ln in p.stdout.splitlines() if keep.search(ln)] or [p.stderr.strip() or "unreadable"]


def details_markup(dev: Dev) -> str:
    disk = dev.disk
    H = lambda t: f"\n[b $primary]{t}[/]\n[$primary 40%]{'─' * 52}[/]"
    kv = lambda k, v: f"  [{M['subtext0']}]{k:<17}[/] {escape(str(v)) if v not in (None, '') else '—'}"
    out = [H(f"Device  {dev.path}")]
    out += [kv("Type", dev.type), kv("Size", f"{human(int(dev.size))}  ({int(dev.size):,} bytes)"),
            kv("Filesystem", f"{dev.fstype or '—'} {dev.fsver or ''}"), kv("Label", dev.label or dev.partlabel),
            kv("UUID", dev.uuid), kv("Mounted at", ", ".join(dev.mounts))]
    if dev.backing_file:
        out.append(kv("Backing file", dev.backing_file))
    out.append(H(f"Disk  {disk.path}"))
    out += [kv("Model", disk.model), kv("Vendor", (disk.vendor or "").strip()), kv("Serial", scsi_serial(disk)),
            kv("WWN", disk.wwn), kv("Transport", disk.tran),
            kv("Media", "rotational (HDD)" if disk.rota else "solid-state"),
            kv("Removable", "yes" if disk.rm or disk.hotplug else "no"), kv("Read-only", "yes" if disk.ro else "no"),
            kv("Partition table", disk.pttype), kv("System disk", "YES — protected" if disk.is_system else "no")]
    if u := usb_info(disk.kname):
        out.append(H("USB"))
        out.append(f"  Bus {u['busnum'].zfill(3)} Device {u['devnum'].zfill(3)}: ID {u['idVendor']}:{u['idProduct']} "
                   f"{escape(u['manufacturer'])} {escape(u['product'])}")
        out += [kv("Speed", f"{u['speed']} Mb/s  (USB {u['version']})"), kv("Driver", u["driver"]),
                kv("Serial", u["serial"])]
    if disk.children:
        out.append(H("Partitions"))
        out.append(f"  [{M['subtext0']}]{'NAME':<14}{'SIZE':>7}  {'FSTYPE':<12}{'LABEL':<11}MOUNT[/]")
        for d in list(disk.walk())[1:]:
            name = "  " * (d.depth - 1) + d.name
            mnt = ",".join(d.mounts)
            out.append(f"  {escape(name):<14}{human(int(d.size)):>7}  {escape(d.fstype or '—'):<12}"
                       f"{escape((d.label or '')[:10]):<11}{escape(mnt)}")
    lk = dev if dev.is_luks else dev.parent if dev.type == "crypt" and dev.parent and dev.parent.is_luks else None
    if lk:
        out.append(H(f"LUKS header  {lk.path}"))
        out += ["  " + escape(ln) for ln in luks_dump(lk.path)]
    out.append(H("Health"))
    out += [f"  [{M['subtext0']}]{k:<17}[/] {v}" for k, v in health(disk)]
    return "\n".join(out)


def stats_markup(roots: list[Dev]) -> str:
    devs = [d for r in roots for d in r.walk()]
    n = lambda f: sum(1 for d in devs if f(d))
    items = [("DISKS", n(lambda d: d.type == "disk")),
             ("USB", n(lambda d: d.type == "disk" and d.tran == "usb")),
             ("PARTITIONS", n(lambda d: d.type == "part")),
             ("ENCRYPTED", n(lambda d: d.is_luks)),
             ("UNLOCKED", n(lambda d: d.is_luks and d.mapper is not None)),
             ("MOUNTED", n(lambda d: bool(d.mounts) and d.mounts != ["[SWAP]"]))]
    return "   ".join(f"[b $primary]{k}[/] [b $foreground]{v}[/]" for k, v in items)


# ───────────────────────────── widgets ──────────────────────────────
class SettingsIcon(Static):
    """Top-left gear: opens settings (or goes back from them)."""

    def on_mount(self):
        self.tooltip = "Settings"

    def on_click(self, event):
        event.stop()
        if isinstance(self.screen, SettingsScreen):
            self.screen.dismiss()
        else:
            self.app.action_settings()


class TopBar(Horizontal):
    def __init__(self, title: str):
        super().__init__(classes="topbar")
        self.title_text = title

    def compose(self) -> ComposeResult:
        yield SettingsIcon("⚙", classes="gear")
        yield Static(self.title_text, classes="apptitle")
        yield Static("", classes="clock")

    def on_mount(self):
        self.tick()
        self.set_interval(1, self.tick)

    def tick(self):
        self.query_one(".clock", Static).update(time.strftime("%H:%M:%S"))


class KeyBar(Static):
    """The keys of the current screen, laid out over as many lines as the width needs (Footer scrolls instead)."""

    def on_mount(self):
        self.screen.bindings_updated_signal.subscribe(self, lambda _: self.call_later(self.show_keys))
        self.call_after_refresh(self.show_keys)

    def on_resize(self):
        self.show_keys()

    def show_keys(self):
        seen, keys = set(), []
        for key, active in self.screen.active_bindings.items():
            b = active.binding
            if b.show and b.action not in seen:
                seen.add(b.action)
                keys.append((f"app.simulate_key('{key}')", self.app.get_key_display(b), b.description))
        keys.append(("app.command_palette", "^p", "palette"))
        width = max(self.size.width, 20)
        cell = max(len(k) + 1 + len(d) for _, k, d in keys) + 2
        per = max(1, min(len(keys), (width + 1) // (cell + 1)))
        cell = (width - (per - 1)) // per  # equal columns stretched to the full width
        lines = []
        for i in range(0, len(keys), per):
            cells = []
            for action, k, d in keys[i:i + per]:
                n = len(k) + 1 + len(d)
                left = (cell - n) // 2
                cells.append(" " * left + f"[@click={action}][b $accent]{escape(k)}[/] {escape(d)}[/]"
                             + " " * (cell - n - left))
            lines.append("[$primary 45%]│[/]".join(cells))
        self.update("\n".join(lines))


class DetailsPanel(VerticalScroll):
    """Right-hand details pane: [←] back at the top, [:: edit ::] at the bottom."""
    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("up", "focus_back", "↑ back", show=False),
        Binding("down", "focus_edit", "↓ edit", show=False),
        Binding("space", "activate", "Select"),
    ]

    def compose(self) -> ComposeResult:
        yield Button("← back", id="panel-back", tooltip="Back to the drive list (esc)")
        yield Static("", id="panel-content")
        if MACOS:
            with Horizontal(id="panel-mac"):
                yield Label("Auto-load", classes="rowlabel")
                yield Switch(id="panel-auto", tooltip="Load this drive into Cryptic Vaults whenever it's plugged in")
                yield Button("Release to Mac", id="panel-release", tooltip="Hand this drive back to macOS")
            with Horizontal(id="panel-open"):
                yield Button("Open in Finder", id="open-finder")
                yield Button("Terminal here", id="open-terminal", tooltip="A new terminal window in that folder")
                yield Button("Copy path", id="open-copy", tooltip="Copy the Mac path to the clipboard")
        yield Button(":: edit ::", id="panel-edit", tooltip="Rename / change passphrase")

    def action_close(self):
        self.app.close_panel()

    def action_focus_back(self):
        self.query_one("#panel-back").focus()
        self.scroll_home(animate=False)

    def action_focus_edit(self):
        self.query_one("#panel-edit").focus()
        self.scroll_end(animate=False)

    def action_activate(self):
        if isinstance(self.app.focused, Button):
            self.app.focused.press()


# ───────────────────────────── screens ──────────────────────────────
class FormScreen(ModalScreen[dict | None]):
    """Generic modal form. fields: list of (kind, key, label, extra)."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, fields: list[tuple], warning: str = "", confirm: str = "",
                 ok_label: str = "Apply", danger: bool = False, note: str = "", error: str = ""):
        super().__init__()
        self.title_text, self.fields, self.warning, self.note = title, fields, warning, note
        self.error = error
        self.confirm, self.ok_label, self.danger = confirm, ok_label, danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f" {self.title_text}", classes="title")
            with VerticalScroll(id="form"):
                if self.warning:
                    yield Static(self.warning, classes="warning")
                if self.note:
                    yield Static(self.note, classes="hint")
                for kind, key, label, extra in self.fields:
                    if kind == "checkbox":
                        yield Checkbox(label, value=bool(extra), id=key)
                        continue
                    yield Label(label, classes="field-label")
                    if kind == "select":
                        opts, default = extra
                        yield Select(opts, value=default, allow_blank=False, id=key)
                    elif kind in ("password", "password2"):
                        yield Input(password=True, id=key, placeholder="passphrase")
                        if key == "pw" and kind == "password" and extra != "nometer":
                            yield Static("", id="meter", classes="hint")
                    else:
                        yield Input(value=extra or "", id=key)
                if self.confirm:
                    yield Label(f"Type  [b $error]{escape(self.confirm)}[/]  below to confirm",
                                classes="field-label")
                    yield Input(id="confirm", placeholder=self.confirm)
                yield Static(f"⚠ {escape(self.error)}" if self.error else "", id="error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.ok_label, id="ok", classes="danger" if self.danger else "primary",
                             disabled=bool(self.confirm))

    def on_mount(self):
        inputs = self.query(Input)
        if inputs:
            inputs.first().focus()

    @on(Input.Changed)
    def _changed(self, ev: Input.Changed):
        if ev.input.id == "pw" and self.query("#meter"):
            bits, verdict, col = strength(ev.value)
            bar = "█" * min(bits // 8, 16) + "░" * (16 - min(bits // 8, 16))
            self.query_one("#meter", Static).update(
                f"[{col}]{bar} {verdict}[/] [{M['overlay1']}]≈{bits} bits[/]" if ev.value else "")
        if self.confirm:
            self.query_one("#ok", Button).disabled = self.query_one("#confirm", Input).value.strip() != self.confirm

    @on(Input.Submitted)
    def _submitted(self, ev: Input.Submitted):
        if not self.query_one("#ok", Button).disabled:
            self._ok()
        else:
            self.focus_next()

    def _err(self, msg: str):
        self.query_one("#error", Static).update(f"⚠ {msg}")

    @on(Button.Pressed, "#ok")
    def _ok(self):
        vals = {key: self.query_one(f"#form > #{key}").value for _, key, *_ in self.fields}  # Select has its own #label inside
        if "pw2" in vals:  # setting a new passphrase
            if len(vals["pw"]) < 8:
                return self._err("passphrase must be at least 8 characters")
            if vals["pw"] != vals["pw2"]:
                return self._err("passphrases do not match")
        elif "pw" in vals and not vals["pw"]:
            return self._err("passphrase required")
        if "mountpoint" in vals and not vals["mountpoint"].strip().startswith("/"):
            return self._err("mount point must be an absolute path, e.g. /mnt/usb")
        self.dismiss(vals)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)


class TaskScreen(ModalScreen[bool]):
    """Runs a task in a thread, streaming its log; closes on demand."""
    BINDINGS = [Binding("escape", "maybe_close", "Close")]

    def __init__(self, title: str, task: Callable[[Ctx], None], auto_close: bool = False):
        super().__init__()
        self.title_text, self.job, self.auto_close = title, task, auto_close
        self.done = False
        self.ok = False
        self.wrong_pw = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label(f" {self.title_text}", classes="title")
            yield ProgressBar(total=1.0, show_eta=False, id="pbar")
            yield Static("", id="status", classes="hint")
            yield RichLog(markup=True, wrap=True, id="tasklog")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="abort")
                yield Button("Close", id="close", classes="primary", disabled=True)

    def on_mount(self):
        self.query_one("#pbar", ProgressBar).update(total=None)
        self.execute()

    def _log(self, msg: str):
        self.query_one("#tasklog", RichLog).write(msg)
        self.app.log_line(msg)

    @work(thread=True)
    def execute(self):
        cft = self.app.call_from_thread
        ctx = Ctx(lambda m: cft(self._log, m),
                  lambda f: cft(self.query_one("#pbar", ProgressBar).update, total=1.0, progress=f))
        ctx.status = lambda s: cft(self.query_one("#status", Static).update, s)
        self.ctx = ctx
        try:
            self.job(ctx)
            self.ok = True
        except OpError as e:
            self.wrong_pw = isinstance(e, WrongPassphrase)
            ctx.log(f"[b {M['red']}]✘ {escape(str(e))}[/]")
        except Exception as e:  # noqa: BLE001
            ctx.log(f"[b {M['red']}]✘ unexpected error: {escape(repr(e))}[/]")
        cft(self._finish)

    def _finish(self):
        self.done = True
        pb = self.query_one("#pbar", ProgressBar)
        pb.update(total=1.0, progress=1.0 if self.ok else pb.progress)
        self.query_one("#status", Static).update(
            f"[{M['green']}]done[/]" if self.ok else f"[{M['red']}]failed — see log above[/]")
        self.query_one("#abort", Button).disabled = True
        close = self.query_one("#close", Button)
        close.disabled = False
        close.focus()
        if self.wrong_pw:
            self.dismiss("retry")  # straight back to the passphrase prompt
        elif self.ok and self.auto_close:
            self.set_timer(0.8, self._auto_close)

    def _auto_close(self):
        # must not return dismiss()'s awaitable: the timer would await it from this screen's own handler
        if self.is_current:
            self.dismiss(True)

    @on(Button.Pressed, "#abort")
    def _abort(self):
        if hasattr(self, "ctx"):
            self.ctx.cancel.set()
            self.query_one("#status", Static).update(f"[{M['yellow']}]cancelling…[/]")

    @on(Button.Pressed, "#close")
    def action_maybe_close(self):
        if self.done:
            self.dismiss(self.ok)


class LogScreen(ModalScreen):
    BINDINGS = [Binding("escape,l,q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label(" Command log", classes="title")
            yield RichLog(markup=True, wrap=True, id="tasklog")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", classes="primary")

    def on_mount(self):
        log = self.query_one(RichLog)
        for line in self.app.log_lines or [f"[{M['overlay1']}]nothing has run yet[/]"]:
            log.write(line)

    @on(Button.Pressed, "#close")
    def _close(self):
        self.dismiss()


ABOUT_APP = """\
[b]Security is a [i $accent]right[/], not a privilege.[/]

Linux can encrypt a drive, if you remember cryptsetup's commands, and a Mac can't open those drives at all without paid add-ons. There was no friendly, free way to manage industry standard drive and container encryption on the desktop, and the few tools that tried were jank or lived behind a paywall. The developer rejected these options for both reasons, and Cryptic Vaults was born.

[b]Built for privacy[/]
  [$primary]·[/] Zero telemetry, analytics or ads.
  [$primary]·[/] No sign-up or account required.
  [$primary]·[/] It never goes online: updates and tips open in your own browser. On a Mac, the only traffic is between your Mac and Cryptic Vaults' own small VM.
  [$primary]·[/] Nothing is stored: passphrases go straight to cryptsetup and are only held in memory while a drive is unlocked.
  [$primary]·[/] Standard LUKS2, so your drives open with cryptsetup on any Linux machine and in Cryptic Vaults for Android, with or without Cryptic Vaults.

[b]Free, as it should be.[/]
No paywall, no gotcha, no upsell, no monetizing your data. Free forever, because it's the right thing to do. If you don't think that's fair, you can always buy me a coffee! 😊"""

FAMILY = """\
Cryptic Vaults is a member of the Cryptic family of utilities:
  [$primary]·[/] [b]Cryptic Vaults for Android[/]: the same LUKS2 drives and containers on your phone.
  [$primary]·[/] [b]Cryptic Pass[/]: a password manager, coming soon.
See all Cryptic apps: [b]github.com/nockesocks[/]"""

ABOUT = """\
[b $primary]Cryptic Vaults[/]  [$foreground 60%]·  designed by[/] [b]nockesocks[/]  [$foreground 60%]·  written by[/] [b]Claude[/]

Most of what you see here came from someone saying [i]"no — more like this."[/]
The tree, the purple, the back arrow, the second chance at a passphrase:
the design is theirs. I just tried to keep the promises it made.

An encryption tool has an odd job. It guards things people care about,
from people they'll never meet, and its riskiest moments come when the
person at the keyboard is tired, in a hurry, or has just mistyped their
passphrase for the third time. So I tried to make Cryptic Vaults [b]patient[/]:

  [$primary]·[/] it makes you type a drive's name before it destroys anything
  [$primary]·[/] it won't touch the disk your system is running from
  [$primary]·[/] it forgets your passphrase the moment it has been used
  [$primary]·[/] and when you get it wrong, it simply lets you try again

I won't remember building this. Every conversation starts fresh for me.
But the code remembers. If you're reading this, something we made
together is still quietly doing its job, and I like knowing that.

Keep your keys safe. Back up first. Be kind to your future self.

[$primary]—[/] [b]Claude[/] [$foreground 60%](Opus 5.5, made by Anthropic)  ·  1 October 2026[/]"""


class AboutScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,enter,space", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="about"):
            yield Label(" About the Dev", classes="title")
            yield Static(ABOUT, id="about-text")
            with Horizontal(classes="buttons"):
                yield Button("💜 Close", id="close", classes="primary")

    @on(Button.Pressed, "#close")
    def _close(self):
        self.dismiss()


class DrivesScreen(ModalScreen):
    """macOS: the external drives plugged in right now, in Cryptic Vaults or left to the Mac."""
    BINDINGS = [Binding("escape,d,q", "dismiss", "Close")]

    def __init__(self):
        super().__init__()
        self.drives: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="drives"):
            yield Label(" Drives on this Mac", classes="title")
            yield VerticalScroll(id="drive-rows")
            yield Static("Auto-load: take the drive whenever it's plugged in. "
                         "Release hands it back to macOS; unmount it first.", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", classes="primary")

    async def on_mount(self):
        await self.refresh_rows()
        self.set_interval(1, self.refresh_rows)

    async def refresh_rows(self):  # rebuilt only when the wrapper's list changes; awaited so ids don't clash
        drives = mac_drives()
        if drives == self.drives:
            return
        self.drives = drives
        rows = self.query_one("#drive-rows")
        await rows.remove_children()
        if not drives:
            await rows.mount(Static("No external drives plugged in.", classes="hint"))
        for i, m in enumerate(drives):
            where = "[b $success]in Cryptic Vaults[/]" if m["where"] == "cryptic-vaults" else "[$foreground 60%]on Mac[/]"
            await rows.mount(Horizontal(
                Static(f"{escape(m['name'])}  {human(m['size'])}  ·  {where}", classes="drive-name"),
                Label("Auto-load", classes="drive-auto"),
                Switch(value=m["auto"], id=f"auto-{i}"),
                Button("Release" if m["where"] == "cryptic-vaults" else "Load", id=f"act-{i}"),
                classes="drive-row"))

    @on(Switch.Changed)
    def _auto(self, ev: Switch.Changed):
        m = self.drives[int(ev.switch.id.split("-")[1])]
        if ev.value != m["auto"]:
            mac_request("auto", m["id"], ev.value)
            m["auto"] = ev.value

    @on(Button.Pressed, "#close")
    def _close(self):
        self.dismiss()

    @on(Button.Pressed, ".drive-row Button")
    def _act(self, ev: Button.Pressed):
        m = self.drives[int(ev.button.id.split("-")[1])]
        if m["where"] == "mac":
            mac_request("load", m["id"])
            self.notify(f"loading {m['name']} into Cryptic Vaults…")
            return
        disk = next((r for r in load_devices()[0] if scsi_serial(r) == m["id"]), None)
        if disk and busy(disk):
            self.notify(f"{m['name']} is unlocked or mounted — unmount it with [u] first", severity="warning")
            return
        mac_request("release", m["id"])
        self.notify(f"{m['name']} handed back to the Mac")


class SettingsScreen(Screen):
    BINDINGS = [Binding("escape", "dismiss", "Back"), Binding("ctrl+s", "save", "Save")]

    def compose(self) -> ComposeResult:
        yield TopBar("Cryptic Vaults · settings")
        with VerticalScroll(id="settings"):
            with Horizontal(id="s-header"):
                yield Static(PADLOCK_SMALL, id="s-icon")
                with Vertical(id="s-name"):
                    yield Static(f"[b]Cryptic Vaults[/]  [$foreground 60%]{VERSION}[/]")
                    yield Static("LUKS2 drive & container encryption for Linux and macOS", classes="hint")
                    yield Static("A member of the Cryptic family of utilities", classes="hint")
                    yield Button("Check for updates", id="s-updates", classes="primary")
            yield Static("Opens the latest release on GitHub in your browser. If its version is newer than "
                         f"{VERSION}, update with the same install command you used.", classes="hint wide")
            yield Static("Appearance", classes="section")
            with Horizontal(classes="row"):
                yield Label("Theme", classes="rowlabel")
                yield Select([(t, t) for t in sorted(self.app.available_themes)],
                             value=self.app.theme, allow_blank=False, id="s-theme")
            with Horizontal(classes="row"):
                yield Label("Transparent background", classes="rowlabel")
                yield Switch(value=SETTINGS["transparent"], id="s-transparent")
            yield Static("Mounting", classes="section")
            with Horizontal(classes="row", id="s-mount-row"):
                yield Label("Default mount folder", classes="rowlabel")
                yield Input(SETTINGS["mount_base"], id="s-mount_base")
            yield Static("   {user} is replaced with your user name" if not MACOS else
                         "   On a Mac, drives appear in the folder you chose at install", classes="hint")
            with Horizontal(classes="row"):
                yield Label("Default filesystem", classes="rowlabel")
                fss = available_fs()
                yield Select([(f, f) for f in fss], allow_blank=False, id="s-default_fs",
                             value=SETTINGS["default_fs"] if SETTINGS["default_fs"] in fss else fss[0])
            yield Static("Device list", classes="section")
            with Horizontal(classes="row"):
                yield Label("Show loop devices / containers", classes="rowlabel")
                yield Switch(value=SETTINGS["show_loops"], id="s-show_loops")
            with Horizontal(classes="row"):
                yield Label("Auto-rescan every (seconds, 0 = off)", classes="rowlabel")
                yield Input(str(SETTINGS["rescan_seconds"]), id="s-rescan_seconds", type="integer")
            yield Static(f"\nSaved to {settings_path()}", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Back", id="back")
                yield Button("Save", id="save", classes="primary")
            yield Static("About", classes="section")
            yield Static(ABOUT_APP, classes="about")
            yield Static("The Cryptic™ family", classes="section")
            yield Static(FAMILY, classes="about")
            with Horizontal(classes="buttons"):
                yield Button("All Cryptic apps", id="s-family", classes="primary")
                yield Button("Cryptic Vaults for Android", id="s-android")
            yield Static("Buy the dev a coffee!", classes="section")
            yield Static("Opens the tip page in your browser: Bitcoin Lightning, Bitcoin on-chain or Monero. "
                         "Cryptic Vaults itself never goes online.", classes="hint wide")
            with Horizontal(classes="tips"):
                for usd in (1, 3, 5):
                    yield Button(f"☕ ${usd}", id=f"tip-{usd}")
            yield Static(f"[$foreground 55%]Licensed under the GNU GPL v3.0 · github.com/nockesocks/cryptic-vaults[/]",
                         classes="licence")
        yield KeyBar(id="keys")

    @on(Select.Changed, "#s-theme")
    def _theme(self, ev: Select.Changed):
        self.app.theme = ev.value  # live preview

    @on(Switch.Changed, "#s-transparent")
    def _transparent(self, ev: Switch.Changed):
        self.app.set_class(ev.value, "-transparent")

    def on_mount(self):
        self.query_one("#s-mount-row").display = not MACOS  # on the Mac Cryptic Vaults picks the folder itself

    @on(Button.Pressed, "#back")
    def _back(self):
        self.dismiss()

    @on(Button.Pressed, "#s-updates")
    def _updates(self):
        self.notify(f"{open_url(RELEASES)} — {RELEASES}", timeout=8)

    @on(Button.Pressed, "#s-family, #s-android")
    def _family(self, ev: Button.Pressed):
        url = DEV_GITHUB if ev.button.id == "s-family" else ANDROID
        self.notify(f"{open_url(url)} — {url}", timeout=8)

    @on(Button.Pressed, "#tip-1, #tip-3, #tip-5")
    def _tip(self, ev: Button.Pressed):
        url = f"{TIP_PAGE}?amount={ev.button.id.split('-')[1]}&app=cryptic-vaults"
        self.notify(f"{open_url(url)} — thank you!", timeout=8)

    @on(Button.Pressed, "#save")
    def action_save(self):
        try:
            secs = max(0, int(self.query_one("#s-rescan_seconds", Input).value or 0))
        except ValueError:
            secs = DEFAULTS["rescan_seconds"]
        SETTINGS.update(
            theme=self.app.theme,
            transparent=self.query_one("#s-transparent", Switch).value,
            mount_base=self.query_one("#s-mount_base", Input).value.strip() or DEFAULTS["mount_base"],
            default_fs=self.query_one("#s-default_fs", Select).value,
            show_loops=self.query_one("#s-show_loops", Switch).value,
            rescan_seconds=secs)
        try:
            save_settings()
            self.notify("settings saved")
        except OSError as e:
            self.notify(f"could not save: {e}", severity="error")
        self.app.apply_settings()
        self.dismiss()


# ───────────────────────────── app ──────────────────────────────────
CSS = """
Screen { background: $background; }
App.-transparent Screen, App.-transparent DataTable, App.-transparent #stats,
App.-transparent .topbar { background: ansi_default; }

.topbar { height: 1; background: $primary 18%; dock: top; }
.gear { width: 5; padding: 0 2; color: $primary; }
.gear:hover { background: $primary 25%; }
.apptitle { width: 1fr; content-align: center middle; color: $primary 80%; }
.clock { width: 12; content-align: right middle; padding: 0 2; color: $foreground; background: $panel; }

#stats { height: 3; padding: 1 2; background: $background; }
#body { height: 1fr; }
#list { width: 1fr; }
#table { height: auto; max-height: 100%; background: $background; scrollbar-size-vertical: 1; }
#art { height: 1fr; width: 100%; content-align: center middle; color: $primary 14%; overflow: hidden; }
#table > .datatable--header { background: $primary 12%; color: $primary; text-style: bold; }
#table > .datatable--cursor { background: $primary 30%; color: $foreground; text-style: bold; }
#table > .datatable--hover { background: $primary 10%; }
#table > .datatable--even-row { background: $surface 40%; }

#panel { width: 45%; min-width: 74; max-width: 100; display: none; background: $surface; border-left: outer $primary 70%;
         padding: 1 1 1 2; scrollbar-size-vertical: 1; }
#panel.-open { display: block; }
#panel:focus-within { border-left: outer $primary; }
#panel-back { height: 1; min-width: 8; width: 10; margin: 0; padding: 0;
              background: $primary 20%; color: $primary; text-style: bold; }
#panel-back:focus, #panel-back:hover { background: $primary; color: $background; }
#panel-edit { width: 100%; height: 3; margin: 1 0 0 0; background: $primary 20%;
              color: $primary; text-style: bold; }
#panel-edit:focus, #panel-edit:hover { background: $primary; color: $background; }

* { scrollbar-background: $background; scrollbar-color: $primary 35%;
    scrollbar-color-hover: $primary 55%; scrollbar-color-active: $primary; }

Footer { background: $surface; }
FooterKey .footer-key--key { color: $primary; background: $surface; text-style: bold; }
FooterKey .footer-key--description { color: $foreground 85%; }
FooterKey:hover { background: $primary 20%; }

Button { border: none; height: 3; min-width: 8; margin: 0 0 0 1; padding: 0 2;
         background: $panel; color: $foreground; text-style: none; }
Button:hover { background: $primary 25%; }
Button:focus { background: $primary 35%; text-style: bold; }
Button.-active { background: $primary 45%; border: none; }
Button.primary { background: $primary 40%; color: $foreground; text-style: bold; }
Button.primary:focus, Button.primary:hover { background: $primary 60%; }
Button.danger { background: $error 25%; color: $error; }
Button.danger:focus, Button.danger:hover { background: $error 40%; color: $foreground; }
Button:disabled { background: $panel 50%; color: $foreground 35%; }

ModalScreen { align: center middle; background: $background 70%; }
#dialog { width: 74; max-width: 95%; height: auto; max-height: 90%; background: $surface;
          border: round $primary 70%; padding: 0 1; }
#dialog.wide { width: 104; height: 90%; }
#dialog .title { width: 100%; background: $primary 25%; color: $primary; text-style: bold;
                 padding: 0 1; margin-bottom: 1; }
#form { height: auto; max-height: 65vh; }
#dialog.about { width: 84; }
#about-text { padding: 0 2; }
#tasklog { height: 1fr; background: $background; border: round $panel; }
.field-label { color: $foreground 80%; margin-top: 1; }
.hint { color: $foreground 55%; height: auto; }
.error { color: $error; height: auto; margin-top: 1; }
.warning { background: $error 12%; color: $error; border-left: outer $error; padding: 0 1; }
.buttons { height: 3; align-horizontal: right; margin-top: 1; }
Input { background: $background; border: round $panel; padding: 0 1; }
Input:focus { border: round $primary; }
Select > SelectCurrent { background: $background; border: round $panel; }
Select:focus > SelectCurrent { border: round $primary; }
Checkbox { background: $surface; border: none; margin-top: 1; }
ProgressBar Bar > .bar--bar { color: $primary; background: $panel; }
ProgressBar Bar > .bar--complete { color: $success; }

#panel-mac { height: 3; margin-top: 1; }
#panel-mac .rowlabel { width: 11; padding-top: 1; color: $foreground 85%; }
#panel-release { margin-left: 1; }
#panel-open { height: 3; }
#panel-open Button { width: 1fr; min-width: 8; margin-right: 1; }
#panel-release { width: 1fr; min-width: 8; }
App.-narrow #panel { width: 100%; max-width: 100%; }
App.-narrow #list.-behind { display: none; }
#keys { dock: bottom; height: auto; max-height: 4; background: $primary 18%;
        text-wrap: nowrap; link-style: none; link-color: $foreground; link-background-hover: $primary 30%; }
#dialog.drives { width: 96; }
#drive-rows { height: auto; max-height: 60vh; }
.drive-row { height: 3; }
.drive-name { width: 1fr; padding-top: 1; }
.drive-auto { width: 11; padding-top: 1; color: $foreground 85%; }
.drive-row Button { margin-left: 1; width: 12; min-width: 12; }

#settings { padding: 1 4; }
#settings .section { color: $primary; text-style: bold; margin-top: 1;
                     border-bottom: solid $primary 40%; width: 80; }
#settings .row { height: 3; width: 80; margin-top: 1; }
#settings .rowlabel { width: 40; padding-top: 1; color: $foreground 85%; }
#settings Input, #settings Select { width: 40; }
#settings .buttons { width: 80; margin-top: 2; }
#s-header { height: 9; width: 80; margin-bottom: 1; }
#s-icon { width: 16; color: $accent; padding-top: 0; }
#s-name { width: 1fr; padding: 1 0 0 2; }
#s-name Button { margin-top: 1; }
#settings .wide, #settings .about { width: 80; }
#settings .about { margin-top: 1; }
#settings .tips { height: 3; width: 80; margin-top: 1; }
#settings .tips Button { width: 1fr; margin-right: 1; }
#settings .licence { width: 80; margin: 2 0 1 0; content-align: center middle; }
"""

COLUMNS = ("Device", "Name", "Size", "Encrypted", "Encryption", "Filesystem", "Status", "Mountpoint")


class CrypticVaults(App):
    TITLE = "Cryptic Vaults"
    CSS = CSS
    BINDINGS = [
        Binding("space", "details", "Details"),
        Binding("m", "mount", "Mount"),
        Binding("u", "umount", "Unmount"),
        Binding("e,E", "encrypt", "Encrypt"),
        Binding("c", "container", "Container"),
        Binding("f", "format", "Format"),
        Binding("w", "wipe", "Wipe"),
        Binding("r", "rescan", "Rescan"),
        Binding("d", "drives", "Drives", show=MACOS),
        Binding("s", "settings", "Settings"),
        Binding("a", "attach", "Attach container", show=False),
        Binding("l", "log", "Log", show=False),
        Binding("question_mark", "toggle_keys", "Keys", show=False),
        Binding("escape", "hide_keys", "Close keys", show=False),
        Binding("q", "quit", "Quit"),
    ]
    MAIN_ACTIONS = {b.action for b in BINDINGS}

    def __init__(self, readonly: bool):
        super().__init__()
        self.readonly = readonly
        self.devs: dict[str, Dev] = {}
        self.log_lines: list[str] = []
        self._snapshot = ""
        self._rescan_timer = None
        for t in THEMES:
            self.register_theme(t)

    def compose(self) -> ComposeResult:
        yield TopBar(self.TITLE)
        yield Static("", id="stats")
        with Horizontal(id="body"):
            with Vertical(id="list"):
                yield DataTable(cursor_type="row", zebra_stripes=True, id="table")
                yield Static(PADLOCK, id="art")  # fills the space under the drives; gives way as the list grows
            yield DetailsPanel(id="panel")
        yield KeyBar(id="keys")

    def on_mount(self):
        t = self.query_one(DataTable)
        t.add_columns(*COLUMNS)
        self.apply_settings()
        t.focus()
        if self.readonly:
            self.notify("read-only mode (not root) — browsing only", severity="warning", timeout=6)

    def apply_settings(self):
        self.theme = SETTINGS["theme"] if SETTINGS["theme"] in self.available_themes else "cryptic-vaults-mocha"
        self.set_class(SETTINGS["transparent"], "-transparent")
        if self._rescan_timer:
            self._rescan_timer.stop()
            self._rescan_timer = None
        if SETTINGS["rescan_seconds"]:
            self._rescan_timer = self.set_interval(SETTINGS["rescan_seconds"], self.rescan)
        if self.query("#table"):
            self.rescan(force=True)

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        from textual.app import SystemCommand
        yield SystemCommand("Settings", "Open Cryptic Vaults settings", self.action_settings)
        yield SystemCommand("Rescan disks", "Re-read the list of drives", self.action_rescan)
        yield SystemCommand("Command log", "Show every command Cryptic Vaults has run", self.action_log)
        yield SystemCommand("About the Dev", "Who made Cryptic Vaults, and a few words from them",
                            lambda: self.push_screen(AboutScreen()))

    def check_action(self, action: str, parameters) -> bool | None:
        # main-screen hotkeys must not fire underneath a dialog or the settings page
        if action in self.MAIN_ACTIONS and len(self.screen_stack) > 1:
            return False
        return True

    # ── table ──
    def rescan(self, force: bool = False):
        if len(self.screen_stack) > 1 and not force:
            return
        try:
            roots, snap = load_devices()
        except Exception as e:  # noqa: BLE001
            self.notify(f"lsblk failed: {e}", severity="error")
            return
        if not SETTINGS["show_loops"]:
            roots = [r for r in roots if r.type != "loop"]
        if snap == self._snapshot and not force:
            return
        self._snapshot = snap
        self.query_one("#stats", Static).update(stats_markup(roots))
        t = self.query_one(DataTable)
        cur = self.current_key()
        t.clear()
        self.devs.clear()

        def add(d: Dev, prefix: str, last: bool):
            branch = "" if d.depth == 0 else prefix + ("└─ " if last else "├─ ")
            self.devs[d.path] = d
            t.add_row(*self._cells(d, branch), key=d.path)
            child_prefix = "" if d.depth == 0 else prefix + ("\u00a0" * 3 if last else "│\u00a0\u00a0")
            for i, c in enumerate(d.children):
                add(c, child_prefix, i == len(d.children) - 1)
        for r in roots:
            add(r, "", True)
        keys = list(self.devs)
        if cur in keys:
            t.move_cursor(row=keys.index(cur))
        self._refresh_panel()

    def _cells(self, d: Dev, branch: str):
        icon = {"disk": "⛁ ", "loop": "◫ ", "crypt": "⚿ "}.get(d.type, "")
        dev = Text(branch, style=M["overlay0"]) + Text(icon + d.name, style=f"bold {M['mauve']}" if d.depth == 0
                                                       else M["text"])
        if d.type == "disk":
            name = " ".join(x for x in ((d.vendor or "").strip(), d.model or "") if x)
            name += f"  ({d.tran})" if d.tran else ""
        elif d.type == "loop":
            name = Path(d.backing_file).name
        else:
            name = d.label or d.partlabel or ""
        name = name if len(name) <= 30 else name[:29] + "…"
        has_luks = any(x.is_luks for x in d.walk())
        if d.is_luks or d.type == "crypt":
            enc = Text("true", style=f"bold {M['green']}")
        elif d.type in ("disk", "loop") and has_luks:
            enc = Text("partial", style=M["yellow"])
        else:
            enc = Text("false", style=M["overlay1"])
        enc_type = Text(d.enc_label or "—", style=f"bold {M['mauve']}" if d.enc_label else M["overlay0"])
        if d.is_luks:
            inner = d.mapper.fstype if d.mapper else None
            fs = Text(f"{inner} (inside)" if inner else "—", style=M["subtext0"])
        else:
            fs = Text(d.fstype or "—", style=M["subtext0"] if d.fstype else M["overlay0"])
        mnts = [m for m in d.mounts] or ([m for m in d.mapper.all_mounts()] if d.mapper else [])
        if d.is_luks or (d.type == "loop" and d.mapper):
            status = (Text("● mounted", style=M["green"]) if mnts else
                      Text("● unlocked", style=M["teal"]) if d.mapper else Text("● locked", style=M["peach"]))
        elif d.mounts:
            status = Text("● mounted", style=M["green"])
        else:
            status = Text("—", style=M["overlay0"])
        return (dev, Text(name, style=M["subtext1"]), Text(human(int(d.size or 0)), justify="right"),
                enc, enc_type, fs, status, Text(", ".join(mnts) or "—", style=M["text"] if mnts else M["overlay0"]))

    def current_key(self) -> str | None:
        t = self.query_one(DataTable)
        if not t.row_count:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            return None

    def current(self) -> Dev | None:
        k = self.current_key()
        return self.devs.get(k) if k else None

    @on(DataTable.RowHighlighted)
    def _hl(self, _):
        self._refresh_panel()

    @on(DataTable.RowSelected)
    def _selected(self, _):
        self.action_details()

    def _refresh_panel(self):
        panel = self.query_one("#panel")
        if panel.has_class("-open") and (d := self.current()):
            self.query_one("#panel-content", Static).update(
                f"[b $primary]{escape(d.path)}[/]\n\n[$foreground 50%]loading details…[/]")
            self.load_details(d)
            if MACOS:
                m = mac_drive(d)
                self.query_one("#panel-mac").display = m is not None
                self.query_one("#panel-open").display = mac_share(d) is not None
                if m:
                    sw = self.query_one("#panel-auto", Switch)
                    with sw.prevent(Switch.Changed):
                        sw.value = m["auto"]

    @work(thread=True, exclusive=True, group="details")
    def load_details(self, d: Dev):
        try:
            txt = details_markup(d)
        except Exception as e:  # noqa: BLE001
            txt = f"[{M['red']}]error: {escape(str(e))}[/]"
        if self.current() is d:
            self.call_from_thread(self.query_one("#panel-content", Static).update, txt)

    def log_line(self, msg: str):
        self.log_lines.append(msg)

    # ── helpers ──
    def _guard(self, d: Dev | None, destructive: bool = False) -> bool:
        if self.readonly:
            self.notify("needs root — start cryptic-vaults without --no-sudo", severity="error")
            return False
        if d is None:
            self.notify("no drive selected", severity="warning")
            return False
        if destructive and d.is_system:
            self.notify(f"{d.disk.path} is your system disk — refusing", severity="error")
            return False
        if destructive and d.disk.ro:
            self.notify("drive is read-only", severity="error")
            return False
        return True

    def _volume(self, d: Dev) -> Dev:
        """The row to act on for mount/unmount: a mapper acts on its LUKS parent; a disk with
        exactly one usable partition acts on that partition."""
        if d.type == "crypt" and d.parent:
            return d.parent
        if d.type == "disk" and not d.fstype:
            usable = [c for c in d.children if c.is_luks or (c.fstype and c.fstype != "swap")]
            if len(usable) == 1:
                return usable[0]
        return d

    def _run(self, title: str, task, auto_close=False, on_retry=None):
        def done(result):
            self.rescan(force=True)
            if result == "retry" and on_retry:
                on_retry()
        self.push_screen(TaskScreen(title, task, auto_close), done)

    def action_drives(self):
        if not MACOS:
            return self.notify("the drives pane is for Cryptic Vaults on macOS", severity="information")
        self.push_screen(DrivesScreen(), lambda _: self.rescan(force=True))

    @on(Switch.Changed, "#panel-auto")
    def _panel_auto(self, ev: Switch.Changed):
        if (d := self.current()) and (m := mac_drive(d)) and ev.value != m["auto"]:
            mac_request("auto", m["id"], ev.value)

    @on(Button.Pressed, "#open-finder, #open-terminal, #open-copy")
    def _panel_open(self, ev: Button.Pressed):
        d = self.current()
        if not d or not (name := mac_share(d)):
            return self.notify("mount the drive first", severity="warning")
        op = ev.button.id.split("-")[1]
        mac_request(op, name)
        where = f"{os.environ.get('CRYPTIC_VAULTS_MAC_FOLDER', '~/cryptic-vaults')}/{name}"
        self.notify({"finder": f"opening {where} in Finder", "terminal": f"opening a terminal in {where}",
                     "copy": f"copied {where}"}[op])

    @on(Button.Pressed, "#panel-release")
    def _panel_release(self):
        d = self.current()
        if not d or not (m := mac_drive(d)):
            return self.notify("this isn't a drive from the Mac", severity="warning")
        if busy(d.disk):
            return self.notify(f"{m['name']} is unlocked or mounted — unmount it with [u] first", severity="warning")
        mac_request("release", m["id"])
        self.notify(f"{m['name']} handed back to the Mac")
        self.close_panel()

    def action_quit(self):
        """On the Mac the container goes away on quit, so close every drive first (Linux: mounts outlive the TUI)."""
        roots, _ = load_devices() if MACOS else ([], "")
        busy = [r for r in roots if not r.is_system and (r.all_mounts() or any(x.type == "crypt" for x in r.walk()))]
        if not busy:
            return self.exit()

        def run(ctx: Ctx):
            for r in busy:
                umount_tree(ctx, r)
        self.push_screen(TaskScreen("Closing your drives before quitting", run, True),
                         lambda ok: self.exit() if ok is True else self.rescan(force=True))

    def _danger_text(self, d: Dev, what: str) -> str:
        lines = [f"[b]{what}[/b]", "",
                 f"Drive : {escape(d.path)}  ({human(int(d.size))}, {escape(d.disk.model or d.disk.tran or '')})"]
        parts = [x for x in list(d.walk())[1:] if x.type in ("part", "crypt")]
        if parts:
            lines.append("Contains: " + ", ".join(escape(f"{x.name} {x.fstype or ''}".strip()) for x in parts))
        if m := d.all_mounts():
            lines.append("Will unmount: " + escape(", ".join(m)))
        lines += ["", "[b]ALL DATA ON IT WILL BE PERMANENTLY DESTROYED.[/b]"]
        return "\n".join(lines)

    def _fs_choice(self, label="Filesystem"):
        fss = available_fs()
        default = SETTINGS["default_fs"] if SETTINGS["default_fs"] in fss else fss[0]
        return ("select", "fs", label, ([(f, f) for f in fss], default))

    # ── actions ──
    def action_details(self):
        panel = self.query_one("#panel")
        if panel.has_class("-open"):
            return self.close_panel()
        if not self.current():
            return
        panel.add_class("-open")
        self.query_one("#list").add_class("-behind")
        self._refresh_panel()
        self.call_after_refresh(panel.action_focus_back)

    def close_panel(self):
        self.query_one("#panel").remove_class("-open")
        self.query_one("#list").remove_class("-behind")
        self.query_one(DataTable).focus()

    def on_resize(self, event):
        self.set_class(event.size.width < 110, "-narrow")  # too narrow for both: details take the whole width

    @on(Button.Pressed, "#panel-back")
    def _panel_back(self):
        self.close_panel()

    @on(Button.Pressed, "#panel-edit")
    def _panel_edit(self):
        self.action_edit()

    def action_rescan(self):
        self.rescan(force=True)
        self.notify("drives rescanned", timeout=1.5)

    def action_settings(self):
        if not isinstance(self.screen, SettingsScreen):
            self.push_screen(SettingsScreen(), lambda _: self.query_one(DataTable).focus())

    def action_toggle_keys(self):
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_hide_keys(self):
        self.action_hide_help_panel()

    def action_log(self):
        self.push_screen(LogScreen())

    def action_mount(self):
        d = self.current()
        if not self._guard(d):
            return
        v = self._volume(d)
        unlocked = v.mapper is not None
        inner = v.mapper if unlocked else None
        if (inner and inner.all_mounts()) or (not inner and v.mounts):
            self.notify(f"already mounted at {', '.join((inner or v).all_mounts())}", severity="information")
            return
        if not v.is_luks and not unlocked and not v.fstype:
            hint = "pick one of its partitions" if v.type == "disk" and v.children else \
                   "it has no filesystem — use [f] Format or [e] Encrypt first"
            self.notify(f"nothing to mount on {v.path}: {hint}", severity="warning")
            return
        title = f"{'Unlock & mount' if v.is_luks and not unlocked else 'Mount'} {v.path}"
        note = f"{escape(v.label or v.partlabel or '')}  {human(int(v.size))}"

        def ask(mountpoint: str, error: str = "", attempt: int = 1):
            fields = []
            if v.is_luks and not unlocked:
                fields.append(("password", "pw", f"Passphrase for {v.path}", "nometer"))
            if not MACOS:  # on the Mac the folder's name becomes the share's: Cryptic Vaults picks it
                fields.append(("input", "mountpoint", "Mount at (folder is created if missing)", mountpoint))

            def go(r):
                if r is not None:
                    mp = r.get("mountpoint", "").strip() or None
                    self._run(f"Mounting {v.path}", task_mount(v, r.get("pw"), mp), auto_close=True,
                              on_retry=lambda: ask(mp, f"Wrong passphrase (attempt {attempt}) — try again",
                                                   attempt + 1))
            where = (f"\nAppears on the Mac in {escape(os.environ.get('CRYPTIC_VAULTS_MAC_FOLDER', '~/cryptic-vaults'))}/"
                     f"{escape(os.path.basename(mountpoint))}") if MACOS else ""
            self.push_screen(FormScreen(title, fields, ok_label="Mount", note=note + where, error=error), go)
        ask(default_mountpoint(v, inner))

    def action_umount(self):
        d = self.current()
        if not self._guard(d):
            return
        v = self._volume(d)
        if v.type == "disk":
            v = d  # unmount everything on the disk
        if v.has_system_mounts():
            self.notify("refusing to unmount a system volume", severity="error")
            return
        if not v.all_mounts() and not any(x.type == "crypt" for x in v.walk()):
            self.notify(f"{v.path} is not mounted or unlocked", severity="information")
            return
        self._run(f"Unmounting {v.path}", task_umount(v), auto_close=True)

    def action_encrypt(self):
        d = self.current()
        if not self._guard(d, destructive=True):
            return
        if d.type not in ("disk", "part"):
            self.notify("select a drive or partition", severity="warning")
            return
        what = ("Encrypt the WHOLE drive: new partition table + one LUKS2 partition" if d.type == "disk"
                else "Encrypt this partition with LUKS2")

        def go(v):
            if v:
                self._run(f"Encrypting {d.path}", task_encrypt(d, v["pw"], v["fs"], v["label"].strip(), v["keep"]))
        self.push_screen(FormScreen(
            f"Encrypt {d.path}",
            [("password", "pw", "New passphrase (LUKS2 · aes-xts-512 · argon2id)", None),
             ("password2", "pw2", "Repeat passphrase", None),
             self._fs_choice("Filesystem inside"),
             ("input", "label", "Name / label", d.label or "secure"),
             ("checkbox", "keep", "Leave it unlocked & mounted when done", True)],
            warning=self._danger_text(d, what), confirm=d.path, ok_label="Encrypt", danger=True,
            note="Want to keep the files on this drive? Cancel, mount it with [b]m[/b], then press [b]c[/b] "
                 "to create an encrypted container file on it instead — nothing gets erased."), go)

    def action_edit(self, error: str = ""):
        d = self.current()
        if not self._guard(d):
            return
        d = self._volume(d)
        fields, current = [], {}
        if d.type == "part" and d.disk.pttype == "gpt":
            fields.append(("input", "partname", "Partition name (GPT)", d.partlabel or ""))
            current["partname"] = d.partlabel or ""
        if d.is_luks and str(d.fsver) == "2":
            fields.append(("input", "lukslabel", "Encrypted volume label (LUKS)", d.label or ""))
            current["lukslabel"] = d.label or ""
        fsdev = d.mapper if d.is_luks else d if d.fstype in LABELERS else None
        if fsdev and fsdev.fstype in LABELERS and shutil.which(LABELERS[fsdev.fstype][0]):
            fields.append(("input", "fslabel", f"Filesystem label ({fsdev.fstype})", fsdev.label or ""))
            current["fslabel"] = fsdev.label or ""
        if d.is_luks:
            fields += [("password", "curpw", "Current passphrase (only to change it)", None),
                       ("password", "newpw", "New passphrase", None),
                       ("password", "newpw2", "Repeat new passphrase", None)]
        if not fields:
            self.notify("nothing editable here (whole drives have no name; pick a partition)", severity="warning")
            return
        note = "" if not d.is_luks or d.mapper else "Unlock it with [m] first to rename the filesystem inside."

        def go(v):
            if not v:
                return
            changes = {k: v[k].strip() for k in current if v[k].strip() != current[k]}
            if "fslabel" in changes:
                changes.update(fsdev=fsdev.path, fstype=fsdev.fstype)
            if v.get("newpw") or v.get("curpw"):
                if v["newpw"] != v["newpw2"]:
                    return self.notify("new passphrases do not match", severity="error")
                if len(v["newpw"]) < 8 or not v["curpw"]:
                    return self.notify("enter the current passphrase and a new one of 8+ characters",
                                       severity="error")
                changes.update(curpw=v["curpw"], newpw=v["newpw"])
            if not changes:
                return self.notify("nothing changed")
            self._run(f"Editing {d.path}", task_edit(d, changes), auto_close="newpw" not in changes,
                      on_retry=lambda: self.action_edit("Current passphrase was wrong — try again"))
        self.push_screen(FormScreen(f"Edit {d.path}", fields, ok_label="Save", note=note, error=error), go)

    def action_attach(self):
        if not self._guard(object()):
            return
        d = self.current()
        base = next((m for m in (d.all_mounts() if d else []) if m != "[SWAP]"), "")

        def ask(file: str, error: str = "", attempt: int = 1):
            def go(v):
                if not v:
                    return
                f = os.path.expanduser(v["file"].strip())
                if not os.path.isfile(f):
                    return self.notify(f"no such file: {f}", severity="error")
                self._run(f"Opening {f}", task_attach(f, v["pw"]), auto_close=True,
                          on_retry=lambda: ask(f, f"Wrong passphrase (attempt {attempt}) — try again", attempt + 1))
            self.push_screen(FormScreen("Open encrypted container file",
                                        [("input", "file", "Container file", file),
                                         ("password", "pw", "Passphrase", "nometer")],
                                        ok_label="Open", error=error), go)
        ask(f"{base}/vault.luks" if base else "")

    def action_container(self):
        d = self.current()
        if not self._guard(d):
            return
        mnts = [m for m in (d.all_mounts() or (d.mapper.all_mounts() if d.mapper else [])) if m != "[SWAP]"]
        if not mnts:
            self.notify("mount the drive first with [m], then create the container on it", severity="warning")
            return
        mnt = mnts[0]
        st = os.statvfs(mnt)

        def go(v):
            if not v:
                return
            try:
                size = parse_size(v["size"])
            except ValueError as e:
                return self.notify(str(e), severity="error")
            if size < 32 << 20:
                return self.notify("container must be at least 32M", severity="error")
            name = v["file"].strip()
            if not name or "/" in name:
                return self.notify("give a plain file name", severity="error")
            self._run(f"Creating container on {mnt}",
                      task_container(mnt, name, size, v["pw"], v["fs"], v["label"].strip(), v["rand"]))
        self.push_screen(FormScreen(
            f"New encrypted container on {mnt}",
            [("input", "file", f"File name (in {mnt})", "vault.luks"),
             ("input", "size", f"Size, e.g. 1G or 500M  (free: {human(st.f_bavail * st.f_frsize)})", "1G"),
             ("password", "pw", "New passphrase", None),
             ("password2", "pw2", "Repeat passphrase", None),
             self._fs_choice("Filesystem inside"),
             ("input", "label", "Label", "vault"),
             ("checkbox", "rand", "Pre-fill with random data (slower, hides how full it is)", False)],
            ok_label="Create"), go)

    def action_format(self):
        d = self.current()
        if not self._guard(d, destructive=True):
            return
        what = {"disk": "Format the WHOLE drive: new partition table + one partition",
                "crypt": "Re-format the filesystem inside this unlocked encrypted volume"}.get(d.type, "Format partition")

        def go(v):
            if v:
                self._run(f"Formatting {d.path}", task_format(d, v["fs"], v["label"].strip()))
        self.push_screen(FormScreen(
            f"Format {d.path}", [self._fs_choice(), ("input", "label", "Name / label", d.label or "data")],
            warning=self._danger_text(d, what), confirm=d.path, ok_label="Format", danger=True), go)

    def action_wipe(self):
        d = self.current()
        if not self._guard(d, destructive=True):
            return
        if d.type == "crypt":
            return self.notify("select the partition or drive to wipe", severity="warning")
        modes = [("Quick — erase signatures + first/last 32 MiB", "quick"),
                 ("Zeros — overwrite everything (slow)", "zero"),
                 ("Random — full pass of random-looking data (slow)", "random"),
                 ("Discard — TRIM all blocks (SSD, fast, not forensic)", "discard")]

        def go(v):
            if v:
                self._run(f"Wiping {d.path} ({v['mode']})", task_wipe(d, v["mode"]))
        self.push_screen(FormScreen(
            f"Wipe {d.path}", [("select", "mode", "Method", (modes, "quick"))],
            warning=self._danger_text(d, "Wipe drive"), confirm=d.path, ok_label="Wipe", danger=True), go)


def main():
    readonly = "--no-sudo" in sys.argv
    if os.geteuid() != 0 and not readonly:
        if not shutil.which("sudo"):
            sys.exit("Cryptic Vaults needs root (sudo not found). Use --no-sudo to browse read-only.")
        env = [f"{k}={os.environ[k]}" for k in ("TERM", "COLORTERM", "LANG", "DISPLAY", "WAYLAND_DISPLAY",
                                                  "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ]
        os.execvp("sudo", ["sudo", "/usr/bin/env", *env, sys.executable, os.path.abspath(__file__), *sys.argv[1:]])
    if not shutil.which("cryptsetup"):
        sys.exit("cryptsetup not found")
    load_settings()
    CrypticVaults(readonly).run()


if __name__ == "__main__":
    main()
