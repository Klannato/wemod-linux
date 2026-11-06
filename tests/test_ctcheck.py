#!/usr/bin/env python3
"""Offline checks for ctcheck's decision logic (no network).

Learned the hard way against the real API:
  * "Far Cry 5" finds nothing; "FarCry5" finds the tables → queries() must try both
  * `FarCryNewDawn.CT` must not count as a Far Cry 5 table → matches_game()
  * an error body ("Not Found") must never compare as a date → ISO validation
  * only a strictly newer commit counts

Run: python3 tests/test_ctcheck.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctcheck


def main() -> int:
    failures = []

    q = ctcheck.queries("Far Cry 5")
    if "FarCry5" not in q:
        failures.append(f"queries() misses the compact form: {q}")

    if not ctcheck.matches_game("FarCry5.CT", "Far Cry 5"):
        failures.append("FarCry5.CT rejected for Far Cry 5")
    if ctcheck.matches_game("FarCryNewDawn.CT", "Far Cry 5"):
        failures.append("FarCryNewDawn.CT accepted for Far Cry 5")
    if ctcheck.matches_game("NMS.CT", "No Man's Sky"):
        failures.append("acronym table accepted for a different name")

    local = [{"name": "old.CT", "mtime": 0, "size": 1, "path": Path("old.CT")}]
    online = [
        {"repo": "a/b", "path": "FarCry5.CT", "date": "2025-11-30T04:32:54Z"},
        {"repo": "a/c", "path": "FarCry5.CT", "date": None},
        {"repo": "a/d", "path": "FarCry5.CT", "date": '{\n  "message": "Not Found"\n}'},
    ]
    verdict = ctcheck.assess(local, online, "2026-10-25T00:00:00+00:00")
    if verdict["has_newer"]:
        failures.append(f"older/undated hits reported as newer: {verdict['newer']}")

    newer = ctcheck.assess(local, [{"repo": "a/b", "path": "x.CT",
                                    "date": "2026-11-01T00:00:00Z"}],
                           "2026-10-25T00:00:00+00:00")
    if not newer["has_newer"]:
        failures.append("a genuinely newer commit was not detected")

    for f in failures:
        print("FAIL:", f)
    print("PASS: table-freshness logic" if not failures else f"FAIL: {len(failures)} case(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
