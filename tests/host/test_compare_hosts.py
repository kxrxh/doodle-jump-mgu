"""The comparison runner must reject truncated output and real media differences."""
import importlib.util
import os
from pathlib import Path
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('compare_hosts', ROOT / 'tools/compare_hosts.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ComparisonTests(unittest.TestCase):
    def pipe(self, data):
        read, write = os.pipe()
        os.write(write, data)
        os.close(write)
        return os.fdopen(read, 'rb', buffering=0)

    def test_packet_reader_keeps_binary_payload_and_rejects_truncation(self):
        with self.pipe(b'GPF1' + struct.pack('<I', 3) + b'\x00\xffx') as source:
            self.assertEqual(runner.read_packet(source), (b'GPF1', b'\x00\xffx'))
        with self.pipe(b'GPF1' + struct.pack('<I', 4) + b'one') as source:
            with self.assertRaises(EOFError):
                runner.read_packet(source)
        with self.pipe(b'GPF1' + struct.pack('<I', runner.MAX_PACKET + 1)) as source:
            with self.assertRaises(ValueError):
                runner.read_packet(source)

    def test_scenery_json_order_does_not_hide_texture_or_frame_changes(self):
        runner.compare_packets([(b'EVT1', b'{"type":"scenery","paper":null}')],
                               [(b'EVT1', b'{"paper":null,"type":"scenery"}')], 'startup')
        with self.assertRaises(AssertionError):
            runner.compare_packets([(b'EVT1', b'{"width":2}')],
                                   [(b'EVT1', b'{"width":2.0000000000001}')], 'width')
        for kind in (b'TEX1', b'GPF1', b'FRM2'):
            with self.assertRaises(AssertionError):
                runner.compare_packets([(kind, b'old')], [(kind, b'new')], 'frame')

    def test_audio_voice_bound_tracks_stops_unloads_and_resets(self):
        events = [dict(type='load', id=1, path='jump.ogg', loop=False),
                  dict(type='load', id=2, path='fall.ogg', loop=False),
                  *[dict(type='message', message=m, id=i, gain=g, loops=0)
                    for m, i, g in ((8, 1, 1), (8, 2, .5), (9, 1, 1), (14, 0, 1),
                                    (8, 1, 1), (7, 1, 1))]]
        self.assertEqual(runner.audio_voice_bound(events), 2)

    def test_pcm_comparison_enforces_frame_count_and_quantization_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            left, right = (Path(temporary) / name for name in ('left.pcm', 'right.pcm'))
            left.write_bytes(struct.pack('<4h', 0, 10, -10, 32767))
            right.write_bytes(struct.pack('<4h', 0, 11, -9, 32767))
            report = runner.compare_pcm(left, right, 1, samples_per_frame=4, tolerance=1)
            self.assertEqual((report['maxDifference'], report['changedSamples']), (1, 2))
            with self.assertRaises(AssertionError):
                runner.compare_pcm(left, right, 1, samples_per_frame=4, tolerance=0)
            right.write_bytes(b'')
            with self.assertRaises(AssertionError):
                runner.compare_pcm(left, right, 1, samples_per_frame=4, tolerance=1)


if __name__ == '__main__':
    unittest.main()
