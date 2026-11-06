"""GLES 1.1 calls from the original engine, rendered into a portable RGBA surface."""
import ctypes
import math
from pathlib import Path
import plistlib
import struct
import subprocess
import sys

from PIL import Image, ImageDraw
from commerce import excluded


IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def multiply(a, b):
    return [sum(a[row + k * 4] * b[k + col * 4] for k in range(4))
            for col in range(4) for row in range(4)]


class GLES:
    def __init__(self, machine, width, height):
        self.m = m = machine
        self.width, self.height = width, height
        self.frame = bytearray(width * height * 4)
        self.frame_pointer = (ctypes.c_uint8 * len(self.frame)).from_buffer(self.frame)
        self.viewport = (0, 0, width, height)
        self.matrices = {mode: [IDENTITY.copy()] for mode in (0x1700, 0x1701, 0x1702)}
        self.matrix_mode = 0x1700
        self.color = (1, 1, 1, 1)
        self.clear_color = (0, 0, 0, 0)
        self.enabled = set()
        self.arrays = set()
        self.pointers = {}
        self.textures = {}
        self.texture = 0
        self.next_texture = 1
        self.buffers = {}
        self.bound_buffers = {}
        self.next_buffer = 1
        self.source, self.destination = 1, 0
        self.replace = False
        self.draws = 0
        self.font = None
        self.asset_name = ''
        self.excluded_rectangles = []
        self.input_rectangles = []
        directory = Path(__file__).resolve().parent
        library = directory / ('raster.dylib' if sys.platform == 'darwin' else 'raster.so')
        source = directory / 'raster.c'
        if not library.exists() or source.stat().st_mtime > library.stat().st_mtime:
            subprocess.run(['cc', '-shared', '-fPIC', '-O3', str(source), '-o', str(library), '-lm'], check=True)
        self.library = ctypes.CDLL(str(library))
        self.native = self.library.raster
        self.native.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                               ctypes.c_void_p] + [ctypes.c_int] * 9
        self.native.restype = None
        self.unpack16 = self.library.unpack16
        self.unpack16.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self.unpack16.restype = None
        self.after_clear = False
        self.background_cache = None
        self.background_info = None
        a = m.arg
        handlers = {
            'glGenTextures': self.gen_textures,
            'glBindTexture': lambda: setattr(self, 'texture', a(1)),
            'glDeleteTextures': self.delete_textures,
            'glTexParameteri': self.texture_parameter,
            'glTexImage2D': self.texture_image,
            'glTexEnvi': lambda: setattr(self, 'replace', a(2) == 7681) if a(1) == 8704 else None,
            'glTexEnvfv': lambda: None,
            'glEnable': lambda: self.enabled.add(a(0)),
            'glDisable': lambda: self.enabled.discard(a(0)),
            'glEnableClientState': lambda: self.arrays.add(a(0)),
            'glDisableClientState': lambda: self.arrays.discard(a(0)),
            'glBlendFunc': self.blend_func,
            'glVertexPointer': lambda: self.pointer(0x8074),
            'glColorPointer': lambda: self.pointer(0x8076),
            'glTexCoordPointer': lambda: self.pointer(0x8078),
            'glPointSizePointerOES': lambda: self.pointer(0x8B9C, True),
            'glColor4f': lambda: setattr(self, 'color', tuple(self.f(i) for i in range(4))),
            'glColor4ub': lambda: setattr(self, 'color', tuple(a(i) / 255 for i in range(4))),
            'glClearColor': lambda: setattr(self, 'clear_color', tuple(self.f(i) for i in range(4))),
            'glClear': self.clear,
            'glMatrixMode': lambda: setattr(self, 'matrix_mode', a(0)),
            'glLoadIdentity': lambda: self.set_matrix(IDENTITY.copy()),
            'glPushMatrix': lambda: self.matrices[self.matrix_mode].append(self.matrix.copy()),
            'glPopMatrix': self.pop_matrix,
            'glTranslatef': self.translate,
            'glScalef': self.scale,
            'glRotatef': self.rotate,
            'glOrthof': self.ortho,
            'glViewport': lambda: setattr(self, 'viewport', tuple(a(i) for i in range(4))),
            'glGetIntegerv': self.get_integer,
            'glDrawArrays': lambda: self.draw(a(0), list(range(a(1), a(1) + a(2)))),
            'glDrawElements': self.draw_elements,
            'glGenBuffers': self.gen_buffers,
            'glBindBuffer': lambda: self.bound_buffers.__setitem__(a(0), a(1)),
            'glBufferData': self.buffer_data,
            'glBufferSubData': self.buffer_subdata,
            'glDeleteBuffers': self.delete_buffers,
            'glActiveTexture': lambda: None,
            'glClientActiveTexture': lambda: None,
            'glDepthMask': lambda: None,
            'eglGetCurrentDisplay': lambda: 1,
            'eglGetCurrentSurface': lambda: 1,
            'eglSwapBuffers': lambda: 1,
        }
        m.handlers.update(handlers)

    @property
    def matrix(self):
        return self.matrices[self.matrix_mode][-1]

    def set_matrix(self, value):
        self.matrices[self.matrix_mode][-1] = value

    def pop_matrix(self):
        self.matrices[self.matrix_mode].pop()

    def f(self, index):
        return struct.unpack('<f', struct.pack('<I', self.m.arg(index)))[0]

    def image(self):
        return Image.frombytes('RGBA', (self.width, self.height), bytes(self.frame))

    def clear(self):
        if self.m.arg(0) & 0x4000:
            self.frame[:] = bytes(max(0, min(255, round(c * 255))) for c in self.clear_color) * (self.width * self.height)
            self.after_clear = True

    def blend_func(self):
        self.source, self.destination = self.m.arg(0), self.m.arg(1)
        if self.source not in (0, 1, 770, 771) or self.destination not in (0, 1, 770, 771):
            raise NotImplementedError(f'GLES blend {self.source}, {self.destination}')

    def translate(self):
        matrix = IDENTITY.copy()
        matrix[12:15] = [self.f(i) for i in range(3)]
        self.set_matrix(multiply(self.matrix, matrix))

    def scale(self):
        matrix = IDENTITY.copy()
        matrix[0], matrix[5], matrix[10] = (self.f(i) for i in range(3))
        self.set_matrix(multiply(self.matrix, matrix))

    def rotate(self):
        angle, x, y, z = (self.f(i) for i in range(4))
        length = math.sqrt(x*x + y*y + z*z)
        if not length:
            return
        x, y, z = x/length, y/length, z/length
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        t = 1 - c
        rotation = [t*x*x+c, t*x*y+s*z, t*x*z-s*y, 0,
                    t*x*y-s*z, t*y*y+c, t*y*z+s*x, 0,
                    t*x*z+s*y, t*y*z-s*x, t*z*z+c, 0, 0, 0, 0, 1]
        self.set_matrix(multiply(self.matrix, rotation))

    def ortho(self):
        l, r, b, t, n, f = (self.f(i) for i in range(6))
        matrix = [2/(r-l), 0, 0, 0, 0, 2/(t-b), 0, 0, 0, 0, -2/(f-n), 0,
                  -(r+l)/(r-l), -(t+b)/(t-b), -(f+n)/(f-n), 1]
        self.set_matrix(multiply(self.matrix, matrix))

    def get_integer(self):
        name, pointer = self.m.arg(0), self.m.arg(1)
        if name == 0xBA2:
            self.m.uc.mem_write(pointer, struct.pack('<4I', *self.viewport))
        else:
            self.m.put32(pointer, {0x8069: self.texture, 0xD33: 4096, 0x84E2: 2, 0xD05: 4, 0xCF5: 4, 0x86A2: 0}.get(name, 0))

    def gen_textures(self):
        for i in range(self.m.arg(0)):
            self.m.put32(self.m.arg(1) + i*4, self.next_texture)
            self.textures[self.next_texture] = {'params': {}}
            self.next_texture += 1

    def delete_textures(self):
        for i in range(self.m.arg(0)):
            self.textures.pop(self.m.u32(self.m.arg(1) + i*4), None)

    def texture_parameter(self):
        self.textures.setdefault(self.texture, {'params': {}})['params'][self.m.arg(1)] = self.m.arg(2)

    def texture_image(self):
        m = self.m
        width, height, fmt, kind, pointer = (m.arg(i) for i in (3, 4, 6, 7, 8))
        if width > 4096 or height > 4096:
            raise ValueError(f'Invalid texture dimensions {width}x{height}')
        if not pointer:
            image = Image.new('RGBA', (width, height))
        elif kind == 0x1401:
            mode = {0x1908: 'RGBA', 0x1907: 'RGB', 0x1906: 'L', 0x1909: 'L', 0x190A: 'LA'}[fmt]
            data = bytes(m.uc.mem_read(pointer, width * height * len(mode)))
            image = Image.frombytes(mode, (width, height), data).convert('RGBA')
            if fmt == 0x1906:
                image = Image.new('RGBA', (width, height), 'white')
                image.putalpha(Image.frombytes('L', (width, height), data))
        elif kind in (0x8363, 0x8033, 0x8034):
            raw = bytes(m.uc.mem_read(pointer, width * height * 2))
            output = ctypes.create_string_buffer(width*height*4)
            self.unpack16(output,ctypes.c_char_p(raw),width*height,kind)
            image = Image.frombytes('RGBA', (width, height), output.raw)
        else:
            raise NotImplementedError(f'GLES texture type {kind:#x}')
        texture = self.textures.setdefault(self.texture, {'params': {}})
        texture.update(width=width, height=height, pixels=image.tobytes(), excluded=excluded(self.asset_name), asset=self.asset_name)

    def gen_buffers(self):
        for i in range(self.m.arg(0)):
            self.m.put32(self.m.arg(1) + i*4, self.next_buffer)
            self.buffers[self.next_buffer] = bytearray()
            self.next_buffer += 1

    def delete_buffers(self):
        for i in range(self.m.arg(0)):
            self.buffers.pop(self.m.u32(self.m.arg(1) + i*4), None)

    def buffer_data(self):
        m = self.m
        value = bytearray(m.uc.mem_read(m.arg(2), m.arg(1))) if m.arg(2) else bytearray(m.arg(1))
        self.buffers[self.bound_buffers[m.arg(0)]] = value

    def buffer_subdata(self):
        m = self.m
        buffer = self.buffers[self.bound_buffers[m.arg(0)]]
        offset, size = m.arg(1), m.arg(2)
        if offset + size > len(buffer):
            raise ValueError('GLES buffer upload exceeds its bounds')
        buffer[offset:offset+size] = m.uc.mem_read(m.arg(3), size)

    def pointer(self, which, point=False):
        m = self.m
        size, kind, stride, pointer = (1, m.arg(0), m.arg(1), m.arg(2)) if point else tuple(m.arg(i) for i in range(4))
        self.pointers[which] = (size, kind, stride, pointer, self.bound_buffers.get(0x8892, 0))

    def attribute(self, which, index, default):
        return self.attributes(which, [index], default)[0]

    def attributes(self, which, indices, default):
        if which not in self.arrays:
            return [default] * len(indices)
        size, kind, stride, pointer, buffer = self.pointers[which]
        code, width = {0x1406: ('f', 4), 0x1401: ('B', 1), 0x1402: ('h', 2), 0x140C: ('i', 4)}[kind]
        if not indices:
            return []
        step = stride or size * width
        first, last = min(indices), max(indices)
        offset, length = pointer + first*step, (last-first)*step + size*width
        raw = self.buffers[buffer][offset:offset+length] if buffer else self.m.uc.mem_read(offset, length)
        unpack = struct.Struct('<' + code*size).unpack_from
        values = [unpack(raw, (index-first)*step) for index in indices]
        if kind == 0x1401 and which == 0x8076:
            values = [tuple(x/255 for x in value) for value in values]
        if kind == 0x140C:
            values = [tuple(x/65536 for x in value) for value in values]
        return values

    def draw_elements(self):
        m = self.m
        count, kind, pointer = m.arg(1), m.arg(2), m.arg(3)
        code, size = {0x1401: ('B', 1), 0x1403: ('H', 2)}[kind]
        buffer = self.bound_buffers.get(0x8893, 0)
        raw = self.buffers[buffer][pointer:pointer+count*size] if buffer else m.uc.mem_read(pointer, count*size)
        self.draw(m.arg(0), list(struct.unpack('<' + code*count, raw)))

    def draw(self, mode, indices):
        matrix = multiply(self.matrices[0x1701][-1], self.matrices[0x1700][-1])
        vertices = []
        vx, vy, vw, vh = self.viewport
        positions = self.attributes(0x8074, indices, (0, 0))
        uvs = self.attributes(0x8078, indices, (0, 0))
        colors = self.attributes(0x8076, indices, self.color)
        for position, uv, color in zip(positions, uvs, colors):
            px, py = position[:2]
            pz, pw = position[2] if len(position)>2 else 0, position[3] if len(position)>3 else 1
            cw = matrix[3]*px + matrix[7]*py + matrix[11]*pz + matrix[15]*pw
            cx = matrix[0]*px + matrix[4]*py + matrix[8]*pz + matrix[12]*pw
            cy = matrix[1]*px + matrix[5]*py + matrix[9]*pz + matrix[13]*pw
            x = vx + (cx/cw + 1) * vw/2
            y = self.height - (vy + (cy/cw + 1) * vh/2)
            vertices.append((x, y, *uv[:2], *color))
        if mode == 4:
            triangles = vertices
        elif mode == 5:
            triangles = [v for i in range(len(vertices)-2) for v in (vertices[i], vertices[i+1], vertices[i+2])]
        elif mode == 6:
            triangles = [v for i in range(1, len(vertices)-1) for v in (vertices[0], vertices[i], vertices[i+1])]
        elif mode == 0:
            triangles = []
            for index, v in zip(indices, vertices):
                radius = self.attribute(0x8B9C, index, (1,))[0] / 2
                x, y = v[:2]
                quad = [(x-radius, y-radius, 0, 0, *v[4:]), (x+radius, y-radius, 1, 0, *v[4:]),
                        (x-radius, y+radius, 0, 1, *v[4:]), (x+radius, y+radius, 1, 1, *v[4:])]
                triangles.extend(quad[i] for i in (0, 1, 2, 2, 1, 3))
        else:
            raise NotImplementedError(f'GLES primitive {mode}')
        if not triangles:
            return
        data = (ctypes.c_float * (len(triangles)*8))(*(x for v in triangles for x in v))
        texture = self.textures.get(self.texture) if 0xDE1 in self.enabled else None
        rectangle = (min(v[0] for v in vertices), min(v[1] for v in vertices),
                     max(v[0] for v in vertices), max(v[1] for v in vertices))
        self.input_rectangles.append((rectangle, bool(texture and texture.get('excluded'))))
        if texture and texture.get('excluded'):
            self.excluded_rectangles.append(rectangle)
            return
        params = texture['params'] if texture else {}
        cache_key = None
        if self.after_clear and texture and len(vertices) <= 6 and rectangle[0] <= 0 and rectangle[1] <= 0 and rectangle[2] >= self.width and rectangle[3] >= self.height:
            if Path(texture.get('asset','')).name.startswith('bck_'):
                self.background_info = (self.texture, tuple(vertices))
            cache_key = (tuple(triangles), texture.get('pixels'), texture.get('width'), texture.get('height'), tuple(sorted(params.items())), self.clear_color,
                         0xBE2 in self.enabled, self.source, self.destination, self.replace)
        self.after_clear = False
        if cache_key is not None and self.background_cache and self.background_cache[0] == cache_key:
            self.frame[:] = self.background_cache[1]
            self.draws += 1
            return
        pixels = ctypes.c_char_p(texture['pixels']) if texture and 'pixels' in texture else None
        self.native(self.frame_pointer, self.width, self.height, data, len(triangles), pixels,
                    texture.get('width', 0) if texture else 0, texture.get('height', 0) if texture else 0,
                    params.get(0x2800, 9729) == 9729, params.get(0x2802, 10497) == 10497,
                    params.get(0x2803, 10497) == 10497, 0xBE2 in self.enabled, self.source, self.destination, self.replace)
        if cache_key is not None:
            self.background_cache = (cache_key, bytes(self.frame))
        self.draws += 1

    def text_texture(self, message, obj, jni):
        width, height = jni.field(obj, 'width'), jni.field(obj, 'height')
        tm = struct.unpack('<f', struct.pack('<I', jni.field(obj, 'texture_multiplier')))[0]
        if not 0 < width <= 4096 or not 0 < height <= 4096:
            raise ValueError('Invalid generated text texture dimensions')
        if self.font is None:
            with (jni.assets / 'doodlejumpfont.plist').open('rb') as source:
                glyphs = plistlib.load(source)['chars']['char']
            self.font = {int(g['-id']): {k[1:]: int(v) for k, v in g.items()} for g in glyphs}
            self.font_atlas = Image.open(jni.assets / 'fonts/doodlejumpfont_X.png').convert('RGBA')
        image = Image.new('RGBA', (width, height))
        size = 12 * tm
        cap = self.font[ord('H')]
        scale = size * .72 / cap['height']
        def text(value, x, baseline, align):
            glyphs = [self.font[ord(c)] for c in value if ord(c) in self.font]
            pen = x - sum(g['xadvance'] for g in glyphs) * scale * align
            for g in glyphs:
                cell = self.font_atlas.crop((g['x'], g['y'], g['x']+g['width'], g['y']+g['height']))
                w, h = max(1, round(g['width']*scale)), max(1, round(g['height']*scale))
                alpha = cell.getchannel('A').resize((w, h), Image.Resampling.BILINEAR)
                cell = Image.new('RGBA', (w, h), (0, 0, 0, 0))
                cell.putalpha(alpha)
                image.alpha_composite(cell, (round(pen+g['xoffset']*scale), round(baseline-(cap['yoffset']+cap['height']-g['yoffset'])*scale)))
                pen += g['xadvance']*scale
        def strings(field):
            handle = jni.field(obj, field)
            if not handle:
                return []
            array = jni.values[handle]
            return [jni.text(self.m.u32(array['data'] + i*4)) for i in range(array['length'])]
        if message == 63:
            for i, value in enumerate(strings('texts')):
                text(value, 128*tm, tm*(i*50+25) + size*.72/2, .5)
        else:
            draw = ImageDraw.Draw(image)
            for field in ('local_names', 'recent_names'):
                for i, value in enumerate(strings(field)):
                    draw.rectangle((0, round(height-tm*(i*24+7)), round(tm*64), round(height-tm*(i*24+3))-1), fill=(0, 85, 170, 255))
                    text(value[:8], tm*62, height-tm*(i*24+7), 1)
            image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        identifier = self.next_texture
        self.next_texture += 1
        self.textures[identifier] = dict(width=width, height=height, pixels=image.tobytes(), params={0x2802: 33071, 0x2803: 33071})
        jni.set_field(obj, 'texture_id', identifier)
        jni.set_field(obj, 'status', 1)
