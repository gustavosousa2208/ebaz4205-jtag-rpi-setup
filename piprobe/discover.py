#!/usr/bin/env python3
"""List unverified PiProbe candidates from mDNS and an optional peer file."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import os
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path


SERVICE_TYPE = "_piprobe._tcp.local."
DEFAULT_TIMEOUT = 2.5
MAX_TIMEOUT = 120.0
DEFAULT_QUIET_PERIOD = 0.6
SERVICE_INFO_TIMEOUT_MS = 500
SAFE_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,79}$")
SAFE_CAPABILITY = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")
SUPPORTED_TRANSPORTS = {"tcp", "ssh"}


def safe_text(value: object, limit: int = 120) -> str:
    """Remove terminal control characters from untrusted network text."""
    text = str(value or "")
    return "".join(char if not unicodedata.category(char).startswith("C") else "�" for char in text)[:limit]


def decode_properties(properties: dict[object, object]) -> dict[str, str]:
    decoded: dict[str, str] = {}
    for raw_key, raw_value in properties.items():
        key = raw_key.decode("utf-8", "replace") if isinstance(raw_key, bytes) else str(raw_key)
        value = raw_value.decode("utf-8", "replace") if isinstance(raw_value, bytes) else str(raw_value)
        decoded[safe_text(key, 32).lower()] = safe_text(value, 160)
    return decoded


def valid_ip_addresses(info: object) -> list[str]:
    try:
        addresses = info.parsed_addresses()
    except (AttributeError, OSError, ValueError):
        return []
    result = []
    for address in addresses:
        try:
            ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            continue
        result.append(address)
    return sorted(set(result))


def candidate_from_service(info: object, service_name: str, source: str = "mdns") -> dict[str, object]:
    properties = decode_properties(getattr(info, "properties", {}) or {})
    raw_id = properties.get("id", "")
    node_id = raw_id if SAFE_TOKEN.fullmatch(raw_id) else ""
    raw_caps = properties.get("caps", "")
    capabilities = sorted({
        item for item in raw_caps.lower().split(",")
        if SAFE_CAPABILITY.fullmatch(item)
    })
    transport = properties.get("transport", "tcp").lower()
    if transport not in SUPPORTED_TRANSPORTS:
        transport = "tcp"
    port = getattr(info, "port", 0) or 0
    instance_name = service_name.removesuffix(SERVICE_TYPE).rstrip(".")
    return {
        "name": safe_text(instance_name, 120),
        "node_id": node_id,
        "api": properties.get("api", "")[:32],
        "capabilities": capabilities,
        "transport": transport,
        "host": safe_text(getattr(info, "server", ""), 253),
        "port": port if 1 <= port <= 65535 else None,
        "addresses": valid_ip_addresses(info),
        "source": source,
        "state": "unverified",
    }


def host_names(row: dict[str, object]) -> set[str]:
    """Host names a record can be recognized by, normalized without resolving anything."""
    names: set[str] = set()
    for value in (row.get("host"), *(row.get("aliases") or [])):
        text = str(value or "").strip().rstrip(".").casefold()
        if not text:
            continue
        names.add(text)
        if text.endswith(".local"):
            names.add(text[: -len(".local")])
    return names


def node_addresses(row: dict[str, object]) -> set[str]:
    return {str(address).strip().casefold() for address in (row.get("addresses") or []) if str(address).strip()}


def same_node(first: dict[str, object], second: dict[str, object]) -> bool:
    """True when two records provably describe one machine, without contacting either of them.

    Names are compared with names and addresses with addresses, and also across the two, because a
    pinned peer may name the probe by address while the mDNS record names it by host.
    """
    first_names, second_names = host_names(first), host_names(second)
    first_addresses, second_addresses = node_addresses(first), node_addresses(second)
    return bool(first_addresses & second_addresses
                or first_names & second_names
                or first_addresses & second_names
                or second_addresses & first_names)


def merge_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    """Collapse one machine discovered through more than one path into a single entry.

    A pinned peer entry and the mDNS record for the same probe are one node: announcing both would
    report one machine as two probes. Every path stays visible in 'sources' so an operator can still
    see why the node appeared, and the peer's name becomes an alias. Two records from the same path
    are never merged, because a duplicate there is a configuration problem worth seeing.
    """
    merged: list[dict[str, object]] = []
    for row in candidates:
        for index, existing in enumerate(merged):
            if existing.get("source") == row.get("source") or not same_node(existing, row):
                continue
            # the richer record leads; a peer entry contributes its name and pinned identity
            preferred, other = (existing, row) if existing.get("source") != "peer-config" else (row, existing)
            preferred = dict(preferred)
            paths = [str(preferred.get("source")), *(str(path) for path in (preferred.get("sources") or [])),
                     str(other.get("source")), *(str(path) for path in (other.get("sources") or []))]
            preferred["sources"] = list(dict.fromkeys(paths))
            names = [*(preferred.get("aliases") or []), str(other.get("name", "")), *(other.get("aliases") or [])]
            aliases = [str(name) for name in dict.fromkeys(names) if name]
            if aliases:
                preferred["aliases"] = aliases
            if other.get("expected_identity"):
                preferred["expected_identity"] = other["expected_identity"]
            merged[index] = preferred
            break
        else:
            merged.append(dict(row))
    return merged


class ProbeListener:
    """Collect only service records; never connect to an advertised endpoint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._records: dict[str, dict[str, object]] = {}
        self._revision = 0
        self._executor = ThreadPoolExecutor(max_workers=8)

    def add_service(self, zc: object, type_: str, name: str) -> None:
        self._executor.submit(self._refresh, zc, type_, name)

    def update_service(self, zc: object, type_: str, name: str) -> None:
        self._executor.submit(self._refresh, zc, type_, name)

    def remove_service(self, zc: object, type_: str, name: str) -> None:
        with self._changed:
            self._records.pop(name, None)
            self._revision += 1
            self._changed.notify_all()

    def _refresh(self, zc: object, type_: str, name: str) -> None:
        # mDNS answers may arrive in separate packets; a bounded lookup lets
        # the library finish assembling SRV, TXT, and address records.
        info = zc.get_service_info(type_, name, timeout=SERVICE_INFO_TIMEOUT_MS)
        record = candidate_from_service(info, name) if info is not None else {
            "name": safe_text(name.removesuffix(SERVICE_TYPE).rstrip("."), 120),
            "node_id": "",
            "api": "",
            "capabilities": [],
            "transport": "tcp",
            "host": "",
            "port": None,
            "addresses": [],
            "source": "mdns",
            "state": "unverified",
        }
        with self._changed:
            self._records[name] = record
            self._revision += 1
            self._changed.notify_all()

    def wait_for_change(self, revision: int, timeout: float) -> int:
        with self._changed:
            self._changed.wait_for(lambda: self._revision != revision, timeout=max(0.0, timeout))
            return self._revision

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return sorted((dict(row) for row in self._records.values()), key=lambda row: str(row["name"]).casefold())


def default_peers_path() -> Path:
    if os.name == "nt":
        config_root = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return config_root / "PiProbe" / "peers.json"
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "piprobe" / "peers.json"


def load_peer_candidates(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read peer file {path}: {exc}") from exc
    peers = document.get("peers") if isinstance(document, dict) else None
    if not isinstance(peers, list):
        raise ValueError("peer file must contain a 'peers' array")

    candidates = []
    for index, peer in enumerate(peers):
        if not isinstance(peer, dict):
            raise ValueError(f"peer {index + 1} must be an object")
        name = safe_text(peer.get("name", ""), 120).strip()
        host = safe_text(peer.get("host", ""), 253).strip()
        identity = safe_text(peer.get("identity", ""), 160).strip()
        transport = safe_text(peer.get("transport", "tcp"), 16).strip().lower()
        try:
            port = int(peer.get("port", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"peer {index + 1} has an invalid port") from exc
        if not name or not host or not identity or not 1 <= port <= 65535:
            raise ValueError(f"peer {index + 1} needs name, host, port, and pinned identity")
        if transport not in SUPPORTED_TRANSPORTS:
            raise ValueError(f"peer {index + 1} has an unsupported transport")
        candidates.append({
            "name": name,
            "node_id": "",
            "api": "",
            "capabilities": [],
            "transport": transport,
            "host": host,
            "port": port,
            "addresses": [],
            "source": "peer-config",
            "expected_identity": identity,
            "state": "unverified",
        })
    return candidates


def discover_mdns(timeout: float) -> list[dict[str, object]]:
    try:
        from zeroconf import IPVersion, ServiceBrowser, Zeroconf
    except ImportError as exc:
        raise RuntimeError("missing dependency; install piprobe/requirements.txt") from exc

    listener = ProbeListener()
    try:
        with Zeroconf(ip_version=IPVersion.All) as zc:
            browser = ServiceBrowser(zc, SERVICE_TYPE, listener)
            try:
                deadline = time.monotonic() + timeout
                revision = 0
                quiet_deadline: float | None = None
                while True:
                    now = time.monotonic()
                    if now >= deadline or (quiet_deadline is not None and now >= quiet_deadline):
                        break
                    end = min(deadline, quiet_deadline) if quiet_deadline is not None else deadline
                    previous_revision = revision
                    revision = listener.wait_for_change(revision, end - now)
                    if revision != previous_revision:
                        quiet_deadline = min(deadline, time.monotonic() + DEFAULT_QUIET_PERIOD)
            finally:
                browser.cancel()
                listener.close()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"mDNS discovery could not start: {exc}") from exc
    return listener.snapshot()


def display_width(value: str) -> int:
    return sum(
        0 if unicodedata.combining(char) else
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in value
    )


def fit_cell(value: object, width: int) -> str:
    text = safe_text(value, 256)
    if display_width(text) > width:
        clipped = ""
        for char in text:
            if display_width(clipped + char + "…") > width:
                break
            clipped += char
        text = clipped + "…"
    return text + " " * max(0, width - display_width(text))


def color_enabled() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    force = os.environ.get("FORCE_COLOR")
    if force is not None:
        return force.strip().lower() not in {"", "0", "false", "no"}
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def paint(text: str, code: str, enabled: bool) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if enabled else text


def endpoint_for(row: dict[str, object]) -> str:
    addresses = row.get("addresses") or []
    host = ""
    for address in addresses:
        candidate = str(address)
        if ":" not in candidate:
            host = candidate
            break
        if not host:
            host = f"[{candidate}]"
    if not host:
        host = safe_text(row.get("host", ""), 253).removesuffix(".")
    port = row.get("port")
    return f"{host}:{port}" if port else host or "-"


TABLE_WIDTHS = (16, 10, 20, 5, 6, 10)
SOURCE_LABELS = {"mdns": "mDNS", "peer-config": "peer"}


def print_table(candidates: list[dict[str, object]], *, color: bool | None = None) -> None:
    use_color = color_enabled() if color is None else color
    if not candidates:
        print(paint("No probes found.", "33", use_color))
        print("mDNS is local-network only; use a trusted peer if multicast is blocked.")
        return
    count = len(candidates)
    noun = "probe" if count == 1 else "probes"
    print(f"{paint('PiProbe', '1;36', use_color)}  {paint(f'{count} {noun} found | all unverified', '2', use_color)}")

    widths = TABLE_WIDTHS
    headers = ("PROBE", "STATE", "ADDRESS", "LINK", "CAPS", "FROM")
    print("  ".join(paint(fit_cell(label, width), "1", use_color) for label, width in zip(headers, widths)))

    state_colors = {"ready": "32", "trusted": "32", "busy": "33", "untrusted": "31", "unverified": "33"}
    for row in sorted(candidates, key=lambda item: str(item.get("name", "")).casefold()):
        state = safe_text(row.get("state", "unknown"), 24).upper()
        paths = row.get("sources") or [row.get("source", "?")]
        source = ",".join(SOURCE_LABELS.get(str(path), safe_text(path, 24)) for path in paths)
        caps = ",".join(safe_text(cap, 16).upper() for cap in row.get("capabilities", [])) or "-"
        values = (
            safe_text(row.get("name", "-"), 256),
            state,
            endpoint_for(row),
            safe_text(row.get("transport", "tcp"), 12).upper(),
            caps,
            source,
        )
        codes = ("1;36", state_colors.get(state.lower(), "0"), "36", "34", "35", "2")
        cells = [paint(fit_cell(value, width), code, use_color) for value, width, code in zip(values, widths, codes)]
        print("  ".join(cells))
    print(paint("Discovery does not authorize JTAG access.", "2", use_color))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover PiProbe candidates without connecting to their control endpoints.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"maximum mDNS browse duration in seconds (default: {DEFAULT_TIMEOUT:g}; max: {MAX_TIMEOUT:g}); after a result, discovery ends after {DEFAULT_QUIET_PERIOD:g}s without changes")
    parser.add_argument("--peers-file", type=Path, default=default_peers_path(), help="optional JSON file of explicitly pinned peers")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    if not 0.5 <= args.timeout <= MAX_TIMEOUT:
        parser.error(f"--timeout must be between 0.5 and {MAX_TIMEOUT:g} seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        candidates = load_peer_candidates(args.peers_file.expanduser())
    except ValueError as exc:
        print(f"piprobe-discover: {exc}", file=sys.stderr)
        return 2

    try:
        candidates.extend(discover_mdns(args.timeout))
    except RuntimeError as exc:
        if not candidates:
            print(f"piprobe-discover: {exc}", file=sys.stderr)
            return 2
        print(f"piprobe-discover: warning: {exc}; showing configured peers only", file=sys.stderr)

    candidates = merge_candidates(candidates)
    if args.json:
        print(json.dumps(candidates, indent=2, ensure_ascii=False))
    else:
        print_table(candidates)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
