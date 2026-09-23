# Banana Pi UART bridge

This package owns the UART bridge that runs on `gusta-bpi`. It reads the
EBAZ4205 transmit-only heartbeat from `/dev/ttyS3` at 115200 baud and exposes
replay on TCP 2217 and live data on TCP 2218. The bridge listens on all LAN
interfaces; the live endpoint is read-only by default.

The cron entry runs `run-bridge.sh`, which restarts the bridge after a startup
or runtime failure. If `/dev/ttyS3` disappears after startup, the bridge keeps
both listeners open and retries opening the serial device once per second.
Failures and retry messages go to `logs/uart-bridge.log`.

Install the per-user reboot entry on the Banana Pi:

```sh
cd ~/ebaz4205-jtag/uart
./pi-cron.sh status
./pi-cron.sh add
```

The cron entry supervises `uart-bridge.py` after reboot and writes to
`uart/logs/uart-bridge.log`. Use `./pi-cron.sh remove` to remove only this
package's current or legacy tagged entry. The legacy link-test experiments and
their original logs are preserved under `jtag/lab/link-test/`.

Run the Linux PTY integration checks with:

```sh
python3 -m unittest -v uart/test_uart_bridge.py
```

These checks use temporary ports and pseudo-terminals. They do not touch the
production UART or JTAG pins.
