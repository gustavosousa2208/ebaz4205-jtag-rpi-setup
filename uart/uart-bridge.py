#!/usr/bin/env python3
"""Serial-to-TCP bridge for Board 1's UART on the Banana Pi (standard library only, no root).

Two listening ports on the same serial device:
  --port      (default 2217)  replays the last --history bytes on connect, then streams live
  --live-port (default 2218)  live data only (use this to capture one run without old output)

Read-only by default: bytes sent by clients are discarded unless --allow-write is given.
Only this process should open the serial device (do not run uart-capture.py at the same time).
"""
import argparse
import errno
import os
import select
import socket
import sys
import termios
import time

BAUDS = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
         57600: termios.B57600, 115200: termios.B115200, 230400: termios.B230400}


def open_serial(dev, baud):
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0                                                   # iflag: raw
    attrs[1] = 0                                                   # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL       # cflag: 8N1, no flow control
    attrs[3] = 0                                                   # lflag: no echo/canonical
    attrs[4] = attrs[5] = BAUDS[baud]
    attrs[6] = list(attrs[6])
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def listener(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(4)
    s.setblocking(False)
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--device', default='/dev/ttyS3')
    ap.add_argument('--baud', type=int, default=115200, choices=sorted(BAUDS))
    ap.add_argument('--listen', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=2217)
    ap.add_argument('--live-port', type=int, default=2218)
    ap.add_argument('--history', type=int, default=65536)
    ap.add_argument('--allow-write', action='store_true')
    args = ap.parse_args()

    replay_srv = listener(args.listen, args.port)
    live_srv = listener(args.listen, args.live_port)
    fd = open_serial(args.device, args.baud)
    history = bytearray()
    clients = []                                   # sockets
    print('uart-bridge: %s %d 8N1 -> tcp %s:%d (replay) and :%d (live), %s' % (
        args.device, args.baud, args.listen, args.port, args.live_port,
        'read-write' if args.allow_write else 'read-only'), flush=True)

    def drop(c):
        if c in clients:
            clients.remove(c)
        try:
            c.close()
        except OSError:
            pass

    while True:
        readable, _, _ = select.select([replay_srv, live_srv, fd] + clients, [], [], 1.0)
        for r in readable:
            if r is replay_srv or r is live_srv:
                conn, addr = r.accept()
                conn.setblocking(False)
                if r is replay_srv and history:
                    try:
                        conn.sendall(bytes(history))
                    except OSError:
                        conn.close()
                        continue
                clients.append(conn)
                print('%s client %s:%d on port %d' % (time.strftime('%H:%M:%S'), addr[0], addr[1],
                      r.getsockname()[1]), flush=True)
            elif r == fd:
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                except OSError as e:
                    if e.errno in (errno.EIO, errno.ENODEV):
                        print('serial error %s, reopening' % e, flush=True)
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        time.sleep(1)
                        fd = open_serial(args.device, args.baud)
                        continue
                    raise
                if data:
                    history += data
                    if len(history) > args.history:
                        del history[:len(history) - args.history]
                    for c in list(clients):
                        try:
                            c.sendall(data)
                        except (BlockingIOError, OSError):
                            drop(c)
            else:                                   # a client sent something, or closed
                try:
                    data = r.recv(4096)
                except BlockingIOError:
                    continue
                except OSError:
                    drop(r)
                    continue
                if data == b'':                     # client closed
                    drop(r)
                elif args.allow_write:
                    os.write(fd, data)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
