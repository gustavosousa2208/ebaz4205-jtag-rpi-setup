# Byte-for-byte verification by reading target memory back with plain DAP reads and comparing with `cmp`.
# Used instead of OpenOCD's verify_image, whose CPU-side CRC algorithm segfaults this OpenOCD build (exit 139)
# when the board has just been power-cycled (see docs/link-test/pi-services.md, change log 2026-09-21).
proc verify_readback {file addr {size ""}} {
    if {$size eq ""} { set size [file size $file] }
    set tmp "/tmp/ebaz-readback-[clock seconds].bin"
    if {[catch {dump_image $tmp $addr $size} msg]} { catch {file delete $tmp}; error "dump_image: $msg" }
    if {[catch {exec cmp -s $tmp $file} msg]} { catch {file delete $tmp}; error "READBACK DIFFERS from $file ($msg)" }
    file delete $tmp
    return "$size bytes identical"
}
