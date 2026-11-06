#!/usr/bin/env python3
"""force_stop must kill a wine tree that a polite request leaves behind.

Mirrors the Far Cry 5 state: a wine process plus wineserver holding a Proton
prefix (`compatdata/<appid>/pfx/`) that Steam could not stop. The trap is the
prefix STRING — Proton exports a trailing slash, and a `wineserver -k` with a
different string silently does nothing.

Run: python3 tests/test_force_stop.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctlaunch as L

WINE = Path.home() / ".steam" / "steam" / "steamapps" / "common" / "Proton - Experimental" / "files" / "bin" / "wine"


def main() -> int:
    if not WINE.exists():
        print("SKIP: no Proton wine on this machine")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        prefix = base / "compatdata" / "999999" / "pfx"
        prefix.mkdir(parents=True)
        game_dir = base / "FakeGame"
        game_dir.mkdir()
        (game_dir / "FakeGame.exe").write_bytes(b"MZ" + b"\0" * 40)

        app = {"appid": "999999", "name": "FakeGame", "installdir": "FakeGame",
               "path": game_dir, "steamapps": base}

        env = dict(os.environ, WINEPREFIX=str(prefix) + "/", WINEDEBUG="-all",
                   DISPLAY=os.environ.get("DISPLAY", ":1"))
        proc = subprocess.Popen([str(WINE), "cmd", "/c", "ping", "-n", "120", "127.0.0.1"],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(8)
        before = L._pids_with_prefix(prefix.absolute())
        print(f"wine processes holding the prefix: {len(before)}")
        if not before:
            print("FAIL: nothing to kill (wine never started)")
            proc.kill()
            return 1

        lines = []
        res = L.force_stop("FakeGame", log=lines.append, app=app)
        after = L._pids_with_prefix(prefix.absolute())
        print("\n".join(lines[-4:]))
        print(f"after force_stop: {len(after)} remaining; ok={res['ok']}")
        ok = not after
        print("PASS: tree killed" if ok else "FAIL: processes survived")
        proc.kill()
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
