import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from native_backend import library


class NativeBackendTests(unittest.TestCase):
    def test_extracted_backend_loads_with_newer_sources_and_no_compilers(self):
        binary = Path(library()._name)
        original = binary.parent
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for path in [*original.glob('*.cpp'), *original.glob('*.h'),
                         original / 'raster.c', original / 'native_backend.py', binary,
                         *original.glob('native.*.sha256')]:
                shutil.copy2(path, directory / path.name)
            timestamp = binary.stat().st_mtime + 10
            os.utime(directory / 'raster.c', (timestamp, timestamp))
            spec = importlib.util.spec_from_file_location('extracted_backend', directory / 'native_backend.py')
            backend = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(backend)
            with patch.dict(os.environ, {'PATH': ''}):
                loaded = backend.library()
            self.assertEqual(loaded.graphics_create(0, 0, None), 0)


if __name__ == '__main__':
    unittest.main()
