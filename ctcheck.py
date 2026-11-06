#!/usr/bin/env python3
"""ctcheck — is there a newer table before we start remapping?

Step 0 of the "this table is incompatible" pipeline: prefer a table written for
the installed build over any heuristic patch. Cheap, and it beats guessing.

    python3 ctcheck.py "Far Cry 5"            # report
    python3 ctcheck.py "Far Cry 5" --fetch    # download newer tables into the library

Online search uses the authenticated `gh` CLI (GitHub code search); the local side
compares table file mtimes, so "newer" means "committed after the table you have".
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

LIB = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "ctfetch"


def local_tables(game: str) -> list[dict]:
    d = LIB / "tables" / game
    if not d.is_dir():
        return []
    out = []
    for f in sorted(p for p in d.iterdir() if p.suffix.lower() == ".ct"):
        st = f.stat()
        out.append({"path": f, "name": f.name, "mtime": st.st_mtime, "size": st.st_size})
    return out


def queries(game: str) -> list[str]:
    """GitHub code search needs the name as the files are named.

    "Far Cry 5" returns nothing; "FarCry5" returns the tables (files are
    `<Game>.CT`, no spaces).
    """
    compact = re.sub(r"[^A-Za-z0-9]+", "", game)
    out = [game, compact]
    if " " in game:
        out.append(game.replace(" ", ""))
    seen, uniq = set(), []
    for q in out:
        if q and q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    return uniq


def gh_search(game: str, limit: int = 8) -> list[dict]:
    """`gh search code` hits: [{repo, path, url}] — empty when gh is unavailable."""
    hits: list[dict] = []
    seen = set()
    for query in queries(game):
        try:
            done = subprocess.run(
                ["gh", "search", "code", "--extension", "ct", query,
                 "--limit", str(limit), "--json", "repository,path"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode != 0:
            continue
        try:
            raw = json.loads(done.stdout or "[]")
        except ValueError:
            continue
        for item in raw:
            repo = item.get("repository") or {}
            name = repo.get("nameWithOwner") or repo.get("name") or ""
            path = item.get("path") or ""
            key = (name, path)
            if name and path and key not in seen:
                seen.add(key)
                hits.append({"repo": name, "path": path,
                             "url": "https://raw.githubusercontent.com/"
                                    f"{name}/HEAD/{urllib.parse.quote(path)}"})
    return hits


ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def commit_date(repo: str, path: str) -> str | None:
    """Last commit date for one file.

    `gh api -f` would make this a POST; the query string keeps it a GET, and the
    result is validated as ISO-8601 so an error body never compares as a date.
    """
    url = f"repos/{repo}/commits?path={urllib.parse.quote(path)}&per_page=1"
    try:
        done = subprocess.run(["gh", "api", url, "--jq", ".[0].commit.committer.date"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    date = (done.stdout or "").strip()
    return date if ISO.match(date) else None


def matches_game(path: str, game: str) -> bool:
    """Is this table actually for this game? (`FarCryNewDawn` is not `Far Cry 5`.)"""
    stem = re.sub(r"[^a-z0-9]", "", Path(path).stem.lower())
    key = re.sub(r"[^a-z0-9]", "", game.lower())
    return bool(stem) and (stem == key or stem.startswith(key))


def assess(local: list[dict], online: list[dict], newest_local_iso: str | None) -> dict:
    """Pure decision: which online tables are newer than what we have?"""
    newer = []
    for hit in online:
        date = hit.get("date") or ""
        # validate here too: assess() is pure and gets called with injected data,
        # and an error body ("{...}") sorts *after* every ISO date
        if not ISO.match(date) or not newest_local_iso:
            continue
        if date > newest_local_iso:      # ISO-8601 sorts lexicographically
            newer.append(hit)
    return {"newer": newer, "has_newer": bool(newer), "checked": len(online)}


def fetch(hit: dict, game: str) -> Path | None:
    dest_dir = LIB / "tables" / game
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(hit["path"]).name) or "table.ct"
    dest = dest_dir / f"new_{name}"
    try:
        with urllib.request.urlopen(hit["url"], timeout=60) as resp:
            data = resp.read()
    except OSError:
        return None
    if b"<CheatTable" not in data[:4096]:
        return None
    dest.write_bytes(data)
    return dest


def check(game: str, do_fetch: bool = False) -> dict:
    local = local_tables(game)
    newest_local = max((t["mtime"] for t in local), default=None)
    newest_iso = None
    if newest_local:
        import datetime
        newest_iso = datetime.datetime.fromtimestamp(
            newest_local, datetime.timezone.utc).isoformat()

    online = [h for h in gh_search(game) if matches_game(h["path"], game)]
    for hit in online:
        hit["date"] = commit_date(hit["repo"], hit["path"])
    verdict = assess(local, online, newest_iso)
    verdict.update({"game": game, "local": [t["name"] for t in local],
                    "online": online, "newest_local": newest_iso})
    if do_fetch:
        fetched = []
        for hit in verdict["newer"]:
            path = fetch(hit, game)
            if path:
                fetched.append(str(path))
        verdict["fetched"] = fetched
    return verdict


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    game = sys.argv[1]
    res = check(game, "--fetch" in sys.argv)
    print(f"{game}: {len(res['local'])} local table(s), newest {res['newest_local']}")
    print(f"  online hits: {len(res['online'])}")
    for hit in res["online"][:8]:
        mark = "NEWER" if hit in res["newer"] else "     "
        print(f"   {mark} {hit['date'] or '?':20} {hit['repo']}/{hit['path']}")
    if res.get("fetched"):
        print(f"  fetched: {res['fetched']}")
    elif not res["has_newer"]:
        print("  no newer table found — remapping is the fallback")


if __name__ == "__main__":
    main()
