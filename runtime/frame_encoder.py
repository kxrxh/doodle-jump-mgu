"""Encode lossless frame regions for the engine pipe and native renderer."""
import ctypes
import weakref

from native_backend import library


class FrameEncoder:
    def __init__(self, width, height):
        if any(type(value) is not int or not 1 <= value <= 2048 for value in (width, height)):
            raise ValueError('Invalid frame encoder dimensions')
        self.frame_bytes = width * height * 4
        self.library = lib = library()
        signatures = {
            'frame_encoder_create': ([ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
            'frame_encoder_destroy': ([ctypes.c_void_p], None),
            'frame_encoder_encode': ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int),
            'frame_encoder_data': ([ctypes.c_void_p], ctypes.c_void_p),
            'frame_encoder_size': ([ctypes.c_void_p], ctypes.c_size_t),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(lib, name)
            function.argtypes, function.restype = arguments, result
        self._handle = lib.frame_encoder_create(width, height)
        if not self._handle:
            raise RuntimeError('Could not allocate the native frame encoder')
        self._finalizer = weakref.finalize(self, lib.frame_encoder_destroy, self._handle)

    def encode(self, frame):
        """Return a native byte view, valid until the next successful encode."""
        if type(frame) is not bytearray or len(frame) != self.frame_bytes:
            raise ValueError('Expected an RGBA bytearray matching the encoder dimensions')
        source = (ctypes.c_uint8 * self.frame_bytes).from_buffer(frame)
        if not self.library.frame_encoder_encode(self._handle, source, len(frame)):
            raise RuntimeError('Could not encode the frame regions')
        size = self.library.frame_encoder_size(self._handle)
        pointer = self.library.frame_encoder_data(self._handle)
        output = (ctypes.c_uint8 * size).from_address(pointer)
        output._encoder = self
        return memoryview(output).cast('B')
