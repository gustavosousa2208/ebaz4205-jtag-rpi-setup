#!/usr/bin/env python3
"""Capture a UART without reopening it after configuring termios."""

import os
import sys
import termios
import time


device = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 3600.0

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

deadline = time.time() + seconds
while time.time() < deadline:
    try:
        data = os.read(fd, 4096)
    except BlockingIOError:
        data = b""
    if data:
        os.write(sys.stdout.fileno(), data)
    else:
        time.sleep(0.01)

os.close(fd)
