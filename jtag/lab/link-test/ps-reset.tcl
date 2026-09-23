# Recovery: ask the PS to reset itself (SLCR PSS_RST_CTRL soft reset) through the DAP's AHB-AP, which reaches memory
# independently of the CPU. Use it when the core debug unit or the core is wedged. `Invalid ACK` right after the write is
# expected: the DAP disappears because the reset took. Same method as ebaz4205-jtag-rpi-setup ps-reset.cfg.
target create zynq.ahb mem_ap -dap zynq.dap -ap-num 0
init
targets zynq.ahb
echo "AHB-AP up; issuing PS soft reset"
catch {mww 0xF8000008 0x0000DF0D}
echo "PSRESET_DONE (SLCR unlocked, about to write PSS_RST_CTRL)"
catch {mww 0xF8000200 0x00000001}
shutdown
