"""Publish minimal PiProbe DNS-SD metadata; this never starts JTAG control."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import socket
import sys
import time
import uuid
from pathlib import Path

from piprobe.discover import SERVICE_TYPE, default_peers_path


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


def publish(node_id: str, port: int) -> int:
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
    except ImportError as exc:
        print("piprobe announce: install piprobe with its zeroconf dependency", file=sys.stderr)
        return 2

    try:
        addresses = local_addresses()
        hostname = socket.gethostname().split(".", 1)[0]
        server = f"{hostname}.local."
        name = f"PiProbe-{node_id[:8]}.{SERVICE_TYPE}"
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
            zc.register_service(info)
            print(f"PiProbe {node_id} announced on SSH port {port}", flush=True)
            try:
                while running:
                    time.sleep(1)
            finally:
                zc.unregister_service(info)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"piprobe announce: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish minimal PiProbe DNS-SD metadata; does not enable JTAG control.")
    parser.add_argument("--port", type=int, default=22, help="SSH port advertised through DNS-SD (default: 22)")
    parser.add_argument("--identity-file", type=Path, default=default_identity_path())
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        node_id = load_or_create_node_id(args.identity_file.expanduser())
    except ValueError as exc:
        print(f"piprobe announce: {exc}", file=sys.stderr)
        return 2
    return publish(node_id, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
