#!/bin/sh
# Restart the bridge if the UART is absent at boot or the process exits.
set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
restart_delay=${UART_RESTART_DELAY:-5}
trap 'exit 0' INT TERM

while :; do
    python3 "$script_dir/uart-bridge.py" "$@"
    status=$?
    printf 'uart-supervisor: bridge exited with status %s; retrying in %ss\n' \
        "$status" "$restart_delay" >&2
    sleep "$restart_delay"
done
