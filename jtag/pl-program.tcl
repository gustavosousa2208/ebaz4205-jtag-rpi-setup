# Reliable Zynq-7000 PL initialization and verification around OpenOCD's
# Series-7-compatible virtex2 loader.

proc ebaz_reg_read {addr} {
    return [lindex [read_memory $addr 32 1] 0]
}

proc ebaz_reg_write {addr value} {
    write_memory $addr 32 $value
}

proc ebaz_wait_reg {addr mask expected attempts description} {
    for {set i 0} {$i < $attempts} {incr i} {
        set value [ebaz_reg_read $addr]
        if {[expr {$value & $mask}] == $expected} {
            return $value
        }
        sleep 1
    }
    set value [ebaz_reg_read $addr]
    error [format "%s timed out: register 0x%08x = 0x%08x, mask 0x%08x expected 0x%08x" \
        $description $addr $value $mask $expected]
}

proc ebaz_prepare_pl {} {
    set ctrl_addr 0xF8007000
    set int_sts_addr 0xF800700C
    set status_addr 0xF8007014
    set pcfg_prog_b 0x40000000
    set pcfg_init 0x00000010

    set ctrl [ebaz_reg_read $ctrl_addr]
    set ctrl_high [expr {$ctrl | $pcfg_prog_b}]
    set ctrl_low [expr {$ctrl_high & 0xBFFFFFFF}]

    echo "Preparing Zynq PL configuration (PCFG_PROG_B high-low-high)..."
    ebaz_reg_write $ctrl_addr $ctrl_high
    sleep 1
    ebaz_reg_write $ctrl_addr $ctrl_low
    ebaz_wait_reg $status_addr $pcfg_init 0 100 "PL reset assertion"
    ebaz_reg_write $ctrl_addr $ctrl_high
    set status [ebaz_wait_reg $status_addr $pcfg_init $pcfg_init 100 "PL initialization"]

    # INT_STS bits are write-to-clear. A later PCFG_DONE must belong to this
    # programming attempt, not an older configuration.
    ebaz_reg_write $int_sts_addr 0xFFFFFFFF
    echo [format "PL ready for bitstream: DEVCFG.STATUS=0x%08x" $status]
}

proc ebaz_verify_pl {} {
    set int_sts [ebaz_reg_read 0xF800700C]
    set status [ebaz_reg_read 0xF8007014]

    echo [format "PL result: DEVCFG.INT_STS=0x%08x DEVCFG.STATUS=0x%08x" $int_sts $status]
    virtex2 read_stat $::env(EBAZ_PLD_DEVICE)

    if {[expr {$int_sts & 0x00000004}] == 0} {
        error [format "PL programming failed: PCFG_DONE is 0 (INT_STS=0x%08x)" $int_sts]
    }
    if {[expr {$status & 0x00000010}] == 0} {
        error [format "PL programming failed: PCFG_INIT is 0 (STATUS=0x%08x)" $status]
    }
    echo "PL configuration verified: PCFG_DONE=1"
}

proc ebaz_verify_post_config {} {
    set resets [ebaz_reg_read 0xF8000240]
    set level_shifters [ebaz_reg_read 0xF8000900]
    echo [format "PL post-config: FPGA_RST_CTRL=0x%08x LVL_SHFTR_EN=0x%08x" \
        $resets $level_shifters]

    if {$resets != 0} {
        error [format "PL post-configuration failed: FPGA resets remain asserted (0x%08x)" $resets]
    }
    if {[expr {$level_shifters & 0x0000000F}] != 0x0000000F} {
        error [format "PL post-configuration failed: level shifters are not all enabled (0x%08x)" \
            $level_shifters]
    }
}

proc ebaz_program_pl {bitstream} {
    set retry_limit $::env(EBAZ_PLD_RETRIES)
    if {![string is integer -strict $retry_limit] || $retry_limit < 1} {
        error "EBAZ_PLD_RETRIES must be a positive integer, got '$retry_limit'"
    }

    for {set attempt 1} {$attempt <= $retry_limit} {incr attempt} {
        echo [format "PL programming attempt %d/%d..." $attempt $retry_limit]
        set result [catch {
            ebaz_prepare_pl
            echo "Programming the FPGA bitstream..."
            pld load $::env(EBAZ_PLD_DEVICE) $bitstream
            ebaz_verify_pl
        } failure]
        if {!$result} {
            echo [format "PL configuration verified on attempt %d/%d." $attempt $retry_limit]
            return
        }
        echo [format "PL attempt %d/%d failed: %s" $attempt $retry_limit $failure]
        if {$attempt == $retry_limit} {
            error [format "PL configuration failed after %d attempts: %s" $retry_limit $failure]
        }
    }
}

proc ebaz_mark_upload_success {} {
    set marker [open $::env(EBAZ_SUCCESS_MARKER) w]
    puts $marker "Application started at $::env(EBAZ_ENTRY)"
    close $marker
}
