# EBAZ4205 Raspberry Pi JTAG tools

This directory contains the tested OpenOCD executable, the minimal Pi 4 GPIO
and Zynq-7000 configuration, and PS initialization generated from the project's
`hardware/ebaz_test.xsa`.

## Wiring

| Pi physical pin | GPIO | EBAZ4205 J8 |
|---:|---:|---:|
| 23 | 11 | 6 (TCK) |
| 24 | 8 | 4 (TMS) |
| 19 | 10 | 10 (TDI) |
| 21 | 9 | 8 (TDO) |
| 20 | GND | 7 (GND) |

Do not connect power between the Pi and EBAZ4205.

At the configured 1 MHz GPIO-JTAG rate, the 2.08 MB test bitstream takes about
17 seconds. This is expected: its roughly 16.7 million bits already require a
theoretical minimum of 16.7 seconds, before protocol overhead.

The visible green EBAZ LEDs are user PL outputs, not a guaranteed configuration
DONE indicator. They turn on only when the loaded design drives their FPGA pins.

## Upload only the FPGA bitstream

With the default project bitstream:

```sh
./upload-bitstream
```

Or specify another bitstream:

```sh
./upload-bitstream /path/to/design.bit
```

## Upload bitstream and application

With the default `build/hello.elf` and `hardware/ebaz_test.bit`:

```sh
./upload-code
```

Or specify both files:

```sh
./upload-code /path/to/application.elf /path/to/design.bit
```

After resetting or power-cycling the board, explicitly start a new hardware
session:

```sh
./upload-code --full
```

UART capture is disabled by default. Enable it explicitly when needed:

```sh
./upload-code --uart
./upload-code --full --uart
```

The application entry point is read from the ELF automatically. Both uploads
are volatile and are lost when the EBAZ4205 is reset or powered off.

The first `upload-code` (or `upload-code --full`) initializes the PS and
transfers both the bitstream and ELF. OpenOCD then stays attached. Later
`upload-code` runs with the same bitstream transfer only the ELF, which should
take a few seconds. If the requested bitstream changes while that session is
alive, the script transfers the new bitstream and ELF without repeating PS
initialization.

## Diagnostic logs

Every run saves a timestamped OpenOCD log under `logs/`. With `--uart`,
`upload-code` also captures `/dev/ttyUSB0` at 115200 baud and saves a matching
`*-uart.log`. Without that option it never opens the UART device. The adapter
must not be open in another terminal while capture is active.

After `upload-code`, OpenOCD remains attached in the background because this
OpenOCD/Cortex-A9 combination halts CPU0 during target teardown. Its PID is in
`run/openocd.pid`. Do not stop it between uploads if you want the fast ELF-only
path. Use `upload-code --full` after resetting or power-cycling the board;
process liveness cannot reliably distinguish a board reset because OpenOCD may
remain alive after the JTAG target disappears.

The hello-world test now transmits a numbered heartbeat continuously. For UART,
connect J7 pin 1 to FTDI GND and J7 pin 2 (EBAZ TX) to FTDI RX. Do not connect
FTDI VCC. If the application is running but the UART log is empty, verify that
J7 pin 2 idles near 3.3 V and shows repeated bursts with a scope or logic
analyzer. Replug the FTDI adapter and ensure no other terminal owns ttyUSB0.

To test the FTDI independently, disconnect it from the EBAZ4205, short the
adapter's TX pin directly to its RX pin, and run `./test-ftdi-loopback`. The
result is saved as a timestamped `*-ftdi-loopback.log`.
