#pragma once
#include <stddef.h>
#include <stdint.h>
#include "arm32_context.h"

#ifdef __cplusplus
extern "C" {
#endif

struct GuestApi;
void *guest_create(ArcDispatchFn resolver);
void guest_free(void *guest);
Arm32Ctx *guest_context(void *guest);
size_t guest_context_size(void);
int guest_context_save(void *guest, void *output, size_t size);
int guest_context_restore(void *guest, const void *input, size_t size);
int guest_context_jump(void *guest, const void *input, size_t size);
int guest_run(void *guest, uint32_t target, uint32_t until);
const char *guest_error(void *guest);
void guest_get_api(struct GuestApi *output);
int guest_reg_read(void *guest, int reg, void *output);
int guest_reg_write(void *guest, int reg, const void *input);
int guest_mem_map(void *guest, uint64_t address, size_t size, uint32_t permissions);
int guest_mem_map_ptr(void *guest, uint64_t address, size_t size, uint32_t permissions, void *pointer);
int guest_mem_read(void *guest, uint64_t address, void *output, size_t size);
int guest_mem_write(void *guest, uint64_t address, const void *input, size_t size);
int guest_mem_unmap(void *guest, uint64_t address, size_t size);
int guest_mem_protect(void *guest, uint64_t address, size_t size, uint32_t permissions);
int guest_hook_add(void *guest, size_t *handle, int type, void *callback, void *user,
                   uint64_t begin, uint64_t end, ...);
int guest_hook_del(void *guest, size_t handle);
int guest_stop(void *guest);

#ifdef __cplusplus
}
#endif
