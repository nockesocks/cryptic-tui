#!/usr/bin/env python3
"""Headless check of the macOS drives pane and details controls, inside the image (no VM disks needed):

    docker --context colima-cryptic-vaults run --rm -v "$PWD:/src:ro" -e CRYPTIC_VAULTS_HOST=macos --entrypoint python3 \
        cryptic-vaults:latest /src/docker/test_tui.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
import cryptic_vaults as c  # noqa: E402

state = Path("/run/cryptic-vaults/drives.json")
state.parent.mkdir(parents=True, exist_ok=True)
state.write_text(json.dumps([
    {"id": "AA11", "name": "SanDisk Ultra", "size": 64 << 30, "auto": True, "where": "cryptic-vaults"},
    {"id": "BB22", "name": "Kingston Copy", "size": 32 << 30, "auto": False, "where": "mac"},
]))
c.CTL = Path("/tmp/ctl")
c.load_devices = lambda: ([], "")  # no disks in this container; release looks a drive up by serial


def requests():
    return [json.loads(p.read_text()) for p in sorted((c.CTL / "requests").glob("*.json"))]


async def main():
    app = c.CrypticVaults(False)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("d")
        await pilot.pause(0.5)
        screen = app.screen
        assert isinstance(screen, c.DrivesScreen), screen
        assert len(screen.query(".drive-row")) == 2
        await pilot.click("#act-1")            # Load the Mac's drive
        await pilot.click("#auto-0")           # turn auto-load off for the Cryptic Vaults one
        await pilot.click("#act-0")            # release it
        await pilot.pause(0.3)
        got = [(r["op"], r["id"], r["value"]) for r in requests()]
        assert got == [("load", "BB22", None), ("auto", "AA11", False), ("release", "AA11", None)], got
        state.write_text(json.dumps([]))       # wrapper says: nothing plugged in
        await pilot.pause(1.5)
        assert len(screen.query(".drive-row")) == 0
        await pilot.press("escape")
    print("PASS: drives pane lists, loads, toggles auto-load, releases, and follows the wrapper's list")


asyncio.run(main())
