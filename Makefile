# Makefile for checkpoint/restore
# Context: gem5 SE mode, x86-64, no ASLR
#
# Targets:
#   all         - build everything
#   libckpt.so  - LD_PRELOAD library that generates the checkpoints
#   loader      - the custom restorer for non-PIE targets; also the placeholder
#                 executable gem5 needs for direct restoration
#   loader_pie  - the same restorer for PIE targets (SPEC, most dynamic apps)
#   clean       - remove build artifacts

CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra
# Flags the checkpoint/restore flow depends on. They are appended with
# `override` so that a CFLAGS/LDFLAGS exported by the environment (conda,
# cluster modules, -march=native...) can add flags but never drop these:
# losing them yields AVX instructions gem5 cannot run, or a dynamic loader.
REQUIRED_CFLAGS := -fno-stack-protector -mno-avx -mno-avx2 -mno-sse3 -mno-ssse3 -mno-sse4.1 -mno-sse4.2 -fno-builtin
override CFLAGS += $(REQUIRED_CFLAGS)
# The loaders (and only they) are linked statically: they must not depend on
# the target's libc, and gem5 SE runs them as the process image.
LOADER_LDFLAGS := -static

# -----------------------------------------------------------------------
# The loader MUST be linked at a VA that does NOT collide with the target.
# The default text segment for static x86-64 binaries is ~0x400000.
# We put the loader at 0x20000000 (512 MB) which is safely above any
# typical single-binary layout.
# -----------------------------------------------------------------------
LOADER_LOAD_ADDR := 0x20000000

# Source files
LOADER_SRCS  := src/loader.c

# Output directory
BUILD_DIR := build

.PHONY: all clean show_layout

all: $(BUILD_DIR)/loader $(BUILD_DIR)/loader_pie $(BUILD_DIR)/libckpt.so

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# --- Libreria LD_PRELOAD (libckpt.so) --------------------------------------
# Con -fPIC porque va en una biblioteca compartida. Este es el binario que
# usa el flujo de generacion de checkpoints (LD_PRELOAD sobre el benchmark).
PIC_CFLAGS := $(CFLAGS) -fPIC

$(BUILD_DIR)/dumper_pic.o: src/dumper.c src/checkpoint.h src/dumper.h | $(BUILD_DIR)
	$(CC) $(PIC_CFLAGS) -c -o $@ src/dumper.c

$(BUILD_DIR)/dumper_asm_pic.o: src/dumper_asm.S | $(BUILD_DIR)
	$(CC) $(PIC_CFLAGS) -c -o $@ src/dumper_asm.S

$(BUILD_DIR)/libckpt.so: src/libckpt.c $(BUILD_DIR)/dumper_pic.o $(BUILD_DIR)/dumper_asm_pic.o src/checkpoint.h | $(BUILD_DIR)
	$(CC) $(PIC_CFLAGS) -shared -o $@ src/libckpt.c \
		$(BUILD_DIR)/dumper_pic.o $(BUILD_DIR)/dumper_asm_pic.o -ldl -lpthread
	@echo "Built libckpt.so at $@"

# --- Custom Loader --------------------------------------------------------
$(BUILD_DIR)/loader: $(LOADER_SRCS) src/checkpoint.h | $(BUILD_DIR)
	$(CC) $(CFLAGS) $(LOADER_LDFLAGS) \
		-no-pie \
		-Wl,-Ttext-segment=$(LOADER_LOAD_ADDR) \
		-o $@ $(LOADER_SRCS)
	@echo "Built loader at $@"

# --- Loader for PIE targets -----------------------------------------------
# Same loader with its initial program break anchored at 0x555500000000,
# just below where Linux loads PIE executables (0x555555554000 without
# ASLR), so moving the break to a PIE target's heap end is cheap in gem5.
# Use build/loader for non-PIE targets; gem5_configs pick automatically.
LOADER_PIE_BRK := 0x555500000000

$(BUILD_DIR)/loader_pie: $(LOADER_SRCS) src/checkpoint.h | $(BUILD_DIR)
	$(CC) $(CFLAGS) -DRMC_BRK_ANCHOR $(LOADER_LDFLAGS) \
		-no-pie \
		-Wl,-Ttext-segment=$(LOADER_LOAD_ADDR) \
		-Wl,--section-start=.rmc_brk_anchor=$(LOADER_PIE_BRK) \
		-o $@ $(LOADER_SRCS)
	@echo "Built loader_pie at $@"

# --- Show memory layout of both loaders -----------------------------------
show_layout: $(BUILD_DIR)/loader $(BUILD_DIR)/loader_pie
	@echo "=== loader segments ==="
	readelf -l $(BUILD_DIR)/loader | grep -A1 "LOAD"
	@echo ""
	@echo "=== loader_pie segments ==="
	readelf -l $(BUILD_DIR)/loader_pie | grep -A1 "LOAD"

clean:
	rm -rf $(BUILD_DIR)
	@echo "Cleaned."
