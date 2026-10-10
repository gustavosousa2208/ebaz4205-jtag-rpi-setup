# PiProbe discovery and announcement

The host-side discovery command supports macOS, Windows, and Linux with Python 3.11 or newer. It listens for `_piprobe._tcp` DNS-SD advertisements on IPv4 and IPv6 mDNS. It does not connect to the advertised endpoint, authenticate, or grant JTAG access. All network advertisements are shown as unverified candidates. This first client supports mDNS and an explicit pinned-peer file; managed unicast DNS-SD records are a documented extension for networks that provide them.
On a Banana Pi or Raspberry Pi, `piprobe announce` publishes only an opaque
stable node ID, API major version, coarse capability, and SSH port. It does not
start the XVC server or enable JTAG. Use `piprobe/piprobe-announce.service` as
an unprivileged systemd user unit:

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

The transport is OpenOCD's `remote_bitbang` over the SSH forward: no stock
Windows OpenOCD build ships the `xvc` adapter driver, while every build has
`remote_bitbang`. OpenSSH must be able to verify the probe's host key without
prompting, because the command runs unattended. One session per host is enforced
with a lock file next to the peer file, and the probe refuses a second service on
its own.

Install the globally callable `piprobe` command through `pipx`; it exposes a launcher on `PATH` and manages PiProbe's isolated runtime environment. Users do not need to activate or maintain a project virtual environment. The package requires Python 3.11+ without pinning one exact minor version, and never replaces the macOS/OS-provided Python.

From a clone of this repository, install and run discovery with:

```sh
pipx install --python 3.11 .
piprobe discover
```

To select another installed Python that satisfies the minimum, pass that interpreter to `--python`.

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
