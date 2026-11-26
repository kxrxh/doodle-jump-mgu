"""Exercise current C++ backends through pinned external Python adapters."""
import _support

import ctypes
import io
import struct
import unittest

from PIL import Image
from arm import BASE
from png_decoder import PngDecoder
from unicorn.arm_const import UC_ARM_REG_D0
import test_gles


class SharedPngTests(unittest.TestCase):
    def image(self, width=256, height=256, mode=2):
        m, renderer, gl, upload, draw = test_gles.GraphicsTests().compact_context()
        upload(struct.pack('<H', 0), 1, 1, 0x8033)
        pixels = b''.join(bytes((i % 16 * 17, i // 16 % 16 * 17, i // 256 % 16 * 17, 255))
                          for i in range(width * height))
        image = Image.frombytes('RGBA', (width, height), pixels)
        encoded = io.BytesIO(); image.save(encoded, format='PNG'); encoded = encoded.getvalue()
        source, result = m.malloc(len(encoded)), m.malloc(36)
        m.uc.mem_write(source, encoded)
        PngDecoder(m, renderer.library)
        self.assertEqual(m.call(BASE + 0xadc59, source, len(encoded), mode, 1, result), 1)
        pointer, fmt, kind, bpp, size, w, h, tw, th = struct.unpack('<9I', m.uc.mem_read(result, 36))
        return m, renderer, gl, pointer, fmt, kind, size, tw, th

    def test_png_upload_keeps_guest_pixels_without_a_second_renderer_allocation(self):
        m, renderer, gl, pointer, fmt, kind, size, w, h = self.image()
        pixels = bytes(m.uc.mem_read(pointer, size))
        before = renderer.texture_storage_bytes
        gl('glTexImage2D', 0xDE1, 0, fmt, w, h, 0, fmt, kind, pointer)
        self.assertLessEqual(renderer.texture_storage_bytes - before, 1024,
                             'decoded PNG uploads must share their existing pixel storage')
        self.assertEqual(bytes(m.uc.mem_read(pointer, size)), pixels)
        self.assertEqual(renderer.texture_info(1)['pixels'][:8], bytes((0, 0, 0, 255, 17, 0, 0, 255)))

    def rgba(self, renderer, identifier, width, height):
        output = ctypes.create_string_buffer(width * height * 4)
        self.assertEqual(renderer.library.graphics_texture_rgba(renderer._handle, identifier,
                                                               output, len(output)), 1)
        return output.raw

    def test_snapshot_survives_host_arm_and_native_host_writes(self):
        for method in ('host', 'arm', 'memset', 'native_host', 'copy'):
            with self.subTest(method=method):
                m, renderer, gl, pointer, fmt, kind, size, w, h = self.image(4, 4, 0)
                gl('glTexImage2D', 0xDE1, 0, fmt, w, h, 0, fmt, kind, pointer)
                expected = bytes(m.uc.mem_read(pointer, size))
                if method == 'host':
                    m.uc.mem_write(pointer, b'ABCD')
                elif method == 'arm':
                    code = m.malloc(8)
                    m.uc.mem_write(code, struct.pack('<2I', 0xE5801000, 0xE12FFF1E))
                    m.call(code, pointer, 0x11223344)
                elif method == 'memset':
                    m.call(m.import_address('memset'), pointer, 0, 4)
                elif method == 'native_host':
                    gl('glGetIntegerv', 0x8069, pointer)
                else:
                    m.copy(pointer, pointer + 4, 4)
                self.assertNotEqual(bytes(m.uc.mem_read(pointer, size)), expected)
                self.assertEqual(self.rgba(renderer, 1, w, h), expected)

    def test_reuploads_keep_each_snapshot_and_free_does_not_destroy_textures(self):
        m, renderer, gl, pointer, fmt, kind, size, w, h = self.image(4, 4, 0)
        original = bytes(m.uc.mem_read(pointer, size))
        gl('glTexImage2D', 0xDE1, 0, fmt, w, h, 0, fmt, kind, pointer)
        m.uc.mem_write(pointer, b'ABCD')
        modified = bytes(m.uc.mem_read(pointer, size))
        gl('glBindTexture', 0xDE1, 2)
        gl('glTexImage2D', 0xDE1, 0, fmt, w, h, 0, fmt, kind, pointer)
        m.uc.mem_write(pointer + 4, b'EFGH')
        m.free(pointer)
        replacement, _ = m.image_malloc(size)
        m.uc.mem_write(replacement, bytes(size))
        self.assertEqual(self.rgba(renderer, 1, w, h), original)
        self.assertEqual(self.rgba(renderer, 2, w, h), modified)
        m.free(replacement)

    def test_offset_upload_and_realloc_preserve_pixels(self):
        m, renderer, gl, pointer, fmt, kind, size, w, h = self.image(4, 4, 0)
        expected = bytes(m.uc.mem_read(pointer + 4, 12))
        gl('glTexImage2D', 0xDE1, 0, fmt, 3, 1, 0, fmt, kind, pointer + 4)
        grown = m.call(m.import_address('realloc'), pointer, size * 2)
        self.assertEqual(bytes(m.uc.mem_read(grown + 4, 12)), expected)
        m.fill(grown, 0, size * 2)
        self.assertEqual(self.rgba(renderer, 1, 3, 1), expected)
        m.free(grown)

    def test_one_write_detaches_both_adjacent_images(self):
        m, renderer, gl, _, fmt, kind, _, _, _ = self.image(4, 4, 0)
        first, _ = m.image_malloc(4096)
        second, _ = m.image_malloc(4096)
        self.assertEqual(second, first + 4096)
        for identifier, pointer in ((1, first), (2, second)):
            m.uc.mem_write(pointer, bytes([identifier]) * 4096)
            gl('glBindTexture', 0xDE1, identifier)
            gl('glTexImage2D', 0xDE1, 0, fmt, 32, 32, 0, fmt, kind, pointer)
        m.uc.mem_write(first + 4094, b'ABCDEFGH')
        self.assertEqual(bytes(m.uc.mem_read(first + 4094, 8)), b'ABCDEFGH')
        self.assertEqual(self.rgba(renderer, 1, 32, 32), bytes([1]) * 4096)
        self.assertEqual(self.rgba(renderer, 2, 32, 32), bytes([2]) * 4096)


    def test_protected_stores_keep_width_unaligned_bytes_and_register_writeback(self):
        cases = ((0xE5C01000, 0, b'\x44', 0),
                 (0xE1C010B0, 0, b'\x44\x33', 0),
                 (0xE5801000, 4094, b'\x44\x33\x22\x11', 0),
                 (0xE1C020F0, 0, bytes.fromhex('88776655ccbbaa99'), 0),
                 (0xED800B00, 0, bytes.fromhex('efcdab8967452301'), 0),
                 (0xE8A0000E, 0, bytes.fromhex('4433221188776655ccbbaa99'), 12))
        for instruction, offset, written, advance in cases:
            with self.subTest(instruction=hex(instruction)):
                m, renderer, gl, pointer, fmt, kind, size, w, h = self.image(64, 64, 0)
                original = bytes(m.uc.mem_read(pointer, size))
                gl('glTexImage2D', 0xDE1, 0, fmt, w, h, 0, fmt, kind, pointer)
                code = m.malloc(8)
                m.uc.mem_write(code, struct.pack('<2I', instruction, 0xE12FFF1E))
                m.uc.reg_write(UC_ARM_REG_D0, 0x0123456789ABCDEF)
                self.assertEqual(m.call(code, pointer + offset, 0x11223344, 0x55667788, 0x99AABBCC),
                                 pointer + offset + advance)
                expected = bytearray(original); expected[offset:offset+len(written)] = written
                self.assertEqual(bytes(m.uc.mem_read(pointer, size)), expected)
                self.assertEqual(self.rgba(renderer, 1, w, h), original)

    def test_failed_mapping_can_be_retried_without_losing_the_image_arena(self):
        m, renderer, gl, pointer, fmt, kind, size, w, h = self.image(4, 4, 0)
        address = m.image_heap
        m.uc.mem_map(address, 4096)
        with self.assertRaises(MemoryError):
            m.image_malloc(64)
        m.uc.mem_unmap(address, 4096)
        allocated, _ = m.image_malloc(64)
        self.assertEqual(allocated, address)
        m.uc.mem_write(allocated, b'ABCD')
        self.assertEqual(bytes(m.uc.mem_read(allocated, 4)), b'ABCD')
        m.free(allocated)



if __name__ == '__main__':
    unittest.main()
