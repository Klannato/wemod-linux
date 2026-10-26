#!/usr/bin/env python3
"""
ctagent — the free-agent layer for wemod-linux.

An LLM (OpenRouter free model) that:
  scan <pid> "<goal>"   — parse the goal into a scanmem plan and execute it
  translate <file.ct>   — explain what a table's cheats do in plain English

The LLM only reasons; all memory work is done by ctscan (scanmem) / ctapply.

API key (any one): OPENROUTER_API_KEY env var, ~/.config/ctagent/api_key file,
or --key. Create the file with:  mkdir -p ~/.config/ctagent && echo "sk-or-..." > ~/.config/ctagent/api_key
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ctapply as A
import ctscan as S

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning:free"
FALLBACKS = ["recursive/flint-v1.0:free", "google/gemma-4-31b-it:free"]


def get_key(cli: str | None = None) -> str | None:
    for src in (cli, os.environ.get("OPENROUTER_API_KEY")):
        if src:
            return src.strip()
    p = Path.home() / ".config" / "ctagent" / "api_key"
    return p.read_text().strip() if p.exists() else None


def llm(messages: list[dict], key: str | None = None, model: str = DEFAULT_MODEL,
        max_tokens: int = 800, json_mode: bool = False) -> str:
    key = get_key(key)
    if not key:
        raise SystemExit("no OpenRouter key — set OPENROUTER_API_KEY or write "
                         "~/.config/ctagent/api_key")
    models = [model] + [m for m in FALLBACKS if m != model]
    last_err = None
    for m in models:
        body: dict = {"model": m, "messages": messages, "max_tokens": max_tokens}
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "ctagent",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                msg = json.loads(r.read())["choices"][0]["message"]
                content = msg.get("content")
                if content:
                    return content
                last_err = "empty content"  # reasoning model ran out of tokens; try next
                continue
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 404, 403):  # rate-limited / retired / gated -> next model
                continue
            raise
    raise SystemExit(f"all models failed: {last_err}")


SYSTEM = (
    "You control the scanmem memory scanner on Linux. Commands: type a BARE number to "
    "SEARCH for that value (e.g. `100`); filters: `=` equal/unchanged, `!=` changed, `+` "
    "increased, `-` decreased, `>` greater, `<` less; `set <value>` writes, `lock` freezes, "
    "`list` shows matches, `pid <n>` attaches. Respond ONLY with valid JSON."
)


def _extract_json(txt: str) -> dict:
    """Pull the first {...} object out of the model's text (handles prose,
    markdown fences, and models that wrap JSON in extra text)."""
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in model output: {txt[:200]}")
    return json.loads(m.group(0))


def parse_goal(goal: str, key: str | None = None, model: str | None = None) -> dict:
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"Convert this request into a JSON plan: {goal!r}. "
            'Return {"search": <number or null>, "type": "int", '
            '"action": "set|lock|none", "action_value": <number or null>}. '
            "If the current value is unknown, set search to null."},
    ]
    txt = llm(msgs, key=key, model=model or DEFAULT_MODEL, max_tokens=600)
    return _extract_json(txt)


def scan(pid: int, goal: str, key: str | None = None, model: str | None = None) -> tuple[dict, str]:
    plan = parse_goal(goal, key=key, model=model)
    s = S.Scanmem(pid)
    out = []
    if plan.get("search") is not None:
        out.append(s.search(plan["search"]))
    action = plan.get("action")
    if action == "set" and plan.get("action_value") is not None:
        out.append(s.set(plan["action_value"]))
    elif action == "lock":
        out.append(s.lock())
    s.close()
    return plan, "\n".join(out)


def translate(path: str, key: str | None = None, model: str | None = None) -> str:
    entries = A.flatten(A.parse_ct(path))
    names = [p[-1] for p, e in entries if p[-1]]
    msgs = [
        {"role": "system", "content": "You explain video-game Cheat Engine tables briefly."},
        {"role": "user", "content":
            "Here are the cheat names in a table: " + ", ".join(names[:60]) +
            ". Summarize in 3-5 bullets what this table offers."},
    ]
    return llm(msgs, key=key, model=model or DEFAULT_MODEL, max_tokens=600)


def main() -> None:
    ap = argparse.ArgumentParser(prog="ctagent", description="Free-agent layer (OpenRouter).")
    ap.add_argument("--key", help="OpenRouter API key")
    ap.add_argument("--model", help=f"model (default {DEFAULT_MODEL})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="find/set/lock a value in a running process")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("goal")
    p.set_defaults(func=lambda a, k: (lambda pl, o: (print("plan:", json.dumps(pl)), print(o)))(*scan(a.pid, a.goal, k, a.model)))

    p = sub.add_parser("translate", help="explain a .ct table")
    p.add_argument("file")
    p.set_defaults(func=lambda a, k: print(translate(a.file, k, a.model)))

    args = ap.parse_args()
    key = get_key(args.key)
    args.func(args, key)


if __name__ == "__main__":
    main()
