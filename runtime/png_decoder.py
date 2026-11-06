"""Host implementation of the original 3.10.0 PNG loader's verified ABI."""
import ctypes
import io
import struct
from PIL import Image,UnidentifiedImageError


class PngDecoder:
    def __init__(self, machine, library):
        self.m=machine
        self.convert=library.png_pixels
        self.convert.argtypes=[ctypes.c_void_p,ctypes.c_void_p]+[ctypes.c_int]*4
        self.convert.restype=None
        # Original RVA 0xadc58: PNG bytes, size, pixel format, NPOT flag, nine-word result.
        machine.intercept(0xadc58,'host.decode_png',self.decode)

    def decode(self):
        m=self.m
        source,length,mode,npot,result=(m.arg(i) for i in range(5))
        if length>64*1024*1024:
            raise ValueError('PNG exceeds the engine asset size limit')
        try:
            with Image.open(io.BytesIO(bytes(m.uc.mem_read(source,length)))) as image:
                if not 0<image.width<=4096 or not 0<image.height<=4096:
                    raise ValueError('Invalid game image dimensions')
                image=image.convert('RGBA')
        except (UnidentifiedImageError,OSError):
            return 0
        width,height=image.size
        tw=width if npot else 1<<(width-1).bit_length()
        th=height if npot else 1<<(height-1).bit_length()
        fmt,kind,bpp={2:(0x1908,0x8033,2),4:(0x1907,0x8363,2),6:(0x1909,0x1401,1),
                      7:(0x1906,0x1401,1),8:(0x190A,0x1401,2),13:(0x1908,0x8033,2)}.get(mode,(0x1908,0x1401,4))
        size=tw*th*bpp
        output=ctypes.create_string_buffer(size)
        rgba=image.tobytes()
        self.convert(output,ctypes.c_char_p(rgba),width,height,tw,mode)
        pointer=m.malloc(size)
        m.uc.mem_write(pointer,output.raw)
        m.uc.mem_write(result,struct.pack('<9I',pointer,fmt,kind,bpp,size,width,height,tw,th))
        return 1
