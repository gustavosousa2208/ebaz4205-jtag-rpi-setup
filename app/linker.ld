ENTRY(_start)

SECTIONS
{
    /* Run entirely from low OCM so a bad/misconfigured DDR cannot block JTAG tests. */
    . = 0x00010000;
    .text : {
        KEEP(*(.text.start))
        *(.text*)
        *(.rodata*)
    }
    .data : { *(.data*) }
    .bss (NOLOAD) : {
        __bss_start__ = .;
        *(.bss*)
        *(COMMON)
        __bss_end__ = .;
    }
}
