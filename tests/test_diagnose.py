#!/usr/bin/env python3
"""Launch transcripts must produce actionable diagnoses, not just noise.

Each case here is a real failure seen on this machine (Far Cry 5's GLX BadAlloc,
the 64-bit-shim-in-a-32-bit-process warning, a Steam client that wasn't running).
Freetype chatter must NOT be reported as a missing redistributable.

Run: python3 tests/test_diagnose.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctlaunch as L

CASES = [
    ("X Error of failed request:  BadAlloc (insufficient resources for operation)\n"
     "  Major opcode of failed request:  149 (GLX)\n"
     "  Minor opcode of failed request:  5 (X_GLXMakeCurrent)\n",
     "graphics context creation failed"),
    ("ERROR: ld.so: object '/home/geo/wemod-linux/native/ptrace_allow.so' from "
     "LD_PRELOAD cannot be preloaded (wrong ELF class: ELFCLASS64): ignored.\n",
     "32-bit"),
    ("err:steamclient:steamclient_init_registry Failed to connect to Steam\n",
     "Steam client was not reachable"),
    ("Wine cannot find the FreeType font library.  To enable Wine to\n"
     "use TrueType fonts please install a version of FreeType greater than\n"
     "or equal to 2.0.5.\n", None),
    ("err:module:import_dll Library msvcp140.dll (which is needed by "
     "Z:\\game\\thing.exe) not found\n", "redistributable is missing"),
]


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (text, expect) in enumerate(CASES):
            p = Path(tmp) / f"case{i}.log"
            p.write_text(text, encoding="utf-8")
            hints = L.diagnose(p)
            joined = " | ".join(hints)
            if expect is None and hints:
                failures.append(f"case {i}: expected no diagnosis, got {hints}")
            elif expect is not None and expect not in joined:
                failures.append(f"case {i}: expected '{expect}', got {hints or '[]'}")

    for f in failures:
        print("FAIL:", f)
    print("PASS: diagnosis rules" if not failures else f"FAIL: {len(failures)} case(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
