#!/bin/sh
# Installs (or removes) the XVC server for Board 1's JTAG on the Banana Pi. Needs root, so the USER runs it:
#     sudo ~/ebaz4205-jtag/jtag/xvc/install-xvc.sh            install
#     sudo ~/ebaz4205-jtag/jtag/xvc/install-xvc.sh --remove   undo everything this script did
# What it changes (see jtag/xvc/README.md):
#   /usr/local/libexec/ebaz-xvc-server   root-owned copy of ~/ebaz4205-jtag/jtag/xvc/ebaz-xvc-server
#   /etc/sudoers.d/ebaz-xvc              "<user> ALL=(root) NOPASSWD: /usr/local/libexec/ebaz-xvc-server"  (that path only)
set -eu

installed=/usr/local/libexec/ebaz-xvc-server
sudoers=/etc/sudoers.d/ebaz-xvc

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0 [--remove]" >&2
    exit 2
fi

if [ "${1:-}" = "--remove" ]; then
    rm -f "$sudoers" "$installed"
    echo "Removed: $installed and $sudoers"
    exit 0
fi

user=${SUDO_USER:-}
case "$user" in
    ''|*[!A-Za-z0-9_.-]*) echo "Cannot determine a safe invoking username from SUDO_USER." >&2; exit 2 ;;
esac

src=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/ebaz-xvc-server
if [ ! -x "$src" ]; then
    echo "Build $src first: gcc -std=c11 -O2 -Wall -Wextra -Werror -o ebaz-xvc-server xvc-server.c" >&2
    exit 2
fi

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$src" "$installed"

candidate=$(mktemp /tmp/ebaz-xvc-sudoers.XXXXXX)
trap 'rm -f "$candidate"' EXIT HUP INT TERM
printf '%s ALL=(root) NOPASSWD: %s\n' "$user" "$installed" > "$candidate"
chmod 0440 "$candidate"
visudo -cf "$candidate"
install -o root -g root -m 0440 "$candidate" "$sudoers"
visudo -cf "$sudoers"

# Negative access test: the installed server must reject any bind address.
# XVC has no authentication, so a binary that honours --bind can expose JTAG
# control to every host on the LAN. --fake touches no pins or hardware, and the
# address below is loopback, so this check never opens a network port itself.
set +e
timeout 2 "$installed" --fake --port 2549 --bind 127.0.0.1 >/dev/null 2>&1
bind_rc=$?
set -e
if [ "$bind_rc" -ne 2 ]; then
    echo "REFUSING to install: $installed did not reject --bind (exit $bind_rc, expected 2)." >&2
    echo "That binary can serve unauthenticated XVC on a LAN address; rebuild from the current xvc-server.c." >&2
    exit 3
fi

echo "Installed: $installed"
echo "Sudoers:   $sudoers  ($user may run exactly that binary as root without a password)"
echo "Verified:  rejects --bind, so it can only listen on 127.0.0.1"
echo "Check:     sudo -n $installed --fake --port 2542   (then Ctrl-C)"
