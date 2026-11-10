#include <math.h>
#include <stdint.h>

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

void raster(uint8_t *out, int width, int height, const Vertex *vertices, int count,
            const uint8_t *texture, int tw, int th, int linear, int repeat_s, int repeat_t,
            int blend, int source, int destination, int replace) {
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
        for (int y = y0; y < y1; y++) for (int x = x0; x < x1; x++) {
            float ea = edge(b, c, x + 0.5f, y + 0.5f);
            float eb = edge(c, a, x + 0.5f, y + 0.5f);
            float ec = edge(a, b, x + 0.5f, y + 0.5f);
            if (ea < 0 || (ea == 0 && !top_left(b, c)) ||
                eb < 0 || (eb == 0 && !top_left(c, a)) ||
                ec < 0 || (ec == 0 && !top_left(a, b))) continue;
            float wa = ea / area, wb = eb / area, wc = ec / area;
            float color[4] = {a.r * wa + b.r * wb + c.r * wc,
                              a.g * wa + b.g * wb + c.g * wc,
                              a.b * wa + b.b * wb + c.b * wc,
                              a.a * wa + b.a * wb + c.a * wc};
            float rgba[4] = {255, 255, 255, 255};
            if (texture && tw && th) {
                float u = a.u * wa + b.u * wb + c.u * wc;
                float v = a.v * wa + b.v * wb + c.v * wc;
                float tx = u * tw - (linear ? 0.5f : 0), ty = v * th - (linear ? 0.5f : 0);
                int ix = (int)floorf(tx), iy = (int)floorf(ty);
                float fx = linear ? tx - ix : 0, fy = linear ? ty - iy : 0;
                int p00 = (texel(iy, th, repeat_t) * tw + texel(ix, tw, repeat_s)) * 4;
                int p10 = (texel(iy, th, repeat_t) * tw + texel(ix + 1, tw, repeat_s)) * 4;
                int p01 = (texel(iy + 1, th, repeat_t) * tw + texel(ix, tw, repeat_s)) * 4;
                int p11 = (texel(iy + 1, th, repeat_t) * tw + texel(ix + 1, tw, repeat_s)) * 4;
                for (int k = 0; k < 4; k++)
                    rgba[k] = (texture[p00+k] * (1-fx) + texture[p10+k] * fx) * (1-fy) +
                              (texture[p01+k] * (1-fx) + texture[p11+k] * fx) * fy;
            }
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
                pixel[k] = (uint8_t)fminf(255, fmaxf(0, value + 0.5f));
            }
        }
    }
}
