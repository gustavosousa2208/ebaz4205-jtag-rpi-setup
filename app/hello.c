#include <stdint.h>

#define UART1_BASE       0xE0001000u
#define UART_STATUS      (*(volatile uint32_t *)(UART1_BASE + 0x2Cu))
#define UART_FIFO        (*(volatile uint32_t *)(UART1_BASE + 0x30u))
#define UART_SR_TXFULL   (1u << 4)

static void uart_putc(char c)
{
    while ((UART_STATUS & UART_SR_TXFULL) != 0u) {
    }
    UART_FIFO = (uint32_t)(uint8_t)c;
}

static void uart_puts(const char *text)
{
    while (*text != '\0') {
        if (*text == '\n') {
            uart_putc('\r');
        }
        uart_putc(*text++);
    }
}

static void uart_put_u32(uint32_t value)
{
    char digits[10];
    unsigned count = 0;

    do {
        digits[count++] = (char)('0' + (value % 10u));
        value /= 10u;
    } while (value != 0u);

    while (count != 0u) {
        uart_putc(digits[--count]);
    }
}

static void delay(void)
{
    for (volatile uint32_t i = 0; i < 100000000u; ++i) {
        __asm__ volatile ("nop");
    }
}

void main(void)
{
    uint32_t heartbeat = 0;

    for (;;) {
        uart_puts("Hello world from the EBAZ4205 Cortex-A9! heartbeat=");
        uart_put_u32(heartbeat++);
        uart_puts("\n");
        delay();
    }
}
