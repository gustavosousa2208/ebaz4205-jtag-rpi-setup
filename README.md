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
