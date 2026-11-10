"""Fetch the verified game files for local testing."""
import json
import zipfile
from store_build import ROOT,APK_URL,APK_SHA256,download,include_game_file


def main():
    apk=download(APK_URL,APK_SHA256,ROOT/'.cache/original.apk')
    root=ROOT/'original'
    with zipfile.ZipFile(apk) as archive:
        for entry in archive.infolist():
            name=entry.filename
            if entry.is_dir() or not include_game_file(name):continue
            path=root/'extracted'/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(archive.read(entry))
    (root/'inventory.json').write_text(json.dumps({'url':APK_URL,'sha256':APK_SHA256})+'\n')


if __name__=='__main__':main()
