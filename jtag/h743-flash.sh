#!/bin/sh
# Flash the bench WeAct STM32H743VIT6 through the probe's SWD header, with verify and reset.
# Run it on the probe: the pins are local to it, and this is the fast path (the network
# remote_bitbang transport is a bit-per-command link, wrong for bulk payloads).
#
#   jtag/h743-flash.sh app.elf                # ELF: load addresses come from the file
#   jtag/h743-flash.sh image.bin 0x08000000   # raw image: the load address is required
#
# The snapshot taken before the first write to this board restores its factory firmware:
#   jtag/h743-flash.sh weact-h743vit6-flash-2026-10-10.bin 0x08000000
#
# Not obvious, all learned on hardware (2026-10-10):
#   - the probe grants passwordless root for exactly this binary (install-xvc.sh writes the rule), which
#     is what lets this run unattended: without it the caller needs sudo on the whole script.
#   - target/stm32h7x_dual_bank.cfg is the config for a 2 MB H743. The single-bank stm32h7x.cfg would
#     mis-map everything above 0x080FFFFF, and this board uses both banks.
#   - the probe's OpenOCD was built without its scripts directory baked in, so the search path has to be
#     given explicitly (EBAZ_OPENOCD_SCRIPTS overrides it).
#   - srst is wired to the target through a series resistor, which is what makes the final reset real.
set -e
FILE="${1:?usage: h743-flash.sh <elf|bin> [load-address]}"
ADDR="$2"
J=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OCD="sudo -n ${EBAZ_OPENOCD:-/usr/local/libexec/openocd-ebaz-mmio}"
SCRIPTS="${EBAZ_OPENOCD_SCRIPTS:-/usr/share/openocd/scripts}"
BASE="-s $SCRIPTS -f $J/adapters/bananapi-m2-zero-swd.cfg -f target/stm32h7x_dual_bank.cfg"

if [ -n "$ADDR" ]; then
	TARGET="$FILE $ADDR"
else
	TARGET="$FILE"
fi

echo "=== programming $FILE ==="
LOG=/tmp/h743-flash.log
eval "$OCD $BASE -c \"adapter gpio srst -chip 0 6\" -c \"reset_config srst_only\" \
  -c \"adapter speed 1000\" -c init -c \"program $TARGET verify reset\" -c shutdown" >"$LOG" 2>&1 || true
grep -E "Programming|Verified|Resetting|Error" "$LOG" | head -10
# The exit status of a piped grep is not OpenOCD's: assert the outcome in the log instead, or a failed
# write looks like a success (that silent case is exactly how a flash run was mistaken for done).
grep -q "Verified OK" "$LOG" || { echo "FAILED: no 'Verified OK' in $LOG" >&2; exit 1; }

echo "=== executing afterwards? ==="
# Two program counters 300 ms apart: the same value can just mean the idle thread, a different value
# proves the core is running.
eval "$OCD $BASE -c \"adapter speed 1000\" -c init -c halt -c \"reg pc\" -c resume -c \"sleep 300\" \
  -c halt -c \"reg pc\" -c resume -c shutdown" 2>&1 | grep -E "pc \(" | head -3
