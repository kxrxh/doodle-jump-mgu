"""A scrolling/fading frame must not expand its unchanged background texture again."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from test_host import HOST, REFERENCE, packet


class PaperCacheTests(unittest.TestCase):
    def test_unchanged_paper_pixels_are_read_only_when_the_texture_changes(self):
        self.assertTrue(HOST.is_file(), 'Build the native host first')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / 'texture_probe.so'
            subprocess.run(['cc', '-shared', '-fPIC', '-O2', str(Path(__file__).with_name('texture_probe.c')),
                            '-ldl', '-o', str(probe)], check=True)
            env = dict(os.environ, LD_PRELOAD=str(probe))
            with (root/'stderr').open('wb') as log, subprocess.Popen(
                    [str(HOST), '--original', str(REFERENCE/'original'), '--data', str(root/'game'),
                     '--no-audio', '--epoch', '1700000000'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=log, env=env) as child:
                paper_seen = False
                try:
                    while True:
                        kind, value = packet(child.stdout)
                        if kind == b'EVT1' and json.loads(value).get('type') == 'ready':
                            break
                    for frame in range(320):
                        child.stdin.write(json.dumps(dict(frame=True, gpu=True, gpu_reset=frame==0)).encode()+b'\n')
                        child.stdin.flush()
                        while True:
                            kind, value = packet(child.stdout)
                            if kind == b'TEX1' and int.from_bytes(value[:4], 'little') == 2:
                                paper_seen = True
                            if kind == b'GPF1':
                                break
                finally:
                    child.stdin.close()
                    child.wait(timeout=10)
                self.assertEqual(child.returncode, 0)
            self.assertTrue(paper_seen, 'The test must draw the original paper')
            match = re.search(r'PAPER_PROBE calls=(\d+)', (root/'stderr').read_text())
            self.assertIsNotNone(match)
            self.assertLessEqual(int(match[1]), 16, 'Static paper must reuse its expanded pixels across frames')


if __name__ == '__main__':
    unittest.main()
