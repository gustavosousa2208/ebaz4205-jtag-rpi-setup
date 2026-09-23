#!/usr/bin/env python3
"""Capture a UART without reopening it after configuring termios."""

import os
import socket
import sys
import termios
import time
from urllib.parse import urlsplit


device = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 3600.0

reader = None
fd = None
try:
    if device.startswith("tcp://"):
        endpoint = urlsplit(device)
        if endpoint.hostname is None or endpoint.port is None:
            raise ValueError("TCP UART source must include a host and port")
        reader = socket.create_connection((endpoint.hostname, endpoint.port), timeout=5)
        reader.settimeout(0.2)
    else:
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = termios.B115200
        attrs[5] = termios.B115200
        attrs[6] = list(attrs[6])
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIFLUSH)
except (OSError, ValueError) as exc:
    print(f"uart-capture: cannot open {device}: {exc}", file=sys.stderr, flush=True)
    sys.exit(1)

deadline = time.time() + seconds
while time.time() < deadline:
    try:
        data = reader.recv(4096) if reader else os.read(fd, 4096)
    except (BlockingIOError, TimeoutError, socket.timeout):
        data = b""
    if reader and not data:
        break
    if data:
        os.write(sys.stdout.fileno(), data)
    else:
        time.sleep(0.01)

if reader:
    reader.close()
if fd is not None:
    os.close(fd)
