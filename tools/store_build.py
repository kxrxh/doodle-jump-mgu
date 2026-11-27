"""Build the app in mgu-store's network-enabled source sandbox."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import urllib.request

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
        request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(request,timeout=300) as source,temporary.open('wb') as output:
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


def include_game_file(name):
    if not (name.startswith('assets/') or name==LIBRARY):return False
    safe_path(name)
    # Host text rendering reads the X font atlas directly; the engine uses L images.
    return not name.startswith(('assets/store/','assets/popups/game-over-')) and (
        not name.endswith('_X.png') or name=='assets/fonts/doodlejumpfont_X.png')


def extract_engine(package,destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_suffix('.tmp')
    try:
        with tarfile.open(package,'r:gz') as archive:
            entry=archive.getmember('package/javascript/engine.pack')
            if not entry.isfile():raise ValueError('Expected a regular engine pack')
            with archive.extractfile(entry) as source,temporary.open('wb') as output:
                shutil.copyfileobj(source,output,CHUNK)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    app_id=sys.argv[1] if len(sys.argv)>1 else 'kxrxh_doodle_jump'
    if not app_id.replace('_','').isalnum():raise ValueError('Invalid app id')
    app=ROOT/'apps'/app_id
    if not (app/'app.json').is_file():raise ValueError('App manifest not found')
    config=json.loads((ROOT/'tools/runtime.json').read_text())
    package=download(config['url'],config['sha256'],ROOT/'.cache/released-app.tgz')
    extract_engine(package,app/'engine.pack')
    package.unlink()
    for name in ('LICENSE','NOTICE'):shutil.copy2(ROOT/name,app/name)
    subprocess.run([sys.executable,str(ROOT/'kit/tools/build.py'),app_id],cwd=ROOT,check=True)


if __name__=='__main__':main()
