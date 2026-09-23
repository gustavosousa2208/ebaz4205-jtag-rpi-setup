# Probe package hardening protocol

Track execution in Bead `ebaz4205-71t.2`; this document defines the repeatable
cases and records observed results. Do not run XVC and the production OpenOCD
uploader at the same time: both own the physical JTAG pins.

## Test environment

- Canonical package: `~/ebaz4205-jtag`; UART recovery hardening tested at
  `12453cb`
- Banana Pi after reboot: Armbian 26.11.0-trunk.57 (Debian 13 trixie), Linux
  `6.18.53-current-sunxi` on armv7l
- Desktop: Windows 11 Pro build 26200, PowerShell 7.6.6, Windows OpenSSH
- OpenOCD: `0.12.0+dev-gb04ccfe-dirty`
- Vivado: v2025.2, SW Build 6299465, at
  `D:\AMDDesignTools\2025.2\Vivado`; bundled `hw_server.exe` runs from its
  `bin\unwrapped\win64.o` directory and listens on localhost TCP 3121.
- UART service: `~/ebaz4205-jtag/uart/uart-bridge.py`; replay TCP 2217, live TCP 2218
- UART cron tag: `ebaz4205-jtag:uart-bridge`
- OpenOCD source/build: `~/source/openocd-ebaz`
- XVC source: `~/ebaz4205-jtag/jtag/xvc/`
- Preserve OpenOCD's pre-existing `src/jtag/drivers/linuxgpiod.c` modification and
  untracked `doc/openocd.info-3` file in every case.

## Cases and results

### 1. UART cron ownership, reboot, and recovery

1. Record `crontab -l`, `uart/pi-cron.sh status`, service PID, and listeners.
2. Run `uart/pi-cron.sh remove`; verify only the tagged line is removed and
   unrelated cron entries are byte-for-byte unchanged. Re-add with
   `uart/pi-cron.sh add`; verify exactly one tagged line.
3. Reboot the Pi. Verify after boot that exactly one bridge process is running,
   ports 2217 and 2218 are listening, and the cron line still targets
   `~/ebaz4205-jtag/uart`.
4. Connect and disconnect clients on both ports; verify the service remains up.

**Status:** the live crontab had only the project line; removing it left no
entries and adding restored exactly one. A synthetic crontab with unrelated
entries proved remove/add preserves them and repeated add is idempotent. The
script also replaces the former `link-test:uart-bridge` tag. Clients connected
and disconnected on both ports. I replayed the exact delayed `@reboot` command
body: after its 20-second delay exactly one supervisor and bridge appeared,
and both ports listened. After the user rebooted for the kernel update, boot
time was 2026-09-23 17:14:43 UTC and the kernel is
`6.18.53-current-sunxi`. The tagged cron entry survived; exactly one supervisor
and bridge returned, and both listeners (2217/2218) are active. Two physical
IDCODE scans passed at 100 kHz after reboot and reported `restored=yes`.

### 2. UART service failure behavior

Use alternate TCP ports so production listeners stay available. The repeatable
Linux PTY suite is `python3 -m unittest -v uart/test_uart_bridge.py`. Record
exit codes, logs, and whether any listener leaks remain.

- Missing serial device: start with `--device /dev/does-not-exist` and alternate
  ports; expect a clear failure and no leftover listener.
- Port collision: occupy one alternate port, start the bridge, and verify clean
  failure with no listener left on the other port.
- Read-only contract: send client bytes to the default bridge and confirm they
  are discarded; confirm only explicit `--allow-write` enables serial writes.
- Serial removal/recovery: the bridge retries reopening after `EIO`/`ENODEV`,
  including if the path is temporarily absent. Unit-test transient reopen
  failures; perform a real unplug/replug check only with an isolated UART.
  Closing a PTY master did not trigger the same error signal on this kernel, so
  it is not evidence of physical UART unplug behavior. Do not detach production
  `/dev/ttyS3`.
- Client disconnect: disconnect replay and live clients during traffic and
  confirm the service continues accepting clients.
- Supervisor stop: send `SIGTERM` to the supervisor with a PTY bridge active;
  verify it also terminates the child and releases both listeners.
- Device return: start the supervisor while the serial path is missing, make a
  PTY appear at that path, and verify the bridge starts and delivers live data.

**Status:** Linux PTY suite passed 9/9 on gusta-bpi. It uses alternate ports and
PTYs and covers startup failure, listener cleanup, port collision, replay/live,
read-only default, explicit write mode, duplicate serial-owner rejection,
client disconnect, serial-open retry, supervisor retry, graceful supervisor
termination, and recovery when the serial path becomes available. A live PTY
experiment first reproduced a shutdown defect: signaling
the supervisor left its bridge child alive and both ports bound. `run-bridge.sh`
now forwards `SIGTERM` to the child and waits; the regression test confirms
supervisor exit and successful rebinding of both ports. The expanded suite
passed 10/10 on gusta-bpi. On macOS, 9 tests pass and the Linux-specific
`TIOCEXCL` ownership test is skipped. A closed PTY master did not generate `EIO` on this
kernel; that setup cannot represent physical UART removal. The Pi's onboard UART
controller remains present when its signal wire is unplugged. After deploying
the fix, restarted the live service and verified one supervisor, one bridge,
and both listeners on ports 2217/2218. The recovery test creates the missing
PTY path after repeated startup failures, then verifies bridge startup, live
data delivery, and continued supervisor operation. After the kernel update and
reboot, the same Linux PTY suite passed 10/10 on `gusta-bpi`.

### 3. XVC protocol, Vivado, ownership handoff, and OpenOCD recovery

- Run `jtag/xvc/test_xvc.py` against the XVC server in `--fake` mode. This
  covers protocol framing, fragmented requests, varied/non-byte-aligned scans,
  and maximum vector size without hardware.
- On the Pi, connect Vivado Hardware Manager from `gusta-desktop`, identify both
  Zynq TAPs, then disconnect XVC before starting production OpenOCD.
- Upload/probe through OpenOCD after XVC exits. Repeat in the opposite order:
  finish OpenOCD, start XVC, connect/disconnect, stop XVC.
- Record Vivado/hw_server versions, LAN addresses, exact connect commands,
  XVC port/firewall state, chain IDs, and whether XVC releases GPIO ownership.

Exact real-client check used after the Pi reboot:

```powershell
# gusta-bpi
sudo -n /usr/local/libexec/ebaz-xvc-server --port 2542 --rate-khz 1000

# gusta-desktop PowerShell, in a second terminal
ssh -N -L 127.0.0.1:2543:127.0.0.1:2542 gusta-bpi

# Vivado v2025.2 PowerShell
& 'D:\AMDDesignTools\2025.2\Vivado\bin\vivado.bat' -mode batch `
  -source "$env:TEMP\ebaz-check-vivado-xvc.tcl" `
  -tclargs 127.0.0.1:2543 -nojournal -nolog
```

Copy the checked-in `jtag/xvc/check-vivado-xvc.tcl` to the desktop temp path
before running this sequence. The XVC server must be stopped cleanly before
starting OpenOCD.

**Status:** fake XVC protocol suite passed 32/32 on gusta-bpi. The production
XVC binary was verified to match the compiled package source. Physical XVC
IDCODE scan passed 5/5 (`0x13722093`, `0x4ba00477`); after gracefully stopping
XVC, canonical `make probe` found both TAPs and examined both Cortex-A9 cores.
The reverse sequence also passed: OpenOCD probe first, then XVC IDCODE scans
passed 3/3. Each handoff stopped the current owner before starting the other.
With OpenOCD intentionally holding the GPIO lines, a concurrent XVC start
failed at the kernel line request with `Device or resource busy` (exit 2),
before XVC touched the pins. Once OpenOCD exited, XVC reacquired the pins and
IDCODE passed 2/2; after stopping XVC, canonical OpenOCD `make probe` passed
again. Fake XVC also passed end-to-end through the documented SSH local-forward
started from Windows PowerShell; the desktop received `xvcServer_v1.0:32768`
from the Pi's loopback-only server.
Vivado v2025.2 batch mode passed through a Windows SSH tunnel to the Pi XVC
server. Tcl `open_hw_target -xvc_url 127.0.0.1:2543` discovered `arm_dap_0`
(`0x4ba00477`) and `xc7z010_1` (`0x13722093`); checked-in read-only script
`jtag/xvc/check-vivado-xvc.tcl` repeats this check and passed with exit 0. The
same Vivado session programmed the known-good shared-drive bitstream
`D:\SHARED-DATA\FPGA\personal\real-projects\ebaz4205-jtag-rpi-setup\hardware\ebaz_test.bit`
(2,083,870 bytes, SHA-256
`99e1416352c7f1778a8576ba0c1ce8980a61ac835b973710bc745f1da9ba3132`, target
`7z010clg400`). `program_hw_devices` exited 0 and reported `End of startup
status: HIGH`; `refresh_hw_device` confirmed the programmed `xc7z010`.

Cleanup is still pending: Vivado closed its target, but the root XVC server
remains active as PID 1862 on the Pi and owns the GPIO lines. A noninteractive
`sudo -n /bin/kill -TERM 1862` was denied because it requires a password. Do
not start OpenOCD until the server receives SIGTERM interactively and logs
`stopped, pins restored`; then run `make probe` to verify post-program recovery.

AMD documents both the Hardware Manager XVC connection flow and Tcl
`open_hw_target -xvc_url`; our server binds only to Pi localhost, so the remote
desktop uses SSH local forwarding. See [AMD PG195](https://docs.amd.com/r/en-US/pg195-pcie-dma/Connecting-the-Vivado-Design-Suite-to-the-XVC-Server-Application)
and [UG835](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/open_hw_target).

### 4. OpenOCD source move and installer lookup

- Verify `~/source/openocd-ebaz/src/openocd` is executable and the pre-existing
  dirty/untracked files remain present.
- Inspect the installer lookup in `jtag/install-mmio-runner`; it must resolve
  the source binary from `$runner_home/source/openocd-ebaz/src/openocd`.
- Verify installed `/usr/local/libexec/openocd-ebaz-mmio` remains executable.
- Exercise `sudo ./jtag/install-mmio-runner` only during a controlled window;
  then verify the installed binary and `sudo -n` runner rule. Do not rebuild or
  overwrite the moved checkout as part of this path check.

**Status:** `~/source/openocd-ebaz/src/openocd` is executable, the original
modified/untracked source files remain unchanged, and the installed OpenOCD
binary completed a canonical physical probe after the move. Installer source
lookup now points to the moved checkout. The source and installed OpenOCD
binaries have identical SHA-256
`c2370ab9af349bcb2019eec3472387a6a5a0291e8ee861948517f60a2d89664a`; the
installed passwordless IDCODE runner passed a live one-scan check of both TAPs
at 100 kHz and reported `restored=yes`. `sudo -n -l` confirms the two installed
runners are authorized. Running the privileged installer itself remains
unverified because it requires interactive sudo authentication.

### 5. Fresh-agent discoverability

Starting at the repository root, follow only `README.md` and its links. Verify a
new agent can locate: production JTAG commands, UART service/setup/status and
remove/re-add commands, XVC build/install instructions, the simultaneous-owner
warning, lab archive location, Beads Vivado task, and the OpenOCD source path.

**Status:** README links to the UART guide, XVC guide, and this test matrix;
the guides expose the production commands, cron status/remove/add commands,
source path, one-owner warning, and related Beads. Manual README link walk
passed. See Bead `ebaz4205-71t.1` for Vivado compatibility.
