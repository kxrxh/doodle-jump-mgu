#include <math.h>
#include <stdint.h>
#include "texture.h"
#ifdef __SSE2__
#include <emmintrin.h>

static inline __m128 rgba_float(const uint8_t *p) {
    int32_t word;
    memcpy(&word, p, sizeof(word));
    __m128i bytes = _mm_cvtsi32_si128(word);
    __m128i zero = _mm_setzero_si128();
    return _mm_cvtepi32_ps(_mm_unpacklo_epi16(_mm_unpacklo_epi8(bytes, zero), zero));
}
#endif

typedef struct { float x, y, u, v, r, g, b, a; } Vertex;

void png_pixels(uint8_t *out, const uint8_t *rgba, int width, int height, int stride, int mode) {
    int bytes = mode == 2 || mode == 4 || mode == 8 || mode == 13 ? 2 : mode == 6 || mode == 7 ? 1 : 4;
    for (int y = 0; y < height; y++) for (int x = 0; x < width; x++) {
        const uint8_t *p = rgba + (y * width + x) * 4;
        uint8_t *d = out + (y * stride + x) * bytes;
        int a = p[3], r = p[0], g = p[1], b = p[2];
        if (mode != 4 && mode != 13 && mode != 14) { r = r*a/255; g = g*a/255; b = b*a/255; }
        if (mode == 2 || mode == 13) { d[0] = (b & 0xf0) | (a >> 4); d[1] = (r & 0xf0) | (g >> 4); }
        else if (mode == 4) { d[0] = (b >> 3) | ((g & 0x1c) << 3); d[1] = (r & 0xf8) | (g >> 5); }
        else if (mode == 6) d[0] = r;
        else if (mode == 7) d[0] = a;
        else if (mode == 8) { d[0] = r; d[1] = a; }
        else { d[0] = r; d[1] = g; d[2] = b; d[3] = a; }
    }
}

void unpack16(uint8_t *out, const uint8_t *source, int count, int kind) {
    for (int i = 0; i < count; i++) {
        unsigned v = source[i*2] | (source[i*2+1] << 8);
        uint8_t *p = out + i*4;
        if (kind == 0x8363) { p[0]=(v>>11)*255/31; p[1]=((v>>5)&63)*255/63; p[2]=(v&31)*255/31; p[3]=255; }
        else if (kind == 0x8033) { p[0]=(v>>12)*17; p[1]=((v>>8)&15)*17; p[2]=((v>>4)&15)*17; p[3]=(v&15)*17; }
        else { p[0]=(v>>11)*255/31; p[1]=((v>>6)&31)*255/31; p[2]=((v>>1)&31)*255/31; p[3]=(v&1)*255; }
    }
}

static float edge(Vertex a, Vertex b, float x, float y) {
    return (b.x - a.x) * (y - a.y) - (b.y - a.y) * (x - a.x);
}

static int top_left(Vertex a, Vertex b) {
    return b.y < a.y || (b.y == a.y && b.x > a.x);
}

static int texel(int p, int size, int repeat) {
    if (repeat) return (p % size + size) % size;
    return p < 0 ? 0 : p >= size ? size - 1 : p;
}

static float factor(int mode, float alpha) {
    return mode == 0 ? 0 : mode == 1 ? 1 : mode == 770 ? alpha : 1 - alpha;
}

static inline void texture_addresses(const TextureView *texture, const int *indices,
                                     const uint8_t **out, uint32_t *rgb) {
    const uint8_t *p = texture->pixels;
    /* Read palette entries as RGBA bytes so interpolation needs no channel shifts. */
#define ADDRESSES(address) for (int i = 0; i < 4; i++) out[i] = (const uint8_t *)(address)
    switch (texture->format) {
    case TEXTURE_CONSTANT: ADDRESSES(texture->palette); break;
    case TEXTURE_INDEX8: ADDRESSES(texture->palette + p[indices[i]]); break;
    case TEXTURE_INDEX16:
        ADDRESSES(texture->palette + (p[indices[i]*2] | (p[indices[i]*2+1] << 8))); break;
    case TEXTURE_RGB8:
        for (int i = 0; i < 4; i++) {
            rgb[i] = texture_pixel(texture, indices[i]);
            out[i] = (const uint8_t *)(rgb + i);
        }
        break;
    default: ADDRESSES(p + indices[i] * 4); break;
    }
#undef ADDRESSES
}

void raster_compact_rows(uint8_t *out, int width, int height, const Vertex *vertices, int count,
            const TextureView *texture, int tw, int th, int linear, int repeat_s, int repeat_t,
            int blend, int source, int destination, int replace, int clip_y0, int clip_y1) {
    for (int n = 0; n + 2 < count; n += 3) {
        Vertex a = vertices[n], b = vertices[n + 1], c = vertices[n + 2];
        float area = edge(a, b, c.x, c.y);
        if (fabsf(area) < 0.00001f) continue;
        if (area < 0) { Vertex swap = b; b = c; c = swap; area = -area; }
        int x0 = (int)floorf(fminf(a.x, fminf(b.x, c.x)));
        int y0 = (int)floorf(fminf(a.y, fminf(b.y, c.y)));
        int x1 = (int)ceilf(fmaxf(a.x, fmaxf(b.x, c.x)));
        int y1 = (int)ceilf(fmaxf(a.y, fmaxf(b.y, c.y)));
        if (x0 < 0) x0 = 0;
        if (y0 < 0) y0 = 0;
        if (x1 > width) x1 = width;
        if (y1 > height) y1 = height;
        if (y0 < clip_y0) y0 = clip_y0;
        if (y1 > clip_y1) y1 = clip_y1;
        for (int y = y0; y < y1; y++) for (int x = x0; x < x1; x++) {
            float ea = edge(b, c, x + 0.5f, y + 0.5f);
            float eb = edge(c, a, x + 0.5f, y + 0.5f);
            float ec = edge(a, b, x + 0.5f, y + 0.5f);
            if (ea < 0 || (ea == 0 && !top_left(b, c)) ||
                eb < 0 || (eb == 0 && !top_left(c, a)) ||
                ec < 0 || (ec == 0 && !top_left(a, b))) continue;
            float wa = ea / area, wb = eb / area, wc = ec / area;
#ifdef __SSE2__
            __m128 color4 = _mm_add_ps(_mm_add_ps(
                _mm_mul_ps(_mm_loadu_ps(&a.r), _mm_set1_ps(wa)),
                _mm_mul_ps(_mm_loadu_ps(&b.r), _mm_set1_ps(wb))),
                _mm_mul_ps(_mm_loadu_ps(&c.r), _mm_set1_ps(wc)));
            __m128 rgba4 = _mm_set1_ps(255);
#else
            float color[4] = {a.r * wa + b.r * wb + c.r * wc,
                              a.g * wa + b.g * wb + c.g * wc,
                              a.b * wa + b.b * wb + c.b * wc,
                              a.a * wa + b.a * wb + c.a * wc};
            float rgba[4] = {255, 255, 255, 255};
#endif
            if (texture && tw && th) {
                float u = a.u * wa + b.u * wb + c.u * wc;
                float v = a.v * wa + b.v * wb + c.v * wc;
                float tx = u * tw - (linear ? 0.5f : 0), ty = v * th - (linear ? 0.5f : 0);
                int ix = (int)floorf(tx), iy = (int)floorf(ty);
                float fx = linear ? tx - ix : 0, fy = linear ? ty - iy : 0;
                uint32_t rgb[4];
                const uint8_t *samples[4];
                int indices[4] = {
                    texel(iy, th, repeat_t) * tw + texel(ix, tw, repeat_s),
                    texel(iy, th, repeat_t) * tw + texel(ix + 1, tw, repeat_s),
                    texel(iy + 1, th, repeat_t) * tw + texel(ix, tw, repeat_s),
                    texel(iy + 1, th, repeat_t) * tw + texel(ix + 1, tw, repeat_s)};
                texture_addresses(texture, indices, samples, rgb);
#ifdef __SSE2__
                __m128 fx4 = _mm_set1_ps(fx), inv_x = _mm_set1_ps(1-fx);
                __m128 top = _mm_add_ps(_mm_mul_ps(rgba_float(samples[0]), inv_x),
                                        _mm_mul_ps(rgba_float(samples[1]), fx4));
                __m128 bottom = _mm_add_ps(_mm_mul_ps(rgba_float(samples[2]), inv_x),
                                           _mm_mul_ps(rgba_float(samples[3]), fx4));
                rgba4 = _mm_add_ps(_mm_mul_ps(top, _mm_set1_ps(1-fy)),
                                   _mm_mul_ps(bottom, _mm_set1_ps(fy)));
#else
                for (int k = 0; k < 4; k++)
                    rgba[k] = (samples[0][k] * (1-fx) + samples[1][k] * fx) * (1-fy) +
                              (samples[2][k] * (1-fx) + samples[3][k] * fx) * fy;
#endif
            }
#ifdef __SSE2__
            if (replace == 2 && texture) {
                __m128 alpha4 = _mm_shuffle_ps(color4, color4, _MM_SHUFFLE(3,3,3,3));
                __m128 rgb = _mm_add_ps(_mm_mul_ps(_mm_mul_ps(color4, _mm_set1_ps(255)), alpha4),
                    _mm_mul_ps(rgba4, _mm_sub_ps(_mm_set1_ps(1), alpha4)));
                /* GL_DECAL changes RGB only. */
                __m128 rgb_mask = _mm_castsi128_ps(_mm_set_epi32(0, -1, -1, -1));
                rgba4 = _mm_or_ps(_mm_and_ps(rgb_mask, rgb), _mm_andnot_ps(rgb_mask, rgba4));
            } else if (!replace || !texture) {
                rgba4 = _mm_mul_ps(rgba4, color4);
            }
            float alpha = _mm_cvtss_f32(_mm_shuffle_ps(rgba4, rgba4, _MM_SHUFFLE(3,3,3,3))) / 255;
            float sf = blend ? factor(source, alpha) : 1;
            float df = blend ? factor(destination, alpha) : 0;
            uint8_t *pixel = out + (y * width + x) * 4;
            __m128 destination4 = blend && destination == 768
                ? _mm_div_ps(rgba4, _mm_set1_ps(255)) : _mm_set1_ps(df);
            __m128 value = _mm_add_ps(_mm_mul_ps(rgba4, _mm_set1_ps(sf)),
                                     _mm_mul_ps(rgba_float(pixel), destination4));
            __m128 rounded = _mm_add_ps(value, _mm_set1_ps(.5f));
            /* The second max operand keeps NaN mapped to zero. */
            __m128 clipped = _mm_min_ps(_mm_max_ps(rounded, _mm_setzero_ps()), _mm_set1_ps(255));
            __m128i words = _mm_cvttps_epi32(clipped);
            __m128i shorts = _mm_packs_epi32(words, _mm_setzero_si128());
            uint32_t packed = _mm_cvtsi128_si32(_mm_packus_epi16(shorts, _mm_setzero_si128()));
            memcpy(pixel, &packed, sizeof(packed));
#else
            if (replace == 2 && texture) {
                for (int k = 0; k < 3; k++) rgba[k] = color[k] * 255 * color[3] + rgba[k] * (1 - color[3]);
            } else {
                for (int k = 0; k < 4; k++) if (!replace || !texture) rgba[k] *= color[k];
            }
            float alpha = rgba[3] / 255;
            float sf = blend ? factor(source, alpha) : 1;
            float df = blend ? factor(destination, alpha) : 0;
            uint8_t *pixel = out + (y * width + x) * 4;
            for (int k = 0; k < 4; k++) {
                float value = rgba[k] * sf + pixel[k] * (blend && destination == 768 ? rgba[k] / 255 : df);
                float rounded = value + 0.5f;
                /* GCC's fminf/fmaxf call libm for every channel on the MGU. */
                pixel[k] = rounded >= 255 ? 255 : rounded > 0 ? (uint8_t)rounded : 0;
            }
#endif
        }
    }
}

void raster_compact(uint8_t *out, int width, int height, const Vertex *vertices, int count,
            const TextureView *texture, int tw, int th, int linear, int repeat_s, int repeat_t,
            int blend, int source, int destination, int replace) {
    raster_compact_rows(out, width, height, vertices, count, texture, tw, th, linear,
                        repeat_s, repeat_t, blend, source, destination, replace, 0, height);
}

/* Scenery passes decoded RGBA snapshots through this existing ABI. */
void raster(uint8_t *out, int width, int height, const Vertex *vertices, int count,
            const uint8_t *texture, int tw, int th, int linear, int repeat_s, int repeat_t,
            int blend, int source, int destination, int replace) {
    TextureView view = {texture, NULL, TEXTURE_RGBA8};
    raster_compact(out, width, height, vertices, count, texture ? &view : NULL, tw, th, linear,
                   repeat_s, repeat_t, blend, source, destination, replace);
}
