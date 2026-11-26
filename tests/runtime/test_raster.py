import ctypes
import unittest

from native_backend import library


class Texture(ctypes.Structure):
    _fields_ = [('pixels', ctypes.c_void_p), ('palette', ctypes.c_void_p), ('format', ctypes.c_int)]


class RasterTests(unittest.TestCase):
    def draw(self, texture=None, color=(1, 1, 1, 1), blend=0, source=1, destination=0,
             replace=0, initial=(9, 29, 49, 69), nonfinite=False):
        if nonfinite:
            points = ((-10, -10, 0, 0), (20, -10, 1, 0), (-10, 20, 0, 1))
            width = height = 1
        else:
            points = ((0, 0, 0, 0), (4, 0, 1, 0), (0, 4, 0, 1),
                      (0, 4, 0, 1), (4, 0, 1, 0), (4, 4, 1, 1))
            width = height = 4
        vertices = (ctypes.c_float * (len(points)*8))(*(n for p in points for n in (*p, *color)))
        pixels = (ctypes.c_uint8 * (width*height*4))(*(initial * (width*height)))
        render = library().raster_compact
        render.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                          ctypes.c_int, ctypes.POINTER(Texture)] + [ctypes.c_int] * 9
        render.restype = None
        render(pixels, width, height, vertices, len(points), ctypes.byref(texture) if texture else None,
               2, 2, 1, 1, 1, blend, source, destination, replace)
        return bytes(pixels)

    def test_all_compact_formats_keep_exact_bilinear_channels(self):
        palette = (ctypes.c_uint32 * 2)(0xFF0000FF, 0xFFFF0000)
        red, blue = bytes((255, 0, 0, 255)), bytes((0, 0, 255, 255))
        a, b = (159, 0, 96, 255), (96, 0, 159, 255)
        expected = bytes(a+a+b+b)*2 + bytes(b+b+a+a)*2
        for kind, raw in ((0, red+blue+blue+red), (1, red[:3]+blue[:3]+blue[:3]+red[:3]),
                          (2, bytes((0, 1, 1, 0))), (3, bytes((0, 0, 1, 0, 1, 0, 0, 0))),
                          (4, b'\0')):
            with self.subTest(format=kind):
                data = ctypes.create_string_buffer(raw)
                texture = Texture(ctypes.addressof(data), ctypes.addressof(palette), kind)
                self.assertEqual(self.draw(texture), red*16 if kind == 4 else expected)

    def test_modulate_replace_decal_and_blend_preserve_alpha(self):
        palette = (ctypes.c_uint32 * 1)(32 | 88 << 8 | 180 << 16 | 64 << 24)
        texture = Texture(None, ctypes.addressof(palette), 4)
        for blend, source, destination, replace, expected in (
                (1, 770, 771, 0, (9, 31, 60, 64)),
                (0, 1, 0, 1, (32, 88, 180, 64)),
                (0, 1, 0, 2, (48, 108, 186, 64)),
                (1, 1, 768, 0, (8, 49, 161, 41))):
            with self.subTest(blend=blend, destination=destination, replace=replace):
                self.assertEqual(self.draw(texture, (.25, .5, .75, .5), blend, source,
                                           destination, replace), bytes(expected)*16)
        self.assertEqual(self.draw(color=(.25, .5, .75, .5), replace=1), bytes((64, 128, 191, 128))*16)

    def test_final_channel_clamp_handles_nonfinite_intermediate_values(self):
        self.assertEqual(self.draw(color=(float('nan'), float('inf'), -float('inf'), 1),
                                   nonfinite=True), bytes((0, 255, 0, 255)))


if __name__ == '__main__':
    unittest.main()
