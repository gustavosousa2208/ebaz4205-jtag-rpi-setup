# EBAZ4205 bare-metal JTAG project

Cortex-A9 firmware, matching Zynq bitstream/XSA, and OpenOCD upload tools.
The verified fast path uses Banana Pi M2 Zero Port-C MMIO JTAG.

## Agent start

```sh
bd prime
git status --short
bd ready
```

Beads is the project task and memory source of truth. Do not replace it with a
Markdown task list or import `.beads/issues.jsonl` during normal work.

## Verified Banana Pi setup

| Banana Pi header | H2+ pin | Signal | EBAZ4205 J8 |
|---:|:---:|:---:|---:|
| 19 | PC0 | TDI | 10 |
| 21 | PC1 | TDO | 8 |
| 23 | PC2 | TCK | 6 |
| 24 | PC3 | TMS | 4 |
| 20 | GND | GND | 7 |

Use approximately 15 cm direct jumpers and one common ground. Do not connect
power between the boards.

### Second port: SWD for an MCU

A second target can hang off the same Banana Pi while the EBAZ stays wired: PC0-PC3 keep the Zynq
chain, and four Port A pins on the same PIO block carry SWD to an STM32-class MCU. Checked on the
running board on 2026-10-10: no device-tree pinctrl state references any of these lines, and a
`gpioget` request on each succeeds, so nothing on the board owns them.

| Banana Pi header | H2+ pin | gpiochip0 line | SWD signal | Notes |
|---:|:---:|---:|:---:|---|
| 31 | PA8 | 8 | SWCLK | the clock; JTAG calls it TCK |
| 33 | PA9 | 9 | SWDIO | the data line; JTAG calls it TMS, and it is **bidirectional** |
| 7 | PA6 | 6 | reserved | spare, the obvious place for NRST later |
| 29 | PA7 | 7 | reserved | an input from the target, so the natural pin for SWO trace later |
| GND (6, 9, 14, 25, 30, 34, 39; pin 20 is shared with the EBAZ) | – | – | GND | one common ground |

A minimum SWD link is SWCLK, SWDIO and ground. The MCU is powered separately; its VDD must be 3.3 V
(the H2+ pins are not 5 V tolerant) and the power rails must never be bridged.

JTAG to SWD, and the difference that matters:

| JTAG (EBAZ, PC0-PC3) | SWD (MCU, PA8-PA9) | Difference |
|---|---|---|
| TCK | SWCLK | same role: the clock |
| TMS | SWDIO | JTAG drives TMS as an output; SWD turns SWDIO around to read the target, so the line is switched between output and input |
| TDI | – | unused by SWD, reserved here for NRST |
| TDO | – | unused by SWD; being an input, it is the natural capture pin for SWO trace |

OpenOCD's `linuxgpiod` driver handles SWD on one SWDIO pin by re-requesting that line as input or
output as needed (`gpiod_line_settings_set_direction`), so no direction pin is wired here; its
optional `swdio_dir` is for boards with an external direction buffer.

The repository's MMIO runner contains that driver, but its MMIO acceleration is hard-coded to Port C
— the EBAZ pins — and engages whenever the binary is named `openocd-ebaz-mmio`, whatever pins the
adapter file configures. Run with the SWD adapter, it would drive PC0-PC3 instead of PA8/PA9 and the
traffic would never reach the MCU (reproduced on 2026-10-10; gating it on the configured pins is Bead
`ebaz4205-3pb.15`). The plain libgpiod path needs neither root nor `/dev/mem`, so use a copy whose
name does not carry that marker:

```sh
cp /usr/local/libexec/openocd-ebaz-mmio ~/openocd-plain    # MMIO stays off for this copy
~/openocd-plain -f ~/ebaz4205-jtag/jtag/adapters/bananapi-m2-zero-swd.cfg -f /path/to/target.cfg
```

That runs as your normal user through `plugdev`, logs `Linux GPIOD JTAG/SWD bitbang driver` and no
MMIO line, and touches only the Port A pins. Keep the installed runner (with sudo) for the EBAZ, where
the acceleration is what makes a 2 MB upload finish in about 19 s; the distro `/usr/bin/openocd` on
the probe has no `linuxgpiod`, so it cannot serve either port.

Not yet available over the network: the PiProbe server speaks XVC and remote_bitbang for JTAG only
and refuses reset asserts, so SWD needs the driver's four SWD commands implemented in the server
(`'d'` SWD write, `'O'`/`'o'` SWDIO direction, `'c'` SWDIO read) plus the same runtime direction
switch on the SWDIO pin, and a way for a session to select the SWD transport and the second pin set.
Tracked as Bead `ebaz4205-s85`.

Production settings:

- Adapter: `bananapi-m2-zero-mmio`
- Safe default: 1000 kHz
- Rate ladder: `1000,500,250,100`
- Zynq TAP IDs: PL `0x13722093`, ARM `0x4ba00477`
- Qualification: 20/20 full uploads passed at 1000 kHz
- 2000 kHz is outside the reliable range; it failed with `PCFG_DONE=0`

## Build and upload

```sh
make
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio make probe
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio make upload-full UART=1
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio make upload
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio make upload-pcap
```

Use `upload-full` after every reset or power cycle. Later `upload` commands skip
an unchanged bitstream and transfer only the ELF.

`upload-pcap` extracts the Xilinx `.bit` payload, converts it to the Zynq PCAP
byte order, stages it in DDR, checks it with the Cortex-A target CRC, then
configures the PL and loads/starts the ELF. The tested Banana Pi run completed
in 34.2 seconds at 1 MHz with a 4-clock DAP memory delay and no WAIT retries.
Add `UART=1` for an optional heartbeat capture.

Install the Banana Pi helper binaries after building patched OpenOCD in
`~/source/openocd-ebaz`:

```sh
./jtag/build-mmio-jtag
sudo ./jtag/install-mmio-runner
```

The installer places root-owned runners in `/usr/local/libexec`. Upload logs go
to `jtag/logs/`. Success requires DEVCFG checks, released PL resets, ELF verify,
and UART heartbeat—not LEDs or process exit alone.

## Raspberry Pi 4 setup

Same 40-pin positions as the Banana Pi: header 19 TDI (GPIO10), 21 TDO (GPIO9),
23 TCK (GPIO11), 24 TMS (GPIO8), 20 GND; UART header 10 (RXD) to EBAZ J7-2,
header 6 GND. On the Pi, remove `dtparam=spi=on` and any SPI display overlay
from `config.txt`, add `dtoverlay=disable-bt` (UART becomes the PL011
`/dev/ttyAMA0`), and drop `console=serial0,115200` from `cmdline.txt`.

The tracked `jtag/bin/openocd` and the distro package corrupt DAP traffic on
multi-megabyte transfers. Build upstream OpenOCD on the Pi and install it as
the runner (steps in `jtag/install-rpi-openocd`); `EBAZ_JTAG_ADAPTER=raspberrypi-gpio`
then selects it and `/dev/ttyAMA0` automatically:

```sh
sudo ./jtag/install-rpi-openocd
EBAZ_JTAG_ADAPTER=raspberrypi-gpio EBAZ_JTAG_RATE_LADDER=2000 make upload-full UART=1
EBAZ_JTAG_ADAPTER=raspberrypi-gpio EBAZ_PCAP_KHZ=2000 make upload-pcap UART=1
```

Measured on a Pi 4 (2026-09-26): 20/20 full uploads at 1000 kHz with the 2.08 MB
bitstream (about 23 s); 15/15 at 2000 kHz with a compressed bitstream
(`upload-full` 9.1 s, `upload-pcap` 10.5 s). 4000 kHz gave a checksum mismatch
and is not qualified. Pi 4 GPIO drive strength must stay at its default (16 mA);
2 mA breaks IDCODE reads.

Generate a compressed bitstream (about 39% of the size) from any routed design
without touching the project:

```sh
vivado -mode batch -nojournal -nolog -source tools/vivado-compress-bitstream.tcl     -tclargs path/to/design_routed.dcp out.bit
```

`EBAZ_PCAP_KHZ` and `EBAZ_DAP_MEMACCESS` override the PCAP path's fixed
1000 kHz and 4-clock defaults.

## ELF first, bitstream later, and debugging (Pi)

The application does not depend on the PL, so the ELF can run before the
bitstream is loaded and the PL can be programmed later without restarting it.
`jtag/elf-first-test.py` proves this on hardware with one resident OpenOCD:
PS init, PL cleared, ELF started with no bitstream, then a compressed bitstream
loaded at 2000 kHz in 3.7 s while the program runs (the UART heartbeat keeps
counting and is not reset), then debugging through OpenOCD (halt, hardware
breakpoint, single-step, memory reads) and `gdb-multiarch` (source-level
backtrace and breakpoint arguments). 11/11 checks passed on 2026-09-26.

```sh
make BUILD_DIR=/tmp/fastbuild EXTRA_CFLAGS=-DHEARTBEAT_DELAY=2000000
python3 jtag/elf-first-test.py path/to/design.bit 2000 /tmp/fastbuild/hello.elf
```

The default `HEARTBEAT_DELAY` (100 million iterations) is far slower than 1 s
with the caches off, so use the short build when you need to see progress.
Attach gdb with `gdb-multiarch -ex 'set architecture arm' -ex 'target
extended-remote localhost:3333' build/hello.elf`. After `detach` the core stays
halted; send `resume` to OpenOCD. CPU0 is the SMP target, so gdb sees one core.

## Other adapters

macOS defaults to CMSIS-DAP. Linux GPIO remains the correctness fallback:

```sh
make probe
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-gpio make probe
```

See [jtag/README.md](jtag/README.md) for adapter setup, rate fallback, stress
results, recovery, and diagnostic details. The Banana Pi UART bridge lives in
[`uart/`](uart/README.md); XVC support and its Vivado follow-up are tracked
under [`jtag/xvc/`](jtag/xvc/README.md) and Bead `ebaz4205-71t.1`.
The operational failure/recovery test matrix is
[docs/probe-hardening.md](docs/probe-hardening.md).

PiProbe network discovery design and fallback paths are documented in
[docs/piprobe-discovery.md](docs/piprobe-discovery.md) and tracked under Bead
`ebaz4205-3pb`. The host-side mDNS discovery command is described in
[piprobe/README.md](piprobe/README.md), which also covers `piprobe openocd`:
one command that resolves a probe, runs the loopback-only service through an
authenticated SSH forward, and runs host OpenOCD against the EBAZ4205.

## Project layout

- `app/` — C application, startup assembly, linker script
- `hardware/` — matching `.bit` and `.xsa`
- `jtag/` — adapters, OpenOCD configuration, upload tools, XVC, and lab archive
- `uart/` — Banana Pi serial bridge and reboot service
- `tools/` — platform maintenance helpers
- `build/` — generated ELF, objects, and map files

## Update the hardware export

Export an XSA containing the bitstream, then import both artifacts together:

```tcl
write_hw_platform -fixed -include_bit -force -file /tmp/ebaz_test.xsa
```

```sh
make platform XSA=/tmp/ebaz_test.xsa
```

If PS initialization changed, power-cycle the EBAZ4205 and run
`make upload-full`. The editable Vivado project remains the hardware source of
truth; an XSA is only a deployment export.
