/*
 * JTAG-over-network server for the EBAZ4205 through the Banana Pi M2 Zero's Port C GPIO.
 *
 * One binary, two protocols, selected with --protocol:
 *   xvc             Xilinx Virtual Cable 1.0: Vivado / XSDB / hw_server.
 *   remote-bitbang  OpenOCD's remote_bitbang driver: no stock Windows OpenOCD build
 *                   ships the xvc adapter driver, but every build has remote_bitbang.
 * Either one lets a host tool use Board 1's JTAG like any other cable. The GPIO code is taken from the
 * proven ~/ebaz-mmio-jtag/mmio-jtag-idcode.c (same PIO page, same pins, same GPIO-line request that a cold
 * boot needs). Pins: TDI=PC0 (header 19), TDO=PC1 (21), TCK=PC2 (23), TMS=PC3 (24).
 *
 * SECURITY: this runs as root (it needs /dev/mem for one PIO page). It therefore
 *   - binds ONLY to 127.0.0.1 (reach it through an ssh tunnel), one client at a time,
 *   - holds an exclusive lock on the pins, so an XVC session and an OpenOCD session can never
 *     drive them at the same time: a second server refuses to start instead,
 *   - refuses remote_bitbang reset asserts, because no reset line is wired to the target,
 *   - bounds-checks every length and closes the connection on any protocol violation,
 *   - never executes or interprets client data, it only toggles four pins.
 *
 * usage: ebaz-xvc-server [--protocol xvc|remote-bitbang] [--port N] [--rate-khz N] [--fake]
 *   --port N      default 2542 for xvc, 4444 for remote-bitbang (OpenOCD's default)
 *   --rate-khz N  xvc: internal clock pacing. remote-bitbang: unused, that client owns the clock
 *                 through 'adapter speed' and 'remote_bitbang use_remote_sleep'.
 *   --pins TDI,TDO,TCK,TMS   gpiochip0 line numbers (default 64,65,66,67 = PC0..PC3)
 *   --swd CLK,DIO            remote_bitbang: the port speaks SWD, TDI/TDO are not claimed
 *   --nrst LINE              the port may drive NRST (wired through a series resistor)
 *   --stop-holder  stop the server that still holds the pin lock, then exit
 *   --fake   no hardware and no root: TDO is TDI delayed by one bit (for protocol tests).
 *
 * If stdin is a pipe or socket, the server also stops when that pipe closes: a service started over
 * ssh must not outlive the session that started it, or it keeps the pins and the next session is
 * refused by the lock. A terminal or /dev/null stdin is ignored, so starting it by hand is unchanged.
 */
#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/gpio.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define PIO_PAGE_BASE 0x01c20000u
#define PIO_PAGE_SIZE 0x1000u
#define PIO_CFG0_BASE 0x0800u          /* PIO register block inside that page: PA at +0 */
#define PIO_PORT_STRIDE 0x24u          /* size of one port's register set */
#define PIO_DAT_OFF 0x10u              /* data register inside a port's register set */
#define GPIO_LINES_PER_PORT 32u
#define GPIO_MAX_LINE 223u             /* gpiochip0 exposes PA..PG, 224 lines, on this SoC */
#define GPIO_LINE_NONE 0xffffffffu

/* Roles a port can carry. SWD reuses two of them: its clock is the TCK pin and its data line is the
 * TMS pin, so one pin map serves either transport and only the protocol differs. */
enum { ROLE_TDI = 0, ROLE_TDO, ROLE_TCK, ROLE_TMS, ROLE_COUNT };
#define ROLE_SWCLK ROLE_TCK
#define ROLE_SWDIO ROLE_TMS

#define MAX_VECTOR_BYTES 32768u   /* advertised in getinfo: and enforced on every shift */

static volatile uint32_t *pio;
static bool configured, fake;
static int role_fd[ROLE_COUNT] = { -1, -1, -1, -1 };   /* claim on each role's line */
static bool swdio_driving;                            /* SWDIO is ours to drive right now */
static unsigned role_line[ROLE_COUNT];         /* gpiochip line number per role, NONE when unused */
static bool swd_mode;
static unsigned nrst_line = GPIO_LINE_NONE;    /* only when the operator says a reset wire exists */
static int nrst_fd = -1;
static uint32_t port_cfg[8], port_dat[8];      /* saved register values per port */
static unsigned ports_touched;
static uint64_t half_period_ns, next_edge_ns;
static int fake_last_tdi;
static volatile sig_atomic_t stop_requested;
static const char *protocol = "xvc";     /* "xvc" or "remote-bitbang" */
static char proc_name[32] = "xvc-server";
static unsigned long listen_port;        /* for the pin-lock message */
static int pin_lock_fd = -1;
static bool trace_bytes;                 /* --trace: log the client's command bytes, for wireshark-free debugging */
static int rbb_fake_tck;                 /* remote_bitbang fake-mode edge detector */
static int rbb_fake_tdo, rbb_fake_tdi;
static int fake_swdio_driven, fake_swdio_value;

static uint64_t monotonic_ns(void)
{
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
		return 0;
	return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static int swdio_drive(bool is_output);      /* SWD: hand the data line between input and output */
static void swd_ensure_driving(void);
static void swd_ensure_listening(void);

static void wait_edge(void)
{
	if (!half_period_ns)
		return;
	next_edge_ns += half_period_ns;
	while (monotonic_ns() < next_edge_ns)
		;
}

static unsigned line_port(unsigned line) { return line / GPIO_LINES_PER_PORT; }
static unsigned line_bit(unsigned line) { return line % GPIO_LINES_PER_PORT; }
static uint32_t cfg_off(unsigned port) { return PIO_CFG0_BASE + port * PIO_PORT_STRIDE; }
static uint32_t dat_off(unsigned port) { return cfg_off(port) + PIO_DAT_OFF; }

/* One role at a time, read-modify-write, so pins of the same port that belong to someone else (the
 * console UART shares Port A, for instance) keep their data. */
static void set_role_level(unsigned role, unsigned level)
{
	unsigned line = role_line[role];
	if (line == GPIO_LINE_NONE)
		return;
	uint32_t value = pio[dat_off(line_port(line)) / 4];
	if (level)
		value |= 1u << line_bit(line);
	else
		value &= ~(1u << line_bit(line));
	pio[dat_off(line_port(line)) / 4] = value;
}

static int role_level(unsigned role)
{
	unsigned line = role_line[role];
	if (line == GPIO_LINE_NONE)
		return 0;
	return (int)((pio[dat_off(line_port(line)) / 4] >> line_bit(line)) & 1u);
}

static int clock_bit(unsigned int tms, unsigned int tdi)
{
	if (fake) {
		int out = fake_last_tdi;
		(void)tms;
		fake_last_tdi = (int)(tdi & 1u);
		return out;
	}
	set_role_level(ROLE_TMS, tms & 1u);
	set_role_level(ROLE_TDI, tdi & 1u);
	__sync_synchronize();
	wait_edge();
	set_role_level(ROLE_TCK, 1u);
	__sync_synchronize();
	int tdo = role_level(ROLE_TDO);
	wait_edge();
	set_role_level(ROLE_TCK, 0u);
	__sync_synchronize();
	return tdo;
}

/* ---- remote_bitbang pin access ----
 * OpenOCD drives the clock itself (see 'adapter speed' and 'remote_bitbang use_remote_sleep'), so a pin
 * state is applied immediately here and no pacing is added. */
static void apply_pins(unsigned int tck, unsigned int tms, unsigned int tdi)
{
	if (swd_mode) {
		/* '0'-'7' carries the SWD line reset in this driver: SWDIO must be ours for it to arrive. */
		swd_ensure_driving();
		if (fake)
			fake_swdio_value = (int)(tms & 1u);
	}
	if (fake) {
		/* Same one-bit-delay model as clock_bit, but the edges come from the client: the TDO bit
		 * visible after a rising edge is the TDI sampled at the previous rising edge. */
		if (!rbb_fake_tck && tck) {
			rbb_fake_tdo = rbb_fake_tdi;
			rbb_fake_tdi = (int)(tdi & 1u);
		}
		rbb_fake_tck = (int)(tck & 1u);
		return;
	}
	set_role_level(ROLE_TCK, tck & 1u);
	set_role_level(ROLE_TMS, tms & 1u);
	if (role_line[ROLE_TDI] != GPIO_LINE_NONE)
		set_role_level(ROLE_TDI, tdi & 1u);
	__sync_synchronize();
}

static int tdo_level(void)
{
	if (fake)
		return rbb_fake_tdo;
	return role_level(ROLE_TDO);
}

/* ---- SWD ---- */
/* A write means the client is driving SWDIO, a read means it has released it. OpenOCD's driver sends
 * that explicitly ('O'/'o'), but its line-reset phase goes out as plain pin writes ('0'-'7'), so a
 * server that waits for 'O' can clock a line reset onto a floating SWDIO -- which reaches the target
 * as nothing at all, and the next thing the client sees is "cannot read IDR". Turn the line around
 * from the traffic itself, and keep honouring the explicit commands. */
static void swd_ensure_driving(void)
{
	if (!swdio_driving)
		(void)swdio_drive(true);
}

static void swd_ensure_listening(void)
{
	if (swdio_driving)
		(void)swdio_drive(false);
}

/* 'd' from OpenOCD: drive SWDIO and set the clock in one step. */
static void swd_write(unsigned int swclk, unsigned int swdio)
{
	if (fake) {
		if (fake_swdio_driven)
			fake_swdio_value = (int)(swdio & 1u);
		return;
	}
	swd_ensure_driving();
	set_role_level(ROLE_SWDIO, swdio & 1u);
	set_role_level(ROLE_SWCLK, swclk & 1u);
	__sync_synchronize();
}

/* 'c' from OpenOCD: sample SWDIO, which the client has released for the turnaround. */
static int swdio_level(void)
{
	if (fake)
		return fake_swdio_driven ? fake_swdio_value : 1;   /* released reads as the target's pull-up */
	if (role_line[ROLE_SWDIO] == GPIO_LINE_NONE)
		return 0;
	swd_ensure_listening();
	return role_level(ROLE_SWDIO);
}

/* Reset line, optional: it only exists once the operator has fitted the series resistor and says so
 * with --nrst. Without it every 's' is refused, because driving the pin straight into the target's
 * reset net is what the resistor is there to prevent. */
static int request_nrst_line(void)
{
	if (nrst_line == GPIO_LINE_NONE)
		return 0;
	int chip = open("/dev/gpiochip0", O_RDONLY | O_CLOEXEC);
	if (chip < 0)
		return -1;
	struct gpiohandle_request request = { 0 };
	request.lineoffsets[0] = nrst_line;
	request.default_values[0] = 1;                 /* deasserted: NRST is active low */
	request.lines = 1;
	request.flags = GPIOHANDLE_REQUEST_OUTPUT;
	strncpy(request.consumer_label, "ebaz-xvc", sizeof(request.consumer_label) - 1);
	int rc = ioctl(chip, GPIO_GET_LINEHANDLE_IOCTL, &request);
	close(chip);
	if (rc < 0)
		return -1;
	nrst_fd = request.fd;
	return 0;
}

static int set_nrst(bool asserted)
{
	if (nrst_line == GPIO_LINE_NONE)
		return -1;
	if (fake)
		return 0;
	unsigned port = line_port(nrst_line), bit = line_bit(nrst_line);
	uint32_t value = pio[dat_off(port) / 4];
	if (asserted)
		value &= ~(1u << bit);                     /* active low */
	else
		value |= 1u << bit;
	pio[dat_off(port) / 4] = value;
	__sync_synchronize();
	return 0;
}

/* Exclusive lock on the JTAG pins. An XVC session (Vivado) and a remote_bitbang session (OpenOCD) must
 * never drive the same pins at once, and the lock also catches a server someone forgot about. */
/* Root owns /run; an unprivileged --fake server falls back to a per-user path, so two test runs still
 * exclude each other and --stop-holder finds the same file they used. */
static const char *pin_lock_path(void)
{
	static char fallback[80];
	if (access("/run", W_OK) == 0)
		return "/run/ebaz-jtag-pins.lock";
	snprintf(fallback, sizeof(fallback), "/tmp/ebaz-jtag-pins-%lu.lock", (unsigned long)getuid());
	return fallback;
}

static int acquire_pin_lock(void)
{
	const char *path = pin_lock_path();
	pin_lock_fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0644);
	if (pin_lock_fd < 0) {
		fprintf(stderr, "open %s: %s\n", path, strerror(errno));
		return -1;
	}
	struct flock lock = { .l_type = F_WRLCK, .l_whence = SEEK_SET };
	if (fcntl(pin_lock_fd, F_SETLK, &lock) != 0) {
		char holder[160] = "";
		if (pread(pin_lock_fd, holder, sizeof(holder) - 1, 0) > 0) {
			char *nl = strchr(holder, '\n');
			if (nl)
				*nl = '\0';
		}
		fprintf(stderr, "JTAG pins already in use (%s in %s); refusing to start\n",
			holder[0] ? holder : "held by another process", path);
		close(pin_lock_fd);
		pin_lock_fd = -1;
		return -1;
	}
	if (ftruncate(pin_lock_fd, 0) != 0 ||
	    dprintf(pin_lock_fd, "pid %ld protocol %s port %lu\n", (long)getpid(), protocol, listen_port) < 0) {
		fprintf(stderr, "cannot record the holder in %s: %s\n", path, strerror(errno));
		return -1;
	}
	return 0;
}

static void release_pin_lock(void)
{
	if (pin_lock_fd >= 0) {
		close(pin_lock_fd);          /* closing drops the record lock */
		pin_lock_fd = -1;
	}
}

/* A zombie still answers kill(pid, 0): it has exited and is only waiting to be reaped, which is as
 * stopped as this process can observe. */
static bool process_alive(long pid)
{
	char path[64], buf[512];
	snprintf(path, sizeof(path), "/proc/%ld/stat", pid);
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return false;
	ssize_t n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n <= 0)
		return false;
	buf[n] = '\0';
	/* The command name is parenthesized and may contain spaces, so the state is what follows the last ')'. */
	const char *after = strrchr(buf, ')');
	if (after == NULL || after[1] != ' ' || after[2] == '\0')
		return true;                      /* unparsable: assume alive rather than claim a false success */
	return after[2] != 'Z' && after[2] != 'X';
}

/* Stop a server that is still holding the pin lock, e.g. one left behind when its ssh channel died
 * before this binary learned to watch stdin. Only a process whose command line names a server binary
 * is signalled: the lock file is root-owned, so nothing else can put a pid there, and this must never
 * become a general-purpose killer. Exits 0 when the pins are free afterwards. */
static int stop_holder(void)
{
	const char *path = pin_lock_path();
	char buf[256];
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		printf("no pin lock at %s: no server to stop\n", path);
		return 0;
	}
	ssize_t n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	buf[n > 0 ? n : 0] = '\0';
	long pid = -1;
	if (n <= 0 || sscanf(buf, "pid %ld", &pid) != 1 || pid <= 1) {
		fprintf(stderr, "pin lock %s names no usable pid (%s)\n", path, n > 0 ? buf : "empty");
		return 2;
	}
	char proc[64], cmdline[512];
	snprintf(proc, sizeof(proc), "/proc/%ld/cmdline", pid);
	fd = open(proc, O_RDONLY | O_CLOEXEC);
	if (fd < 0 || !process_alive(pid)) {
		if (fd >= 0)
			close(fd);
		printf("pid %ld is already gone, nothing to stop\n", pid);
		return 0;
	}
	n = read(fd, cmdline, sizeof(cmdline) - 1);
	close(fd);
	cmdline[n > 0 ? n : 0] = '\0';
	for (ssize_t i = 0; i < n - 1; ++i) {
		if (cmdline[i] == '\0')
			cmdline[i] = ' ';
	}
	if (!strstr(cmdline, "xvc-server")) {
		fprintf(stderr, "pid %ld (%s) is not a probe server; refusing to signal it\n", pid, cmdline);
		return 2;
	}
	if (kill((pid_t)pid, SIGTERM) != 0) {
		fprintf(stderr, "cannot signal pid %ld: %s\n", pid, strerror(errno));
		return 2;
	}
	for (int i = 0; i < 50; ++i) {
		struct timespec pause = { .tv_sec = 0, .tv_nsec = 100000000L };
		nanosleep(&pause, NULL);
		if (!process_alive(pid)) {
			printf("stopped pid %ld (%s)\n", pid, cmdline);
			return 0;
		}
	}
	fprintf(stderr, "pid %ld is still alive after SIGTERM\n", pid);
	return 2;
}

static void remember_port(unsigned port)
{
	if (!(ports_touched & (1u << port))) {
		port_cfg[port] = pio[cfg_off(port) / 4];
		port_dat[port] = pio[dat_off(port) / 4];
		ports_touched |= 1u << port;
	}
}

/* Restore only the pins this server owns: another user of the same port (the console UART shares Port
 * A, for instance) keeps its configuration. */
static void restore_pins(void)
{
	if (!configured || fake)
		return;
	for (unsigned role = 0; role < ROLE_COUNT; ++role) {
		if (role_fd[role] >= 0) {
			close(role_fd[role]);
			role_fd[role] = -1;
		}
	}
	if (nrst_fd >= 0) {
		close(nrst_fd);
		nrst_fd = -1;
	}
	for (unsigned port = 0; port < 8; ++port) {
		if (!(ports_touched & (1u << port)))
			continue;
		uint32_t bits = 0, nibbles = 0;
		for (unsigned role = 0; role < ROLE_COUNT; ++role) {
			unsigned line = role_line[role];
			if (line == GPIO_LINE_NONE || line_port(line) != port)
				continue;
			bits |= 1u << line_bit(line);
			nibbles |= 0xfu << (line_bit(line) * 4);
		}
		if (nrst_line != GPIO_LINE_NONE && line_port(nrst_line) == port) {
			bits |= 1u << line_bit(nrst_line);
			nibbles |= 0xfu << (line_bit(nrst_line) * 4);
		}
		uint32_t data = pio[dat_off(port) / 4];
		pio[dat_off(port) / 4] = (data & ~bits) | (port_dat[port] & bits);
		uint32_t cfg = pio[cfg_off(port) / 4];
		pio[cfg_off(port) / 4] = (cfg & ~nibbles) | (port_cfg[port] & nibbles);
	}
	__sync_synchronize();
	configured = false;
}

static void handle_signal(int signo)
{
	(void)signo;
	stop_requested = 1;          /* the accept/read loops notice and exit; pins are restored on the way out */
}

/* Roles this server drives, given the transport and the direction SWDIO currently holds. */
static bool role_is_output(unsigned role)
{
	if (role_line[role] == GPIO_LINE_NONE)
		return false;
	if (role == ROLE_TDO)
		return false;                                  /* always the target's output */
	if (swd_mode) {
		if (role == ROLE_SWDIO)
			return swdio_driving;
		if (role == ROLE_TDI)
			return false;                          /* nothing is wired to it in SWD mode */
	}
	return true;
}

/* Same GPIO-line request as the proven tool: after a cold boot the mux registers alone give an all-zero
 * TDO, so the line has to be claimed through gpiochip for reads to work. */
static int request_role_line(unsigned role)
{
	int chip = open("/dev/gpiochip0", O_RDONLY | O_CLOEXEC);
	if (chip < 0)
		return -1;
	struct gpiohandle_request request = { 0 };
	request.lineoffsets[0] = role_line[role];
	/* TMS and SWDIO idle high, which is the target's own reset/idle level. */
	request.default_values[0] = (role == ROLE_TMS) ? 1u : 0u;
	request.lines = 1;
	request.flags = role_is_output(role) ? GPIOHANDLE_REQUEST_OUTPUT : GPIOHANDLE_REQUEST_INPUT;
	strncpy(request.consumer_label, "ebaz-xvc", sizeof(request.consumer_label) - 1);
	int rc = ioctl(chip, GPIO_GET_LINEHANDLE_IOCTL, &request);
	close(chip);
	if (rc < 0)
		return -1;
	role_fd[role] = request.fd;
	return 0;
}

static int request_gpio_lines(void)
{
	for (unsigned role = 0; role < ROLE_COUNT; ++role) {
		if (role_line[role] == GPIO_LINE_NONE)
			continue;
		if (request_role_line(role) != 0)
			return -1;
	}
	return 0;
}

/* OpenOCD calls this around every SWD turnaround. Only the data line is re-requested: releasing the
 * clock pin as well could clock the target with a floating level mid-transaction. */
static int swdio_drive(bool is_output)
{
	if (fake) {
		fake_swdio_driven = is_output ? 1 : 0;
		return 0;
	}
	if (role_line[ROLE_SWDIO] == GPIO_LINE_NONE)
		return -1;
	if (is_output == swdio_driving)
		return 0;
	if (role_fd[ROLE_SWDIO] >= 0) {
		close(role_fd[ROLE_SWDIO]);
		role_fd[ROLE_SWDIO] = -1;
	}
	swdio_driving = is_output;
	if (request_role_line(ROLE_SWDIO) != 0) {
		fprintf(stderr, "%s: cannot switch SWDIO to %s: %s\n", proc_name,
			is_output ? "output" : "input", strerror(errno));
		return -1;
	}
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
	for (unsigned role = 0; role < ROLE_COUNT; ++role) {
		unsigned line = role_line[role];
		if (line == GPIO_LINE_NONE)
			continue;
		if (line > GPIO_MAX_LINE) {
			fprintf(stderr, "line %u is outside gpiochip0 (0..%u)\n", line, GPIO_MAX_LINE);
			munmap(map, PIO_PAGE_SIZE);
			return -1;
		}
		remember_port(line_port(line));
	}
	if (nrst_line != GPIO_LINE_NONE && nrst_line > GPIO_MAX_LINE) {
		fprintf(stderr, "line %u is outside gpiochip0 (0..%u)\n", nrst_line, GPIO_MAX_LINE);
		munmap(map, PIO_PAGE_SIZE);
		return -1;
	}
	if (request_gpio_lines() != 0 || request_nrst_line() != 0) {
		fprintf(stderr, "request the configured lines through gpiochip0: %s\n", strerror(errno));
		munmap(map, PIO_PAGE_SIZE);
		return -1;
	}
	if (nrst_line != GPIO_LINE_NONE)
		remember_port(line_port(nrst_line));
	uint32_t expected[8];
	for (unsigned port = 0; port < 8; ++port)
		expected[port] = pio[cfg_off(port) / 4];
	for (unsigned role = 0; role < ROLE_COUNT; ++role) {
		if (!role_is_output(role))
			continue;
		unsigned line = role_line[role];
		unsigned port = line_port(line), shift = line_bit(line) * 4;
		uint32_t cfg = pio[cfg_off(port) / 4];
		cfg &= ~(7u << shift);
		cfg |= 1u << shift;                     /* function 1 drives the pin */
		pio[cfg_off(port) / 4] = cfg;
		expected[port] = cfg;
	}
	if (nrst_line != GPIO_LINE_NONE) {
		unsigned port = line_port(nrst_line), shift = line_bit(nrst_line) * 4;
		uint32_t cfg = pio[cfg_off(port) / 4];
		cfg &= ~(7u << shift);
		cfg |= 1u << shift;
		pio[cfg_off(port) / 4] = cfg;
		expected[port] = cfg;
	}
	__sync_synchronize();
	configured = true;
	for (unsigned port = 0; port < 8; ++port) {
		if (!(ports_touched & (1u << port)))
			continue;
		uint32_t mask = 0;
		for (unsigned role = 0; role < ROLE_COUNT; ++role) {
			unsigned line = role_line[role];
			if (line != GPIO_LINE_NONE && line_port(line) == port)
				mask |= 0xfu << (line_bit(line) * 4);
		}
		if (nrst_line != GPIO_LINE_NONE && line_port(nrst_line) == port)
			mask |= 0xfu << (line_bit(nrst_line) * 4);
		if ((pio[cfg_off(port) / 4] & mask) != (expected[port] & mask)) {
			fprintf(stderr, "port %u mux readback mismatch; refusing to run\n", port);
			restore_pins();
			return -1;
		}
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

/* Serves one remote_bitbang client (host OpenOCD) until it quits or breaks the protocol.
 * Wire contract: OpenOCD src/jtag/drivers/remote_bitbang.c. Every 'R' must produce exactly one response
 * byte and the stream must carry no other bytes, otherwise the client loses framing. Responses are
 * flushed at the end of each read chunk: the client always flushes its command batch before it blocks
 * reading a sample, so this cannot deadlock and still costs one packet per batch. */
static int serve_remote_bitbang(int fd)
{
	static uint8_t in[256], out[256];
	size_t pending = 0;
	for (;;) {
		ssize_t n = read(fd, in, sizeof(in));
		if (n == 0)
			return 0;                                  /* client closed the connection */
		if (n < 0) {
			if (errno == EINTR && !stop_requested)
				continue;
			return -1;
		}
		for (ssize_t i = 0; i < n; ++i) {
			unsigned char c = in[i];
			if (trace_bytes) {
				/* stderr, so the trace cannot be mistaken for protocol data */
				if (i == 0)
					fprintf(stderr, "rb[%zd]", n);
				fprintf(stderr, " %02x", c);
			}
			if (c >= '0' && c <= '7') {
				apply_pins((c >> 2) & 1u, (c >> 1) & 1u, c & 1u);
			} else if (c == 'r') {
				/* both reset lines deasserted */
				set_nrst(false);
			} else if (c == 's') {
				if (nrst_line == GPIO_LINE_NONE) {
					fprintf(stderr, "%s: reset assert refused, no reset line is wired to the target "
					                "(start with --nrst LINE once the series resistor is in place)\n", proc_name);
					return -1;
				}
				set_nrst(true);
			} else if (c == 't' || c == 'u') {
				fprintf(stderr, "%s: reset assert 0x%02x refused, this probe has no TRST line\n", proc_name, c);
				return -1;
			} else if (c == 'O' || c == 'o' || c == 'c' || (c >= 'd' && c <= 'd' + 3)) {
				if (!swd_mode) {
					fprintf(stderr, "%s: SWD command 0x%02x rejected, this port runs JTAG "
					                "(start the server with --swd CLK,DIO for an SWD target)\n", proc_name, c);
					return -1;
				}
				if (c == 'O') {
					if (swdio_drive(true) != 0)
						return -1;
				} else if (c == 'o') {
					if (swdio_drive(false) != 0)
						return -1;
				} else if (c == 'c') {
					out[pending++] = swdio_level() ? '1' : '0';
				} else {
					swd_write((unsigned)((c - 'd') >> 1) & 1u, (unsigned)(c - 'd') & 1u);
				}
			} else if (c == 'R') {
				if (swd_mode) {
					fprintf(stderr, "%s: TDO read refused, an SWD port has no TDO line\n", proc_name);
					return -1;
				}
				out[pending++] = tdo_level() ? '1' : '0';
			} else if (c == 'B' || c == 'b') {
				/* status LED on/off: this probe has no LED */
			} else if (c == 'Z') {
				struct timespec ms = { .tv_sec = 0, .tv_nsec = 1000000L };   /* 1 ms */
				nanosleep(&ms, NULL);
			} else if (c == 'z') {
				struct timespec us = { .tv_sec = 0, .tv_nsec = 1000L };      /* 1 us */
				nanosleep(&us, NULL);
			} else if (c == 'Q') {
				if (pending && write_full(fd, out, pending) != 0)
					return -1;
				return 0;
			} else {
				fprintf(stderr, "%s: unknown command 0x%02x, closing\n", proc_name, c);
				return -1;
			}
			if (pending == sizeof(out)) {
				if (write_full(fd, out, pending) != 0)
					return -1;
				pending = 0;
			}
		}
		if (trace_bytes && n > 0)
			fprintf(stderr, "\n");
		if (pending) {
			if (write_full(fd, out, pending) != 0)
				return -1;
			pending = 0;
		}
	}
}

/* Wait for a client, or notice that whatever started us is gone.
 *
 * A service started over ssh must die with the session that started it: piprobe starts one that way,
 * and an orphan would keep the pins claimed and make the next session fail on the pin lock. Only a
 * pipe or socket stdin is watched -- a terminal or /dev/null must not be read as "the parent went away",
 * so starting the server by hand from a shell behaves exactly as before.
 */
static int wait_for_client(int srv)
{
	struct pollfd fds[2] = { { .fd = srv, .events = POLLIN }, { .fd = -1, .events = POLLIN } };
	struct stat st;
	bool watch_stdin = fstat(STDIN_FILENO, &st) == 0 && (S_ISFIFO(st.st_mode) || S_ISSOCK(st.st_mode));
	nfds_t count = 1;
	if (watch_stdin) {
		fds[1].fd = STDIN_FILENO;
		count = 2;
	}
	for (;;) {
		int rc = poll(fds, count, -1);
		if (rc < 0) {
			if (errno == EINTR && !stop_requested)
				continue;
			return -1;
		}
		if (watch_stdin && (fds[1].revents & (POLLIN | POLLHUP | POLLERR))) {
			char byte;
			if (read(STDIN_FILENO, &byte, 1) <= 0) {
				printf("%s: stdin closed, stopping (the session that started me is gone)\n", proc_name);
				fflush(stdout);
				return -1;
			}
		}
		if (fds[0].revents & POLLIN)
			return 0;
		if (stop_requested)
			return -1;
	}
}

/* Parse a comma-separated list of gpiochip0 line numbers ("64,65,66,67"). */
static int parse_lines(const char *spec, unsigned *out, unsigned count)
{
	const char *p = spec;
	for (unsigned i = 0; i < count; ++i) {
		char *end;
		unsigned long value = strtoul(p, &end, 10);
		if (end == p || value > GPIO_MAX_LINE)
			return -1;
		out[i] = (unsigned)value;
		if (i + 1 < count) {
			if (*end != ',')
				return -1;
			p = end + 1;
		} else if (*end) {
			return -1;
		}
	}
	return 0;
}

int main(int argc, char **argv)
{
	unsigned long port = 0, rate_khz = 1000;
	bool port_given = false;
	/* Default map: the four Port C pins the header carries, in role order TDI, TDO, TCK, TMS. */
	unsigned jtag_pins[ROLE_COUNT] = { 64, 65, 66, 67 };
	bool swd_given = false;
	bool stop_holder_only = false;
	for (int i = 1; i < argc; ++i) {
		char *end;
		if (!strcmp(argv[i], "--fake")) {
			fake = true;
		} else if (!strcmp(argv[i], "--trace")) {
			trace_bytes = true;
		} else if (!strcmp(argv[i], "--stop-holder")) {
			stop_holder_only = true;
		} else if (!strcmp(argv[i], "--pins") && i + 1 < argc) {
			if (parse_lines(argv[++i], jtag_pins, ROLE_COUNT) != 0) {
				fprintf(stderr, "bad --pins (want TDI,TDO,TCK,TMS as gpiochip0 line numbers)\n");
				return 2;
			}
		} else if (!strcmp(argv[i], "--swd") && i + 1 < argc) {
			unsigned swd_lines[2];
			if (parse_lines(argv[++i], swd_lines, 2) != 0) {
				fprintf(stderr, "bad --swd (want SWCLK,SWDIO as gpiochip0 line numbers)\n");
				return 2;
			}
			jtag_pins[ROLE_TCK] = swd_lines[0];
			jtag_pins[ROLE_TMS] = swd_lines[1];
			swd_given = true;
		} else if (!strcmp(argv[i], "--nrst") && i + 1 < argc) {
			unsigned long line = strtoul(argv[++i], &end, 10);
			if (*end || line > GPIO_MAX_LINE) {
				fprintf(stderr, "bad --nrst (want a gpiochip0 line number)\n");
				return 2;
			}
			nrst_line = (unsigned)line;
		} else if (!strcmp(argv[i], "--protocol") && i + 1 < argc) {
			protocol = argv[++i];
			if (strcmp(protocol, "xvc") != 0 && strcmp(protocol, "remote-bitbang") != 0) {
				fprintf(stderr, "bad --protocol (xvc|remote-bitbang)\n");
				return 2;
			}
		} else if (!strcmp(argv[i], "--port") && i + 1 < argc) {
			port = strtoul(argv[++i], &end, 10);
			if (*end || port < 1024 || port > 65535) { fprintf(stderr, "bad --port\n"); return 2; }
			port_given = true;
		} else if (!strcmp(argv[i], "--rate-khz") && i + 1 < argc) {
			rate_khz = strtoul(argv[++i], &end, 10);
			if (*end || rate_khz == 0 || rate_khz > 10000) { fprintf(stderr, "bad --rate-khz (1..10000)\n"); return 2; }
		} else {
			fprintf(stderr, "usage: %s [--protocol xvc|remote-bitbang] [--port N] [--rate-khz N]\n"
			                "          [--pins TDI,TDO,TCK,TMS] [--swd SWCLK,SWDIO] [--nrst LINE] [--fake]\n",
			        argv[0]);
			return 2;
		}
	}
	bool rbb = strcmp(protocol, "remote-bitbang") == 0;
	swd_mode = swd_given;
	if (stop_holder_only)
		return stop_holder();        /* nothing else runs: no pins, no socket, no lock */
	if (swd_mode && !rbb) {
		fprintf(stderr, "--swd selects the remote-bitbang transport; XVC carries its own JTAG pin map\n");
		return 2;
	}
	/* SWD has no separate data-in and data-out pin: TDI and TDO must not be claimed at all, and the
	 * data line is the TMS pin, which swdio_drive() hands back and forth. */
	role_line[ROLE_TDI] = swd_mode ? GPIO_LINE_NONE : jtag_pins[ROLE_TDI];
	role_line[ROLE_TDO] = swd_mode ? GPIO_LINE_NONE : jtag_pins[ROLE_TDO];
	role_line[ROLE_TCK] = jtag_pins[ROLE_TCK];
	role_line[ROLE_TMS] = jtag_pins[ROLE_TMS];
	if (!port_given)
		port = rbb ? 4444 : 2542;
	listen_port = port;
	snprintf(proc_name, sizeof(proc_name), "%s-server", protocol);
	half_period_ns = 500000ull / rate_khz;
	char transport[160];
	if (swd_mode)
		snprintf(transport, sizeof(transport), "SWD lines (SWCLK=%u SWDIO=%u)",
		         role_line[ROLE_SWCLK], role_line[ROLE_SWDIO]);
	else
		snprintf(transport, sizeof(transport), "JTAG lines (TDI=%u TDO=%u TCK=%u TMS=%u)",
		         role_line[ROLE_TDI], role_line[ROLE_TDO], role_line[ROLE_TCK], role_line[ROLE_TMS]);
	if (nrst_line != GPIO_LINE_NONE) {
		size_t used = strlen(transport);
		snprintf(transport + used, sizeof(transport) - used, ", NRST line %u", nrst_line);
	}
	char pin_desc[192];
	snprintf(pin_desc, sizeof(pin_desc), "%sgpiochip0 %s",
	         fake ? "FAKE mode (no hardware), " : "", transport);
	const char *pins = pin_desc;

	struct sigaction sa = { .sa_handler = handle_signal };
	sigemptyset(&sa.sa_mask);
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGHUP, &sa, NULL);
	signal(SIGPIPE, SIG_IGN);

	/* Before the pins are muxed: a second server, whatever protocol it speaks, must never drive them.
	 * The lock refusal exits 3, deliberately not the 2 used for a rejected argument, so
	 * install-xvc.sh's bind-rejection gate cannot mistake "pins busy" for "bind address refused". */
	if (acquire_pin_lock() != 0)
		return 3;
	if (!fake && setup_pins() != 0) {
		release_pin_lock();
		return 2;
	}

	int srv = socket(AF_INET, SOCK_STREAM, 0);
	if (srv < 0) { perror("socket"); restore_pins(); release_pin_lock(); return 2; }
	int one = 1;
	setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons((uint16_t)port) };
	addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
	if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) != 0 || listen(srv, 1) != 0) {
		perror("bind/listen");
		restore_pins();
		release_pin_lock();
		return 2;
	}
	if (rbb)
		printf("%s: %s, listening on 127.0.0.1:%lu, clock driven by the client\n", proc_name, pins, port);
	else
		printf("%s: %s, listening on 127.0.0.1:%lu, rate %lu kHz, max vector %u bytes\n",
		       proc_name, pins, port, rate_khz, MAX_VECTOR_BYTES);
	fflush(stdout);

	while (!stop_requested) {
		if (wait_for_client(srv) != 0)
			break;
		int c = accept(srv, NULL, NULL);
		if (c < 0) {
			if (errno == EINTR)
				continue;
			perror("accept");
			break;
		}
		setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
		printf("%s: client connected\n", proc_name);
		fflush(stdout);
		next_edge_ns = monotonic_ns();
		if (rbb)
			serve_remote_bitbang(c);
		else
			serve(c);
		close(c);
		printf("%s: client disconnected\n", proc_name);
		fflush(stdout);
	}
	close(srv);
	restore_pins();
	release_pin_lock();
	printf("%s: stopped, pins restored\n", proc_name);
	return 0;
}
