from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image
from game import Game
from scenery import Scenery


class SceneryTests(unittest.TestCase):
    def fixture(self, directory):
        pixels=bytes([255,0,0,255,0,255,0,255,0,0,255,255,255,255,255,128])
        Image.frombytes('RGBA',(2,2),pixels).save(Path(directory)/'bck_L.png')
        texture=dict(width=2,height=2,pixels=pixels,asset='bck_L.png',revision=1)
        graphics=SimpleNamespace(background_info=(7,()),texture_info=lambda _:texture,
                                 logical_width=544,logical_height=960,native=lambda *args:None)
        game=SimpleNamespace(graphics=graphics,jni=SimpleNamespace(assets=Path(directory)))
        return Scenery(game),graphics,texture

    def vertices(self,v=0,alpha=1,flip=False):
        color=(1,1,1,alpha)
        return ((0,0,1 if flip else 0,v,*color),(544,0,0 if flip else 1,v,*color),
                (0,960,1 if flip else 0,v+1,*color),(544,960,0 if flip else 1,v+1,*color))

    def test_scrolling_and_fading_change_projection_without_uploading_images(self):
        with tempfile.TemporaryDirectory() as directory:
            scenery,graphics,texture=self.fixture(directory)
            graphics.background_info=(7,self.vertices())
            self.assertEqual(len(scenery.paper(1008)),1)
            first=scenery.projection.copy()
            graphics.background_info=(7,self.vertices(.25,.5))
            self.assertEqual(scenery.paper(1008),[],'Scrolling must not regenerate side images')
            self.assertEqual(scenery.projection['v'],.25)
            self.assertEqual(scenery.projection['alpha'],.5)
            self.assertEqual((scenery.projection['du'],scenery.projection['dv']),(1/544,1/960))
            self.assertEqual(scenery.paper(600),[],'Layout width changes use the same source texture')
            self.assertNotEqual(scenery.projection,first)
            texture['revision']+=1
            self.assertEqual(len(scenery.paper(600)),1,'Updated source pixels must be republished')

    def test_source_wrap_border_and_flipped_projection_preserve_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            scenery,graphics,texture=self.fixture(directory)
            graphics.background_info=(7,self.vertices())
            identifier,image=scenery.paper(1008)[0]
            self.assertEqual((identifier,image.size,image.mode),(2,(4,4),'RGBa'))
            source=Image.frombytes('RGBA',(2,2),texture['pixels']).convert('RGBa')
            for y in range(4):
                for x in range(4):
                    self.assertEqual(image.getpixel((x,y)),source.getpixel(((x-1)%2,(y-1)%2)))
            graphics.background_info=(7,self.vertices(3.25,flip=True))
            flipped=scenery.paper(1008)[0][1]
            self.assertEqual(flipped.getpixel((1,1)),source.getpixel((1,0)))
            self.assertEqual(scenery.projection['v'],.25)
            self.assertGreater(scenery.projection['du'],0)
            self.assertEqual(scenery.projection['u'],0)

    def test_original_art_is_published_once_with_a_small_paper_source(self):
        with tempfile.TemporaryDirectory() as data:
            game=Game(data=Path(data),render_size=(408,720))
            for _ in range(240):game.step(readback=False)
            scenery=Scenery(game)
            sprites=scenery.assets()
            self.assertGreaterEqual(len(sprites),5)
            self.assertTrue(all(image.getbbox() for _,image in sprites))
            cursor=scenery.sprites['cursor']
            image=dict(sprites)[cursor['id']]
            self.assertEqual(image.mode,'RGBa')
            self.assertEqual(image.getextrema()[-1],(0,255))
            self.assertTrue(0<=cursor['hotspotX']<image.width)
            self.assertTrue(0<=cursor['hotspotY']<image.height)
            planes=scenery.paper(1008)
            self.assertEqual([(identifier,image.size) for identifier,image in planes],[(2,(482,514))])
            self.assertEqual(scenery.paper(1008),[])
            self.assertEqual(scenery.paper(600),[])
            self.assertEqual((scenery.projection['width'],scenery.projection['height']),(480,512))
            self.assertEqual(planes[0][1].getextrema()[-1],(255,255))
