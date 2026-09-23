#!/bin/sh
# Restart the bridge if the UART is absent at boot or the process exits.
set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
restart_delay=${UART_RESTART_DELAY:-5}
bridge_pid=

stop() {
    trap - INT TERM
    if [ -n "$bridge_pid" ]; then
        kill -TERM "$bridge_pid" 2>/dev/null || true
        wait "$bridge_pid" 2>/dev/null || true
    fi
    exit 0
}

trap stop INT TERM

while :; do
    python3 "$script_dir/uart-bridge.py" "$@" &
    bridge_pid=$!
    wait "$bridge_pid"
    status=$?
    bridge_pid=
    printf 'uart-supervisor: bridge exited with status %s; retrying in %ss\n' \
        "$status" "$restart_delay" >&2
    sleep "$restart_delay"
done
