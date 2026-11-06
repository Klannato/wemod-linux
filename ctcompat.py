#!/usr/bin/env python3
"""ctcompat — score a table against the game build on disk, without launching it.

A `.ct` table is only valid for one build. Every script with a direct address
carries its own proof: `assert(MODULE+offset, <bytes>)`. Those bytes can be checked
against the DLL/EXE on disk, so "is this table for my install?" is answerable
offline in milliseconds instead of by launching the game and crashing it.

    python3 ctcompat.py "Far Cry 5"
    python3 ctcompat.py "Far Cry 5" --json

Per table it reports how many asserts match, how many are off by a consistent
drift, and the sample that didn't — which is what decides "use this table" vs
"remap" vs "rebuild from scratch".

Scope: Asserts that name an absolute address (`0x7ff…`) cannot be checked against
a file and are skipped; only `MODULE+offset` forms are scored.
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
from pathlib import Path

LIB = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "ctfetch"
ASSERT = re.compile(r"assert\s*\(\s*([A-Za-z0-9_.\\-]+\.(?:dll|exe))\s*\+\s*([0-9A-Fa-f]+)\s*,"
                    r"\s*([0-9A-Fa-f? ]+?)\s*\)", re.I)


# ---------------------------------------------------------------- PE plumbing
def pe_sections(path: Path) -> list[tuple[int, int, int, int]] | None:
    """[(vaddr, vsize, raw_off, raw_size)] or None when not a PE file."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x400)
            if head[:2] != b"MZ":
                return None
            pe = struct.unpack_from("<I", head, 0x3C)[0]
            if head[pe:pe + 4] != b"PE\0\0":
                return None
            nsec = struct.unpack_from("<H", head, pe + 6)[0]
            optsz = struct.unpack_from("<H", head, pe + 20)[0]
            table = pe + 24 + optsz
            fh.seek(table)
            raw = fh.read(nsec * 40)
    except (OSError, struct.error):
        return None
    out = []
    for i in range(nsec):
        off = i * 40
        vsize, vaddr, rsize, raw_off = struct.unpack_from("<IIII", raw, off + 8)
        out.append((vaddr, vsize, raw_off, rsize))
    return out


def read_rva(path: Path, sections: list[tuple[int, int, int, int]], rva: int, size: int) -> bytes | None:
    for vaddr, vsize, raw_off, rsize in sections:
        span = max(vsize, rsize)
        if vaddr <= rva < vaddr + span:
            start = raw_off + (rva - vaddr)
            if rva - vaddr + size > span:
                return None
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    return fh.read(size)
            except OSError:
                return None
    return None


# ---------------------------------------------------------------- game files
def game_dir(game: str) -> Path | None:
    if not LIB.exists():
        return None
    target = re.sub(r"[^a-z0-9]", "", game.lower())
    for d in LIB.iterdir():
        if d.is_dir() and re.sub(r"[^a-z0-9]", "", d.name.lower()) == target:
            return d
    return None


def steam_install_dir(game: str) -> Path | None:
    """Where the game is actually installed (Steam library), if it is there."""
    try:
        import ctlaunch as L
        app = L.find_app(game)
        return app["path"] if app else None
    except Exception:
        return None


def find_module_file(roots: list[Path], module: str) -> Path | None:
    name = module.lower()
    for root in roots:
        if not root or not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.name.lower() == name:
                return p
    return None


# ---------------------------------------------------------------- scoring
def table_asserts(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for m in ASSERT.finditer(text):
        out.append({"module": m.group(1), "rva": int(m.group(2), 16),
                    "bytes": m.group(3).strip()})
    return out


def _pattern(hexbytes: str) -> tuple[bytes, list[bool]]:
    data, mask = bytearray(), []
    for token in hexbytes.split():
        if token in ("?", "??", "*"):
            data.append(0)
            mask.append(False)
        else:
            try:
                data.append(int(token, 16))
                mask.append(True)
            except ValueError:
                continue
    return bytes(data), mask


def score_table(path: Path, module_roots: list[Path]) -> dict:
    asserts = table_asserts(path)
    if not asserts:
        return {"table": path.name, "asserts": 0, "checked": 0, "matched": 0,
                "verdict": "no direct-address asserts"}
    by_module: dict[str, list[dict]] = {}
    for a in asserts:
        by_module.setdefault(a["module"], []).append(a)

    matched = checked = 0
    drifts: list[int] = []
    samples: list[dict] = []
    for module, items in by_module.items():
        file = find_module_file(module_roots, module)
        if file is None:
            samples.append({"module": module, "status": "module not found on disk"})
            continue
        sections = pe_sections(file)
        if sections is None:
            samples.append({"module": module, "status": "not a PE file"})
            continue
        for item in items:
            want, mask = _pattern(item["bytes"])
            if not want:
                continue
            checked += 1
            got = read_rva(file, sections, item["rva"], len(want))
            if got is not None and all((not mask[i]) or got[i] == want[i] for i in range(len(want))):
                matched += 1
                continue
            # Look for the same bytes nearby: a consistent shift means "another
            # build". The window must stay inside the section, or read_rva refuses
            # and every drift comes back empty.
            drift = None
            for vaddr, vsize, _raw_off, rsize in sections:
                span = max(vsize, rsize)
                if not (vaddr <= item["rva"] < vaddr + span):
                    continue
                lo = max(vaddr, item["rva"] - 0x4000)
                hi = min(vaddr + span, item["rva"] + 0x4000 + len(want))
                window = read_rva(file, sections, lo, hi - lo)
                if not window:
                    break
                hits = [i for i in range(len(window)) if window.startswith(want, i)]
                if len(hits) == 1:
                    drift = (lo + hits[0]) - item["rva"]
                break
            if drift is not None:
                drifts.append(drift)
            if len(samples) < 6:
                samples.append({
                    "module": module, "rva": f"{item['rva']:#x}",
                    "expected": item["bytes"],
                    "actual": got.hex(" ") if got else "unreadable",
                    "drift": f"{drift:+#x}" if drift is not None else None})

    distinct = sorted(set(drifts))
    if checked and matched == checked:
        verdict = "matches this build"
    elif checked and matched == 0 and len(distinct) > 1:
        # Non-uniform drift: different functions moved by different amounts, so a
        # single shift is meaningless — remapping this table is unsafe.
        verdict = "different build (inconsistent drift)"
    elif checked and matched == 0:
        verdict = "different build"
    elif checked:
        verdict = "partial match"
    else:
        verdict = "unchecked"
    return {"table": path.name, "asserts": len(asserts), "checked": checked,
            "matched": matched, "verdict": verdict,
            "consistent_drift": (f"{distinct[0]:+#x}" if len(distinct) == 1 else None),
            "drifts": [f"{d:+#x}" for d in distinct],
            "remap_safe": bool(checked and matched == checked) or len(distinct) == 1,
            "samples": samples}


def verify_game(game: str, include_online: bool = False) -> dict:
    roots = [p for p in (game_dir(game), steam_install_dir(game)) if p]
    tables = []
    local_dir = LIB / "tables" / game
    if local_dir.is_dir():
        for f in sorted(local_dir.glob("*.ct")) + sorted(local_dir.glob("*.CT")):
            tables.append(score_table(f, roots))
    if include_online:
        try:
            import ctcheck
            for hit in ctcheck.gh_search(game)[:4]:
                tmp = Path("/tmp") / Path(hit["path"]).name
                got = ctcheck.fetch(hit, game)
                if got:
                    tables.append(score_table(got, roots))
        except Exception:
            pass
    best = max(tables, key=lambda t: (t["matched"], -t["checked"]), default=None)
    return {"game": game, "roots": [str(r) for r in roots], "tables": tables, "best": best}


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    game = sys.argv[1]
    res = verify_game(game, include_online="--online" in sys.argv)
    if "--json" in sys.argv:
        print(json.dumps(res, indent=1))
        return
    print(f"{game}: searching {', '.join(res['roots']) or 'no install found'}")
    for t in res["tables"]:
        print(f"  {t['table'][:50]:50} {t['matched']}/{t['checked']} asserts  "
              f"{t['verdict']}" + (f"  drift {t['consistent_drift']}"
                                   if t.get("consistent_drift") else ""))
        for s in t["samples"][:3]:
            print(f"      {s}")
    if res["best"]:
        b = res["best"]
        print(f"  best: {b['table']} ({b['matched']}/{b['checked']})")


if __name__ == "__main__":
    main()
