# EBAZ4205 JTAG tools

This directory supports a Sipeed RV CMSIS-DAP connected to macOS and native
Raspberry Pi GPIO-JTAG. It also contains the Zynq-7000 configuration and PS
initialization generated from the project's `hardware/ebaz_test.xsa`.

## macOS connections

The default adapter is the Sipeed RV CMSIS-DAP with serial `012345ABCDEF`.
Homebrew OpenOCD is selected automatically. The board UART is detected as the
first `/dev/cu.usbserial-*` device.

Verify that macOS sees both adapters:

```sh
system_profiler SPUSBDataType
ls /dev/cu.usbserial-*
```

Select another probe or UART explicitly:

```sh
EBAZ_CMSIS_DAP_SERIAL=012345ABCDEF ./upload-code --full
EBAZ_UART_DEVICE=/dev/cu.usbserial-A5069RR4 ./upload-code --uart
./upload-code --full --uart-seconds 25 /path/to/app.elf /path/to/design.bit
./upload-code --pcap --uart /path/to/app.elf /path/to/design.bit
```

macOS uploads run without `sudo`.

Probe the adapter and both Zynq JTAG TAPs without programming:

```sh
./probe
```

## Raspberry Pi GPIO wiring

| Pi physical pin | GPIO | EBAZ4205 J8 |
|---:|---:|---:|
| 23 | 11 | 6 (TCK) |
| 24 | 8 | 4 (TMS) |
| 19 | 10 | 10 (TDI) |
| 21 | 9 | 8 (TDO) |
| 20 | GND | 7 (GND) |

Do not connect power between the Pi and EBAZ4205.

## Banana Pi M2 Zero GPIO wiring

Install `openocd` and `gcc-arm-none-eabi` on the Banana Pi, then select its
native Linux GPIO adapter:

```sh
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-gpio make probe
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-gpio make upload-full
```

The same 40-pin header *positions* as the Raspberry Pi layout are used. On
the Banana Pi M2 Zero, connect header 23 to EBAZ J8-6 (TCK), 24 to J8-4
(TMS), 19 to J8-10 (TDI), 21 to J8-8 (TDO), and 20 to J8-7 (GND). Do not
connect power between the boards. The linuxgpiod and sysfsgpio backends do not
provide configurable clock speed; their `adapter speed` value does not change
the effective rate. Keep this backend as the correctness baseline while the
SPI0 transport is developed.

For the EBAZ's UART heartbeat, enable the Banana Pi's `uart3` Armbian overlay
and reboot. It appears as `/dev/ttyS3` at 115200 baud. Connect Banana header
10 (UART RX) to EBAZ J7-2 (TX), and Banana header 6 (GND) to EBAZ J7-1 (GND).
Header 8 (UART TX) is unused by the current transmit-only application.

The verified Banana Pi baseline is a 2,083,870-byte bitstream plus ELF and
10-second UART capture in 88.974 seconds; ELF-only upload is 5.055 seconds.
The two verified Zynq TAP IDs are PL `0x13722093` and ARM `0x4ba00477`.
These timings were measured with the bundled linuxgpiod backend, whose
effective clock cannot be selected through OpenOCD.

The next high-speed transport uses SPI0 for TDI/TDO/TCK and keeps PC3 as the
GPIO TMS line. Do not enable Armbian's stock `spi-spidev` overlay for this
wiring: it claims PC3 as SPI chip-select. Use
`adapters/bananapi-m2-zero-spi-tms-overlay.dts`, which enables SPI0 with a
three-pin pinmux; the transport must open the resulting spidev device with
`SPI_NO_CS` and drive TMS through GPIO.

On the reference Pi (`192.168.18.194`), the custom overlay has been staged and
rebooted successfully: `/dev/spidev0.0` exists, SPI0 is enabled, and PC3 is
still an unclaimed GPIO. The original boot configuration is backed up as
`/boot/armbianEnv.txt.pre-semi-jtag-20260920`. Do not enable the stock
`sun8i-h3-spi-spidev` overlay for this wiring.

The stock spidev approach was subsequently rejected for generic JTAG. The
H2+/H3 `spi-sun6i` driver exposes only 8-bit words, while JTAG requires TMS to
change on an exact, potentially non-byte-aligned final scan bit. Extra padding
clocks are not safe because they change TAP state or the selected instruction.

The working fallback is `mmio-jtag-idcode.c`, which requests PC0-PC3 through
the GPIO character device before using a bounded `/dev/mem` mapping of the PIO
register page. The GPIO request is required after a cold boot; directly
changing the mux registers without it produced an all-zero TDO stream. Build
it on the Banana Pi with `jtag/build-mmio-jtag`. The proof restores the saved
Port C mux/data bits on exit and validates both Zynq IDCODEs without programming
the FPGA.

On the reference board, a fresh-boot test passed 20/20 scans at every requested
rate from 100, 250, 500, 1000, 2000, and 5000 kHz. Effective rates were 100,
250, 500, 998, 1185, and 1187 kHz respectively. Use 1000 kHz as the initial
conservative rate; requests above 1 MHz currently saturate around 1.19 MHz.

The production Banana Pi path is `bananapi-m2-zero-mmio`. Build the patched
OpenOCD tree as `~/openocd-ebaz`, build the scanner, and install both root-owned
binaries plus the narrow passwordless runner rule:

```sh
./jtag/build-mmio-jtag
sudo ./jtag/install-mmio-runner
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio make probe
```

PL uploads default to a `1000,500,250,100` kHz rate ladder. Override it with
`EBAZ_JTAG_RATE_LADDER`; `EBAZ_PLD_RETRIES` remains the maximum number of full
attempts. Only transport and configuration failures advance to the next rate.
Each attempt logs its backend, requested rate, measured effective rate, failure
category, DEVCFG result, and elapsed time. `EBAZ_TEST_FAIL_PL_ATTEMPT=1` injects
a transport failure before touching PL state and is intended only for testing
fallback and exhausted-ladder handling.

### Stress-tested deployment limits

The reference Banana Pi passed 20/20 consecutive full uploads at a requested
1000 kHz on 2026-09-20. Every run had `PCFG_DONE=1`, `PCFG_INIT=1`, released
FPGA resets, enabled level shifters, a verified ELF at `0x10000`, and a UART
heartbeat. The PL-stage timing was 18.594 s minimum, 18.602 s median, 18.613 s
mean, and 18.834 s maximum. Observed 1-minute system load average was 0.22 at
the start and at most 1.07 during the series. Five subsequent ELF-only uploads
passed in 1.220-1.222 s each, versus the 5.055 s linuxgpiod baseline.

A requested 2000 kHz test reached 1252 kHz effective and failed safely with
`PCFG_DONE=0`, `INT_STS=0x0802000b`, category `configuration`, and a nonzero
exit. A full 1000 kHz upload immediately recovered and restored the UART
heartbeat. Therefore 1000 kHz is both the highest zero-failure tested rate and
the production default; 2000 kHz is outside the demonstrated reliable range.

Reproduce the production-rate series with:

```sh
for run in $(seq 1 20); do
    EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio \
    EBAZ_JTAG_RATE_LADDER=1000 EBAZ_PLD_RETRIES=1 \
    EBAZ_UART_SECONDS=1 make upload-full UART=1 || break
done
```

The verified harness used approximately 15 cm direct TCK/TMS/TDI/TDO jumpers
and exactly one common ground between Banana header 20 and EBAZ J8-7. Treat
longer wiring as unqualified until it passes the same stress procedure. Do not
infer success from LEDs: require all register, ELF, and UART checks listed
above. After any failed high-rate experiment, rerun a full upload at 1000 kHz;
the failure path invalidates the cached PL-verification marker.

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

For the Banana Pi MMIO adapter, use the faster PS-side PCAP path:

```sh
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio make upload-pcap
EBAZ_JTAG_ADAPTER=bananapi-m2-zero-mmio make upload-pcap UART=1
```

The PCAP flow converts `.bit` or gzip-compressed `.bit.gz` input to PCAP byte
order, stages it in DDR, and verifies the full image with the Cortex-A target
CRC before programming. It requires the local OpenOCD build with MMIO support
and keeps the GDB server on localhost because this OpenOCD version needs its
GDB service during target-side CRC execution. A tested 2,083,700-byte image,
PL load, and ELF start took 36.6 seconds at 1 MHz and an 8-clock DAP delay.
The older zero-delay / 1.5 MHz experiment took 27.4 seconds but generated DAP
WAIT retries, so it is not the default.

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
`upload-code` also captures the detected UART at 115200 baud and saves a
matching `*-uart.log`. Without that option it never opens the UART device. The
adapter must not be open in another terminal while capture is active.
The capture helper keeps one serial descriptor open while configuring and
reading it; this avoids macOS resetting the FTDI baud rate when a second
program opens the device.

OpenOCD is stopped automatically after the requested UART capture, so the
command returns without `Ctrl-C`. Use `--keep-openocd` only when intentionally
keeping the debugger attached for a later fast ELF-only upload.

The verified bitstream hash survives after OpenOCD stops. An unchanged
bitstream is skipped on the next upload; a changed one is programmed before
the ELF. Because FPGA configuration is volatile, use `--full` after every
board reset or power cycle.

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
