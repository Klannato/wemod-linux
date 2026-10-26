#!/usr/bin/env python3
"""
ctfetch — fetch Cheat Engine (.ct) tables for games.

Phase 1: GitHub collection sources (no auth wall, direct raw downloads).
Later phases: opencheattables.com (phpBB, Cloudflare challenge) and
FearlessRevolution (login + Cloudflare).

Library layout:
    ~/.local/share/ctfetch/index.json            # {"built":..., "entries":[...]}
    ~/.local/share/ctfetch/tables/<Game>/<file>.ct

An entry: {game, norm, pathnorm, repo, branch, path, url}
  norm     = normalize(filename stem)  — quick display match
  pathnorm = normalize(full path)      — robust match (captures game folders)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Collection repos we index. Matching uses the full path, so layout differences
# (game folders vs flat files) don't matter. Repos that don't exist or have no
# .ct files are skipped gracefully.
SOURCES = [
    "MBRKiNG/CE-Tables",
    "bbfox0703/Mydev-Cheat-Engine-Tables",
    "atul1503/Cheat-Engine-Tables",
    "Hexorg/CheatEngineTables",
    "dsasmblr/cheat-engine",
    "CJBok/Cheat-Tables",
    "ragnaroks/CheatTables",
    "grasmanek94/cheat-tables",
    "ken1882/CheatTables",
]

DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "ctfetch"
INDEX_PATH = DATA_HOME / "index.json"
TABLES_DIR = DATA_HOME / "tables"

UA = "Mozilla/5.0 (X11; Linux x86_64) ctfetch/0.2"


def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def list_ct_paths(repo: str) -> list[str]:
    out = gh("api", f"repos/{repo}/git/trees/HEAD?recursive=1",
             "--jq", r'.tree[] | select(.path | ascii_downcase | endswith(".ct")) | .path')
    return [ln for ln in out.splitlines() if ln.strip()]


def default_branch(repo: str) -> str:
    return gh("api", f"repos/{repo}", "--jq", ".default_branch")


def raw_url(repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{urllib.parse.quote(path)}"


def build_index() -> dict:
    entries: list[dict] = []
    for repo in SOURCES:
        try:
            branch = default_branch(repo)
            paths = list_ct_paths(repo)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {repo}: {exc}", file=sys.stderr)
            continue
        print(f"  {repo}: {len(paths)} .ct", file=sys.stderr)
        for p in paths:
            stem = Path(p).stem
            entries.append({
                "game": stem,
                "norm": normalize(stem),
                "pathnorm": normalize(p),
                "repo": repo,
                "branch": branch,
                "path": p,
                "url": raw_url(repo, branch, p),
            })
    return {"built": _dt.datetime.now().isoformat(timespec="seconds"), "entries": entries}


def load_index() -> dict:
    if not INDEX_PATH.exists():
        raise SystemExit("no index yet — run: ctfetch index")
    return json.loads(INDEX_PATH.read_text())


def save_index(index: dict) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index))


# Tokens that must not drive a match on their own — stopwords and edition/suffix
# words that appear across many unrelated games.
WEAK = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is",
    "it", "at", "edition", "special", "remastered", "definitive", "anniversary",
    "deluxe", "complete", "gold", "enhanced", "legacy", "collection", "goty",
    "game", "digital", "season", "ultimate", "origins", "version", "pack",
}


def _tok_present(tok: str, words: set[str]) -> bool:
    """A token matches a word if equal, or (for tokens >=4 chars) a prefix —
    so "borderlands" matches "borderlands2.ct" but "rage" does not match
    "chronorage" (suffix), and "strike" alone won't match "counterstrike"."""
    return any(w == tok or (len(tok) >= 4 and w.startswith(tok)) for w in words)


def _tokens_hit(toks: list[str], t: str) -> int:
    if not toks:
        return 0
    words = set(t.split())
    if all(_tok_present(tok, words) for tok in toks):
        return 50
    # concatenated form for no-space filenames, e.g. "half life" -> "halflife2"
    if len(toks) >= 2 and "".join(toks) in t.replace(" ", ""):
        return 50
    return 0


def _score(q: str, e: dict) -> int:
    """Match query norm against the entry's stem norm and full-path norm.

    Tiers: 100 exact · 60 substring · 50 token match. Token matching is
    word-boundary based so a substring inside another word (e.g. "rage" in
    "chronorage") does not match.
    """
    s = 0
    toks = [t for t in q.split() if len(t) >= 2 and t not in WEAK]
    for t in (e["norm"], e["pathnorm"]):
        if not t:
            continue
        if q == t:
            s = max(s, 100)
        elif q in t or t in q:
            s = max(s, 60)
        else:
            s = max(s, _tokens_hit(toks, t))
    return s


def _top_tier(hits: list[tuple[int, dict]], n: int) -> list[tuple[int, dict]]:
    """Keep only entries at the highest score tier (avoids pulling tables for
    sibling games in the same franchise), then cap at n."""
    if not hits:
        return []
    top = hits[0][0]
    return [(s, e) for s, e in hits if s == top][:n]


def search_local(query: str, index: dict, min_score: int = 40) -> list[tuple[int, dict]]:
    q = normalize(query)
    out = [(s, e) for e in index["entries"] if (s := _score(q, e)) >= min_score]
    out.sort(key=lambda r: (-r[0], r[1]["path"]))
    return out


def github_code_search(query: str, limit: int = 10) -> list[dict]:
    """Fallback: search all of GitHub's indexed code for .ct files matching query."""
    out = gh("search", "code", query, "--extension", "ct",
             "--limit", str(limit), "--json", "repository,path")
    if not out or out == "[]":
        return []
    results = json.loads(out)
    branch_cache: dict[str, str] = {}
    entries = []
    for r in results:
        repo = r["repository"]["nameWithOwner"]
        if repo not in branch_cache:
            try:
                branch_cache[repo] = default_branch(repo)
            except RuntimeError:
                branch_cache[repo] = "master"
        path = r["path"]
        entries.append({
            "game": Path(path).stem,
            "norm": normalize(Path(path).stem),
            "pathnorm": normalize(path),
            "repo": repo,
            "branch": branch_cache[repo],
            "path": path,
            "url": raw_url(repo, branch_cache[repo], path),
        })
    return entries


def download(url: str, dest: Path) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)


def looks_like_ct(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<?xml") or b"<cheattable" in head


def do_index(_args) -> None:
    index = build_index()
    save_index(index)
    print(f"indexed {len(index['entries'])} tables -> {INDEX_PATH}")


def do_search(args) -> None:
    index = load_index()
    hits = search_local(args.query, index)
    if not hits:
        print(f"no local match for '{args.query}'")
    for s, e in hits[:args.limit]:
        print(f"[{s}] {e['repo']} :: {e['path']}")
    if args.github:
        print(f"\n--- GitHub code search for '{args.query}' ---")
        for e in github_code_search(args.query, limit=args.limit):
            print(f"  - {e['repo']} :: {e['path']}")


def do_fetch(args) -> None:
    index = load_index()
    hits = search_local(args.query, index)
    entries = [e for _s, e in _top_tier(hits, args.per_game)]
    if not entries and args.github:
        print(f"no local match; trying GitHub code search for '{args.query}' ...")
        entries = github_code_search(args.query, limit=args.limit)

    if not entries:
        raise SystemExit(f"no tables found for '{args.query}'")

    gamedir = TABLES_DIR / args.query
    multi = len(entries) > 1
    print(f"fetching up to {args.per_game} table(s) for '{args.query}' -> {gamedir}")
    for e in entries:
        owner = e["repo"].split("/")[0]
        fname = f"{owner}__{Path(e['path']).name}" if multi else Path(e["path"]).name
        dest = gamedir / fname
        if dest.exists() and not args.force:
            print(f"  skip (exists): {fname}")
            continue
        try:
            size = download(e["url"], dest)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED {e['repo']} :: {e['path']}: {exc}")
            continue
        ok = looks_like_ct(dest.read_bytes())
        if not ok:
            dest.unlink()
            print(f"  BAD (deleted) {size:>7}B  {fname}")
        else:
            print(f"  OK  {size:>7}B  {fname}")


def _read_game_list(path: str) -> list[str]:
    names = []
    for ln in Path(path).read_text().splitlines():
        if not ln.strip():
            continue
        parts = ln.split("\t", 1)
        names.append(parts[1].strip() if len(parts) > 1 else ln.strip())
    return names


def do_library(args) -> None:
    index = load_index()
    games = _read_game_list(args.file)
    matched: dict[str, list[tuple[int, dict]]] = {}
    unmatched: list[str] = []
    for name in games:
        hits = search_local(name, index, min_score=args.min_score)
        if hits:
            matched[name] = hits
        else:
            unmatched.append(name)

    n_tables = sum(len(h) for h in matched.values())
    print(f"library: {len(matched)}/{len(games)} games matched, {len(unmatched)} unmatched, "
          f"{n_tables} candidate tables")

    if args.fetch:
        fetched = 0
        for name, hits in matched.items():
            gamedir = TABLES_DIR / name
            multi = len(hits) > 1
            for s, e in _top_tier(hits, args.per_game):
                owner = e["repo"].split("/")[0]
                fname = f"{owner}__{Path(e['path']).name}" if multi else Path(e["path"]).name
                dest = gamedir / fname
                if dest.exists():
                    continue
                try:
                    size = download(e["url"], dest)
                    ok = looks_like_ct(dest.read_bytes())
                    if not ok:
                        dest.unlink()
                        print(f"  BAD (deleted) {name}: {e['path']}")
                        continue
                    fetched += 1
                    if args.verbose:
                        print(f"  [{s}] {name}: {size}B OK {e['repo']}::{e['path']}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAIL {name}: {e['path']} ({exc})")
        print(f"downloaded {fetched} tables")
    else:
        report = Path(args.file).with_suffix(".coverage.txt")
        lines = [f"{len(games)} games scanned, {len(matched)} matched, {len(unmatched)} unmatched",
                 "", "== MATCHED =="]
        for name, hits in sorted(matched.items()):
            lines.append(f"{name}\t{len(hits)}")
        lines += ["", "== UNMATCHED =="] + sorted(unmatched)
        report.write_text("\n".join(lines) + "\n")
        print(f"coverage report -> {report}")
        print(f"unmatched ({len(unmatched)}): {', '.join(sorted(unmatched))}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="ctfetch", description="Fetch Cheat Engine tables.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("index", help="rebuild the local index from GitHub collection repos")

    p = sub.add_parser("search", help="search the local index")
    p.add_argument("query")
    p.add_argument("--github", action="store_true", help="also search GitHub code")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("fetch", help="download tables for a game")
    p.add_argument("query")
    p.add_argument("--github", action="store_true", help="fall back to GitHub code search")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--per-game", type=int, default=3)

    p = sub.add_parser("library", help="match + optionally fetch tables for a list of games (TSV)")
    p.add_argument("file", help="TSV file: appid<TAB>name")
    p.add_argument("--fetch", action="store_true", help="download matched tables")
    p.add_argument("--per-game", type=int, default=3, help="max tables to download per game")
    p.add_argument("--min-score", type=int, default=40, help="minimum match score (40/60/100)")
    p.add_argument("--verbose", action="store_true", help="log every download")

    args = ap.parse_args()
    {"index": do_index, "search": do_search, "fetch": do_fetch, "library": do_library}[args.cmd](args)


if __name__ == "__main__":
    main()
