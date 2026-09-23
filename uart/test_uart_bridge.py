#!/usr/bin/env python3
"""Linux integration tests for uart-bridge.py; uses PTYs and non-production ports."""
import os
import pty
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import importlib.util
import contextlib
import io
from pathlib import Path


BRIDGE = Path(__file__).with_name("uart-bridge.py")
SPEC = importlib.util.spec_from_file_location("uart_bridge", BRIDGE)
UART_BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UART_BRIDGE)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class UartBridgeTests(unittest.TestCase):
    def setUp(self):
        self.processes = []
        self.master_fds = []
        self.slave_fds = []
        self.tempdirs = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            if process.stdout:
                process.stdout.close()
        for fd in self.master_fds + self.slave_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        for directory in self.tempdirs:
            directory.cleanup()

    def pty_pair(self):
        master, slave = pty.openpty()
        self.master_fds.append(master)
        self.slave_fds.append(slave)
        return master, slave, os.ttyname(slave)

    def start_bridge(self, device, allow_write=False, port=None, live_port=None):
        port = port or free_port()
        live_port = live_port or free_port()
        args = [sys.executable, str(BRIDGE), "--device", str(device),
                "--port", str(port), "--live-port", str(live_port)]
        if allow_write:
            args.append("--allow-write")
        process = subprocess.Popen(args, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1,
                                   start_new_session=True)
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read()
                raise AssertionError("bridge exited during startup: " + output)
            ready, _, _ = select.select([process.stdout], [], [], 0.05)
            if ready:
                line = process.stdout.readline()
                if line.startswith("uart-bridge:"):
                    return process, port, live_port
            time.sleep(0.02)
        raise AssertionError("bridge did not report startup")

    @staticmethod
    def connect(port):
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.settimeout(2)
        return sock

    def test_missing_serial_device_exits_without_leaking_listener(self):
        port, live_port = free_port(), free_port()
        result = subprocess.run(
            [sys.executable, str(BRIDGE), "--device", "/dev/ebaz-test-missing",
             "--port", str(port), "--live-port", str(live_port)],
            capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ebaz-test-missing", result.stderr)
        for candidate in (port, live_port):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", candidate))

    def test_port_collision_exits_without_leaking_first_listener(self):
        first_port, collision_port = free_port(), free_port()
        blocker = socket.socket()
        blocker.bind(("0.0.0.0", collision_port))
        blocker.listen(1)
        try:
            result = subprocess.run(
                [sys.executable, str(BRIDGE), "--device", "/dev/ebaz-test-unused",
                 "--port", str(first_port), "--live-port", str(collision_port)],
                capture_output=True, text=True, timeout=3)
        finally:
            blocker.close()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Address already in use", result.stderr)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", first_port))

    def test_read_only_replay_live_and_client_disconnect(self):
        master, slave, device = self.pty_pair()
        process, replay_port, live_port = self.start_bridge(device)
        os.close(slave)
        self.slave_fds.remove(slave)

        live = self.connect(live_port)
        live.sendall(b"must-not-reach-uart")
        readable, _, _ = select.select([master], [], [], 0.2)
        self.assertFalse(readable, "default mode wrote client bytes to serial")
        os.write(master, b"first-")
        self.assertEqual(live.recv(6), b"first-")
        live.close()

        replay = self.connect(replay_port)
        self.assertEqual(replay.recv(6), b"first-")
        replay.close()
        again = self.connect(live_port)
        again.close()
        self.assertIsNone(process.poll(), "bridge exited after client disconnect")

    def test_allow_write_forwards_client_bytes_to_serial(self):
        master, slave, device = self.pty_pair()
        process, _, live_port = self.start_bridge(device, allow_write=True)
        os.close(slave)
        self.slave_fds.remove(slave)
        client = self.connect(live_port)
        client.sendall(b"authorized-write")
        readable, _, _ = select.select([master], [], [], 2)
        self.assertTrue(readable, "--allow-write did not forward serial bytes")
        self.assertEqual(os.read(master, 16), b"authorized-write")
        client.close()
        self.assertIsNone(process.poll())

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "TIOCEXCL duplicate-open behavior is Linux-specific")
    def test_second_bridge_cannot_steal_same_serial_device(self):
        _, slave, device = self.pty_pair()
        process, _, _ = self.start_bridge(device)
        os.close(slave)
        self.slave_fds.remove(slave)
        port, live_port = free_port(), free_port()
        result = subprocess.run(
            [sys.executable, str(BRIDGE), "--device", device,
             "--port", str(port), "--live-port", str(live_port)],
            capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Device or resource busy", result.stderr)
        self.assertIsNone(process.poll(), "first bridge exited after duplicate open")
        for candidate in (port, live_port):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", candidate))

    def test_serial_reopen_retries_until_device_returns(self):
        calls = []

        def flaky_open(device, baud):
            calls.append((device, baud))
            if len(calls) == 1:
                raise OSError(2, "device temporarily absent")
            return 42

        original = UART_BRIDGE.open_serial
        original_sleep = UART_BRIDGE.time.sleep
        try:
            UART_BRIDGE.open_serial = flaky_open
            UART_BRIDGE.time.sleep = lambda _delay: None
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(UART_BRIDGE.reopen_serial("/dev/test-uart", 115200), 42)
        finally:
            UART_BRIDGE.open_serial = original
            UART_BRIDGE.time.sleep = original_sleep
        self.assertEqual(calls, [("/dev/test-uart", 115200)] * 2)

    def test_supervisor_retries_after_missing_device(self):
        port, live_port = free_port(), free_port()
        env = dict(os.environ, UART_RESTART_DELAY="0.1")
        process = subprocess.Popen(
            ["/bin/sh", str(Path(__file__).with_name("run-bridge.sh")),
             "--device", "/dev/ebaz-test-missing", "--port", str(port),
             "--live-port", str(live_port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True, env=env)
        self.processes.append(process)
        time.sleep(1.5)
        self.assertIsNone(process.poll(), "supervisor exited after bridge startup failure")
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
        output = process.stdout.read()
        self.assertGreaterEqual(output.count("startup failed"), 2, output)
        self.assertIn("retrying in 0.1s", output)
        for candidate in (port, live_port):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", candidate))

    def test_supervisor_termination_stops_active_bridge_and_releases_ports(self):
        _, slave, device = self.pty_pair()
        port, live_port = free_port(), free_port()
        process = subprocess.Popen(
            ["/bin/sh", str(Path(__file__).with_name("run-bridge.sh")),
             "--device", device, "--listen", "127.0.0.1", "--port", str(port),
             "--live-port", str(live_port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        self.processes.append(process)
        deadline = time.monotonic() + 5
        started = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("supervisor exited before bridge startup")
            ready, _, _ = select.select([process.stdout], [], [], 0.05)
            if ready and process.stdout.readline().startswith("uart-bridge:"):
                started = True
                break
        self.assertTrue(started, "supervised bridge did not start")
        os.close(slave)
        self.slave_fds.remove(slave)

        process.send_signal(signal.SIGTERM)
        process.wait(timeout=3)
        self.assertEqual(process.returncode, 0)
        for candidate in (port, live_port):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", candidate))

    def test_cron_script_preserves_unrelated_entries_and_is_idempotent(self):
        tempdir = tempfile.TemporaryDirectory(prefix="uart-cron-test-")
        self.tempdirs.append(tempdir)
        root = Path(tempdir.name)
        bindir = root / "bin"
        bindir.mkdir()
        state = root / "crontab"
        unrelated = ["MAILTO=operator@example.invalid", "17 3 * * * /usr/local/bin/backup"]
        initial = unrelated + ["@reboot old-command # link-test:uart-bridge"]
        state.write_text("\n".join(initial) + "\n")
        stub = bindir / "crontab"
        stub.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  -l) cat \"$TEST_CRONTAB_FILE\" ;;\n"
            "  -) cat >\"$TEST_CRONTAB_FILE\" ;;\n"
            "  -r) python3 -c 'import os; os.unlink(os.environ[\"TEST_CRONTAB_FILE\"])' ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n"
        )
        stub.chmod(0o755)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                   TEST_CRONTAB_FILE=str(state))
        cron_script = Path(__file__).with_name("pi-cron.sh")

        subprocess.run([str(cron_script), "remove"], env=env, check=True,
                       capture_output=True, text=True)
        self.assertEqual(state.read_text().splitlines(), unrelated)
        subprocess.run([str(cron_script), "add"], env=env, check=True,
                       capture_output=True, text=True)
        first_add = state.read_text().splitlines()
        owned = [line for line in first_add if "ebaz4205-jtag:uart-bridge" in line]
        self.assertEqual(len(owned), 1)
        self.assertEqual([line for line in first_add if "ebaz4205-jtag:uart-bridge" not in line],
                         unrelated)
        subprocess.run([str(cron_script), "add"], env=env, check=True,
                       capture_output=True, text=True)
        self.assertEqual(sum("ebaz4205-jtag:uart-bridge" in line
                             for line in state.read_text().splitlines()), 1)
        subprocess.run([str(cron_script), "remove"], env=env, check=True,
                       capture_output=True, text=True)
        self.assertEqual(state.read_text().splitlines(), unrelated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
