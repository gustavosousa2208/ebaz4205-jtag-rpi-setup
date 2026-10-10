# Xilinx Virtual Cable prototype

JTAG-over-TCP server for the Banana Pi probe: two protocols from one binary.
XVC lets Vivado Hardware Manager connect over TCP to a remote JTAG target, and
OpenOCD's `remote_bitbang` lets host OpenOCD do the same. The Vivado
compatibility investigation is tracked as Bead `ebaz4205-71t.1`; the
OpenOCD/PiProbe path is `ebaz4205-3pb.4`.

The source is `xvc-server.c`; build the Banana Pi binary with:

```sh
gcc -std=c11 -O2 -Wall -Wextra -Werror -o ebaz-xvc-server xvc-server.c
```

Install or remove its narrow sudo rule on the Banana Pi with
`sudo ./install-xvc.sh` or `sudo ./install-xvc.sh --remove`. Never point two
tools at the probe at the same time: the server holds an exclusive lock on the
pins (`/run/ebaz-jtag-pins.lock`, or a per-user path under `/tmp` when it runs
unprivileged in `--fake`), so a second server refuses to start and names the
current holder. That is what keeps a Vivado (XVC) session and an OpenOCD
(remote_bitbang) session from driving the same pins, alongside the documented
rule against running XVC and the production OpenOCD uploader together.
Prototype logs from the previous `link-test` folder are preserved in
`jtag/lab/link-test/xvc/`.

The server binds to `127.0.0.1` only. Start it in a Banana Pi terminal:

```sh
sudo -n /usr/local/libexec/ebaz-xvc-server --port 2542 --rate-khz 1000
```

Keep it running, then start this tunnel on the Vivado computer (Windows
PowerShell supports the same OpenSSH command):

```sh
ssh -N -L 2542:127.0.0.1:2542 gusta-bpi
```

In Hardware Manager, open a local hardware server, add an XVC cable at
`127.0.0.1:2542`, then open the target. AMD documents this XVC target workflow
in [PG195](https://docs.amd.com/r/en-US/pg195-pcie-dma/Connecting-the-Vivado-Design-Suite-to-the-XVC-Server-Application).
For a repeatable, read-only device discovery check from a Vivado command
prompt, run:

```sh
vivado -mode batch -source check-vivado-xvc.tcl -tclargs 127.0.0.1:2542
```

The Tcl script opens the local `hw_server`, connects to XVC, and requires both
Zynq TAP IDCODEs (`0x4ba00477` and `0x13722093`) before reporting success. AMD
documents the `open_hw_target -xvc_url` Tcl command in
[UG835](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/open_hw_target).
See [the hardening matrix](../../docs/probe-hardening.md) for tested Vivado
client discovery, programming, ownership handoff, and remaining cleanup checks.
Stop the server with Ctrl-C and wait for its `stopped, pins restored` message
before using OpenOCD.

## OpenOCD host access (remote_bitbang)

No stock Windows OpenOCD build ships the XVC adapter driver: xPack
`0.12.0-7`, Sysprogs `2026-03-02` and MSYS2 `ucrt64 0.12.0-4` all answer
`adapter driver xvc` with "was not found". Every one of them does have
OpenOCD's own remote adapter protocol, which is why the same binary also
serves `remote_bitbang` (default port 4444):

```sh
sudo -n /usr/local/libexec/ebaz-xvc-server --protocol remote-bitbang --port 4444
```

Host OpenOCD reaches it over the same authenticated SSH forward:

```sh
ssh -N -L 4444:127.0.0.1:4444 gusta-bpi
```

```tcl
adapter driver remote_bitbang
remote_bitbang host 127.0.0.1
remote_bitbang port 4444
adapter speed 1000
```

`piprobe openocd` performs all of that — discovery, tunnel, service lifecycle —
from a single command. The wire contract follows OpenOCD's
[remote_bitbang driver](https://github.com/openocd-org/openocd/blob/master/src/jtag/drivers/remote_bitbang.c):
`'0'`-`'7'` set TCK/TMS/TDI, `'R'` returns exactly one TDO sample byte, `'Q'`
quits, `'B'`/`'b'` blink and `'Z'`/`'z'` sleep. The client owns the clock, so
`--rate-khz` does not apply in this mode.

Two refusals keep the JTAG-only, no-reset promise honest: a reset assert
(`'s'`, `'t'`, `'u'`) and any SWD-only command (`'O'`, `'o'`, `'c'`, `'d'`) close
the connection instead of being silently accepted, because no reset line is
wired to the target and the pins are JTAG-only. `test_remote_bitbang.py`
checks those refusals, the response framing and the one-client-at-a-time rule
against a `--fake` server.

## Secure host access

Raw XVC has no authentication. PiProbe and the supported workflow keep the
server bound to `127.0.0.1` on the Pi and carry traffic through SSH. Start the
server using the loopback-only command above, then on the host run:

```powershell
.\jtag\xvc\start-xvc.ps1 [-Pi gusta-bpi] [-Port 2542]
```

The helper opens an authenticated local SSH forward; it does not start XVC or
bind it to a LAN address. In Vivado, connect to `127.0.0.1:2542`. SSH must be
configured to verify the Pi's host key and use an authorized public key. A
missing server, denied SSH access, or failed tunnel must make access fail
closed.

The XVC server accepts only loopback binding; attempts to pass a bind address
are rejected. This keeps its unauthenticated protocol out of reach from LAN
clients even if a caller accidentally supplies an external address.

`install-xvc.sh` enforces that property instead of assuming it: after copying
the binary it runs `--fake --bind 127.0.0.1` on a scratch port and refuses to
report success unless the binary exits 2 (argument rejected). An older binary
that still honours `--bind` therefore cannot be installed as the supported
path. Re-check any installation with:

```sh
timeout 2 /usr/local/libexec/ebaz-xvc-server --fake --port 2549 --bind 127.0.0.1
echo "exit $?  # 2 = rejected; 124 = the server ran and accepted --bind"
```

The check uses `--fake` (no pins, no `/dev/mem`) and a loopback address, so it
never opens a LAN port of its own.
