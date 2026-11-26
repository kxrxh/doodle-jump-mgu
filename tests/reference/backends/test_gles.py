"""Exercise current C++ backends through pinned external Python adapters."""
import _support

import ctypes
import importlib
import struct
import unittest

from arm import Arm
from libc import LibC
from test_arm import LIBRARY


class GraphicsTests(unittest.TestCase):
    def compact_context(self, width=4, height=4):
        m = Arm(LIBRARY)
        LibC(m)
        renderer = importlib.import_module('gles').GLES(m, width, height)
        def gl(name, *args):
            m.call(m.import_address(name), *[struct.unpack('<I', struct.pack('<f', x))[0]
                if isinstance(x, float) else x for x in args])
        identifier = m.malloc(4)
        gl('glGenTextures', 1, identifier)
        gl('glBindTexture', 0xDE1, m.u32(identifier))
        def upload(pixels, w, h, kind=0x1401, fmt=0x1908):
            pointer = m.malloc(len(pixels))
            m.uc.mem_write(pointer, pixels)
            gl('glTexImage2D', 0xDE1, 0, fmt, w, h, 0, fmt, kind, pointer)
            return pointer
        def draw():
            vertices, uv = m.malloc(32), m.malloc(32)
            m.uc.mem_write(vertices, struct.pack('<8f', 0, 0, width, 0, 0, height, width, height))
            m.uc.mem_write(uv, struct.pack('<8f', 0, 0, 1, 0, 0, 1, 1, 1))
            gl('glMatrixMode', 0x1701)
            gl('glOrthof', 0.0, float(width), float(height), 0.0, -1.0, 1.0)
            gl('glVertexPointer', 2, 0x1406, 0, vertices)
            gl('glTexCoordPointer', 2, 0x1406, 0, uv)
            gl('glEnableClientState', 0x8074)
            gl('glEnableClientState', 0x8078)
            gl('glEnable', 0xDE1)
            gl('glDrawArrays', 5, 0, 4)
        return m, renderer, gl, upload, draw

    def test_exact_palettes_reduce_storage_without_retaining_rgba_snapshots(self):
        m, renderer, gl, upload, draw = self.compact_context()
        shared = renderer.texture_storage_bytes
        for colors, width, height, limit in ((2, 32, 32, 1032), (300, 64, 64, 9392)):
            with self.subTest(colors=colors):
                pixels = b''.join(bytes((i % colors & 255, (i % colors) >> 8, 53, 255))
                                  for i in range(width * height))
                upload(pixels, width, height)
                self.assertLessEqual(renderer.texture_storage_bytes - shared, limit)
                allocated = renderer.texture_storage_bytes
                self.assertEqual(renderer.texture_info(1)['pixels'], pixels)
                self.assertEqual(renderer.texture_storage_bytes, allocated)

    def test_packed_texture_keeps_two_bytes_per_texel_and_owns_its_upload(self):
        m, renderer, gl, upload, draw = self.compact_context()
        upload(struct.pack('<H', 0), 1, 1, 0x8033)
        lookup_storage = renderer.texture_storage_bytes
        pixels = struct.pack('<1024H', *range(1024))
        pointer = upload(pixels, 32, 32, 0x8033)
        self.assertLessEqual(renderer.texture_storage_bytes, lookup_storage + len(pixels))
        m.uc.mem_write(pointer, bytes(len(pixels)))
        snapshot = renderer.texture_info(1)['pixels']
        self.assertEqual(snapshot[:8], bytes((0, 0, 0, 0, 0, 0, 0, 17)))
        self.assertEqual(snapshot[-4:], bytes((0, 51, 255, 255)))

    def test_draw_without_sampled_texture_ignores_unused_nonfinite_uvs(self):
        for state in ('unbound', 'empty', 'disabled'):
            with self.subTest(state=state):
                m, renderer, gl, upload, draw = self.compact_context()
                draw()
                if state == 'unbound':
                    gl('glBindTexture', 0xDE1, 0)
                elif state == 'disabled':
                    upload(bytes((255, 255, 255, 255)), 1, 1)
                    gl('glDisable', 0xDE1)
                uv = m.malloc(32)
                m.uc.mem_write(uv, struct.pack('<8f', *([float('nan'), float('inf')] * 4)))
                gl('glTexCoordPointer', 2, 0x1406, 0, uv)
                gl('glColor4f', 1.0, 0.0, 0.0, 1.0)
                gl('glDrawArrays', 5, 0, 4)
                self.assertEqual(renderer.image().getpixel((1, 1)), (255, 0, 0, 255))

    def test_sampled_texture_still_rejects_nonfinite_uvs(self):
        m, renderer, gl, upload, draw = self.compact_context()
        upload(bytes((255, 255, 255, 255)), 1, 1)
        draw()
        uv = m.malloc(32)
        m.uc.mem_write(uv, struct.pack('<8f', *([float('nan'), float('inf')] * 4)))
        gl('glTexCoordPointer', 2, 0x1406, 0, uv)
        with self.assertRaisesRegex(RuntimeError, 'Nonfinite GLES attribute'):
            gl('glDrawArrays', 5, 0, 4)

    def test_palette_sampling_keeps_bilinear_repeat_and_nearest_clamp(self):
        m, renderer, gl, upload, draw = self.compact_context()
        shared = renderer.texture_storage_bytes
        red, blue = bytes((255, 0, 0, 255)), bytes((0, 0, 255, 255))
        upload(red + blue + blue + red, 2, 2)
        self.assertLessEqual(renderer.texture_storage_bytes - shared, 12)
        draw()
        self.assertEqual(renderer.image().getpixel((0, 0)), (159, 0, 96, 255))
        self.assertEqual(renderer.image().getpixel((2, 0)), (96, 0, 159, 255))
        gl('glTexParameteri', 0xDE1, 0x2800, 9728)
        gl('glTexParameteri', 0xDE1, 0x2802, 33071)
        gl('glTexParameteri', 0xDE1, 0x2803, 33071)
        gl('glLoadIdentity')
        draw()
        self.assertEqual(renderer.image().getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(renderer.image().getpixel((2, 0)), (0, 0, 255, 255))

    def test_excluded_texture_omits_pixels_and_keeps_its_input_rectangle(self):
        m, renderer, gl, upload, draw = self.compact_context()
        shared = renderer.texture_storage_bytes
        renderer.asset_name = 'newspanel_L.png'
        upload(bytes((255, 0, 0, 255)) * 1024, 32, 32)
        self.assertEqual(renderer.texture_storage_bytes, shared)
        info = renderer.texture_info(1)
        self.assertEqual((info['width'], info['height'], info['pixels']), (32, 32, b''))
        draw()
        self.assertEqual(renderer.image().getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(renderer.excluded_rectangles, [(0.0, 0.0, 4.0, 4.0)])

    def test_dense_upload_replacement_and_deletion_release_storage(self):
        m, renderer, gl, upload, draw = self.compact_context()
        shared = renderer.texture_storage_bytes
        pixels = b''.join(bytes((i & 255, i >> 8 & 255, i >> 16, 255)) for i in range(257 * 256))
        upload(pixels, 257, 256)
        self.assertEqual(renderer.texture_storage_bytes - shared, len(pixels))
        self.assertEqual(renderer.texture_info(1)['pixels'], pixels)
        upload(bytes((1, 2, 3, 4)) * 1024, 32, 32)
        self.assertLessEqual(renderer.texture_storage_bytes - shared, 4)
        identifier = m.malloc(4)
        m.uc.mem_write(identifier, struct.pack('<I', 1))
        gl('glDeleteTextures', 1, identifier)
        self.assertEqual(renderer.texture_storage_bytes, shared)

    def test_packed_alpha_is_preserved_by_bilinear_sampling(self):
        for kind, values, expected in (
                (0x8033, (0xF000, 0x0F0F, 0x00F8, 0xFFFF), (159, 64, 64, 89)),
                (0x8034, (0xF800, 0x07C1, 0x003F, 0xFFFF), (159, 64, 64, 112)),
                (0x8363, (0xF800, 0x07E0, 0x001F, 0xFFFF), (159, 64, 64, 255))):
            with self.subTest(kind=kind):
                m, renderer, gl, upload, draw = self.compact_context()
                upload(struct.pack('<4H', *values), 2, 2, kind)
                draw()
                self.assertEqual(renderer.image().getpixel((0, 0)), expected)

    def test_rgba_accessor_checks_output_bounds_without_writing(self):
        m, renderer, gl, upload, draw = self.compact_context()
        upload(bytes((1, 2, 3, 4)), 1, 1)
        output = ctypes.create_string_buffer(b'untouched')
        read = renderer.library.graphics_texture_rgba
        for handle, texture, pointer, size in ((renderer._handle, 1, output, len(output)),
                (renderer._handle, 2, output, 4), (None, 1, output, 4),
                (renderer._handle, 1, None, 4)):
            with self.subTest(texture=texture, size=size, null_output=pointer is None):
                self.assertEqual(read(handle, texture, pointer, size), 0)
                self.assertEqual(output.value, b'untouched')
        self.assertEqual(read(renderer._handle, 1, output, 4), 1)
        self.assertEqual(output.raw[:4], bytes((1, 2, 3, 4)))

    def test_null_packed_upload_is_transparent_and_generated_text_is_compact(self):
        m, renderer, gl, upload, draw = self.compact_context()
        gl('glTexImage2D', 0xDE1, 0, 0x1907, 32, 32, 0, 0x1907, 0x8363, 0)
        self.assertEqual(renderer.texture_info(1)['pixels'], bytes(32 * 32 * 4))
        shared = renderer.texture_storage_bytes
        pixels = bytes((0, 0, 0, 127)) * (32 * 32)
        identifier = renderer.library.graphics_text_texture(renderer._handle, 32, 32, pixels)
        self.assertGreater(identifier, 0)
        self.assertLessEqual(renderer.texture_storage_bytes - shared, 4)
        self.assertEqual(renderer.texture_info(identifier)['pixels'], pixels)

    def test_deleted_textures_disappear_from_the_resource_snapshot(self):
        m = Arm(LIBRARY)
        LibC(m)
        renderer = importlib.import_module('gles').GLES(m, 1, 1)
        identifier, pixel = m.malloc(4), m.malloc(4)
        m.call(m.import_address('glGenTextures'), 1, identifier)
        texture = m.u32(identifier)
        m.call(m.import_address('glBindTexture'), 0xDE1, texture)
        m.uc.mem_write(pixel, bytes((255, 0, 0, 255)))
        m.call(m.import_address('glTexImage2D'), 0xDE1, 0, 0x1908, 1, 1, 0, 0x1908, 0x1401, pixel)
        self.assertIn(texture, renderer.textures)
        self.assertEqual(renderer.texture_info(texture)['pixels'], bytes((255, 0, 0, 255)))
        m.call(m.import_address('glDeleteTextures'), 1, identifier)
        self.assertNotIn(texture, renderer.textures)
        with self.assertRaises(KeyError):
            renderer.texture_info(texture)

    def test_engine_graphics_calls_render_without_python_import_handlers(self):
        m = Arm(LIBRARY)
        LibC(m)
        renderer = importlib.import_module('gles').GLES(m, 4, 4)
        names = ('glClearColor', 'glClear', 'glMatrixMode', 'glOrthof', 'glColor4f',
                 'glVertexPointer', 'glEnableClientState', 'glDrawArrays')
        def python_handler():
            self.fail('A graphics call crossed back into Python')
        for name in names:
            m.handlers[name] = python_handler
        def gl(name, *args):
            m.call(m.import_address(name), *[struct.unpack('<I', struct.pack('<f', x))[0] if isinstance(x, float) else x for x in args])
        gl('glClearColor', 0.0, 0.0, 0.0, 1.0)
        gl('glClear', 0x4000)
        gl('glMatrixMode', 0x1701)
        gl('glOrthof', 0.0, 4.0, 4.0, 0.0, -1.0, 1.0)
        gl('glColor4f', 0.0, 1.0, 0.0, 1.0)
        vertices = m.malloc(24)
        m.uc.mem_write(vertices, struct.pack('<6f', 0, 0, 4, 0, 0, 4))
        gl('glVertexPointer', 2, 0x1406, 0, vertices)
        gl('glEnableClientState', 0x8074)
        gl('glDrawArrays', 4, 0, 3)
        self.assertEqual(renderer.image().getpixel((0, 0)), (0, 255, 0, 255))

    def test_overlay_interpolates_texture_with_primary_color_and_keeps_texture_alpha(self):
        m = Arm(LIBRARY)
        LibC(m)
        renderer = importlib.import_module('gles').GLES(m, 4, 4)
        def gl(name, *args):
            m.call(m.import_address(name), *[struct.unpack('<I', struct.pack('<f', x))[0] if isinstance(x, float) else x for x in args])
        gl('glMatrixMode', 0x1701)
        gl('glOrthof', 0.0, 4.0, 4.0, 0.0, -1.0, 1.0)
        vertices, pixel = m.malloc(24), m.malloc(4)
        m.uc.mem_write(vertices, struct.pack('<6f', 0, 0, 4, 0, 0, 4))
        m.uc.mem_write(pixel, bytes((100, 150, 200, 128)))
        gl('glVertexPointer', 2, 0x1406, 0, vertices)
        gl('glEnableClientState', 0x8074)
        gl('glBindTexture', 0xDE1, 1)
        gl('glTexImage2D', 0xDE1, 0, 0x1908, 1, 1, 0, 0x1908, 0x1401, pixel)
        gl('glEnable', 0xDE1)
        gl('glColor4f', 0.2, 0.4, 0.6, 0.25)
        for key, value in ((0x2200, 0x8570), (0x8571, 0x8575), (0x8580, 0x8578),
                           (0x8581, 0x1702), (0x8582, 0x8578), (0x8590, 0x300),
                           (0x8591, 0x300), (0x8592, 0x302), (0x8572, 0x1E01),
                           (0x8588, 0x1702), (0x8598, 0x302)):
            gl('glTexEnvi', 0x2300, key, value)
        gl('glDrawArrays', 4, 0, 3)
        self.assertEqual(renderer.image().getpixel((0, 0)), (88, 138, 188, 128))

    def test_secondary_texture_state_does_not_disable_the_primary_texture(self):
        m = Arm(LIBRARY)
        LibC(m)
        renderer = importlib.import_module('gles').GLES(m, 4, 4)
        def gl(name, *args):
            m.call(m.import_address(name), *[struct.unpack('<I', struct.pack('<f', x))[0] if isinstance(x, float) else x for x in args])
        gl('glMatrixMode', 0x1701)
        gl('glOrthof', 0.0, 4.0, 4.0, 0.0, -1.0, 1.0)
        vertices, uv, pixel = m.malloc(32), m.malloc(32), m.malloc(4)
        m.uc.mem_write(vertices, struct.pack('<8f', 0, 0, 4, 0, 0, 4, 4, 4))
        m.uc.mem_write(uv, struct.pack('<8f', 0, 0, 1, 0, 0, 1, 1, 1))
        m.uc.mem_write(pixel, bytes((255, 0, 0, 255)))
        gl('glVertexPointer', 2, 0x1406, 0, vertices)
        gl('glTexCoordPointer', 2, 0x1406, 0, uv)
        gl('glEnableClientState', 0x8074)
        gl('glEnableClientState', 0x8078)
        gl('glBindTexture', 0xDE1, 1)
        gl('glTexImage2D', 0xDE1, 0, 0x1908, 1, 1, 0, 0x1908, 0x1401, pixel)
        gl('glEnable', 0xDE1)
        gl('glColor4f', 0.5, 0.5, 0.5, 1.0)
        gl('glActiveTexture', 0x84C1)
        gl('glBindTexture', 0xDE1, 0)
        gl('glTexEnvi', 0x2300, 0x2200, 0x1E01)
        gl('glDisable', 0xDE1)
        gl('glClientActiveTexture', 0x84C1)
        gl('glDisableClientState', 0x8078)
        gl('glActiveTexture', 0x84C0)
        gl('glClientActiveTexture', 0x84C0)
        gl('glDrawArrays', 5, 0, 4)
        self.assertEqual(renderer.image().getpixel((1, 1)), (128, 0, 0, 255))

    def test_source_color_blending_multiplies_each_destination_channel(self):
        m = Arm(LIBRARY)
        LibC(m)
        renderer = importlib.import_module('gles').GLES(m, 4, 4)
        def gl(name, *args):
            m.call(m.import_address(name), *[struct.unpack('<I', struct.pack('<f', x))[0] if isinstance(x, float) else x for x in args])
        gl('glClearColor', 0.8, 0.4, 0.2, 1.0)
        gl('glClear', 0x4000)
        gl('glMatrixMode', 0x1701)
        gl('glOrthof', 0.0, 4.0, 4.0, 0.0, -1.0, 1.0)
        gl('glColor4f', 0.25, 0.5, 0.75, 0.5)
        vertices = m.malloc(24)
        m.uc.mem_write(vertices, struct.pack('<6f', 0, 0, 4, 0, 0, 4))
        gl('glVertexPointer', 2, 0x1406, 0, vertices)
        gl('glEnableClientState', 0x8074)
        gl('glEnable', 0xBE2)
        gl('glBlendFunc', 0, 768)
        gl('glDrawArrays', 4, 0, 3)
        self.assertEqual(renderer.image().getpixel((0, 0)), (51, 51, 38, 128))

    def test_cached_background_preserves_blending_and_texture_changes(self):
        m=Arm(LIBRARY);LibC(m)
        renderer=importlib.import_module('gles').GLES(m,4,4)
        def gl(name,*args):
            m.call(m.import_address(name),*[struct.unpack('<I',struct.pack('<f',v))[0] if isinstance(v,float) else v for v in args])
        vertices=m.malloc(32);m.uc.mem_write(vertices,struct.pack('<8f',0,0,4,0,0,4,4,4))
        uv=m.malloc(32);m.uc.mem_write(uv,struct.pack('<8f',0,0,1,0,0,1,1,1))
        identifier,pixel=m.malloc(4),m.malloc(4)
        gl('glMatrixMode',0x1701);gl('glOrthof',0.0,4.0,4.0,0.0,-1.0,1.0)
        gl('glVertexPointer',2,0x1406,0,vertices);gl('glTexCoordPointer',2,0x1406,0,uv)
        gl('glEnableClientState',0x8074);gl('glEnableClientState',0x8078)
        gl('glGenTextures',1,identifier);gl('glBindTexture',0xDE1,m.u32(identifier))
        gl('glEnable',0xDE1);gl('glEnable',0xBE2);gl('glBlendFunc',770,771)
        m.uc.mem_write(pixel,bytes((0,0,255,128)))
        gl('glTexImage2D',0xDE1,0,0x1908,1,1,0,0x1908,0x1401,pixel)
        def render(r,g):
            gl('glClearColor',float(r),float(g),0.0,1.0);gl('glClear',0x4000);gl('glDrawArrays',5,0,4)
            return renderer.image()
        first=render(1,0)
        self.assertEqual(first.tobytes(),render(1,0).tobytes())
        self.assertEqual(first.getpixel((1,1))[:3],(127,0,128))
        self.assertEqual(render(0,1).getpixel((1,1))[:3],(0,127,128))
        m.uc.mem_write(pixel,bytes((255,255,255,255)))
        gl('glTexImage2D',0xDE1,0,0x1908,1,1,0,0x1908,0x1401,pixel)
        self.assertEqual(render(0,1).getpixel((1,1)),(255,255,255,255))

    def test_packed_texture_uploads_expand_each_channel(self):
        m = Arm(LIBRARY)
        LibC(m)
        renderer = importlib.import_module('gles').GLES(m,1,1)
        identifier, pixels = m.malloc(4), m.malloc(4)
        m.call(m.import_address('glGenTextures'),1,identifier)
        m.call(m.import_address('glBindTexture'),0xDE1,m.u32(identifier))
        for kind, value, expected in ((0x8033,0x1234,(17,34,51,68)),
                                      (0x8363,0xF81F,(255,0,255,255)),
                                      (0x8034,0xF801,(255,0,0,255))):
            m.uc.mem_write(pixels,struct.pack('<H',value))
            m.call(m.import_address('glTexImage2D'),0xDE1,0,0x1908,1,1,0,0x1908,kind,pixels)
            self.assertEqual(tuple(renderer.textures[renderer.texture]['pixels']),expected)

    def test_tutorial_can_restore_the_bound_texture_after_drawing_text(self):
        m = Arm(LIBRARY)
        LibC(m)
        renderer = importlib.import_module('gles').GLES(m,4,4)
        output = m.malloc(4)
        m.call(m.import_address('glBindTexture'),0xDE1,123)
        m.call(m.import_address('glGetIntegerv'),0x8069,output)
        self.assertEqual(m.u32(output),123)

    def test_original_gl_coordinates_and_triangle_coverage(self):
        try:
            module = importlib.import_module('gles')
        except ModuleNotFoundError:
            self.fail('The original engine GLES renderer is missing')
        m = Arm(LIBRARY)
        LibC(m)
        renderer = module.GLES(m, 4, 4)
        def gl(name, *args):
            m.call(m.import_address(name), *[struct.unpack('<I', struct.pack('<f', x))[0] if isinstance(x, float) else x for x in args])
        gl('glClearColor', 0.0, 0.0, 0.0, 1.0)
        gl('glClear', 0x4000)
        gl('glMatrixMode', 0x1701)
        gl('glOrthof', 0.0, 4.0, 4.0, 0.0, -1.0, 1.0)
        gl('glColor4f', 1.0, 0.0, 0.0, 1.0)
        vertices = m.malloc(24)
        m.uc.mem_write(vertices, struct.pack('<6f', 0, 0, 4, 0, 0, 4))
        gl('glVertexPointer', 2, 0x1406, 0, vertices)
        gl('glEnableClientState', 0x8074)
        gl('glDrawArrays', 4, 0, 3)
        image = renderer.image()
        self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(image.getpixel((3, 3)), (0, 0, 0, 255))


if __name__ == '__main__':
    unittest.main()
