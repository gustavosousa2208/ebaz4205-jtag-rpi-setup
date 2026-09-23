# Configure the PL through the PS's own PCAP/DevCfg engine (the only reliable path on this
# setup, see ebaz4205-jtag-rpi-setup/pcap-flow/docs/ebaz-jtag-flow.md). NEVER use `pld load`.
#
# Environment (set with -c "set ::env(...)" by run.sh): EBAZ_DIR, EBAZ_BIN, EBAZ_WORDS.
#
# Differences from the original pcap-load.cfg (same register sequence, same values):
#  * abort BEFORE touching PCAP unless the staged DDR image reads back byte for byte identical (verify.tcl;
#    OpenOCD verify_image crashes this build after a cold power-up);
#  * poll for PCFG_DONE instead of a fixed sleep, and require INIT_B high as well;
#  * open the level shifters and release the PL resets ONLY when PCFG_DONE is confirmed, so a
#    failed load can never leave an AXI-stalling blank fabric connected to the PS;
#  * exit status: OpenOCD exits non-zero on failure ("shutdown error").
source $::env(EBAZ_DIR)/openocd-xilinx-compat.tcl
source $::env(EBAZ_DIR)/ps7_init.tcl
source $::env(EBAZ_DIR)/verify.tcl

proc rd {addr} { return [format 0x%08X [lindex [read_memory $addr 32 1] 0]] }
proc fail {msg} { echo "PCAP_RESULT: FAIL - $msg"; shutdown error }

set t0 [clock seconds]
init
targets zynq.cpu0
halt
zynq.cpu0 configure -work-area-phys 0x00000000 -work-area-size 0x10000 -work-area-backup 0
ps7_init
echo "PS_INIT_DONE"

# ---- stage the bitstream image in DDR over the DAP and verify it
if {[catch { load_image $::env(EBAZ_BIN) 0x00100000 bin } msg]} { fail "load_image: $msg" }
echo "STAGED: $msg  (+[expr {[clock seconds] - $t0}] s)"
if {[catch { verify_readback $::env(EBAZ_BIN) 0x00100000 } msg]} { fail "readback verify: $msg" }
echo "VERIFY_OK: byte-for-byte readback, $msg  (+[expr {[clock seconds] - $t0}] s)"
echo "DDR\[0\]=[rd 0x00100000] DDR\[1\]=[rd 0x00100004] DDR\[8\]=[rd 0x00100020]"

# ---- PCAP: pulse PROG_B, then DMA the staged image into the PL exactly as the FSBL does
mww 0xF8007034 0x757BDF0D
set ctrl [lindex [read_memory 0xF8007000 32 1] 0]
set ctrl [expr {$ctrl | 0x0C000000}]
mww 0xF8007000 $ctrl
mww 0xF8007000 [expr {$ctrl & ~0x40000000}]
sleep 50
echo "after PROG_B low : STATUS=[rd 0xF8007014]"
mww 0xF8007000 [expr {$ctrl | 0x40000000}]
sleep 100
echo "after PROG_B high: STATUS=[rd 0xF8007014]"
mww 0xF800700C 0xFFFFFFFF
mww 0xF8007018 0x00100001
mww 0xF800701C 0xFFFFFFFF
mww 0xF8007020 $::env(EBAZ_WORDS)
mww 0xF8007024 $::env(EBAZ_WORDS)

# ---- wait up to 20 s for PCFG_DONE (INT_STS bit 2)
set done 0
for {set i 0} {$i < 200} {incr i} {
    set sts [lindex [read_memory 0xF800700C 32 1] 0]
    if {$sts & 0x4} { set done 1; break }
    sleep 100
}
set int_sts [rd 0xF800700C]
set status  [rd 0xF8007014]
echo "DEVCFG INT_STS=$int_sts STATUS=$status"
set init_b_high [expr {([lindex [read_memory 0xF8007014 32 1] 0] >> 4) & 1}]
if {!$done}        { fail "PCFG_DONE not set (INT_STS=$int_sts): PL NOT configured, level shifters left closed" }
if {!$init_b_high} { fail "INIT_B low (STATUS=$status): bitstream rejected, level shifters left closed" }

# ---- PL is configured: now (and only now) open the level shifters and release the PL resets
mww 0xF8000008 0x0000DF0D
mww 0xF8000900 0x0000000F
mww 0xF8000240 0x00000000
mww 0xF8000004 0x0000767B
# Deliberately NOT resumed: ps7_init just re-initialised PLLs/DDR under whatever was in OCM; the
# ELF load overwrites it and starts clean.
echo "PCAP_RESULT: OK INT_STS=$int_sts STATUS=$status  (+[expr {[clock seconds] - $t0}] s)"
shutdown
