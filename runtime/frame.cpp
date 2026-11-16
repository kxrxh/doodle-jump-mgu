#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <vector>

namespace {
struct Rectangle {
    uint32_t x, y, width, height;
};

struct FrameEncoder {
    enum { Tile = 32, MaxRectangles = 64 };
    int width, height;
    size_t frame_bytes, payload_size = 0;
    bool initialized = false;
    std::vector<uint8_t> previous, payload;

    FrameEncoder(int w, int h)
        : width(w), height(h), frame_bytes(size_t(w) * h * 4), previous(frame_bytes),
          payload(frame_bytes + 12 + MaxRectangles * 16) {}

    unsigned bounding_box(const uint8_t *frame, Rectangle &rectangle) const {
        int left = width, top = height, right = 0, bottom = 0;
        const size_t stride = size_t(width) * 4;
        for (int y = 0; y < height; ++y) {
            const uint8_t *a = previous.data() + size_t(y) * stride;
            const uint8_t *b = frame + size_t(y) * stride;
            if (!std::memcmp(a, b, stride))
                continue;
            int x = 0, end = width;
            while (x < end && !std::memcmp(a + x * 4, b + x * 4, 4))
                ++x;
            while (end > x && !std::memcmp(a + (end - 1) * 4, b + (end - 1) * 4, 4))
                --end;
            if (x < left)
                left = x;
            if (end > right)
                right = end;
            if (y < top)
                top = y;
            bottom = y + 1;
        }
        if (left == width)
            return 0;
        rectangle = {uint32_t(left), uint32_t(top), uint32_t(right - left), uint32_t(bottom - top)};
        return 1;
    }

    unsigned changed_rectangles(const uint8_t *frame,
                                std::array<Rectangle, MaxRectangles> &rectangles) const {
        const size_t stride = size_t(width) * 4;
        const int columns = (width + Tile - 1) / Tile;
        std::array<unsigned, 64> previous_row;
        unsigned previous_count = 0, count = 0;
        for (int y = 0; y < height; y += Tile) {
            std::array<unsigned, 64> current_row;
            unsigned current_count = 0;
            int start = -1, h = height - y < Tile ? height - y : Tile;
            for (int column = 0; column <= columns; ++column) {
                int x = column * Tile, w = width - x < Tile ? width - x : Tile;
                bool dirty = false;
                if (column < columns) {
                    for (int row = 0; row < h; ++row) {
                        const size_t offset = size_t(y + row) * stride + x * 4;
                        if (std::memcmp(previous.data() + offset, frame + offset, size_t(w) * 4)) {
                            dirty = true;
                            break;
                        }
                    }
                }
                if (dirty && start < 0)
                    start = x;
                if (!dirty && start >= 0) {
                    int end = x < width ? x : width;
                    unsigned index = count;
                    for (unsigned i = 0; i < previous_count; ++i) {
                        const auto &r = rectangles[previous_row[i]];
                        if (r.x == uint32_t(start) && r.width == uint32_t(end - start)) {
                            index = previous_row[i];
                            break;
                        }
                    }
                    if (index < count)
                        rectangles[index].height += h;
                    else {
                        if (count == MaxRectangles)
                            return bounding_box(frame, rectangles[0]);
                        rectangles[count++] = {uint32_t(start), uint32_t(y),
                                               uint32_t(end - start), uint32_t(h)};
                    }
                    current_row[current_count++] = index;
                    start = -1;
                }
            }
            std::memcpy(previous_row.data(), current_row.data(), current_count * sizeof(unsigned));
            previous_count = current_count;
        }
        return count;
    }

    static void word(uint8_t *destination, uint32_t value) {
        for (unsigned i = 0; i < 4; ++i)
            destination[i] = uint8_t(value >> (i * 8));
    }

    bool encode(const uint8_t *frame, size_t size) {
        if (!frame || size != frame_bytes)
            return false;
        std::array<Rectangle, MaxRectangles> rectangles;
        unsigned count = 1;
        if (initialized)
            count = changed_rectangles(frame, rectangles);
        else
            rectangles[0] = {0, 0, uint32_t(width), uint32_t(height)};
        size_t needed = 12 + count * 16;
        for (unsigned i = 0; i < count; ++i)
            needed += size_t(rectangles[i].width) * rectangles[i].height * 4;
        if (needed > payload.size())
            return false;
        word(payload.data(), width);
        word(payload.data() + 4, height);
        word(payload.data() + 8, count);
        size_t offset = 12 + count * 16;
        for (unsigned i = 0; i < count; ++i) {
            const auto &r = rectangles[i];
            uint8_t *header = payload.data() + 12 + i * 16;
            word(header, r.x);
            word(header + 4, r.y);
            word(header + 8, r.width);
            word(header + 12, r.height);
            for (uint32_t y = r.y; y < r.y + r.height; ++y) {
                const size_t at = (size_t(y) * width + r.x) * 4, bytes = size_t(r.width) * 4;
                std::memcpy(payload.data() + offset, frame + at, bytes);
                std::memcpy(previous.data() + at, frame + at, bytes);
                offset += bytes;
            }
        }
        initialized = true;
        payload_size = needed;
        return true;
    }
};
} // namespace

extern "C" {
void *frame_encoder_create(int width, int height) noexcept {
    if (width < 1 || height < 1 || width > 2048 || height > 2048)
        return nullptr;
    try {
        return new FrameEncoder(width, height);
    } catch (...) {
        return nullptr;
    }
}

void frame_encoder_destroy(void *context) noexcept { delete static_cast<FrameEncoder *>(context); }

int frame_encoder_encode(void *context, const uint8_t *frame, size_t size) noexcept {
    if (!context)
        return 0;
    try {
        return static_cast<FrameEncoder *>(context)->encode(frame, size) ? 1 : 0;
    } catch (...) {
        return 0;
    }
}

const uint8_t *frame_encoder_data(void *context) noexcept {
    const auto *encoder = static_cast<FrameEncoder *>(context);
    return encoder && encoder->payload_size ? encoder->payload.data() : nullptr;
}

size_t frame_encoder_size(void *context) noexcept {
    return context ? static_cast<FrameEncoder *>(context)->payload_size : 0;
}
}
