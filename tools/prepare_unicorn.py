"""Build the patched ARM-only Unicorn oracle for Linux development tests."""
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

from store_build import download


ROOT=Path(__file__).resolve().parents[1]/'runtime'
OUTPUT=ROOT/'data/oracle/site-packages'
IMAGE='ubuntu:18.04@sha256:152dc042452c496007f07ca9127571cb9c29697f42acbfad72324b2bb2e43c98'
UNICORN_URL='https://github.com/kxrxh/doodle-jump-mgu/releases/download/v0.2.1/unicorn-2.1.4.tar.gz'
UNICORN_SHA256='00567a70e323f749b419cd86bee4f9115beab7ebba32194581c090cbb7c59cff'


def apply_unicorn_patch(source):
    lines=(ROOT/'unicorn-write-protect.patch').read_text().splitlines(keepends=True)
    if sum(line.startswith('@@ ') for line in lines)!=1:
        raise ValueError('Expected one pinned Unicorn patch hunk')
    before=''.join(line[1:] for line in lines[3:] if line.startswith((' ','-')))
    after=''.join(line[1:] for line in lines[3:] if line.startswith((' ','+')))
    target=source/'unicorn-2.1.4/src/qemu/accel/tcg/cputlb.c'
    text=target.read_text()
    if not before or text.count(before)!=1:
        raise ValueError('Unicorn write-protection patch does not match the pinned source')
    target.write_text(text.replace(before,after,1))


def main():
    archive=download(UNICORN_URL,UNICORN_SHA256,ROOT/'data/unicorn-2.1.4.tar.gz')
    source=ROOT/'data/unicorn-source'
    shutil.rmtree(source,ignore_errors=True)
    shutil.rmtree(ROOT/'data/unicorn-build',ignore_errors=True)
    source.mkdir(parents=True)
    with tarfile.open(archive) as package:package.extractall(source,filter='data')
    apply_unicorn_patch(source)
    shutil.rmtree(OUTPUT.parent,ignore_errors=True)
    subprocess.run([sys.executable,'-m','pip','install','--no-compile','--no-deps',
        '--only-binary=:all:','--platform','manylinux2014_x86_64','--implementation','cp',
        '--python-version','3.12','--abi','cp312','--target',str(OUTPUT),'unicorn==2.1.4'],check=True)
    command=['docker','run','--rm','--platform','linux/amd64','-v',f'{ROOT}:/work','-w','/work',IMAGE]
    subprocess.run(command+['bash','-ec',r'''
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends gcc g++ make libc6-dev cmake pkg-config >data/oracle-toolchain.log 2>&1 || {
    cat data/oracle-toolchain.log
    exit 1
}
mkdir -p data/unicorn-build
cd data/unicorn-build
cmake ../unicorn-source/unicorn-2.1.4/src -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
    -DUNICORN_ARCH=arm -DUNICORN_BUILD_TESTS=OFF -DUNICORN_INSTALL=OFF \
    -DUNICORN_LEGACY_STATIC_ARCHIVE=OFF >/tmp/cmake.log
test -f config-host.h
cmake --build . -- -j2 >/tmp/unicorn.log
strip --strip-unneeded libunicorn.so.2
cp libunicorn.so.2 /work/data/oracle/site-packages/unicorn/lib/libunicorn.so.2
cc -x c - -I../unicorn-source/unicorn-2.1.4/src/include -L. \
    -Wl,-rpath,/work/data/unicorn-build -l:libunicorn.so.2 -o check-arm <<'C'
#include <assert.h>
#include <unicorn/unicorn.h>
int main(void) {
    assert(uc_arch_supported(UC_ARCH_ARM));
    for (int arch=UC_ARCH_ARM+1; arch<UC_ARCH_MAX; arch++) assert(!uc_arch_supported(arch));
    return 0;
}
C
./check-arm
'''],check=True)
    library=(OUTPUT/'unicorn/lib/libunicorn.so.2').read_bytes()
    assert library[:5]==b'\x7fELF\x02' and library[18:20]==b'\x3e\x00','Expected a Linux x86-64 library'
    print(f'Development oracle: {OUTPUT}')


if __name__=='__main__':main()
