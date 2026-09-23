#!/usr/bin/env python3
"""Reads Board 1's JTAG chain IDCODEs THROUGH the running XVC server (real GPIO, not --fake).
   usage: idcode_test.py [port] [scans]      expects PL 0x13722093 + ARM DAP 0x4ba00477 on every scan."""
import socket
import struct
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 2542
SCANS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
EXPECT = {0x13722093, 0x4BA00477}


def recv_n(s, n):
    data = b""
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            raise RuntimeError("server closed the connection")
        data += chunk
    return data


def shift(s, tms_bits, tdi_bits):
    n = len(tms_bits)
    nb = (n + 7) // 8
    tms = bytearray(nb)
    tdi = bytearray(nb)
    for i in range(n):
        tms[i >> 3] |= tms_bits[i] << (i & 7)
        tdi[i >> 3] |= tdi_bits[i] << (i & 7)
    s.sendall(b"shift:" + struct.pack("<I", n) + bytes(tms) + bytes(tdi))
    tdo = recv_n(s, nb)
    return [(tdo[i >> 3] >> (i & 7)) & 1 for i in range(n)]


s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
s.sendall(b"getinfo:")
print("server:", s.recv(64).decode().strip())
ok = 0
t0 = time.time()
for scan in range(SCANS):
    # 6x TMS=1 (Test-Logic-Reset), Idle, Select-DR, Capture-DR, Shift-DR, then 64 data clocks (last with TMS=1), Update-DR, Idle
    tms = [1] * 6 + [0, 1, 0, 0] + [0] * 63 + [1] + [1, 0]
    tdo = shift(s, tms, [0] * len(tms))
    chain = sum(tdo[10 + i] << i for i in range(64))
    first, second = chain & 0xFFFFFFFF, chain >> 32
    good = {first, second} == EXPECT
    ok += good
    print("scan %d: chain=0x%016x first=0x%08x second=0x%08x %s" % (scan + 1, chain, first, second, "pass" if good else "FAIL"))
dt = time.time() - t0
print("passed %d/%d in %.2f s" % (ok, SCANS, dt))
sys.exit(0 if ok == SCANS else 1)
