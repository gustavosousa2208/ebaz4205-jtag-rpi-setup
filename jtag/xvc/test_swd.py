#!/usr/bin/env python3
"""Protocol test for the SWD mode of ebaz-xvc-server (--swd SWCLK,SWDIO with OpenOCD's remote_bitbang
driver).

Unlike test_remote_bitbang.py, which talks to a server someone else started, this test starts its own
servers in fake mode: the mode is selected by server arguments, and every argument combination below
needs its own process.

  python3 test_swd.py [path-to-ebaz-xvc-server] [first-port]

Checks, all against the fake model:
  * released SWDIO reads as the target's pull-up, a driven value reads back as driven
  * SWD commands are accepted in SWD mode and refused, fail-closed, in JTAG mode
  * TDO reads are refused on an SWD port, TRST is refused on every port
  * srst is refused until --nrst names a line, and accepted once it does
  * bad arguments (--swd with XVC, out-of-range --nrst, malformed --pins) exit 2
"""
import os
import socket
import subprocess
import sys
import time

BIN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "ebaz-xvc-server")
FIRST_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4450
fails = 0
checks = 0


def check(cond, what):
    global fails, checks
    checks += 1
    if not cond:
        fails += 1
        print("FAIL:", what)


def connect(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    s.settimeout(3)
    return s


def read_response(s, what):
    try:
        data = s.recv(1)
    except socket.timeout:
        data = b""
    if data == b"":
        check(False, "expected one response byte for " + what)
    return data


def expect_eof(s, what):
    """The server must close the connection (fail closed) and send no response byte."""
    s.settimeout(3)
    try:
        data = s.recv(1)
    except socket.timeout:
        data = None
    check(data == b"", what + " (got %r instead of EOF)" % data)


def start_server(extra_args, port):
    argv = [BIN, "--protocol", "remote-bitbang", "--fake", "--port", str(port)] + extra_args
    # stdin=/dev/null, not inherited: a server that sees a pipe stdin stops when that pipe closes (which is
    # what keeps a service started over ssh from outliving its session), and a test harness must not look
    # like one.
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return proc
        except OSError:
            if proc.poll() is not None:
                proc.wait()
                raise SystemExit("server exited with %d before listening: %s"
                                 % (proc.returncode, proc.stderr.read()))
            time.sleep(0.05)
    proc.kill()
    raise SystemExit("server never listened on port %d" % port)


def stop_server(proc):
    proc.kill()
    proc.wait()


def run_to_exit(args):
    proc = subprocess.run([BIN] + args, capture_output=True, text=True, timeout=10)
    return proc.returncode, proc.stderr


port = FIRST_PORT

# --- SWD port: released and driven SWDIO, benign commands, and the refusals that must stay refusals
proc = start_server(["--swd", "8,9"], port)
banner = proc.stdout.readline()
check("SWCLK=8 SWDIO=9" in banner, "banner reports the SWD lines (got %r)" % banner)
s = connect(port)

s.sendall(b"o")
s.sendall(b"c")
check(read_response(s, "released SWDIO") == b"1", "released SWDIO reads as the pull-up")
s.sendall(b"O")
s.sendall(b"d")                     # d = clk low, SWDIO low
s.sendall(b"c")
check(read_response(s, "driven low SWDIO") == b"0", "a driven low level reads back as 0")
s.sendall(b"d" + bytes([ord("d") + 1]))   # d + 1 = clk low, SWDIO high
s.sendall(b"c")
check(read_response(s, "driven high SWDIO") == b"1", "a driven high level reads back as 1")
s.sendall(b"o")
s.sendall(b"c")
check(read_response(s, "released SWDIO again") == b"1", "releasing the line returns it to the pull-up")

s.sendall(b"BbzZ")                  # no LED, no-op sleeps
s.sendall(b"r")                     # deasserting a reset line that is not wired is a no-op
s.sendall(b"01234567")              # pin writes are accepted in SWD mode too
s.sendall(b"c")
check(read_response(s, "connection survives SWD pin writes") in (b"0", b"1"), "still serving after writes")

# A pin write means the client is driving SWDIO, so the line must read back as driven without an explicit
# 'O': OpenOCD's line-reset phase goes out as plain pin writes, and a server that waits for 'O' clocks
# that reset onto a floating line (which is what "cannot read IDR" looks like from the host).
s.sendall(b"0")                     # tck=0 tms=0 tdi=0: SWDIO driven low
s.sendall(b"c")
check(read_response(s, "SWDIO after a low pin write") == b"0",
      "a pin write drives SWDIO, so it reads back as the written level")
s.sendall(b"7")                     # tck=1 tms=1 tdi=1: SWDIO driven high
s.sendall(b"c")
check(read_response(s, "SWDIO after a high pin write") == b"1", "a high pin write reads back high")
s.sendall(b"o")                     # an explicit release still wins
s.sendall(b"c")
check(read_response(s, "SWDIO after explicit release") == b"1", "releasing still returns the pull-up")
s.close()

for name, cmd in (("TDO read", b"R"), ("trst", b"t"), ("both resets", b"u")):
    h = connect(port)
    h.sendall(cmd)
    expect_eof(h, "%s on an SWD port is refused" % name)
    h.close()

h = connect(port)
h.sendall(b"s")                     # no --nrst: there is no reset wire to assert
expect_eof(h, "srst without --nrst is refused")
h.close()

h = connect(port)
h.sendall(b"X")
expect_eof(h, "unknown command closes the connection")
h.close()

h = connect(port)
h.sendall(b"Q")
expect_eof(h, "Q closes the connection cleanly")
h.close()
stop_server(proc)

# --- same port with a reset line wired: srst becomes available, TRST and TDO do not
port += 1
proc = start_server(["--swd", "8,9", "--nrst", "6"], port)
banner = proc.stdout.readline()
check("NRST line 6" in banner, "banner reports the NRST line (got %r)" % banner)
h = connect(port)
h.sendall(b"s")
h.sendall(b"r")
h.sendall(b"c")
check(read_response(h, "still serving after srst assert/deassert") in (b"0", b"1"),
      "srst assert and deassert are accepted once --nrst is given")
h.close()

h = connect(port)
h.sendall(b"t")
expect_eof(h, "trst is refused even with an srst line")
h.close()
stop_server(proc)

# --- JTAG port: the SWD commands must still be refused, exactly as before this feature
port += 1
proc = start_server([], port)
h = connect(port)
for name, cmd in (("swdio_drive", b"O"), ("swdio_drive_low", b"o"), ("swdio_read", b"c"), ("sw_write", b"d")):
    h.close()
    h = connect(port)
    h.sendall(cmd)
    expect_eof(h, "%s on a JTAG port is refused" % name)
h.close()
h = connect(port)
h.sendall(b"R")
check(read_response(h, "TDO read on a JTAG port") in (b"0", b"1"), "TDO reads still work on a JTAG port")
h.close()
stop_server(proc)

# --- argument handling: nonsense combinations must be rejected, not half-applied
code, err = run_to_exit(["--protocol", "xvc", "--swd", "8,9", "--fake"])
check(code == 2, "--swd with XVC exits 2 (got %d)" % code)
check("remote-bitbang" in err, "--swd with XVC explains the transport (got %r)" % err)
code, _ = run_to_exit(["--protocol", "remote-bitbang", "--swd", "8", "--fake"])
check(code == 2, "a one-line --swd exits 2 (got %d)" % code)
code, _ = run_to_exit(["--protocol", "remote-bitbang", "--swd", "8,999", "--fake"])
check(code == 2, "a --swd line outside gpiochip0 exits 2 (got %d)" % code)
code, _ = run_to_exit(["--protocol", "remote-bitbang", "--nrst", "999", "--fake"])
check(code == 2, "a --nrst line outside gpiochip0 exits 2 (got %d)" % code)
code, _ = run_to_exit(["--protocol", "remote-bitbang", "--pins", "64,65,66", "--fake"])
check(code == 2, "a short --pins list exits 2 (got %d)" % code)

print("checks=%d failures=%d" % (checks, fails))
print("ALL SWD TESTS PASSED" if fails == 0 else "SWD TESTS FAILED")
sys.exit(0 if fails == 0 else 1)
