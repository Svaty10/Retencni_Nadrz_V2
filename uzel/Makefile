# ==============================================================================
# Makefile for ATtiny1624 Ultra-Low-Power Telemetry Sensor Node
# ==============================================================================

MCU          = attiny1624
F_CPU        = 3333333UL
CC           = avr-gcc
OBJCOPY      = avr-objcopy
OBJDUMP      = avr-objdump
SIZE         = avr-size
AVRDUDE      = avrdude

# Programmer settings (default SerialUPDI, override with: make flash PORT=COM3)
PROGRAMMER  ?= serialupdi
PORT        ?= COM3
BAUD        ?= 115200

# Directory structure
SRC_DIR      = src
INC_DIR      = include
BUILD_DIR    = build

# Source files
SRCS         = $(SRC_DIR)/main.c \
               $(SRC_DIR)/power.c \
               $(SRC_DIR)/adc.c \
               $(SRC_DIR)/sht40.c \
               $(SRC_DIR)/cc1101.c

OBJS         = $(patsubst $(SRC_DIR)/%.c, $(BUILD_DIR)/%.o, $(SRCS))

# Compiler & Linker Flags
CFLAGS       = -mmcu=$(MCU) \
               -DF_CPU=$(F_CPU) \
               -I$(INC_DIR) \
               -Os \
               -std=gnu11 \
               -Wall \
               -Wextra \
               -Werror \
               -pedantic \
               -funsigned-char \
               -funsigned-bitfields \
               -fpack-struct \
               -fshort-enums \
               -ffunction-sections \
               -fdata-sections

LDFLAGS      = -mmcu=$(MCU) \
               -Wl,--gc-sections \
               -Wl,--relax \
               -Wl,-Map=$(BUILD_DIR)/output.map

TARGET_ELF   = $(BUILD_DIR)/firmware.elf
TARGET_HEX   = $(BUILD_DIR)/firmware.hex
TARGET_LSS   = $(BUILD_DIR)/firmware.lss

.PHONY: all clean flash fuses size

all: $(TARGET_HEX) $(TARGET_LSS) size

$(BUILD_DIR):
	@mkdir $(BUILD_DIR) 2>nul || exit 0

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.c | $(BUILD_DIR)
	@echo Compiling $<...
	@$(CC) $(CFLAGS) -c $< -o $@

$(TARGET_ELF): $(OBJS)
	@echo Linking $(TARGET_ELF)...
	@$(CC) $(LDFLAGS) $(OBJS) -o $@

$(TARGET_HEX): $(TARGET_ELF)
	@echo Generating HEX $@...
	@$(OBJCOPY) -O ihex -R .eeprom -R .fuse -R .lock -R .signature $< $@

$(TARGET_LSS): $(TARGET_ELF)
	@echo Generating Disassembly $@...
	@$(OBJDUMP) -h -S $< > $@

size: $(TARGET_ELF)
	@echo.
	@echo === Memory Usage Summary ===
	@$(SIZE) -C --mcu=$(MCU) $(TARGET_ELF)
	@echo.

flash: $(TARGET_HEX)
	@echo Flashing target MCU via $(PROGRAMMER) on $(PORT)...
	$(AVRDUDE) -c $(PROGRAMMER) -p $(MCU) -P $(PORT) -b $(BAUD) -U flash:w:$(TARGET_HEX):i

# Burn fuses for sampled BOD and 20 MHz internal oscillator
fuses:
	@echo Burning fuses (OSCCFG: 20MHz, BODCFG: 2.2V sampled)...
	$(AVRDUDE) -c $(PROGRAMMER) -p $(MCU) -P $(PORT) -b $(BAUD) \
		-U fuse1:w:0x00:m \
		-U fuse2:w:0x02:m \
		-U fuse5:w:0xC6:m

clean:
	@echo Cleaning build artifacts...
	@rmdir /s /q $(BUILD_DIR) 2>nul || exit 0
