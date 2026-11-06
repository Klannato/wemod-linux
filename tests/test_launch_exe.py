#!/usr/bin/env python3
"""choose the game executable, not the installer.

Regression test for the Far Cry 5 failure: the install dir contains a 262 MB
`UbisoftConnectInstaller.exe` next to a 215 KB `bin/FarCry5.exe`, and the old
"largest .exe wins" rule launched the installer — which then hung in wine.

Run: python3 tests/test_launch_exe.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctdetect as D
import ctlaunch as L


def make(tree: dict[str, int], root: Path) -> Path:
    for rel, size in tree.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as fh:
            fh.truncate(size)          # sparse: size without writing the bytes
    return root


def app_for(root: Path, installdir: str) -> dict:
    return {"appid": "0", "name": installdir, "installdir": installdir,
            "path": root, "steamapps": root}


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        # 1. the Far Cry 5 shape: huge installer, small real exe in bin/
        root = make({
            "UbisoftConnectInstaller.exe": 262_000_000,
            "bin/FarCry5.exe": 215_000,
            "bin/ArcadeEditor64.exe": 56_000,
            "installscript_552521.vdf": 10,
        }, Path(tmp) / "FarCry5")
        got = L.find_exe(app_for(root, "FarCry5"), "Far Cry 5")
        if got is None or got.name != "FarCry5.exe":
            failures.append(f"FC5 shape picked {got}")

        # 2. table hint beats everything, even a bigger sibling
        root = make({"bin/Game.exe": 300_000, "bin/Other.exe": 900_000}, Path(tmp) / "Hint")
        D.game_exe_names = lambda game: {"Other"}
        got = L.find_exe(app_for(root, "Hint"), "Hint Game")
        if got is None or got.name != "Other.exe":
            failures.append(f"table hint ignored, picked {got}")

        # 3. indie shape: exe named after the installdir at the top level
        root = make({"Besiege.exe": 800_000, "UnityCrashHandler64.exe": 900_000},
                    Path(tmp) / "Besiege")
        got = L.find_exe(app_for(root, "Besiege"), "Besiege")
        if got is None or got.name != "Besiege.exe":
            failures.append(f"installdir match failed, picked {got}")

        # 4. only installers present -> no target, and they are reported
        root = make({"redist/vcredist_x64.exe": 90_000,
                     "UbisoftConnectInstaller.exe": 262_000_000}, Path(tmp) / "OnlyInstaller")
        app = app_for(root, "OnlyInstaller")
        got = L.find_exe(app, "Only Installer")
        if got is not None:
            failures.append(f"picked an installer: {got}")
        if not L.blacklisted_exes(app):
            failures.append("blacklisted_exes() reported nothing")

    for f in failures:
        print("FAIL:", f)
    print("PASS: exe selection" if not failures else f"FAIL: {len(failures)} case(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
