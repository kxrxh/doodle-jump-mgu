#include "dispatch.h"

#include <cmath>
#include <cstring>
#include <map>
#include <memory>
#include <vector>

namespace {
struct Binding {
    NativeHandler handler;
    void *context;
};

struct Dispatch {
    void *uc;
    GuestApi api;
    int hook_type;
    DispatchFallback fallback;
    void *context;
    std::map<uint64_t, std::string> names;
    std::map<std::string, Binding> handlers;
    std::vector<size_t> hooks;
    std::vector<uint64_t> budgets;
    std::string error;

    Dispatch(void *engine, const GuestApi &functions, int type, DispatchFallback python, void *user)
        : uc(engine), api(functions), hook_type(type), fallback(python), context(user) {}
    ~Dispatch() {
        for (size_t hook : hooks)
            api.hook_del(uc, hook);
    }
    void hook(uint64_t begin, uint64_t end, int type = 0, void *handler = nullptr);
};

int zero(void *, NativeCall *, NativeResult *result) {
    result->flags = 1;
    return 0;
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

void callback(void *uc, uint64_t address, uint32_t, void *context) noexcept {
    auto &dispatch = *static_cast<Dispatch *>(context);
    try {
        if (!dispatch.error.empty()) {
            dispatch.api.emu_stop(uc);
            return;
        }
        const auto name = dispatch.names.find(address);
        if (name == dispatch.names.end())
            throw std::runtime_error("Unknown host call address");
        NativeCall call{uc, &dispatch.api, name->second.c_str()};
        NativeResult result;
        auto handler = dispatch.handlers.find(name->second);
        const int status = handler == dispatch.handlers.end()
                               ? dispatch.fallback(dispatch.context, address, &result)
                               : handler->second.handler(handler->second.context, &call, &result);
        if (status)
            throw std::runtime_error("Host callback failed: " + name->second);
        if (result.flags & 1) {
            const uint32_t low = uint32_t(result.value);
            call.check(dispatch.api.reg_write(uc, dispatch.api.registers[0], &low));
        }
        if (result.flags & 2) {
            const uint32_t high = uint32_t(result.value >> 32);
            call.check(dispatch.api.reg_write(uc, dispatch.api.registers[1], &high));
        }
        const uint32_t link = call.reg(5);
        call.check(dispatch.api.reg_write(uc, dispatch.api.registers[6], &link));
    } catch (const std::exception &error) {
        dispatch.error = error.what();
        dispatch.api.emu_stop(uc);
    } catch (...) {
        dispatch.error = "Unknown native host callback error";
        dispatch.api.emu_stop(uc);
    }
}

void budget_callback(void *uc, uint64_t, uint32_t size, void *context) noexcept {
    auto &dispatch = *static_cast<Dispatch *>(context);
    // Thumb instructions occupy at least two bytes; charge every active caller too.
    const uint64_t instructions = size > 1 ? size / 2 : 1;
    for (auto &remaining : dispatch.budgets) {
        if (remaining <= instructions) {
            remaining = 0;
            dispatch.error = "Engine call instruction limit exhausted";
            dispatch.api.emu_stop(uc);
            return;
        }
        remaining -= instructions;
    }
}

void Dispatch::hook(uint64_t begin, uint64_t end, int type, void *handler) {
    size_t handle = 0;
    const int status =
        api.hook_add(uc, &handle, type ? type : hook_type,
                     handler ? handler : reinterpret_cast<void *>(callback), this, begin, end);
    if (status)
        throw std::runtime_error("Cannot register native Unicorn hook: " + std::to_string(status));
    try {
        hooks.push_back(handle);
    } catch (...) {
        api.hook_del(uc, handle);
        throw;
    }
}
} // namespace

extern "C" void *dispatch_create(void *uc, const GuestApi *api, int type, int block_type, uint64_t begin,
                                 uint64_t end, DispatchFallback fallback, void *context) {
    try {
        auto dispatch = std::unique_ptr<Dispatch>(new Dispatch(uc, *api, type, fallback, context));
        dispatch->hook(begin, end);
        dispatch->hook(1, 0, block_type, reinterpret_cast<void *>(budget_callback));
        for (const char *name : {"pthread_mutex_lock", "pthread_mutex_unlock", "pthread_mutex_init",
                                 "pthread_mutex_destroy"})
            dispatch->handlers.emplace(name, Binding{zero, nullptr});
        for (const char *name : {"roundf", "sinf", "cosf"})
            dispatch->handlers.emplace(name, Binding{single_math, nullptr});
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
