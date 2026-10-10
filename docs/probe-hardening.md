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

### 2a. UART capture during an upload

The first canonical upload after the kernel reboot completed in 34.285 s but
captured zero UART bytes. The upload helper tried to open `/dev/ttyS3` directly
while the reboot-started bridge already held it with `TIOCEXCL`; a second open
was reproduced as `EBUSY`. Banana Pi uploads now subscribe to the bridge's live
TCP endpoint (`127.0.0.1:2218`) before programming. The first TCP attempt still
captured zero bytes because the helper treated a normal 200 ms read timeout as
end-of-stream and exited during bitstream staging. The helper now waits through
timeouts and exits only on an actual socket close.

**Status:** on 2026-09-23,
`EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio EBAZ_USE_SUDO=0 make upload-pcap UART=1`
completed with `PCFG_DONE=1`, `INIT_B=1`, and the ELF started. It captured
`Hello world from the EBAZ4205 Cortex-A9! heartbeat=0` (54 bytes) from that
same run. PCAP staging took 28.811 s, target CRC 1.422 s, and the OpenOCD flow
reported 34.128 s total. Afterward the UART bridge remained the only UART
owner, with both ports 2217 and 2218 listening; no OpenOCD/XVC process or port
remained.

Correction 2026-10-10: the `EBAZ_USE_SUDO=0` in that command only worked because
the shell was already root. From a normal user shell the root-owned MMIO runner
cannot open `/dev/mem` (`EBAZ MMIO: open /dev/mem: Permission denied`), so the
supported form is now:

```sh
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio make upload-pcap UART=1
```

with the helper defaulting to `EBAZ_USE_SUDO=1` on Linux and handing the
configuration to OpenOCD as `-c "set ::env(...)"` arguments, because a pinned
`NOPASSWD` rule refuses `--preserve-env` unless it also grants `SETENV`. See
Bead `ebaz4205-71t.3`.

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

Post-program ownership handoff passed on 2026-09-23. After the user stopped
XVC PID 1862 with interactive `sudo kill -TERM 1862`, the process disappeared,
TCP 2542 stopped listening, and `/tmp/ebaz-xvc-vivado.log` ended with
`xvc-server: stopped, pins restored`. The canonical post-Vivado command
`cd ~/ebaz4205-jtag && EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio make probe`
passed: PL ID `0x13722093`, ARM ID `0x4ba00477`, and both Cortex-A9 cores
examined successfully. OpenOCD exited cleanly. UART bridge remained healthy
with one supervisor, one bridge, and listeners on TCP 2217/2218. No XVC or
OpenOCD owner remained after the probe. The privileged installer invocation
still requires interactive sudo and remains unverified.

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
at 100 kHz and reported `restored=yes`. On 2026-09-23, the user reran
`sudo ./jtag/install-mmio-runner` interactively. Afterward the installed
IDCODE runner passed two scans at 100 kHz with both TAPs and `restored=yes`;
`sudo -n /usr/local/libexec/openocd-ebaz-mmio -v` also succeeded. The installed
OpenOCD hash still matches the moved source binary, verifying the installer and
both passwordless runner paths.

### 5. Fresh-agent discoverability

Starting at the repository root, follow only `README.md` and its links. Verify a
new agent can locate: production JTAG commands, UART service/setup/status and
remove/re-add commands, XVC build/install instructions, the simultaneous-owner
warning, lab archive location, Beads Vivado task, and the OpenOCD source path.

**Status:** README links to the UART guide, XVC guide, and this test matrix;
the guides expose the production commands, cron status/remove/add commands,
source path, one-owner warning, and related Beads. Manual README link walk
passed. See Bead `ebaz4205-71t.1` for Vivado compatibility.

## PiProbe network path acceptance (2026-10-10)

The probe server now speaks two protocols from one binary: XVC (Vivado) and
OpenOCD's `remote_bitbang`, because no stock Windows OpenOCD build ships the
`xvc` adapter driver. Both stay loopback-only behind the SSH forward, one
process at a time.

**Status:** real-hardware read-only checks passed.

- Host OpenOCD 0.12.0 over the SSH forward, read-only Zynq-7000 TAP scan:
  `zynq_pl.bs 0x13722093` and `zynq.cpu 0x4ba00477`, exit 0, no reset or
  programming issued (`piprobe openocd`, Bead `ebaz4205-3pb.4`).
- XVC unchanged: 5/5 in-probe IDCODE scans and 3/3 scans from the Windows host
  through the tunnel returned `0x4ba00477` + `0x13722093`; the checked-in
  `check-vivado-xvc.tcl` reported
  `VIVADO_XVC_PASS ... devices=arm_dap_0 xc7z010_1` through Vivado v2025.2.
- Single owner: with one session holding the pins, a second server was refused
  (`JTAG pins already in use (pid N protocol remote-bitbang port 4444 in
  /run/ebaz-jtag-pins.lock)`, exit 3) and a second `piprobe openocd` exited 3;
  a held host lock likewise refused a second client. After the session the
  probe had no listener, no server process and a reusable lock.
- Protocol suites: XVC 32/32, remote_bitbang 20/20, SWD 29/29 and — see the
  next section — lifecycle 16/16, all against `--fake` servers started by the
  tests themselves. They cover response framing, one-client-at-a-time, the
  fail-closed refusals (reset asserts on a port with no reset line, TRST
  everywhere, TDO reads on an SWD port, SWD commands on a JTAG port) and, on an
  SWD port, the SWDIO turnaround and the `--nrst` gate. All four pass on the
  Banana Pi (armv7) and on x86_64.

## Service lifecycle hardening (2026-10-10)

Found while driving the SWD path from the host: a server started over a channel
can outlive that channel, and a surviving server holds the pins, so the next
session is refused by the lock while `--stop-holder`-less builds offer no way
out without another password prompt. Two additions in the server:

- If stdin is a pipe or a socket, the server stops when it closes. That is the
  shape a service has when a tool starts it over ssh, so a dropped channel now
  ends the service by itself. Terminal and `/dev/null` stdin are ignored, and a
  script that starts the server should redirect stdin from `/dev/null` rather
  than inheriting a pipe.
- `--stop-holder` reads the pin lock and signals the server named in it, after
  checking that the command line really is a server binary. Exit 0 means the
  pins are free. It is the documented recovery for a server left by an older
  build or a `kill -9`.

Four protocol suites now cover the server in `--fake` mode, all passing on
x86_64 and on the Banana Pi: XVC 32/32, remote_bitbang 20/20, SWD 29/29 and
lifecycle 16/16. The lifecycle suite drives the stdin rule, the second-server
refusal (exit 3) and the reap.

### Flashing (validated 2026-10-10)

Writing works through the same local runner, and it was validated destructively on
the bench H743VIT6, so the recovery path is not theoretical: both flash banks
erased (`erased sectors 0 through 7` per bank, ~7 s each), blankness confirmed by
reading `0x08000000`, `0x08100000` and `0x081ff000` back as `ffffffff`, the 2 MB
snapshot programmed with `program <file> 0x08000000 verify reset`, and then an
independent re-dump whose md5 equals the snapshot's (`799c18e0…`). The board was
confirmed executing afterwards: `pc` moved from `0x08021686` to `0x0803ae80`
across a 300 ms resume.

Two practical notes: the Pi's OpenOCD needs `-s /usr/share/openocd/scripts`
because its scripts directory is not compiled in, and `target/stm32h7x_dual_bank.cfg`
is the config for a 2 MB H743 — the single-bank file would mis-map the upper
megabyte. Program plus verify of 2 MB takes ~150 s at an effective 13.7 KiB/s, far below the requested 1000 kHz, so
the rate deserves a look before bulk work. The runner's own log narrows that down: it reports
`EBAZ MMIO rate selected: requested_khz=4000` while the transfer runs at 13.7 KiB/s, so the limit is
per-transaction overhead in the SWD access path, not the shift clock. Sector erase leaves the option
bytes alone, so a board's boot configuration survives this, and the remote_bitbang network path remains
the wrong transport for bulk payloads.

### Zephyr and RTT (validated 2026-10-10)

The bench H743 now has a console that needs no UART and no extra pins: an RTT-only Zephyr app, built for
the `mini_stm32h743` board target (that is the WeAct H743VIT6), flashed through the same SWD header, and
read back over the same link.

- App `h743-rtt-test` (three files: `CMakeLists.txt`, `prj.conf` with `CONFIG_USE_SEGGER_RTT` and a 1 KB
  up buffer, and `src/main.c`, which writes a counter to channel 0 every 500 ms). Build it inside the
  Zephyr workspace: `west build -b mini_stm32h743 -d ~/h743-rtt-build <app-dir>`.
- Control block address comes from the image, so `rtt setup` needs no search:
  `arm-zephyr-eabi-nm zephyr.elf | grep _SEGGER_RTT` printed `24000410` for this build (AXI SRAM).
- Repo tools: `jtag/h743-flash.sh` (program with verify and reset, then a two-sample program counter
  check that distinguishes "idle" from "hung") and `jtag/h743-rtt-read.sh <addr> [seconds]`. The reader
  starts OpenOCD on the probe with RTT on channel 0 and shuts it down through its own telnet console,
  because a root-owned OpenOCD cannot be signalled from the login account.
- Evidence: `tick 145 uptime 72524 ms` through `tick 163 uptime 81526 ms` over an 8 s read — 18 ticks in
  18 slots at 500 ms, nothing dropped — then `openocd stopped` with the pins released.
- The flash run's closing reset really does restart the app, which the same numbers show: a read taken a
  minute after a `program … reset` reports `tick 144 uptime 72024 ms`, a 72 s old boot, not a
  continuation of the stream before it.
- Not exercised: RTT over the network path (probe `remote_bitbang` transport), the RTT down channel, and
  ETM/ITM trace, which stays open in Bead `ebaz4205-s85`.

## SWD transport (2026-10-10)

An MCU target is reached over the same remote_bitbang path, with the port asked
for SWD explicitly: `--swd 8,9` claims SWCLK/SWDIO on the lines the repository
already wires for it, `--nrst 6` adds the reset line (the series resistor in
[`jtag/adapters/bananapi-m2-zero-swd.cfg`](../jtag/adapters/bananapi-m2-zero-swd.cfg)
applies to that wire, not to this flag), and TDI/TDO are never claimed. The
data line is re-requested as input or output on every turnaround, which is what
the `linuxgpiod` driver does inside OpenOCD for the local path; the remote path
does it with gpiochip handle requests.

**Status:** verified on hardware, read-only, from both ends of the link.

- The probe-side service was started with `--swd 8,9` alone (no `--nrst`, so
  every reset request stays refused).
- Pi-side client, over loopback: `SWD DPIDR 0x6ba02477`, `Cortex-M7 r1p1
  processor detected`, `target has 8 breakpoints, 4 watchpoints`, `Examination
  succeed`, then `pc (/32): 0x08021694`, `sp (/32): 0x24009158`, `resume`.
- Windows host, xPack OpenOCD 0.12.0-7 through the SSH forward, with
  `interface/pi-remote-bitbang.cfg` and `target/stm32-swd.cfg`: identical
  detection and `pc (/32): 0x08021698` — the program counter advanced between
  the two runs, so the core was running its own firmware and the halt/resume
  pair did not rewind it.
- Readback commands only print when they are passed as their own `-c`
  argument; the same commands inside a `-f` script run without printing their
  result. Both runs above used separate `-c` flags for that reason.
Root cause of the host-side failure that looked like a cable problem, found with
`--trace` (which logs the client's command bytes to stderr): the server held
SWDIO as an input until OpenOCD sent `'O'`, but OpenOCD's driver sends its SWD
line reset as plain pin writes (`'0'`-`'7'`). When that reset arrived first it
was clocked onto a floating line, the DP never woke, and the host reported
`Error connecting DP: cannot read IDR` against a target that the local
`linuxgpiod` driver reached without trouble. The server now infers the direction
from the traffic — a pin write or a `'d'` drives SWDIO, a `'c'` releases it —
while the explicit `'O'`/`'o'` are still honoured. `--trace` stays: a bit-level
transport with no way to see the bytes is what made this expensive to find.

- Still open on this transport: programming, RTT streaming and ETM/ITM trace,
  a reset-assert check with `--nrst` and the series resistor in place, and an
  NXP part. Bead `ebaz4205-s85`.

Note for the Vivado check: if an unrelated `hw_server` already owns
`localhost:3121` and carries its own stale XVC target, `open_hw_target` fails
with `no active target available ... may be locked by another hw_server`. Pass
the optional second argument to point the check at a fresh hardware server:
`-tclargs 127.0.0.1:2543 localhost:3122`.
