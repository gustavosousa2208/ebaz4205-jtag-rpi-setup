"""Portable tests for PiProbe DNS-SD record handling and CLI configuration."""

from __future__ import annotations

import io
import json
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


class MergeTests(unittest.TestCase):
    """One machine must not be reported as two probes just because two paths found it."""

    def peer(self, name: str = "gusta-bpi", host: str = "gusta-bpi") -> dict[str, object]:
        return {"name": name, "host": host, "port": 22, "transport": "ssh", "capabilities": [],
                "addresses": [], "source": "peer-config", "node_id": "",
                "expected_identity": "ssh-ed25519 SHA256:BICkrMc+MgmpbIzycwz3AAFa7eRaK5syYc/ThRFcaCU"}

    def mdns(self, name: str = "PiProbe-5ef16103", host: str = "gusta-bpi.local.",
             address: str = "192.168.0.101") -> dict[str, object]:
        return {"name": name, "host": host, "port": 22, "transport": "ssh", "capabilities": ["jtag"],
                "addresses": [address], "source": "mdns", "node_id": "5ef16103-0cba-4df1-8af4-ddf366da7387"}

    def test_peer_entry_and_mdns_record_for_one_host_become_one_probe(self) -> None:
        merged = discover.merge_candidates([self.peer(), self.mdns()])
        self.assertEqual(len(merged), 1)
        node = merged[0]
        self.assertEqual(node["name"], "PiProbe-5ef16103")            # the richer record leads
        self.assertEqual(node["sources"], ["mdns", "peer-config"])    # both paths stay visible
        self.assertIn("gusta-bpi", node["known_names"])
        self.assertEqual(node["expected_identity"], self.peer()["expected_identity"])
        self.assertEqual(node["addresses"], ["192.168.0.101"])

    def test_two_machines_found_twice_report_two_probes(self) -> None:
        rows = [self.peer(), self.mdns(), self.mdns("PiProbe-dc4817ec", "gusta-rpi.local.", "192.168.0.69")]
        merged = discover.merge_candidates(rows)
        self.assertEqual([node["name"] for node in merged], ["PiProbe-5ef16103", "PiProbe-dc4817ec"])
        output = io.StringIO()
        with patch.object(sys, "stdout", output):
            discover.print_table(merged, color=False)
        self.assertIn("2 probes found", output.getvalue())

    def test_a_peer_that_names_an_address_merges_with_its_mdns_record(self) -> None:
        merged = discover.merge_candidates([self.peer(host="192.168.0.101"), self.mdns()])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sources"], ["mdns", "peer-config"])

    def test_two_records_from_the_same_path_stay_visible(self) -> None:
        # a duplicate inside one path is a configuration problem, not a node found twice
        merged = discover.merge_candidates([self.peer(), self.peer(name="gusta-bpi-again")])
        self.assertEqual([node["name"] for node in merged], ["gusta-bpi", "gusta-bpi-again"])

    def test_records_for_different_machines_never_merge(self) -> None:
        rows = [self.mdns(), self.mdns("PiProbe-dc4817ec", "gusta-rpi.local.", "192.168.0.69")]
        self.assertEqual(len(discover.merge_candidates(rows)), 2)

    def test_a_peer_alone_is_unchanged(self) -> None:
        merged = discover.merge_candidates([self.peer()])
        self.assertEqual(len(merged), 1)
        self.assertNotIn("sources", merged[0])


class AnnounceNameTests(unittest.TestCase):
    """The advertised instance name is a label for humans; the node id stays the identity."""

    def test_label_is_lowercased_and_reduced_to_dashes(self) -> None:
        self.assertEqual(announce.sanitize_label("Banana Pi JTAG #1"), "banana-pi-jtag-1")
        self.assertEqual(announce.sanitize_label("  --weird--  "), "weird")
        self.assertEqual(announce.sanitize_label(None), "")
        self.assertEqual(announce.sanitize_label("ünïcode"), "n-code")

    def test_label_is_bounded_and_never_left_dangling(self) -> None:
        cleaned = announce.sanitize_label("a" * 40)
        self.assertEqual(len(cleaned), announce.LABEL_MAX)
        self.assertFalse(announce.sanitize_label("a" * 23 + "-" * 5).endswith("-"))

    def test_default_label_is_the_host_name_the_srv_record_already_publishes(self) -> None:
        name = announce.service_name("5ef16103-0cba-4df1-8af4-ddf366da7387", None, "gusta-bpi")
        self.assertEqual(name, f"PiProbe-gusta-bpi.{discover.SERVICE_TYPE}")

    def test_explicit_label_wins(self) -> None:
        name = announce.service_name("5ef16103-0cba-4df1-8af4-ddf366da7387", "Banana Pi!", "gusta-bpi")
        self.assertEqual(name, f"PiProbe-banana-pi.{discover.SERVICE_TYPE}")

    def test_unusable_names_fall_back_to_the_opaque_node_id(self) -> None:
        name = announce.service_name("5ef16103-0cba-4df1-8af4-ddf366da7387", None, "...")
        self.assertEqual(name, f"PiProbe-5ef16103.{discover.SERVICE_TYPE}")

    def test_announce_rejects_a_label_with_nothing_usable_in_it(self) -> None:
        self.assertEqual(announce.main(["--label=---"]), 2)

    def zeroconf_fakes(self, behaviour: str = "ok") -> tuple[dict[str, object], dict[str, object]]:
        """Minimal stand-ins for the zeroconf API announce uses, plus a record of what it registered."""
        fake_ifaddr = types.ModuleType("ifaddr")
        fake_ifaddr.get_adapters = lambda: [SimpleNamespace(ips=[SimpleNamespace(ip="192.168.0.101")])]
        calls: dict[str, object] = {}

        class Info:
            def __init__(self, type_, name, addresses=None, port=None, properties=None, server=None):
                self.type, self.name = type_, name
                self.addresses, self.port, self.properties, self.server = addresses, port, properties, server

        class Zeroconf:
            def __init__(self, ip_version=None):
                self.ip_version = ip_version

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def register_service(self, info, **kwargs):
                calls["name"], calls["kwargs"] = info.name, kwargs
                if behaviour == "conflict":
                    raise module.Error("NonUniqueNameException: the name is in use")
                raise SystemExit(0)      # stop publish before its serve loop

            def unregister_service(self, info):
                return None

        module = types.ModuleType("zeroconf")
        module.IPVersion = SimpleNamespace(All=1)
        module.ServiceInfo = Info
        module.Zeroconf = Zeroconf
        module.Error = type("Error", (Exception,), {})
        module.NonUniqueNameException = type("NonUniqueNameException", (module.Error,), {})
        return {"ifaddr": fake_ifaddr, "zeroconf": module}, calls

    def test_publish_asks_for_conflict_resolution_and_uses_the_effective_name(self) -> None:
        fakes, calls = self.zeroconf_fakes()
        with patch.dict(sys.modules, fakes):
            with self.assertRaises(SystemExit):
                announce.publish("5ef16103-0cba-4df1-8af4-ddf366da7387", 22, "Banana Pi")
        self.assertEqual(calls["name"], f"PiProbe-banana-pi.{discover.SERVICE_TYPE}")
        self.assertTrue(calls["kwargs"].get("allow_name_change"),
                        "a name conflict must rename, not stop the probe from being discoverable")

    def test_publish_reports_a_name_conflict_cleanly(self) -> None:
        fakes, calls = self.zeroconf_fakes(behaviour="conflict")
        with patch.dict(sys.modules, fakes):
            self.assertEqual(announce.publish("5ef16103-0cba-4df1-8af4-ddf366da7387", 22, "banana-pi"), 1)
        self.assertEqual(calls["name"], f"PiProbe-banana-pi.{discover.SERVICE_TYPE}")


class AliasTests(unittest.TestCase):
    """Aliases are the operator's local names: unpublished and never a trust decision."""

    def write(self, directory: str, document: object) -> Path:
        path = Path(directory) / "aliases.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def row(self, name: str, node_id: str = "", address: str = "192.168.0.101") -> dict[str, object]:
        return {"name": name, "node_id": node_id, "host": f"{name}.local.", "port": 22, "addresses": [address],
                "capabilities": ["jtag"], "transport": "ssh", "source": "mdns"}

    def test_missing_file_means_no_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(discover.load_aliases(Path(temp_dir) / "aliases.json"), {})

    def test_alias_applies_by_node_id_and_by_advertised_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write(temp_dir, {"5ef16103-0cba-4df1-8af4-ddf366da7387": "banana-pi",
                                         "PiProbe-dc4817ec": "rpi4-lab"})
            aliases = discover.load_aliases(path)
            rows = discover.apply_aliases(
                [self.row("PiProbe-5ef16103", "5ef16103-0cba-4df1-8af4-ddf366da7387"),
                 self.row("PiProbe-dc4817ec", "dc4817ec-2570-47f4-b6b7-471b66bbee6b", "192.168.0.69"),
                 self.row("PiProbe-other", "ffffffff-0000-0000-0000-000000000000", "192.168.0.70")],
                aliases)
            self.assertEqual([discover.display_name(row) for row in rows], ["banana-pi", "rpi4-lab", "PiProbe-other"])
            self.assertEqual(rows[0]["name"], "PiProbe-5ef16103")       # the wire name is untouched

    def test_the_table_shows_the_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            aliases = discover.load_aliases(self.write(temp_dir, {"PiProbe-5ef16103": "banana-pi"}))
            rows = discover.apply_aliases([self.row("PiProbe-5ef16103", "5ef16103-0000")], aliases)
            output = io.StringIO()
            with patch.object(sys, "stdout", output):
                discover.print_table(rows, color=False)
            self.assertIn("banana-pi", output.getvalue())

    def test_an_invalid_alias_is_an_error_not_a_silent_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            for bad in ("has space", "sla/sh", "", "x" * 40):
                with self.assertRaises(ValueError):
                    discover.load_aliases(self.write(temp_dir, {"PiProbe-5ef16103": bad}))

    def test_a_non_object_document_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                discover.load_aliases(self.write(temp_dir, ["banana-pi"]))


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
        expected_width = sum(discover.TABLE_WIDTHS) + 2 * (len(discover.TABLE_WIDTHS) - 1)
        for line in lines[1:4]:
            self.assertEqual(len(line), expected_width, line)
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
