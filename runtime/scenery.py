"""Extend the original paper and sprite art into the MGU screen's margins."""
import ctypes
from pathlib import Path

from PIL import Image

from jni import Jni

# Crops from the original gametiles_L.png; original pixels and attribution are retained.
SPRITES = {
    'platform': (10,(0,0,90,24)),
    'broken': (11,(0,107,90,138)),
    'bat': (12,(223,0,340,73)),
    'ufo': (13,(642,309,769,362)),
    'monster': (14,(101,277,216,358)),
    'springs': (15,(298,396,334,449)),
}


class Scenery:
    def __init__(self,game):
        self.game=game
        self.key=None
        self.sprites={}

    def assets(self):
        images=[]
        with Image.open(self.game.jni.assets/'gametiles_L.png') as atlas:
            atlas=atlas.convert('RGBA')
            for name,(identifier,box) in SPRITES.items():
                image=atlas.crop(box).convert('RGBa')
                self.sprites[name]=dict(id=identifier,width=image.width,height=image.height)
                images.append((identifier,image))
        with Image.open(Path(__file__).parent/'assets/cursor.png') as cursor:
            self.sprites['cursor']=dict(id=20,width=cursor.width,height=cursor.height,
                hotspotX=int(cursor.info['hotspot_x']),hotspotY=int(cursor.info['hotspot_y']))
            images.append((20,cursor.convert('RGBa')))
        return images

    def paper(self,width):
        g=self.game.graphics
        if not g.background_info or not width:
            return []
        identifier,vertices=g.background_info
        texture=g.textures[identifier]
        key=(identifier,id(texture['pixels']),vertices,width)
        if key==self.key:
            return []
        self.key=key
        with Image.open(Jni.path(self.game.jni.assets,texture['asset'])) as original:
            content=original.size
        source=Image.frombytes('RGBA',(texture['width'],texture['height']),texture['pixels']).crop((0,0,*content))
        pixels=source.tobytes()
        left,top=min(v[0] for v in vertices),min(v[1] for v in vertices)
        right,bottom=max(v[0] for v in vertices),max(v[1] for v in vertices)
        corner=lambda x,y:min(vertices,key=lambda v:(v[0]-x)**2+(v[1]-y)**2)
        tl,tr,bl=corner(left,top),corner(right,top),corner(left,bottom)
        du=(tr[2]-tl[2])/(right-left)*texture['width']/content[0]
        dv=(bl[3]-tl[3])/(bottom-top)*texture['height']/content[1]
        u=tl[2]*texture['width']/content[0]-left*du
        v=tl[3]*texture['height']/content[1]-top*dv
        color=tl[4:]
        def render(offset):
            image=bytearray(width*g.height*4)
            pointer=(ctypes.c_uint8*len(image)).from_buffer(image)
            corners=[(0,0,u+offset*du,v,*color),(width,0,u+(offset+width)*du,v,*color),
                     (0,g.height,u+offset*du,v+g.height*dv,*color),
                     (width,g.height,u+(offset+width)*du,v+g.height*dv,*color)]
            triangles=[corners[i] for i in (0,1,2,2,1,3)]
            data=(ctypes.c_float*48)(*(number for vertex in triangles for number in vertex))
            g.native(pointer,width,g.height,data,6,ctypes.c_char_p(pixels),content[0],content[1],1,1,1,0,1,0,0)
            return Image.frombytes('RGBA',(width,g.height),bytes(image))
        return [(2,render(-width)),(3,render(g.width))]
