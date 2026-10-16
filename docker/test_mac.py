#!/usr/bin/env python3
"""End-to-end check of the macOS bridge without sudo or a real stick.

    /usr/bin/python3 docker/test_mac.py <luks2-ext4.img> <passphrase>

Attaches the image as a Mac disk (hdiutil), plugs it into the running `cryptic` VM, unlocks and mounts it with
cryptic.py's own code in macOS mode, checks the Mac sees it over SMB, unmounts in cryptic's order, unplugs.
The VM must be running (`colima start cryptic`); the image is never formatted or written outside the stick.
"""
import importlib.util
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cm", HERE / "cryptic-mac.py")
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)

img, pw = sys.argv[1], sys.argv[2]
tmp = Path(tempfile.mkdtemp(dir=Path(img).resolve().parent))  # under $HOME, so the VM sees it
cm.CONFIG, cm.CTL = tmp / "config", tmp / "config" / "ctl"
(cm.CTL / "shares").mkdir(parents=True)

IN_CONTAINER = r'''
import sys; sys.path.insert(0, "/app"); import cryptic as c
ctx = c.Ctx(print, lambda f: None)
dev = next(d for r in c.load_devices()[0] for d in r.walk() if d.is_luks)
assert all(not r.name.startswith(("vd", "sr")) for r in c.load_devices()[0]), "VM disks must be hidden"
if sys.argv[1] == "mount":
    c.task_mount(dev, sys.argv[2], "/media/CTEST")(ctx)
else:  # quit the TUI with the stick still mounted: it must close everything first
    import asyncio
    async def quit_tui():
        app = c.Cryptic(False)
        async with app.run_test() as pilot:
            await pilot.press("q")
            for _ in range(300):
                if app.return_code is not None:
                    return
                await pilot.pause(0.1)
            raise SystemExit("TUI did not quit")
    asyncio.run(quit_tui())
    left = [m for r in c.load_devices()[0] for m in r.all_mounts()]
    assert not left and not c.glob("/dev/mapper/cryptic_*"), f"left open: {left}"
'''


def cryptic(*args):
    return cm.as_user([*cm.DOCKER, "exec", "-e", "CRYPTIC_HOST=macos", "-e", "SUDO_USER=cryptic", "cryptic",
                       "python3", "-c", IN_CONTAINER, *args], check=False, capture_output=True, text=True)


disk = subprocess.run(["hdiutil", "attach", "-nomount", "-imagekey", "diskimage-class=CRawDiskImage", img],
                      capture_output=True, text=True, check=True).stdout.split()[0].removeprefix("/dev/")
bridge = cm.Bridge(cm.secrets.token_urlsafe(24))
bridge.base = tmp / "mnt"
bridge.sync_disks = lambda: None  # only the test image, never a real stick
try:
    cm.compose("up", "-d", "--force-recreate", env={"CRYPTIC_SMB_PW": bridge.smb_pw},
               capture_output=True)
    bridge.info[disk] = cm.usb_identity(disk)  # a disk image: no USB serial, so the id is a name+size hash
    bridge.plug(disk)
    bridge.start()
    time.sleep(2)
    r = cryptic("mount", pw)
    assert r.returncode == 0, r.stdout + r.stderr
    for _ in range(40):
        if (bridge.base / "CTEST" / "lost+found").exists():
            break
        time.sleep(0.25)
    else:
        raise AssertionError("share never appeared on the Mac")
    (bridge.base / "CTEST" / "from-test.txt").write_text("ok\n")
    r = cryptic("quit")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (bridge.base / "CTEST").exists(), "Mac mount left behind"
    print("PASS: plug → unlock → SMB on the Mac → write → quit closes it in order")
finally:
    bridge.stop.set()
    if bridge.is_alive():
        bridge.join(timeout=5)
    for name in list(bridge.mounted):
        bridge.release(name, force=True)
    cm.close_all_in_vm()
    for d in list(bridge.disks):
        bridge.unplug(d)
    cm.compose("down", check=False, capture_output=True)
    subprocess.run(["hdiutil", "detach", f"/dev/{disk}"], capture_output=True)
