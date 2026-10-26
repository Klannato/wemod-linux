#!/usr/bin/env python3
"""
ctdetect — detect running and installed games for the wemod-linux GUI.

Automates "step 3" (finding the game's PID): each game's .ct tables name the
executable they target (the module arg of aobscanmodule, e.g. "NMS.exe"), so we
scan /proc for a process with that name. Installed-game detection reads Steam's
appmanifest files.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

TABLES = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "ctfetch" / "tables"

# Common Steam roots (native, flatpak, snap), plus libraryfolders.vdf for extras.
_STEAM_ROOTS = [
    Path.home() / ".steam" / "steam",
    Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    Path.home() / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
]


def game_tables(game: str) -> list[Path]:
    d = TABLES / game
    return sorted(p for p in d.iterdir() if p.suffix.lower() == ".ct") if d.exists() else []


def game_exe_names(game: str) -> set[str]:
    """Executable names a game's tables target (aobscanmodule's module arg)."""
    names: set[str] = set()
    for f in game_tables(game):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"aobscanmodule\s*\(\s*\w+\s*,\s*([^,\s\)]+)", text):
            exe = m.group(1).strip().strip('"')
            if exe and not exe.startswith("{"):
                names.add(exe.lower())
    return names


def _proc_info(pid: int) -> tuple[str, set[str]]:
    """(comm, mapped-file basenames) for a pid. Maps matter for Wine/Proton
    games, where the Windows exe shows up as a mapped file, not the comm name."""
    base = Path(f"/proc/{pid}")
    comm = ""
    try:
        comm = (base / "comm").read_text().strip().lower()
    except OSError:
        pass
    mapped: set[str] = set()
    try:
        for line in (base / "maps").read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 6 and parts[-1].startswith("/"):
                mapped.add(Path(parts[-1]).name.lower())
    except OSError:
        pass
    return comm, mapped


def find_processes() -> dict[str, list[int]]:
    """Map executable basename -> [pids], scanning /proc."""
    procs: dict[str, list[int]] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        pid = int(d.name)
        try:
            comm, mapped = _proc_info(pid)
        except Exception:
            continue
        for name in {comm, *mapped}:
            if name:
                procs.setdefault(name, []).append(pid)
    return procs


def running_pids(game: str, procs: dict[str, list[int]] | None = None) -> list[int]:
    """PIDs running any of the game's target executables."""
    if procs is None:
        procs = find_processes()
    pids: set[int] = set()
    for exe in game_exe_names(game):
        pids.update(procs.get(exe, []))
    return sorted(pids)


def _norm(name: str) -> str:
    """Normalize a game name for fuzzy matching (strip punctuation/case)."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def steam_installed_games() -> set[str]:
    """Normalized names of every Steam-installed game on this machine."""
    installed: set[str] = set()
    steamapps_dirs: list[Path] = []
    for root in _STEAM_ROOTS:
        sa = root / "steamapps"
        if sa.exists():
            steamapps_dirs.append(sa)
        vdf = sa / "libraryfolders.vdf"
        if vdf.exists():
            try:
                for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
                    steamapps_dirs.append(Path(m.group(1)) / "steamapps")
            except OSError:
                pass
    seen = set()
    for steamapps in steamapps_dirs:
        if steamapps in seen or not steamapps.exists():
            continue
        seen.add(steamapps)
        for acf in steamapps.glob("appmanifest_*.acf"):
            try:
                m = re.search(r'"name"\s+"([^"]+)"', acf.read_text(errors="replace"))
                if m:
                    installed.add(_norm(m.group(1)))
            except OSError:
                pass
    return installed


def scan_games() -> list[dict]:
    """One pass over the library -> rows with installed/running status."""
    installed = steam_installed_games()
    procs = find_processes()
    rows = []
    if not TABLES.exists():
        return rows
    for game in sorted(d.name for d in TABLES.iterdir() if d.is_dir()):
        pids = running_pids(game, procs)
        rows.append({
            "game": game,
            "installed": _norm(game) in installed,
            "running": pids,
            "exes": sorted(game_exe_names(game)),
        })
    return rows
