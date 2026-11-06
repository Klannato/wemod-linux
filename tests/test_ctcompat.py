#!/usr/bin/env python3
"""Score a table against a build on disk — no game launch, no crash.

Builds a tiny synthetic PE (one .text section at RVA 0x1000) and three tables:
  * assert bytes correct            → "matches this build", remap_safe
  * every assert off by the same
    amount                          → "different build", remap_safe (one drift)
  * asserts off by two different
    amounts                         → "different build (inconsistent drift)",
                                      NOT remap_safe — the Far Cry 5 case, where
                                      a single shift is meaningless and patching
                                      killed the game

Run: python3 tests/test_ctcompat.py
"""
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctcompat

TEXT_RVA = 0x1000
TEXT_RAW = 0x200


def make_pe(path: Path, payload: bytes, raw_size: int = 0x400) -> None:
    buf = bytearray(TEXT_RAW + raw_size)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)
    buf[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", buf, 0x86, 1)          # NumberOfSections
    struct.pack_into("<H", buf, 0x94, 0xE0)       # SizeOfOptionalHeader
    sec = 0x80 + 24 + 0xE0
    buf[sec:sec + 8] = b".text\0\0\0"
    struct.pack_into("<I", buf, sec + 8, 0x1000)  # VirtualSize
    struct.pack_into("<I", buf, sec + 12, TEXT_RVA)
    struct.pack_into("<I", buf, sec + 16, raw_size)
    struct.pack_into("<I", buf, sec + 20, TEXT_RAW)
    buf[TEXT_RAW:TEXT_RAW + len(payload)] = payload
    path.write_bytes(bytes(buf))


def table(path: Path, asserts: list[tuple[int, str]]) -> None:
    body = "[ENABLE]\n" + "\n".join(
        f"assert(Fake.dll+{rva:X},{data})\nFake.dll+{rva:X}:\n  nop 2\n" for rva, data in asserts)
    path.write_text(body + "\n[DISABLE]\n")


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        payload = bytearray(0x400)
        payload[0x10:0x15] = bytes.fromhex("F30F587310")      # at RVA 0x1010
        payload[0x100:0x105] = bytes.fromhex("0F5757F690")    # at RVA 0x1100
        make_pe(tmp / "Fake.dll", bytes(payload))
        roots = [tmp]

        good = tmp / "good.ct"
        table(good, [(0x1010, "F3 0F 58 73 10"), (0x1100, "0F 57 57 F6 90")])
        r = ctcompat.score_table(good, roots)
        if r["matched"] != r["checked"] or r["verdict"] != "matches this build":
            failures.append(f"correct table scored {r['matched']}/{r['checked']}: {r['verdict']}")
        if not r.get("remap_safe"):
            failures.append("correct table should be remap_safe")

        # uniformly shifted by 0x40 → the instruction genuinely moved
        shifted = tmp / "shifted.ct"
        table(shifted, [(0x1050, "F3 0F 58 73 10")])
        r = ctcompat.score_table(shifted, roots)
        if r["verdict"] == "matches this build" or r["consistent_drift"] != "-0x40":
            failures.append(f"uniform drift not reported: {r}")

        # two different drifts → remapping cannot be trusted (Far Cry 5)
        inconsistent = tmp / "inconsistent.ct"
        # second instruction lives 0x30 before where the table expects it: a
        # different amount than the -0x40 above → non-uniform drift
        payload[0x120:0x125] = bytes.fromhex("660F57C090")   # unique bytes
        make_pe(tmp / "Fake.dll", bytes(payload))
        table(inconsistent, [(0x1050, "F3 0F 58 73 10"),
                             (0x1150, "66 0F 57 C0 90")])
        r = ctcompat.score_table(inconsistent, roots)
        if "inconsistent" not in r["verdict"]:
            failures.append(f"inconsistent drift not flagged: {r['verdict']}")
        if r.get("remap_safe"):
            failures.append("inconsistent drift marked remap_safe")

    for f in failures:
        print("FAIL:", f)
    print("PASS: table/build compatibility" if not failures else f"FAIL: {len(failures)} case(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
