# Banana Pi UART bridge

This package owns the UART bridge that runs on `gusta-bpi`. It reads the
EBAZ4205 transmit-only heartbeat from `/dev/ttyS3` at 115200 baud and exposes
replay on TCP 2217 and live data on TCP 2218. The bridge listens on all LAN
interfaces; the live endpoint is read-only by default.

Install the per-user reboot entry on the Banana Pi:

```sh
cd ~/ebaz4205-jtag/uart
./pi-cron.sh status
./pi-cron.sh add
```

The cron entry starts `uart-bridge.py` after reboot and writes to
`uart/logs/uart-bridge.log`. Use `./pi-cron.sh remove` to remove only this
package's entry. The legacy link-test experiments and their original logs are
preserved under `jtag/lab/link-test/`.
