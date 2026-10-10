"""Run host OpenOCD against a PiProbe node over an authenticated SSH forward.

The probe keeps its JTAG service on loopback and this module owns the transport: it resolves the node,
runs the probe-side service for the duration of the session, forwards the traffic over one
authenticated SSH channel, runs OpenOCD, and tears everything down. Nothing is ever retried
automatically, and a second session is refused before it can touch the pins.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from piprobe import discover

DEFAULT_SERVER_PATH = "/usr/local/libexec/ebaz-xvc-server"
DEFAULT_REMOTE_PORT = 4444            # OpenOCD's remote_bitbang default
SERVICE_READY_TIMEOUT = 20.0
TUNNEL_READY_TIMEOUT = 15.0
STOP_TIMEOUT = 10.0

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TRANSPORT = 3
EXIT_VERIFY = 4

TAP_FOUND_RE = re.compile(r"JTAG tap: (\S+) tap/device found: 0x([0-9a-fA-F]{8})")


class UsageError(Exception):
    """The request cannot be carried out as given (bad arguments, missing tool, no node)."""


class TransportError(Exception):
    """The probe side did not come up, or the session failed before OpenOCD could run."""


@dataclass(frozen=True)
class ScanTap:
    """One TAP of the Zynq-7000 chain on the EBAZ4205 (XC7Z010)."""

    chain: str
    tap: str
    irlen: int
    expected: int
    mask_version: bool
    extra: str = ""

    @property
    def name(self) -> str:
        return f"{self.chain}.{self.tap}"


# The PL boundary-scan TAP and the ARM DAP TAP. Vivado reports the PL IDCODE as 0x13722093 while
# OpenOCD's own zynq_7000.cfg expects 0x03722093: the difference is only the IDCODE version nibble,
# so that check is version-insensitive and reports what the chain actually answered.
SCAN_TAPS = (
    ScanTap("zynq_pl", "bs", 6, 0x03722093, True, "-ignore-version -ircapture 0x1 -irmask 0x03"),
    ScanTap("zynq", "cpu", 4, 0x4BA00477, False, "-ircapture 0x1 -irmask 0xf"),
)


@dataclass
class Node:
    name: str
    host: str
    port: int
    source: str
    node_id: str = ""

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}" if self.port and self.port != 22 else self.host


@dataclass
class Result:
    node: Node | None = None
    transport: str = ""
    idcodes: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    openocd_exit: int | None = None
    cleanup: str = "not checked"
    error: str | None = None
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and not self.problems and self.openocd_exit == 0

    def to_json(self) -> str:
        return json.dumps({
            "node": {"name": self.node.name, "host": self.node.host, "port": self.node.port,
                     "source": self.node.source, "node_id": self.node.node_id} if self.node else None,
            "transport": self.transport,
            "idcodes": {name: f"0x{value:08x}" for name, value in sorted(self.idcodes.items())},
            "expected": {tap.name: (f"0x{tap.expected:08x}" + (" (version-insensitive)" if tap.mask_version else ""))
                         for tap in SCAN_TAPS},
            "problems": self.problems,
            "openocd_exit": self.openocd_exit,
            "cleanup": self.cleanup,
            "error": self.error,
            "ok": self.ok,
        }, indent=2)


class SessionLock:
    """Exclusive host-side lock: one session per machine, and it is held across the whole run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = None
        try:
            handle = open(self.path, "a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")             # byte 0 is what gets locked, so it must exist
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if handle is not None:
                with contextlib.suppress(OSError):   # a held range can refuse even closing
                    handle.close()
            return False
        # The diagnostics live after the locked byte: a Windows byte-range lock blocks reads of the
        # locked range from other handles, and a refused session must still be able to name the holder.
        handle.seek(1)
        handle.truncate(1)
        handle.write(f"pid {os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%S')}\n".encode())
        handle.flush()
        self._handle = handle
        return True

    def holder(self) -> str:
        """Describe the session holding the lock, if it can be read."""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(1)
                return handle.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None


def ssh_base(ssh_port: int | None) -> list[str]:
    """SSH options for unattended use: no prompts, and the saved host key must match.

    BatchMode turns an unknown host key into a hard failure, which is what fail-closed means here.
    """
    args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    if ssh_port and ssh_port != 22:
        args += ["-p", str(ssh_port)]
    return args


def resolve_node(requested: str | None, ssh_user: str | None, timeout: float,
                 peers_path: Path | None = None, aliases_path: Path | None = None) -> Node:
    candidates: list[dict[str, object]] = []
    mdns_error: str | None = None
    try:
        candidates += discover.discover_mdns(timeout)
    except RuntimeError as exc:
        mdns_error = str(exc)
    candidates += discover.load_peer_candidates(peers_path or discover.default_peers_path())
    try:
        aliases = discover.load_aliases(aliases_path or discover.default_aliases_path())
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    candidates = discover.apply_aliases(discover.merge_candidates(candidates), aliases)

    def identifiers(row: dict[str, object]) -> set[str]:
        """Every name a record can be asked for, so one node answers to all of its paths."""
        names = {str(row.get("name", "")).casefold(), str(row.get("node_id", "")).casefold(),
                 str(row.get("alias", "")).casefold()}
        names |= {str(known).casefold() for known in (row.get("known_names") or [])}
        names |= discover.host_names(row)
        names |= discover.short_names(row)
        names |= {str(address).casefold() for address in (row.get("addresses") or [])}
        return {name for name in names if name}

    def usable(row: dict[str, object]) -> bool:
        caps = row.get("capabilities") or []
        return "jtag" in caps or row.get("source") == "peer-config" or "peer-config" in (row.get("sources") or [])

    def build(row: dict[str, object]) -> Node:
        addresses = [str(a) for a in (row.get("addresses") or [])]
        host = addresses[0] if addresses else str(row.get("host") or "").rstrip(".")
        port = int(row.get("port") or 22)
        if not host:
            raise UsageError(f"candidate {row.get('name')!r} advertises no usable address")
        paths = row.get("sources") or [row.get("source") or "unknown"]
        return Node(name=discover.display_name(row), host=host, port=port,
                    source=",".join(str(path) for path in paths), node_id=str(row.get("node_id") or ""))

    if requested:
        wanted = requested.casefold()
        matches = [row for row in candidates if wanted in identifiers(row)]
        if not matches:
            known = ", ".join(sorted(str(row.get("name")) for row in candidates)) or "none found"
            raise UsageError(f"no PiProbe node matches {requested!r} (known: {known})"
                             + (f"; mDNS: {mdns_error}" if mdns_error else ""))
        if len(matches) > 1:
            raise UsageError(f"{requested!r} matches {len(matches)} nodes; use a unique name")
        return build(matches[0])

    usable_rows = [row for row in candidates if usable(row)]
    if not usable_rows:
        raise UsageError("no PiProbe node found over mDNS or in the peer file"
                         + (f" (mDNS: {mdns_error})" if mdns_error else "")
                         + "; pass --node to name one explicitly")
    if len(usable_rows) > 1:
        names = ", ".join(sorted(str(row.get("name")) for row in usable_rows))
        raise UsageError(f"several PiProbe nodes are usable ({names}); pass --node to choose one")
    return build(usable_rows[0])


def service_argv(node: Node, user: str | None, server_path: str, port: int) -> list[str]:
    """SSH command that runs the probe-side service for one session.

    The remote shell kills the sudo child when its stdin closes, which happens when this side stops
    the ssh process: sudo relays the signal and the server prints 'stopped, pins restored'. The whole
    remote command travels as one quoted argument, because ssh joins trailing arguments with spaces
    without escaping them. Path and port go in as positional parameters so shell quoting cannot
    break them.
    """
    script = ('sudo -n "$1" --protocol remote-bitbang --port "$2" & child=$!; '
              "trap 'kill \"$child\" 2>/dev/null' EXIT HUP INT TERM; "
              "cat >/dev/null")
    remote = " ".join([shlex.quote("sh"), "-c", shlex.quote(script),
                       shlex.quote("piprobe"), shlex.quote(server_path), shlex.quote(str(port))])
    destination = f"{user}@{node.host}" if user else node.host
    return [*ssh_base(node.port), destination, remote]


def tunnel_argv(node: Node, user: str | None, local_port: int, remote_port: int) -> list[str]:
    destination = f"{user}@{node.host}" if user else node.host
    return [*ssh_base(node.port), "-o", "ExitOnForwardFailure=yes", "-N",
            "-L", f"{local_port}:127.0.0.1:{remote_port}", destination]


class RemoteService:
    """Runs the probe's loopback-only JTAG service for exactly one session.

    The remote shell kills the sudo child when its stdin closes, which happens when this side stops
    the ssh process: sudo relays the signal, and the server prints 'stopped, pins restored'.
    """

    def __init__(self, node: Node, user: str | None, server_path: str, port: int, verbose: bool) -> None:
        self.node, self.user, self.server_path, self.port, self.verbose = node, user, server_path, port, verbose
        self.lines: list[str] = []
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str] = queue.Queue()

    def start(self) -> None:
        assert self._proc is None, "service already started"
        argv = service_argv(self.node, self.user, self.server_path, self.port)
        if self.verbose:
            print("+ " + " ".join(shlex.quote(a) for a in argv), file=sys.stderr)
        self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()
        deadline = time.monotonic() + SERVICE_READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=0.2)
            except queue.Empty:
                if self._proc.poll() is not None:
                    raise TransportError("the probe-side service exited before it listened: "
                                         + self._tail())
                continue
            self.lines.append(line)
            if f"listening on 127.0.0.1:{self.port}" in line:
                return
        raise TransportError(f"the probe-side service did not listen within {SERVICE_READY_TIMEOUT:.0f}s: "
                             + self._tail())

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line.rstrip("\n"))
        self._lines.put("")

    def _tail(self, limit: int = 6) -> str:
        recent = [line for line in self.lines if line][-limit:]
        return " | ".join(recent) if recent else "no output"

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.stdin:
            self._proc.stdin.close()          # EOF on the remote stdin kills the sudo child
        try:
            self._proc.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._proc = None


class Tunnel:
    """Authenticated local forward to the probe's loopback listener."""

    def __init__(self, node: Node, user: str | None, local_port: int, remote_port: int, verbose: bool) -> None:
        self.node, self.user, self.local_port, self.remote_port, self.verbose = node, user, local_port, remote_port, verbose
        self._proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        argv = tunnel_argv(self.node, self.user, self.local_port, self.remote_port)
        if self.verbose:
            print("+ " + " ".join(shlex.quote(a) for a in argv), file=sys.stderr)
        self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + TUNNEL_READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise TransportError("the SSH forward exited: " + self._errors())
            with socket.socket() as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect(("127.0.0.1", self.local_port))
                    return
                except OSError:
                    time.sleep(0.2)
        raise TransportError(f"the SSH forward did not accept on 127.0.0.1:{self.local_port}: " + self._errors())

    def finished_early(self) -> bool:
        """True when the forward died while the session was still running.

        A dead forward means the transport was interrupted: whatever OpenOCD reported afterwards
        cannot be trusted, and the session must not be mistaken for a clean one just because OpenOCD
        exited 0 after logging socket errors.
        """
        return self._proc is not None and self._proc.poll() is not None

    def _errors(self) -> str:
        if self._proc is None or self._proc.stderr is None or self._proc.poll() is None:
            return "no error reported"
        return (self._proc.stderr.read() or "").strip() or "no error reported"

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None


def transport_commands(local_port: int) -> list[str]:
    """Point OpenOCD at the forwarded remote_bitbang endpoint.

    No 'adapter speed' here: OpenOCD's remote_bitbang driver reports that it does not support a
    configurable speed, so the effective rate is whatever the link allows. The probe-side server
    applies each pin state immediately, which keeps a read-only scan well inside the target's timing.
    """
    return ["adapter driver remote_bitbang",
            "remote_bitbang host 127.0.0.1",
            f"remote_bitbang port {local_port}"]


def scan_commands() -> list[str]:
    commands = []
    for tap in SCAN_TAPS:
        line = f"jtag newtap {tap.chain} {tap.tap} -irlen {tap.irlen} -expected-id 0x{tap.expected:08x}"
        if tap.extra:
            line += " " + tap.extra
        commands.append(line)
    return [*commands, "init", "scan_chain", "shutdown"]


def find_openocd(explicit: str | None) -> str:
    if explicit:
        resolved = shutil.which(explicit) or (explicit if Path(explicit).exists() else None)
        if not resolved:
            raise UsageError(f"openocd not found at {explicit!r}")
        return resolved
    found = shutil.which("openocd")
    if found:
        return found
    raise UsageError("openocd is not on PATH; pass --opencd, or install a build that has the "
                     "remote_bitbang driver (for example "
                     "https://github.com/xpack-dev-tools/openocd-xpack/releases)")


def run_openocd(binary: str, commands: list[str], scripts: list[str], verbose: bool) -> tuple[int, str]:
    argv = [binary]
    for command in commands:
        argv += ["-c", command]
    for script in scripts:
        argv += ["-f", script]
    if verbose:
        print("+ " + " ".join(shlex.quote(a) for a in argv), file=sys.stderr)
    completed = subprocess.run(argv, capture_output=True, text=True)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def verify_idcodes(output: str) -> tuple[dict[str, int], list[str]]:
    found = {name: int(value, 16) for name, value in TAP_FOUND_RE.findall(output)}
    problems: list[str] = []
    for tap in SCAN_TAPS:
        value = found.get(tap.name)
        if value is None:
            problems.append(f"{tap.name}: no IDCODE reported")
        elif tap.mask_version:
            if (value & 0x0FFFFFFF) != (tap.expected & 0x0FFFFFFF):
                problems.append(f"{tap.name}: IDCODE 0x{value:08x}, expected 0x{tap.expected:08x} "
                                "(ignoring the version nibble)")
        elif value != tap.expected:
            problems.append(f"{tap.name}: IDCODE 0x{value:08x}, expected 0x{tap.expected:08x}")
    return found, problems


def check_cleanup(node: Node, user: str | None, port: int, verbose: bool) -> str:
    """Confirm the probe released the listener, so a session cannot leave pins owned or a port open."""
    destination = f"{user}@{node.host}" if user else node.host
    argv = [*ssh_base(node.port), destination, f"ss -ltn 2>/dev/null | grep -q ':{port} ' && echo BUSY || echo FREE"]
    if verbose:
        print("+ " + " ".join(shlex.quote(a) for a in argv), file=sys.stderr)
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=STOP_TIMEOUT + 10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"not checked ({exc})"
    answer = (completed.stdout or "").strip()
    if answer.endswith("BUSY"):
        return f"WARNING: the probe still listens on 127.0.0.1:{port}"
    if answer.endswith("FREE"):
        return f"probe released 127.0.0.1:{port}"
    return "not checked (probe did not answer)"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="piprobe openocd",
        description="Run host OpenOCD against a PiProbe node through an authenticated SSH forward. "
                    "Without -f it performs the read-only Zynq-7000 TAP scan of the EBAZ4205 and verifies "
                    "both IDCODEs; with -f it runs the given host OpenOCD scripts after the transport "
                    "configuration, so target scripts stay on the host.")
    parser.add_argument("--node", help="node to use: its label (banana-pi), advertised name (PiProbe-banana-pi), "
                                       "node id, host, or address (default: the only usable node)")
    parser.add_argument("--user", help="SSH user on the probe (default: the local user)")
    parser.add_argument("--server-path", default=DEFAULT_SERVER_PATH,
                        help=f"probe-side server binary (default: {DEFAULT_SERVER_PATH})")
    parser.add_argument("--rbb-port", type=int, default=DEFAULT_REMOTE_PORT,
                        help=f"remote_bitbang port on the probe (default: {DEFAULT_REMOTE_PORT})")
    parser.add_argument("--local-port", type=int, default=None,
                        help="local forwarded port (default: the same number as --rbb-port)")
    parser.add_argument("-f", "--file", action="append", default=[], dest="scripts",
                        help="host OpenOCD script to run after the transport configuration (repeatable); "
                             "with -f the built-in TAP scan is not used")
    parser.add_argument("--peers-file", type=Path, default=discover.default_peers_path(),
                        help="pinned-peer file to read (default: the native PiProbe config path)")
    parser.add_argument("--aliases-file", type=Path, default=discover.default_aliases_path(),
                        help="local aliases file to read (default: the native PiProbe config path)")
    parser.add_argument("--opencd", help="openocd binary to use (default: the one on PATH)")
    parser.add_argument("--discovery-timeout", type=float, default=None, help="mDNS browse deadline in seconds")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit without touching the probe")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every command and the full OpenOCD log")
    return parser.parse_args(argv)


def report(result: Result, verbose: bool) -> None:
    if result.node is not None:
        print(f"node: {result.node.name} ({result.node.endpoint}, from {result.node.source})")
    if result.transport:
        print(f"transport: {result.transport}")
    for tap in SCAN_TAPS:
        value = result.idcodes.get(tap.name)
        shown = f"0x{value:08x}" if value is not None else "not reported"
        print(f"  {tap.name:<12} {shown}  expected 0x{tap.expected:08x}"
              + ("  (version-insensitive)" if tap.mask_version else ""))
    for problem in result.problems:
        print(f"  problem: {problem}")
    if result.cleanup:
        print(f"cleanup: {result.cleanup}")
    if result.error:
        print(f"error: {result.error}")
    if verbose and result.output:
        print(result.output)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = Result()
    lock_path = discover.default_peers_path().parent / "openocd.lock"

    try:
        if not 1 <= args.rbb_port <= 65535:
            raise UsageError("--rbb-port must be 1..65535")
        local_port = args.local_port or args.rbb_port
        if not 1 <= local_port <= 65535:
            raise UsageError("--local-port must be 1..65535")
        openocd = find_openocd(args.opencd)
        result.node = resolve_node(args.node, args.user, args.discovery_timeout or 2.5,
                                   peers_path=args.peers_file.expanduser(),
                                   aliases_path=args.aliases_file.expanduser())
        commands = transport_commands(local_port) + ([] if args.scripts else scan_commands())
        result.transport = (f"remote_bitbang over SSH (127.0.0.1:{local_port} -> "
                            f"probe 127.0.0.1:{args.rbb_port}), openocd {openocd}")
        if args.dry_run:
            print(f"node: {result.node.name} ({result.node.endpoint}, from {result.node.source})")
            print(f"plan: {result.transport}")
            print(f"lock: {lock_path}")
            for command in commands:
                print(f"  -c {command}")
            for script in args.scripts:
                print(f"  -f {script}")
            return EXIT_OK
    except UsageError as exc:
        print(f"piprobe openocd: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"piprobe openocd: {exc}", file=sys.stderr)
        return EXIT_USAGE

    lock = SessionLock(lock_path)
    if not lock.acquire():
        print(f"piprobe openocd: another session holds {lock_path} ({lock.holder() or 'holder unknown'}); "
              "refusing to drive the probe", file=sys.stderr)
        return EXIT_TRANSPORT

    service = RemoteService(result.node, args.user, args.server_path, args.rbb_port, args.verbose)
    tunnel = Tunnel(result.node, args.user, local_port, args.rbb_port, args.verbose)
    try:
        service.start()
        tunnel.start()
        exit_code, output = run_openocd(openocd, commands, list(args.scripts), args.verbose)
        result.openocd_exit = exit_code
        result.output = output
        if tunnel.finished_early():
            result.problems.append("the SSH forward died during the session, so the transport was "
                                   "interrupted and this result is not trustworthy")
        if args.scripts:
            if exit_code != 0:
                result.problems.append(f"openocd exited with {exit_code}")
        else:
            result.idcodes, result.problems = verify_idcodes(output)
    except TransportError as exc:
        result.error = str(exc)
    finally:
        tunnel.stop()
        service.stop()
        lock.release()
        result.cleanup = check_cleanup(result.node, args.user, args.rbb_port, args.verbose)

    if args.json:
        print(result.to_json())
    else:
        report(result, args.verbose)
    if result.ok:
        return EXIT_OK
    return EXIT_TRANSPORT if result.error else EXIT_VERIFY


if __name__ == "__main__":
    raise SystemExit(main())
