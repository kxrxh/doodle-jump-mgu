import hashlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch
import zipfile


class StoreBuildTests(unittest.TestCase):
    def module(self):
        try:return importlib.import_module('store_build')
        except ModuleNotFoundError:self.fail('The Store install build is missing')

    def test_release_engine_is_copied_without_unpacking_game_files(self):
        build=self.module()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);package=root/'app.tgz';out=root/'engine.pack'
            with tarfile.open(package,'w:gz') as archive:
                entry=tarfile.TarInfo('package/javascript/engine.pack')
                entry.size=len(b'released engine')
                archive.addfile(entry,io.BytesIO(b'released engine'))
                entry=tarfile.TarInfo('../../outside')
                entry.size=3
                archive.addfile(entry,io.BytesIO(b'bad'))
            build.extract_engine(package,out)
            self.assertEqual(out.read_bytes(),b'released engine')
            self.assertFalse((root.parent/'outside').exists())

    def test_invalid_release_preserves_existing_engine(self):
        build=self.module()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);package=root/'app.tgz';out=root/'engine.pack'
            out.write_bytes(b'existing engine')
            with tarfile.open(package,'w:gz') as archive:
                entry=tarfile.TarInfo('package/javascript/engine.pack')
                entry.type=tarfile.SYMTYPE
                entry.linkname='/etc/passwd'
                archive.addfile(entry)
            with self.assertRaisesRegex(ValueError,'regular engine pack'):
                build.extract_engine(package,out)
            self.assertEqual(out.read_bytes(),b'existing engine')
            self.assertFalse(out.with_suffix('.tmp').exists())

    def test_local_extraction_omits_extra_x_images_without_dropping_themes_or_sound(self):
        original=importlib.import_module('get_original')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);apk=root/'.cache/original.apk';apk.parent.mkdir()
            kept={'assets/paper_L.png':b'paper','assets/fonts/doodlejumpfont_X.png':b'font',
                  'assets/Atlases/tutorial_X.atlas':b'atlas','assets/ninja/game_L.png':b'ninja',
                  'assets/jump.ogg':b'sound','lib/armeabi-v7a/libDoodleJump.so':b'engine'}
            with zipfile.ZipFile(apk,'w') as archive:
                for name,value in {**kept,'assets/paper_X.png':b'high resolution',
                        'assets/ninja/game_X.png':b'high resolution ninja','assets/store/banner.png':b'shop',
                        'assets/popups/game-over-plush_L.png':b'ad','classes.dex':b'android'}.items():archive.writestr(name,value)
            with patch.object(original,'ROOT',root),patch.object(original,'APK_SHA256',hashlib.sha256(apk.read_bytes()).hexdigest()):
                original.main()
            extracted=root/'original/extracted'
            self.assertEqual({file.relative_to(extracted).as_posix():file.read_bytes()
                              for file in extracted.rglob('*') if file.is_file()},kept)

    def test_zip_paths_cannot_escape_the_game_directory(self):
        build=self.module()
        with self.assertRaisesRegex(ValueError,'path'):
            build.include_game_file('assets/../../outside')

    def test_download_checks_digest_before_replacing_a_cached_file(self):
        build=self.module()
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'runtime.pack';target.write_bytes(b'old')
            with patch('urllib.request.urlopen',return_value=io.BytesIO(b'corrupt')):
                with self.assertRaisesRegex(ValueError,'SHA-256'):
                    build.download('https://example.test/runtime.pack',hashlib.sha256(b'expected').hexdigest(),target)
            self.assertEqual(target.read_bytes(),b'old')

    def test_download_uses_a_client_header_accepted_by_the_game_cdn(self):
        build=self.module()
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'game.apk'
            with patch('urllib.request.urlopen',return_value=io.BytesIO(b'game')) as fetch:
                build.download('https://example.test/game.apk',hashlib.sha256(b'game').hexdigest(),target)
            import urllib.request
            request=fetch.call_args[0][0]
            self.assertIsInstance(request,urllib.request.Request)
            self.assertTrue(request.get_header('User-agent','').startswith('Mozilla/'))
            self.assertEqual(target.read_bytes(),b'game')


if __name__=='__main__':unittest.main()
