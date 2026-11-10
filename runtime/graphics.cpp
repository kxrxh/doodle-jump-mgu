#include "dispatch.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

extern "C" void raster(uint8_t *, int, int, const float *, int, const uint8_t *, int, int, int, int,
                       int, int, int, int, int);
extern "C" void unpack16(uint8_t *, const uint16_t *, int, int);
namespace {
using Matrix = std::array<double, 16>;
using Vertex = std::array<double, 8>;
const Matrix identity{{1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1}};
Matrix multiply(const Matrix &a, const Matrix &b) {
    Matrix out{};
    for (int col = 0; col < 4; ++col)
        for (int row = 0; row < 4; ++row)
            for (int k = 0; k < 4; ++k)
                out[row + col * 4] += a[row + k * 4] * b[k + col * 4];
    return out;
}
struct Pointer {
    uint32_t size = 0, kind = 0, stride = 0, pointer = 0, buffer = 0;
};
struct Texture {
    int width = 0, height = 0, excluded = 0;
    uint64_t revision = 0;
    std::vector<uint8_t> pixels;
    std::string asset;
    std::map<uint32_t, uint32_t> params;
};
struct Unit {
    uint32_t texture = 0;
    bool enabled = false;
    int replace = 0;
    std::map<uint32_t, uint32_t> env;
};
struct Client {
    bool enabled = false;
    Pointer pointer;
};
struct Rectangle {
    double left, top, right, bottom;
    int excluded;
};
struct Graphics {
    int width, height;
    uint8_t *frame;
    std::array<int32_t, 4> viewport;
    std::map<uint32_t, std::vector<Matrix>> matrices{
        {0x1700, {identity}}, {0x1701, {identity}}, {0x1702, {identity}}};
    uint32_t matrix_mode = 0x1700;
    std::array<double, 4> color{{1, 1, 1, 1}}, clear_color{{0, 0, 0, 0}};
    std::map<uint32_t, bool> enabled, arrays;
    std::map<uint32_t, Pointer> pointers;
    std::map<uint32_t, Texture> textures;
    uint32_t texture = 0, next_texture = 1, texture_unit = 0x84c0, client_unit = 0x84c0;
    std::map<uint32_t, Unit> units;
    std::map<uint32_t, Client> clients;
    std::map<uint32_t, uint32_t> environment;
    std::map<uint32_t, std::vector<uint8_t>> buffers;
    std::map<uint32_t, uint32_t> bound;
    uint32_t next_buffer = 1, source = 1, destination = 0;
    int replace = 0;
    uint64_t draws = 0, revision = 0;
    std::string asset;
    bool asset_excluded = false, after_clear = false;
    std::vector<Rectangle> rectangles;
    uint32_t background_texture = 0;
    std::vector<Vertex> background;
    std::string cache_key;
    std::vector<uint8_t> cache;
    Graphics(int w, int h, uint8_t *f) : width(w), height(h), frame(f), viewport{{0, 0, w, h}} {}
    Matrix &matrix() { return matrices.at(matrix_mode).back(); }
    static uint32_t limit(uint32_t n, uint32_t max, const char *message) {
        if (n > max)
            throw std::runtime_error(message);
        return n;
    }
    static float floating(uint32_t bits) {
        float value;
        std::memcpy(&value, &bits, 4);
        if (!std::isfinite(value))
            throw std::runtime_error("Nonfinite GLES value");
        return value;
    }
    double f(NativeCall &call, unsigned index) { return floating(call.argument(index)); }
    std::vector<uint8_t> read(NativeCall &call, uint32_t buffer, uint64_t offset, size_t length) {
        if (length > 64 * 1024 * 1024)
            throw std::runtime_error("GLES upload exceeds its size limit");
        std::vector<uint8_t> out(length);
        if (buffer) {
            const auto &bytes = buffers.at(buffer);
            if (offset > bytes.size() || length > bytes.size() - offset)
                throw std::runtime_error("GLES buffer read exceeds its bounds");
            std::copy_n(bytes.data() + offset, length, out.data());
        } else if (length)
            call.read(offset, out.data(), length);
        return out;
    }
    std::vector<std::array<double, 4>> attributes(NativeCall &call, uint32_t which,
                                                  const std::vector<uint32_t> &indices,
                                                  std::array<double, 4> defaults) {
        if (!arrays[which])
            return std::vector<std::array<double, 4>>(indices.size(), defaults);
        const auto &p = pointers.at(which);
        if (p.size < 1 || p.size > 4)
            throw std::runtime_error("Invalid GLES attribute size");
        unsigned width = p.kind == 0x1401 ? 1 : p.kind == 0x1402 ? 2 : 4;
        if (p.kind != 0x1401 && p.kind != 0x1402 && p.kind != 0x1406 && p.kind != 0x140c)
            throw std::runtime_error("Unsupported GLES attribute type");
        if (indices.empty())
            return {};
        auto bounds = std::minmax_element(indices.begin(), indices.end());
        uint64_t step = p.stride ? p.stride : p.size * width;
        uint64_t start = uint64_t(p.pointer) + *bounds.first * step;
        uint64_t length = uint64_t(*bounds.second - *bounds.first) * step + p.size * width;
        auto raw = read(call, p.buffer, start, length);
        std::vector<std::array<double, 4>> values;
        values.reserve(indices.size());
        for (auto index : indices) {
            auto value = defaults;
            // Missing position Z/W follow GLES defaults, rather than stale client data.
            if (which == 0x8074)
                value = {{0, 0, 0, 1}};
            const uint8_t *bytes = raw.data() + (index - *bounds.first) * step;
            for (unsigned i = 0; i < p.size; ++i) {
                if (p.kind == 0x1401)
                    value[i] = bytes[i] / (which == 0x8076 ? 255.0 : 1.0);
                else if (p.kind == 0x1402) {
                    int16_t v;
                    std::memcpy(&v, bytes + i * 2, 2);
                    value[i] = v;
                } else if (p.kind == 0x140c) {
                    int32_t v;
                    std::memcpy(&v, bytes + i * 4, 4);
                    value[i] = v / 65536.0;
                } else {
                    float v;
                    std::memcpy(&v, bytes + i * 4, 4);
                    if (!std::isfinite(v))
                        throw std::runtime_error("Nonfinite GLES attribute");
                    value[i] = v;
                }
            }
            values.push_back(value);
        }
        return values;
    }
    template <class T> static void append(std::string &key, const T &value) {
        key.append(reinterpret_cast<const char *>(&value), sizeof(value));
    }
    void draw(NativeCall &call, uint32_t mode, const std::vector<uint32_t> &indices) {
        auto matrix = multiply(matrices.at(0x1701).back(), matrices.at(0x1700).back());
        auto positions = attributes(call, 0x8074, indices, {{0, 0, 0, 1}});
        auto uvs = attributes(call, 0x8078, indices, {{0, 0, 0, 1}});
        auto colors = attributes(call, 0x8076, indices, color);
        std::vector<Vertex> vertices;
        vertices.reserve(indices.size());
        for (size_t i = 0; i < positions.size(); ++i) {
            const auto &p = positions[i];
            double cw = matrix[3] * p[0] + matrix[7] * p[1] + matrix[11] * p[2] + matrix[15] * p[3];
            double cx = matrix[0] * p[0] + matrix[4] * p[1] + matrix[8] * p[2] + matrix[12] * p[3];
            double cy = matrix[1] * p[0] + matrix[5] * p[1] + matrix[9] * p[2] + matrix[13] * p[3];
            if (cw == 0)
                throw std::runtime_error("GLES vertex has zero homogeneous coordinate");
            vertices.push_back(
                {{viewport[0] + (cx / cw + 1) * viewport[2] / 2.0,
                  height - (viewport[1] + (cy / cw + 1) * viewport[3] / 2.0), uvs[i][0], uvs[i][1],
                  colors[i][0], colors[i][1], colors[i][2], colors[i][3]}});
        }
        std::vector<Vertex> triangles;
        if (mode == 4)
            triangles = vertices;
        else if (mode == 5) {
            for (size_t i = 0; i + 2 < vertices.size(); ++i)
                for (size_t j = 0; j < 3; ++j)
                    triangles.push_back(vertices[i + j]);
        } else if (mode == 6) {
            for (size_t i = 1; i + 1 < vertices.size(); ++i) {
                triangles.push_back(vertices[0]);
                triangles.push_back(vertices[i]);
                triangles.push_back(vertices[i + 1]);
            }
        } else if (mode == 0) {
            auto sizes = attributes(call, 0x8b9c, indices, {{1, 0, 0, 1}});
            for (size_t i = 0; i < vertices.size(); ++i) {
                auto v = vertices[i];
                double r = sizes[i][0] / 2;
                std::array<Vertex, 4> quad{{{{v[0] - r, v[1] - r, 0, 0, v[4], v[5], v[6], v[7]}},
                                            {{v[0] + r, v[1] - r, 1, 0, v[4], v[5], v[6], v[7]}},
                                            {{v[0] - r, v[1] + r, 0, 1, v[4], v[5], v[6], v[7]}},
                                            {{v[0] + r, v[1] + r, 1, 1, v[4], v[5], v[6], v[7]}}}};
                for (int j : {0, 1, 2, 2, 1, 3})
                    triangles.push_back(quad[j]);
            }
        } else
            throw std::runtime_error("Unsupported GLES primitive");
        if (triangles.empty())
            return;
        Texture *tex = enabled[0xde1] && textures.count(texture) ? &textures.at(texture) : nullptr;
        Rectangle box{vertices[0][0], vertices[0][1], vertices[0][0], vertices[0][1],
                      tex ? tex->excluded : 0};
        for (const auto &v : vertices) {
            box.left = std::min(box.left, v[0]);
            box.top = std::min(box.top, v[1]);
            box.right = std::max(box.right, v[0]);
            box.bottom = std::max(box.bottom, v[1]);
        }
        rectangles.push_back(box);
        if (box.excluded)
            return;
        bool cached = after_clear && tex && vertices.size() <= 6 && box.left <= 0 && box.top <= 0 &&
                      box.right >= width && box.bottom >= height;
        std::string key;
        if (cached) {
            auto slash = tex->asset.find_last_of('/');
            if (tex->asset.compare(slash == std::string::npos ? 0 : slash + 1, 4, "bck_") == 0) {
                background_texture = texture;
                background = vertices;
            }
            append(key, texture);
            append(key, tex->revision);
            append(key, clear_color);
            append(key, source);
            append(key, destination);
            append(key, replace);
            append(key, enabled[0xbe2]);
            for (const auto &v : triangles)
                append(key, v);
        }
        after_clear = false;
        if (cached && !cache.empty() && key == cache_key) {
            std::copy(cache.begin(), cache.end(), frame);
            ++draws;
            return;
        }
        std::vector<float> data;
        data.reserve(triangles.size() * 8);
        for (const auto &v : triangles)
            for (auto n : v)
                data.push_back(n);
        auto param = [&](uint32_t name, uint32_t fallback) {
            return tex && tex->params.count(name) ? tex->params.at(name) : fallback;
        };
        raster(frame, width, height, data.data(), triangles.size(),
               tex && !tex->pixels.empty() ? tex->pixels.data() : nullptr, tex ? tex->width : 0,
               tex ? tex->height : 0, param(0x2800, 9729) == 9729, param(0x2802, 10497) == 10497,
               param(0x2803, 10497) == 10497, enabled[0xbe2], source, destination, replace);
        if (cached) {
            cache_key = std::move(key);
            cache.assign(frame, frame + size_t(width) * height * 4);
        }
        ++draws;
    }
    void texture_image(NativeCall &call) {
        auto w = limit(call.argument(3), 4096, "Invalid texture width"),
             h = limit(call.argument(4), 4096, "Invalid texture height");
        uint32_t fmt = call.argument(6), kind = call.argument(7), pointer = call.argument(8);
        auto &t = textures[texture];
        t.width = w;
        t.height = h;
        t.asset = asset;
        t.excluded = asset_excluded;
        t.revision = ++revision;
        t.pixels.assign(size_t(w) * h * 4, 0);
        if (!pointer)
            return;
        if (kind == 0x1401) {
            int channels = fmt == 0x1908                      ? 4
                           : fmt == 0x1907                    ? 3
                           : fmt == 0x190a                    ? 2
                           : (fmt == 0x1906 || fmt == 0x1909) ? 1
                                                              : 0;
            if (!channels)
                throw std::runtime_error("Unsupported GLES texture format");
            auto raw = read(call, 0, pointer, size_t(w) * h * channels);
            for (size_t i = 0; i < size_t(w) * h; ++i) {
                auto *out = t.pixels.data() + i * 4;
                const auto *in = raw.data() + i * channels;
                if (fmt == 0x1906) {
                    out[0] = out[1] = out[2] = 255;
                    out[3] = in[0];
                } else if (channels <= 2) {
                    out[0] = out[1] = out[2] = in[0];
                    out[3] = channels == 2 ? in[1] : 255;
                } else {
                    std::copy_n(in, channels, out);
                    if (channels == 3)
                        out[3] = 255;
                }
            }
        } else if (kind == 0x8363 || kind == 0x8033 || kind == 0x8034) {
            auto raw = read(call, 0, pointer, size_t(w) * h * 2);
            unpack16(t.pixels.data(), reinterpret_cast<const uint16_t *>(raw.data()), w * h, kind);
        } else
            throw std::runtime_error("Unsupported GLES texture type");
    }
    void call(NativeCall &c, NativeResult &result) {
        const std::string name(c.name);
        auto a = [&](unsigned i) { return c.argument(i); };
        auto put = [&](uint64_t address, uint32_t value) { c.write(address, &value, 4); };
        if (name == "glGenTextures" || name == "glGenBuffers") {
            auto n = limit(a(0), 1 << 20, "Too many GLES objects");
            uint64_t output = a(1);
            for (uint32_t i = 0; i < n; ++i) {
                uint32_t id;
                if (name == "glGenTextures") {
                    id = next_texture++;
                    textures[id] = Texture{};
                    ++revision;
                } else {
                    id = next_buffer++;
                    buffers[id] = {};
                }
                put(output + i * 4, id);
            }
        } else if (name == "glDeleteTextures" || name == "glDeleteBuffers") {
            auto raw = read(c, 0, a(1), limit(a(0), 1 << 20, "Too many GLES objects") * 4);
            for (size_t i = 0; i < raw.size(); i += 4) {
                uint32_t id;
                std::memcpy(&id, raw.data() + i, 4);
                if (name == "glDeleteTextures") {
                    textures.erase(id);
                    ++revision;
                } else
                    buffers.erase(id);
            }
        } else if (name == "glBindTexture")
            texture = a(1);
        else if (name == "glTexParameteri") {
            auto &t = textures[texture];
            if (!t.params.count(a(1)) || t.params[a(1)] != a(2)) {
                t.params[a(1)] = a(2);
                t.revision = ++revision;
            }
        } else if (name == "glTexImage2D")
            texture_image(c);
        else if (name == "glTexEnvi") {
            environment[a(1)] = a(2);
            auto mode = environment.count(0x2200) ? environment.at(0x2200) : 0x2100;
            if (mode == 0x8570) {
                const std::map<uint32_t, uint32_t> overlay{
                    {0x2200, 0x8570}, {0x8571, 0x8575}, {0x8580, 0x8578}, {0x8581, 0x1702},
                    {0x8582, 0x8578}, {0x8590, 0x300},  {0x8591, 0x300},  {0x8592, 0x302},
                    {0x8572, 0x1e01}, {0x8588, 0x1702}, {0x8598, 0x302}};
                // shortcut: other combiners retain modulation; extend when their effects are
                // implemented.
                replace = 2;
                for (auto item : overlay)
                    if (!environment.count(item.first) || environment[item.first] != item.second) {
                        replace = 0;
                        break;
                    }
            } else
                replace = mode == 0x1e01;
        } else if (name == "glEnable" || name == "glDisable")
            enabled[a(0)] = name == "glEnable";
        else if (name == "glEnableClientState" || name == "glDisableClientState")
            arrays[a(0)] = name == "glEnableClientState";
        else if (name == "glBlendFunc") {
            source = a(0);
            destination = a(1);
            if ((source != 0 && source != 1 && source != 770 && source != 771) ||
                (destination != 0 && destination != 1 && destination != 768 && destination != 770 &&
                 destination != 771))
                throw std::runtime_error("Unsupported GLES blend function");
        } else if (name == "glVertexPointer" || name == "glColorPointer" ||
                   name == "glTexCoordPointer" || name == "glPointSizePointerOES") {
            bool point = name == "glPointSizePointerOES";
            uint32_t which = point                       ? 0x8b9c
                             : name == "glVertexPointer" ? 0x8074
                             : name == "glColorPointer"  ? 0x8076
                                                         : 0x8078;
            pointers[which] = Pointer{point ? 1 : a(0), a(point ? 0 : 1), a(point ? 1 : 2),
                                      a(point ? 2 : 3), bound[0x8892]};
        } else if (name == "glColor4f" || name == "glClearColor" || name == "glColor4ub") {
            auto &target = name == "glClearColor" ? clear_color : color;
            for (unsigned i = 0; i < 4; ++i)
                target[i] = name == "glColor4ub" ? a(i) / 255.0 : f(c, i);
        } else if (name == "glClear") {
            if (a(0) & 0x4000) {
                std::array<uint8_t, 4> rgba;
                for (int i = 0; i < 4; ++i)
                    rgba[i] = std::max(0.0, std::min(255.0, std::nearbyint(clear_color[i] * 255)));
                for (size_t i = 0; i < size_t(width) * height; ++i)
                    std::copy(rgba.begin(), rgba.end(), frame + i * 4);
                after_clear = true;
            }
        } else if (name == "glMatrixMode") {
            if (!matrices.count(a(0)))
                throw std::runtime_error("Invalid GLES matrix mode");
            matrix_mode = a(0);
        } else if (name == "glLoadIdentity")
            matrix() = identity;
        else if (name == "glPushMatrix") {
            auto &stack = matrices.at(matrix_mode);
            if (stack.size() >= 1024)
                throw std::runtime_error("GLES matrix stack overflow");
            stack.push_back(stack.back());
        } else if (name == "glPopMatrix") {
            auto &stack = matrices.at(matrix_mode);
            if (stack.size() < 2)
                throw std::runtime_error("GLES matrix stack underflow");
            stack.pop_back();
        } else if (name == "glTranslatef" || name == "glScalef") {
            auto mat = identity;
            for (int i = 0; i < 3; ++i)
                mat[name == "glScalef" ? i * 5 : 12 + i] = f(c, i);
            matrix() = multiply(matrix(), mat);
        } else if (name == "glRotatef") {
            double angle = f(c, 0), x = f(c, 1), y = f(c, 2), z = f(c, 3),
                   len = std::sqrt(x * x + y * y + z * z);
            if (len) {
                x /= len;
                y /= len;
                z /= len;
                double co = std::cos(angle * 3.14159265358979323846 / 180),
                       si = std::sin(angle * 3.14159265358979323846 / 180), t = 1 - co;
                Matrix mat{{t * x * x + co, t * x * y + si * z, t * x * z - si * y, 0,
                            t * x * y - si * z, t * y * y + co, t * y * z + si * x, 0,
                            t * x * z + si * y, t * y * z - si * x, t * z * z + co, 0, 0, 0, 0, 1}};
                matrix() = multiply(matrix(), mat);
            }
        } else if (name == "glOrthof") {
            double l = f(c, 0), r = f(c, 1), b = f(c, 2), t = f(c, 3), n = f(c, 4), far = f(c, 5);
            if (r == l || t == b || far == n)
                throw std::runtime_error("Invalid GLES projection");
            Matrix mat{{2 / (r - l), 0, 0, 0, 0, 2 / (t - b), 0, 0, 0, 0, -2 / (far - n), 0,
                        -(r + l) / (r - l), -(t + b) / (t - b), -(far + n) / (far - n), 1}};
            matrix() = multiply(matrix(), mat);
        } else if (name == "glViewport") {
            for (unsigned i = 0; i < 4; ++i)
                viewport[i] = a(i);
        } else if (name == "glGetIntegerv") {
            uint32_t key = a(0), output = a(1);
            if (key == 0xba2)
                c.write(output, viewport.data(), 16);
            else {
                std::map<uint32_t, uint32_t> values{{0x8069, texture}, {0xd33, 4096}, {0x84e2, 2},
                                                    {0xd05, 4},        {0xcf5, 4},    {0x86a2, 0}};
                put(output, values[key]);
            }
        } else if (name == "glDrawArrays") {
            uint32_t start = a(1), n = limit(a(2), 1 << 20, "Too many GLES vertices");
            if (uint64_t(start) + n > 0xffffffff)
                throw std::runtime_error("GLES index overflow");
            std::vector<uint32_t> indices(n);
            for (uint32_t i = 0; i < n; ++i)
                indices[i] = start + i;
            draw(c, a(0), indices);
        } else if (name == "glDrawElements") {
            uint32_t n = limit(a(1), 1 << 20, "Too many GLES indices"), kind = a(2),
                     size = kind == 0x1401 ? 1 : 2;
            if (kind != 0x1401 && kind != 0x1403)
                throw std::runtime_error("Unsupported GLES index type");
            auto raw = read(c, bound[0x8893], a(3), size_t(n) * size);
            std::vector<uint32_t> indices(n);
            for (uint32_t i = 0; i < n; ++i) {
                if (size == 1)
                    indices[i] = raw[i];
                else {
                    uint16_t value;
                    std::memcpy(&value, raw.data() + i * 2, 2);
                    indices[i] = value;
                }
            }
            draw(c, a(0), indices);
        } else if (name == "glBindBuffer")
            bound[a(0)] = a(1);
        else if (name == "glBufferData") {
            auto size = limit(a(1), 64 * 1024 * 1024, "GLES buffer exceeds its size limit");
            buffers[bound.at(a(0))] = a(2) ? read(c, 0, a(2), size) : std::vector<uint8_t>(size);
        } else if (name == "glBufferSubData") {
            auto &buffer = buffers.at(bound.at(a(0)));
            uint64_t offset = a(1), size = a(2);
            if (offset > buffer.size() || size > buffer.size() - offset)
                throw std::runtime_error("GLES buffer upload exceeds its bounds");
            auto raw = read(c, 0, a(3), size);
            std::copy(raw.begin(), raw.end(), buffer.begin() + offset);
        } else if (name == "glActiveTexture") {
            units[texture_unit] = Unit{texture, enabled[0xde1], replace, environment};
            texture_unit = a(0);
            auto unit = units[texture_unit];
            texture = unit.texture;
            enabled[0xde1] = unit.enabled;
            replace = unit.replace;
            environment = unit.env;
        } else if (name == "glClientActiveTexture") {
            clients[client_unit] = Client{arrays[0x8078], pointers[0x8078]};
            client_unit = a(0);
            auto unit = clients[client_unit];
            arrays[0x8078] = unit.enabled;
            pointers[0x8078] = unit.pointer;
        } else if (name == "eglGetCurrentDisplay" || name == "eglGetCurrentSurface" ||
                   name == "eglSwapBuffers") {
            result.value = 1;
            result.flags = 1;
        } else if (name != "glTexEnvfv" && name != "glDepthMask")
            throw std::runtime_error("Unknown native GLES import: " + name);
    }
};
} // namespace
extern "C" {
void *graphics_create(int width, int height, uint8_t *frame) {
    try {
        if (width <= 0 || height <= 0 || width > 4096 || height > 4096 || !frame)
            return nullptr;
        return new Graphics(width, height, frame);
    } catch (...) {
        return nullptr;
    }
}
void graphics_destroy(void *handle) { delete static_cast<Graphics *>(handle); }
int graphics_call(void *handle, NativeCall *call, NativeResult *result) {
    static_cast<Graphics *>(handle)->call(*call, *result);
    return 0;
}
void graphics_asset(void *handle, const char *asset, int excluded) {
    auto &g = *static_cast<Graphics *>(handle);
    g.asset = asset;
    g.asset_excluded = excluded;
}
uint64_t graphics_draws(void *handle) { return static_cast<Graphics *>(handle)->draws; }
uint32_t graphics_bound_texture(void *handle) { return static_cast<Graphics *>(handle)->texture; }
uint64_t graphics_revision(void *handle) { return static_cast<Graphics *>(handle)->revision; }
uint32_t graphics_texture_count(void *handle) {
    return static_cast<Graphics *>(handle)->textures.size();
}
uint32_t graphics_texture_id(void *handle, uint32_t index) {
    auto &t = static_cast<Graphics *>(handle)->textures;
    auto it = t.begin();
    std::advance(it, std::min<size_t>(index, t.size()));
    return it == t.end() ? 0 : it->first;
}
struct TextureInfo {
    uint32_t width, height;
    const uint8_t *pixels;
    size_t size;
    const char *asset;
    int excluded;
    uint64_t revision;
};
int graphics_texture_info(void *handle, uint32_t id, TextureInfo *info) {
    auto &t = static_cast<Graphics *>(handle)->textures;
    auto it = t.find(id);
    if (it == t.end())
        return 0;
    auto &v = it->second;
    *info = TextureInfo{uint32_t(v.width), uint32_t(v.height), v.pixels.data(), v.pixels.size(),
                        v.asset.c_str(),   v.excluded,         v.revision};
    return 1;
}
uint32_t graphics_rectangles(void *handle, const Rectangle **out) {
    auto &r = static_cast<Graphics *>(handle)->rectangles;
    *out = r.data();
    return r.size();
}
void graphics_reset_rectangles(void *handle) {
    static_cast<Graphics *>(handle)->rectangles.clear();
}
uint32_t graphics_background(void *handle, uint32_t *texture, double *vertices) {
    auto &g = *static_cast<Graphics *>(handle);
    *texture = g.background_texture;
    if (g.background.size() > 6)
        return 0;
    for (size_t i = 0; i < g.background.size(); ++i)
        std::copy(g.background[i].begin(), g.background[i].end(), vertices + i * 8);
    return g.background.size();
}
uint32_t graphics_text_texture(void *handle, int width, int height, const uint8_t *pixels) {
    try {
        auto &g = *static_cast<Graphics *>(handle);
        if (width <= 0 || height <= 0 || width > 4096 || height > 4096)
            return 0;
        auto id = g.next_texture++;
        auto &t = g.textures[id];
        t.width = width;
        t.height = height;
        t.pixels.assign(pixels, pixels + size_t(width) * height * 4);
        t.params = {{0x2802, 33071}, {0x2803, 33071}};
        t.revision = ++g.revision;
        return id;
    } catch (...) {
        return 0;
    }
}
}
