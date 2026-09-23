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

echo "Installed: $installed"
echo "Sudoers:   $sudoers  ($user may run exactly that binary as root without a password)"
echo "Check:     sudo -n $installed --fake --port 2542   (then Ctrl-C)"
