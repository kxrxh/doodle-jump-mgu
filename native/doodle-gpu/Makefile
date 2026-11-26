DOCKER ?= docker
CFLAGS ?= -O2 -std=gnu11 -Wall -Wextra -Werror
LIBS = -lwayland-client -lEGL -lGLESv2 -lm
SOURCES = worker.c scene.c video.c
HEADERS = scene.h video.h
TARGET_IMAGE ?= doodle-gpu-target
TEST_IMAGE ?= doodle-gpu-test
OUT ?= host

.PHONY: target test binary

target:
	$(DOCKER) build --platform linux/amd64 -t $(TARGET_IMAGE) docker/target
	$(DOCKER) run --rm --platform linux/amd64 -v "$(CURDIR):/work" -w /work $(TARGET_IMAGE) make binary OUT=target LDFLAGS=-Wl,--dynamic-linker=/lib/ld-linux-x86-64.so.2

binary: build/$(OUT)/doodle-gpu

build/$(OUT)/doodle-gpu: $(SOURCES) $(HEADERS)
	mkdir -p build/$(OUT)
	$(CC) $(CFLAGS) $(LDFLAGS) $(SOURCES) $(LIBS) -o $@

# The tests exercise the normal socket and shared-memory interfaces with Mesa.
test:
	$(DOCKER) build -t $(TEST_IMAGE) docker/test
	$(DOCKER) run --rm -v "$(CURDIR)/../..:/work" -w /work -e EGL_PLATFORM=surfaceless -e LIBGL_ALWAYS_SOFTWARE=1 $(TEST_IMAGE) sh -ec 'make -C native/doodle-gpu binary; python3 tests/native/test_gpu_worker.py native/doodle-gpu/build/host/doodle-gpu'
