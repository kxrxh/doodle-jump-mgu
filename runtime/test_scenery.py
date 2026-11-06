from pathlib import Path
import unittest
from game import Game

class SceneryTests(unittest.TestCase):
    def test_original_art_is_published_once_and_paper_matches_game_scale(self):
        from scenery import Scenery
        import tempfile
        with tempfile.TemporaryDirectory() as data:
            game=Game(data=Path(data))
            for _ in range(240):game.step()
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
            self.assertEqual([(identifier,image.size) for identifier,image in planes],[(2,(1008,960)),(3,(1008,960))])
            self.assertEqual(scenery.paper(1008),[],'Static paper is uploaded once, not every frame')
            self.assertEqual(scenery.paper(600)[0][1].size,(600,960))
            self.assertEqual(planes[0][1].getextrema()[-1],(255,255))
