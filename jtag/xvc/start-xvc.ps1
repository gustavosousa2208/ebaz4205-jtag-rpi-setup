# Opens an authenticated SSH local forward to the loopback-only XVC server.
# Start the server on the Pi separately; this script never asks it to bind LAN.
param(
    [string]$Pi = 'gusta-bpi',
    [int]$Port = 2542
)
$ErrorActionPreference = 'Stop'
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }

Write-Host "Forwarding localhost:$Port to $Pi`:127.0.0.1:$Port over SSH."
Write-Host 'Ensure the XVC server is running on the Pi bound to 127.0.0.1 only.'
Write-Host 'Ctrl-C closes the tunnel.'
ssh -N -L "${Port}:127.0.0.1:${Port}" $Pi
if ($LASTEXITCODE -ne 0) { throw "SSH tunnel exited with status $LASTEXITCODE" }
