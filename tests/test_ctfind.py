#!/usr/bin/env python3
"""Value scan + hardware watchpoint, tested against a real writing process.

The fixture is C (compiled here) so the writer is a normal instruction, not
CPython's memmove: it holds 0x1337 in an anonymous mapping until it reads a line
from stdin, then decrements it in a loop.

Checks:
  * scan_value finds the address that holds the sentinel (and it is the right one)
  * refine keeps it after the value changes
  * watch_writes traps on the write and reports a RIP inside the executable
  * instruction_ending_at decodes the writing instruction

Run: python3 tests/test_ctfind.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctapply as C
import ctfind
import ctinject as INJ

SRC = r"""
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>
int main(void) {
    volatile int *v = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (v == MAP_FAILED) return 1;
    *v = 0x1337;
    printf("%p %d\n", (void *)v, *v);
    fflush(stdout);
    getchar();                      /* wait until the test starts changing it */
    for (;;) {
        usleep(50000);
        *v = *v - 1;
    }
    return 0;
}
"""


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "writer.c"
        exe = Path(tmp) / "writer"
        src.write_text(SRC)
        build = subprocess.run(["gcc", "-O0", "-g", "-no-pie", "-o", str(exe), str(src)],
                               capture_output=True, text=True)
        if build.returncode != 0:
            print("SKIP: fixture did not compile:", build.stderr[:200])
            return 0

        proc = subprocess.Popen([str(exe)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True)
        line = proc.stdout.readline().split()
        addr, value = int(line[0], 16), int(line[1])
        pid = proc.pid

        hits = ctfind.scan_value(pid, value, "int32")
        if addr not in hits:
            failures.append(f"scan did not find {addr:#x} (got {len(hits)} hits)")
        if len(hits) > 64:
            failures.append(f"scan too noisy: {len(hits)} hits for a sentinel")

        # start the child's write loop, then refine on the new value
        proc.stdin.write("\n")
        proc.stdin.flush()
        time.sleep(0.35)
        current = ctfind.unpack(C.read_mem(pid, addr, 4), "int32")
        refined = ctfind.refine(pid, hits, current, "int32")
        if addr not in refined:
            failures.append(f"refine dropped the real address (kept {len(refined)})")

        result = ctfind.watch_writes(pid, addr, 4, timeout=5.0, hits=1)
        if not result.get("ok"):
            failures.append(f"watchpoint did not fire: {result.get('error')}")
        else:
            rip = result["rip"]
            inside = any(r["perms"].startswith("r-x") and r["start"] <= rip < r["end"]
                         for r in C.parse_maps(pid))
            if not inside:
                failures.append(f"watchpoint RIP {rip:#x} is not in executable memory")
            if not result.get("instruction"):
                failures.append("could not decode the writing instruction")
            else:
                print("writer instruction:", result["instruction"]["text"],
                      "| suggestion:", ctfind.suggest_patch(result["instruction"]))

        proc.kill()

    for f in failures:
        print("FAIL:", f)
    print("PASS: value scan + watchpoint" if not failures else f"FAIL: {len(failures)} case(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
