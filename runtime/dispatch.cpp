#include "dispatch.h"
#include "image_memory.h"

#include <cmath>
#include <cstring>
#include <map>
#include <memory>
#include <vector>

void NativeCall::write(uint64_t address, const void *data, size_t size) const {
    if (size) {
        images->before_write(address, size);
        check(api->mem_write(guest, address, data, size));
    }
}

namespace {
struct Binding {
    NativeHandler handler;
    void *context;
};

struct Dispatch {
    void *guest;
    GuestApi api;
    ImageMemory images;
    int hook_type;
    DispatchFallback fallback;
    void *context;
    std::map<uint64_t, std::string> names;
    std::map<std::string, Binding> handlers;
    std::vector<size_t> hooks;
    std::vector<uint64_t> budgets;
    std::string error;

    Dispatch(void *engine, const GuestApi &functions, int type, DispatchFallback host, void *user)
        : guest(engine), api(functions), images(engine, api), hook_type(type), fallback(host), context(user) {}
    ~Dispatch() {
        for (size_t hook : hooks)
            api.hook_del(guest, hook);
    }
    void hook(uint64_t begin, uint64_t end, int type = 0, void *handler = nullptr);
};

int zero(void *, NativeCall *, NativeResult *result) {
    result->flags = 1;
    return 0;
}

int transfer_memory(NativeCall *call, NativeResult *result, size_t size, uint32_t value, bool fill) {
    if (size > 64 * 1024 * 1024)
        throw std::runtime_error("Bulk memory operation exceeds its size limit");
    const uint32_t destination = call->argument(0);
    if (size) {
        uint8_t small[256];
        std::vector<uint8_t> large;
        uint8_t *bytes = small;
        if (size > sizeof small) {
            large.resize(size);
            bytes = large.data();
        }
        if (fill)
            std::memset(bytes, int(value & 255), size);
        else
            call->read(value, bytes, size);
        // Reading first handles overlap; NativeCall::write preserves PNG snapshots.
        call->write(destination, bytes, size);
    }
    result->value = destination;
    result->flags = 1;
    return 0;
}

int copy_memory(void *, NativeCall *call, NativeResult *result) {
    return transfer_memory(call, result, call->argument(2), call->argument(1), false);
}

int fill_memory(void *, NativeCall *call, NativeResult *result) {
    const bool standard = std::strcmp(call->name, "memset") == 0;
    return transfer_memory(call, result, call->argument(standard ? 2 : 1),
                           call->argument(standard ? 1 : 2), true);
}

int clear_memory(void *, NativeCall *call, NativeResult *result) {
    return transfer_memory(call, result, call->argument(1), 0, true);
}

int single_math(void *, NativeCall *call, NativeResult *result) {
    uint32_t bits = call->argument(0);
    float input;
    std::memcpy(&input, &bits, sizeof(input));
    const double x = input;
    double value;
    if (std::strcmp(call->name, "roundf") == 0) {
        if (!std::isfinite(x))
            throw std::domain_error("Invalid roundf argument");
        value = x >= 0 ? std::floor(x + 0.5) : std::ceil(x - 0.5);
        if (value == 0)
            value = 0; // Python's integer result loses the sign of zero.
    } else {
        if (std::isinf(x))
            throw std::domain_error("Invalid trigonometric argument");
        value = std::strcmp(call->name, "sinf") == 0 ? std::sin(x) : std::cos(x);
    }
    const float output = static_cast<float>(value);
    std::memcpy(&bits, &output, sizeof(bits));
    result->value = bits;
    result->flags = 1;
    return 0;
}

void callback(void *guest, uint64_t address, uint32_t, void *context) noexcept {
    auto &dispatch = *static_cast<Dispatch *>(context);
    try {
        if (!dispatch.error.empty()) {
            dispatch.api.emu_stop(guest);
            return;
        }
        const auto name = dispatch.names.find(address);
        if (name == dispatch.names.end())
            throw std::runtime_error("Unknown host call address");
        NativeCall call{guest, &dispatch.api, name->second.c_str(), &dispatch.images};
        NativeResult result;
        auto handler = dispatch.handlers.find(name->second);
        const int status = handler == dispatch.handlers.end()
                               ? dispatch.fallback(dispatch.context, address, &result)
                               : handler->second.handler(handler->second.context, &call, &result);
        if (status)
            throw std::runtime_error("Host callback failed: " + name->second);
        if (result.flags & 1) {
            const uint32_t low = uint32_t(result.value);
            call.check(dispatch.api.reg_write(guest, dispatch.api.registers[0], &low));
        }
        if (result.flags & 2) {
            const uint32_t high = uint32_t(result.value >> 32);
            call.check(dispatch.api.reg_write(guest, dispatch.api.registers[1], &high));
        }
        const uint32_t link = call.reg(5);
        call.check(dispatch.api.reg_write(guest, dispatch.api.registers[6], &link));
    } catch (const std::exception &error) {
        dispatch.error = error.what();
        dispatch.api.emu_stop(guest);
    } catch (...) {
        dispatch.error = "Unknown native host callback error";
        dispatch.api.emu_stop(guest);
    }
}

void budget_callback(void *guest, uint64_t, uint32_t size, void *context) noexcept {
    auto &dispatch = *static_cast<Dispatch *>(context);
    // Thumb instructions occupy at least two bytes; charge every active caller too.
    const uint64_t instructions = size > 1 ? size / 2 : 1;
    for (auto &remaining : dispatch.budgets) {
        if (remaining <= instructions) {
            remaining = 0;
            dispatch.error = "Engine call instruction limit exhausted";
            dispatch.api.emu_stop(guest);
            return;
        }
        remaining -= instructions;
    }
}

bool image_write_callback(void *guest, int, uint64_t address, int size, int64_t, void *context) noexcept {
    auto &dispatch = *static_cast<Dispatch *>(context);
    try {
        if (dispatch.error.empty() && size > 0)
            return dispatch.images.before_write(address, size);
    } catch (const std::exception &error) {
        dispatch.error = error.what();
    } catch (...) {
        dispatch.error = "Unknown PNG copy-on-write error";
    }
    dispatch.api.emu_stop(guest);
    return false;
}

void Dispatch::hook(uint64_t begin, uint64_t end, int type, void *handler) {
    size_t handle = 0;
    const int status =
        api.hook_add(guest, &handle, type ? type : hook_type,
                     handler ? handler : reinterpret_cast<void *>(callback), this, begin, end);
    if (status)
        throw std::runtime_error("Cannot register guest hook (backend status " + std::to_string(status) + ")");
    try {
        hooks.push_back(handle);
    } catch (...) {
        api.hook_del(guest, handle);
        throw;
    }
}
} // namespace

extern "C" void *dispatch_create(void *guest, const GuestApi *api, int type, int block_type, int write_type, uint64_t begin,
                                 uint64_t end, DispatchFallback fallback, void *context) {
    try {
        auto dispatch = std::unique_ptr<Dispatch>(new Dispatch(guest, *api, type, fallback, context));
        dispatch->hook(begin, end);
        dispatch->hook(1, 0, block_type, reinterpret_cast<void *>(budget_callback));
        dispatch->hook(0x50000000, 0x5fffffff, write_type, reinterpret_cast<void *>(image_write_callback));
        for (const char *name : {"pthread_mutex_lock", "pthread_mutex_unlock", "pthread_mutex_init",
                                 "pthread_mutex_destroy"})
            dispatch->handlers.emplace(name, Binding{zero, nullptr});
        for (const char *name : {"roundf", "sinf", "cosf"})
            dispatch->handlers.emplace(name, Binding{single_math, nullptr});
        for (const char *name : {"memcpy", "memmove", "__aeabi_memcpy", "__aeabi_memmove"})
            dispatch->handlers.emplace(name, Binding{copy_memory, nullptr});
        for (const char *name : {"memset", "__aeabi_memset"})
            dispatch->handlers.emplace(name, Binding{fill_memory, nullptr});
        for (const char *name : {"__aeabi_memclr", "__aeabi_memclr4", "__aeabi_memclr8"})
            dispatch->handlers.emplace(name, Binding{clear_memory, nullptr});
        return dispatch.release();
    } catch (...) {
        return nullptr;
    }
}

extern "C" void dispatch_destroy(void *context) { delete static_cast<Dispatch *>(context); }

extern "C" int dispatch_begin_call(void *context, uint64_t limit) {
    auto &dispatch = *static_cast<Dispatch *>(context);
    try {
        dispatch.budgets.push_back(limit);
        return 0;
    } catch (const std::exception &error) {
        dispatch.error = error.what();
        return 1;
    }
}

extern "C" void dispatch_end_call(void *context) {
    static_cast<Dispatch *>(context)->budgets.pop_back();
}

extern "C" int dispatch_add(void *context, uint64_t address, const char *name, int intercept) {
    auto &dispatch = *static_cast<Dispatch *>(context);
    try {
        dispatch.names[address] = name;
        if (intercept)
            dispatch.hook(address, address);
        return 0;
    } catch (const std::exception &error) {
        dispatch.error = error.what();
        return 1;
    }
}

extern "C" int dispatch_register(void *context, const char *name, NativeHandler handler,
                                 void *user) {
    auto &dispatch = *static_cast<Dispatch *>(context);
    try {
        dispatch.handlers[name] = Binding{handler, user};
        return 0;
    } catch (const std::exception &error) {
        dispatch.error = error.what();
        return 1;
    }
}

extern "C" const char *dispatch_error(void *context) {
    return static_cast<Dispatch *>(context)->error.c_str();
}

extern "C" void dispatch_clear_error(void *context) {
    static_cast<Dispatch *>(context)->error.clear();
}

extern "C" void *dispatch_allocate_image(void *context, uint32_t address, size_t size) {
    auto &dispatch = *static_cast<Dispatch *>(context);
    try {
        return dispatch.images.allocate(address, size);
    } catch (const std::exception &error) {
        dispatch.error = error.what();
    } catch (...) {
        dispatch.error = "Unknown PNG allocation error";
    }
    return nullptr;
}

extern "C" int dispatch_release_image(void *context, uint32_t address) {
    auto &dispatch = *static_cast<Dispatch *>(context);
    try {
        dispatch.images.release(address);
        return 0;
    } catch (const std::exception &error) {
        dispatch.error = error.what();
    } catch (...) {
        dispatch.error = "Unknown PNG release error";
    }
    return 1;
}

extern "C" int dispatch_before_write(void *context, uint64_t address, size_t size) {
    auto &dispatch = *static_cast<Dispatch *>(context);
    try {
        dispatch.images.before_write(address, size);
        return 0;
    } catch (const std::exception &error) {
        dispatch.error = error.what();
    } catch (...) {
        dispatch.error = "Unknown PNG copy-on-write error";
    }
    return 1;
}
