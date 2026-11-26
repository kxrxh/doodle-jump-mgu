"""Check the production artifact after rebuilding runtime/prepare.py."""
import json
from pathlib import Path
import struct
import unittest
import zlib


PACK = Path(__file__).resolve().parents[2] / 'dist/runtime-linux-x86_64.pack'


@unittest.skipUnless(PACK.is_file(), 'requires a built production runtime')
class RuntimePackTests(unittest.TestCase):
    def test_native_runtime_excludes_the_development_emulator(self):
        with PACK.open('rb') as stream:
            header = stream.read(12)
            self.assertEqual(header[:8], b'MGUPACK1')
            index = json.loads(zlib.decompress(stream.read(struct.unpack_from('<I', header, 8)[0]), -15))
        files = set(index['files'])
        self.assertTrue({'recomp/game.so', 'recomp/machine.py', 'arm.py', 'engine_process.py'} <= files)
        unwanted = sorted(name for name in files if any(
            part.startswith(('unicorn', 'libunicorn', 'capstone')) for part in name.split('/')))
        self.assertEqual(unwanted, [], 'Development dependencies must not ship in the production runtime')


if __name__ == '__main__':
    unittest.main()
