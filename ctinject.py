#!/usr/bin/env python3
"""
ctinject — code-injection half of AA translation.

ptrace primitives + minimal x86-64 patching: write a `jmp` hook into a running
process, inject code into an executable cave, and restore on disable.

For arbitrary AA scripts you need a full assembler (keystone); this module ships
hand-rolled encodings for the common forms and proves the injection mechanics.
"""
import ctypes
import os
import struct

from keystone import Ks, KS_ARCH_X86, KS_MODE_64

_KS = Ks(KS_ARCH_X86, KS_MODE_64)

libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.ptrace.restype = ctypes.c_long
libc.ptrace.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]

PTRACE_ATTACH = 16
PTRACE_DETACH = 17
PTRACE_PEEKDATA = 2
PTRACE_POKEDATA = 5
PTRACE_CONT = 7
PTRACE_GETREGS = 12
PTRACE_SETREGS = 13


class UserRegs(ctypes.Structure):
    _fields_ = [
        ("r15", ctypes.c_ulong), ("r14", ctypes.c_ulong), ("r13", ctypes.c_ulong), ("r12", ctypes.c_ulong),
        ("rbp", ctypes.c_ulong), ("rbx", ctypes.c_ulong), ("r11", ctypes.c_ulong), ("r10", ctypes.c_ulong),
        ("r9", ctypes.c_ulong), ("r8", ctypes.c_ulong), ("rax", ctypes.c_ulong), ("rcx", ctypes.c_ulong),
        ("rdx", ctypes.c_ulong), ("rsi", ctypes.c_ulong), ("rdi", ctypes.c_ulong), ("orig_rax", ctypes.c_ulong),
        ("rip", ctypes.c_ulong), ("cs", ctypes.c_ulong), ("eflags", ctypes.c_ulong), ("rsp", ctypes.c_ulong),
        ("ss", ctypes.c_ulong), ("fs_base", ctypes.c_ulong), ("gs_base", ctypes.c_ulong),
        ("ds", ctypes.c_ulong), ("es", ctypes.c_ulong), ("fs", ctypes.c_ulong), ("gs", ctypes.c_ulong),
    ]


def getregs(pid: int) -> "UserRegs":
    regs = UserRegs()
    libc.ptrace(PTRACE_GETREGS, pid, 0, ctypes.byref(regs))
    return regs


def setregs(pid: int, regs: "UserRegs") -> None:
    libc.ptrace(PTRACE_SETREGS, pid, 0, ctypes.byref(regs))


def attach(pid: int) -> None:
    if libc.ptrace(PTRACE_ATTACH, pid, 0, 0) == -1:
        raise OSError(ctypes.get_errno(), "ptrace attach")
    os.waitpid(pid, 0)


def detach(pid: int) -> None:
    libc.ptrace(PTRACE_DETACH, pid, 0, 0)


def cont(pid: int) -> None:
    libc.ptrace(PTRACE_CONT, pid, 0, 0)


def _peek(pid: int, addr: int) -> int:
    ctypes.set_errno(0)
    w = libc.ptrace(PTRACE_PEEKDATA, pid, addr, 0)
    if w == -1 and ctypes.get_errno() != 0:
        raise OSError(ctypes.get_errno(), "ptrace peek")
    return w & 0xFFFFFFFFFFFFFFFF


def read(pid: int, addr: int, size: int) -> bytes:
    out = bytearray()
    for off in range(0, size, 8):
        out += _peek(pid, addr + off).to_bytes(8, "little")
    return bytes(out[:size])


def write(pid: int, addr: int, data: bytes) -> None:
    # byte-wise read-modify-write at 8-byte word boundaries
    for i, b in enumerate(data):
        base = (addr + i) & ~7
        off = (addr + i) & 7
        w = _peek(pid, base)
        buf = bytearray(w.to_bytes(8, "little"))
        buf[off] = b
        libc.ptrace(PTRACE_POKEDATA, pid, base, int.from_bytes(buf, "little"))


# ------------------------------------------------------- minimal x86-64 encoders
def mov_mem_imm32(abs_addr: int, imm32: int) -> bytes:
    """mov dword ptr [abs_addr], imm32  ->  C7 04 25 <disp32> <imm32>

    NOTE: C7 05 would be RIP-relative in 64-bit mode; the SIB byte 0x25
    (no base, no index) is required for a true absolute [disp32]."""
    return b"\xC7\x04\x25" + struct.pack("<I", abs_addr & 0xFFFFFFFF) + struct.pack("<i", imm32)


def jmp_rel32(src: int, dst: int) -> bytes:
    """jmp dst (5-byte relative)"""
    return b"\xE9" + struct.pack("<i", dst - (src + 5))


def ret() -> bytes:
    return b"\xC3"


def nop(n: int) -> bytes:
    return b"\x90" * n


def assemble(asm: str, base_addr: int = 0) -> bytes:
    """Assemble x86-64 to bytes via keystone. Pass the code's target address so
    RIP-relative operands resolve correctly."""
    code, _ = _KS.asm(asm, addr=base_addr)
    return bytes(code)


# next low-address hint for code-cave mmap. Successive caves must not re-use the
# same hint — the earlier one stays mapped, so a repeat hint would push the
# kernel's placement out of E9 rel32 range. Advances after each allocation.
_NEXT_CAVE = 0x10000000


def _mmap_stub(size: int, hint: int) -> bytes:
    asm = (
        "pushfq; "
        "push rax; push rcx; push rdx; push rsi; push rdi; push r8; push r9; push r10; "
        "mov rax, 9; "          # SYS_mmap
        f"mov rdi, {hint:#x}; "  # hint: low address near game code (so E9 rel32 reaches)
        f"mov rsi, {size}; "    # length
        "mov rdx, 7; "          # PROT_READ|WRITE|EXEC
        "mov r10, 0x22; "       # MAP_PRIVATE|MAP_ANONYMOUS
        "mov r8, -1; "          # fd = -1
        "mov r9, 0; "           # offset
        "syscall; "
        "mov r11, rax; "        # stash result in r11 (caller-saved scratch)
        "pop r10; pop r9; pop r8; pop rdi; pop rsi; pop rdx; pop rcx; pop rax; "
        "popfq; "
        "int3"                  # stop; tracer reads r11
    )
    return assemble(asm)


def remote_mmap(pid: int, size: int, hook_addr: int, stub_addr: int) -> int:
    """Allocate rwx memory in the target by jmp-hooking `hook_addr` to an mmap
    syscall stub placed at `stub_addr` (must be an r-x region).

    Uses the jmp-hook path (a direct E9 branch), which is reliable — direct
    ptrace RIP manipulation segfaults under this box's CET shadow-stack."""
    global _NEXT_CAVE
    # Hint near the injection point (~256MB below), so the 5-byte E9 hook stays
    # in ±2GB range. A fixed low 0x10000000 would be ~5GB from a 64-bit game
    # loaded at 0x140000000 and overflow rel32.
    near = hook_addr - 0x10000000
    if near < 0x10000000:
        near = 0x10000000
    hint = max(_NEXT_CAVE, near) & ~0xFFF
    stub = _mmap_stub(size, hint)
    write(pid, stub_addr, stub)
    orig = patch_jmp(pid, hook_addr, stub_addr)
    # resume; the target will reach hook_addr -> stub -> mmap -> int3 (SIGTRAP)
    libc.ptrace(PTRACE_CONT, pid, 0, 0)
    _, status = os.waitpid(pid, 0)
    if not (os.WIFSTOPPED(status) and os.WSTOPSIG(status) == 5):
        restore(pid, hook_addr, orig)
        raise OSError(f"remote mmap: unexpected stop (status={status})")
    fin = getregs(pid)
    result = fin.r11  # stashed by the stub (registers are otherwise preserved)
    if result >= (1 << 63):
        result -= (1 << 64)
    if result < 0:
        restore(pid, hook_addr, orig)
        raise OSError(-result, "remote mmap")
    # advance the next hint past this actual allocation so a later cave doesn't
    # collide and land out of E9 rel32 range
    _NEXT_CAVE = (result + size + 0x1000) & ~0xFFF
    # restore the hook and rewind rip so the target re-runs the original code
    restore(pid, hook_addr, orig)
    regs = getregs(pid)
    regs.rip = hook_addr
    setregs(pid, regs)
    return result


# ------------------------------------------------------- high-level operations
def patch_jmp(pid: int, hook_addr: int, target_addr: int) -> bytes:
    """Overwrite 5 bytes at hook_addr with `jmp target`; return original bytes."""
    orig = read(pid, hook_addr, 5)
    write(pid, hook_addr, jmp_rel32(hook_addr, target_addr))
    return orig


def restore(pid: int, addr: int, orig: bytes) -> None:
    write(pid, addr, orig)
