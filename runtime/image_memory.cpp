#include "image_memory.h"
#include "dispatch.h"

#include <cerrno>
#include <cstring>
#include <iterator>
#include <limits>
#include <sys/mman.h>

namespace {
constexpr uint64_t image_begin = 0x50000000, image_end = 0x60000000;
constexpr size_t page_size = 4096;
constexpr unsigned read_only = 1, read_write = 3;

void check(int status) {
    if (status)
        throw std::runtime_error("PNG guest access failed (Unicorn " + std::to_string(status) + ")");
}
} // namespace

struct PixelAllocation {
    uint8_t *data;
    size_t size, mapped;

    explicit PixelAllocation(size_t bytes) : size(bytes), mapped((bytes + page_size - 1) & ~(page_size - 1)) {
        void *memory = mmap(nullptr, mapped, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (memory == MAP_FAILED)
            throw std::runtime_error(std::string("Cannot allocate PNG pixels: ") + std::strerror(errno));
        data = static_cast<uint8_t *>(memory);
    }
    ~PixelAllocation() { munmap(data, mapped); }
};

PixelSnapshot::PixelSnapshot(std::shared_ptr<PixelAllocation> pixels) : allocation(std::move(pixels)) {}

const uint8_t *PixelSnapshot::data() const { return allocation ? allocation->data : owned.data(); }

size_t PixelSnapshot::owned_bytes() const { return owned.capacity(); }

void PixelSnapshot::detach() {
    if (!allocation)
        return;
    std::vector<uint8_t> copy(allocation->data, allocation->data + allocation->size);
    owned.swap(copy);
    allocation.reset();
}

ImageMemory::ImageMemory(void *engine, const GuestApi &functions) : uc(engine), api(functions) {}

ImageMemory::~ImageMemory() {
    for (const auto &entry : sources)
        api.mem_unmap(uc, entry.first, entry.second.allocation->mapped);
}

void *ImageMemory::allocate(uint32_t address, size_t size) {
    if (address < image_begin || address >= image_end || address % page_size || !size || size > 64 * 1024 * 1024)
        throw std::invalid_argument("Invalid PNG allocation");
    const auto allocation = std::make_shared<PixelAllocation>(size);
    if (allocation->mapped > image_end - address)
        throw std::invalid_argument("PNG allocation exceeds its arena");
    auto next = sources.lower_bound(address);
    if ((next != sources.end() && next->first < uint64_t(address) + allocation->mapped) ||
        (next != sources.begin() && std::prev(next)->first + std::prev(next)->second.allocation->mapped > address))
        throw std::invalid_argument("Overlapping PNG allocation");
    const auto inserted = sources.emplace(address, Source{allocation, {}, false});
    const int status = api.mem_map_ptr(uc, address, allocation->mapped, read_write, allocation->data);
    if (status) {
        sources.erase(inserted.first);
        check(status);
    }
    return allocation->data;
}

void ImageMemory::release(uint32_t address) {
    const auto source = sources.find(address);
    if (source == sources.end())
        throw std::invalid_argument("Unknown PNG allocation");
    check(api.mem_unmap(uc, address, source->second.allocation->mapped));
    sources.erase(source);
}

ImageSlice ImageMemory::snapshot(uint32_t address, size_t size) {
    if (!size)
        return {};
    auto found = sources.upper_bound(address);
    if (found == sources.begin())
        return {};
    --found;
    auto &source = found->second;
    const size_t offset = uint64_t(address) - found->first;
    if (offset > source.allocation->size || size > source.allocation->size - offset)
        return {};
    auto pixels = source.snapshot.lock();
    if (!pixels) {
        pixels = std::make_shared<PixelSnapshot>(source.allocation);
        if (!source.readonly)
            check(api.mem_protect(uc, found->first, source.allocation->mapped, read_only));
        source.readonly = true;
        source.snapshot = pixels;
    }
    return {pixels, offset};
}

bool ImageMemory::before_write(uint64_t address, size_t size) {
    if (!size)
        return false;
    if (size > std::numeric_limits<uint64_t>::max() - address)
        throw std::invalid_argument("PNG write range overflow");
    const uint64_t end = address + size;
    auto source = sources.upper_bound(address);
    if (source != sources.begin())
        --source;
    bool resolved = false;
    for (; source != sources.end() && source->first < end; ++source) {
        auto &image = source->second;
        if (source->first + image.allocation->mapped <= address || !image.readonly)
            continue;
        if (auto pixels = image.snapshot.lock())
            pixels->detach();
        check(api.mem_protect(uc, source->first, image.allocation->mapped, read_write));
        image.readonly = false;
        image.snapshot.reset();
        resolved = true;
    }
    return resolved;
}
