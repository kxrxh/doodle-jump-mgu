import hashlib
import importlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib


def pack(files):
    blob=bytearray();index={}
    for name,value in files.items():
        compressor=zlib.compressobj(wbits=-15)
        data=compressor.compress(value)+compressor.flush()
        index[name]=[len(blob),len(data)];blob.extend(data)
    compressor=zlib.compressobj(wbits=-15)
    data=compressor.compress(json.dumps({'files':index,'split':{}}).encode())+compressor.flush()
    return b'MGUPACK1'+struct.pack('<I',len(data))+data+blob


class StoreBuildTests(unittest.TestCase):
    def module(self):
        try:return importlib.import_module('store_build')
        except ModuleNotFoundError:self.fail('The Store install build is missing')

    def test_install_combines_runtime_and_game_without_shop_assets(self):
        build=self.module()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runtime=root/'runtime.pack';apk=root/'game.apk';out=root/'engine.pack'
            runtime.write_bytes(pack({'engine_process.py':b'host','site-packages/elftools/__init__.py':b'ELF parser'}))
            kept={'assets/paper_L.png':b'paper','assets/fonts/doodlejumpfont_X.png':b'font',
                  'assets/Atlases/tutorial_X.atlas':b'atlas','assets/ninja/game_L.png':b'ninja',
                  'assets/jump.ogg':b'sound','lib/armeabi-v7a/libDoodleJump.so':b'engine'}
            with zipfile.ZipFile(apk,'w') as archive:
                for name,value in {**kept,'assets/paper_X.png':b'high resolution',
                        'assets/ninja/game_X.png':b'high resolution ninja','assets/store/banner.png':b'shop',
                        'assets/popups/game-over-plush_L.png':b'ad',
                        'classes.dex':b'android'}.items():archive.writestr(name,value)
            build.assemble(runtime,apk,out)
            data=out.read_bytes();n=struct.unpack_from('<I',data,8)[0];base=12+n
            files=json.loads(zlib.decompress(data[12:base],-15))['files']
            self.assertEqual(set(files),{'engine_process.py','site-packages/elftools/__init__.py','original/inventory.json'}|
                             {'original/extracted/'+name for name in kept})
            for name,value in kept.items():
                off,size=files['original/extracted/'+name]
                self.assertEqual(zlib.decompress(data[base+off:base+off+size],-15),value)

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
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runtime=root/'runtime.pack';apk=root/'game.apk'
            runtime.write_bytes(pack({'engine_process.py':b'host'}))
            with zipfile.ZipFile(apk,'w') as archive:archive.writestr('assets/../../outside',b'bad')
            with self.assertRaisesRegex(ValueError,'path'):build.assemble(runtime,apk,root/'engine.pack')
            self.assertFalse((root.parent/'outside').exists())

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
