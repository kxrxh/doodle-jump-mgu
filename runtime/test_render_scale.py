import ctypes
import struct
import unittest

from arm import Arm
from gles import GLES
from libc import LibC
from test_arm import LIBRARY


class RenderScaleTests(unittest.TestCase):
    def context(self, render_size=None):
        machine = Arm(LIBRARY)
        LibC(machine)
        renderer = GLES(machine, 4, 4, render_size=render_size)

        def gl(name, *args):
            machine.call(machine.import_address(name), *[
                struct.unpack('<I', struct.pack('<f', value))[0]
                if isinstance(value, float) else value for value in args])

        vertices, uv, pixels = machine.malloc(32), machine.malloc(32), machine.malloc(16)
        machine.uc.mem_write(vertices, struct.pack('<8f', 0, 0, 4, 0, 0, 4, 4, 4))
        machine.uc.mem_write(uv, struct.pack('<8f', 0, 0, 1, 0, 0, 1, 1, 1))
        machine.uc.mem_write(pixels, bytes((255, 0, 0, 0, 0, 255, 0, 64,
                                            0, 0, 255, 128, 255, 255, 255, 255)))
        gl('glMatrixMode', 0x1701)
        gl('glOrthof', 0.0, 4.0, 4.0, 0.0, -1.0, 1.0)
        gl('glVertexPointer', 2, 0x1406, 0, vertices)
        gl('glTexCoordPointer', 2, 0x1406, 0, uv)
        gl('glEnableClientState', 0x8074)
        gl('glEnableClientState', 0x8078)
        gl('glEnable', 0xDE1)
        gl('glBindTexture', 0xDE1, 1)
        renderer.asset_name = 'bck_test.png'
        gl('glTexImage2D', 0xDE1, 0, 0x1908, 2, 2, 0, 0x1908, 0x1401, pixels)
        for key, value in ((0x2800, 9728), (0x2802, 33071), (0x2803, 33071)):
            gl('glTexParameteri', 0xDE1, key, value)
        return machine, renderer, gl

    def test_smaller_target_preserves_texture_edges_alpha_and_logical_geometry(self):
        machine, renderer, gl = self.context((2, 2))
        gl('glClear', 0x4000)
        gl('glDrawArrays', 5, 0, 4)
        image = renderer.image()
        self.assertEqual(image.size, (2, 2))
        self.assertEqual(image.tobytes(), bytes((255, 0, 0, 0, 0, 255, 0, 64,
                                               0, 0, 255, 128, 255, 255, 255, 255)))
        self.assertEqual(len(renderer.frame), 16)
        self.assertEqual(renderer.input_rectangles, [((0.0, 0.0, 4.0, 4.0), False)])
        identifier, vertices = renderer.background_info
        self.assertEqual(identifier, 1)
        self.assertEqual([vertex[:2] for vertex in vertices],
                         [(0.0, 0.0), (4.0, 0.0), (0.0, 4.0), (4.0, 4.0)])
        viewport = machine.malloc(16)
        gl('glGetIntegerv', 0xBA2, viewport)
        self.assertEqual(struct.unpack('<4i', machine.uc.mem_read(viewport, 16)), (0, 0, 4, 4))

    def test_clear_and_cached_background_use_the_physical_buffer(self):
        machine, renderer, gl = self.context((2, 2))
        gl('glClear', 0x4000)
        gl('glDrawArrays', 5, 0, 4)
        expected = renderer.frame[:]
        gl('glClear', 0x4000)
        gl('glDrawArrays', 5, 0, 4)
        self.assertEqual(renderer.frame, expected)
        gl('glClearColor', 0.0, 0.25, 0.5, 1.0)
        gl('glClear', 0x4000)
        self.assertEqual(renderer.frame, bytes((0, 64, 128, 255)) * 4)

    def test_default_target_keeps_original_pixel_coverage(self):
        machine, renderer, gl = self.context()
        gl('glDrawArrays', 5, 0, 4)
        self.assertEqual(renderer.image().size, (4, 4))
        self.assertEqual(renderer.image().getpixel((0, 0)), (255, 0, 0, 0))
        self.assertEqual(renderer.image().getpixel((3, 0)), (0, 255, 0, 64))
        self.assertEqual(renderer.image().getpixel((0, 3)), (0, 0, 255, 128))
        self.assertEqual(renderer.image().getpixel((3, 3)), (255, 255, 255, 255))

    def test_binding_a_target_discards_cached_pixels_from_the_previous_size(self):
        machine, renderer, gl = self.context((2, 2))
        gl('glClear', 0x4000)
        gl('glDrawArrays', 5, 0, 4)
        output = (ctypes.c_uint8 * 20)(*([91] * 20))
        self.assertEqual(renderer.library.graphics_render_target(renderer._handle, 1, 1, output), 1)
        gl('glClear', 0x4000)
        gl('glDrawArrays', 5, 0, 4)
        self.assertEqual(bytes(output[:4]), bytes((255, 255, 255, 255)))
        self.assertEqual(bytes(output[4:]), bytes((91,)) * 16)

    def test_rejected_target_leaves_the_active_framebuffer_unchanged(self):
        machine, renderer, gl = self.context((2, 2))
        output = (ctypes.c_uint8 * 64)(*([91] * 64))
        target = renderer.library.graphics_render_target
        for handle, width, height, pointer in (
                (renderer._handle, 0, 2, output), (renderer._handle, 2, -1, output),
                (renderer._handle, 4097, 2, output), (renderer._handle, 2, 4097, output),
                (renderer._handle, 2, 2, None), (None, 2, 2, output)):
            with self.subTest(width=width, height=height, null_pointer=pointer is None):
                self.assertEqual(target(handle, width, height, pointer), 0)
        gl('glClearColor', 1.0, 0.0, 0.0, 1.0)
        gl('glClear', 0x4000)
        self.assertEqual(renderer.frame, bytes((255, 0, 0, 255)) * 4)
        self.assertEqual(bytes(output), bytes((91,)) * 64)

    def test_python_rejects_invalid_target_dimensions_before_allocating(self):
        for size in ((0, 2), (2, -1), (4097, 2), (2, 4097)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.context(size)


if __name__ == '__main__':
    unittest.main()
