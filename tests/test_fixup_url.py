import unittest

from app.utils import fixup_url

class TestFixupUrl(unittest.TestCase):

    def test_basic_usage(self):
        """Test basic usage of fixup_url"""
        thumbnail, embed = fixup_url('https://www.youtube.com/watch?v=t08dDBjzudQ')
        self.assertEqual(thumbnail, 'https://youtu.be/t08dDBjzudQ')
        self.assertEqual(embed, 'https://www.youtube.com/watch?v=t08dDBjzudQ')

    def test_playlist(self):
        """Test YouTube playlists"""
        thumbnail, embed = fixup_url('https://www.youtube.com/playlist?list=OLAK5uy_m0ymBxrjfVeJeuv9sde3pN_yvAfwWuxGw')
        self.assertEqual(thumbnail, '')
        self.assertEqual(embed, 'https://www.youtube.com/playlist?list=OLAK5uy_m0ymBxrjfVeJeuv9sde3pN_yvAfwWuxGw')

    def test_post(self):
        """Test youtube posts"""
        thumbnail, embed = fixup_url('https://www.youtube.com/post/abcdefg')
        self.assertEqual(thumbnail, '')
        self.assertEqual(embed, 'https://www.youtube.com/post/abcdefg')
