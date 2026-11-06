#!/usr/bin/env python3
"""Finding the game must not match Steam's launch-options shell.

Regression for the 19:26 Far Cry 5 report: Steam runs the launch options as
`/bin/sh -c 'LD_PRELOAD=… %command%'`, whose command line contains the game path.
Matching on "any cmdline mentioning the exe" won the race against the actual game
and returned a process with no shim — reported as "no shim" + EPERM.

Here we create both shapes: a shell whose cmdline mentions the exe (must be
ignored) and a real executable with the game's name (must be found).

Run: python3 tests/test_game_pid.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctlaunch as L

NAME = "FakeGame.exe"


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / NAME
        shutil.copy2("/bin/sleep", fake)
        os.chmod(fake, 0o755)

        # 1. a real process whose comm is the game name
        game = subprocess.Popen([str(fake), "30"])
        time.sleep(0.4)
        hits = L.pids_for_cmdline(NAME.lower())
        if game.pid not in hits:
            failures.append(f"real process {game.pid} not found; got {hits}")

        # 2. Steam's wrapper shell: cmdline mentions the exe, comm is 'sh'
        shell = subprocess.Popen(["/bin/sh", "-c", f"sleep 30 # {fake}"])
        time.sleep(0.4)
        hits = L.pids_for_cmdline(NAME.lower())
        if shell.pid in hits:
            failures.append(f"wrapper shell {shell.pid} was treated as the game")

        found = L.game_pid(fake, "FakeGame", exclude=os.getpid())
        if not found or found[0] != game.pid:
            failures.append(f"game_pid picked {found} instead of {(game.pid, 'cmdline')}")

        game.kill()
        shell.kill()

    for f in failures:
        print("FAIL:", f)
    print("PASS: game detection" if not failures else f"FAIL: {len(failures)} case(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
