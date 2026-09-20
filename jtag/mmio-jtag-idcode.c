/*
 * Minimal Allwinner H2+/H3 PIO JTAG proof for the EBAZ4205.
 *
 * This intentionally does only reset-to-idle and a 64-bit IDCODE DR scan.
 * It maps the single PIO register page, changes only PC0-PC3, and restores
 * their original mux/data state on every normal exit and handled signal.
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/gpio.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define PIO_PAGE_BASE 0x01c20000u
#define PIO_PAGE_SIZE 0x1000u
#define PC_CFG0_OFF   0x0800u + 2u * 0x24u
#define PC_DAT_OFF    (PC_CFG0_OFF + 0x10u)

#define TDI_BIT 0u
#define TDO_BIT 1u
#define TCK_BIT 2u
#define TMS_BIT 3u
#define JTAG_OUTPUT_MASK ((1u << TDI_BIT) | (1u << TCK_BIT) | (1u << TMS_BIT))
#define JTAG_PIN_MASK    (JTAG_OUTPUT_MASK | (1u << TDO_BIT))

#define ZYNQ_PL_IDCODE  0x13722093u
#define ZYNQ_ARM_IDCODE 0x4ba00477u
static volatile uint32_t *pio;
static uint32_t saved_cfg0;
static uint32_t saved_dat;
static bool configured;
static int gpio_outputs_fd = -1;
static int gpio_input_fd = -1;
static uint64_t half_period_ns;
static uint64_t next_edge_ns;

static uint64_t monotonic_ns(void)
{
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
		perror("clock_gettime");
		exit(2);
	}
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

static void restore_pins(void)
{
	if (!configured)
		return;
	if (gpio_outputs_fd >= 0) {
		close(gpio_outputs_fd);
		gpio_outputs_fd = -1;
	}
	if (gpio_input_fd >= 0) {
		close(gpio_input_fd);
		gpio_input_fd = -1;
	}
	pio[PC_DAT_OFF / 4] = (pio[PC_DAT_OFF / 4] & ~JTAG_PIN_MASK) |
		(saved_dat & JTAG_PIN_MASK);
	__sync_synchronize();
	pio[PC_CFG0_OFF / 4] = saved_cfg0;
	__sync_synchronize();
	configured = false;
}

static void handle_signal(int signo)
{
	restore_pins();
	_exit(128 + signo);
}

static int clock_bit(unsigned int tms, unsigned int tdi)
{
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

static int request_gpio_lines(void)
{
	int chip = open("/dev/gpiochip0", O_RDONLY | O_CLOEXEC);
	if (chip < 0)
		return -1;

	struct gpiohandle_request outputs = { 0 };
	outputs.lineoffsets[0] = 64;
	outputs.lineoffsets[1] = 66;
	outputs.lineoffsets[2] = 67;
	outputs.default_values[0] = 0; /* TDI */
	outputs.default_values[1] = 0; /* TCK */
	outputs.default_values[2] = 1; /* TMS: safe reset level */
	outputs.lines = 3;
	outputs.flags = GPIOHANDLE_REQUEST_OUTPUT;
	strncpy(outputs.consumer_label, "ebaz-mmio-jtag", sizeof(outputs.consumer_label) - 1);
	if (ioctl(chip, GPIO_GET_LINEHANDLE_IOCTL, &outputs) < 0) {
		close(chip);
		return -1;
	}
	gpio_outputs_fd = outputs.fd;

	struct gpiohandle_request input = { 0 };
	input.lineoffsets[0] = 65;
	input.lines = 1;
	input.flags = GPIOHANDLE_REQUEST_INPUT;
	strncpy(input.consumer_label, "ebaz-mmio-jtag", sizeof(input.consumer_label) - 1);
	if (ioctl(chip, GPIO_GET_LINEHANDLE_IOCTL, &input) < 0) {
		close(gpio_outputs_fd);
		gpio_outputs_fd = -1;
		close(chip);
		return -1;
	}
	gpio_input_fd = input.fd;
	close(chip);
	return 0;
}

static uint64_t scan_idcodes(void)
{
	uint64_t value = 0;

	for (unsigned int i = 0; i < 6; ++i)
		clock_bit(1, 0); /* Test-Logic-Reset */
	clock_bit(0, 0);     /* Run-Test/Idle */
	clock_bit(1, 0);     /* Select-DR-Scan */
	clock_bit(0, 0);     /* Capture-DR */
	clock_bit(0, 0);     /* Shift-DR */
	for (unsigned int i = 0; i < 64; ++i)
		value |= (uint64_t)clock_bit(i == 63, 0) << i;
	clock_bit(1, 0);     /* Update-DR */
	clock_bit(0, 0);     /* Run-Test/Idle */
	return value;
}

static bool valid_chain(uint64_t chain)
{
	uint32_t first = (uint32_t)chain;
	uint32_t second = (uint32_t)(chain >> 32);
	return (first == ZYNQ_PL_IDCODE && second == ZYNQ_ARM_IDCODE) ||
		(first == ZYNQ_ARM_IDCODE && second == ZYNQ_PL_IDCODE);
}

int main(int argc, char **argv)
{
	unsigned long rate_khz = 500;
	unsigned long scans = 20;
	char *end;

	if (argc > 1) {
		rate_khz = strtoul(argv[1], &end, 10);
		if (*end || rate_khz == 0 || rate_khz > 10000) {
			fprintf(stderr, "usage: %s [rate-khz 1..10000] [scans]\n", argv[0]);
			return 2;
		}
	}
	if (argc > 2) {
		scans = strtoul(argv[2], &end, 10);
		if (*end || scans == 0 || scans > 100000) {
			fprintf(stderr, "usage: %s [rate-khz 1..10000] [scans]\n", argv[0]);
			return 2;
		}
	}
	half_period_ns = 500000ull / rate_khz;

	int fd = open("/dev/mem", O_RDWR | O_SYNC);
	if (fd < 0) {
		fprintf(stderr, "open /dev/mem: %s (run as root)\n", strerror(errno));
		return 2;
	}
	void *map = mmap(NULL, PIO_PAGE_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED,
		fd, PIO_PAGE_BASE);
	close(fd);
	if (map == MAP_FAILED) {
		fprintf(stderr, "mmap PIO: %s\n", strerror(errno));
		return 2;
	}
	pio = map;

	saved_cfg0 = pio[PC_CFG0_OFF / 4];
	saved_dat = pio[PC_DAT_OFF / 4];
	if (request_gpio_lines() != 0) {
		fprintf(stderr, "request PC0-PC3 through gpiochip0: %s\n", strerror(errno));
		munmap(map, PIO_PAGE_SIZE);
		return 2;
	}
	uint32_t cfg = saved_cfg0;
	for (unsigned int pin = 0; pin < 4; ++pin)
		cfg &= ~(7u << (pin * 4));
	cfg |= 1u << (TDI_BIT * 4); /* function 1: output */
	cfg |= 1u << (TCK_BIT * 4);
	cfg |= 1u << (TMS_BIT * 4);
	pio[PC_DAT_OFF / 4] = saved_dat & ~JTAG_OUTPUT_MASK;
	pio[PC_CFG0_OFF / 4] = cfg; /* PC1 remains function 0: input */
	__sync_synchronize();
	configured = true;
	uint32_t active_cfg0 = pio[PC_CFG0_OFF / 4];
	uint32_t active_dat = pio[PC_DAT_OFF / 4];
	printf("pio_base=0x%08x pc_cfg0_offset=0x%03x pc_dat_offset=0x%03x "
	       "saved_cfg0=0x%08" PRIx32 " active_cfg0=0x%08" PRIx32
	       " saved_dat=0x%08" PRIx32 " active_dat=0x%08" PRIx32
	       " initial_tdo=%u\n",
	       PIO_PAGE_BASE, PC_CFG0_OFF, PC_DAT_OFF, saved_cfg0, active_cfg0,
	       saved_dat, active_dat, (active_dat >> TDO_BIT) & 1u);
	if ((active_cfg0 & 0xffffu) != (cfg & 0xffffu)) {
		fprintf(stderr, "PC0-PC3 mux readback mismatch; refusing to clock JTAG\n");
		restore_pins();
		munmap(map, PIO_PAGE_SIZE);
		return 2;
	}

	struct sigaction sa = { .sa_handler = handle_signal };
	sigemptyset(&sa.sa_mask);
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGHUP, &sa, NULL);

	unsigned long passed = 0;
	uint64_t start = monotonic_ns();
	next_edge_ns = start;
	for (unsigned long i = 0; i < scans; ++i) {
		uint64_t chain = scan_idcodes();
		bool ok = valid_chain(chain);
		printf("scan=%lu chain=0x%016" PRIx64 " first=0x%08" PRIx32
		       " second=0x%08" PRIx32 " result=%s\n", i + 1, chain,
		       (uint32_t)chain, (uint32_t)(chain >> 32), ok ? "pass" : "fail");
		passed += ok;
	}
	uint64_t elapsed = monotonic_ns() - start;
	restore_pins();
	munmap(map, PIO_PAGE_SIZE);

	const uint64_t clocks_per_scan = 6 + 1 + 3 + 64 + 2;
	double effective_khz = (double)(clocks_per_scan * scans) * 1000000.0 /
		(double)elapsed;
	printf("requested_khz=%lu scans=%lu passed=%lu elapsed_ms=%.3f "
	       "effective_khz=%.3f restored=yes\n", rate_khz, scans, passed,
	       (double)elapsed / 1000000.0, effective_khz);
	return passed == scans ? 0 : 1;
}
