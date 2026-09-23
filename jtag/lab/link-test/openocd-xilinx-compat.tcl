# Minimal XSCT-to-OpenOCD memory-command compatibility for generated ps7_init.tcl.
proc mrd {addr} {
    set value [lindex [read_memory $addr 32 1] 0]
    # Generated XSCT Tcl takes the final eight characters and adds "0x".
    return [format "%08x" $value]
}

proc mwr {args} {
    if {[lindex $args 0] eq "-force"} {
        set addr [lindex $args 1]
        set value [lindex $args 2]
    } else {
        set addr [lindex $args 0]
        set value [lindex $args 1]
    }
    write_memory $addr 32 $value
}

proc mask_write {addr mask value} {
    set old [lindex [read_memory $addr 32 1] 0]
    set new [expr {($value & $mask) | ($old & ~$mask)}]
    write_memory $addr 32 $new
}

# XSCT toggles this around post-configuration. OpenOCD's DAP memory access is
# already available while the Cortex-A9 target is halted, so it is a no-op.
proc configparams {name args} {
    return 0
}
