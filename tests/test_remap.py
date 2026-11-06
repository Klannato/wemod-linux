#!/usr/bin/env python3
"""Remap a cheat from the table's own signature when its address moved.

Real case (Far Cry 5, 19:41): the script asserts 5 bytes at `FC_m64.dll+114B512`,
but in the installed build that instruction sits 0x1050 later — and the ±0x8000
drift search is ambiguous because the 5-byte assert appears twice. The table's
comment carries a 20-byte signature that *is* unique, which is what ctremap uses.

The test maps a real file named `FakeModule.dll` into a child process:
  * garbage at the table's address (0x1000), and the 5-byte assert twice nearby
    (0x1F00 and inside the signature) → the ±0x8000 drift search is ambiguous,
    exactly the failing condition
  * the full 20-byte signature once, at 0x2000, with the assert inside it
  * a script whose assert/patch address is wrong (0x1000)
and asserts the patch lands on the signature-derived address, then restores.

Run: python3 tests/test_remap.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctaa as AA
import ctremap
import ctinject as INJ

SIG = bytes.fromhex("F30F5EF7F3410F59F3F30F59732CF30F587310F30F117310")
ASSERT = bytes.fromhex("F30F587310")
PATCH = b"\x90" * 5

CHILD = r"""
import ctypes, mmap, os, sys, time
path = sys.argv[1]
size = os.path.getsize(path)
fd = os.open(path, os.O_RDWR)
buf = mmap.mmap(fd, size, prot=mmap.PROT_READ | mmap.PROT_WRITE)
print(ctypes.addressof(ctypes.c_char.from_buffer(buf)), flush=True)
time.sleep(30)
"""


def read(pid: int, addr: int, n: int) -> bytes:
    INJ.attach(pid)
    try:
        return INJ.read(pid, addr, n)
    finally:
        INJ.detach(pid)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "FakeModule.dll"
        blob = bytearray(0x8000)
        blob[0x1000:0x1000 + len(ASSERT)] = bytes.fromhex("1122334455")  # wrong bytes
        blob[0x1F00:0x1F00 + len(ASSERT)] = ASSERT            # decoy → drift is ambiguous
        blob[0x2000:0x2000 + len(SIG)] = SIG                  # the real, shifted one
        path.write_bytes(bytes(blob))

        proc = subprocess.Popen([sys.executable, "-c", CHILD, str(path)],
                                stdout=subprocess.PIPE, text=True)
        try:
            base = int(proc.stdout.readline().strip())
        except ValueError:
            print("FAIL child did not report a mapping address")
            proc.kill()
            return 1
        pid = proc.pid
        failures: list[str] = []
        real_point = base + 0x2000 + SIG.find(ASSERT)
        wrong_point = base + 0x1000

        script = f"""[ENABLE]
//{SIG.hex(' ').upper()}
assert({hex(wrong_point)},{ASSERT.hex(' ').upper()})

{hex(wrong_point)}:
  nop 5
[DISABLE]
{hex(wrong_point)}:
  DB {ASSERT.hex(' ').upper()}
"""

        sigs = ctremap.extract_signatures(script)
        longest = max((s["bytes"] for s in sigs), key=len)
        if longest != SIG:
            failures.append(f"signature extraction wrong: {longest.hex(' ')}")

        # the remap engine must find the unique signature in the mapped module
        found = ctremap.remap(pid, script)
        if not found:
            failures.append("remap found nothing")
        else:
            if found["addr"] != real_point:
                failures.append(f"remap pointed at {found['addr']:#x}, expected {real_point:#x}")
            if found["source"] != "hex-comment" or found["signature_len"] != len(SIG):
                failures.append(f"wrong evidence: {found['source']} {found['signature_len']}B")

        # and the whole path must patch the remapped address, then restore it
        handle = AA.auto_enable(pid, script, bases={})
        patched = read(pid, real_point, 5)
        if patched != PATCH:
            failures.append(f"patch not applied at remapped point: {patched.hex(' ')}")
        if not handle.get("remap"):
            failures.append("handle does not record the remap")
        AA.auto_disable(pid, handle)
        if read(pid, real_point, 5) != ASSERT:
            failures.append("disable did not restore the remapped bytes")

        proc.kill()

    for f in failures:
        print("FAIL:", f)
    print("PASS: remap from table signature" if not failures else f"FAIL: {len(failures)} case(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
