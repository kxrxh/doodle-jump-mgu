"""Exercise current C++ backends through pinned external Python adapters."""
import _support

import ctypes
import importlib
import struct
import unittest


class FrameEncoderTests(unittest.TestCase):
    def encoder(self, width, height):
        try:
            module = importlib.import_module('frame_encoder')
        except ModuleNotFoundError:
            self.fail('The native lossless frame encoder is missing')
        return module.FrameEncoder(width, height)

    def reconstruct(self, payload, width, height, previous=None):
        w, h, count = struct.unpack_from('<III', payload)
        self.assertEqual((w, h), (width, height))
        self.assertLessEqual(count, 64)
        offset = 12 + count * 16
        image = bytearray(width * height * 4) if previous is None else bytearray(previous)
        rectangles = []
        for index in range(count):
            x, y, rw, rh = struct.unpack_from('<IIII', payload, 12 + index * 16)
            self.assertGreater(rw, 0)
            self.assertGreater(rh, 0)
            self.assertLessEqual(x + rw, width)
            self.assertLessEqual(y + rh, height)
            rectangles.append((x, y, rw, rh))
            for row in range(y, y + rh):
                size = rw * 4
                self.assertLessEqual(offset + size, len(payload))
                start = (row * width + x) * 4
                image[start:start + size] = payload[offset:offset + size]
                offset += size
        self.assertEqual(offset, len(payload), 'Only initialized rectangle pixels are exposed')
        return image, rectangles

    def test_first_zero_frame_is_a_full_keyframe_then_unchanged_is_empty(self):
        encoder = self.encoder(3, 2)
        frame = bytearray(24)
        image, rectangles = self.reconstruct(encoder.encode(frame), 3, 2)
        self.assertEqual(rectangles, [(0, 0, 3, 2)])
        self.assertEqual(image, frame)
        payload = encoder.encode(frame)
        image, rectangles = self.reconstruct(payload, 3, 2, image)
        self.assertEqual(rectangles, [])
        self.assertEqual(len(payload), 12)
        self.assertEqual(image, frame)

    def test_alpha_only_change_and_partial_edge_tiles_keep_every_byte(self):
        width, height = 65, 35
        encoder = self.encoder(width, height)
        frame = bytearray(bytes((17, 31, 93, 255)) * (width * height))
        image, _ = self.reconstruct(encoder.encode(frame), width, height)
        for x, y in ((35, 3), (64, 34)):
            with self.subTest(x=x, y=y):
                frame[(y * width + x) * 4 + 3] = 7
                payload = encoder.encode(frame)
                image, rectangles = self.reconstruct(payload, width, height, image)
                self.assertEqual(image, frame)
                self.assertTrue(any(rx <= x < rx + rw and ry <= y < ry + rh
                                    for rx, ry, rw, rh in rectangles))
                self.assertLess(len(payload), 12 + 16 + len(frame))

    def test_adjacent_changed_tiles_merge_in_both_directions(self):
        width, height = 96, 96
        encoder = self.encoder(width, height)
        frame = bytearray(width * height * 4)
        image, _ = self.reconstruct(encoder.encode(frame), width, height)
        for y in range(64):
            frame[y * width * 4:(y * width + 64) * 4] = bytes((3, 9, 27, 81)) * 64
        image, rectangles = self.reconstruct(encoder.encode(frame), width, height, image)
        self.assertEqual(rectangles, [(0, 0, 64, 64)])
        self.assertEqual(image, frame)

    def test_scrolling_and_moving_patterns_reconstruct_with_a_reused_input_buffer(self):
        width, height = 173, 129
        encoder = self.encoder(width, height)
        background = bytes(value for y in range(height) for x in range(width)
                           for value in ((x * 7 + y * 3) & 255, (x + y) & 255, x ^ y, 255))
        frame = bytearray(background)
        image, _ = self.reconstruct(encoder.encode(frame), width, height)
        for step in range(12):
            offset = step * width * 4
            frame[:] = background[offset:] + background[:offset]
            x, y = step * 37 % (width - 16), step * 23 % (height - 20)
            for row in range(y, y + 20):
                start = (row * width + x) * 4
                frame[start:start + 16 * 4] = bytes((step, 33, 77, step * 19)) * 16
            image, _ = self.reconstruct(encoder.encode(frame), width, height, image)
            self.assertEqual(image, frame, step)
            unchanged, rectangles = self.reconstruct(encoder.encode(frame), width, height, image)
            self.assertEqual(rectangles, [])
            self.assertEqual(unchanged, frame)

    def test_more_than_64_regions_falls_back_to_the_exact_changed_box(self):
        width, height = 512, 512
        encoder = self.encoder(width, height)
        frame = bytearray(width * height * 4)
        image, _ = self.reconstruct(encoder.encode(frame), width, height)
        for y in range(0, height, 32):
            for x in range(0, width, 32):
                if (x // 32 + y // 32) % 2 == 0:
                    frame[(y * width + x) * 4 + 3] = 255
        image, rectangles = self.reconstruct(encoder.encode(frame), width, height, image)
        self.assertEqual(rectangles, [(0, 0, 481, 481)])
        self.assertEqual(image, frame)

    def test_rejected_frames_leave_payload_and_previous_pixels_unchanged(self):
        encoder = self.encoder(4, 3)
        frame = bytearray(bytes((1, 2, 3, 4)) * 12)
        view = encoder.encode(frame)
        original = bytes(view)
        source = (ctypes.c_uint8 * len(frame)).from_buffer(frame)
        for pointer, size in ((None, len(frame)), (source, len(frame) - 1), (source, len(frame) + 1)):
            self.assertEqual(encoder.library.frame_encoder_encode(encoder._handle, pointer, size), 0)
            self.assertEqual(bytes(view), original)
        _, rectangles = self.reconstruct(encoder.encode(frame), 4, 3, frame)
        self.assertEqual(rectangles, [])

    def test_invalid_dimensions_and_null_native_handles_are_rejected(self):
        encoder = self.encoder(1, 1)
        for width, height in ((0, 1), (1, 0), (-1, 1), (2049, 1)):
            self.assertIsNone(encoder.library.frame_encoder_create(width, height))
        self.assertEqual(encoder.library.frame_encoder_encode(None, None, 0), 0)
        self.assertIsNone(encoder.library.frame_encoder_data(None))
        self.assertEqual(encoder.library.frame_encoder_size(None), 0)



if __name__ == '__main__':
    unittest.main()
