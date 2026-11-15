#ifndef DOODLE_TEXTURE_H
#define DOODLE_TEXTURE_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

enum TextureFormat {
    TEXTURE_RGBA8,
    TEXTURE_RGB8,
    TEXTURE_INDEX8,
    TEXTURE_INDEX16,
    TEXTURE_CONSTANT
};

typedef struct {
    const uint8_t *pixels;
    const uint32_t *palette;
    int format;
} TextureView;

/* Palette entries use RGBA byte order, including the packed GLES formats' exact
 * expansion. */
static inline uint32_t texture_pixel(const TextureView *texture, size_t index) {
    const uint8_t *p = texture->pixels;
    switch (texture->format) {
    case TEXTURE_CONSTANT:
        return texture->palette[0];
    case TEXTURE_INDEX8:
        return texture->palette[p[index]];
    case TEXTURE_INDEX16:
        return texture->palette[p[index * 2] | (p[index * 2 + 1] << 8)];
    case TEXTURE_RGB8:
        p += index * 3;
        return p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | UINT32_C(0xff000000);
    default: {
        uint32_t value;
        memcpy(&value, p + index * 4, sizeof(value));
        return value;
    }
    }
}

#endif
