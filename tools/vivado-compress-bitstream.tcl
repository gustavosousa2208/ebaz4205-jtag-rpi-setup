# Write a compressed bitstream from an already routed Vivado design.
#
#   vivado -mode batch -nojournal -nolog -source tools/vivado-compress-bitstream.tcl \
#       -tclargs <routed.dcp | project.xpr> <output.bit> [run_name]
#
# A .dcp is opened directly and a .xpr is opened read-only, so neither is
# modified. Bitstream compression replaces repeated configuration frames with
# multiple-frame writes; the design is unchanged but far fewer bytes have to
# cross a slow JTAG link. Implementation is not re-run: the run must already
# have a routed design.

if {$argc < 2} {
    puts stderr "usage: vivado -mode batch -source vivado-compress-bitstream.tcl -tclargs <routed.dcp|project.xpr> <output.bit> \[run_name\]"
    exit 2
}

set source_path [file normalize [lindex $argv 0]]
set output_bit  [file normalize [lindex $argv 1]]
set run_name    [expr {$argc > 2 ? [lindex $argv 2] : "impl_1"}]

if {![file exists $source_path]} {
    puts stderr "ERROR: $source_path does not exist"
    exit 2
}
file mkdir [file dirname $output_bit]

switch -- [string tolower [file extension $source_path]] {
    .dcp {
        open_checkpoint $source_path
    }
    .xpr {
        open_project -read_only $source_path
        open_run $run_name
    }
    default {
        puts stderr "ERROR: expected a .dcp or .xpr, got $source_path"
        exit 2
    }
}

set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
write_bitstream -force $output_bit
puts "COMPRESSED_BITSTREAM: $output_bit [file size $output_bit] bytes"
exit 0
