#!/usr/bin/env python3
"""Regenerate the README screenshots (SVG) from the real TUI, with made-up drives.

    docker build -f docker/Dockerfile -t cryptic-vaults .
    docker run --rm -v "$PWD:/src" -w /src --entrypoint python3 cryptic-vaults assets/screenshots.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
OUT = Path("assets")
G = 1 << 30


def dev(name, typ, size, children=(), **kw):
    raw = dict(name=name, kname=name, path=f"/dev/{name}" if typ != "crypt" else f"/dev/mapper/{name}", type=typ,
               size=str(int(size)), fstype=None, fsver=None, label=None, uuid=None, model=None, serial=None,
               vendor=None, tran=None, rota=False, rm=False, ro=False, hotplug=False, mountpoints=[None],
               pkname=None, pttype=None, partlabel=None, wwn=None)
    raw.update(kw)
    if children:
        raw["children"] = list(children)
    return raw


DRIVES = {"blockdevices": [
    dev("sda", "disk", 58.2 * G, [
        dev("sda1", "part", 58.2 * G, [
            dev("cryptic-vaults_sda1", "crypt", 58.2 * G, fstype="ext4", fsver="1.0", label="Vault",
                uuid="3f1c9a2e-7b4d-4e8a-9c21-5d6e7f8a9b0c", mountpoints=["/run/media/you/Vault"])],
            fstype="crypto_LUKS", fsver="2", label="Vault", uuid="8a7b6c5d-4e3f-4a1b-9c8d-7e6f5a4b3c2d",
            partlabel="Vault")],
        model="Ultra Fit", vendor="SanDisk", serial="4C530001230915112233", tran="usb", rm=True, hotplug=True,
        pttype="gpt"),
    dev("sdb", "disk", 931.5 * G, [
        dev("sdb1", "part", 931.5 * G, fstype="exfat", fsver="1.0", label="Photos", uuid="64A1-2B3C")],
        model="Portable SSD T7", vendor="Samsung", serial="S6XNNS0T912345", tran="usb", hotplug=True, pttype="gpt"),
    dev("sdc", "disk", 14.9 * G, [
        dev("sdc1", "part", 14.9 * G, fstype="crypto_LUKS", fsver="2", label="Backup",
            uuid="b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e")],
        model="DataTraveler", vendor="Kingston", serial="E0D55EA573F1", tran="usb", rm=True, hotplug=True,
        pttype="gpt"),
]}

LUKS = """LUKS header information
Version:       	2
Epoch:         	3
Metadata area: 	16384 [bytes]
Keyslots area: 	16744448 [bytes]
UUID:          	8a7b6c5d-4e3f-4a1b-9c8d-7e6f5a4b3c2d
Label:         	Vault
Data segments:
  0: crypt
	cipher: aes-xts-plain64
	sector: 4096 [bytes]
Keyslots:
  0: luks2
	Key:        512 bits
	PBKDF:      argon2id
	Memory:     1048576
	Hash:       sha512""".splitlines()


def fake_lsblk(real):
    def run(cmd, *a, **kw):
        if cmd and cmd[0] == "lsblk":
            return type("P", (), {"stdout": json.dumps(DRIVES), "returncode": 0})()
        return real(cmd, *a, **kw)
    return run


async def shoot(macos: bool):
    import cryptic_vaults as c
    if macos:  # inside the Mac's VM sticks are plain SCSI disks, mounted under /media
        for d in DRIVES["blockdevices"]:
            d["tran"] = None
        DRIVES["blockdevices"][0]["children"][0]["children"][0]["mountpoints"] = ["/media/Vault"]
    c.subprocess.run = fake_lsblk(c.subprocess.run)
    c.usb_info = lambda kname: {"busnum": "3", "devnum": "7", "idVendor": "0781", "idProduct": "5583",
                                "manufacturer": "SanDisk", "product": "Ultra Fit", "speed": "5000",
                                "version": "3.20", "driver": "uas", "serial": "4C530001230915112233"}
    c.luks_dump = lambda path: LUKS
    c.health = lambda disk: [("SMART", "[#a6e3a1]PASSED[/]")] if not macos else \
        [("SMART", "[#7f849c]not available on macOS[/]")]
    c.Ctx.settle = lambda self: None
    app = c.CrypticVaults(False)
    async with app.run_test(size=(150, 34)) as pilot:
        await pilot.pause(0.5)
        prefix = "mac-" if macos else ""
        if not macos:
            app.save_screenshot(f"{prefix}main.svg", str(OUT))
            await pilot.press("down", "down")         # the unlocked Vault
            await pilot.press("space")
            await pilot.pause(1.0)
            app.save_screenshot("details.svg", str(OUT))
        else:
            Path("/run/cryptic-vaults").mkdir(parents=True, exist_ok=True)
            Path("/run/cryptic-vaults/drives.json").write_text(json.dumps([
                {"id": "4C530001230915112233", "name": "SanDisk Ultra Fit", "size": int(58.2 * G),
                 "auto": True, "where": "cryptic-vaults"},
                {"id": "E0D55EA573F1", "name": "Kingston DataTraveler", "size": int(14.9 * G),
                 "auto": True, "where": "cryptic-vaults"},
                {"id": "S6XNNS0T912345", "name": "Samsung Portable SSD T7", "size": int(931.5 * G),
                 "auto": False, "where": "mac"}]))
            await pilot.press("d")
            await pilot.pause(1.0)
            app.save_screenshot("mac-drives.svg", str(OUT))


if __name__ == "__main__":
    if len(sys.argv) == 1:  # each mode needs a fresh import: CRYPTIC_VAULTS_HOST is read at import time
        for mode in ("linux", "macos"):
            env = {**os.environ, **({"CRYPTIC_VAULTS_HOST": "macos"} if mode == "macos" else {})}
            os.spawnve(os.P_WAIT, sys.executable, [sys.executable, __file__, mode], env)
    else:
        asyncio.run(shoot(sys.argv[1] == "macos"))
