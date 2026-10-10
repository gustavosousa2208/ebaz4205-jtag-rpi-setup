#!/bin/sh
# Read SEGGER RTT channel 0 from the bench STM32H743VIT6 through the probe's SWD header.
# Run it on the probe, and only while no other tool is driving the pins.
#
#   jtag/h743-rtt-read.sh <SEGGER_RTT address> [seconds]
#
# The address comes from the image being read, no search needed:
#   arm-zephyr-eabi-nm zephyr.elf | grep ' _SEGGER_RTT'      # -> 0x24000410 for the test app
#
# RTT is pure memory access over the debug port, so it needs no pins and no UART on the target: that is
# why it is the console of choice on a board whose only wired connection is SWD.
#
# OpenOCD keeps its telnet console on 4445 (4444 is the network probe's remote_bitbang port) and is shut
# down through that console: it runs as root, so a plain kill from the login account would fail and
# leave it holding the pins.
set -e
ADDR="${1:?usage: h743-rtt-read.sh <SEGGER_RTT address> [seconds]}"
SECS="${2:-8}"
J=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OCD="sudo -n ${EBAZ_OPENOCD:-/usr/local/libexec/openocd-ebaz-mmio}"
SCRIPTS="${EBAZ_OPENOCD_SCRIPTS:-/usr/share/openocd/scripts}"
BASE="-s $SCRIPTS -f $J/adapters/bananapi-m2-zero-swd.cfg -f target/stm32h7x_dual_bank.cfg"

$OCD $BASE -c "telnet_port 4445" -c "adapter speed 1000" -c init \
	-c "rtt setup $ADDR 0x1000 \"SEGGER RTT\"" -c "rtt start" \
	-c "rtt server start 9090 0" >/tmp/rtt-openocd.log 2>&1 &
sleep 3
echo "=== openocd rtt ==="
grep -E "Control block|rtt server|Error|Not configured" /tmp/rtt-openocd.log | head -5

status=0
python3 - "$SECS" <<'PY' || status=1
import socket, sys
seconds = float(sys.argv[1])
data = b""
try:
    s = socket.create_connection(("127.0.0.1", 9090), timeout=5)
    s.settimeout(seconds)
    while len(data) < 4000:
        chunk = s.recv(512)
        if not chunk:
            break
        data += chunk
except OSError as exc:
    print(f"RTT read failed: {exc}")
print("--- RTT channel 0 ---")
print(data.decode("utf-8", "replace") or "(no data)")
sys.exit(0 if data else 1)      # an empty read is a failure, not a quiet success
PY

python3 - <<'PY'
import socket, time
try:
    s = socket.create_connection(("127.0.0.1", 4445), timeout=3)
    time.sleep(0.3)
    s.sendall(b"shutdown\n")
    time.sleep(0.5)
    s.close()
    print("openocd asked to shut down")
except OSError as exc:
    print(f"could not reach the openocd console: {exc}")
PY
sleep 1
pgrep -f "openocd-ebaz-mmi[o]" >/dev/null && echo "WARNING: an openocd is still running" || echo "openocd stopped"
exit $status
