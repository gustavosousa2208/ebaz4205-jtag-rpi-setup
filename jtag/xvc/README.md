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
`sudo ./install-xvc.sh` or `sudo ./install-xvc.sh --remove`. The rule it writes
covers exactly one path (`<user> ALL=(root) NOPASSWD:
/usr/local/libexec/ebaz-xvc-server`), which is what lets `piprobe openocd`
start the service unattended: its remote command uses `sudo -n`, so without the
rule the session stops at `sudo: a password is required`. Never point two
tools at the probe at the same time: the server holds an exclusive lock on the
pins (`/run/ebaz-jtag-pins.lock`, or a per-user path under `/tmp` when it runs
unprivileged in `--fake`), so a second server refuses to start and names the
current holder. That is what keeps a Vivado (XVC) session and an OpenOCD
(remote_bitbang) session from driving the same pins, alongside the documented
rule against running XVC and the production OpenOCD uploader together.
Prototype logs from the previous `link-test` folder are preserved in
`jtag/lab/link-test/xvc/`.

A service started over a pipe must not outlive it. If the server's stdin is a
pipe or a socket — which is what it gets when a tool starts it over ssh — the
server stops as soon as that pipe closes, so a dropped channel cannot leave a
root-owned server holding the pins. A terminal or `/dev/null` stdin is ignored,
so starting it by hand is unchanged; if you do start it from a script, redirect
stdin from `/dev/null` rather than letting it inherit a pipe.

Should a server still be left behind (an older build, or a `kill -9`), the pin
lock names it and the same binary can reap it without a second look:

```sh
sudo -n /usr/local/libexec/ebaz-xvc-server --stop-holder
```

It only ever signals a process whose command line names a server binary, and it
exits 0 once the pins are free. `test_server_lifecycle.py` covers that, the
stdin rule, and the refusal a second server gets while the lock is held.

Making the installed binary group-writable by your account — a one-off
`sudo chown root:<you> /usr/local/libexec/ebaz-xvc-server` plus
`sudo chmod 0775 /usr/local/libexec/ebaz-xvc-server` — turns a rebuild into a
plain copy instead of another `sudo` command. It is a real widening, not a
formality: a binary that runs as root and that your account can overwrite is
passwordless root for that account.

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

The optional second argument selects the hardware server (default
`localhost:3121`). Pass it when another `hw_server` already owns that port,
because a server carrying its own stale XVC target makes `open_hw_target` fail
with "no active target available ... may be locked by another hw_server" before
this endpoint is reached:

```sh
vivado -mode batch -source check-vivado-xvc.tcl -tclargs 127.0.0.1:2543 localhost:3122
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
```

No `adapter speed` here: OpenOCD's remote_bitbang driver reports that it does
not support a configurable speed, so the rate is whatever the link allows. The
full set of commands, including the SWD variant, is in
[`jtag/openocd-scripts/interface/pi-remote-bitbang.cfg`](../openocd-scripts/interface/pi-remote-bitbang.cfg),
which is what `piprobe openocd -f ...` puts in front of a target script.

`piprobe openocd` performs all of that — discovery, tunnel, service lifecycle —
from a single command; add `--swd 8,9` (and `--nrst 6`, with the series resistor
in place) to have it start the service in SWD mode instead of JTAG. The wire
contract follows OpenOCD's
[remote_bitbang driver](https://github.com/openocd-org/openocd/blob/master/src/jtag/drivers/remote_bitbang.c):
`'0'`-`'7'` set TCK/TMS/TDI, `'R'` returns exactly one TDO sample byte, `'Q'`
quits, `'B'`/`'b'` blink and `'Z'`/`'z'` sleep. The client owns the clock, so
`--rate-khz` does not apply in this mode.

Refusals keep the JTAG-only, no-reset promise honest: on a JTAG port an
SWD-only command (`'O'`, `'o'`, `'c'`, `'d'`) and a reset assert (`'s'`, `'t'`,
`'u'`) close the connection instead of being silently accepted, because no
reset line is wired to the target and the pins run JTAG. `test_remote_bitbang.py`
checks those refusals, the response framing and the one-client-at-a-time rule
against a `--fake` server.

## SWD targets (remote_bitbang)

An SWD target needs no extra wiring beyond the pins the JTAG header already
carries: SWCLK on one line, SWDIO on another. Ask for that mode explicitly,
because the port then answers SWD commands and refuses the JTAG-only ones:

```sh
sudo -n /usr/local/libexec/ebaz-xvc-server --protocol remote-bitbang --port 4444 --swd 8,9
```

`--swd CLK,DIO` takes gpiochip0 line numbers; `8,9` is the header the
repository already wires for SWD (PA8/PA9, header 31/33) with the reset line on
line 6. That wiring table, including the 330 ohm series resistor for NRST, is in
[`jtag/adapters/bananapi-m2-zero-swd.cfg`](../adapters/bananapi-m2-zero-swd.cfg).
`--swd 66,67` is the same thing only if the four EBAZ4205 JTAG pins were
repurposed. TDI and TDO are not claimed at all in this mode.

The data line is handed between input and output around every turnaround:
`'O'` drives, `'o'` releases, `'c'` samples. The server also works that out
from the traffic itself — a pin write `'0'`-`'7'` or a `'d'` means the client is
driving, a `'c'` means it has released — because OpenOCD's driver sends its SWD
line reset as plain pin writes and only sometimes precedes it with `'O'`. A
server that waited for that `'O'` clocked the reset onto a floating line, and
the host saw `Error connecting DP: cannot read IDR` for a target that answers
fine. The clock line is never released, because a floating SWCLK in the middle
of a transaction would clock the target. `--swd` is a transport choice, so
passing it with `--protocol xvc` is rejected: Vivado's XVC cable carries its
own JTAG pin map.

`--trace` logs the client's command bytes (to stderr, so they cannot be mistaken
for protocol data), which is how that turnaround bug was found: the byte stream
is visible without a packet capture on either side.

Host OpenOCD drives it with `adapter driver remote_bitbang` as above, plus
`transport select swd`. `'R'` stays refused on such a port, because SWD has no
TDO line to read.

`srst` (`'s'`/`'r'`) stays refused until the operator says a reset line exists
and which one: `--nrst LINE` claims that gpiochip0 line as an output, idles it
deasserted and drives it active low. The wire from that pin to the target's
reset net needs a series resistor; without one, asserting it fights whatever
else drives that net. TRST (`'t'`/`'u'`) is refused on every port, because the
probe has no such line.

`--pins TDI,TDO,TCK,TMS` overrides the four lines for a different header, and
the startup line always prints the map it actually claimed. `test_swd.py`
covers the SWD command set, the turnaround, both refusals and the argument
validation, against `--fake` servers it starts on its own:

```sh
python3 test_swd.py ./ebaz-xvc-server
```

`jtag/openocd-scripts/target/stm32-swd.cfg` declares `srst` but drives nothing
by itself. A target whose firmware takes the SWD pins after boot answers the
first attach after power-up and every later one with `Error connecting DP:
cannot read IDR`; the fix is to hold reset while connecting, which is exactly
what `--nrst LINE` plus `connect_assert_srst` gives:

```sh
openocd -f interface/pi-remote-bitbang.cfg -f target/stm32-swd.cfg \
        -c "connect_assert_srst" -c "init" -c "halt" -c "reg pc" -c shutdown
```

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
echo "exit $?  # 2 = rejected (safe); 3 = another server holds the pins, re-run when idle; 124 = the server ran and accepted --bind"
```

The check uses `--fake` (no pins, no `/dev/mem`) and a loopback address, so it
never opens a LAN port of its own.
