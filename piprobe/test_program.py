"""Tests for the program subcommand: command construction, verdict parsing, and the shipping path."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from piprobe import openocd, program

NODE = openocd.Node(name="PiProbe-banana-pi", host="192.168.0.101", port=22, source="mdns",
                    candidates=["192.168.0.101"])

# trimmed from the real runner output captured on 2026-10-10
PASS_LOG = """time=2026-10-10T16:53:00+0000
adapter=bananapi-m2-zero-mmio
PL attempt=1/3 backend=bananapi-m2-zero-mmio requested_khz=1000
Preparing Zynq PL configuration (PCFG_PROG_B high-low-high)...
PL configuration verified: PCFG_DONE=1
PL attempt=1 result=pass category=none requested_khz=1000 elapsed_ms=18582
exit_status=0
"""
FAIL_LOG = """adapter=bananapi-m2-zero-mmio
PL attempt=1 result=fail category=transport requested_khz=1000 elapsed_ms=16540 error=failed loading file
exit_status=1
"""


class VerdictTests(unittest.TestCase):
    def test_a_pass_is_read_from_the_runners_own_verdict(self) -> None:
        verdict, evidence, code = program.parse_verdict(PASS_LOG)
        self.assertEqual(verdict, "pass")
        self.assertEqual(code, 0)
        self.assertTrue(any("PCFG_DONE=1" in line for line in evidence))
        self.assertTrue(any("result=pass" in line for line in evidence))

    def test_a_failure_keeps_the_reason_and_the_category(self) -> None:
        verdict, evidence, code = program.parse_verdict(FAIL_LOG)
        self.assertEqual(verdict, "fail")
        self.assertEqual(code, 1)
        self.assertTrue(any("category=transport" in line for line in evidence))

    def test_output_without_a_verdict_but_a_zero_exit_still_passes(self) -> None:
        self.assertEqual(program.parse_verdict("exit_status=0\n")[0], "pass")

    def test_output_without_any_verdict_is_unknown(self) -> None:
        self.assertEqual(program.parse_verdict("nothing here\n")[0], "unknown")


class CommandTests(unittest.TestCase):
    def test_shipping_puts_the_payload_on_stdin_with_a_single_quoted_path(self) -> None:
        argv = program.ship_argv(NODE, "gusta", "~/.cache/piprobe", "~/.cache/piprobe/design.bit")
        self.assertEqual(argv[-2], "gusta@192.168.0.101")
        import shlex
        words = shlex.split(argv[-1])
        self.assertEqual(words[0], "sh")
        self.assertIn("mkdir -p", words[2])
        self.assertIn("cat >", words[2])
        self.assertIn('"$HOME"/.cache/piprobe/design.bit', words[2])
        self.assertNotIn("preserve-env", argv[-1])

    def test_the_runner_command_uses_the_qualified_target_and_adapter(self) -> None:
        argv = program.runner_argv(NODE, None, "~/ebaz4205-jtag", "bananapi-m2-zero-mmio", "~/.cache/piprobe/d.bit")
        script = argv[-1]
        self.assertIn('cd "$HOME"/ebaz4205-jtag', script)
        self.assertIn("EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio", script)
        self.assertIn("make bitstream", script)
        self.assertIn('UPLOAD_BITSTREAM="$HOME"/.cache/piprobe/d.bit', script)

    def test_a_leading_tilde_stays_expandable(self) -> None:
        """shlex.quote("~/x") is "'~/x'", and a quoted tilde never expands -- cd would fail on the probe."""
        self.assertEqual(program.remote_quote("~/ebaz4205-jtag"), '"$HOME"/ebaz4205-jtag')
        self.assertEqual(program.remote_quote("~"), '"$HOME"')
        self.assertNotIn("'~", program.remote_quote("~/with space/dir"))
        self.assertIn("'with space/dir'", program.remote_quote("~/with space/dir"))
        self.assertEqual(program.remote_quote("/opt/ebaz"), "/opt/ebaz")

    def test_a_path_with_spaces_survives_quoting(self) -> None:
        argv = program.runner_argv(NODE, None, "~/odd dir", "bananapi-m2-zero-mmio", "~/odd dir/with space.bit")
        script = argv[-1]
        self.assertIn('"$HOME"/\'odd dir\'', script)
        self.assertIn('"$HOME"/\'odd dir/with space.bit\'', script)
        self.assertNotIn("'~/", script, "a quoted tilde would not expand on the probe")

    def test_verification_and_removal_target_the_shipped_path(self) -> None:
        self.assertIn("sha256sum", program.verify_argv(NODE, None, "~/f.bit")[-1])
        self.assertIn("rm -f", program.remove_argv(NODE, None, "~/f.bit")[-1])


class MainTests(unittest.TestCase):
    def bitstream(self, directory: str, content: bytes = b"bitstream-bytes") -> Path:
        path = Path(directory) / "design.bit"
        path.write_bytes(content)
        return path

    def test_check_ships_verifies_and_stops_without_programming(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bit = self.bitstream(temp_dir)
            digest = program.sha256_file(bit)
            calls: list[list[str]] = []

            def fake(argv, **kwargs):
                calls.append(list(argv) if isinstance(argv, list) else [argv])
                joined = " ".join(argv) if isinstance(argv, list) else str(argv)
                if "sha256sum" in joined:
                    return subprocess.CompletedProcess(argv, 0, f"{digest}  /remote/design.bit\n", "")
                if "cat >" in joined:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(openocd, "resolve_node", return_value=NODE), \
                 patch.object(openocd, "choose_identity", return_value="192.168.0.101"), \
                 patch.object(program.subprocess, "run", side_effect=fake), \
                 patch.object(openocd, "run_child", side_effect=fake), \
                 patch.object(openocd.SessionLock, "acquire", return_value=True), \
                 patch.object(openocd.SessionLock, "release"):
                self.assertEqual(program.main(["--file", str(bit), "--check"]), openocd.EXIT_OK)

            joined = [" ".join(c) for c in calls]
            self.assertTrue(any("cat >" in j for j in joined), "the file must be shipped")
            self.assertTrue(any("sha256sum" in j for j in joined), "the copy must be verified")
            self.assertFalse(any("make bitstream" in j for j in joined), "--check must not program")

    def test_a_mismatched_remote_digest_stops_before_programming(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bit = self.bitstream(temp_dir)
            calls: list[str] = []

            def fake(argv, **kwargs):
                joined = " ".join(argv) if isinstance(argv, list) else str(argv)
                calls.append(joined)
                if "sha256sum" in joined:
                    return subprocess.CompletedProcess(argv, 0, "deadbeef  /remote/design.bit\n", "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(openocd, "resolve_node", return_value=NODE), \
                 patch.object(openocd, "choose_identity", return_value="192.168.0.101"), \
                 patch.object(program.subprocess, "run", side_effect=fake), \
                 patch.object(openocd, "run_child", side_effect=fake), \
                 patch.object(openocd.SessionLock, "acquire", return_value=True), \
                 patch.object(openocd.SessionLock, "release"):
                rc = program.main(["--file", str(bit)])
            self.assertEqual(rc, openocd.EXIT_TRANSPORT)
            self.assertFalse(any("make bitstream" in c for c in calls), "a bad copy must not be programmed")

    def test_a_missing_bitstream_is_a_usage_error_before_any_session(self) -> None:
        with patch.object(openocd.SessionLock, "acquire") as acquire:
            self.assertEqual(program.main(["--file", "/definitely/not/here.bit"]), openocd.EXIT_USAGE)
            acquire.assert_not_called()

    def test_a_real_run_reports_the_runners_verdict_and_removes_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bit = self.bitstream(temp_dir)
            digest = program.sha256_file(bit)
            calls: list[str] = []

            def fake(argv, **kwargs):
                joined = " ".join(argv) if isinstance(argv, list) else str(argv)
                calls.append(joined)
                if "sha256sum" in joined:
                    return subprocess.CompletedProcess(argv, 0, f"{digest}  /remote/design.bit\n", "")
                if "make bitstream" in joined:
                    return subprocess.CompletedProcess(argv, 0, PASS_LOG, "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(openocd, "resolve_node", return_value=NODE), \
                 patch.object(openocd, "choose_identity", return_value="192.168.0.101"), \
                 patch.object(program.subprocess, "run", side_effect=fake), \
                 patch.object(openocd, "run_child", side_effect=fake), \
                 patch.object(openocd.SessionLock, "acquire", return_value=True), \
                 patch.object(openocd.SessionLock, "release"):
                self.assertEqual(program.main(["--file", str(bit), "--json"]), openocd.EXIT_OK)
            self.assertTrue(any("rm -f" in c for c in calls), "the temp copy is removed on success")
            self.assertTrue(any("make bitstream" in c for c in calls))


if __name__ == "__main__":
    unittest.main()
