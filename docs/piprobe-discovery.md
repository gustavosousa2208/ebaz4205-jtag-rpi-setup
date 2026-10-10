# PiProbe discovery contract

PiProbe discovery finds probe nodes and describes their advertised capabilities. Discovery does not grant permission to control a probe. The Pi-side service and host client live in this project; PiProbe stays outside the `lstty` mainline.

## Discovery paths

Use DNS-Based Service Discovery (DNS-SD) as the common record model, with multiple discovery paths:

1. **mDNS/DNS-SD is the zero-configuration default** on the same local link. It supports automatic discovery without a DNS server, over IPv4 and IPv6.
2. **Unicast DNS-SD is an optional managed-network path.** A network can publish the same service records through ordinary DNS when multicast is filtered. This requires a configured DNS domain and service records or an approved registration mechanism.
3. **Trusted peer entries are the last-resort path** for networks with neither usable multicast nor managed DNS-SD. Store explicit hostnames or addresses and a pinned node identity. Do not probe whole subnets or guess addresses.

These paths can be enabled together. Each result records how it was found so operators can diagnose
why a node appeared. When two paths provably describe one machine — a pinned peer entry and the mDNS
record share a host name, or a peer named by address matches the advertised address — `piprobe
discover` reports a single row whose `FROM` column lists every path (for example `mDNS,peer`), so one
probe is never presented as two. Two records from the same path are not merged: a duplicate there is
a configuration problem worth seeing. Merging never contacts a node; it only compares the host names
and addresses already in the records. Do not assume `.local` name resolution works when mDNS is
blocked.

The host client implements mDNS and the explicit peer file. `piprobe announce`
publishes the Pi-side mDNS record and advertises SSH as its SRV endpoint. A
systemd user unit is provided in `piprobe/piprobe-announce.service`; installing
it under a dedicated unprivileged account avoids a system daemon. For it to run
at boot without an interactive login, the account's systemd lingering must be
enabled by an administrator. Unicast DNS-SD stays as a documented provider for
managed networks; add it when there is a real DNS-SD zone to target.

## Host command and Python runtime

Expose discovery as a globally callable `piprobe discover` command on macOS, Windows, and Linux. Package the host client as an installable Python CLI and use `pipx` to put a small launcher on `PATH`; `pipx` owns an isolated environment for the command, so users do not activate or manage a project virtual environment and PiProbe dependencies cannot pollute the system Python.

Require Python 3.11 or newer, with no exact minor-version pin in the package metadata. The installer must select a compatible interpreter (explicitly on machines whose default Python is older) without replacing the operating system's Python. The user-facing installation should work from the repository/package source and produce the same `piprobe discover` interface on all three operating systems. Do not add OS-specific Bonjour/Avahi dependencies.

The CLI package and pipx entry point are implemented in `pyproject.toml`; use the direct script invocation for development or `pipx install --python 3.11 .` for a user-level command on `PATH`.

mDNS is link-local and does not cross routers. Access-point client isolation, VLAN policy, or host firewalls can also block direct traffic. Discovery cannot bypass those controls; use an explicitly permitted routed path or trusted relay where needed. There is no promise of zero-configuration discovery across an isolated network boundary.

## Records and trust

Advertise one PiProbe DNS-SD service, using `_piprobe._tcp` as the project
service type. The first record points only to the Pi's SSH port. TXT metadata
is limited to protocol major version, opaque stable node ID, coarse capability
tokens, and `transport=ssh`. Never publish credentials, host keys, serial
numbers, or debug data in discovery records. The stable random node ID is stored
with user-only permissions.

The instance name is a label for humans: `PiProbe-<label>`, defaulting to the
board's host name, which the SRV record already publishes, and overridable with
`piprobe announce --label`. It is never an identity, because any device on the
link can claim any name; the TXT node ID plus the verified SSH host key are what
a host trusts. Operators can also give a node a purely local name with
`aliases.json` (see `piprobe/README.md`), which publishes nothing at all.

Treat every advertisement and address as an untrusted candidate. The host confirms the node identity with the control service before accepting detailed hardware metadata or connecting to a capability endpoint. Detailed model, endpoint, and readiness data come from that service, not from unchecked TXT fields. A repeated stable ID with a different key or conflicting endpoint is an identity conflict, not an automatic replacement.

Known limitation, measured on 2026-10-10: a client dials the first address a record carries. A record
that lists an unreachable address first makes a healthy probe look unreachable, and a host whose
`known_hosts` knows the probe by name is not trusted at that address. Both are fixed by dialling every
identity the record offers — advertised name first, then each address — with strict host-key checking
at every step; tracked as Bead `ebaz4205-3pb.12`. Until then, pin the address the record carries in
`known_hosts` and prefer a peer entry when the probe is multi-homed. IPv6-only links are untested:
the client browses both families and the unit tests cover the address handling, but no IPv6-only
network was available (Bead `ebaz4205-3pb.13`).

The discovery state is separate from control authorization. Report candidates as `discovered`, `reachable`, `untrusted`, `trusted`, and, where applicable, `busy` or `ready`. A stale advertisement alone must never imply that the probe is reachable or safe to use.

The service API reports capabilities as named, versioned resources rather than encoding a JTAG-only schema. JTAG is the first capability; SWD and RTT remain future capabilities already tracked in Bead `ebaz4205-s85`.

## Resilience and traffic

A one-shot host command performs a bounded browse on active network interfaces. It returns 600 ms after the latest service change, capped by a 2.5-second default deadline; `--timeout` raises that deadline for slow networks. Record resolution runs outside the mDNS callback thread, so a slow peer cannot serialize processing of other probes. A future long-running client should use DNS-SD TTLs and change notifications to refresh its cache, and retry after interface changes and transient errors with jittered backoff. Expire candidates when their TTL elapses, but distinguish expiry from an explicit service withdrawal in diagnostics.

Reachability checks use short deadlines and do not block discovery of other nodes. Preserve separate discovery-source and last-seen fields so a failed mDNS path can be distinguished from a node that is offline. Avoid aggressive polling and repeated multicast floods when Wi-Fi drops or reconnects.

## Security boundary

Discovery is not authentication or authorization. LAN visibility exposes only
the SSH endpoint and minimal metadata; it never exposes JTAG control. The
supported JTAG path keeps the probe's service bound to `127.0.0.1` and forwards
it through OpenSSH, for both of the protocols that service speaks: XVC for
Vivado and `remote_bitbang` for OpenOCD. The host must verify the saved SSH host
key and authenticate with an authorized user key; unknown or changed host keys
fail closed. Access is authorized by the Pi's SSH account and the existing
narrow sudo rule for the server executable. Do not use passwordless broad sudo
and never bind the service to a LAN address. If SSH authentication, host-key
verification, the forwarding channel, or the local server fails, control is
unavailable. PiProbe discovery alone never connects to an advertised service or
grants trust.

### Threat model and enforcement

| Threat | Enforced by |
|---|---|
| A LAN client connects to the JTAG port directly | `xvc-server.c` binds `INADDR_LOOPBACK` unconditionally in both protocol modes; the binary has no bind-address option, and `install-xvc.sh` refuses to install one that accepts it |
| An operator or agent starts the server on a LAN address | The same rejection; the supported start commands pass only `--protocol`, `--port` and `--rate-khz` |
| A look-alike host uses the tunnel | OpenSSH host-key verification plus public-key authentication; an unknown or changed key fails closed, and the host opens the tunnel rather than the probe accepting clients |
| A discovery record is treated as authorization | TXT metadata carries only protocol major version, opaque node ID, coarse capability tokens, and `transport=ssh`; candidates stay untrusted until the control service confirms identity |
| A probe service escalates privilege | The server runs as root only for one `/dev/mem` PIO page, never interprets client data, and toggles four pins; the sudoers rule pins that one binary path with `NOPASSWD` and grants nothing else |
| Two sessions drive the JTAG pins at once | The server serves one client at a time, and holds an exclusive lock on the pins, so a second server refuses to start (exit 3) and names the holder; the host client holds its own session lock as well |
| An XVC session and an OpenOCD session overlap | The same pin lock: the two protocols are separate processes, and only one can hold the pins. Neither tool is ever pointed at a probe another tool is driving |
| A tool asks the probe to reset or debug the target | `remote_bitbang` reset asserts (`s`/`t`/`u`) and SWD-only commands are refused by closing the connection, because no reset line is wired and the pins are JTAG-only; the read-only scan never issues them |

Residual risk, accepted rather than mitigated here: neither XVC nor
`remote_bitbang` authenticates its client, so any principal with a shell on the
probe can drive JTAG while a server is running; the SSH account is the trust
anchor, and nothing below authorizes individual operations; mDNS records are
unauthenticated and spoofable, which is why a repeated node ID with a different
key or endpoint is an identity conflict rather than a replacement; and the
probe's own sudo logging is the only audit trail for starting the server.

Verify an installation with:

```sh
timeout 2 /usr/local/libexec/ebaz-xvc-server --fake --port 2549 --bind 127.0.0.1
echo "exit $?  # 2 = rejected (safe); 3 = another server holds the pins; 124 = the server ran and accepted --bind"
```

## Standards

- [RFC 6762: Multicast DNS](https://www.rfc-editor.org/rfc/rfc6762.html) defines link-local mDNS behavior.
- [RFC 6763: DNS-Based Service Discovery](https://www.rfc-editor.org/rfc/rfc6763.html) supports the same DNS-SD record model over multicast DNS and conventional unicast DNS; unicast deployments require network configuration.
- [RFC 9665: Service Registration Protocol](https://www.rfc-editor.org/rfc/rfc9665.html) is a possible future managed-network registration path, subject to client and infrastructure support.
