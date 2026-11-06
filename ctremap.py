#!/usr/bin/env python3
"""ctremap — re-locate a cheat when the table's address no longer exists.

A `.ct` table hard-codes addresses from one game build. When the build moves, the
`assert(...)` fails and a ±0x8000 drift search (ctaa._find_shift) is often too
narrow or ambiguous. This module is the "agent" fallback, and it looks for the
strongest evidence a table still carries:

1. **Hex comment signatures** — table authors paste the surrounding instruction
   bytes as a comment, e.g.
       //F3 0F 5E F7 F3 41 0F 59 F3 F3 0F 59 73 2C F3 0F 58 73 10 F3 0F 11 73 10
   That is 20 bytes: unique in a 250 MB module, unlike the 5-byte assert.
2. **DISABLE `DB` blocks** — the original bytes, sometimes longer than the assert.
3. **The assert bytes themselves** — last resort, and only if unique.

Confidence rules (a wrong patch crashes the game, so this is deliberately strict):
  * the signature must be unique across the module's readable mappings
  * the signature must contain the assert bytes (that is what locates the patch
    point inside it)
  * signatures are tried longest-first, and anything under 8 bytes is never
    trusted for remapping

What this does NOT do (yet): string/xref-driven discovery — searching memory for
"Invincible"/"GodMode" strings and disassembling the whole `.text` for
`lea reg,[rip+str]` references. That is the next step; it needs capstone over
tens of MB and a confidence model that does not exist yet.
"""
from __future__ import annotations

import re
from pathlib import Path

import ctapply as C

HEX_LINE = re.compile(r"//\s*((?:[0-9A-Fa-f]{2}[ ?]+){3,}[0-9A-Fa-f]{2})")
DB_LINE = re.compile(r"^\s*DB\s+((?:[0-9A-Fa-f]{2}\s*)+)$", re.M | re.I)
ASSERT = re.compile(r"assert\s*\(\s*([^,]+?)\s*,\s*([0-9A-Fa-f? ]+?)\s*\)", re.I)
MIN_TRUSTED = 8


def _bytes_from(text: str) -> bytes:
    out = bytearray()
    for token in text.replace("?", " ").split():
        try:
            out.append(int(token, 16))
        except ValueError:
            continue
    return bytes(out)


def extract_signatures(script: str) -> list[dict]:
    """Candidate signatures, longest first, each tagged with where it came from."""
    sigs: list[dict] = []
    for m in HEX_LINE.finditer(script):
        data = _bytes_from(m.group(1))
        if len(data) >= 3:
            sigs.append({"bytes": data, "source": "hex-comment"})
    for m in DB_LINE.finditer(script):
        data = _bytes_from(m.group(1))
        if len(data) >= 3:
            sigs.append({"bytes": data, "source": "disable-db"})
    for m in ASSERT.finditer(script):
        data = _bytes_from(m.group(2))
        if len(data) >= 3:
            sigs.append({"bytes": data, "source": "assert"})
    # dedupe (same bytes from different sources), keep the first label
    seen, out = set(), []
    for sig in sorted(sigs, key=lambda s: -len(s["bytes"])):
        key = sig["bytes"]
        if key in seen:
            continue
        seen.add(key)
        out.append(sig)
    return out


def module_regions(pid: int, module: str) -> list[dict]:
    name = module.lower()
    return [r for r in C.parse_maps(pid)
            if r["path"] and name in Path(r["path"]).name.lower() and r["perms"].startswith("r")]


def find_unique(pid: int, module: str, needle: bytes) -> int | None:
    """Absolute address of the single occurrence of `needle` in the module."""
    hits: list[int] = []
    for region in module_regions(pid, module):
        size = region["end"] - region["start"]
        try:
            blob = C.read_mem(pid, region["start"], size)
        except OSError:
            continue
        start = 0
        while True:
            i = blob.find(needle, start)
            if i < 0:
                break
            hits.append(region["start"] + i)
            if len(hits) > 1:
                return None          # ambiguous: refuse to guess
            start = i + 1
    return hits[0] if len(hits) == 1 else None


def _module_for(pid: int, expr: str) -> str | None:
    """Module name from an assert expression: `M.dll+off` or an absolute address."""
    if "+" in expr and not expr.lower().startswith("0x"):
        return expr.split("+")[0].strip()
    try:
        addr = int(expr, 16) if expr.lower().startswith("0x") else int(expr, 16)
    except ValueError:
        return None
    for region in C.parse_maps(pid):
        if region["start"] <= addr < region["end"] and region["path"].startswith("/"):
            return Path(region["path"]).name
    return None


def remap(pid: int, script: str, log=None) -> dict | None:
    """(address of the patch point, evidence) or None when nothing is convincing."""
    assert_m = ASSERT.search(script)
    if not assert_m:
        return None
    expr = assert_m.group(1).strip()
    module = _module_for(pid, expr)
    if not module:
        return None
    assert_bytes = _bytes_from(assert_m.group(2))
    if not assert_bytes:
        return None

    for sig in extract_signatures(script):
        data = sig["bytes"]
        if len(data) < MIN_TRUSTED:
            continue
        offset = data.find(assert_bytes)
        if offset < 0:
            continue
        hit = find_unique(pid, module, data)
        if hit is None:
            if log:
                log(f"  remap: {sig['source']} signature ({len(data)}B) not unique — skipping")
            continue
        addr = hit + offset
        if log:
            log(f"  remap: {sig['source']} signature ({len(data)}B) unique at {hit:#x} "
                f"→ patch point {addr:#x}")
        return {"addr": addr, "module": module, "source": sig["source"],
                "signature": data.hex(), "signature_len": len(data),
                "patch_offset": offset}
    return None
