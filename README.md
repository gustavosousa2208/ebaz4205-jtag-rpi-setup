# EBAZ4205 bare-metal project

This project contains the Cortex-A9 application, Vivado hardware outputs, and
the self-contained Raspberry Pi GPIO-JTAG/OpenOCD tools.

## Layout

- `app/`: editable C, startup assembly, and linker script
- `build/`: generated ELF, object, and map files
- `hardware/`: matching bitstream and XSA
- `jtag/`: bundled OpenOCD, board configuration, upload scripts, and logs
- `tools/`: project maintenance helpers

## Build and upload

From this directory:

```sh
make                 # compile build/hello.elf
make upload-full     # after an EBAZ4205 reset or power cycle
make upload          # later fast ELF-only uploads
make upload UART=1   # upload and capture /dev/ttyUSB0
make bitstream       # upload only hardware/ebaz_test.bit
make clean
```

The upload scripts find the ELF and bitstream from this layout automatically.
UART capture is disabled by default, so uploads do not open `/dev/ttyUSB0`.
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
