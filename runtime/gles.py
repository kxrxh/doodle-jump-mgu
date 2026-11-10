"""Python resource adapter for the native GLES 1.1 host."""
import ctypes
from pathlib import Path
import plistlib
import struct
import weakref

from PIL import Image, ImageDraw
from commerce import excluded
from native_backend import library


class TextureInfo(ctypes.Structure):
    _fields_ = [('width', ctypes.c_uint32), ('height', ctypes.c_uint32),
                ('pixels', ctypes.c_void_p), ('size', ctypes.c_size_t),
                ('asset', ctypes.c_char_p), ('excluded', ctypes.c_int), ('revision', ctypes.c_uint64)]


class Rectangle(ctypes.Structure):
    _fields_ = [('left', ctypes.c_double), ('top', ctypes.c_double),
                ('right', ctypes.c_double), ('bottom', ctypes.c_double), ('excluded', ctypes.c_int)]


IMPORTS = (
    'glGenTextures glBindTexture glDeleteTextures glTexParameteri glTexImage2D glTexEnvi '
    'glTexEnvfv glEnable glDisable glEnableClientState glDisableClientState glBlendFunc '
    'glVertexPointer glColorPointer glTexCoordPointer glPointSizePointerOES glColor4f '
    'glColor4ub glClearColor glClear glMatrixMode glLoadIdentity glPushMatrix glPopMatrix '
    'glTranslatef glScalef glRotatef glOrthof glViewport glGetIntegerv glDrawArrays '
    'glDrawElements glGenBuffers glBindBuffer glBufferData glBufferSubData glDeleteBuffers '
    'glActiveTexture glClientActiveTexture glDepthMask eglGetCurrentDisplay '
    'eglGetCurrentSurface eglSwapBuffers'
).split()


class GLES:
    def __init__(self, machine, width, height):
        self.m = machine
        self.width, self.height = width, height
        self.frame = bytearray(width * height * 4)
        self.frame_pointer = (ctypes.c_uint8 * len(self.frame)).from_buffer(self.frame)
        self.font = None
        self._asset_name = ''
        self._texture_revision = -1
        self._textures = {}
        self.library = lib = library()
        signatures = {
            'graphics_create': ([ctypes.c_int, ctypes.c_int, ctypes.c_void_p], ctypes.c_void_p),
            'graphics_destroy': ([ctypes.c_void_p], None),
            'graphics_asset': ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int], None),
            'graphics_draws': ([ctypes.c_void_p], ctypes.c_uint64),
            'graphics_bound_texture': ([ctypes.c_void_p], ctypes.c_uint32),
            'graphics_revision': ([ctypes.c_void_p], ctypes.c_uint64),
            'graphics_texture_count': ([ctypes.c_void_p], ctypes.c_uint32),
            'graphics_texture_id': ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_uint32),
            'graphics_texture_info': ([ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(TextureInfo)], ctypes.c_int),
            'graphics_rectangles': ([ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(Rectangle))], ctypes.c_uint32),
            'graphics_reset_rectangles': ([ctypes.c_void_p], None),
            'graphics_background': ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_double)], ctypes.c_uint32),
            'graphics_text_texture': ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p], ctypes.c_uint32),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(lib, name)
            function.argtypes, function.restype = arguments, result
        self._handle = lib.graphics_create(width, height, self.frame_pointer)
        if not self._handle:
            raise ValueError('Could not allocate native GLES renderer')
        # The native renderer holds this buffer until its context is destroyed.
        self._finalizer = weakref.finalize(self, lambda handle, frame: lib.graphics_destroy(handle), self._handle, self.frame_pointer)
        for name in IMPORTS:
            machine.register_native(name, lib.graphics_call, self._handle)
        machine._graphics = self
        self.native = lib.raster
        self.native.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                               ctypes.c_void_p] + [ctypes.c_int] * 9
        self.native.restype = None
        lib.unpack16.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.unpack16.restype = None

    @property
    def asset_name(self):
        return self._asset_name

    @asset_name.setter
    def asset_name(self, value):
        self._asset_name = value
        self.library.graphics_asset(self._handle, value.encode(), excluded(value))

    @property
    def draws(self):
        return self.library.graphics_draws(self._handle)

    @property
    def texture(self):
        return self.library.graphics_bound_texture(self._handle)

    @property
    def textures(self):
        revision = self.library.graphics_revision(self._handle)
        if revision != self._texture_revision:
            textures = {}
            for index in range(self.library.graphics_texture_count(self._handle)):
                identifier = self.library.graphics_texture_id(self._handle, index)
                textures[identifier] = self.texture_info(identifier)
            self._textures = textures
            self._texture_revision = revision
        return self._textures

    def texture_info(self, identifier):
        info = TextureInfo()
        if not self.library.graphics_texture_info(self._handle, identifier, ctypes.byref(info)):
            self._textures.pop(identifier, None)
            raise KeyError(identifier)
        value = self._textures.get(identifier)
        if value is None or value['revision'] != info.revision:
            value = dict(width=info.width, height=info.height,
                pixels=ctypes.string_at(info.pixels, info.size), asset=info.asset.decode(),
                excluded=bool(info.excluded), revision=info.revision)
            self._textures[identifier] = value
        return value

    @property
    def background_info(self):
        identifier = ctypes.c_uint32()
        vertices = (ctypes.c_double * 48)()
        count = self.library.graphics_background(self._handle, ctypes.byref(identifier), vertices)
        if count:
            return identifier.value, tuple(tuple(vertices[i*8:(i+1)*8]) for i in range(count))

    @property
    def input_rectangles(self):
        pointer = ctypes.POINTER(Rectangle)()
        count = self.library.graphics_rectangles(self._handle, ctypes.byref(pointer))
        return [((r.left, r.top, r.right, r.bottom), bool(r.excluded)) for r in pointer[:count]]

    @input_rectangles.setter
    def input_rectangles(self, value):
        if value:
            raise ValueError('Native hit rectangles can only be cleared')
        self.library.graphics_reset_rectangles(self._handle)

    @property
    def excluded_rectangles(self):
        return [rectangle for rectangle, hidden in self.input_rectangles if hidden]

    @excluded_rectangles.setter
    def excluded_rectangles(self, value):
        self.input_rectangles = value

    def image(self):
        return Image.frombytes('RGBA', (self.width, self.height), bytes(self.frame))

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
        pixels = image.tobytes()
        identifier = self.library.graphics_text_texture(self._handle, width, height, pixels)
        if not identifier:
            raise RuntimeError('Could not create native text texture')
        jni.set_field(obj, 'texture_id', identifier)
        jni.set_field(obj, 'status', 1)
