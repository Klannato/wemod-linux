#!/usr/bin/env python3
"""Check that the tracer shim loads inside a real Wine process (no game needed).

Runs Proton's own bundled wine with a throwaway prefix and builtin `cmd`, then
asks two questions:
  1. is native/ptrace_allow.so mapped into the Wine process?  (LD_PRELOAD survived)
  2. can we read its memory?                                 (PR_SET_PTRACER took effect)

Answers the two assumptions behind "Launch & attach" without launching a game.

Run: python3 tests/test_wine_preload.py
"""
import os
import subprocess
import sys
import time
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctlaunch as L

# CT_SHIM lets the test exercise an alternative build (used to prove which prctl
# calls are actually load-bearing).
SHIM = Path(os.environ.get("CT_SHIM") or (ROOT / "native" / "ptrace_allow.so"))
PREFIX = Path.home() / ".hermes" / "cache" / "scratch" / "wineprefix_probe"
WINE = Path.home() / ".steam" / "steam" / "steamapps" / "common" / "Proton - Experimental" / "files" / "bin" / "wine"


def wine_procs() -> list[int]:
    """PIDs with the shim mapped in — Wine's PE processes are the interesting ones."""
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            maps = (entry / "maps").read_text(errors="replace")
        except OSError:
            continue
        if SHIM.name in maps:
            out.append(int(entry.name))
    return out


def main() -> int:
    if not WINE.exists():
        print(f"no wine at {WINE}")
        return 2
    if not SHIM.exists():
        print(f"missing shim {SHIM}")
        return 2
    PREFIX.mkdir(parents=True, exist_ok=True)
    before = set(wine_procs())
    env = dict(os.environ,
               WINEPREFIX=str(PREFIX),
               WINEDEBUG="-all",
               LD_PRELOAD=str(SHIM),
               CT_TRACER_PID=str(os.getpid()),
               DISPLAY=os.environ.get("DISPLAY", ":1"))
    # `ping` keeps the PE process alive long enough to inspect.
    proc = subprocess.Popen([str(WINE), "cmd", "/c", "ping", "-n", "25", "127.0.0.1"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    found = None
    deadline = time.time() + 120
    while time.time() < deadline:
        # Prefer a PE process (cmd.exe / ping.exe) over the wine loader itself.
        candidates = []
        for pid in wine_procs():
            if pid in before:          # a leftover from an earlier run: stale tracer
                continue
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if cmd.strip():
                candidates.append((pid, cmd))
        if candidates:
            pe = [c for c in candidates if ".exe" in c[1].lower() or c[1].rstrip().endswith("cmd")]
            found = (pe or candidates)[0][0]
            break
        if proc.poll() is not None:
            break
        time.sleep(1.0)

    if found is None:
        print("shim NOT found in any wine process")
        proc.kill()
        return 1

    ok, detail = L.trace_ok(found)
    print(f"shim mapped into wine pid {found}; memory access: {detail}")
    proc.kill()
    subprocess.run([str(WINE.parent / "wineserver"), "-k"], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
