#!/usr/bin/env python3
"""Prove native/ptrace_allow.so does what the launcher needs.

The hard case is a target that has *left* our process subtree: Yama (ptrace_scope=1)
then refuses both process_vm_readv and PTRACE_ATTACH. Wine's wineserver does exactly
that to a Proton game, so a passing test here is the whole premise of option 1.

Three controls:
  A  no shim, fork+exec detach    -> expect EPERM   (documents the problem)
  B  shim, fork+exec detach       -> expect success (the Wine shape)
  C  shim, fork-only detach       -> expect success (covered by pthread_atfork)

Run: python3 tests/test_ptrace_allow.py
"""
import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "native" / "ptrace_allow.so"
sys.path.insert(0, str(ROOT))

import ctapply as C

# Stage 1 detaches: double fork + setsid, then either execs a fresh image
# (Wine's shape: wineserver/preloader hand over with exec) or keeps forking.
LAUNCHER = r"""
import os, sys, time
mode, pid_file = sys.argv[1], sys.argv[2]
SLEEPER = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
if os.fork() > 0: sys.exit(0)
os.setsid()
if os.fork() > 0: sys.exit(0)
if mode == "exec":
    os.execv(sys.executable, [sys.executable, "-c", SLEEPER, pid_file])
open(pid_file, "w").write(str(os.getpid()))
time.sleep(30)
"""


def spawn_detached(mode: str, pid_file: str, env_extra: dict) -> int:
    env = dict(os.environ, **env_extra)
    subprocess.run([sys.executable, "-c", LAUNCHER, mode, pid_file], env=env, check=True)
    for _ in range(200):
        try:
            return int(Path(pid_file).read_text())
        except (OSError, ValueError):
            time.sleep(0.05)
    raise RuntimeError("target never reported its pid")


def first_readable(pid: int) -> int:
    for line in open(f"/proc/{pid}/maps"):
        parts = line.split()
        if parts[1].startswith("r") and not parts[1].startswith("rw"):
            return int(parts[0].split("-")[0], 16)
    raise RuntimeError("no mapping")


def probe(pid: int) -> tuple[bool, str]:
    try:
        C.read_mem(pid, first_readable(pid), 8)
        read = "OK"
    except OSError as exc:
        read = f"{exc.strerror} (errno {exc.errno})"
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    ok = libc.ptrace(16, pid, None, None) == 0  # PTRACE_ATTACH
    if ok:
        libc.ptrace(17, pid, None, None)        # PTRACE_DETACH
    return read == "OK" and ok, f"readv={read} attach={'OK' if ok else 'EPERM'}"


def ppid_of(pid: int) -> str:
    return open(f"/proc/{pid}/status").read().split("PPid:")[1].split()[0]


def run_case(label: str, mode: str, shim: bool, tmp: str) -> tuple[bool, str]:
    pid_file = Path(tmp) / f"{label.replace(' ', '_')}.pid"
    env = {"LD_PRELOAD": str(SHIM), "CT_TRACER_PID": str(os.getpid())} if shim else {}
    pid = spawn_detached(mode, str(pid_file), env)
    works, detail = probe(pid)
    line = f"{label:28} pid={pid} ppid={ppid_of(pid)}  {detail}"
    os.kill(pid, 9)
    return works, line


def main() -> int:
    if not SHIM.exists():
        print(f"missing {SHIM} — build: gcc -shared -fPIC -O2 -o {SHIM} {SHIM.with_suffix('.c')} -lpthread")
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        a_works, a_line = run_case("A  no shim, exec", "exec", False, tmp)
        b_works, b_line = run_case("B  shim, exec", "exec", True, tmp)
        c_works, c_line = run_case("C  shim, fork only", "fork", True, tmp)

    for line in (a_line, b_line, c_line):
        print(line)
    ok = (not a_works) and b_works and c_works
    print("\nPASS" if ok else "\nFAIL",
          "- control denied; both shimmed targets traced"
          if ok else "- expected A denied, B and C allowed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
