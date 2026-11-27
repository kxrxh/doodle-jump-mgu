#include "guest.h"
#include "../dispatch.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <vector>

namespace {
constexpr size_t page_size = 4096, page_count = 1u << 20;
constexpr int hook_code = 4, hook_block = 8, hook_write_protected = 256;
constexpr std::array<int, 3> hook_types{{hook_code, hook_block, hook_write_protected}};
struct Stopped {};
struct Allocation {
    void *pointer;
    size_t size;
    explicit Allocation(size_t bytes) : size(bytes) {
        pointer = mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (pointer == MAP_FAILED) throw std::runtime_error("Cannot allocate guest memory");
    }
    ~Allocation() { munmap(pointer, size); }
};
struct Mapping {
    uint64_t begin, end;
    uint8_t *pointer;
    std::shared_ptr<Allocation> allocation;
};
struct Hook {
    int type;
    void *callback, *user;
    uint64_t begin, end;
    bool matches(uint32_t address) const { return begin > end || (begin <= address && address <= end); }
};
struct Run {
    uint32_t until;
    bool stopped = false;
    bool jumped = false;
};
struct Guest {
    Arm32Ctx context{};
    ArcDispatchFn resolver;
    std::vector<uint8_t *> pages{page_count, nullptr};
    std::vector<uint8_t> permissions = std::vector<uint8_t>(page_count);
    std::map<uint64_t, Mapping> mappings;
    std::array<std::map<size_t, Hook>, 3> hooks;
    std::vector<Run> runs;
    size_t next_hook = 1, hook_epoch = 0;
    std::string error;
    explicit Guest(ArcDispatchFn fn) : resolver(fn) {
        context.owner = this;
        context.pages = pages.data();
        context.permissions = permissions.data();
        context.cpsr = 0x1d3;
        context.zf = 1;
    }
    bool stopped() const { return !runs.empty() && runs.back().stopped; }
};

Guest &owner(Arm32Ctx *c) {
    if (!c || !c->owner) throw std::runtime_error("Missing AOT guest context");
    return *static_cast<Guest *>(c->owner);
}
void range(uint64_t address, size_t size) {
    if (address > UINT32_MAX || size > (uint64_t(1) << 32) - address)
        throw std::runtime_error("Guest memory range exceeds 32 bits");
}
void page_range(uint64_t address, size_t size) {
    range(address, size);
    if (!size || address % page_size || size % page_size)
        throw std::runtime_error("Guest mapping must contain aligned pages");
}
void mapped(Guest &g, uint64_t address, size_t size, int permission = 0) {
    range(address, size);
    const uint64_t end = address + size;
    for (uint64_t page = address / page_size; size && page <= (end - 1) / page_size; page++) {
        if (!g.pages[page]) throw std::runtime_error("Unmapped guest memory");
        if (permission && !(g.permissions[page] & permission))
            throw std::runtime_error("Guest memory permission denied");
    }
}
void transfer(Guest &g, uint64_t address, void *bytes, size_t size, bool write) {
    uint8_t *data = static_cast<uint8_t *>(bytes);
    while (size) {
        size_t offset = address % page_size, count = std::min(size, page_size - offset);
        uint8_t *memory = g.pages[address / page_size] + offset;
        if (write) std::memcpy(memory, data, count);
        else std::memcpy(data, memory, count);
        address += count;
        data += count;
        size -= count;
    }
}
template<class F> int checked(void *guest, F action) noexcept {
    if (!guest) return 1;
    auto &g = *static_cast<Guest *>(guest);
    try { action(g); return 0; }
    catch (const std::exception &e) { g.error = e.what(); }
    catch (...) { g.error = "Unknown AOT guest access error"; }
    return 1;
}
void set_pc(Guest &g, uint32_t address) {
    g.context.r[15] = address & ~1u;
    g.context.cpsr = (g.context.cpsr & ~32u) | ((address & 1u) << 5);
}
void hooks(Guest &g, int type, uint32_t address, uint32_t bytes) {
    using Callback = void (*)(void *, uint64_t, uint32_t, void *);
    auto &selected = g.hooks[type == hook_code ? 0 : 1];
    size_t cursor = 0;
    if (selected.size() == 1) {
        const auto first = selected.begin();
        cursor = first->first;
        const Hook hook = first->second;
        const size_t epoch = g.hook_epoch;
        if (hook.matches(address)) {
            reinterpret_cast<Callback>(hook.callback)(&g, address, bytes, hook.user);
            if (g.stopped()) throw Stopped{};
        }
        if (epoch == g.hook_epoch) return;
    }
    for (auto i = selected.upper_bound(cursor); i != selected.end(); i = selected.upper_bound(cursor)) {
        cursor = i->first;
        Hook hook = i->second;
        if (hook.matches(address)) {
            reinterpret_cast<Callback>(hook.callback)(&g, address, bytes, hook.user);
            if (g.stopped()) throw Stopped{};
        }
    }
}
void mapping(Guest &g, uint64_t address, size_t size, uint32_t permissions, uint8_t *pointer,
             std::shared_ptr<Allocation> allocation) {
    page_range(address, size);
    if (permissions & ~7u || !pointer) throw std::runtime_error("Invalid guest mapping");
    for (uint64_t page = address / page_size; page < (address + size) / page_size; page++)
        if (g.pages[page]) throw std::runtime_error("Guest mapping overlaps existing pages");
    g.mappings.emplace(address, Mapping{address, address + size, pointer, std::move(allocation)});
    for (size_t offset = 0; offset < size; offset += page_size) {
        const size_t page = (address + offset) / page_size;
        g.pages[page] = pointer + offset;
        g.permissions[page] = uint8_t(permissions);
    }
}
} // namespace

extern "C" void *guest_create(ArcDispatchFn resolver) {
    try { return new Guest(resolver); } catch (...) { return nullptr; }
}
extern "C" void guest_free(void *guest) { delete static_cast<Guest *>(guest); }
extern "C" Arm32Ctx *guest_context(void *guest) { return guest ? &static_cast<Guest *>(guest)->context : nullptr; }
extern "C" size_t guest_context_size(void) { return offsetof(Arm32Ctx, owner); }
extern "C" int guest_context_save(void *guest, void *output, size_t size) {
    return checked(guest, [&](Guest &g) {
        if (!output || size != guest_context_size()) throw std::runtime_error("Invalid guest context size");
        std::memcpy(output, &g.context, size);
    });
}
extern "C" int guest_context_restore(void *guest, const void *input, size_t size) {
    return checked(guest, [&](Guest &g) {
        if (!input || size != guest_context_size()) throw std::runtime_error("Invalid guest context size");
        std::memcpy(&g.context, input, size);
    });
}
extern "C" int guest_context_jump(void *guest, const void *input, size_t size) {
    return checked(guest, [&](Guest &g) {
        if (!input || size != guest_context_size()) throw std::runtime_error("Invalid guest context size");
        if (g.runs.empty()) throw std::runtime_error("Nonlocal guest jump outside execution");
        std::memcpy(&g.context, input, size);
        g.runs.back().jumped = true;
    });
}
extern "C" const char *guest_error(void *guest) {
    return guest ? static_cast<Guest *>(guest)->error.c_str() : "Missing AOT guest";
}
extern "C" int guest_reg_read(void *guest, int reg, void *output) {
    return checked(guest, [&](Guest &g) {
        if (!output) throw std::runtime_error("Missing guest register output");
        auto &c = g.context;
        uint32_t value;
        if (reg >= 66 && reg <= 78) value = c.r[reg - 66];
        else if (reg == 12) value = c.r[13];
        else if (reg == 10) value = c.r[14];
        else if (reg == 11) value = c.r[15];
        else if (reg == 1 || reg == 2 || reg == 3)
            value = (c.cpsr & 0x07ffffffu) | (c.nf << 31) | (c.zf << 30) | (c.cf << 29) | (c.vf << 28) | (c.qf << 27);
        else if (reg == 6) value = c.fpscr;
        else if (reg == 4) value = c.fpexc;
        else if (reg == 111) value = c.cpacr;
        else if (reg >= 14 && reg <= 45) { std::memcpy(output, &c.v.u64[reg - 14], 8); return; }
        else if (reg >= 79 && reg <= 110) value = c.v.u32[reg - 79];
        else if (reg >= 50 && reg <= 65) { std::memcpy(output, &c.v.u64[(reg - 50) * 2], 16); return; }
        else throw std::runtime_error("Unsupported AOT guest register");
        std::memcpy(output, &value, 4);
    });
}
extern "C" int guest_reg_write(void *guest, int reg, const void *input) {
    return checked(guest, [&](Guest &g) {
        if (!input) throw std::runtime_error("Missing guest register input");
        auto &c = g.context;
        uint32_t value;
        std::memcpy(&value, input, 4);
        if (reg >= 66 && reg <= 78) c.r[reg - 66] = value;
        else if (reg == 12) c.r[13] = value;
        else if (reg == 10) c.r[14] = value;
        else if (reg == 11) set_pc(g, value);
        else if (reg == 1 || reg == 2 || reg == 3) {
            c.nf = value >> 31; c.zf = (value >> 30) & 1; c.cf = (value >> 29) & 1;
            c.vf = (value >> 28) & 1; c.qf = (value >> 27) & 1;
            if (reg == 3) c.cpsr = value & 0x07ffffffu;
        } else if (reg == 6) c.fpscr = value;
        else if (reg == 4) c.fpexc = value;
        else if (reg == 111) c.cpacr = value;
        else if (reg >= 14 && reg <= 45) std::memcpy(&c.v.u64[reg - 14], input, 8);
        else if (reg >= 79 && reg <= 110) c.v.u32[reg - 79] = value;
        else if (reg >= 50 && reg <= 65) std::memcpy(&c.v.u64[(reg - 50) * 2], input, 16);
        else throw std::runtime_error("Unsupported AOT guest register");
    });
}
extern "C" int guest_mem_map(void *guest, uint64_t address, size_t size, uint32_t permissions) {
    return checked(guest, [&](Guest &g) {
        page_range(address, size);
        auto allocation = std::make_shared<Allocation>(size);
        mapping(g, address, size, permissions, static_cast<uint8_t *>(allocation->pointer), allocation);
    });
}
extern "C" int guest_mem_map_ptr(void *guest, uint64_t address, size_t size, uint32_t permissions, void *pointer) {
    return checked(guest, [&](Guest &g) { mapping(g, address, size, permissions, static_cast<uint8_t *>(pointer), {}); });
}
extern "C" int guest_mem_read(void *guest, uint64_t address, void *output, size_t size) {
    return checked(guest, [&](Guest &g) {
        if (size && !output) throw std::runtime_error("Missing guest read output");
        mapped(g, address, size);
        transfer(g, address, output, size, false);
    });
}
extern "C" int guest_mem_write(void *guest, uint64_t address, const void *input, size_t size) {
    return checked(guest, [&](Guest &g) {
        if (size && !input) throw std::runtime_error("Missing guest write input");
        mapped(g, address, size);
        // Like uc_mem_write, host writes bypass page protection. NativeCall and
        // Arm's Python wrapper detach shared images before calling this API.
        transfer(g, address, const_cast<void *>(input), size, true);
    });
}
extern "C" int guest_mem_unmap(void *guest, uint64_t address, size_t size) {
    return checked(guest, [&](Guest &g) {
        page_range(address, size);
        mapped(g, address, size);
        const uint64_t end = address + size;
        std::vector<Mapping> retained;
        for (auto i = g.mappings.begin(); i != g.mappings.end();) {
            const Mapping m = i->second;
            if (m.begin >= end || m.end <= address) { ++i; continue; }
            i = g.mappings.erase(i);
            if (m.begin < address) retained.push_back({m.begin, address, m.pointer, m.allocation});
            if (m.end > end) retained.push_back({end, m.end, m.pointer + (end - m.begin), m.allocation});
        }
        for (auto &m : retained) g.mappings.emplace(m.begin, std::move(m));
        for (uint64_t page = address / page_size; page < end / page_size; page++) {
            g.pages[page] = nullptr;
            g.permissions[page] = 0;
        }
    });
}
extern "C" int guest_mem_protect(void *guest, uint64_t address, size_t size, uint32_t permissions) {
    return checked(guest, [&](Guest &g) {
        page_range(address, size);
        if (permissions & ~7u) throw std::runtime_error("Invalid guest permissions");
        mapped(g, address, size);
        for (uint64_t page = address / page_size; page < (address + size) / page_size; page++)
            g.permissions[page] = uint8_t(permissions);
    });
}
extern "C" int guest_hook_add(void *guest, size_t *handle, int type, void *callback, void *user,
                              uint64_t begin, uint64_t end, ...) {
    return checked(guest, [&](Guest &g) {
        if (!handle || !callback || !(type & (hook_code | hook_block | hook_write_protected)) ||
            (type & ~(hook_code | hook_block | hook_write_protected)) || begin > UINT32_MAX || end > UINT32_MAX)
            throw std::runtime_error("Invalid AOT guest hook");
        *handle = g.next_hook++;
        try {
            for (size_t i = 0; i < hook_types.size(); ++i)
                if (type & hook_types[i])
                    g.hooks[i].emplace(*handle, Hook{type, callback, user, begin, end});
            ++g.hook_epoch;
        } catch (...) {
            for (auto &selected : g.hooks) selected.erase(*handle);
            throw;
        }
    });
}
extern "C" int guest_hook_del(void *guest, size_t handle) {
    return checked(guest, [&](Guest &g) {
        size_t removed = 0;
        for (auto &selected : g.hooks) removed += selected.erase(handle);
        if (!removed) throw std::runtime_error("Unknown AOT guest hook");
        ++g.hook_epoch;
    });
}
extern "C" int guest_stop(void *guest) {
    return checked(guest, [&](Guest &g) { if (!g.runs.empty()) g.runs.back().stopped = true; });
}
extern "C" void guest_get_api(GuestApi *output) {
    if (output) *output = GuestApi{guest_reg_read, guest_reg_write, guest_mem_read, guest_mem_write,
        guest_hook_add, guest_hook_del, guest_stop, guest_mem_map_ptr, guest_mem_unmap, guest_mem_protect,
        {66, 67, 68, 69, 12, 10, 11}};
}

extern "C" uint64_t arc_load(Arm32Ctx *c, uint32_t address, size_t size) {
    auto &g = owner(c);
    if (!size || size > 8) throw std::runtime_error("Invalid generated guest read width");
    mapped(g, address, size, 1);
    uint64_t value = 0;
    transfer(g, address, &value, size, false);
    return value;
}
extern "C" void arc_store(Arm32Ctx *c, uint32_t address, uint64_t value, size_t size) {
    auto &g = owner(c);
    if (!size || size > 8) throw std::runtime_error("Invalid generated guest write width");
    mapped(g, address, size);
    const uint64_t end = uint64_t(address) + size;
    bool protected_page = false;
    for (uint64_t page = address / page_size; page <= (end - 1) / page_size; page++)
        protected_page |= !(g.permissions[page] & 2);
    if (protected_page) {
        using Callback = bool (*)(void *, int, uint64_t, int, int64_t, void *);
        auto &selected = g.hooks[2];
        size_t cursor = 0;
        for (auto i = selected.begin(); i != selected.end(); i = selected.upper_bound(cursor)) {
            cursor = i->first;
            Hook hook = i->second;
            if (hook.matches(address))
                reinterpret_cast<Callback>(hook.callback)(&g, 22, address, int(size), int64_t(value), hook.user);
            if (g.stopped()) throw Stopped{};
        }
    }
    mapped(g, address, size, 2);
    transfer(g, address, &value, size, true);
}
extern "C" void arc_trap(Arm32Ctx *, const char *what) {
    throw std::runtime_error(what ? what : "AOT guest trap");
}
extern "C" void arc_block(Arm32Ctx *c, uint32_t address, uint32_t bytes) {
    auto &g = owner(c);
    c->r[15] = address & ~1u;
    if (g.stopped()) throw Stopped{};
    hooks(g, hook_block, c->r[15], bytes);
}
extern "C" int arc_enter(Arm32Ctx *c, uint32_t address, uint32_t bytes) {
    auto &g = owner(c);
    set_pc(g, address);
    arc_block(c, address, bytes);
    hooks(g, hook_code, c->r[15], bytes);
    return c->r[15] != (address & ~1u);
}
extern "C" void arc_return(Arm32Ctx *c, uint32_t target) { set_pc(owner(c), target); }
extern "C" void arc_dispatch(Arm32Ctx *c, uint32_t target) {
    auto &g = owner(c);
    set_pc(g, target);
    if (g.stopped()) throw Stopped{};
    if (!g.runs.empty() && c->r[15] == (g.runs.back().until & ~1u)) return;
    hooks(g, hook_code, c->r[15], 2);
    if (c->r[15] != (target & ~1u)) return;
    if (!g.resolver) arc_dispatch_miss(c, target);
    g.resolver(c, target);
}
extern "C" void arc_dispatch_miss(Arm32Ctx *, uint32_t target) {
    char message[80];
    std::snprintf(message, sizeof message, "Unresolved AOT target 0x%08x", target);
    throw std::runtime_error(message);
}
extern "C" int guest_run(void *guest, uint32_t target, uint32_t until) {
    if (!guest) return 1;
    auto &g = *static_cast<Guest *>(guest);
    g.error.clear();
    g.runs.push_back(Run{until});
    int status = 0;
    try {
        size_t resumed = 0;
        for (;;) {
            arc_dispatch(&g.context, target);
            if (g.context.r[15] == (until & ~1u)) break;
            if (!g.runs.back().jumped) {
                char message[100];
                std::snprintf(message, sizeof message, "AOT returned to 0x%08x instead of 0x%08x", g.context.r[15], until);
                throw std::runtime_error(message);
            }
            if (++resumed > 4096) throw std::runtime_error("Nonlocal guest continuation limit exceeded");
            g.runs.back().jumped = false;
            target = g.context.r[15] | ((g.context.cpsr >> 5) & 1u);
            arc_block(&g.context, target, 2);
        }
    } catch (const Stopped &) {
        if (g.error.empty()) g.error = "AOT guest execution stopped";
        status = 1;
    } catch (const std::exception &e) {
        g.error = e.what();
        status = 1;
    } catch (...) {
        g.error = "Unknown AOT guest execution error";
        status = 1;
    }
    g.runs.pop_back();
    return status;
}
