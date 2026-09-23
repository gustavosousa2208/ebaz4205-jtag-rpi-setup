#!/bin/bash
# Runs ONE OpenOCD job on the Banana Pi M2 Zero (fast Port-C MMIO GPIO-JTAG).
#   run.sh probe               read-only health probe (touches nothing)
#   run.sh psreset             PS soft reset through the AHB-AP (recovery from a wedged core / dead core debug unit)
#   run.sh pcap <file.bin>     configure the PL through PCAP (stage in DDR, verify, DMA)
#   run.sh elf  <file.elf>     load and start a bare-metal ELF (does not touch the PL)
# OpenOCD runs as root here, so its gdb/Tcl/telnet servers must not be reachable from the network (each would
# give root command execution): bound to 127.0.0.1 only (`bindto`), tcl and telnet disabled.
# The gdb port must NOT be `disabled`: zynq_7000.cfg makes the two cores an SMP group and cortex_a_resume() then dereferences
# target->gdb_service, which OpenOCD only allocates when a gdb port is opened. With `gdb port disabled` every resume/step/
# verify_image segfaults (exit 139). That was the 'cold-start crash' (rp-nni.1.15), not the power state.
# Never uses `pld load`. Logs go to ./logs/. Exit status 0 only if the job's result marker is OK.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
openocd=/usr/local/libexec/openocd-ebaz-mmio          # the name enables the MMIO acceleration
scripts=$HOME/ebaz4205/jtag/openocd-scripts
mkdir -p "$here/logs"

mode=${1:-}
extra=()
case "$mode" in
    probe)
        tcl=probe.tcl; marker='PROBE_DONE'; limit=60 ;;
    psreset)
        tcl=ps-reset.tcl; marker='PSRESET_DONE'; limit=60 ;;
    pcap)
        bin=${2:?usage: run.sh pcap <file.bin>}
        [ -r "$bin" ] || { echo "cannot read $bin"; exit 2; }
        size=$(stat -c %s "$bin")
        [ $((size % 4)) -eq 0 ] || { echo "$bin size $size is not a multiple of 4 (not a PCAP .bin?)"; exit 2; }
        words=$((size / 4))
        extra=(-c "set ::env(EBAZ_BIN) {$(readlink -f "$bin")}" -c "set ::env(EBAZ_WORDS) $words")
        tcl=pcap-load.tcl; marker='PCAP_RESULT: OK'; limit=1800 ;;
    elf)
        elf=${2:?usage: run.sh elf <file.elf>}
        [ -r "$elf" ] || { echo "cannot read $elf"; exit 2; }
        entry=$(arm-none-eabi-readelf -h "$elf" | awk '/Entry point/{print $NF}')
        [ -n "$entry" ] || { echo "no entry point in $elf"; exit 2; }
        # flatten the ELF to raw bytes and find its lowest loadable address, for the byte-for-byte readback
        vbin=$(mktemp /tmp/ebaz-elf-XXXXXX.bin); trap 'rm -f "$vbin"' EXIT
        arm-none-eabi-objcopy -O binary "$elf" "$vbin" || { echo "objcopy failed for $elf"; exit 2; }
        vaddr=""
        while read -r ptype poff pvirt pphys pfsz rest; do
            [ "$ptype" = LOAD ] && [ $((pfsz)) -ne 0 ] && { [ -z "$vaddr" ] || [ $((pphys)) -lt $((vaddr)) ]; } && vaddr=$pphys
        done < <(arm-none-eabi-readelf -lW "$elf")
        [ -n "$vaddr" ] || { echo "no loadable segment in $elf"; exit 2; }
        extra=(-c "set ::env(EBAZ_ELF) {$(readlink -f "$elf")}" -c "set ::env(EBAZ_ENTRY) $entry"
               -c "set ::env(EBAZ_VBIN) {$vbin}" -c "set ::env(EBAZ_VADDR) $vaddr")
        tcl=load-elf.tcl; marker='ELF_RESULT: OK'; limit=300 ;;
    *)
        echo "usage: $0 probe | psreset | pcap <file.bin> | elf <file.elf>"; exit 2 ;;
esac

if pgrep -x openocd >/dev/null || pgrep -f "$openocd" >/dev/null; then
    echo "another OpenOCD is already running (it owns the JTAG GPIO lines); refusing to start"; exit 3
fi

# Diagnostics (off by default): EBAZ_DEBUG=<0..4> raises OpenOCD's debug level; EBAZ_CORE=1 lets a crash leave a core file in $here.
dbg=()
[ -n "${EBAZ_DEBUG:-}" ] && dbg=(-d"$EBAZ_DEBUG")
[ "${EBAZ_CORE:-0}" = 1 ] && { ulimit -c unlimited; cd "$here"; }
log="$here/logs/$mode-$(date +%Y%m%d-%H%M%S).log"
echo "log: $log"
timeout "$limit" sudo -n "$openocd" -s "$scripts" \
    "${dbg[@]}" -c "bindto 127.0.0.1" -c "gdb port 3333" -c "tcl port disabled" -c "telnet port disabled" \
    -c "set ::env(EBAZ_DIR) {$here}" "${extra[@]}" \
    -f "$here/bananapi-m2-zero-gpio.cfg" -f "$here/ebaz4205.cfg" -f "$here/$tcl" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}

if grep -q "$marker" "$log"; then
    echo "== $mode: OK"; exit 0
fi
echo "== $mode: NOT OK (openocd exit $rc, marker '$marker' missing)"; exit 1
