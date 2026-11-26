import io
import struct
import unittest
from PIL import Image
from arm import Arm,BASE
from libc import LibC
from gles import GLES
from test_arm import LIBRARY

class PngTests(unittest.TestCase):
    def test_host_decoder_matches_original_pixels_and_android_result_layout(self):
        from png_decoder import PngDecoder
        m=Arm(LIBRARY);LibC(m);m.initialize()
        image=Image.new('RGBA',(3,2))
        image.putdata([(255,100,16,0),(101,54,231,128),(222,111,33,255)]*2)
        png=io.BytesIO();image.save(png,format='PNG');data=png.getvalue()
        pointer=m.malloc(len(data));m.uc.mem_write(pointer,data)
        expected=[]
        for mode in (0,2,4,6,7,8,13,14):
            result=m.malloc(36)
            self.assertEqual(m.call(BASE+0xadc59,pointer,len(data),mode,0,result),1)
            words=list(struct.unpack('<9I',m.uc.mem_read(result,36)))
            expected.append((mode,words[1:],bytes(m.uc.mem_read(words[0],words[4]))))
        renderer=GLES(m,4,4)
        PngDecoder(m,renderer.library)
        for mode,words,pixels in expected:
            result=m.malloc(36)
            self.assertEqual(m.call(BASE+0xadc59,pointer,len(data),mode,0,result),1)
            actual=list(struct.unpack('<9I',m.uc.mem_read(result,36)))
            self.assertEqual(actual[1:],words,mode)
            self.assertEqual(bytes(m.uc.mem_read(actual[0],actual[4])),pixels,mode)
