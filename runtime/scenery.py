"""Extend the original paper and sprite art into the MGU screen's margins."""
import math
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
        self.asset_key=None
        self.projection=None
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
        background=g.background_info
        if not background or not width:
            self.projection=None
            return []
        identifier,vertices=background
        texture=g.texture_info(identifier)
        asset_key=(identifier,texture['revision'],texture['asset'])
        if asset_key!=self.asset_key:
            with Image.open(Jni.path(self.game.jni.assets,texture['asset'])) as original:
                self.content=original.size
            self.asset_key=asset_key
        content=self.content
        left,top=min(v[0] for v in vertices),min(v[1] for v in vertices)
        right,bottom=max(v[0] for v in vertices),max(v[1] for v in vertices)
        if right<=left or bottom<=top:
            raise ValueError('Invalid paper geometry')
        corner=lambda x,y:min(vertices,key=lambda v:(v[0]-x)**2+(v[1]-y)**2)
        tl,tr,bl=corner(left,top),corner(right,top),corner(left,bottom)
        du=(tr[2]-tl[2])/(right-left)*texture['width']/content[0]
        dv=(bl[3]-tl[3])/(bottom-top)*texture['height']/content[1]
        u=tl[2]*texture['width']/content[0]-left*du
        v=tl[3]*texture['height']/content[1]-top*dv
        color=tl[4:]
        if not all(math.isfinite(n) for n in (u,v,du,dv,*color)) or not du or not dv:
            raise ValueError('Invalid paper projection')
        self.projection=dict(width=content[0],height=content[1],
            u=(u if du>0 else -u)%1,v=(v if dv>0 else -v)%1,
            du=abs(du),dv=abs(dv),alpha=max(0,min(1,color[3])))
        key=(asset_key,color[:3],du<0,dv<0)
        if key==self.key:
            return []
        source=Image.frombytes('RGBA',(texture['width'],texture['height']),texture['pixels']).crop((0,0,*content))
        if color[:3]!=(1,1,1):
            source=source.point([min(255,max(0,int(value*factor+.5)))
                for factor in (*color[:3],1) for value in range(256)])
        if du<0:source=source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if dv<0:source=source.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        # Wrapped border texels let ordinary linear-filtered images tile without seams.
        w,h=content
        image=Image.new('RGBA',(w+2,h+2))
        image.paste(source,(1,1))
        image.paste(source.crop((w-1,0,w,h)),(0,1))
        image.paste(source.crop((0,0,1,h)),(w+1,1))
        image.paste(source.crop((0,h-1,w,h)),(1,0))
        image.paste(source.crop((0,0,w,1)),(1,h+1))
        for x,y in ((0,0),(w+1,0),(0,h+1),(w+1,h+1)):
            image.putpixel((x,y),source.getpixel(((x-1)%w,(y-1)%h)))
        self.key=key
        return [(2,image.convert('RGBa'))]
