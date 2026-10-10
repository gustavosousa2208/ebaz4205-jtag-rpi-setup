"""Portable tests for PiProbe DNS-SD record handling and CLI configuration."""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from piprobe import discover
from piprobe import announce


def sample_info() -> SimpleNamespace:
    return SimpleNamespace(
        properties={
            b"id": b"node-01",
            b"api": b"1",
            b"caps": b"jtag,swd,invalid capability",
            b"transport": b"ssh",
        },
        port=2542,
        server="piprobe-node.local.",
        parsed_addresses=lambda: ["192.168.1.20", "fe80::20%en0", "not-an-ip"],
    )


class DiscoveryRecordTests(unittest.TestCase):
    def test_human_table_is_aligned_and_uses_optional_color(self) -> None:
        rows = [
            discover.candidate_from_service(sample_info(), "PiProbe-Node._piprobe._tcp.local."),
            {
                "name": "gusta-bpi",
                "state": "unverified",
                "addresses": [],
                "host": "gusta-bpi",
                "port": 22,
                "transport": "ssh",
                "capabilities": [],
                "source": "peer-config",
            },
        ]
        output = io.StringIO()
        with patch.object(sys, "stdout", output):
            discover.print_table(rows, color=True)

        rendered = output.getvalue()
        self.assertIn("\x1b[", rendered)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        lines = plain.splitlines()
        self.assertIn("2 probes found", lines[0])
        self.assertEqual(len(lines[1]), 80)
        self.assertEqual(len(lines[2]), 80)
        self.assertEqual(len(lines[3]), 80)
        self.assertTrue(any("192.168.1.20:2542" in line for line in lines))
        self.assertTrue(any("gusta-bpi:22" in line for line in lines))

        output = io.StringIO()
        with patch.object(sys, "stdout", output):
            discover.print_table(rows, color=False)
        self.assertNotIn("\x1b[", output.getvalue())

    def test_announce_uses_lan_interfaces_and_skips_tailscale(self) -> None:
        fake_ifaddr = types.ModuleType("ifaddr")
        fake_ifaddr.get_adapters = lambda: [SimpleNamespace(ips=[
            SimpleNamespace(ip="192.168.0.20"),
            SimpleNamespace(ip="100.114.73.30"),
            SimpleNamespace(ip=("fd7a:115c:a1e0::1", 0, 0)),
            SimpleNamespace(ip=("fd00::20", 0, 0)),
            SimpleNamespace(ip="127.0.0.1"),
        ])]
        with patch.dict(sys.modules, {"ifaddr": fake_ifaddr}):
            addresses = announce.local_addresses()

        self.assertEqual(addresses, [
            bytes.fromhex("c0a80014"),
            bytes.fromhex("fd000000000000000000000000000020"),
        ])

    def test_announce_identity_is_stable_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            identity_path = Path(temp_dir) / "piprobe" / "node.json"
            first = announce.load_or_create_node_id(identity_path)
            second = announce.load_or_create_node_id(identity_path)

            self.assertEqual(first, second)
            self.assertEqual(len(first), 36)
            self.assertEqual(identity_path.read_text(encoding="utf-8").count(first), 1)
            if os.name != "nt":
                self.assertEqual(identity_path.stat().st_mode & 0o777, 0o600)

    def test_candidate_parses_safe_metadata_and_both_address_families(self) -> None:
        candidate = discover.candidate_from_service(
            sample_info(), "PiProbe-Node._piprobe._tcp.local."
        )

        self.assertEqual(candidate["name"], "PiProbe-Node")
        self.assertEqual(candidate["node_id"], "node-01")
        self.assertEqual(candidate["capabilities"], ["jtag", "swd"])
        self.assertEqual(candidate["transport"], "ssh")
        self.assertEqual(candidate["addresses"], ["192.168.1.20", "fe80::20%en0"])
        self.assertEqual(candidate["port"], 2542)
        self.assertEqual(candidate["state"], "unverified")

    def test_listener_collects_updates_and_removes_services(self) -> None:
        listener = discover.ProbeListener()
        fake_zeroconf = SimpleNamespace(get_service_info=lambda *_args, **_kwargs: sample_info())
        service_name = "PiProbe-Node._piprobe._tcp.local."

        try:
            listener.add_service(fake_zeroconf, discover.SERVICE_TYPE, service_name)
            revision = listener.wait_for_change(0, 1.0)
            self.assertGreater(revision, 0)
            self.assertEqual(len(listener.snapshot()), 1)
            self.assertEqual(listener.snapshot()[0]["name"], "PiProbe-Node")

            listener.update_service(fake_zeroconf, discover.SERVICE_TYPE, service_name)
            next_revision = listener.wait_for_change(revision, 1.0)
            self.assertGreater(next_revision, revision)
            listener.remove_service(fake_zeroconf, discover.SERVICE_TYPE, service_name)
            self.assertEqual(listener.snapshot(), [])
        finally:
            listener.close()

    def test_discovery_uses_both_ip_versions_and_completes_browse(self) -> None:
        info = sample_info()
        state: dict[str, object] = {}

        class FakeZeroconf:
            def __init__(self, *, ip_version: object) -> None:
                state["ip_version"] = ip_version

            def __enter__(self) -> "FakeZeroconf":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def get_service_info(self, *_args: object, **_kwargs: object) -> object:
                return info

        class FakeServiceBrowser:
            def __init__(self, zc: FakeZeroconf, service_type: str, listener: object) -> None:
                state["service_type"] = service_type
                listener.add_service(zc, service_type, "PiProbe-Node._piprobe._tcp.local.")

            def cancel(self) -> None:
                state["cancelled"] = True

        all_versions = object()
        fake_module = types.ModuleType("zeroconf")
        fake_module.IPVersion = SimpleNamespace(All=all_versions)
        fake_module.ServiceBrowser = FakeServiceBrowser
        fake_module.Zeroconf = FakeZeroconf

        with patch.dict(sys.modules, {"zeroconf": fake_module}):
            started = time.monotonic()
            candidates = discover.discover_mdns(2.5)
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0)
        self.assertIs(state["ip_version"], all_versions)
        self.assertEqual(state["service_type"], discover.SERVICE_TYPE)
        self.assertTrue(state["cancelled"])
        self.assertEqual([candidate["name"] for candidate in candidates], ["PiProbe-Node"])

    def test_missing_zeroconf_dependency_has_clear_error(self) -> None:
        with patch.dict(sys.modules, {"zeroconf": None}):
            with self.assertRaisesRegex(RuntimeError, "missing dependency"):
                discover.discover_mdns(1.0)

    def test_peer_file_requires_a_pinned_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            peer_file = Path(temp_dir) / "peers.json"
            peer_file.write_text(
                '{"peers":[{"name":"node","host":"192.0.2.1","port":2542}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "pinned identity"):
                discover.load_peer_candidates(peer_file)

    def test_peer_file_preserves_ssh_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            peer_file = Path(temp_dir) / "peers.json"
            peer_file.write_text(
                '{"peers":[{"name":"node","host":"node.local","port":22,'
                '"identity":"ssh-ed25519 SHA256:example","transport":"ssh"}]}',
                encoding="utf-8",
            )
            candidates = discover.load_peer_candidates(peer_file)

        self.assertEqual(candidates[0]["transport"], "ssh")
        self.assertEqual(candidates[0]["port"], 22)

    def test_default_peer_file_uses_native_config_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            if os.name == "nt":
                with patch.dict(os.environ, {"APPDATA": temp_dir, "XDG_CONFIG_HOME": ""}):
                    expected = Path(temp_dir) / "PiProbe" / "peers.json"
                    self.assertEqual(discover.default_peers_path(), expected)
            else:
                with patch.dict(os.environ, {"XDG_CONFIG_HOME": temp_dir, "APPDATA": ""}):
                    expected = Path(temp_dir) / "piprobe" / "peers.json"
                    self.assertEqual(discover.default_peers_path(), expected)


if __name__ == "__main__":
    unittest.main()
