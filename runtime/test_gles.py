import importlib
import struct
import unittest

from arm import Arm
from libc import LibC
from test_arm import LIBRARY


class GraphicsTests(unittest.TestCase):
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
