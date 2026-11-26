#!/usr/bin/env python3
"""pack_data.py <folder> [out.pack] - create an engine-compatible runtime pack.

runtime/prepare.py uses this to package the prebuilt host. Each file is deflated
separately and retains its original bytes. Files matching .packignore globs are
excluded; lines starting with '#' are comments.

Format: b'MGUPACK1', u32 LE index length, the index (raw deflate of JSON
{"files": {name: [offset, length]}, "split": {}}), then the
data, offsets counted from the end of the index.
"""
import fnmatch, json, os, struct, sys, zlib

MAGIC = b'MGUPACK1'


def deflate(b):
    c = zlib.compressobj(9, zlib.DEFLATED, -15, 9)
    return c.compress(b) + c.flush()


def dump(o):
    return json.dumps(o, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')


def pack(folder, out):
    ignore = ['.packignore']
    pi = os.path.join(folder, '.packignore')
    if os.path.isfile(pi):
        with open(pi, encoding='utf-8') as source:
            ignore += [l.strip() for l in source if l.strip() and not l.startswith('#')]
    entries, raw_total = [], 0
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(root, f), folder).replace(os.sep, '/')
            if any(fnmatch.fnmatch(rel, g) for g in ignore):
                continue
            with open(os.path.join(root, f), 'rb') as source:
                data = source.read()
            raw_total += len(data)
            entries.append((rel, data))
    files, blob = {}, []
    off = 0
    for name, data in entries:
        z = deflate(data)
        files[name] = [off, len(z)]
        blob.append(z)
        off += len(z)
    index = deflate(dump({'files': files, 'split': {}}))
    tmp = out + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(MAGIC + struct.pack('<I', len(index)) + index)
        for z in blob:
            f.write(z)
    os.replace(tmp, out)
    return raw_total, os.path.getsize(out), len(entries)


if __name__ == '__main__':
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    src = sys.argv[1].rstrip('/\\')
    dst = sys.argv[2] if len(sys.argv) == 3 else src + '.pack'
    raw, size, n = pack(src, dst)
    print('%s: %.1f MB in %d parts -> %s (%.2f MB)' % (src, raw / 1e6, n, dst, size / 1e6))
