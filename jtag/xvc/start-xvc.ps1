# Starts the XVC server on the Banana Pi, listening on its LAN IP.
#   .\start-xvc.ps1 [-Pi gusta-bpi] [-Port 2542] [-RateKhz 1000]
# Then in Vivado Hardware Manager: Open Target > Add Xilinx Virtual Cable, host = the IP printed below, port = Port.
# Ctrl-C stops the server (wait for "stopped, pins restored").
param(
    [string]$Pi = 'gusta-bpi',
    [int]$Port = 2542,
    [int]$RateKhz = 1000
)
$ErrorActionPreference = 'Stop'
$bin = '/usr/local/libexec/ebaz-xvc-server'

# One round trip: installed? supports --bind? sudo rule? LAN IP (source address of the default route)? something else on the probe?
$probe = @"
[ -x $bin ] || { echo MISSING; exit 0; }
grep -q -- '--bind' $bin || { echo OLD; exit 0; }
[ -f /etc/sudoers.d/ebaz-xvc ] || { echo NOSUDO; exit 0; }
pgrep -x openocd >/dev/null && { echo BUSY openocd; exit 0; }
pgrep -f ebaz-xvc-server >/dev/null && { echo BUSY xvc; exit 0; }
ip=`$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
echo OK `$ip
"@ -replace "`r", ''
$r = ($probe | ssh -o BatchMode=yes -o ConnectTimeout=8 $Pi 'sh -s').Trim()
if ($LASTEXITCODE -ne 0) { throw "Cannot ssh to $Pi" }

switch -Regex ($r) {
    '^MISSING' { throw "Not installed on $Pi. On the Pi: cd ~/ebaz4205-jtag/jtag/xvc && gcc -std=c11 -O2 -Wall -Wextra -Werror -o ebaz-xvc-server xvc-server.c && sudo ./install-xvc.sh" }
    '^OLD'     { throw "Installed server predates --bind. On the Pi: cd ~/ebaz4205-jtag/jtag/xvc && gcc -std=c11 -O2 -Wall -Wextra -Werror -o ebaz-xvc-server xvc-server.c && sudo ./install-xvc.sh" }
    '^NOSUDO'  { throw "Sudoers rule missing. On the Pi: cd ~/ebaz4205-jtag/jtag/xvc && sudo ./install-xvc.sh" }
    '^BUSY'    { throw "Probe already in use on $Pi ($r). Stop it first." }
    '^OK \d+\.\d+\.\d+\.\d+$' { $ip = ($r -split ' ')[1] }
    default    { throw "Unexpected probe result: $r" }
}

Write-Host "Already installed on $Pi. LAN IP: $ip"
Write-Host "Vivado: add Xilinx Virtual Cable ${ip}:$Port   (no ssh tunnel needed; trusted LAN only, XVC has no auth)"
ssh -t $Pi "sudo -n $bin --bind $ip --port $Port --rate-khz $RateKhz"
