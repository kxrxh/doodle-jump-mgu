"""The GPU stream must preserve GLES draw state without rasterizing a CPU frame."""
import struct
import unittest

import test_gles


TEXTURE = 0x4d520007
SCENE = 0x4d520008


def messages(data):
    result = []
    offset = 0
    while offset < len(data):
        kind, size = struct.unpack_from('<II', data, offset)
        offset += 8
        result.append((kind, data[offset:offset + size]))
        offset += size
    assert offset == len(data)
    return result


def commands(payload):
    width, height = struct.unpack_from('<II', payload)
    result = []
    offset = 8
    while offset < len(payload):
        op = struct.unpack_from('<I', payload, offset)[0]
        if op == 1:
            result.append(('clear', struct.unpack_from('<4f', payload, offset + 4)))
            offset += 20
        else:
            assert op == 2
            token, flags, source, destination, replace, count = struct.unpack_from('<6I', payload, offset + 4)
            vertices = struct.unpack_from('<' + 'f' * count * 8, payload, offset + 28)
            result.append(('draw', token, flags, source, destination, replace, vertices))
            offset += 28 + count * 32
    assert offset == len(payload)
    return width, height, result


class GpuCaptureTests(unittest.TestCase):
    def context(self):
        m, renderer, gl, upload, draw = test_gles.GraphicsTests().compact_context()
        upload(bytes((10, 20, 30, 255)), 1, 1)
        draw()
        return m, renderer, gl, upload

    def test_gpu_captures_clear_texture_vertices_and_blend_without_writing_cpu_frame(self):
        m, renderer, gl, upload = self.context()
        before = bytes(renderer.frame)
        renderer.begin_gpu_frame(True)
        gl('glClearColor', .25, .5, .75, 1.0)
        gl('glClear', 0x4000)
        gl('glEnable', 0xbe2)
        gl('glBlendFunc', 1, 771)
        gl('glDrawArrays', 5, 0, 4)
        records = messages(renderer.gpu_frame())
        self.assertEqual(records[0], (TEXTURE, struct.pack('<III4B', 1, 1, 1, 10, 20, 30, 255)))
        self.assertEqual(records[1][0], SCENE)
        width, height, ops = commands(records[1][1])
        self.assertEqual((width, height), (4, 4))
        self.assertEqual(ops[0], ('clear', (.25, .5, .75, 1.0)))
        self.assertEqual(ops[1][:6], ('draw', 1, 15, 1, 771, 0))
        self.assertEqual(ops[1][6], (
            0, 0, 0, 0, 1, 1, 1, 1, 4, 0, 1, 0, 1, 1, 1, 1,
            0, 4, 0, 1, 1, 1, 1, 1, 4, 0, 1, 0, 1, 1, 1, 1,
            0, 4, 0, 1, 1, 1, 1, 1, 4, 4, 1, 1, 1, 1, 1, 1))
        self.assertEqual(bytes(renderer.frame), before)
        self.assertEqual(renderer.gpu_frame(), b''.join(struct.pack('<II', kind, len(body)) + body for kind, body in records))
        self.assertEqual(renderer.input_rectangles[-1], ((0, 0, 4, 4), False))

    def test_sampler_changes_reuse_texture_upload_and_keep_draw_flags(self):
        m, renderer, gl, upload = self.context()
        renderer.begin_gpu_frame(True)
        gl('glDrawArrays', 5, 0, 4)
        renderer.gpu_frame()
        renderer.begin_gpu_frame(True)
        gl('glTexParameteri', 0xde1, 0x2800, 9728)
        gl('glTexParameteri', 0xde1, 0x2802, 33071)
        gl('glDrawArrays', 5, 0, 4)
        records = messages(renderer.gpu_frame())
        self.assertEqual([kind for kind, _ in records], [SCENE])
        self.assertEqual(commands(records[0][1])[2][0][:6], ('draw', 1, 8, 1, 0, 0))

    def test_midframe_upload_keeps_both_versions_then_retires_old_texture(self):
        m, renderer, gl, upload = self.context()
        renderer.begin_gpu_frame(True)
        gl('glDrawArrays', 5, 0, 4)
        upload(bytes((40, 50, 60, 128)), 1, 1)
        gl('glDrawArrays', 5, 0, 4)
        records = messages(renderer.gpu_frame())
        self.assertEqual([kind for kind, _ in records], [TEXTURE, TEXTURE, SCENE])
        self.assertEqual(records[1][1], struct.pack('<III4B', 2, 1, 1, 40, 50, 60, 128))
        self.assertEqual([op[1] for op in commands(records[2][1])[2]], [1, 2])
        renderer.begin_gpu_frame(True)
        gl('glDrawArrays', 5, 0, 4)
        records = messages(renderer.gpu_frame())
        self.assertEqual([kind for kind, _ in records], [SCENE, TEXTURE])
        self.assertEqual(records[1][1], struct.pack('<III', 1, 0, 0))

    def test_deleted_textures_are_released_after_scene_and_reset_reuploads_live_texture(self):
        m, renderer, gl, upload = self.context()
        renderer.begin_gpu_frame(True)
        gl('glDrawArrays', 5, 0, 4)
        renderer.gpu_frame()
        renderer.begin_gpu_frame(True, reset=True)
        gl('glDrawArrays', 5, 0, 4)
        records = messages(renderer.gpu_frame())
        self.assertEqual([kind for kind, _ in records], [TEXTURE, SCENE, TEXTURE])
        self.assertEqual(struct.unpack_from('<III', records[0][1]), (2, 1, 1))
        self.assertEqual(records[2][1], struct.pack('<III', 1, 0, 0))
        renderer.begin_gpu_frame(True)
        pointer = m.malloc(4)
        m.uc.mem_write(pointer, struct.pack('<I', 1))
        gl('glDeleteTextures', 1, pointer)
        records = messages(renderer.gpu_frame())
        self.assertEqual(records, [(SCENE, struct.pack('<II', 4, 4)), (TEXTURE, struct.pack('<III', 2, 0, 0))])

    def test_packed_pixels_expand_exactly_and_oversized_gpu_upload_is_rejected(self):
        m, renderer, gl, upload = self.context()
        upload(struct.pack('<H', 0x1234), 1, 1, 0x8033)
        renderer.begin_gpu_frame(True)
        gl('glDrawArrays', 5, 0, 4)
        self.assertEqual(messages(renderer.gpu_frame())[0],
                         (TEXTURE, struct.pack('<III4B', 1, 1, 1, 17, 34, 51, 68)))
        upload(bytes((1, 2, 3, 255)) * 2049, 2049, 1)
        renderer.begin_gpu_frame(True)
        with self.assertRaisesRegex(RuntimeError, 'GPU texture exceeds'):
            gl('glDrawArrays', 5, 0, 4)

    def test_incomplete_triangle_vertices_are_ignored(self):
        m, renderer, gl, upload = self.context()
        renderer.begin_gpu_frame(True)
        gl('glDrawArrays', 4, 0, 4)
        records = messages(renderer.gpu_frame())
        self.assertEqual(len(commands(records[1][1])[2][0][6]), 24)

    def test_switching_back_to_cpu_draws_and_clears_the_frame(self):
        m, renderer, gl, upload = self.context()
        renderer.begin_gpu_frame(True)
        gl('glClearColor', 1.0, 0.0, 0.0, 1.0)
        gl('glClear', 0x4000)
        renderer.gpu_frame()
        renderer.begin_gpu_frame(False)
        gl('glClear', 0x4000)
        self.assertEqual(renderer.image().getpixel((0, 0)), (255, 0, 0, 255))
        gl('glDrawArrays', 5, 0, 4)
        self.assertEqual(renderer.image().getpixel((1, 1)), (10, 20, 30, 255))
        self.assertEqual(renderer.gpu_frame(), b'')

    def test_excluded_assets_keep_hit_regions_without_upload_or_draw(self):
        m, renderer, gl, upload = self.context()
        renderer.asset_name = 'store.png'
        upload(bytes((1, 2, 3, 255)), 1, 1)
        renderer.begin_gpu_frame(True)
        gl('glDrawArrays', 5, 0, 4)
        self.assertEqual(messages(renderer.gpu_frame()), [(SCENE, struct.pack('<II', 4, 4))])
        self.assertEqual(renderer.input_rectangles[-1], ((0, 0, 4, 4), True))


if __name__ == '__main__':
    unittest.main()
