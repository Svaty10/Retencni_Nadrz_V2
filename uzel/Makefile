# ==============================================================================
# ATtiny1624 tank sensor node – build & flash
#
# Toolchain: avr-gcc >= 12 with avr-libc >= 2.1 (tinyAVR 2-series support), e.g.
#   https://github.com/ZakKemble/avr-gcc-build/releases (Windows/Linux/macOS)
# Older toolchains (Atmel 7.3 / Ubuntu gcc-avr) do NOT know the attiny1624.
#
#   make            build build/firmware.hex
#   make flash      flash via SerialUPDI (PORT=COM3 or PORT=/dev/ttyUSB0)
#   make fuses      write fuses (once per chip)
# ==============================================================================

MCU        = attiny1624
F_CPU      = 3333333UL
CC         = avr-gcc
OBJCOPY    = avr-objcopy
OBJDUMP    = avr-objdump
SIZE       = avr-size
AVRDUDE    = avrdude

PROGRAMMER ?= serialupdi
PORT       ?= COM3
BAUD       ?= 115200

BUILD_DIR  = build
SRCS       = src/main.c src/power.c src/adc.c src/sht40.c src/cc1101.c src/crypto.c src/proto.c src/link.c
OBJS       = $(patsubst src/%.c,$(BUILD_DIR)/%.o,$(SRCS))

CFLAGS  = -mmcu=$(MCU) -DF_CPU=$(F_CPU) -Iinclude -Os -std=gnu11 \
          -Wall -Wextra -Werror -Wno-array-bounds \
          -funsigned-char -funsigned-bitfields -fshort-enums \
          -ffunction-sections -fdata-sections -flto
LDFLAGS = -mmcu=$(MCU) -Os -flto -Wl,--gc-sections -Wl,-Map=$(BUILD_DIR)/firmware.map

ifeq ($(OS),Windows_NT)
  MKDIR = if not exist $(BUILD_DIR) mkdir $(BUILD_DIR)
  RMDIR = if exist $(BUILD_DIR) rmdir /s /q $(BUILD_DIR)
else
  MKDIR = mkdir -p $(BUILD_DIR)
  RMDIR = rm -rf $(BUILD_DIR)
endif

.PHONY: all clean flash fuses size test

all: $(BUILD_DIR)/firmware.hex $(BUILD_DIR)/firmware.lss size

$(BUILD_DIR)/%.o: src/%.c include/*.h
	@$(MKDIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/firmware.elf: $(OBJS)
	$(CC) $(LDFLAGS) $(OBJS) -o $@

$(BUILD_DIR)/firmware.hex: $(BUILD_DIR)/firmware.elf
	$(OBJCOPY) -O ihex -R .eeprom -R .fuse -R .lock -R .signature $< $@

$(BUILD_DIR)/firmware.lss: $(BUILD_DIR)/firmware.elf
	$(OBJDUMP) -h -S $< > $@

size: $(BUILD_DIR)/firmware.elf
	$(SIZE) $<

flash: $(BUILD_DIR)/firmware.hex
	$(AVRDUDE) -c $(PROGRAMMER) -p $(MCU) -P $(PORT) -b $(BAUD) -U flash:w:$<:i

# fuse1 BODCFG  = 0x04 : BOD enabled in active mode, disabled in sleep, level 1.8 V
#                        (last resort only – the firmware switches off at 3.0 V battery)
# fuse2 OSCCFG  = 0x02 : 20 MHz internal oscillator (divided to 3.333 MHz at run time)
# fuse5 SYSCFG0 = 0xC5 : no CRC check, PA0 = UPDI, EEPROM KEPT on chip erase (pairing keys survive a firmware update)
fuses:
	$(AVRDUDE) -c $(PROGRAMMER) -p $(MCU) -P $(PORT) -b $(BAUD) \
		-U fuse1:w:0x04:m -U fuse2:w:0x02:m -U fuse5:w:0xC5:m

# crypto self-test on the host (needs a native C compiler)
test:
	cc -O2 -Wall -Iinclude tools/crypto_selftest.c src/crypto.c src/proto.c -o $(BUILD_DIR)/crypto_selftest
	$(BUILD_DIR)/crypto_selftest

clean:
	$(RMDIR)
