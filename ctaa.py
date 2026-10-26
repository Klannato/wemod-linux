#!/usr/bin/env python3
"""
ctaa — Auto Assembler (AA) support: AOB scanning + script parsing.

The portable half of AA-script translation. AOB signatures and the injected
machine code are identical under Wine/Proton (same Windows binary); module-base
resolution differs and is handled by ctapply. This module:
  - finds an AOB signature in a module (aob_scan_module)
  - parses an AA script's directives (aobscanmodule/aobscan/alloc/label/
    registersymbol/db-restore, [ENABLE]/[DISABLE])

The injection half (assemble x86-64, write the jmp patch, alloc a code cave) is
a follow-on layer — it needs an assembler (keystone) + ptrace/mmap.
"""
from __future__ import annotations

import re
import struct

import capstone as _cs

import ctapply as C
import ctinject as INJ


# ------------------------------------------------------------------ AOB scan
def compile_pattern(pattern: str):
    """'48 8B ?? 00' -> (bytes, mask); mask[j] True means a fixed byte.

    Handles '??' and '*' wildcards and contiguous hex (no spaces)."""
    s = pattern.strip()
    if " " in s or "\t" in s:
        toks = s.split()
    else:
        toks = [s[i:i + 2] for i in range(0, len(s), 2)]
    mask, data = [], bytearray()
    for t in toks:
        if t in ("??", "?", "*", "**"):
            data.append(0)
            mask.append(False)
        else:
            data.append(int(t, 16))
            mask.append(True)
    return bytes(data), mask


def aob_scan(pid: int, start: int, end: int, pattern: str) -> int | None:
    pat, mask = compile_pattern(pattern)
    n = len(pat)
    CHUNK = 0x20000
    for base in range(start, end, CHUNK):
        size = min(CHUNK, end - base)
        try:
            data = C.read_mem(pid, base, size)
        except OSError:
            continue
        for i in range(len(data) - n + 1):
            if all((not mask[j]) or data[i + j] == pat[j] for j in range(n)):
                return base + i
    return None


def aob_scan_module(pid: int, module: str, pattern: str) -> int | None:
    """Scan a module's executable regions for the pattern."""
    mod = module.lower().strip('"')
    for r in C.parse_maps(pid):
        if r["path"].lower().endswith(mod) and "x" in r["perms"]:
            a = aob_scan(pid, r["start"], r["end"], pattern)
            if a is not None:
                return a
    return None


# ------------------------------------------------------------------ AA parse
def _groups(pat: str, text: str) -> list[tuple]:
    return [m.groups() for m in re.finditer(pat, text, re.DOTALL)]


def parse_aa(text: str) -> dict:
    m = re.search(r"\[ENABLE\](.*?)(?=\[DISABLE\]|$)", text, re.DOTALL)
    enable = m.group(1) if m else ""
    m = re.search(r"\[DISABLE\](.*)", text, re.DOTALL)
    disable = m.group(1) if m else ""

    aob = []
    for sym, mod, pat in _groups(r"aobscanmodule\s*\(\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*(.*?)\)", enable):
        aob.append({"symbol": sym.strip(), "module": mod.strip().strip('"'), "pattern": pat.strip()})
    for sym, pat in _groups(r"aobscan\s*\(\s*([^,]+?)\s*,\s*(.*?)\)", enable):
        aob.append({"symbol": sym.strip(), "module": None, "pattern": pat.strip()})

    allocs = [{"symbol": g[0].strip(), "size": g[1].strip()}
              for g in _groups(r"alloc\s*\(\s*([^,]+?)\s*,\s*([^),]+)", enable)]
    labels = [l.strip() for g in _groups(r"label\s*\(([^)]*)\)", enable)
              for l in g[0].split(",") if l.strip()]
    regs = [r.strip() for g in _groups(r"registersymbol\s*\(([^)]*)\)", enable)
            for r in g[0].split(",") if r.strip()]
    db_restore = [g[0].strip() for g in _groups(r"\bdb\s+([0-9A-Fa-f ?]+)", disable)]

    return {
        "aobscans": aob,
        "allocs": allocs,
        "labels": labels,
        "registersymbols": regs,
        "db_restore": db_restore,
        "has_lua": "{$lua}" in text,
    }


def describe_aa(parsed: dict) -> str:
    """One-line summary of what a parsed AA script does."""
    if not parsed["aobscans"]:
        return "no AOB scan (direct-address or symbol script)"
    parts = []
    for a in parsed["aobscans"]:
        mod = a["module"] or "(exe)"
        parts.append(f"AOB {mod} :: {a['pattern'][:40]}{'…' if len(a['pattern']) > 40 else ''}")
    if parsed["allocs"]:
        parts.append(f"alloc {len(parsed['allocs'])} cave(s)")
    if parsed["registersymbols"]:
        parts.append(f"registers {', '.join(parsed['registersymbols'][:4])}")
    return " | ".join(parts)


# ---------------------------------------------------------------- enable/disable
def aa_enable(pid: int, aob_module: str, aob_pattern: str, cave_asm: str,
              mmap_hook: int, mmap_stub: int, overwrite_len: int = 5) -> dict:
    """Enable a trampoline hook.

    AOB-scan the injection point, remote-mmap a code cave, then assemble the
    cave as:  [cave_asm] + [original overwrite_len bytes] + [jmp back]  and
    hook the injection point with a `jmp cave`. Returns a handle for aa_disable.

    mmap_hook = a frequently-called function (to run the mmap stub), mmap_stub =
    an r-x spot for the stub. For real games both need finding; test scaffolding
    passes tick/cave."""
    inj = aob_scan_module(pid, aob_module, aob_pattern)
    if inj is None:
        raise ValueError(f"AOB not found: {aob_pattern[:50]}")
    cave = INJ.remote_mmap(pid, 4096, hook_addr=mmap_hook, stub_addr=mmap_stub)
    new_bytes = INJ.assemble(cave_asm, base_addr=cave)
    orig = INJ.read(pid, inj, overwrite_len)
    back_addr = cave + len(new_bytes) + overwrite_len
    back = INJ.assemble(f"jmp {inj + overwrite_len:#x}", base_addr=back_addr)
    INJ.write(pid, cave, new_bytes + orig + back)
    INJ.patch_jmp(pid, inj, cave)
    return {"injection": inj, "cave": cave, "orig": orig, "overwrite_len": overwrite_len}


def aa_disable(pid: int, handle: dict) -> None:
    INJ.restore(pid, handle["injection"], handle["orig"])


# ------------------------------------------------------------ script -> injection
_DT = {"db": 1, "dw": 2, "dd": 4, "dq": 8}

# capstone disassembler for the RIP-relative fixup. `readmem` copies original
# bytes into the cave; if those bytes reference a global via [rip+disp32], moving
# them changes RIP, so the displacement must be re-homed.
_MD = _cs.Cs(_cs.CS_ARCH_X86, _cs.CS_MODE_64)
_MD.detail = True

# Placeholder address used during the size pass for not-yet-resolved (forward)
# labels. It must stay in the imm32 size class (like every cave address, which
# sits at 0x10000000) so the encoded instruction size doesn't shift between the
# two passes.
_PLACEHOLDER = 0x11111111

# A label reference, optionally with arithmetic: `name`, `name+0x10`, `name-8`.
_LABEL_EXPR = r"[A-Za-z_]\w*(?:\s*[+-]\s*(?:0x[0-9a-fA-F]+|\d+))?"


def _ce_normalize(line: str) -> str:
    """Translate Cheat Engine AA syntax to keystone-compatible syntax.

    CE writes decimal immediates as `#200`, bare hex offsets in displacements
    (`[rdx+B650]`, `[rdx+0000B650]`), and `nop 3` for 3 padding bytes; keystone
    wants `200`, `0xB650`, and explicit nops. Only offsets containing BOTH a
    digit and an A-F letter are treated as hex (a bare `40` stays unchanged —
    it's ambiguous and usually harmless)."""
    line = re.sub(r"#([0-9]+)", r"\1", line)                    # #200 -> 200

    def _hex(m):
        tok = m.group(2)
        if re.search(r"[0-9]", tok) and re.search(r"[A-Fa-f]", tok):
            return f"{m.group(1)}0x{tok}{m.group(3)}"
        return m.group(0)
    line = re.sub(r"([+\-])([0-9A-Fa-f]+)(\s*\])", _hex, line)  # +B650] -> +0xB650]

    def _nops(m):
        return ("nop; " * int(m.group(1)))[:-2]
    line = re.sub(r"\bnop\s+(\d+)\b", _nops, line)              # nop 3 -> nop; nop; nop

    # mov [mem], imm with no explicit size -> dword ptr (CE's default write size);
    # keystone can't infer the width from a bare immediate.
    line = re.sub(r"\bmov\s+(\[[^\]]+\])\s*,\s*(-?(?:0x[0-9a-fA-F]+|\d+))\b",
                  r"mov dword ptr \1, \2", line)
    return line


def _resolve_expr(expr: str, labels: dict, placeholder: int | None = None) -> int | None:
    """Resolve a label expression (`name`, `name+imm`, `name-imm`) to an address.

    Returns the placeholder when the label isn't known yet (the size pass), and
    None when the expression isn't a label at all (caller keeps the literal)."""
    m = re.match(r"^([A-Za-z_]\w*)\s*([+-]\s*(?:0x[0-9a-fA-F]+|\d+))?$", expr.strip())
    if not m:
        return None
    name, adj = m.group(1), (m.group(2) or "").replace(" ", "")
    base = labels.get(name)
    if base is None:
        return placeholder
    if not adj:
        return base
    return base + (1 if adj[0] == "+" else -1) * int(adj[1:], 0)


def _resolve_labels(asm: str, labels: dict, placeholder: int | None = None) -> str:
    """Rewrite label references (with optional +imm/-imm) as absolute addresses.

    `jmp/call/jcc LABEL` -> the target address; `mov/lea REG, LABEL` -> the
    label's address (CE's 'lea' convention, i.e. an immediate move of the
    address). Unresolved labels become the placeholder during the size pass."""
    def _jmp(m):
        addr = _resolve_expr(m.group(2), labels, placeholder)
        return f"{m.group(1)} {addr:#x}" if addr is not None else m.group(0)

    def _mov(m):
        addr = _resolve_expr(m.group(3), labels, placeholder)
        return f"{m.group(1)} {m.group(2)}, {addr:#x}" if addr is not None else m.group(0)

    asm = re.sub(r"\b(jmp|call|jz|jnz|je|jne|ja|jb|jg|jl|jge|jle|jae|jbe)\s+(" + _LABEL_EXPR + ")",
                 _jmp, asm)
    asm = re.sub(r"\b(mov|lea)\s+([a-z][a-z0-9]*)\s*,\s*(" + _LABEL_EXPR + r")\s*$",
                 _mov, asm)
    return asm


def fix_rip_relative(chunk: bytes, src_base: int, dst_base: int) -> bytes:
    """Re-home RIP-relative operands in `chunk` after relocating it from src_base
    to dst_base. Each [rip+disp32] reference is shifted by (src_base - dst_base)
    so it still resolves to the same absolute address."""
    delta = src_base - dst_base
    out = bytearray(chunk)
    for insn in _MD.disasm(chunk, src_base):
        enc = insn._raw.detail.contents.arch.x86.encoding
        if enc.disp_size != 4:          # disp_size is in bytes; 4 == a 32-bit disp
            continue
        for op in insn.operands:
            if op.type == _cs.x86.X86_OP_MEM and op.mem.base == _cs.x86.X86_REG_RIP:
                off = enc.disp_offset
                out[off:off + 4] = struct.pack("<I", (op.mem.disp + delta) & 0xFFFFFFFF)
                break
    return bytes(out)


def _assemble_block(pid: int, lines: list[str], labels: dict, base: int) -> bytes:
    """Assemble a cave body in two passes.

    Pass 1 sizes each instruction (using a placeholder for forward labels) and
    records every label's offset, so forward references resolve. Pass 2 re-walks
    and emits real bytes, fixing up RIP-relative operands inside readmem copies.
    `labels` is mutated in place to pick up cave-local labels (data slots, etc.)."""
    # --- pass 1: compute label offsets ---
    cur = base
    for raw in lines:
        l = raw.strip()
        if not l or l.startswith("//") or l.startswith("{"):
            continue
        m = re.match(r"^([A-Za-z_]\w*):$", l)          # label definition
        if m:
            labels[m.group(1)] = cur
            continue
        m = re.match(r"^readmem\s*\(\s*(\w+)\s*,\s*(\d+)\s*\)$", l)
        if m:
            cur += int(m.group(2))
            continue
        m = re.match(r"^(d[bdwq])\s+(.+)$", l)          # data directive
        if m:
            size = _DT[m.group(1)]
            vals = [int(t, 0) for t in m.group(2).replace(",", " ").split() if t.strip()]
            cur += size * len(vals)
            continue
        cur += len(INJ.assemble(_resolve_labels(_ce_normalize(l), labels, _PLACEHOLDER), base_addr=cur))

    # --- pass 2: emit with resolved addresses ---
    out = bytearray()
    cur = base
    for raw in lines:
        l = raw.strip()
        if not l or l.startswith("//") or l.startswith("{"):
            continue
        if re.match(r"^([A-Za-z_]\w*):$", l):           # already recorded in pass 1
            continue
        m = re.match(r"^readmem\s*\(\s*(\w+)\s*,\s*(\d+)\s*\)$", l)
        if m:
            n = int(m.group(2))
            src = labels[m.group(1)]
            out += fix_rip_relative(INJ.read(pid, src, n), src, cur)
            cur += n
            continue
        m = re.match(r"^(d[bdwq])\s+(.+)$", l)          # data directive
        if m:
            size = _DT[m.group(1)]
            vals = [int(t, 0) for t in m.group(2).replace(",", " ").split() if t.strip()]
            b = b"".join(v.to_bytes(size, "little") for v in vals)
            out += b
            cur += len(b)
            continue
        b = INJ.assemble(_resolve_labels(_ce_normalize(l), labels), base_addr=cur)
        out += b
        cur += len(b)
    return bytes(out)


def run_aa_script(pid: int, script_text: str, mmap_hook: int, mmap_stub: int) -> dict:
    """Run a common AA script: aobscanmodule + alloc + label + readmem trampoline.

    Covers the value-write/trampoline shape — `label(code/return)`, forward data
    labels, label arithmetic (`label+0x10`), `readmem` (with RIP-relative fixup),
    `jmp/call <label>`, `mov reg, <label>`, `db/dw/dd/dq`, and [DISABLE] `db`
    restore. Lua (`{$lua}`) and `reassemble` are not supported."""
    p = parse_aa(script_text)
    if p["has_lua"]:
        raise ValueError("Lua scripts ({$lua}) are not supported — they require CE's Lua engine")
    if not p["aobscans"]:
        raise ValueError("script has no AOB scan")
    aob = p["aobscans"][0]
    alloc_size = int(p["allocs"][0]["size"], 0) if p["allocs"] else 4096
    alloc_sym = p["allocs"][0]["symbol"] if p["allocs"] else None
    aob_sym = aob["symbol"]

    inj = aob_scan_module(pid, aob["module"] or "", aob["pattern"])
    if inj is None:
        raise ValueError(f"AOB not found: {aob['pattern'][:50]}")
    overwrite = len(compile_pattern(p["db_restore"][0])[0]) if p["db_restore"] else 5
    cave = INJ.remote_mmap(pid, alloc_size, hook_addr=mmap_hook, stub_addr=mmap_stub)

    enable = re.search(r"\[ENABLE\](.*?)(?=\[DISABLE\]|$)", script_text, re.DOTALL).group(1)
    cave_lines, hook_lines, mode = [], [], None
    for l in enable.splitlines():
        s = l.strip()
        if alloc_sym and re.match(rf"^{re.escape(alloc_sym)}:", s):
            mode = "cave"
            continue
        if re.match(rf"^{re.escape(aob_sym)}:", s):
            mode = "hook"
            continue
        (cave_lines if mode == "cave" else hook_lines if mode == "hook" else []).append(l)

    labels = {aob_sym: inj, "code": cave, "return": inj + overwrite}
    if alloc_sym:
        labels[alloc_sym] = cave

    cave_bytes = _assemble_block(pid, cave_lines, labels, cave)
    # the hook is the jmp plus any `nop N` padding that fills the overwritten span
    hook_instrs = []
    for l in hook_lines:
        s = l.strip()
        if not s or s.startswith("//") or re.match(r"^([A-Za-z_]\w*):$", s):
            continue
        if re.match(r"^(registersymbol|unregistersymbol|dealloc|label)\b", s):
            continue  # directive, not an instruction
        hook_instrs.append(s)
    if not any(re.match(r"^\s*jmp\s", i) for i in hook_instrs):
        raise ValueError("no jmp hook found in script")
    hook_asm = "; ".join(_resolve_labels(_ce_normalize(i), labels) for i in hook_instrs)
    hook_bytes = INJ.assemble(hook_asm, base_addr=inj)

    orig = INJ.read(pid, inj, overwrite)
    INJ.write(pid, cave, cave_bytes)
    INJ.write(pid, inj, hook_bytes)
    return {"injection": inj, "cave": cave, "orig": orig, "overwrite_len": overwrite,
            "labels": labels}


def find_code_cave(pid: int, size: int, scan_limit: int = 0x40000) -> int | None:
    """Find a run of padding (0x90/0x00/0xCC) at least `size` bytes long in an
    executable region — a spot to drop the mmap stub. Returns its address."""
    for r in C.parse_maps(pid):
        if "x" not in r["perms"] or not r["path"]:
            continue
        length = min(r["end"] - r["start"], scan_limit)
        try:
            data = C.read_mem(pid, r["start"], length)
        except OSError:
            continue
        run_start = None
        for i, b in enumerate(data):
            if b in (0x90, 0x00, 0xCC):
                if run_start is None:
                    run_start = i
                if i - run_start + 1 >= size:
                    return r["start"] + run_start
            else:
                run_start = None
    return None


def auto_enable(pid: int, script_text: str) -> dict:
    """Full AA enable with auto-discovery — the real-game entry point.

    AOB-scans the injection point (also used as the mmap carrier, since an
    aobscanmodule target is hot code), finds a padding cave for the mmap stub,
    then runs the script. Returns the run_aa_script handle."""
    p = parse_aa(script_text)
    if not p["aobscans"]:
        raise ValueError("script has no AOB scan")
    aob = p["aobscans"][0]
    inj = aob_scan_module(pid, aob["module"] or "", aob["pattern"])
    if inj is None:
        raise ValueError(f"AOB not found: {aob['pattern'][:50]}")
    stub = find_code_cave(pid, 128)
    if stub is None:
        raise ValueError("no code cave found for the mmap stub")
    return run_aa_script(pid, script_text, mmap_hook=inj, mmap_stub=stub)
