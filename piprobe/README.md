# PiProbe discovery and announcement

The host-side discovery command supports macOS, Windows, and Linux with Python 3.11 or newer. It listens for `_piprobe._tcp` DNS-SD advertisements on IPv4 and IPv6 mDNS. It does not connect to the advertised endpoint, authenticate, or grant JTAG access. All network advertisements are shown as unverified candidates. This first client supports mDNS and an explicit pinned-peer file; managed unicast DNS-SD records are a documented extension for networks that provide them.
On a Banana Pi or Raspberry Pi, `piprobe announce` publishes only an opaque
stable node ID, API major version, coarse capability, and SSH port. It does not
start the XVC server or enable JTAG. The advertised name is a label for humans —
`PiProbe-<label>`, defaulting to the board's host name, which the SRV record
already publishes — and `--label` replaces it with whatever you call that probe:

```sh
piprobe announce --label banana-pi          # advertises PiProbe-banana-pi
```

The label is lowercased, reduced to dashes and capped at 24 characters; the
opaque node ID in the TXT record remains the identity, so a spoofed name never
grants trust. Set it once in the unit instead of passing it by hand. Use
`piprobe/piprobe-announce.service` as an unprivileged systemd user unit:

```sh
pipx install .
mkdir -p ~/.config/systemd/user
cp piprobe/piprobe-announce.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now piprobe-announce.service
```

If the Pi's distribution does not provide `pipx`, install it using that
distribution's package manager. On Debian-based images, the Python venv/pipx
support packages may require an administrator. To announce after boot before
the user logs in, an administrator must run `loginctl enable-linger <user>`;
without lingering, the service starts with that user's systemd session.

The supported network-control path is the Pi's authenticated SSH transport to
a loopback-only JTAG service. `piprobe openocd` performs it end to end; starting
the service by hand (see `jtag/xvc/README.md`) is only needed for the Vivado
workflow. Discovery itself never starts or tunnels anything.

## Running host OpenOCD

`piprobe openocd` turns a discovered node into a working JTAG adapter for host
OpenOCD. It resolves the node, starts the probe's loopback-only service for the
session, opens the authenticated SSH forward, runs OpenOCD, and tears both down
again: no manual SSH, no remote shell, and nothing is replayed automatically.

```sh
piprobe openocd --node PiProbe-5ef16103
```

Without further arguments it performs the read-only Zynq-7000 TAP scan of the
EBAZ4205 and verifies both IDCODEs. `-f board.cfg` instead runs your own OpenOCD
scripts after the transport configuration, so target scripts stay on the host
machine. `--json` prints a structured result, `--dry-run` shows the plan without
touching the probe, and `--opencd` points at a specific OpenOCD binary when none
is on `PATH`.

For an MCU target, `--swd CLK,DIO` asks the probe-side service for SWD on two
gpiochip0 lines, and `--nrst LINE` lets it drive the target's reset line (the
wire needs a series resistor; without the flag every reset request is refused).
`--swd` needs `-f`, because the target script is what selects the `swd`
transport — the built-in scan is the EBAZ4205 JTAG chain. `-c COMMAND` runs an
OpenOCD command after those scripts, which is how a session reads something
back and exits instead of idling in its gdb server:

```sh
piprobe openocd --node PiProbe-5ef16103 --swd 8,9 --nrst 6 \
  -f jtag/openocd-scripts/target/stm32-swd.cfg \
  -c "init" -c "halt" -c "reg pc" -c "resume" -c "shutdown"
```

Give every readback its own `-c`. Commands that print a result do so when they
come from the command line, while the same commands inside a `-f` script run
without printing (observed with xPack 0.12.0-7 against an STM32H7).

Starting the service needs `sudo -n` on the probe, so the server path needs a
passwordless rule. Install it once:

```sh
sudo install -m 0755 -o root -g root /tmp/ebaz-xvc-server /usr/local/libexec/ebaz-xvc-server
printf '%s\n' 'gusta ALL=(root) NOPASSWD: /usr/local/libexec/ebaz-xvc-server' |
  sudo tee /etc/sudoers.d/ebaz-jtag >/dev/null
sudo chmod 0440 /etc/sudoers.d/ebaz-jtag && sudo visudo -cf /etc/sudoers.d/ebaz-jtag
```

Without that rule the session fails at once with `sudo: a password is required`
and nothing is driven. Replacing the binary afterwards is a root copy too, which
is the only step that still needs a human at the keyboard.

The transport is OpenOCD's `remote_bitbang` over the SSH forward: no stock
Windows OpenOCD build ships the `xvc` adapter driver, while every build has
`remote_bitbang`. OpenSSH must be able to verify the probe's host key without
prompting, because the command runs unattended. The client dials the advertised
name first and then each advertised address, verifying strictly at every step,
and uses the first identity that verifies — so an entry for any of those forms
is enough, and a probe you already reached by name works without an entry for
its address. An identity that fails verification falls through to the next
one; with no trusted form at all the session fails closed rather than asking.

One session per host is enforced with a lock file next to the peer file, and the
probe refuses a second service on its own. A forward that dies before the
session ends fails the run: OpenOCD can log socket errors and still exit 0, so
the client checks the forward itself rather than trusting that exit code, and
reports the session as an interrupted transport.

`--peers-file` and `--aliases-file` select explicit configuration files (the
same options `piprobe discover` takes), which is what makes a session testable
against a scratch configuration without touching the real one.

## Programming a bitstream

A bitstream cannot travel over the bit-level JTAG transport: every pin state is a command byte, so a
2 MB bitstream becomes tens of megabytes of commands and the client's socket fills long before the
payload is through. `piprobe program` keeps the probe feeling like a USB probe by moving the file over
the authenticated SSH connection this tool already owns — no manual `scp`, no shell on the probe — and
letting the probe program the device locally at full speed:

```sh
piprobe program --node banana-pi --file design.bit           # ship, verify, program, report
piprobe program --node banana-pi --file design.bit --check    # ship and verify only
```

The copy is verified by digest on arrival, the runner's own verdict is reported (`result=pass`,
`PCFG_DONE=1`, `exit_status=0`), and the temporary copy is deleted on success and kept for evidence on
failure. The flow on the probe is the qualified one from `jtag/README.md`; `--adapter`, `--runner-dir`
and `--remote-dir` describe a different probe layout, and `--json` gives a structured result.

For read and debug work, `piprobe openocd` remains the right tool. If a host-side script does try to
push a payload over the bit-level transport, the failure is now reported as what it is, with a pointer
to `piprobe program`, instead of a bare socket error.

Install the globally callable `piprobe` command through `pipx`; it exposes a launcher on `PATH` and manages PiProbe's isolated runtime environment. Users do not need to activate or maintain a project virtual environment. The package requires Python 3.11+ without pinning one exact minor version, and never replaces the macOS/OS-provided Python.

From a clone of this repository, install and run discovery with:

```sh
pipx install --python 3.11 .
piprobe discover
```

To select another installed Python that satisfies the minimum, pass that interpreter to `--python`. Without a clone, install straight from the pushed source:

```sh
pipx install "git+https://github.com/gustavosousa2208/ebaz4205-jtag-rpi-setup@main"
```

### Linux hosts without pipx

Some Debian-based images, including the probes themselves, have no `pipx`; installing it needs an
administrator:

```sh
sudo apt install -y pipx python3-venv
pipx ensurepath
```

If `pipx` cannot be installed, use a virtual environment — a plain `pip install --user` is refused on
images that enforce PEP 668 (`error: externally-managed-environment`), and `--break-system-packages`
is not a supported way around it:

```sh
python3 -m venv ~/.venvs/piprobe
~/.venvs/piprobe/bin/pip install .
~/.venvs/piprobe/bin/piprobe --version
```

Whichever route is used, remove an older user-local install first, because it owns the launcher that
`pipx` refuses to overwrite (`File exists at ~/.local/bin/piprobe … Not modifying`). `pip uninstall` is
itself blocked by PEP 668, so remove it by hand and let `pipx` recreate the command:

```sh
rm -f ~/.local/bin/piprobe
rm -rf ~/.local/lib/python3*/site-packages/piprobe ~/.local/lib/python3*/site-packages/piprobe-*.dist-info
pipx install --force "git+https://github.com/gustavosousa2208/ebaz4205-jtag-rpi-setup@main"
head -1 ~/.local/bin/piprobe        # must point into the pipx venv
```

Copying package files into site-packages is **not** a supported install path: it bypasses the metadata
and drifts from the released version. Use one of the installs above.

For development, install the discovery dependency in a project virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r piprobe/requirements.txt
```

`python-zeroconf` is pure Python and does not require Bonjour or Avahi to be installed on the host. Network firewalls may still block multicast on any operating system; use the peer file when that happens.

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

During development, browse for six seconds and print a human-readable inventory:

```sh
python piprobe/discover.py
```

Run the portable unit tests with:

```sh
python -m unittest discover -s piprobe -p 'test_*.py' -v
```

Discovery defaults to a 2.5-second maximum and returns after 600 ms without new
service changes once a candidate appears. Use `--timeout 6` for slower Wi-Fi
and `--json` for scripts. The optional peer file lists explicit endpoints for
networks where multicast is filtered: `~/.config/piprobe/peers.json` on
macOS/Linux and `%APPDATA%\PiProbe\peers.json` on Windows.

```json
{
  "peers": [
    {
      "name": "gusta-bpi",
      "host": "gusta-bpi",
      "port": 22,
      "transport": "ssh",
      "identity": "ssh-ed25519 SHA256:host-key-fingerprint"
    }
  ]
}
```

The peer entry is only a candidate; this command does not validate its identity or connect to it. For an SSH peer, use the fingerprint of the Pi's host key already verified through OpenSSH. `transport` may be `tcp` (the default) or `ssh`. Never put a private key or access token in this file. The announcement does not authenticate hosts or authorize JTAG. For the current security policy and remaining deployment checks, see Bead `ebaz4205-3pb.5`.

## Local names

Whatever a probe advertises, you can call it what you like on this machine:
`aliases.json` next to the peer file maps a node ID or advertised name to the
name to display and to accept in `--node`.

```json
{
  "5ef16103-0cba-4df1-8af4-ddf366da7387": "banana-pi-jtag",
  "PiProbe-dc4817ec": "rpi4-lab"
}
```

```sh
piprobe discover                    # PROBE column shows banana-pi-jtag and rpi4-lab
piprobe openocd --node rpi4-lab     # either name works
```

Aliases are local: nothing is published, they never grant trust, and `--json`
still reports the advertised name and node ID. A typo is reported as an error
rather than silently ignored, because a name that quietly does not apply is
worse than a rejected file. Matches are case-insensitive; aliases must be 1-32
characters of letters, digits, dot, dash or underscore. Use `--aliases-file` to
read a different file.
