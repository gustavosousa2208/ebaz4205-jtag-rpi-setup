# Xilinx Virtual Cable prototype

This is the existing XVC server prototype for the Banana Pi JTAG probe. XVC
may let Vivado Hardware Manager connect over TCP to a remote JTAG target. The
Vivado compatibility investigation is tracked as Bead `ebaz4205-71t.1`.

The source is `xvc-server.c`; build the Banana Pi binary with:

```sh
gcc -std=c11 -O2 -Wall -Wextra -Werror -o ebaz-xvc-server xvc-server.c
```

Install or remove its narrow sudo rule on the Banana Pi with
`sudo ./install-xvc.sh` or `sudo ./install-xvc.sh --remove`. Do not run XVC and
the production OpenOCD uploader against the probe at the same time. Prototype
logs from the previous `link-test` folder are preserved in
`jtag/lab/link-test/xvc/`.
