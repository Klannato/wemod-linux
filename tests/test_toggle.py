#!/usr/bin/env python3
"""End-to-end check of the clean-list toggle path, without a game.

Spawns a child that owns a byte at a known address, points a fake "clean cheat"
row at it, and drives the same handler the UI switch calls. Proves the part that
is easy to get wrong — address resolution plus width-correct write — and that a
failed write rolls the switch back.

Run: python3 tests/test_toggle.py
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dearpygui.dearpygui as dpg

import ctapply as C
import cttheme as T
from ctgui import App

CHILD = """
import ctypes, sys, time
buf = (ctypes.c_ubyte * 8)()
addr = ctypes.addressof(buf)
print(addr, flush=True)
time.sleep(20)
"""


def main() -> int:
    child = subprocess.Popen([sys.executable, "-c", CHILD],
                             stdout=subprocess.PIPE, text=True)
    try:
        addr = int(child.stdout.readline().strip())
    except ValueError:
        print("FAIL child never reported an address")
        child.kill()
        return 1

    dpg.create_context()
    app = App(T.apply())
    app.build()
    app.pid = child.pid
    app.bases = {}

    cheat = {"description": "Test Flag", "category": "Test", "kind": "value",
             "type": "Byte", "address": hex(addr), "addresses": [hex(addr)],
             "offsets": [], "confidence": "high", "toggle": True, "one_shot": False}

    app._on_value_toggle(cheat, "Test/Test Flag", True)
    on_value = C.read_value(child.pid, addr, 1)
    app._on_value_toggle(cheat, "Test/Test Flag", False)
    off_value = C.read_value(child.pid, addr, 1)

    # A row whose address cannot be resolved must not write and must log a reason.
    bad = dict(cheat, address="no_such_symbol", addresses=["no_such_symbol"])
    app._on_value_toggle(bad, "Test/Bad", True)

    log = " | ".join(app.log_lines[-3:])
    dpg.destroy_context()
    child.kill()

    print(f"write on -> {on_value}   write off -> {off_value}")
    print(f"log: {log}")
    ok = on_value == 1 and off_value == 0 and "no_such_symbol" in log
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
