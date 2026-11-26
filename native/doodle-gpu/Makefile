DOCKER ?= docker
CFLAGS ?= -O2 -std=gnu11 -Wall -Wextra -Werror
LIBS = -lwayland-client -lwayland-egl -lEGL -lGLESv2 -lm
SOURCES = worker.c scene.c video.c present.c
HEADERS = scene.h video.h present.h
XML = protocol/ivi-application.xml protocol/ivi-controller.xml
GEN_DIR = build/$(OUT)/protocol
GEN_HEADERS = $(XML:protocol/%.xml=$(GEN_DIR)/%-client-protocol.h)
GEN_SERVER_HEADERS = $(XML:protocol/%.xml=$(GEN_DIR)/%-server-protocol.h)
GEN_SOURCES = $(XML:protocol/%.xml=$(GEN_DIR)/%-protocol.c)
TARGET_IMAGE ?= doodle-gpu-target
TEST_IMAGE ?= doodle-gpu-test
OUT ?= host

.PHONY: target test binary mock

target:
	$(DOCKER) build --platform linux/amd64 -t $(TARGET_IMAGE) docker/target
	$(DOCKER) run --rm --platform linux/amd64 -v "$(CURDIR):/work" -w /work $(TARGET_IMAGE) make binary OUT=target LDFLAGS=-Wl,--dynamic-linker=/lib/ld-linux-x86-64.so.2

binary: build/$(OUT)/doodle-gpu

$(GEN_DIR)/%-client-protocol.h: protocol/%.xml
	mkdir -p $(GEN_DIR)
	wayland-scanner client-header $< $@

$(GEN_DIR)/%-server-protocol.h: protocol/%.xml
	mkdir -p $(GEN_DIR)
	wayland-scanner server-header $< $@

$(GEN_DIR)/%-protocol.c: protocol/%.xml
	mkdir -p $(GEN_DIR)
	wayland-scanner private-code $< $@ 2>/dev/null || wayland-scanner code $< $@

build/$(OUT)/doodle-gpu: $(SOURCES) $(HEADERS) $(GEN_HEADERS) $(GEN_SOURCES)
	mkdir -p build/$(OUT)
	$(CC) $(CFLAGS) -I$(GEN_DIR) $(LDFLAGS) $(SOURCES) $(GEN_SOURCES) $(LIBS) -o $@

mock: build/$(OUT)/mock_presenter

build/$(OUT)/mock_presenter: ../../tests/native/mock_presenter.c $(GEN_SERVER_HEADERS) $(GEN_SOURCES)
	mkdir -p build/$(OUT)
	$(CC) $(CFLAGS) -I$(GEN_DIR) $< $(GEN_SOURCES) -lwayland-server -o $@

# Exercise socket transport and compositor presentation with Mesa.
test:
	$(DOCKER) build -t $(TEST_IMAGE) docker/test
	$(DOCKER) run --rm -v "$(CURDIR)/../..:/work" -w /work -e EGL_PLATFORM=surfaceless -e LIBGL_ALWAYS_SOFTWARE=1 $(TEST_IMAGE) sh -ec 'make -C native/doodle-gpu binary mock; python3 tests/native/test_gpu_worker.py native/doodle-gpu/build/host/doodle-gpu; python3 tests/native/test_presenter.py native/doodle-gpu/build/host/doodle-gpu native/doodle-gpu/build/host/mock_presenter'
