#!/bin/bash
# Manages the ONE cron line this project owns on the Banana Pi (user gusta, no root needed):
# it starts the UART bridge after a reboot. The line carries the tag below so it can be found and
# removed without touching any other crontab entry.
#   pi-cron.sh status | add | remove
# Every change made with this script must also be recorded in uart/README.md.
set -euo pipefail

tag='ebaz4205-jtag:uart-bridge'
legacy_tag='link-test:uart-bridge'
# `cd ...; cmd &` (not `cd && cmd &`): backgrounding a && chain would keep cron's output pipe open.
line='@reboot sleep 20; cd "$HOME/ebaz4205-jtag/uart" || exit 0; setsid nohup ./run-bridge.sh >> logs/uart-bridge.log 2>&1 < /dev/null &  # '"$tag"
current=$(crontab -l 2>/dev/null || true)
strip() { printf '%s\n' "$current" | grep -v -E "$tag|$legacy_tag" || true; }

case "${1:-status}" in
    status)
        crontab -l 2>/dev/null | grep -E "$tag|$legacy_tag" || echo "cron line not installed" ;;
    add)
        { strip; echo "$line"; } | grep -v '^$' | crontab -
        echo "cron line installed:"; crontab -l | grep "$tag" ;;
    remove)
        rest=$(strip)
        if [ -z "$(printf '%s' "$rest" | tr -d '[:space:]')" ]; then
            crontab -r 2>/dev/null || true
        else
            printf '%s\n' "$rest" | crontab -
        fi
        echo "cron line removed (other entries untouched)" ;;
    *)
        echo "usage: $0 status | add | remove"; exit 2 ;;
esac
