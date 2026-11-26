"""Use frozen ABI adapters, but require the currently built C++ implementation."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools'))
from prepare_reference import activate, digest

REFERENCE = activate()
CURRENT = ROOT / 'runtime/bundle/native.so'
if not CURRENT.is_file() or digest(CURRENT) != digest(REFERENCE / 'native.so'):
    raise RuntimeError('Current backend adapters are stale; rerun tools/prepare_reference.py after building')
from native_backend import library
if Path(library()._name).resolve() != (REFERENCE / 'native.so').resolve():
    raise RuntimeError('Backend tests must load the current C++ library')
