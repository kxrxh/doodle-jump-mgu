#pragma once

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>

// Function pointers use the same loaded Unicorn instance as the Python shell.
struct GuestApi {
    int (*reg_read)(void *, int, void *);
    int (*reg_write)(void *, int, const void *);
    int (*mem_read)(void *, uint64_t, void *, size_t);
    int (*mem_write)(void *, uint64_t, const void *, size_t);
    int (*hook_add)(void *, size_t *, int, void *, void *, uint64_t, uint64_t, ...);
    int (*hook_del)(void *, size_t);
    int (*emu_stop)(void *);
    int registers[7]; // r0-r3, sp, lr, pc
};

struct NativeResult {
    uint64_t value = 0;
    uint32_t flags = 0; // bit 0 writes r0; bit 1 writes r1
};

struct NativeCall {
    void *uc;
    const GuestApi *api;
    const char *name;
    uint32_t values[5] = {};
    unsigned loaded = 0;

    void check(int status) const {
        if (status)
            throw std::runtime_error("Guest access failed (Unicorn " + std::to_string(status) +
                                     ")");
    }
    uint32_t reg(unsigned index) {
        uint32_t value = 0;
        check(api->reg_read(uc, api->registers[index], &value));
        return value;
    }
    void read(uint64_t address, void *output, size_t size) const {
        if (size)
            check(api->mem_read(uc, address, output, size));
    }
    void write(uint64_t address, const void *data, size_t size) const {
        if (size)
            check(api->mem_write(uc, address, data, size));
    }
    uint32_t argument(unsigned index) {
        if (index > 1024)
            throw std::runtime_error("Invalid guest argument index");
        const unsigned slot = index < 4 ? index : 4;
        if (!(loaded & (1u << slot))) {
            values[slot] = reg(slot);
            loaded |= 1u << slot;
        }
        if (index < 4)
            return values[slot];
        uint32_t value;
        read(uint64_t(values[4]) + (index - 4) * 4, &value, sizeof(value));
        return value;
    }
};

using NativeHandler = int (*)(void *, NativeCall *, NativeResult *);
using DispatchFallback = int (*)(void *, uint64_t, NativeResult *);

extern "C" {
void *dispatch_create(void *, const GuestApi *, int, uint64_t, uint64_t, DispatchFallback, void *);
void dispatch_destroy(void *);
int dispatch_add(void *, uint64_t, const char *, int);
int dispatch_register(void *, const char *, NativeHandler, void *);
const char *dispatch_error(void *);
void dispatch_clear_error(void *);
}
