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
```

Use `upload-full` after every reset or power cycle. Later `upload` commands skip
an unchanged bitstream and transfer only the ELF.

Install the Banana Pi helper binaries after building patched OpenOCD in
`~/openocd-ebaz`:

```sh
./jtag/build-mmio-jtag
sudo ./jtag/install-mmio-runner
```

The installer places root-owned runners in `/usr/local/libexec`. Upload logs go
to `jtag/logs/`. Success requires DEVCFG checks, released PL resets, ELF verify,
and UART heartbeat—not LEDs or process exit alone.

## Other adapters

macOS defaults to CMSIS-DAP. Linux GPIO remains the correctness fallback:

```sh
make probe
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-gpio make probe
```

See [jtag/README.md](jtag/README.md) for adapter setup, UART, rate fallback,
stress results, recovery, and diagnostic details.

## Project layout

- `app/` — C application, startup assembly, linker script
- `hardware/` — matching `.bit` and `.xsa`
- `jtag/` — adapters, OpenOCD configuration, upload tools, logs
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
