#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <memory>
#include <vector>

struct GuestApi;
struct PixelAllocation;

class PixelSnapshot {
    std::shared_ptr<PixelAllocation> allocation;
    std::vector<uint8_t> owned;

public:
    explicit PixelSnapshot(std::shared_ptr<PixelAllocation>);
    const uint8_t *data() const;
    size_t owned_bytes() const;
    void detach();
};

struct ImageSlice {
    std::shared_ptr<PixelSnapshot> pixels;
    size_t offset = 0;
};

class ImageMemory {
    struct Source {
        std::shared_ptr<PixelAllocation> allocation;
        std::weak_ptr<PixelSnapshot> snapshot;
        bool readonly = false;
    };
    void *uc;
    const GuestApi &api;
    std::map<uint64_t, Source> sources;

public:
    ImageMemory(void *, const GuestApi &);
    ~ImageMemory();
    void *allocate(uint32_t address, size_t size);
    void release(uint32_t address);
    ImageSlice snapshot(uint32_t address, size_t size);
    bool before_write(uint64_t address, size_t size);
};
