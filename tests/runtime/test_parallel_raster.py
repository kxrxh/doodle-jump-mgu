import ctypes
from concurrent.futures import ThreadPoolExecutor
import unittest

from native_backend import library
from test_raster import Texture


class ParallelRasterTests(unittest.TestCase):
    def test_parallel_rows_match_serial_pixels_and_guards_across_repeated_blended_draws(self):
        lib = library()
        self.assertTrue(hasattr(lib, 'raster_parallel'), 'the renderer needs a bounded parallel row path')
        serial, parallel = lib.raster_compact, lib.raster_parallel
        for function in (serial, parallel):
            function.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                 ctypes.c_int, ctypes.POINTER(Texture)] + [ctypes.c_int] * 9
            function.restype = None
        width, height = 257, 193
        points = ((-7, -9, -.1, -.2), (width+3, 0, 1.1, 0), (0, height+7, 0, 1.2),
                  (0, height+7, 0, 1.2), (width+3, 0, 1.1, 0), (width+9, height+5, 1.3, 1.1))
        vertices = (ctypes.c_float * 48)(*(v for p in points for v in (*p, .37, .61, .83, .54)))
        palette = (ctypes.c_uint32 * 2)(0xBF0308E1, 0x752BCA19)
        formats = ((0, bytes.fromhex('e10803bf19ca2b7519ca2b75e10803bf')),
                   (1, bytes.fromhex('e1080319ca2b19ca2be10803')),
                   (2, bytes((0, 1, 1, 0))), (3, bytes((0, 0, 1, 0, 1, 0, 0, 0))), (4, b''))
        for kind, pixels in formats:
            raw = ctypes.create_string_buffer(pixels)
            texture = Texture(ctypes.addressof(raw), ctypes.addressof(palette), kind)
            for linear, repeat, blend, source, destination, replace in ((1, 1, 1, 770, 771, 0),
                                                                       (0, 0, 1, 1, 768, 1)):
                with self.subTest(format=kind, linear=linear):
                    initial = bytes([0xA5]) * 16 + bytes((7, 31, 91, 123)) * (width * height) + bytes([0xA5]) * 16
                    a, b = ctypes.create_string_buffer(initial, len(initial)), ctypes.create_string_buffer(initial, len(initial))
                    for _ in range(12):
                        for function, output in ((serial, a), (parallel, b)):
                            function(ctypes.addressof(output)+16, width, height, vertices, 6,
                                     ctypes.byref(texture), 2, 2, linear, repeat, repeat, blend,
                                     source, destination, replace)
                        self.assertEqual(a.raw, b.raw)
                    self.assertEqual(b.raw[:16], bytes([0xA5]) * 16)
                    self.assertEqual(b.raw[-16:], bytes([0xA5]) * 16)

    def test_concurrent_callers_keep_off_center_draws_independent(self):
        lib = library()
        signature = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                     ctypes.c_int, ctypes.POINTER(Texture)] + [ctypes.c_int] * 9
        for function in (lib.raster_compact, lib.raster_parallel):
            function.argtypes = signature; function.restype = None
        width, height = 320, 240
        points = ((13, 19, 0, 0), (290, 19, 1, 0), (13, 225, 0, 1),
                  (13, 225, 0, 1), (290, 19, 1, 0), (290, 225, 1, 1))
        def render(function, color):
            vertices = (ctypes.c_float * 48)(*(v for p in points for v in (*p, *color)))
            output = ctypes.create_string_buffer(bytes((7, 31, 91, 123)) * (width * height))
            for _ in range(8):
                function(output, width, height, vertices, 6, None, 0, 0, 1, 1, 1, 1, 770, 771, 0)
            return output.raw
        colors = ((.2, .4, .6, .7), (.7, .3, .1, .5))
        expected = [render(lib.raster_compact, color) for color in colors]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(render, lib.raster_parallel, color) for color in colors]
            self.assertEqual([future.result() for future in futures], expected)



if __name__ == '__main__':
    unittest.main()
