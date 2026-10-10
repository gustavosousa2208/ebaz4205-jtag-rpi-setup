#!/usr/bin/env python3
"""Lifecycle tests for ebaz-xvc-server: the stdin rule, the pin lock and --stop-holder.

All three exist so a root-owned server is never left holding the pins. A service started over a channel
must die when that channel closes; a second server must refuse rather than fight for the pins; and if one
does survive anyway, --stop-holder reaps it without a second password prompt.

  python3 test_server_lifecycle.py [path-to-ebaz-xvc-server] [first-port]
"""
import os
import socket
import subprocess
import sys
import time

BIN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "ebaz-xvc-server")
FIRST_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4470
fails = 0
checks = 0


def check(cond, what):
    global fails, checks
    checks += 1
    if not cond:
        fails += 1
        print("FAIL:", what)


def spawn(port, stdin):
    return subprocess.Popen([BIN, "--fake", "--port", str(port)], stdin=stdin,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_listening(port, proc, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            if proc.poll() is not None:
                return False
            time.sleep(0.05)
    return False


def wait_exit(proc, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


check("xvc-server" in os.path.basename(BIN), f"--stop-holder looks for a server binary name (got {BIN})")

# --- a piped stdin is a supervisor: closing it stops the server
port = FIRST_PORT
proc = spawn(port, subprocess.PIPE)
check(wait_listening(port, proc), "server with a piped stdin listens")
proc.stdin.close()
check(wait_exit(proc), "closing the piped stdin stops the server")
out = proc.stdout.read() if proc.stdout else ""
check("stdin closed" in out, f"the server says why it stopped (got {out[-80:]!r})")
check(proc.returncode == 0, f"the deliberate stop exits 0 (got {proc.returncode})")

# --- a terminal or /dev/null is not a supervisor: the server keeps serving
port += 1
proc = spawn(port, subprocess.DEVNULL)
check(wait_listening(port, proc), "server with /dev/null stdin listens")
time.sleep(1.0)
check(proc.poll() is None, "/dev/null stdin does not stop the server")

# --- a second server refuses instead of driving the same pins
port += 1
second = spawn(port, subprocess.DEVNULL)
check(wait_exit(second), "a second server exits instead of sharing the pins")
second_out = second.stdout.read() if second.stdout else ""
check("already in use" in second_out, f"the refusal names the reason (got {second_out[-120:]!r})")
check(second.returncode == 3, f"the refusal exits 3, not 2 (got {second.returncode})")

# --- --stop-holder reaps the server that still holds the lock
reap = subprocess.run([BIN, "--stop-holder"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
check(reap.returncode == 0, f"--stop-holder exits 0 (got {reap.returncode}: {reap.stderr.strip()!r})")
check("stopped pid" in reap.stdout, f"--stop-holder reports what it stopped (got {reap.stdout.strip()!r})")
check(wait_exit(proc), "the holder is gone after --stop-holder")
holder_out = proc.stdout.read() if proc.stdout else ""
check("stopped, pins restored" in holder_out, f"the reaped server shut down cleanly (got {holder_out[-80:]!r})")

# --- with the pins free again, a fresh server starts and --stop-holder reports nothing to do
time.sleep(0.3)
reap = subprocess.run([BIN, "--stop-holder"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
check(reap.returncode == 0, f"--stop-holder on a free lock exits 0 (got {reap.returncode})")
port += 1
proc = spawn(port, subprocess.DEVNULL)
check(wait_listening(port, proc), "a fresh server starts once the lock is free")
stop(proc)

print("checks=%d failures=%d" % (checks, fails))
print("ALL LIFECYCLE TESTS PASSED" if fails == 0 else "LIFECYCLE TESTS FAILED")
sys.exit(0 if fails == 0 else 1)
