# Load and start a bare-metal ELF WITHOUT touching PL configuration (the PCAP flow owns that).
# Environment: EBAZ_DIR, EBAZ_ELF, EBAZ_ENTRY, EBAZ_VBIN, EBAZ_VADDR. Same sequence as pcap-flow/load-elf.cfg.
source $::env(EBAZ_DIR)/openocd-xilinx-compat.tcl
source $::env(EBAZ_DIR)/ps7_init.tcl
source $::env(EBAZ_DIR)/verify.tcl
proc fail {msg} { echo "ELF_RESULT: FAIL - $msg"; shutdown error }

init
targets zynq.cpu0
halt
# The PS must be initialised before any ELF runs: after a PS reset the UART pins/baud, clocks and
# DDR are unconfigured, so firmware would start and print nothing. Safe here because the core was
# just halted; running ps7_init under LIVE firmware is what wedges the bus.
ps7_init
echo "PS_INIT_DONE"
zynq.cpu0 configure -work-area-phys 0x00000000 -work-area-size 0x10000 -work-area-backup 0
if {[catch { load_image $::env(EBAZ_ELF) } msg]} { fail "load_image: $msg" }
# EBAZ_VBIN = the ELF flattened to raw bytes (run.sh), EBAZ_VADDR = its load address
if {[catch { verify_readback $::env(EBAZ_VBIN) $::env(EBAZ_VADDR) } msg]} { fail "readback verify: $msg" }
echo "VERIFY_OK: byte-for-byte readback, $msg"
echo "ELF_LOADED"
target smp zynq.cpu0
targets zynq.cpu0
resume $::env(EBAZ_ENTRY)
echo "ELF_RESULT: OK started at $::env(EBAZ_ENTRY)"
shutdown
