import unittest
from commerce import excluded, contains, blocked

class CommerceTests(unittest.TestCase):
    def test_purchase_buttons_are_hidden_but_game_sprites_remain(self):
        for path in ('store_L.png','localized/en/toyson_L.png','removeadson_L.png','storeiconon_L.png',
                     'popups/game-over-comic_L.png','popups/game-over-plush_X.png','store/get-more-coins_L.png','share_L.png',
                     'whatsnew_L.png','newspanel_L.png','localized/fr/whatsnewtext_X.png'):
            self.assertTrue(excluded(path),path)
        for path in ('monsters.png','options_L.png','play_L.png','platforms.png'):
            self.assertFalse(excluded(path),path)
    def test_hidden_button_cannot_be_activated(self):
        self.assertTrue(contains([(10,20,30,40)],20,30))
        self.assertFalse(contains([(10,20,30,40)],100,30))
        draws = [((10,20,30,40), True), ((0,25,100,50), False)]
        self.assertTrue(blocked(draws,20,22))
        self.assertFalse(blocked(draws,20,30), 'The original keyboard above a hidden shop button keeps its input')
