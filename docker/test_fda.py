#!/usr/bin/env python3
"""Check of the Mac wrapper's Full Disk Access message."""
import importlib.util
import re
import sys
from pathlib import Path

HELP = """NAME
  test_fda.py - check the Mac wrapper's Full Disk Access message
  Author: Cryptic Vaults

USAGE
  python3 docker/test_fda.py

ARGS:

  --help           shows this menu

EXAMPLES:

  python3 docker/test_fda.py

  Run on the Mac after changing how a denied drive is reported."""


def load_wrapper():
    spec = importlib.util.spec_from_file_location("wrapper", Path(__file__).with_name("cryptic-vaults-mac.py"))
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    return wrapper


def check_message(w):
    msg = w.access_denied_message(["/bin/sh", "/nonexistent/herdr"])
    assert "add: /bin/sh." in msg and "stopped first: /nonexistent/herdr." in msg, msg
    assert "your terminal and any multiplexer" in w.access_denied_message([]), "no apps found"


def check_probe(w):
    real_open = w.os.open
    try:
        w.os.open = lambda *_: (_ for _ in ()).throw(PermissionError(1, "denied"))
        assert not w.has_disk_access(), "a denial must count as no access"
        w.os.open = lambda *_: (_ for _ in ()).throw(FileNotFoundError(2, "gone"))
        assert w.has_disk_access(), "other failures must not block the app"
    finally:
        w.os.open = real_open


def check_links(w):
    source = Path(__file__).parent.parent.joinpath("cryptic_vaults.py").read_text()
    url = re.search(r'^RELEASES = "(.+)"$', source, re.M).group(1)
    assert url.startswith(w.LINKS), f"{url} would not open on a Mac"


def check_ancestors(w):
    apps = w.ancestor_apps()
    assert not any(a.startswith(("/usr/", "/bin/", "/sbin/", "/System/")) for a in apps), apps
    return apps


def main():
    if sys.argv[1:] == ["--help"]:
        return print(HELP)
    if sys.argv[1:]:
        sys.exit("test_fda.py takes no arguments; see --help")
    print("checking the Full Disk Access message")
    w = load_wrapper()
    try:
        check_message(w)
        check_probe(w)
        check_links(w)
        apps = check_ancestors(w)
    except AssertionError as e:
        sys.exit(f"FAIL: {e}")
    print("PASS:", apps)


if __name__ == "__main__":
    main()
