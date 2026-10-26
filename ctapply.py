#!/usr/bin/env python3
"""
ctapply — Cheat Engine (.ct) apply engine for Linux.

Parse a .ct table, list its cheats, and (given a running process) resolve each
cheat's memory address (absolute, module+offset, or pointer path) so a front
end can read/write/freeze the value.

This is "apply mode". "Scan mode" (finding an unknown address) is scanmem's job.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import re
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------- process memory
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


class IoVec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


libc.process_vm_readv.argtypes = [ctypes.c_int, ctypes.POINTER(IoVec), ctypes.c_ulong,
                                  ctypes.POINTER(IoVec), ctypes.c_ulong, ctypes.c_ulong]
libc.process_vm_readv.restype = ctypes.c_ssize_t
libc.process_vm_writev.argtypes = [ctypes.c_int, ctypes.POINTER(IoVec), ctypes.c_ulong,
                                   ctypes.POINTER(IoVec), ctypes.c_ulong, ctypes.c_ulong]
libc.process_vm_writev.restype = ctypes.c_ssize_t


def read_mem(pid: int, addr: int, size: int) -> bytes:
    """Read `size` raw bytes from the target's address space via process_vm_readv
    (no ptrace needed; works on same-uid processes)."""
    buf = ctypes.create_string_buffer(size)
    local = IoVec(ctypes.cast(buf, ctypes.c_void_p), size)
    remote = IoVec(ctypes.c_void_p(addr), size)
    n = libc.process_vm_readv(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
    if n < 0:
        raise OSError(ctypes.get_errno(), "process_vm_readv")
    return buf.raw[:n]


def write_mem(pid: int, addr: int, data: bytes) -> int:
    """Write raw bytes to the target via process_vm_writev; returns bytes written."""
    src = ctypes.create_string_buffer(data)
    local = IoVec(ctypes.cast(src, ctypes.c_void_p), len(data))
    remote = IoVec(ctypes.c_void_p(addr), len(data))
    n = libc.process_vm_writev(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
    if n < 0:
        raise OSError(ctypes.get_errno(), "process_vm_writev")
    return n


def read_ptr(pid: int, addr: int, width: int = 8) -> int:
    """Read a pointer (8- or 4-byte) and return it as an int."""
    return struct.unpack("<Q" if width == 8 else "<I", read_mem(pid, addr, width))[0]


def read_value(pid: int, addr: int, size: int) -> int:
    """Read a little-endian integer of `size` bytes (1/2/4/8)."""
    return struct.unpack("<Q" if size == 8 else "<I" if size == 4 else "<H" if size == 2 else "<B",
                         read_mem(pid, addr, size))[0]


def write_value(pid: int, addr: int, value: int, size: int) -> int:
    """Write a little-endian integer of `size` bytes; returns bytes written."""
    fmt = "<Q" if size == 8 else "<I" if size == 4 else "<H" if size == 2 else "<B"
    return write_mem(pid, addr, struct.pack(fmt, value & ((1 << (size * 8)) - 1)))


# ---------------------------------------------------------------- /proc/<pid>/maps
def parse_maps(pid: int) -> list[dict]:
    """Parse /proc/<pid>/maps into region dicts (start/end/perms/offset/path)."""
    regions = []
    for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        addr, perms, offset, _dev, _inode, path = parts
        s, e = addr.split("-")
        regions.append({
            "start": int(s, 16), "end": int(e, 16), "perms": perms,
            "offset": int(offset, 16), "path": path.strip(),
        })
    return regions


def module_bases(pid: int) -> dict[str, int]:
    """Map module basename -> base address (first mapping with offset 0)."""
    bases: dict[str, int] = {}
    for r in parse_maps(pid):
        if r["path"] and r["offset"] == 0 and r["path"].startswith("/"):
            name = Path(r["path"]).name
            bases.setdefault(name.lower(), r["start"])
    return bases


def find_module(bases: dict[str, int], name: str) -> int | None:
    n = name.lower().strip()
    if n in bases:
        return bases[n]
    # allow "GameAssembly.dll" to match a path whose basename differs slightly
    for k, v in bases.items():
        if k.startswith(n) or n.startswith(k):
            return v
    return None


# ---------------------------------------------------------------- .ct parsing
def parse_int(s: str) -> int:
    """Addresses/offsets in .ct are hex (CE convention)."""
    return int(s.strip().lower().replace("0x", ""), 16)


def _text(el: ET.Element, name: str) -> str | None:
    c = el.find(name)
    return c.text.strip() if c is not None and c.text else None


def parse_entry(el: ET.Element) -> dict:
    """Recursively parse one <CheatEntry> into a dict (description/type/address/
    offsets/script/children)."""
    offs = el.find("Offsets")
    return {
        "description": (_text(el, "Description") or "").strip('"'),
        "type": _text(el, "VariableType") or "",
        "address": _text(el, "Address"),
        "offsets": [o.text.strip() for o in el.findall("./Offsets/Offset") if o is not None and o.text],
        "script": _text(el, "AssemblerScript"),
        "children": [parse_entry(c) for c in el.findall("CheatEntries/CheatEntry")],
    }


def parse_ct(path: str) -> list[dict]:
    root = ET.parse(path).getroot()
    return [parse_entry(c) for c in root.findall("CheatEntries/CheatEntry")]


def flatten(entries: list[dict], prefix: tuple = ()) -> list[tuple[tuple, dict]]:
    """Walk the cheat tree; yield (path, entry)."""
    out = []
    for e in entries:
        p = prefix + (e["description"],)
        out.append((p, e))
        out.extend(flatten(e["children"], p))
    return out


# ---------------------------------------------------------------- address resolution
def _base_addr(addr_str: str | None, bases: dict[str, int]) -> int | None:
    if addr_str is None:
        return None
    s = addr_str.strip()
    m = re.fullmatch(r"([^+\s]+)\+([0-9A-Fa-fx]+(?:\+[0-9A-Fa-fx]+)*)", s)
    if m:  # module+offset
        base = find_module(bases, m.group(1))
        if base is None:
            return None
        return base + sum(parse_int(x) for x in m.group(2).split("+"))
    if re.fullmatch(r"[0-9A-Fa-f]+", s):  # absolute hex
        return parse_int(s)
    return None  # symbol names / AOB entries — need live resolution


def resolve(pid: int, entry: dict, bases: dict[str, int]) -> int | None:
    base = _base_addr(entry["address"], bases)
    if base is None:
        return None
    offs = [parse_int(o) for o in entry["offsets"]]
    if not offs:
        return base
    # Cheat Engine pointer path: value = [[base]+o0]+o1 ... final offset added
    # after the last dereference.
    addr = read_ptr(pid, base)
    for off in offs[:-1]:
        addr = read_ptr(pid, addr + off)
    return addr + offs[-1]


# ---------------------------------------------------------------- CLI
def cmd_list(args) -> None:
    entries = parse_ct(args.file)
    flat = flatten(entries)
    print(f"{args.file}: {len(flat)} cheat entries\n")
    for path, e in flat:
        indent = "  " * (len(path) - 1)
        addr = e["address"] or "(script/no address)"
        off = f"  offsets={e['offsets']}" if e["offsets"] else ""
        typ = f"  [{e['type']}]" if e["type"] else ""
        print(f"{indent}• {path[-1]}  -> {addr}{off}{typ}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="ctapply", description="Cheat Engine .ct apply engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list cheats in a .ct file")
    p.add_argument("file")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("modules", help="list module base addresses of a running process")
    p.add_argument("pid", type=int)
    p.set_defaults(func=lambda a: [print(f"{v:#x}  {k}") for k, v in sorted(module_bases(a.pid).items())])

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
