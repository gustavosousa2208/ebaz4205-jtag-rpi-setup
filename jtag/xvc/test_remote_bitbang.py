#!/usr/bin/env python3
"""Protocol test for the remote_bitbang mode of ebaz-xvc-server (OpenOCD's remote_bitbang driver).

Run the server in fake mode first (no hardware, no root):
    ./ebaz-xvc-server --protocol remote-bitbang --port 4444 --fake
    ./test_remote_bitbang.py [port]            default 4444

The fake mode models TDO as TDI delayed by one rising edge, so the shift checks below are exact.
Prints ALL REMOTE_BITBANG TESTS PASSED only if every check passes.
"""
import socket
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 4444
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


def expect_eof(s, what):
    """The server must close the connection (fail closed) and send no response byte."""
    try:
        data = s.recv(1)
    except socket.timeout:
        check(False, what + " (server kept the connection open)")
        return
    except OSError:
        check(True, what)
        return
    check(data == b"", what + " (got %r instead of EOF)" % data)


def read_response(s, what):
    try:
        return s.recv(1)
    except socket.timeout:
        check(False, what + " (no response)")
        return b""


# --- the server must not send anything on connect: any stray byte breaks OpenOCD's framing
s = connect()
s.settimeout(0.3)
try:
    stray = s.recv(1)
except socket.timeout:
    stray = b""
check(stray == b"", "no unsolicited bytes on connect (got %r)" % stray)
s.settimeout(3)

# --- pin states are applied without responses; TDO follows TDI delayed by one rising edge
s.sendall(b"15")                       # tck low with tdi=1, then rising edge with tdi=1
s.sendall(b"R")
check(read_response(s, "first sample of the delay line") == b"0", "TDO is 0 before any bit was sampled")
s.sendall(b"04")                       # tck low with tdi=0, then rising edge: samples 0, presents the earlier 1
s.sendall(b"R")
check(read_response(s, "second sample of the delay line") == b"1", "TDO presents the previous TDI bit")

# --- every 'R' produces exactly one byte, in order, even when commands are batched
s.sendall(b"R" * 7)
got = b""
deadline = time.time() + 3
while len(got) < 7 and time.time() < deadline:
    got += s.recv(7 - len(got))
check(len(got) == 7, "seven R commands produce seven bytes (got %d)" % len(got))
check(set(got) <= {ord("0"), ord("1")}, "responses are only '0'/'1' (got %r)" % got)
check(got == b"1111111", "the delay line keeps presenting the last sampled 1 (got %r)" % got)

# --- blink, sleeps and the deasserted-reset no-op must be accepted without a response or a close
s.sendall(b"BbZzr")
s.sendall(b"R")
check(read_response(s, "connection survives B/b/Z/z/r") in (b"0", b"1"), "still serving after benign commands")
s.close()

# --- fail closed: reset asserts are refused, because no reset line is wired to the target
for name, cmd in (("srst", b"s"), ("trst", b"t"), ("both resets", b"u")):
    h = connect()
    h.sendall(cmd)
    h.sendall(b"R")
    expect_eof(h, "reset assert (%s) is refused, not acknowledged" % name)
    h.close()

# --- fail closed: SWD-only commands are refused instead of pretending to work
for name, cmd in (("swdio_drive", b"O"), ("swdio_drive_low", b"o"), ("swdio_read", b"c"), ("swd_write", b"d")):
    h = connect()
    h.sendall(cmd)
    expect_eof(h, "SWD command (%s) is refused" % name)
    h.close()

# --- fail closed: an unknown command closes the connection
h = connect()
h.sendall(b"X")
expect_eof(h, "unknown command closes the connection")
h.close()

# --- 'Q' is a clean quit
h = connect()
h.sendall(b"Q")
expect_eof(h, "Q closes the connection cleanly")
h.close()

# --- one client at a time: while a session is open the next connection is not served
a = connect()
b = connect()
a.sendall(b"R")
check(read_response(a, "first client is served") in (b"0", b"1"), "first client gets its sample")
b.settimeout(0.5)
try:
    mid = b.recv(1)
except socket.timeout:
    mid = None
check(mid is None, "second client is not served while the first session is open (got %r)" % mid)
a.close()
b.settimeout(3)
b.sendall(b"R")
check(read_response(b, "second client is served after the first closed") in (b"0", b"1"),
      "the queued client is served once the first disconnects")
b.close()

# --- the server is still alive and correct after all of that
s2 = connect()
s2.sendall(b"1")
s2.sendall(b"R")
check(read_response(s2, "server alive after hostile clients") in (b"0", b"1"), "server survived every hostile client")
s2.close()

print("checks=%d failures=%d" % (checks, fails))
print("ALL REMOTE_BITBANG TESTS PASSED" if fails == 0 else "REMOTE_BITBANG TESTS FAILED")
sys.exit(0 if fails == 0 else 1)
