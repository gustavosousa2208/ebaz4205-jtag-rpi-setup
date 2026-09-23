# Read-only Vivado Hardware Manager check for the EBAZ XVC endpoint.
# Run with: vivado -mode batch -source check-vivado-xvc.tcl -tclargs 127.0.0.1:2542

set xvc_url "127.0.0.1:2542"
if {[info exists ::argc] && $::argc > 0} {
	set xvc_url [lindex $::argv 0]
}

set manager_open 0
set server_connected 0
set target_open 0
set test_error [catch {
	open_hw_manager
	set manager_open 1
	connect_hw_server -url localhost:3121
	set server_connected 1
	open_hw_target -xvc_url $xvc_url
	set target_open 1

	set devices [get_hw_devices]
	set ids {}
	foreach device $devices {
		set idcode [get_property IDCODE $device]
		lappend ids $idcode
		puts "DEVICE=$device IDCODE=$idcode PART=[get_property PART $device]"
	}

	foreach expected {
		01001011101000000000010001110111
		00010011011100100010000010010011
	} {
		if {[lsearch -exact $ids $expected] < 0} {
			error "Expected Zynq IDCODE $expected missing from XVC target $xvc_url"
		}
	}
	puts "VIVADO_XVC_PASS url=$xvc_url devices=$devices"
} test_message test_options]

if {$target_open} {
	catch {close_hw_target}
}
if {$server_connected} {
	catch {disconnect_hw_server}
}
if {$manager_open} {
	catch {close_hw_manager}
}

if {$test_error} {
	puts stderr "VIVADO_XVC_FAIL: $test_message"
	if {[dict exists $test_options -errorinfo]} {
		puts stderr [dict get $test_options -errorinfo]
	}
	exit 1
}
exit 0
