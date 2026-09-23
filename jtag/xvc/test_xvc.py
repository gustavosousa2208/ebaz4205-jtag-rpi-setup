#!/usr/bin/env python3
"""Protocol test for ebaz-xvc-server. Run the server with --fake first (no hardware, no root):
     ./ebaz-xvc-server --fake --port 2542 &      then      python3 test_xvc.py 2542
In --fake mode TDO is TDI delayed by exactly one bit, so every reply can be predicted bit for bit.
Prints ALL XVC TESTS PASSED only if every check passes."""
import random
import socket
import struct
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 2542
fails = 0
checks = 0


def check(cond, what):
    global fails, checks
    checks += 1
    if not cond:
        fails += 1
        print("FAIL:", what)


def connect():
    s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
    s.settimeout(3)
    return s


def recv_n(s, n):
    data = b""
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


def shift(s, bits, tms, tdi, chunk=None):
    nbytes = (bits + 7) // 8
    msg = b"shift:" + struct.pack("<I", bits) + tms + tdi
    if chunk:                                   # fragmented delivery
        for i in range(0, len(msg), chunk):
            s.sendall(msg[i:i + chunk])
            time.sleep(0.001)
    else:
        s.sendall(msg)
    return recv_n(s, nbytes)


def expected_tdo(bits, tdi):
    """fake mode: tdo[i] = tdi[i-1], tdo[0] = previous last tdi bit (0 after a fresh start)."""
    out = bytearray((bits + 7) // 8)
    prev = expected_tdo.prev
    for i in range(bits):
        out[i >> 3] |= prev << (i & 7)
        prev = (tdi[i >> 3] >> (i & 7)) & 1
    expected_tdo.prev = prev
    return bytes(out)


expected_tdo.prev = 0

# --- basic commands
s = connect()
s.sendall(b"getinfo:")
info = s.recv(64)
check(info == b"xvcServer_v1.0:32768\n", "getinfo reply %r" % info)
s.sendall(b"settck:" + struct.pack("<I", 1000))
check(recv_n(s, 4) == struct.pack("<I", 1000), "settck echoes the period")

# the fake delay line remembers the last TDI bit of the previous run: normalise it to 0 first
recv_dummy = shift(s, 1, b"\x00", b"\x00")
expected_tdo.prev = 0

# --- shifts of many lengths, random data, checked bit for bit (includes non-multiples of 8)
rnd = random.Random(1234)
for bits in (1, 2, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65, 100, 1000, 4097, 65536, 262144):
    nb = (bits + 7) // 8
    tms = bytes(rnd.getrandbits(8) for _ in range(nb))
    tdi = bytes(rnd.getrandbits(8) for _ in range(nb))
    got = shift(s, bits, tms, tdi)
    check(got == expected_tdo(bits, tdi), "shift %d bits" % bits)

# --- fragmented delivery must give the same answer
bits = 200
nb = (bits + 7) // 8
tms = bytes(rnd.getrandbits(8) for _ in range(nb))
tdi = bytes(rnd.getrandbits(8) for _ in range(nb))
check(shift(s, bits, tms, tdi, chunk=3) == expected_tdo(bits, tdi), "byte-fragmented shift")

# --- the maximum vector size works, one byte more closes the connection
maxb = 32768
tms = bytes(maxb)
tdi = bytes(rnd.getrandbits(8) for _ in range(maxb))
check(shift(s, maxb * 8, tms, tdi) == expected_tdo(maxb * 8, tdi), "maximum size shift")
s.close()

# --- hostile input: each must close only that connection, and the server must keep serving
for name, payload in (
    ("zero-length shift", b"shift:" + struct.pack("<I", 0)),
    ("oversized shift", b"shift:" + struct.pack("<I", (32768 + 1) * 8)),
    ("huge shift", b"shift:" + struct.pack("<I", 0xFFFFFFFF)),
    ("unknown command", b"hello, world"),
    ("garbage bytes", bytes(range(200))),
    ("truncated command", b"shif"),
):
    h = connect()
    h.sendall(payload)
    try:
        data = h.recv(64)
    except socket.timeout:
        data = b"TIMEOUT"
    except ConnectionResetError:        # server closed with our unread bytes pending: still a rejection
        data = b""
    if name == "truncated command":
        # the server cannot know it is truncated until we hang up, so it keeps waiting silently: that is correct
        check(data == b"TIMEOUT", "%s: server waits for the rest (got %r)" % (name, data))
    else:
        check(data == b"", "%s: connection is closed without a reply (got %r)" % (name, data))
    h.close()
    expected_tdo.prev = 0                          # fresh connection state is not guaranteed, resync below

# --- server still alive and correct after all that
s2 = connect()
s2.sendall(b"getinfo:")
check(s2.recv(64) == b"xvcServer_v1.0:32768\n", "server alive after hostile clients")
# a fresh single-bit shift of 1s then 0s: outputs are predictable relative to what we send in this connection
first = shift(s2, 8, b"\x00", b"\xff")
second = shift(s2, 8, b"\x00", b"\x00")
check(second[0] & 1 == 1, "delay line carries the last bit across shifts (got %r)" % second)
s2.close()

# --- client disconnecting mid-command must not kill the server
m = connect()
m.sendall(b"shift:" + struct.pack("<I", 800) + b"\x00" * 10)   # promises 200 bytes, sends 10
m.close()
time.sleep(0.2)
s3 = connect()
s3.sendall(b"getinfo:")
check(s3.recv(64) == b"xvcServer_v1.0:32768\n", "server alive after a mid-command disconnect")
s3.close()

print("checks=%d failures=%d" % (checks, fails))
print("ALL XVC TESTS PASSED" if fails == 0 else "XVC TESTS FAILED")
sys.exit(0 if fails == 0 else 1)
