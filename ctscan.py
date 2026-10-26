#!/usr/bin/env python3
"""
ctscan — scan-mode engine: drive the scanmem CLI to find/lock a value.

Python API:
    s = Scanmem(pid)
    n = s.search("100")     # bare value = search (int32 by default)
    s.filter("!=")          # changed since last scan
    s.filter("+")           # increased
    s.set("9999")           # write to all matches
    s.lock()                # freeze matches

Runs scanmem in a pty so the "> " prompt is emitted and each command's output
can be read deterministically.
"""
import os
import pty
import select
import subprocess
import time


class Scanmem:
    def __init__(self, pid: int | None = None, timeout: float = 8.0):
        self.timeout = timeout
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            ["scanmem"], stdin=slave, stdout=slave, stderr=slave, close_fds=True
        )
        os.close(slave)
        self._read_until("> ")  # consume banner + first prompt
        if pid is not None:
            self.attach(pid)

    def _read_until(self, marker: str) -> str:
        data = b""
        mb = marker.encode()
        deadline = time.time() + self.timeout
        while mb not in data:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            r, _, _ = select.select([self.master], [], [], remaining)
            if not r:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return data.decode(errors="replace")

    def cmd(self, c: str) -> str:
        os.write(self.master, (c + "\n").encode())
        return self._read_until("> ")

    # scanmem command wrappers
    def attach(self, pid: int) -> str:
        return self.cmd(f"pid {pid}")

    def search(self, value) -> str:
        return self.cmd(str(value))

    def filter(self, how: str, value=None) -> str:
        return self.cmd(f"{how} {value}".strip())

    def set(self, value) -> str:
        return self.cmd(f"set {value}")

    def lock(self) -> str:
        return self.cmd("lock")

    def unlock(self) -> str:
        return self.cmd("unlock")

    def list_matches(self) -> str:
        return self.cmd("list")

    def close(self) -> None:
        try:
            os.write(self.master, b"exit\n")
        except OSError:
            pass
        try:
            self.proc.terminate()
        except OSError:
            pass
