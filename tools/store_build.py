"""Build the app in mgu-store's network-enabled source sandbox."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
import zlib

ROOT=Path(__file__).resolve().parent.parent
APK_URL='https://cdn.didagame.com/20221115/doodle_jump_3.10.0_3910006.apk'
APK_SHA256='fb0e3c717f833a01085c251bd84b729c7e568a5319edbfa3c4a957d022361ddf'
LIBRARY='lib/armeabi-v7a/libDoodleJump.so'
CHUNK=1<<20


def digest(path):
    checksum=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(CHUNK),b''):checksum.update(chunk)
    return checksum.hexdigest()


def download(url,sha256,target):
    if target.is_file() and digest(target)==sha256:return target
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix('.tmp')
    try:
        with urllib.request.urlopen(url,timeout=300) as source,temporary.open('wb') as output:
            shutil.copyfileobj(source,output,CHUNK)
        if digest(temporary)!=sha256:raise ValueError('Download SHA-256 mismatch: '+url)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def safe_path(name):
    path=PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or '\0' in name:
        raise ValueError('Unsafe game pack path: '+name)
    return path


def assemble(runtime,apk,destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_suffix('.tmp')
    reserve=CHUNK
    try:
        with runtime.open('rb') as source,temporary.open('w+b') as output:
            header=source.read(12)
            if len(header)!=12 or header[:8]!=b'MGUPACK1':raise ValueError('Invalid runtime pack')
            size=struct.unpack_from('<I',header,8)[0]
            if not 0<size<CHUNK:raise ValueError('Invalid runtime index')
            index=json.loads(zlib.decompress(source.read(size),-15))['files']
            payload=runtime.stat().st_size-12-size
            for name,part in index.items():
                safe_path(name)
                if name.startswith('original/') or len(part)!=2 or any(type(n)is not int or n<0 for n in part) or sum(part)>payload:
                    raise ValueError('Invalid runtime pack entry')
            output.seek(reserve)
            shutil.copyfileobj(source,output,CHUNK)

            def append(name,stream):
                begin=output.tell();compressor=zlib.compressobj(9,zlib.DEFLATED,-15)
                for chunk in iter(lambda:stream.read(CHUNK),b''):output.write(compressor.compress(chunk))
                output.write(compressor.flush())
                index[name]=[begin-reserve,output.tell()-begin]

            with zipfile.ZipFile(apk) as archive:
                for info in sorted(archive.infolist(),key=lambda item:item.filename):
                    name=info.filename
                    if info.is_dir() or not (name.startswith('assets/') or name==LIBRARY):continue
                    safe_path(name)
                    if name.startswith(('assets/store/','assets/popups/game-over-')):continue
                    with archive.open(info) as data:append('original/extracted/'+name,data)
            import io
            append('original/inventory.json',io.BytesIO(json.dumps({'url':APK_URL,'sha256':APK_SHA256}).encode()))
            payload=output.tell()-reserve
            compressor=zlib.compressobj(9,zlib.DEFLATED,-15)
            raw=json.dumps({'files':index,'split':{}},sort_keys=True,separators=(',',':')).encode()
            encoded=compressor.compress(raw)+compressor.flush();base=12+len(encoded)
            if base>reserve:raise ValueError('Engine index exceeds its reserved space')
            # Move the payload in place: a second 80 MB file would exceed the Store's build quota.
            for offset in range(0,payload,CHUNK):
                output.seek(reserve+offset);chunk=output.read(min(CHUNK,payload-offset))
                output.seek(base+offset);output.write(chunk)
            output.truncate(base+payload);output.seek(0)
            output.write(b'MGUPACK1'+struct.pack('<I',len(encoded))+encoded)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    app_id=sys.argv[1] if len(sys.argv)>1 else 'kxrxh_doodle_jump'
    if not app_id.replace('_','').isalnum():raise ValueError('Invalid app id')
    app=ROOT/'apps'/app_id
    if not (app/'app.json').is_file():raise ValueError('App manifest not found')
    config=json.loads((ROOT/'tools/runtime.json').read_text())
    runtime=download(config['url'],config['sha256'],ROOT/'.cache/runtime.pack')
    apk=download(APK_URL,APK_SHA256,ROOT/'.cache/original.apk')
    assemble(runtime,apk,app/'engine.pack')
    runtime.unlink();apk.unlink()
    for name in ('LICENSE','NOTICE'):shutil.copy2(ROOT/name,app/name)
    subprocess.run([sys.executable,str(ROOT/'kit/tools/build.py'),app_id],cwd=ROOT,check=True)


if __name__=='__main__':main()
