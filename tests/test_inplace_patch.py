#!/usr/bin/env python3
"""Direct-address AA scripts must patch in place, and undo cleanly.

Far Cry 5's table has no `aobscanmodule` at all — it does:

    [ENABLE]
    assert(FC_m64.dll+2C5CE54,F3 0F 58 73 10)
    FC_m64.dll+2C5CE54:
      xorps xmm6,xmm6
      nop 2
    [DISABLE]
    FC_m64.dll+2C5CE54:
      DB F3 0F 58 73 10

That used to raise "script has no AOB scan". This test drives the new in-place
path against a child process with a writable+RX buffer at a known address: apply,
verify the bytes changed, disable, verify they came back, and confirm a wrong
assert is refused instead of patching blind.

Run: python3 tests/test_inplace_patch.py
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctaa as AA
import ctapply as C
import ctinject as INJ

CHILD = r"""
import ctypes, sys, time, mmap
# RWX page so both the report address and the patch target are usable
page = mmap.mmap(-1, 16384, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
ORIG = bytes.fromhex("F30F58731090909090909090")
page.write(ORIG)                       # at offset 0
DRIFT = bytes.fromhex("F30F5973109090")   # mulss, only here
page[0x2000:0x2000+len(DRIFT)] = DRIFT  # the "other build" location
addr = ctypes.addressof(ctypes.c_char.from_buffer(page))
print(addr, flush=True)
time.sleep(30)
"""

DRIFT = bytes.fromhex("F30F597310")   # bytes the drift case expects at addr-0x40

SCRIPT = """[ENABLE]
assert({addr},F3 0F 58 73 10)

{addr}:
  nop 5

[DISABLE]
{addr}:
  DB F3 0F 58 73 10
"""


def read(pid: int, addr: int) -> bytes:
    INJ.attach(pid)
    try:
        return INJ.read(pid, addr, 5)
    finally:
        INJ.detach(pid)


def main() -> int:
    child = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE, text=True)
    try:
        addr = int(child.stdout.readline().strip())
    except ValueError:
        print("FAIL child never reported an address")
        child.kill()
        return 1
    pid = child.pid
    failures = []

    before = read(pid, addr)
    handle = AA.auto_enable(pid, SCRIPT.format(addr=hex(addr)), bases={})
    after = read(pid, addr)
    if handle.get("type") != "inplace":
        failures.append(f"wrong handle type {handle.get('type')!r}")
    if after != b"\x90" * 5:
        failures.append(f"patch not applied: {after.hex(' ')}")

    AA.auto_disable(pid, handle)
    restored = read(pid, addr)
    if restored != before:
        failures.append(f"disable did not restore: {restored.hex(' ')} != {before.hex(' ')}")

    # table drift: table expects the instruction 0x40 earlier than this "build"
    time.sleep(0.2)
    drifted = (SCRIPT.format(addr=hex(addr + 0x1FC0))
               .replace("F3 0F 58 73 10", "F3 0F 59 73 10"))
    handle2 = AA.auto_enable(pid, drifted, bases={})
    if not handle2.get("shift"):
        failures.append(f"no drift detected (shift={handle2.get('shift')})")
    AA.auto_disable(pid, handle2)
    if read(pid, addr + 0x2000) != DRIFT[:5]:
        failures.append("drifted patch did not restore the bytes at the shifted address")

    # wrong assert must refuse, not patch
    bad = SCRIPT.format(addr=hex(addr)).replace("F3 0F 58 73 10)\n\n", "AA BB CC DD EE)\n\n")
    try:
        AA.auto_enable(pid, bad, bases={})
        failures.append("bad assert was accepted")
    except ValueError as exc:
        if "assert failed" not in str(exc):
            failures.append(f"unexpected error: {exc}")

    # an AOB-less, block-less script should say so plainly
    try:
        AA.auto_enable(pid, "[ENABLE]\nnop\n[DISABLE]\n", bases={})
        failures.append("empty script accepted")
    except ValueError as exc:
        if "neither an AOB scan nor a direct-address block" not in str(exc):
            failures.append(f"unexpected error: {exc}")

    child.kill()
    print(f"before={before.hex(' ')} after={after.hex(' ')} restored={restored.hex(' ')}")
    for f in failures:
        print("FAIL:", f)
    print("PASS: in-place patches" if not failures else f"FAIL: {len(failures)} case(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
