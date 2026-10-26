#!/usr/bin/env python3
"""
github_fallback.py — for games the local index missed, find .ct tables via
GitHub code search (full-text, catches acronyms like "No Man's Sky" -> NMS.CT).

Rate-limited (~6s/search, 10/min) with exponential backoff + retry on rate
limit. Resumes from <tsv>.fallback.json. Writes results as it goes.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ctfetch as C


def derive_query(name: str) -> str:
    """Strip stopword/edition words and lone chars, keep original words verbatim —
    GitHub search handles punctuation/case better than a normalized query."""
    keep = []
    for w in name.split():
        core = "".join(ch for ch in w if ch.isalnum())
        if core.lower() in C.WEAK or len(core) < 2:
            continue
        keep.append(w)
    return " ".join(keep) if keep else name


def code_search_retry(query: str, limit: int = 5, retries: int = 6, base_wait: float = 6.0):
    """Returns list of results, [] for no hits, or None if rate-limited out."""
    for attempt in range(retries):
        try:
            out = C.gh("search", "code", query, "--extension", "ct",
                       "--limit", str(limit), "--json", "repository,path")
        except Exception as exc:  # noqa: BLE001
            wait = base_wait * (2 ** attempt)
            print(f"    rate-limit (attempt {attempt+1}/{retries}): wait {wait:.0f}s "
                  f"({str(exc)[:60]})", file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
        if not out or out == "[]":
            return []
        return json.loads(out)
    return None


def main(tsv: str, per_game: int = 1, delay: float = 6.0) -> None:
    index = C.load_index()
    games = C._read_game_list(tsv)
    unmatched = [g for g in games if not C.search_local(g, index, min_score=50)]
    outfile = Path(tsv).with_suffix(".fallback.json")

    results = json.loads(outfile.read_text()) if outfile.exists() else {}
    todo = [g for g in unmatched if g not in results]
    failed: list[str] = []
    print(f"fallback: {len(todo)} to search ({len(results)} done), ~{delay}s/search",
          file=sys.stderr)

    branch_cache: dict[str, str] = {}
    for i, name in enumerate(todo, 1):
        q = derive_query(name)
        hits = code_search_retry(q)
        if hits is None:
            failed.append(name)
            print(f"[{i}/{len(todo)}] {name}: FAILED (rate limit)", file=sys.stderr, flush=True)
            continue
        if hits:
            got = 0
            for r in hits:
                if got >= per_game:
                    break
                repo = r["repository"]["nameWithOwner"]
                path = r["path"]
                if repo not in branch_cache:
                    try:
                        branch_cache[repo] = C.default_branch(repo)
                    except Exception:
                        branch_cache[repo] = "main"
                branch = branch_cache[repo]
                url = C.raw_url(repo, branch, path)
                gamedir = C.TABLES_DIR / name
                dest = gamedir / f"{repo.split('/')[0]}__{Path(path).name}"
                if dest.exists():
                    got += 1
                    continue
                try:
                    size = C.download(url, dest)
                    ok = C.looks_like_ct(dest.read_bytes())
                except Exception as exc:  # noqa: BLE001
                    print(f"[{i}/{len(todo)}] {name}: DL FAIL {repo}::{path} ({exc})",
                          file=sys.stderr, flush=True)
                    continue
                if not ok:
                    dest.unlink()
                    continue
                got += 1
                results.setdefault(name, []).append({"repo": repo, "path": path, "url": url})
                print(f"[{i}/{len(todo)}] {name}: OK {repo}::{path} ({size}B)",
                      file=sys.stderr, flush=True)
            outfile.write_text(json.dumps(results, indent=2))
        time.sleep(delay)

    total = sum(len(v) for v in results.values())
    print(f"DONE: {total} tables across {len(results)}/{len(unmatched)} fallback games; "
          f"failed={len(failed)}")
    if failed:
        Path(tsv).with_suffix(".fallback_failed.txt").write_text("\n".join(failed) + "\n")
        print(f"failed games -> {Path(tsv).with_suffix('.fallback_failed.txt')}")
    print(f"results -> {outfile}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "steam_games.tsv")
