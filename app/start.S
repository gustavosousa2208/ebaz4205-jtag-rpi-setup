.syntax unified
.cpu cortex-a9
.arm

.section .text.start, "ax"
.global _start
.type _start, %function
_start:
    cpsid   if
    cps     #0x13
    ldr     sp, =0x0002f000
    bl      main
1:
    wfi
    b       1b
.size _start, . - _start
