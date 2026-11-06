#!/usr/bin/env python3
"""ctfind — build a cheat from scratch when the table cannot be remapped.

Everything here follows the human workflow the tables themselves were made with,
which is also what makes it verifiable: don't guess an address, *observe* one.

  1. `scan_value(pid, 100, "int32")`   — find every address holding 100
  2. user does something in game       — take damage, buy an item, spend money
  3. `refine(pid, hits, 87)`           — keep the addresses that followed along
  4. `watch_writes(pid, addr)`         — ptrace a hardware watchpoint (DR0) on it
                                         and report the instruction that writes
  5. `suggest_patch(insn)`             — "nop the store" / "skip the sub", the raw
                                         material for a new in-place patch

`guided(pid)` runs 1–4 interactively and tells the user exactly what to do in each
round ("take damage until health drops, then tell me the new value").

Notes on why this design: hardware watchpoints need ptrace, so the trainer must own
the process (Launch & attach) or hold CAP_SYS_PTRACE; a watchpoint hit reports RIP,
not the edited address, so the instruction ending at RIP is recovered by
disassembling backwards. Writes that happen in a different thread than the first
trap will re-trigger — collect a few and report the commonest RIP.
"""
from __future__ import annotations

import os
import struct
import sys
import time
from collections import Counter

import capstone as _cs

import ctapply as C
import ctinject as INJ

FORMATS = {
    "int8": ("<b", 1), "byte": ("<b", 1),
    "int16": ("<h", 2), "int32": ("<i", 4), "4 bytes": ("<i", 4),
    "int64": ("<q", 8), "8 bytes": ("<q", 8),
    "float": ("<f", 4), "double": ("<d", 8),
}

DR_OFFSET = 848                 # offsetof(struct user, u_debugreg[0]) on x86-64
DR7_WRITE_LEN4 = 0x1 | (0b01 << 16) | (0b11 << 18)


def _pack(value, kind: str) -> bytes:
    fmt, _ = FORMATS[kind]
    return struct.pack(fmt, value if "f" in fmt or "d" in fmt else int(value))


def unpack(raw: bytes, kind: str):
    fmt, _ = FORMATS[kind]
    return struct.unpack(fmt, raw)[0]


# ---------------------------------------------------------------- scanning
def scan_value(pid: int, value, kind: str = "int32", limit: int = 400_000) -> list[int]:
    """Every writable address currently holding `value`."""
    needle = _pack(value, kind)
    hits: list[int] = []
    for region in C.parse_maps(pid):
        if "w" not in region["perms"]:
            continue
        size = region["end"] - region["start"]
        if size > 512 * 1024 * 1024:
            continue
        try:
            blob = C.read_mem(pid, region["start"], size)
        except OSError:
            continue
        start = 0
        while len(hits) < limit:
            i = blob.find(needle, start)
            if i < 0:
                break
            hits.append(region["start"] + i)
            start = i + 1
    return hits


def refine(pid: int, addrs: list[int], value, kind: str = "int32") -> list[int]:
    """Keep only the addresses whose value is now `value`."""
    want = _pack(value, kind)
    size = FORMATS[kind][1]
    out = []
    for addr in addrs:
        try:
            if C.read_mem(pid, addr, size) == want:
                out.append(addr)
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- watchpoints
def _poke_user(pid: int, offset: int, value: int) -> None:
    import ctypes
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.ptrace(6, pid, ctypes.c_void_p(offset), ctypes.c_void_p(value)) == -1:
        raise OSError(ctypes.get_errno(), "PTRACE_POKEUSER")


def watch_writes(pid: int, addr: int, size: int = 4, timeout: float = 20.0,
                 hits: int = 3) -> dict:
    """Catch the instruction(s) that write to `addr` using a hardware watchpoint."""
    size_code = {1: 0b00, 2: 0b01, 4: 0b11, 8: 0b10}.get(size, 0b11)
    dr7 = 0x1 | (0b01 << 16) | (size_code << 18)
    rips: list[int] = []
    INJ.attach(pid)
    try:
        _poke_user(pid, DR_OFFSET, addr)
        _poke_user(pid, DR_OFFSET + 8 * 7, dr7)
        deadline = time.time() + timeout
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        while time.time() < deadline and len(rips) < hits:
            libc.ptrace(7, pid, None, None)            # PTRACE_CONT
            _, status = os.waitpid(pid, 0)
            # WIFSTOPPED's marker is 0x7f, so compare WSTOPSIG — checking
            # `status & 0x7f == 5` never matches and silently reports no trap.
            if not (os.WIFSTOPPED(status) and os.WSTOPSIG(status) == 5):
                break
            regs = INJ.getregs(pid)
            rips.append(regs.rip)
        _poke_user(pid, DR_OFFSET + 8 * 7, 0)          # clear
    finally:
        try:
            INJ.detach(pid)
        except Exception:
            pass

    if not rips:
        return {"ok": False, "error": f"no write to {addr:#x} within {timeout:.0f}s"}
    common, count = Counter(rips).most_common(1)[0]
    insn = instruction_ending_at(pid, common)
    return {"ok": True, "addr": addr, "rips": rips, "rip": common,
            "count": count, "instruction": insn}


def instruction_ending_at(pid: int, rip: int, lookback: int = 15) -> dict | None:
    """Recover the instruction that ended at `rip`.

    Disassembling backwards from a fixed offset desynchronises on x86 (variable
    instruction length), so try every start offset inside the maximum instruction
    size and keep the decodes whose stream lands exactly on `rip`.
    """
    md = _cs.Cs(_cs.CS_ARCH_X86, _cs.CS_MODE_64)
    candidates: list[dict] = []
    for back in range(1, lookback + 1):
        start = rip - back
        try:
            blob = C.read_mem(pid, start, back + 16)
        except OSError:
            continue
        last = None
        ok = True
        for insn in md.disasm(blob, start):
            last = insn
            if insn.address + insn.size > rip:
                ok = False
                break
            if insn.address + insn.size == rip:
                break
        if ok and last is not None and last.address + last.size == rip:
            candidates.append({"addr": last.address, "bytes": last.bytes.hex(" "),
                               "text": f"{last.mnemonic} {last.op_str}".strip(),
                               "distance": back})
    if not candidates:
        return None
    # prefer a memory-touching instruction (a store), then the closest start
    def rank(c: dict) -> tuple:
        text = c["text"].lower()
        touches_memory = "[" in text
        writes = text.split(" ")[0] in ("mov", "add", "sub", "xor", "or", "and", "inc", "dec", "call")
        return (not (touches_memory and writes), c["distance"])
    best = sorted(candidates, key=rank)[0]
    best.pop("distance", None)
    return best


def suggest_patch(insn: dict | None) -> str:
    if not insn:
        return "could not decode the writing instruction"
    text = insn["text"].lower()
    if "[" in text and ("mov" in text or "add" in text or "sub" in text):
        return (f"'{insn['text']}' writes the value — replace those bytes "
                f"({insn['bytes']}) with NOPs to stop the write")
    if text.startswith("call"):
        return f"'{insn['text']}' calls a setter — patch the call out, or hook it"
    return f"'{insn['text']}' is the writer — inspect before patching"


# ---------------------------------------------------------------- applying it
def apply_nop_patch(pid: int, insn: dict, log=None) -> dict:
    """Overwrite the writing instruction with NOPs; remember the original bytes."""
    addr = insn["addr"]
    orig = bytes.fromhex(insn["bytes"])
    patch = b"\x90" * len(orig)
    INJ.attach(pid)
    try:
        INJ.write(pid, addr, patch)
    finally:
        INJ.detach(pid)
    if log:
        log(f"  patched {addr:#x}: {orig.hex(' ')} -> NOP × {len(orig)}")
    return {"addr": addr, "orig": orig, "new": patch, "text": insn.get("text", "")}


def undo_nop_patch(pid: int, patch: dict, log=None) -> None:
    INJ.attach(pid)
    try:
        INJ.write(pid, patch["addr"], patch["orig"])
    finally:
        INJ.detach(pid)
    if log:
        log(f"  restored {patch['addr']:#x}: {patch['orig'].hex(' ')}")


# ---------------------------------------------------------------- guided flow
STEPS = {
    "health": "take damage until the number drops (walk into fire, let an enemy hit you)",
    "money": "buy or sell something cheap so the number changes",
    "ammo": "fire a few rounds",
    "stamina": "sprint until the bar drains",
    "generic": "do whatever changes the number (spend, take damage, fire, jump)",
}


def guided(pid: int, kind: str = "int32", thing: str = "generic") -> dict | None:
    """Interactive: scan → ask the user to act in game → refine → watch writes."""
    action = STEPS.get(thing, STEPS["generic"])
    try:
        first = input(f"Current {thing} value in game: ").strip()
    except EOFError:
        return None
    if not first:
        return None
    value = float(first) if kind in ("float", "double") else int(first, 0)
    hits = scan_value(pid, value, kind)
    print(f"{len(hits)} address(es) hold {value}")
    for step in range(6):
        if len(hits) <= 4:
            break
        print(f"\nIn game: {action}.")
        nxt = input("New value now: ").strip()
        if not nxt:
            break
        value = float(nxt) if kind in ("float", "double") else int(nxt, 0)
        hits = refine(pid, hits, value, kind)
        print(f"  {len(hits)} candidate(s) left: {[hex(h) for h in hits[:6]]}")
    if not hits:
        print("no candidates left — the value may be stored as a different type")
        return None
    addr = hits[0] if len(hits) == 1 else None
    if addr is None:
        pick = input(f"Which address? {[hex(h) for h in hits]} ").strip()
        addr = int(pick, 16) if pick else hits[0]
    print(f"\nWatching writes to {addr:#x} — {action} once more…")
    result = watch_writes(pid, addr, FORMATS[kind][1])
    if result.get("ok"):
        insn = result.get("instruction")
        print(f"  writer: {insn['text'] if insn else '?'} at {result['rip']:#x}")
        print(f"  suggestion: {suggest_patch(insn)}")
    else:
        print(f"  {result.get('error')}")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    print(guided(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "int32",
                 sys.argv[3] if len(sys.argv) > 3 else "generic"))
