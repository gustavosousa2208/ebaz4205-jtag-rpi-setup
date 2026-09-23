CROSS_COMPILE ?= arm-none-eabi-

PROJECT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
APP_DIR := $(PROJECT_DIR)/app
BUILD_DIR := $(PROJECT_DIR)/build
JTAG_DIR := $(PROJECT_DIR)/jtag
TOOLS_DIR := $(PROJECT_DIR)/tools

CC := $(CROSS_COMPILE)gcc
SIZE := $(CROSS_COMPILE)size
CFLAGS := -mcpu=cortex-a9 -marm -O2 -g -ffreestanding -fno-builtin \
	-ffunction-sections -fdata-sections -Wall -Wextra
LDFLAGS := -T $(APP_DIR)/linker.ld -nostdlib -Wl,--gc-sections \
	-Wl,-Map=$(BUILD_DIR)/hello.map

ELF := $(BUILD_DIR)/hello.elf
OBJECTS := $(BUILD_DIR)/start.o $(BUILD_DIR)/hello.o
UPLOAD_ELF ?= $(ELF)
UPLOAD_BITSTREAM ?= $(PROJECT_DIR)/hardware/ebaz_test.bit
UPLOAD_DEPS = $(if $(filter $(ELF),$(UPLOAD_ELF)),$(ELF))
UART_FLAG = $(if $(filter 1 yes true,$(UART)),--uart)

.PHONY: all clean probe upload upload-full upload-pcap bitstream platform

all: $(ELF)

$(ELF): $(OBJECTS) $(APP_DIR)/linker.ld
	$(CC) $(CFLAGS) $(LDFLAGS) $(OBJECTS) -o $@
	$(SIZE) $@

$(BUILD_DIR)/start.o: $(APP_DIR)/start.S | $(BUILD_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/hello.o: $(APP_DIR)/hello.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR):
	mkdir -p $@

upload: $(UPLOAD_DEPS)
	$(JTAG_DIR)/upload-code $(UART_FLAG) "$(UPLOAD_ELF)" "$(UPLOAD_BITSTREAM)"

upload-full: $(UPLOAD_DEPS)
	$(JTAG_DIR)/upload-code --full $(UART_FLAG) "$(UPLOAD_ELF)" "$(UPLOAD_BITSTREAM)"

upload-pcap: $(UPLOAD_DEPS)
	$(JTAG_DIR)/upload-code --pcap $(UART_FLAG) "$(UPLOAD_ELF)" "$(UPLOAD_BITSTREAM)"

probe:
	$(JTAG_DIR)/probe

bitstream:
	$(JTAG_DIR)/upload-bitstream "$(UPLOAD_BITSTREAM)"

platform:
	@test -n "$(XSA)" || { \
		echo "Usage: make platform XSA=/path/to/exported-platform.xsa" >&2; \
		exit 2; \
	}
	$(TOOLS_DIR)/import-platform "$(XSA)"

clean:
	rm -f $(OBJECTS) $(ELF) $(BUILD_DIR)/hello.map
