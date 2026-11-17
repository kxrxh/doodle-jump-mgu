import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / 'native/doodle-gpu/gpu_check.js'
UPSTREAM = ROOT / '.cache/gpu-renderer-source'


def packet(kind, *values):
    body = struct.pack('<' + 'I' * len(values), *values)
    return struct.pack('<II', kind, len(body)) + body


class GpuCheckTests(unittest.TestCase):
    def probe(self, chunks, close=True):
        with tempfile.TemporaryDirectory(prefix='dj-gpu-', dir='/tmp') as directory:
            endpoint = str(Path(directory) / 'render.sock')
            server = socket.socket(socket.AF_UNIX)
            server.bind(endpoint)
            server.listen(1)
            server.settimeout(2)
            received = bytearray()

            def respond():
                try:
                    connection, _ = server.accept()
                    with connection:
                        connection.settimeout(2)
                        while len(received) < 8 or len(received) < 8 + struct.unpack_from('<I', received, 4)[0]:
                            data = connection.recv(4096)
                            if not data:
                                return
                            received.extend(data)
                        for chunk in chunks:
                            connection.sendall(chunk)
                        if not close:
                            connection.recv(4096)
                except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
                    pass

            thread = threading.Thread(target=respond)
            thread.start()
            env = dict(os.environ, DOODLE_GPU_CHECK_SOCKET=endpoint, DOODLE_GPU_CHECK_TIMEOUT_MS='120')
            result = subprocess.run(['node', str(CHECK)], env=env, capture_output=True, text=True, timeout=3)
            thread.join(3)
            server.close()
            return result, bytes(received)

    def test_fragmented_capability_and_ready_answer_pass_without_showing_a_surface(self):
        data = packet(0x4d520101, 1, 6, 256) + packet(101, 4, 1920, 720, 1)
        result, sent = self.probe([data[:3], data[3:19], data[19:]])
        self.assertEqual(result.returncode, 0, result.stderr)
        kind, size = struct.unpack_from('<II', sent)
        self.assertEqual(kind, 1)
        self.assertEqual(len(sent), 8 + size)
        self.assertIn(b'\0proto=mgu-render/1', sent[8:])

    def test_missing_capability_truncated_answer_and_silence_fail(self):
        cases = [([packet(0x4d520101, 1, 6, 8) + packet(101, 4, 1920, 720, 1)], True, 'GPU scenes'),
                 ([packet(0x4d520101, 1, 6, 256)[:-1]], True, 'closed'),
                 ([struct.pack('<II', 101, 0x1000000)], False, 'large'),
                 ([], False, 'timeout')]
        for chunks, close, reason in cases:
            with self.subTest(chunks=chunks, close=close):
                result, _ = self.probe(chunks, close)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stderr)

    def test_removed_hook_only_requests_a_restart(self):
        result = subprocess.run(['node', str(CHECK), '--removed'], capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['reboot'])


class GpuPackageTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module('build_gpu_renderer')
        except ModuleNotFoundError:
            self.fail('GPU renderer packaging is missing')

    def test_package_replaces_only_the_renderer_executable_and_keeps_sources_and_licenses(self):
        build = self.module()
        node = subprocess.check_output(['node', '--version'], text=True).strip()
        if int(node.lstrip('v').split('.')[0]) < 14:
            self.skipTest('the Store developer packager requires Node.js 14 or later')
        if not (UPSTREAM / 'deploy/hu').is_file():
            self.skipTest('pinned Store checkout is needed for the real artifact packager')
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / 'source'
            root = directory / 'repo'
            renderer = source / 'native/mgu-render'
            (renderer / 'build/target').mkdir(parents=True)
            binary = b'\x7fELF' + b'fixture renderer'
            (renderer / 'build/target/mgu-render').write_bytes(binary)
            (renderer / 'scene.c').write_text('fixture corresponding source\n')
            (renderer / 'LICENSE.mgu-renderd').write_text('renderer license\n')
            (source / 'LICENSE').write_text('Store license\n')
            for name in ('deploy/hu', 'tools/oap_pack.js'):
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(UPSTREAM / name, source / name)
            shutil.copytree(ROOT / 'native/doodle-gpu', root / 'native/doodle-gpu',
                            ignore=shutil.ignore_patterns('build'))
            with patch.object(build, 'ROOT', root):
                artifact, sources = build.package(source)
            with tarfile.open(artifact) as archive:
                manifest = json.load(archive.extractfile('component.json'))
                self.assertEqual(manifest['id'], 'doodle-gpu')
                self.assertEqual(manifest['units'], [])
                self.assertEqual(manifest['restart'], ['mgu-render.service'])
                self.assertEqual(manifest['dropins'], [{'unit': 'mgu-render.service', 'name': '90-doodle-gpu.conf'}])
                self.assertEqual(archive.extractfile('files/mgu-render').read(), binary)
                self.assertEqual(archive.extractfile('files/LICENSE.store').read(), b'Store license\n')
                self.assertEqual(archive.extractfile('files/LICENSE.mgu-renderd').read(), b'renderer license\n')
                entry = next(x for x in manifest['files'] if x['path'] == 'mgu-render')
                self.assertEqual(entry['sha256'], hashlib.sha256(binary).hexdigest())
                self.assertEqual(entry['mode'], '0755')
                self.assertEqual(archive.extractfile('dropins/mgu-render.service.d/90-doodle-gpu.conf').read(),
                                 b'[Service]\nExecStart=\nExecStart=/var/opt/doodle-gpu/current/mgu-render -c /var/opt/mgu-render/current/mgu-render.conf\n')
            with tarfile.open(sources) as archive:
                self.assertTrue(any(name.endswith('/scene.c') for name in archive.getnames()))
                self.assertFalse(any('/build/' in name for name in archive.getnames()))

    def test_source_archive_cannot_write_outside_its_directory(self):
        build = self.module()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / 'source.tar.gz'
            with tarfile.open(source, 'w:gz') as archive:
                info = tarfile.TarInfo('../escaped')
                info.size = 1
                archive.addfile(info, io.BytesIO(b'x'))
            with self.assertRaisesRegex(ValueError, 'source archive'):
                build.extract(source, directory / 'out')
            self.assertFalse((directory / 'escaped').exists())


if __name__ == '__main__':
    unittest.main()
