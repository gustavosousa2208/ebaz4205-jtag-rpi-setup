"""Load a bitstream into a target through a PiProbe node.

A bit-level JTAG transport cannot carry a bitstream: every pin state is a command byte, so a 2 MB
bitstream becomes tens of megabytes of commands and the client's socket fills long before the payload
is through (measured: `remote_bitbang_putc`, WSAEWOULDBLOCK, after 16 s). The payload therefore
travels over the same authenticated SSH connection this tool already owns -- no manual scp, no shell
work by the operator -- and the probe programs the device locally at full speed with its qualified
runner, streaming the evidence back. The file is verified on arrival and removed on success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from piprobe import discover, openocd
from piprobe.openocd import EXIT_OK, EXIT_TRANSPORT, EXIT_USAGE, EXIT_VERIFY

DEFAULT_RUNNER_DIR = "~/ebaz4205-jtag"
DEFAULT_ADAPTER = "bananapi-m2-zero-mmio"
DEFAULT_REMOTE_DIR = "~/.cache/piprobe"
PROGRAM_TIMEOUT = 600.0
PASS_RE = re.compile(r"PL attempt=(\d+) result=pass")
FAIL_RE = re.compile(r"PL attempt=(\d+) result=fail category=(\S+)")
DONE_RE = re.compile(r"PCFG_DONE=(\d)")
EXIT_RE = re.compile(r"exit_status=(\d+)")


@dataclass
class ProgramResult:
    node: openocd.Node | None = None
    bitstream: Path | None = None
    sha256: str = ""
    identity: str = ""
    remote_path: str = ""
    verdict: str = "not run"
    evidence: list[str] = field(default_factory=list)
    exit_code: int | None = None
    error: str | None = None
    output: str = ""
    method: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and self.verdict == "pass" and self.exit_code == 0

    def to_json(self) -> str:
        return json.dumps({
            "node": {"name": self.node.name, "host": self.node.host, "port": self.node.port,
                     "source": self.node.source} if self.node else None,
            "bitstream": str(self.bitstream) if self.bitstream else None,
            "sha256": self.sha256,
            "identity": self.identity,
            "method": self.method,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "exit_code": self.exit_code,
            "error": self.error,
            "ok": self.ok,
        }, indent=2)


def remote_quote(path: str) -> str:
    """Quote a path for the remote shell, keeping a leading ~ expandable.

    shlex.quote("~/x") is "'~/x'", and a quoted tilde is not expanded by any shell, so cd would fail
    with "No such file or directory". $HOME expands inside double quotes, and the remainder stays
    single-quoted so spaces and metacharacters are inert.
    """
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remote_directory(remote_dir: str) -> str:
    return remote_dir.rstrip("/")


def ship_argv(node: openocd.Node, user: str | None, remote_dir: str, remote_file: str) -> list[str]:
    """SSH command that stores the bitstream on the probe through this tool's own connection.

    The payload goes in on stdin, so no extra service, port or credential is involved and nothing is
    written into a world-readable directory. The path is single-quoted on the probe, and the whole
    remote command travels as one argument because ssh joins trailing arguments without escaping.
    """
    directory = remote_directory(remote_dir)
    remote = " ".join([shlex.quote("sh"), "-c",
                       shlex.quote(f"mkdir -p {remote_quote(directory)} && cat > {remote_quote(remote_file)}"),
                       shlex.quote("piprobe")])
    destination = f"{user}@{node.host}" if user else node.host
    return [*openocd.ssh_base(node.port), destination, remote]


def verify_argv(node: openocd.Node, user: str | None, remote_file: str) -> list[str]:
    destination = f"{user}@{node.host}" if user else node.host
    return [*openocd.ssh_base(node.port), destination, f"sha256sum {remote_quote(remote_file)}"]


def runner_argv(node: openocd.Node, user: str | None, runner_dir: str, adapter: str, remote_file: str) -> list[str]:
    """SSH command that runs the probe-local, qualified programming flow.

    The runner runs on the probe at MMIO speed; the scripts ask for root through their own pinned
    sudo rule, which is why nothing here needs --preserve-env.
    """
    script = (f"cd {remote_quote(runner_dir)} && "
              f"EBAZ_JTAG_ADAPTER={remote_quote(adapter)} "
              f"make bitstream UPLOAD_BITSTREAM={remote_quote(remote_file)}")
    destination = f"{user}@{node.host}" if user else node.host
    return [*openocd.ssh_base(node.port), destination, script]


def remove_argv(node: openocd.Node, user: str | None, remote_file: str) -> list[str]:
    destination = f"{user}@{node.host}" if user else node.host
    return [*openocd.ssh_base(node.port), destination, f"rm -f {remote_quote(remote_file)}"]


def parse_verdict(output: str) -> tuple[str, list[str], int | None]:
    """Read the runner's own verdict out of its log.

    The runner already decides pass or fail and says why, so the tool reports that decision rather
    than inventing a second one from the exit status alone.
    """
    verdict = "unknown"
    evidence: list[str] = []
    exit_code: int | None = None
    for line in output.splitlines():
        stripped = line.strip()
        if PASS_RE.search(stripped) or DONE_RE.search(stripped):
            evidence.append(stripped)
            if PASS_RE.search(stripped):
                verdict = "pass"
        elif FAIL_RE.search(stripped):
            verdict = "fail"
            evidence.append(stripped)
        elif "Error" in stripped and len(evidence) < 8:
            evidence.append(stripped)
        match = EXIT_RE.search(stripped)
        if match:
            exit_code = int(match.group(1))
    if verdict == "unknown" and exit_code == 0:
        verdict = "pass"
    return verdict, evidence[:8], exit_code


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="piprobe program",
        description="Load a bitstream into the target attached to a PiProbe node. The file travels over "
                    "this tool's authenticated SSH connection and the probe programs the device locally at "
                    "full speed, because a bit-level network transport cannot carry a bitstream. With "
                    "--check it only ships the file, verifies it and prints what would run.")
    parser.add_argument("--node", help="node to use: its label, advertised name, node id, host, or address")
    parser.add_argument("--file", required=True, type=Path, help="bitstream to load (it stays on this machine)")
    parser.add_argument("--adapter", default=DEFAULT_ADAPTER,
                        help=f"probe-side JTAG adapter name (default: {DEFAULT_ADAPTER})")
    parser.add_argument("--runner-dir", default=DEFAULT_RUNNER_DIR,
                        help=f"repository checkout on the probe that holds the qualified runner "
                             f"(default: {DEFAULT_RUNNER_DIR})")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR,
                        help=f"directory on the probe for the transferred file (default: {DEFAULT_REMOTE_DIR})")
    parser.add_argument("--user", help="SSH user on the probe (default: the local user)")
    parser.add_argument("--peers-file", type=Path, default=discover.default_peers_path(),
                        help="pinned-peer file to read (default: the native PiProbe config path)")
    parser.add_argument("--aliases-file", type=Path, default=discover.default_aliases_path(),
                        help="local aliases file to read (default: the native PiProbe config path)")
    parser.add_argument("--discovery-timeout", type=float, default=None, help="mDNS browse deadline in seconds")
    parser.add_argument("--timeout", type=float, default=PROGRAM_TIMEOUT, help="seconds allowed for programming")
    parser.add_argument("--check", action="store_true",
                        help="ship and verify the file, print the command that would run, and stop")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every command and the full runner log")
    return parser.parse_args(argv)


def report(result: ProgramResult, verbose: bool) -> None:
    if result.node is not None:
        print(f"node: {result.node.name} ({result.node.host}, from {result.node.source})")
    if result.bitstream is not None:
        print(f"bitstream: {result.bitstream} ({result.sha256[:16]}…)")
    if result.method:
        print(f"method: {result.method}")
    print(f"verdict: {result.verdict}" + (f" (runner exit {result.exit_code})" if result.exit_code is not None else ""))
    for line in result.evidence:
        print(f"  {line}")
    if result.error:
        print(f"error: {result.error}")
    if result.remote_path:
        print(f"on the probe: {result.remote_path}")
    if verbose and result.output:
        print(result.output)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = ProgramResult(bitstream=args.file)
    lock_path = discover.default_peers_path().parent / "openocd.lock"

    try:
        if not args.file.exists():
            raise openocd.UsageError(f"bitstream not found: {args.file}")
        if args.timeout <= 0:
            raise openocd.UsageError("--timeout must be positive")
        result.node = openocd.resolve_node(args.node, args.user, args.discovery_timeout or 2.5,
                                           peers_path=args.peers_file.expanduser(),
                                           aliases_path=args.aliases_file.expanduser())
        result.sha256 = sha256_file(args.file)
    except openocd.UsageError as exc:
        print(f"piprobe program: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"piprobe program: cannot read {args.file}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    lock = openocd.SessionLock(lock_path)
    if not lock.acquire():
        print(f"piprobe program: another session holds {lock_path} ({lock.holder() or 'holder unknown'}); "
              "refusing to drive the probe", file=sys.stderr)
        return EXIT_TRANSPORT

    remote_file = f"{remote_directory(args.remote_dir)}/{args.file.name}"
    result.remote_path = remote_file
    shipped = False
    try:
        result.node.host = openocd.choose_identity(result.node, args.user, args.verbose)
        result.identity = result.node.host
        result.method = (f"ship over SSH to {args.remote_dir}, then {args.adapter} runner on the probe "
                         f"(a bit-level transport cannot carry {args.file.stat().st_size} bytes)")

        ship = ship_argv(result.node, args.user, args.remote_dir, remote_file)
        if args.verbose:
            print("+ " + " ".join(shlex.quote(a) for a in ship), file=sys.stderr)
        payload = args.file.read_bytes()
        sent = subprocess.run(ship, input=payload, capture_output=True, timeout=args.timeout + 120)
        if sent.returncode != 0:
            raise openocd.TransportError("could not store the bitstream on the probe: "
                                         + sent.stderr.decode(errors="replace").strip()[:200])
        shipped = True

        checked = openocd.run_child(verify_argv(result.node, args.user, remote_file), timeout=120)
        remote_sha = (checked.stdout or "").split()[0] if checked.stdout.split() else ""
        if remote_sha != result.sha256:
            raise openocd.TransportError(f"the probe's copy does not match: {remote_sha[:16] or 'no digest'} "
                                         f"!= {result.sha256[:16]}")
        if args.check:
            planned = runner_argv(result.node, args.user, args.runner_dir, args.adapter, remote_file)
            print(f"node: {result.node.name} ({result.node.host}, from {result.node.source})")
            print(f"bitstream: {args.file} ({result.sha256[:16]}…) verified on the probe")
            print("would run on the probe: " + " ".join(shlex.quote(a) for a in planned[planned.index(result.node.host) + 1:]))
            result.verdict = "checked"
            return EXIT_OK

        runner = runner_argv(result.node, args.user, args.runner_dir, args.adapter, remote_file)
        if args.verbose:
            print("+ " + " ".join(shlex.quote(a) for a in runner), file=sys.stderr)
        started = time.monotonic()
        run = openocd.run_child(runner, timeout=args.timeout)
        result.output = (run.stdout or "") + (run.stderr or "")
        result.verdict, result.evidence, result.exit_code = parse_verdict(result.output)
        if args.verbose:
            print(f"(runner took {time.monotonic() - started:.1f}s)", file=sys.stderr)
    except (openocd.TransportError, subprocess.TimeoutExpired, OSError) as exc:
        result.error = str(exc) if not isinstance(exc, subprocess.TimeoutExpired) else f"timed out after {args.timeout:.0f}s"
    finally:
        if shipped:
            if result.ok or args.check:
                openocd.run_child(remove_argv(result.node, args.user, remote_file))
                result.remote_path = f"{remote_file} (removed)"
            else:
                result.remote_path = f"{remote_file} (kept for evidence)"
        lock.release()

    if args.json:
        print(result.to_json())
    else:
        report(result, args.verbose)
    if result.ok or result.verdict == "checked":
        return EXIT_OK
    return EXIT_TRANSPORT if result.error else EXIT_VERIFY


if __name__ == "__main__":
    raise SystemExit(main())
