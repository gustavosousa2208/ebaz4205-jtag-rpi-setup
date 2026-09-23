# READ-ONLY health probe. Reads DevCfg / SLCR state through the DAP's AHB-AP, which reaches
# memory independently of the CPU: nothing is halted, resumed or written, and no PL address is
# ever touched (an access to an unconfigured/unmapped PL never returns and wedges the core).
target create zynq.ahb mem_ap -dap zynq.dap -ap-num 0

proc rd {addr} {
    if {[catch {set v [lindex [read_memory $addr 32 1] 0]} msg]} { return "READ_FAILED" }
    return [format 0x%08X $v]
}

init
targets zynq.ahb
set sts [rd 0xF800700C]
set lvl [rd 0xF8000900]
echo "PROBE_TAPS=ok"
echo "PROBE_DEVCFG_CTRL=[rd 0xF8007000]"
echo "PROBE_DEVCFG_INTSTS=$sts"
echo "PROBE_DEVCFG_STATUS=[rd 0xF8007014]"
echo "PROBE_SLCR_LVL_SHFTR=$lvl"
echo "PROBE_SLCR_FPGA_RST=[rd 0xF8000240]"
echo "PROBE_SLCR_BOOT_MODE=[rd 0xF800025C]"
echo "PROBE_PLL_STATUS=[rd 0xF800010C]"
echo "PROBE_FPGA0_CLK_CTRL=[rd 0xF8000170]"
echo "PROBE_SLCR_A9_CPU_RST_CTRL=[rd 0xF8000244]"
echo "PROBE_DDRC_CTRL=[rd 0xF8006000]"
if {$sts ne "READ_FAILED"} {
    echo "PROBE_PCFG_DONE=[expr {($sts >> 2) & 1}]"
}
echo "PROBE_DONE"
shutdown
