#define _GNU_SOURCE
#include <dlfcn.h>
#include <signal.h>
#include <stdlib.h>
#include <unistd.h>

int graphics_gpu_begin(void *graphics, int enabled, int reset) {
    static int (*next)(void *, int, int);
    static int stopped;
    if (!next) next = dlsym(RTLD_NEXT, "graphics_gpu_begin");
    const char *arm = getenv("DOODLE_DELIVERY_ARM");
    if (!stopped && arm && access(arm, F_OK) == 0) {
        stopped = 1;
        raise(SIGSTOP);
    }
    return next(graphics, enabled, reset);
}
