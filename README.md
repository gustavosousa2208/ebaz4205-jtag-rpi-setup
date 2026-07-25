# EBAZ4205 bare-metal project

This project contains the Cortex-A9 application, Vivado hardware outputs, and
JTAG/OpenOCD tools for macOS CMSIS-DAP and Raspberry Pi GPIO.

## Layout

- `app/`: editable C, startup assembly, and linker script
- `build/`: generated ELF, object, and map files
- `hardware/`: matching bitstream and XSA
- `jtag/`: bundled OpenOCD, board configuration, upload scripts, and logs
- `tools/`: project maintenance helpers

## Raspberry Pi JTAG pinout

| Raspberry Pi 4 physical pin | GPIO | JTAG signal | EBAZ4205 J8 pin |
|---:|---:|:---:|---:|
| 23 | GPIO11 | TCK | 6 |
| 24 | GPIO8 | TMS | 4 |
| 19 | GPIO10 | TDI | 10 |
| 21 | GPIO9 | TDO | 8 |
| 20 | GND | GND | 7 |

or you can read it like this, looking in front, ignoring the first top 4, then looking to the ones in the left you have in the order

TDI
TDO
TCK
TMS
VCC

all on the right are GND


Connect the grounds, but do **not** connect power between the Raspberry Pi and
the EBAZ4205. In particular, TMS is Raspberry Pi physical pin 24 (GPIO8), not
physical pin 22 (GPIO25).

## Build and upload

From this directory:

```sh
make                 # compile build/hello.elf
make probe           # verify the adapter and Zynq JTAG chain
make upload-full     # after an EBAZ4205 reset or power cycle
make upload          # later fast ELF-only uploads
make upload UART=1   # upload and capture the detected FT232 UART
make bitstream       # upload only hardware/ebaz_test.bit
make clean
```

On macOS, connect the Sipeed RV CMSIS-DAP and FT232 adapter directly to the
Mac. The scripts automatically use Homebrew OpenOCD, the CMSIS-DAP probe, and
the first `/dev/cu.usbserial-*` device. Override them when needed:

```sh
EBAZ_OPENOCD=/path/to/openocd make upload-full
EBAZ_CMSIS_DAP_SERIAL=012345ABCDEF make upload-full
EBAZ_UART_DEVICE=/dev/cu.usbserial-A5069RR4 make upload UART=1
make upload-full \
    UPLOAD_ELF=/path/to/application.elf \
    UPLOAD_BITSTREAM=/path/to/design.bit
```

Install OpenOCD on macOS with `brew install open-ocd`. No `sudo` is used on
macOS.

The upload scripts find the ELF and bitstream from this layout automatically.
UART capture is disabled by default, so uploads do not open the serial device.
Pass `UART=1` to either upload Make target when a UART log is wanted. You may
still pass explicit files directly to `jtag/upload-code` when needed. See
`jtag/README.md` for wiring, UART, and diagnostic details.

## Updating the Vivado platform

After modifying the existing Vivado design, generate the bitstream and export
an XSA that includes it. For example, in the Vivado Tcl console:

```tcl
write_hw_platform -fixed -include_bit -force \
    -file /tmp/ebaz_test.xsa
```

Import that export with one command from this project directory:

```sh
make platform XSA=/tmp/ebaz_test.xsa
```

The importer validates that the XSA contains exactly one bitstream and one
`ps7_init.tcl`, then updates these matching files together:

- `hardware/ebaz_test.xsa`
- `hardware/ebaz_test.bit`
- `jtag/xsa/ps7_init.tcl`

It reports which files actually changed. If only the PL bitstream changed, use
`make upload`; the upload script detects the new bitstream hash and transfers
the bitstream plus ELF. If PS initialization changed, the importer records that
a full initialization is required. A normal `make upload` is then blocked:
power-cycle the board and use `make upload-full`. The requirement is cleared
only after a successful full upload.

This command imports an already-exported XSA. It does **not** run Vivado, use
Vitis, generate a BSP or `xparameters.h`, modify or preserve the block design,
assign AXI addresses, or update peripheral base addresses in `app/hello.c`.
Keep the editable Vivado project and any custom IP as the hardware source of
truth—preferably under `hardware/vivado/` if you want everything in this project
tree. An XSA is a deployment export, not a complete replacement for that
project.
