"""Tests for the host OpenOCD wrapper: planning, IDCODE verification, locking and argv construction."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from piprobe import openocd

NODE = openocd.Node(name="PiProbe-5ef16103", host="192.168.0.101", port=22, source="mdns", node_id="5ef16103-aaaa")

GOOD_SCAN = """Open On-Chip Debugger 0.12.0
Info : remote_bitbang driver initialized
Info : JTAG tap: zynq_pl.bs tap/device found: 0x13722093 (mfg: 0x049 (Xilinx), part: 0x3722, ver: 0x1)
Info : JTAG tap: zynq.cpu tap/device found: 0x4ba00477 (mfg: 0x23b (ARM Ltd), part: 0xba00, ver: 0x4)
Info : JTAG scan chain interrogation failed: 0
Info : but continuing anyway
"""


class VerifyTests(unittest.TestCase):
    def test_accepts_the_observed_idcodes_including_the_version_nibble(self) -> None:
        found, problems = openocd.verify_idcodes(GOOD_SCAN)
        self.assertEqual(problems, [])
        self.assertEqual(found, {"zynq_pl.bs": 0x13722093, "zynq.cpu": 0x4BA00477})

    def test_accepts_openocds_own_version_zero_idcode(self) -> None:
        found, problems = openocd.verify_idcodes(
            "Info : JTAG tap: zynq_pl.bs tap/device found: 0x03722093\n"
            "Info : JTAG tap: zynq.cpu tap/device found: 0x4ba00477\n")
        self.assertEqual(problems, [])
        self.assertEqual(found["zynq_pl.bs"], 0x03722093)

    def test_reports_a_wrong_part(self) -> None:
        _, problems = openocd.verify_idcodes(
            "Info : JTAG tap: zynq_pl.bs tap/device found: 0x03727093\n"
            "Info : JTAG tap: zynq.cpu tap/device found: 0x4ba00477\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("zynq_pl.bs", problems[0])

    def test_reports_the_dap_tap_exactly(self) -> None:
        _, problems = openocd.verify_idcodes(
            "Info : JTAG tap: zynq_pl.bs tap/device found: 0x03722093\n"
            "Info : JTAG tap: zynq.cpu tap/device found: 0x5ba00477\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("zynq.cpu", problems[0])

    def test_reports_a_missing_tap(self) -> None:
        found, problems = openocd.verify_idcodes(
            "Info : JTAG tap: zynq_pl.bs tap/device found: 0x13722093\n")
        self.assertEqual(found, {"zynq_pl.bs": 0x13722093})
        self.assertEqual(len(problems), 1)
        self.assertIn("zynq.cpu", problems[0])

    def test_all_zeroes_from_a_broken_chain_is_a_failure(self) -> None:
        _, problems = openocd.verify_idcodes(
            "Info : JTAG tap: zynq_pl.bs tap/device found: 0x00000000\n"
            "Info : JTAG tap: zynq.cpu tap/device found: 0x00000000\n")
        self.assertEqual(len(problems), 2)


class CommandTests(unittest.TestCase):
    def test_transport_commands_point_openocd_at_the_local_forward(self) -> None:
        self.assertEqual(openocd.transport_commands(4444), [
            "adapter driver remote_bitbang",
            "remote_bitbang host 127.0.0.1",
            "remote_bitbang port 4444",
        ])
        # OpenOCD's remote_bitbang driver has no configurable speed, so no adapter speed is set
        self.assertNotIn("adapter speed", " ".join(openocd.transport_commands(4444)))

    def test_scan_commands_declare_both_taps_then_only_interrogate(self) -> None:
        commands = openocd.scan_commands()
        self.assertIn("jtag newtap zynq_pl bs -irlen 6 -expected-id 0x03722093 "
                      "-ignore-version -ircapture 0x1 -irmask 0x03", commands)
        self.assertIn("jtag newtap zynq cpu -irlen 4 -expected-id 0x4ba00477 -ircapture 0x1 -irmask 0xf", commands)
        self.assertEqual(commands[-3:], ["init", "scan_chain", "shutdown"])
        # nothing that could program, reset or debug the target
        joined = " ".join(commands)
        for forbidden in ("reset", "program", "load_image", "flash", "target create", "dap create", "cortex_a"):
            self.assertNotIn(forbidden, joined)

    def test_service_command_is_quoted_and_supervised(self) -> None:
        argv = openocd.service_argv(NODE, "gusta", "/usr/local/libexec/ebaz-xvc-server", 4444)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        # ssh joins trailing arguments with spaces without escaping, so the whole remote command must
        # already be one quoted argument; parse it the way the remote shell will
        self.assertEqual(argv[-2], "gusta@192.168.0.101")
        words = shlex.split(argv[-1])
        self.assertEqual(words[:2], ["sh", "-c"])
        script = words[2]
        self.assertIn('probe="$1"', script)
        self.assertIn('port="$2"', script)
        self.assertIn("--protocol remote-bitbang", script)
        self.assertIn('--port "$port"', script)
        self.assertIn("trap", script)          # the child is killed when this side closes stdin
        self.assertIn("cat >/dev/null", script)
        self.assertEqual(words[3:], ["piprobe", "/usr/local/libexec/ebaz-xvc-server", "4444"])

    def test_service_command_passes_the_mode_arguments_as_separate_words(self) -> None:
        # the server parses its own argv, so --swd and its value must stay two arguments
        argv = openocd.service_argv(NODE, "gusta", "/usr/local/libexec/ebaz-xvc-server", 4444,
                                    ["--swd", "8,9", "--nrst", "6"])
        words = shlex.split(argv[-1])
        self.assertEqual(words[3:], ["piprobe", "/usr/local/libexec/ebaz-xvc-server", "4444",
                                     "--swd", "8,9", "--nrst", "6"])
        # the positional parameters are shifted out of the way, so "$@" is exactly the mode arguments
        self.assertIn('shift 2', words[2])

    def test_service_command_keeps_a_path_with_spaces_intact(self) -> None:
        awkward = "/home/gusta/odd path/ebaz-xvc-server"
        argv = openocd.service_argv(NODE, None, awkward, 4444)
        words = shlex.split(argv[-1])
        self.assertEqual(words[3:], ["piprobe", awkward, "4444"])   # one argument, not re-split
        self.assertIn("odd path", argv[-1])

    def test_tunnel_commands_forward_only_loopback(self) -> None:
        argv = openocd.tunnel_argv(NODE, "gusta", 4444, 4444)
        self.assertIn("-N", argv)
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertEqual(argv[argv.index("-L") + 1], "4444:127.0.0.1:4444")
        self.assertNotIn("0.0.0.0", " ".join(argv))

    def test_custom_ssh_port_is_used_by_both_children(self) -> None:
        node = openocd.Node(name="peer", host="10.0.0.9", port=2222, source="peer-config")
        self.assertEqual(openocd.service_argv(node, None, "/srv/server", 4444)[
            openocd.service_argv(node, None, "/srv/server", 4444).index("-p") + 1], "2222")
        self.assertIn("2222", openocd.tunnel_argv(node, None, 4444, 4444))

    def test_openocd_argv_orders_transport_then_scripts_then_caller_commands(self) -> None:
        captured: list[list[str]] = []

        def fake_child(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
            captured.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(openocd, "run_child", side_effect=fake_child):
            exit_code, _ = openocd.run_openocd(
                "openocd", ["adapter driver remote_bitbang"], ["board.cfg"], False,
                post_commands=["init", "halt", "shutdown"])
        self.assertEqual(exit_code, 0)
        argv = captured[0]
        self.assertEqual(argv[0], "openocd")
        self.assertEqual(argv[1:3], ["-c", "adapter driver remote_bitbang"])
        self.assertEqual(argv[3:5], ["-f", "board.cfg"])
        self.assertEqual(argv[5:], ["-c", "init", "-c", "halt", "-c", "shutdown"])


class LockTests(unittest.TestCase):
    def test_lock_excludes_a_second_process_and_is_reusable_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "openocd.lock"
            lock = openocd.SessionLock(path)
            self.assertTrue(lock.acquire())
            try:
                child = subprocess.run(
                    [sys.executable, "-c",
                     "import pathlib, sys; from piprobe import openocd;"
                     "print('acquired' if openocd.SessionLock(pathlib.Path(sys.argv[1])).acquire() else 'refused')",
                     str(path)],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(child.stdout.strip(), "refused", child.stderr)
            finally:
                lock.release()
            again = openocd.SessionLock(path)
            self.assertTrue(again.acquire())
            again.release()

    def test_a_refused_session_can_still_name_the_holder(self) -> None:
        """A Windows byte-range lock blocks reads of the locked range, so the holder text must not live there."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "openocd.lock"
            lock = openocd.SessionLock(path)
            self.assertTrue(lock.acquire())
            try:
                child = subprocess.run(
                    [sys.executable, "-c",
                     "import pathlib, sys; from piprobe import openocd;"
                     "second = openocd.SessionLock(pathlib.Path(sys.argv[1]));"
                     "second.acquire(); print('holder=' + second.holder())",
                     str(path)],
                    capture_output=True, text=True, timeout=120)
                self.assertIn(f"pid {os.getpid()}", child.stdout, child.stderr)
            finally:
                lock.release()


class NodeResolutionTests(unittest.TestCase):
    def row(self, name: str, host: str, caps: list[str] | None = None, source: str = "mdns",
            port: int = 22, node_id: str = "") -> dict[str, object]:
        return {"name": name, "host": f"{host}.local.", "port": port, "addresses": [host],
                "capabilities": caps if caps is not None else ["jtag"], "source": source, "node_id": node_id}

    def resolve(self, rows: list[dict[str, object]], requested: str | None = None,
                peers: list[dict[str, object]] | None = None) -> openocd.Node:
        with patch.object(openocd.discover, "discover_mdns", return_value=rows), \
             patch.object(openocd.discover, "load_peer_candidates", return_value=peers or []):
            return openocd.resolve_node(requested, None, 0.1)

    def test_single_jtag_node_is_selected_automatically(self) -> None:
        node = self.resolve([self.row("PiProbe-5ef16103", "192.168.0.101", node_id="5ef16103-aaaa")])
        self.assertEqual((node.name, node.host, node.port), ("PiProbe-5ef16103", "192.168.0.101", 22))

    def test_several_nodes_require_an_explicit_choice(self) -> None:
        rows = [self.row("PiProbe-a", "10.0.0.5"), self.row("PiProbe-b", "10.0.0.6")]
        with self.assertRaises(openocd.UsageError) as caught:
            self.resolve(rows)
        self.assertIn("several", str(caught.exception))

    def test_no_node_is_an_error_not_a_guess(self) -> None:
        with self.assertRaises(openocd.UsageError):
            self.resolve([])

    def test_nodes_without_the_jtag_capability_are_not_used(self) -> None:
        with self.assertRaises(openocd.UsageError):
            self.resolve([self.row("PiProbe-storage", "10.0.0.7", caps=["uart"])])

    def test_explicit_request_matches_name_id_host_and_address(self) -> None:
        row = self.row("PiProbe-5ef16103", "192.168.0.101", node_id="5ef16103-aaaa")
        for request in ("PiProbe-5ef16103", "5ef16103-aaaa", "192.168.0.101", "gusta-bpi"):
            rows = [row] if request != "gusta-bpi" else [self.row("gusta-bpi", "192.168.0.101")]
            self.assertEqual(self.resolve(rows, request).host, "192.168.0.101")

    def test_one_machine_found_twice_answers_to_either_name(self) -> None:
        peer = {"name": "gusta-bpi", "host": "gusta-bpi", "port": 22, "transport": "ssh", "capabilities": [],
                "addresses": [], "source": "peer-config", "node_id": ""}
        mdns = {"name": "PiProbe-5ef16103", "host": "gusta-bpi.local.", "port": 22, "transport": "ssh",
                "capabilities": ["jtag"], "addresses": ["192.168.0.101"], "source": "mdns",
                "node_id": "5ef16103-aaaa"}
        for request in ("gusta-bpi", "PiProbe-5ef16103", "192.168.0.101", "5ef16103-aaaa"):
            node = self.resolve([mdns], request, [peer])
            self.assertEqual(node.name, "PiProbe-5ef16103", request)
            self.assertEqual(node.host, "192.168.0.101", request)

    def test_ambiguity_counts_nodes_not_discovery_paths(self) -> None:
        peer = {"name": "gusta-bpi", "host": "gusta-bpi", "port": 22, "transport": "ssh", "capabilities": [],
                "addresses": [], "source": "peer-config", "node_id": ""}
        rows = [{"name": "PiProbe-5ef16103", "host": "gusta-bpi.local.", "port": 22, "transport": "ssh",
                 "capabilities": ["jtag"], "addresses": ["192.168.0.101"], "source": "mdns", "node_id": "5ef16103"},
                {"name": "PiProbe-dc4817ec", "host": "gusta-rpi.local.", "port": 22, "transport": "ssh",
                 "capabilities": ["jtag"], "addresses": ["192.168.0.69"], "source": "mdns", "node_id": "dc4817ec"}]
        with self.assertRaises(openocd.UsageError) as caught:
            self.resolve(rows, None, [peer])
        message = str(caught.exception)
        self.assertIn("PiProbe-5ef16103", message)
        self.assertIn("PiProbe-dc4817ec", message)
        self.assertNotIn("gusta-bpi", message)      # merged into the record it duplicates

    def test_a_local_alias_names_the_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "aliases.json"
            path.write_text(json.dumps({"5ef16103-0cba-4df1-8af4-ddf366da7387": "banana-pi"}), encoding="utf-8")
            row = {"name": "PiProbe-5ef16103", "host": "gusta-bpi.local.", "port": 22, "transport": "ssh",
                   "capabilities": ["jtag"], "addresses": ["192.168.0.101"], "source": "mdns",
                   "node_id": "5ef16103-0cba-4df1-8af4-ddf366da7387"}
            with patch.object(openocd.discover, "default_aliases_path", return_value=path):
                self.assertEqual(self.resolve([row], "banana-pi").name, "banana-pi")
                self.assertEqual(self.resolve([row], "PiProbe-5ef16103").name, "banana-pi")
                self.assertEqual(self.resolve([row], "banana-pi").host, "192.168.0.101")

    def test_an_invalid_aliases_file_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "aliases.json"
            path.write_text(json.dumps({"PiProbe-5ef16103": "not a name"}), encoding="utf-8")
            with patch.object(openocd.discover, "default_aliases_path", return_value=path):
                with self.assertRaises(openocd.UsageError):
                    self.resolve([self.row("PiProbe-5ef16103", "192.168.0.101")])

    def test_a_label_alone_finds_the_advertised_name(self) -> None:
        row = {"name": "PiProbe-banana-pi", "host": "gusta-bpi.local.", "port": 22, "transport": "ssh",
               "capabilities": ["jtag"], "addresses": ["192.168.0.101"], "source": "mdns", "node_id": "5ef16103"}
        for request in ("banana-pi", "PiProbe-banana-pi", "BANANA-PI", "192.168.0.101", "gusta-bpi"):
            self.assertEqual(self.resolve([row], request).name, "PiProbe-banana-pi", request)
        # a node whose advertised name carries no prefix is still reachable by that name
        plain = dict(row, name="gusta-bpi", node_id="")
        self.assertEqual(self.resolve([plain], "gusta-bpi").name, "gusta-bpi")

    def test_a_custom_peers_file_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "peers.json"
            path.write_text(json.dumps({"peers": [{"name": "by-ip", "host": "192.168.0.101", "port": 22,
                                                   "transport": "ssh",
                                                   "identity": "ssh-ed25519 SHA256:abc"}]}), encoding="utf-8")
            with patch.object(openocd.discover, "discover_mdns", return_value=[]), \
                 patch.object(openocd.discover, "load_aliases", return_value={}):
                node = openocd.resolve_node("by-ip", None, 0.1, peers_path=path)
                self.assertEqual((node.name, node.host, node.port), ("by-ip", "192.168.0.101", 22))

    def test_a_forward_that_died_is_reported_as_an_interrupted_transport(self) -> None:
        node = openocd.Node(name="n", host="192.168.0.101", port=22, source="mdns")
        live = openocd.Tunnel(node, None, 4444, 4444, verbose=False)
        live._proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertFalse(live.finished_early(), "a running forward is not an interruption")
        finally:
            live.stop()
        dead = openocd.Tunnel(node, None, 4444, 4444, verbose=False)
        dead._proc = subprocess.Popen([sys.executable, "-c", "pass"])
        dead._proc.wait(timeout=30)
        self.assertTrue(dead.finished_early(), "a dead forward must surface as an interruption")
        dead._proc = None

    def test_candidates_put_the_advertised_name_first_then_each_address(self) -> None:
        row = {"name": "PiProbe-banana-pi", "host": "gusta-bpi.local.", "port": 22, "transport": "ssh",
               "capabilities": ["jtag"], "addresses": ["192.0.2.9", "192.168.0.101"],
               "source": "mdns", "node_id": "5ef16103"}
        with patch.object(openocd.discover, "discover_mdns", return_value=[row]), \
             patch.object(openocd.discover, "load_peer_candidates", return_value=[]), \
             patch.object(openocd.discover, "load_aliases", return_value={}):
            node = openocd.resolve_node(None, None, 0.1)
        self.assertEqual(node.candidates, ["gusta-bpi.local", "192.0.2.9", "192.168.0.101"])
        self.assertEqual(node.host, "192.0.2.9")                # reported endpoint until a dial wins
        self.assertEqual(node.alternatives, ["gusta-bpi.local", "192.168.0.101"])

    def test_choose_identity_skips_a_dead_candidate(self) -> None:
        node = openocd.Node(name="n", host="dead.local", port=22, source="mdns",
                            candidates=["dead.local", "192.0.2.9", "192.168.0.101"])
        calls: list[str] = []

        def fake(argv, **kwargs):
            host = argv[-2]
            calls.append(host)
            return subprocess.CompletedProcess(argv, 0 if host == "192.168.0.101" else 255,
                                               "", "ssh: connect to host %s port 22: Connection timed out" % host)

        with patch.object(openocd.subprocess, "run", side_effect=fake):
            self.assertEqual(openocd.choose_identity(node, None, verbose=False), "192.168.0.101")
        self.assertEqual(calls, ["dead.local", "192.0.2.9", "192.168.0.101"])

    def test_choose_identity_reports_every_attempt_when_none_answer(self) -> None:
        node = openocd.Node(name="ghost", host="192.0.2.1", port=22, source="peer-config",
                            candidates=["192.0.2.1"])
        with patch.object(openocd.subprocess, "run",
                          side_effect=lambda argv, **kw: subprocess.CompletedProcess(argv, 255, "", "refused")):
            with self.assertRaises(openocd.TransportError) as caught:
                openocd.choose_identity(node, None, verbose=False)
        self.assertIn("192.0.2.1", str(caught.exception))
        self.assertIn("refused", str(caught.exception))

    def test_a_single_candidate_is_still_verified(self) -> None:
        node = openocd.Node(name="by-ip", host="192.168.0.101", port=22, source="peer-config",
                            candidates=["192.168.0.101"])
        with patch.object(openocd.subprocess, "run",
                          side_effect=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")) as run:
            self.assertEqual(openocd.choose_identity(node, "gusta", verbose=False), "192.168.0.101")
        self.assertEqual(run.call_args.args[0][-2:], ["gusta@192.168.0.101", "true"])

    def test_child_output_that_is_not_valid_utf8_is_read_instead_of_raising(self) -> None:
        """OpenOCD prints localized text on Windows, which is not UTF-8; a decode error used to abort
        the read and hide the failure that mattered."""
        completed = openocd.run_child([sys.executable, "-c",
                                       r"import sys; sys.stdout.buffer.write(b'ok \xe7 fail\n')"])
        self.assertEqual(completed.returncode, 0)
        self.assertIn("ok", completed.stdout)
        self.assertIn("fail", completed.stdout, "the tail of the line must survive the decode")

    def test_the_socket_buffer_failure_of_a_bulk_payload_is_explained(self) -> None:
        """A 2 MB bitstream over remote_bitbang fills the client's socket; the tool must say so."""
        sample = ("Error: Error on socket 'remote_bitbang_putc': WSAGetLastError==10035, message: "
                  "a non-blocking socket operation could not be completed immediately\nError: failed: -4\n")
        trouble = openocd.bulk_transport_trouble(sample)
        self.assertIsNotNone(trouble)
        self.assertIn("piprobe program", trouble)
        self.assertIsNone(openocd.bulk_transport_trouble("Info : JTAG tap: zynq.cpu tap/device found: 0x4ba00477\n"))

    def test_unknown_request_lists_what_was_found(self) -> None:
        with self.assertRaises(openocd.UsageError) as caught:
            self.resolve([self.row("PiProbe-5ef16103", "192.168.0.101")], "nope")
        self.assertIn("PiProbe-5ef16103", str(caught.exception))

    def test_pinned_peer_entry_is_usable_without_mdns(self) -> None:
        peer = {"name": "gusta-bpi", "host": "gusta-bpi", "port": 22, "transport": "ssh",
                "capabilities": [], "addresses": [], "source": "peer-config", "node_id": ""}
        node = self.resolve([], None, [peer])
        self.assertEqual(node.host, "gusta-bpi")
        self.assertEqual(node.source, "peer-config")

    def test_an_unusable_mdns_record_alone_is_not_selected(self) -> None:
        with self.assertRaises(openocd.UsageError):
            self.resolve([self.row("PiProbe-unknown", "", caps=["jtag"])])


class MainTests(unittest.TestCase):
    def test_dry_run_plans_the_session_without_touching_the_probe(self) -> None:
        rows = [{"name": "PiProbe-5ef16103", "host": "gusta-bpi.local.", "port": 22, "addresses": ["192.168.0.101"],
                 "capabilities": ["jtag"], "source": "mdns", "node_id": "5ef16103-aaaa"}]
        with patch.object(openocd.discover, "discover_mdns", return_value=rows), \
             patch.object(openocd.discover, "load_peer_candidates", return_value=[]), \
             patch.object(openocd, "find_openocd", return_value="openocd"), \
             patch.object(openocd.subprocess, "Popen") as popen, \
             patch.object(openocd.subprocess, "run") as run:
            self.assertEqual(openocd.main(["--dry-run"]), openocd.EXIT_OK)
            popen.assert_not_called()
            run.assert_not_called()

    def test_missing_openocd_is_a_usage_error(self) -> None:
        with patch.object(openocd, "find_openocd", side_effect=openocd.UsageError("no openocd")):
            self.assertEqual(openocd.main([]), openocd.EXIT_USAGE)

    def test_bad_ports_are_refused_before_any_session(self) -> None:
        self.assertEqual(openocd.main(["--rbb-port", "70000"]), openocd.EXIT_USAGE)
        self.assertEqual(openocd.main(["--local-port", "0"]), openocd.EXIT_USAGE)

    def test_swd_is_refused_before_any_session_when_it_cannot_work(self) -> None:
        # the built-in scan interrogates the EBAZ4205 JTAG chain, which an SWD port cannot answer
        self.assertEqual(openocd.main(["--swd", "8,9"]), openocd.EXIT_USAGE)
        self.assertEqual(openocd.main(["--swd", "8", "-f", "board.cfg"]), openocd.EXIT_USAGE)
        self.assertEqual(openocd.main(["--swd", "8,999", "-f", "board.cfg"]), openocd.EXIT_USAGE)
        self.assertEqual(openocd.main(["--swd", "eight,9", "-f", "board.cfg"]), openocd.EXIT_USAGE)
        self.assertEqual(openocd.main(["--nrst", "999", "-f", "board.cfg"]), openocd.EXIT_USAGE)

    def test_script_mode_keeps_the_transport_but_not_the_builtin_scan(self) -> None:
        rows = [{"name": "PiProbe-5ef16103", "host": "gusta-bpi.local.", "port": 22, "addresses": ["192.168.0.101"],
                 "capabilities": ["jtag"], "source": "mdns", "node_id": "5ef16103-aaaa"}]
        captured: dict[str, list[str]] = {}

        def fake_run(binary: str, commands: list[str], scripts: list[str], verbose: bool,
                     post_commands: tuple[str, ...] = ()) -> tuple[int, str]:
            captured["commands"], captured["scripts"] = commands, scripts
            return 0, ""

        with patch.object(openocd.discover, "discover_mdns", return_value=rows), \
             patch.object(openocd.discover, "load_peer_candidates", return_value=[]), \
             patch.object(openocd, "find_openocd", return_value="openocd"), \
             patch.object(openocd, "run_openocd", side_effect=fake_run), \
             patch.object(openocd.RemoteService, "start"), \
             patch.object(openocd.RemoteService, "stop"), \
             patch.object(openocd.Tunnel, "start"), \
             patch.object(openocd.Tunnel, "stop"), \
             patch.object(openocd, "check_cleanup", return_value="checked"), \
             patch.object(openocd.SessionLock, "acquire", return_value=True), \
             patch.object(openocd.SessionLock, "release"):
            self.assertEqual(openocd.main(["-f", "board.cfg"]), openocd.EXIT_OK)
        self.assertEqual(captured["scripts"], ["board.cfg"])
        self.assertIn("adapter driver remote_bitbang", captured["commands"])
        self.assertNotIn("scan_chain", " ".join(captured["commands"]))

    def test_swd_session_reaches_the_service_and_the_caller_commands(self) -> None:
        rows = [{"name": "PiProbe-5ef16103", "host": "gusta-bpi.local.", "port": 22, "addresses": ["192.168.0.101"],
                 "capabilities": ["jtag", "swd"], "source": "mdns", "node_id": "5ef16103-aaaa"}]
        seen: dict[str, list[str]] = {}

        class FakeService:
            def __init__(self, node: object, user: object, server_path: object, port: object,
                         verbose: object, mode_args: object = ()) -> None:
                seen["mode_args"] = list(mode_args)  # type: ignore[arg-type]
                seen["server_path"] = [str(server_path)]
                self.lines: list[str] = ["remote-bitbang-server: listening on 127.0.0.1:4444"]

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def _tail(self, limit: int = 6) -> str:
                return " | ".join(self.lines[-limit:])

        def fake_run(binary: str, commands: list[str], scripts: list[str], verbose: bool,
                     post_commands: tuple[str, ...] = ()) -> tuple[int, str]:
            seen["commands"], seen["scripts"] = list(commands), list(scripts)
            seen["post"] = list(post_commands)
            return 0, ""

        with patch.object(openocd.discover, "discover_mdns", return_value=rows), \
             patch.object(openocd.discover, "load_peer_candidates", return_value=[]), \
             patch.object(openocd, "find_openocd", return_value="openocd"), \
             patch.object(openocd, "RemoteService", FakeService), \
             patch.object(openocd, "run_openocd", side_effect=fake_run), \
             patch.object(openocd.Tunnel, "start"), \
             patch.object(openocd.Tunnel, "stop"), \
             patch.object(openocd, "check_cleanup", return_value="checked"), \
             patch.object(openocd.SessionLock, "acquire", return_value=True), \
             patch.object(openocd.SessionLock, "release"):
            self.assertEqual(openocd.main(["--swd", "8,9", "--nrst", "6", "-f", "stm32-swd.cfg",
                                           "-c", "init; halt; reg pc; resume; shutdown"]), openocd.EXIT_OK)
        self.assertEqual(seen["mode_args"], ["--swd", "8,9", "--nrst", "6"])
        self.assertEqual(seen["scripts"], ["stm32-swd.cfg"])
        self.assertEqual(seen["post"], ["init; halt; reg pc; resume; shutdown"])
        self.assertIn("adapter driver remote_bitbang", seen["commands"])


if __name__ == "__main__":
    unittest.main()
