#!/usr/bin/env python3
"""Test target for ctscan: holds a known int32 in a stable address.

Calls prctl(PR_SET_PTRACER, ANY) so scanmem can ptrace it even though
Yama ptrace_scope=1 would otherwise block a sibling process.
"""
import ctypes
import os
import time

libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
libc.prctl(0x59616D61, ctypes.c_ulong(-1), 0, 0, 0)  # PR_SET_PTRACER, PR_SET_PTRACER_ANY

val = ctypes.c_int(1234567)
print(f"TARGET_PID={os.getpid()}", flush=True)
print(f"VAL_ADDR={ctypes.addressof(val)}", flush=True)
while True:
    if val.value != 1234567:
        print(f"VALUE_CHANGED={val.value}", flush=True)
    time.sleep(0.3)
