import importlib
import struct
import unittest

from arm import Arm
from libc import LibC
from test_arm import LIBRARY


class GraphicsTests(unittest.TestCase):
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
