/*
 * Xilinx Virtual Cable (XVC 1.0) server for the EBAZ4205 through the Banana Pi M2 Zero's Port C GPIO.
 *
 * Lets Vivado / XSDB / hw_server use Board 1's JTAG like any other cable. The GPIO code is taken from the
 * proven ~/ebaz-mmio-jtag/mmio-jtag-idcode.c (same PIO page, same pins, same GPIO-line request that a cold
 * boot needs). Pins: TDI=PC0 (header 19), TDO=PC1 (21), TCK=PC2 (23), TMS=PC3 (24).
 *
 * SECURITY: this runs as root (it needs /dev/mem for one PIO page). It therefore
 *   - binds ONLY to 127.0.0.1 (reach it through an ssh tunnel), one client at a time,
 *   - bounds-checks every length and closes the connection on any protocol violation,
 *   - never executes or interprets client data, it only toggles four pins.
 *
 * usage: ebaz-xvc-server [--port N] [--rate-khz N] [--bind IPV4] [--fake]
 *   --bind   listen on this IPv4 address instead of 127.0.0.1. XVC has no authentication, so
 *            anyone who can reach that address can drive the JTAG pins: trusted LANs only.
 *   --fake   no hardware and no root: TDO is TDI delayed by one bit (for protocol tests).
 */
#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/gpio.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define PIO_PAGE_BASE 0x01c20000u
#define PIO_PAGE_SIZE 0x1000u
#define PC_CFG0_OFF   (0x0800u + 2u * 0x24u)
#define PC_DAT_OFF    (PC_CFG0_OFF + 0x10u)

#define TDI_BIT 0u
#define TDO_BIT 1u
#define TCK_BIT 2u
#define TMS_BIT 3u
#define JTAG_OUTPUT_MASK ((1u << TDI_BIT) | (1u << TCK_BIT) | (1u << TMS_BIT))
#define JTAG_PIN_MASK    (JTAG_OUTPUT_MASK | (1u << TDO_BIT))

#define MAX_VECTOR_BYTES 32768u   /* advertised in getinfo: and enforced on every shift */

static volatile uint32_t *pio;
static uint32_t saved_cfg0, saved_dat;
static bool configured, fake;
static int gpio_outputs_fd = -1, gpio_input_fd = -1;
static uint64_t half_period_ns, next_edge_ns;
static int fake_last_tdi;
static volatile sig_atomic_t stop_requested;

static uint64_t monotonic_ns(void)
{
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
		return 0;
	return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void wait_edge(void)
{
	if (!half_period_ns)
		return;
	next_edge_ns += half_period_ns;
	while (monotonic_ns() < next_edge_ns)
		;
}

static int clock_bit(unsigned int tms, unsigned int tdi)
{
	if (fake) {
		int out = fake_last_tdi;
		(void)tms;
		fake_last_tdi = (int)(tdi & 1u);
		return out;
	}
	uint32_t value = pio[PC_DAT_OFF / 4] & ~JTAG_OUTPUT_MASK;
	value |= (tms & 1u) << TMS_BIT;
	value |= (tdi & 1u) << TDI_BIT;
	pio[PC_DAT_OFF / 4] = value;
	__sync_synchronize();
	wait_edge();
	pio[PC_DAT_OFF / 4] = value | (1u << TCK_BIT);
	__sync_synchronize();
	int tdo = (pio[PC_DAT_OFF / 4] >> TDO_BIT) & 1u;
	wait_edge();
	pio[PC_DAT_OFF / 4] = value;
	__sync_synchronize();
	return tdo;
}

static void restore_pins(void)
{
	if (!configured || fake)
		return;
	if (gpio_outputs_fd >= 0) { close(gpio_outputs_fd); gpio_outputs_fd = -1; }
	if (gpio_input_fd >= 0)   { close(gpio_input_fd);   gpio_input_fd = -1; }
	pio[PC_DAT_OFF / 4] = (pio[PC_DAT_OFF / 4] & ~JTAG_PIN_MASK) | (saved_dat & JTAG_PIN_MASK);
	__sync_synchronize();
	pio[PC_CFG0_OFF / 4] = saved_cfg0;
	__sync_synchronize();
	configured = false;
}

static void handle_signal(int signo)
{
	(void)signo;
	stop_requested = 1;          /* the accept/read loops notice and exit; pins are restored on the way out */
}

/* Same GPIO-line request as the proven tool: after a cold boot the mux registers alone give an all-zero TDO. */
static int request_gpio_lines(void)
{
	int chip = open("/dev/gpiochip0", O_RDONLY | O_CLOEXEC);
	if (chip < 0)
		return -1;
	struct gpiohandle_request outputs = { 0 };
	outputs.lineoffsets[0] = 64;   /* PC0 TDI */
	outputs.lineoffsets[1] = 66;   /* PC2 TCK */
	outputs.lineoffsets[2] = 67;   /* PC3 TMS */
	outputs.default_values[2] = 1; /* TMS high: safe reset level */
	outputs.lines = 3;
	outputs.flags = GPIOHANDLE_REQUEST_OUTPUT;
	strncpy(outputs.consumer_label, "ebaz-xvc", sizeof(outputs.consumer_label) - 1);
	if (ioctl(chip, GPIO_GET_LINEHANDLE_IOCTL, &outputs) < 0) { close(chip); return -1; }
	gpio_outputs_fd = outputs.fd;
	struct gpiohandle_request input = { 0 };
	input.lineoffsets[0] = 65;     /* PC1 TDO */
	input.lines = 1;
	input.flags = GPIOHANDLE_REQUEST_INPUT;
	strncpy(input.consumer_label, "ebaz-xvc", sizeof(input.consumer_label) - 1);
	if (ioctl(chip, GPIO_GET_LINEHANDLE_IOCTL, &input) < 0) {
		close(gpio_outputs_fd); gpio_outputs_fd = -1; close(chip); return -1;
	}
	gpio_input_fd = input.fd;
	close(chip);
	return 0;
}

static int setup_pins(void)
{
	int fd = open("/dev/mem", O_RDWR | O_SYNC);
	if (fd < 0) {
		fprintf(stderr, "open /dev/mem: %s (needs root; use the sudo entry from install-xvc.sh)\n", strerror(errno));
		return -1;
	}
	void *map = mmap(NULL, PIO_PAGE_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, PIO_PAGE_BASE);
	close(fd);
	if (map == MAP_FAILED) {
		fprintf(stderr, "mmap PIO: %s\n", strerror(errno));
		return -1;
	}
	pio = map;
	saved_cfg0 = pio[PC_CFG0_OFF / 4];
	saved_dat = pio[PC_DAT_OFF / 4];
	if (request_gpio_lines() != 0) {
		fprintf(stderr, "request PC0-PC3 through gpiochip0: %s\n", strerror(errno));
		munmap(map, PIO_PAGE_SIZE);
		return -1;
	}
	uint32_t cfg = saved_cfg0;
	for (unsigned int pin = 0; pin < 4; ++pin)
		cfg &= ~(7u << (pin * 4));
	cfg |= (1u << (TDI_BIT * 4)) | (1u << (TCK_BIT * 4)) | (1u << (TMS_BIT * 4)); /* outputs; PC1 stays input */
	pio[PC_DAT_OFF / 4] = (saved_dat & ~JTAG_OUTPUT_MASK) | (1u << TMS_BIT);
	pio[PC_CFG0_OFF / 4] = cfg;
	__sync_synchronize();
	configured = true;
	if ((pio[PC_CFG0_OFF / 4] & 0xffffu) != (cfg & 0xffffu)) {
		fprintf(stderr, "PC0-PC3 mux readback mismatch; refusing to run\n");
		restore_pins();
		return -1;
	}
	return 0;
}

/* ---- socket helpers ---- */
static int read_full(int fd, void *buf, size_t n)
{
	uint8_t *p = buf;
	while (n) {
		ssize_t r = read(fd, p, n);
		if (r == 0)
			return -1;
		if (r < 0) {
			if (errno == EINTR && !stop_requested)
				continue;
			return -1;
		}
		p += r;
		n -= (size_t)r;
	}
	return 0;
}

static int write_full(int fd, const void *buf, size_t n)
{
	const uint8_t *p = buf;
	while (n) {
		ssize_t w = write(fd, p, n);
		if (w <= 0) {
			if (w < 0 && errno == EINTR && !stop_requested)
				continue;
			return -1;
		}
		p += w;
		n -= (size_t)w;
	}
	return 0;
}

static uint32_t le32(const uint8_t *b)
{
	return (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}

/* Serves one client until it disconnects or violates the protocol. */
static void serve(int fd)
{
	static uint8_t buf[2 * MAX_VECTOR_BYTES];    /* tms then tdi */
	static uint8_t tdo[MAX_VECTOR_BYTES];
	for (;;) {
		char cmd[8];
		if (read_full(fd, cmd, 2) != 0)
			return;
		if (memcmp(cmd, "ge", 2) == 0) {                   /* getinfo: */
			if (read_full(fd, cmd, 6) != 0 || memcmp(cmd, "tinfo:", 6) != 0)
				return;
			char reply[64];
			int n = snprintf(reply, sizeof(reply), "xvcServer_v1.0:%u\n", MAX_VECTOR_BYTES);
			if (write_full(fd, reply, (size_t)n) != 0)
				return;
		} else if (memcmp(cmd, "se", 2) == 0) {            /* settck:<period ns> */
			uint8_t period[4];
			if (read_full(fd, cmd, 5) != 0 || memcmp(cmd, "ttck:", 5) != 0 || read_full(fd, period, 4) != 0)
				return;
			if (write_full(fd, period, 4) != 0)             /* accept, the real rate is software-bound */
				return;
		} else if (memcmp(cmd, "sh", 2) == 0) {            /* shift:<bits><tms><tdi> */
			uint8_t nb[4];
			if (read_full(fd, cmd, 4) != 0 || memcmp(cmd, "ift:", 4) != 0 || read_full(fd, nb, 4) != 0)
				return;
			uint32_t bits = le32(nb);
			/* Validate the BIT count first: computing (bits + 7) / 8 in 32 bits wraps for bits near 2^32 and
			 * turns a huge request into a tiny one (found by test_xvc.py, "huge shift"). */
			if (bits == 0 || bits > MAX_VECTOR_BYTES * 8u) {
				fprintf(stderr, "xvc: bad shift length %u bits, closing\n", bits);
				return;
			}
			uint32_t bytes = (bits + 7u) / 8u;
			if (read_full(fd, buf, 2u * bytes) != 0)
				return;
			const uint8_t *tms = buf, *tdi = buf + bytes;
			memset(tdo, 0, bytes);
			for (uint32_t i = 0; i < bits; ++i) {
				int t = clock_bit((tms[i >> 3] >> (i & 7u)) & 1u, (tdi[i >> 3] >> (i & 7u)) & 1u);
				tdo[i >> 3] |= (uint8_t)(t << (i & 7u));
			}
			if (write_full(fd, tdo, bytes) != 0)
				return;
		} else {
			fprintf(stderr, "xvc: unknown command %02x %02x, closing\n", (unsigned char)cmd[0], (unsigned char)cmd[1]);
			return;
		}
	}
}

int main(int argc, char **argv)
{
	unsigned long port = 2542, rate_khz = 1000;
	const char *bind_ip = "127.0.0.1";
	struct in_addr bind_addr = { .s_addr = htonl(INADDR_LOOPBACK) };
	for (int i = 1; i < argc; ++i) {
		char *end;
		if (!strcmp(argv[i], "--fake")) {
			fake = true;
		} else if (!strcmp(argv[i], "--port") && i + 1 < argc) {
			port = strtoul(argv[++i], &end, 10);
			if (*end || port < 1024 || port > 65535) { fprintf(stderr, "bad --port\n"); return 2; }
		} else if (!strcmp(argv[i], "--rate-khz") && i + 1 < argc) {
			rate_khz = strtoul(argv[++i], &end, 10);
			if (*end || rate_khz == 0 || rate_khz > 10000) { fprintf(stderr, "bad --rate-khz (1..10000)\n"); return 2; }
		} else if (!strcmp(argv[i], "--bind") && i + 1 < argc) {
			bind_ip = argv[++i];
			if (inet_pton(AF_INET, bind_ip, &bind_addr) != 1) { fprintf(stderr, "bad --bind (IPv4 address)\n"); return 2; }
		} else {
			fprintf(stderr, "usage: %s [--port N] [--rate-khz N] [--bind IPV4] [--fake]\n", argv[0]);
			return 2;
		}
	}
	half_period_ns = 500000ull / rate_khz;

	struct sigaction sa = { .sa_handler = handle_signal };
	sigemptyset(&sa.sa_mask);
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGHUP, &sa, NULL);
	signal(SIGPIPE, SIG_IGN);

	if (!fake && setup_pins() != 0)
		return 2;

	int srv = socket(AF_INET, SOCK_STREAM, 0);
	if (srv < 0) { perror("socket"); restore_pins(); return 2; }
	int one = 1;
	setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons((uint16_t)port) };
	addr.sin_addr = bind_addr;                              /* loopback unless --bind is given */
	if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) != 0 || listen(srv, 1) != 0) {
		perror("bind/listen");
		restore_pins();
		return 2;
	}
	printf("xvc-server: %s, listening on %s:%lu, rate %lu kHz, max vector %u bytes\n",
	       fake ? "FAKE mode (no hardware)" : "Port C GPIO (TDI=PC0 TDO=PC1 TCK=PC2 TMS=PC3)", bind_ip, port, rate_khz, MAX_VECTOR_BYTES);
	fflush(stdout);

	while (!stop_requested) {
		int c = accept(srv, NULL, NULL);
		if (c < 0) {
			if (errno == EINTR)
				continue;
			perror("accept");
			break;
		}
		setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
		printf("xvc-server: client connected\n");
		fflush(stdout);
		next_edge_ns = monotonic_ns();
		serve(c);
		close(c);
		printf("xvc-server: client disconnected\n");
		fflush(stdout);
	}
	close(srv);
	restore_pins();
	printf("xvc-server: stopped, pins restored\n");
	return 0;
}
