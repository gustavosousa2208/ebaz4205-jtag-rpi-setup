"""Publish minimal PiProbe DNS-SD metadata; this never starts JTAG control."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import time
import uuid
from pathlib import Path

from piprobe.discover import SERVICE_TYPE, default_peers_path

# The advertised instance name is a label for humans, never an identity: the TXT record carries the
# opaque node id, and a host verifies that id plus the SSH host key before trusting anything.
LABEL_MAX = 24
LABEL_JUNK = re.compile(r"[^a-z0-9]+")


def sanitize_label(text: object) -> str:
    """Reduce operator text to a DNS-SD-safe label: lowercase, dashes, no surprises."""
    return LABEL_JUNK.sub("-", str(text or "").strip().casefold()).strip("-")[:LABEL_MAX].strip("-")


def service_name(node_id: str, label: str | None = None, hostname: str | None = None) -> str:
    """DNS-SD instance name: a descriptive label when one is available, the opaque node id otherwise.

    A label is only ever as revealing as the host name the SRV record already publishes, and it can
    always be overridden with --label. It does not identify the node.
    """
    source = label if label is not None else (hostname if hostname is not None else socket.gethostname())
    cleaned = sanitize_label(str(source).split(".", 1)[0])
    return f"PiProbe-{cleaned or node_id[:8]}.{SERVICE_TYPE}"


def default_identity_path() -> Path:
    return default_peers_path().parent / "node.json"


def load_or_create_node_id(path: Path) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            node_id = data.get("node_id") if isinstance(data, dict) else None
            return str(uuid.UUID(node_id))
        node_id = str(uuid.uuid4())
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"node_id": node_id}) + "\n", encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
        return node_id
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ValueError(f"cannot load or create node identity at {path}: {exc}") from exc


def local_addresses() -> list[bytes]:
    found: set[bytes] = set()
    # Include interface addresses directly instead of resolving the hostname,
    # which can select an overlay/VPN address unrelated to the mDNS LAN.
    import ifaddr

    overlay_ranges = (
        ipaddress.ip_network("100.64.0.0/10"),  # shared address space, e.g. Tailscale IPv4
        ipaddress.ip_network("fd7a:115c:a1e0::/48"),  # Tailscale IPv6
    )
    for adapter in ifaddr.get_adapters():
        for address in adapter.ips:
            try:
                raw = address.ip[0] if isinstance(address.ip, tuple) else address.ip
                ip = ipaddress.ip_address(raw.split("%", 1)[0])
            except (ValueError, TypeError, AttributeError):
                continue
            if (ip.is_loopback or ip.is_unspecified or ip.is_link_local
                    or any(ip.version == network.version and ip in network for network in overlay_ranges)):
                continue
            found.add(ip.packed)
    if not found:
        raise RuntimeError("no routable local address found; refusing to publish")
    return sorted(found)


def publish(node_id: str, port: int, label: str | None = None) -> int:
    try:
        from zeroconf import Error as ZeroconfError, IPVersion, ServiceInfo, Zeroconf
    except ImportError as exc:
        print("piprobe announce: install piprobe with its zeroconf dependency", file=sys.stderr)
        return 2

    try:
        addresses = local_addresses()
        hostname = socket.gethostname().split(".", 1)[0]
        server = f"{hostname}.local."
        name = service_name(node_id, label, hostname)
        info = ServiceInfo(
            SERVICE_TYPE,
            name,
            addresses=addresses,
            port=port,
            properties={"id": node_id, "api": "1", "caps": "jtag", "transport": "ssh"},
            server=server,
        )
        running = True

        def stop(_signum: int, _frame: object) -> None:
            nonlocal running
            running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, stop)
        with Zeroconf(ip_version=IPVersion.All) as zc:
            # A name already advertised on this link — a stale record from an earlier run, or another
            # probe with the same label — must not keep this probe undiscoverable: mDNS conflict
            # resolution renames the instance until it is unique, and the TXT node id stays the
            # identity either way. info.name holds the effective name after registration.
            zc.register_service(info, allow_name_change=True)
            print(f"PiProbe {node_id} announced as {info.name.removesuffix('.' + SERVICE_TYPE)} "
                  f"on SSH port {port}", flush=True)
            try:
                while running:
                    time.sleep(1)
            finally:
                zc.unregister_service(info)
        return 0
    except ZeroconfError as exc:
        print(f"piprobe announce: {exc}; if the name is taken, choose a different --label", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"piprobe announce: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish minimal PiProbe DNS-SD metadata; does not enable JTAG control.")
    parser.add_argument("--port", type=int, default=22, help="SSH port advertised through DNS-SD (default: 22)")
    parser.add_argument("--label", help="human-readable name to advertise (default: this machine's host name, "
                                       f"which the SRV record already publishes); lowercased, dashes only, {LABEL_MAX} chars max")
    parser.add_argument("--identity-file", type=Path, default=default_identity_path())
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.label is not None:
        cleaned = sanitize_label(args.label)
        if not cleaned:
            print("piprobe announce: --label needs at least one letter or digit", file=sys.stderr)
            return 2
        if cleaned != args.label.strip().casefold():
            print(f"piprobe announce: advertising label {cleaned!r} for {args.label!r}", file=sys.stderr)
    try:
        node_id = load_or_create_node_id(args.identity_file.expanduser())
    except ValueError as exc:
        print(f"piprobe announce: {exc}", file=sys.stderr)
        return 2
    return publish(node_id, args.port, args.label)


if __name__ == "__main__":
    raise SystemExit(main())
