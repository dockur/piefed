import unittest
from app.models import Post


class TestContentFilter(unittest.TestCase):

    content_filters = {'trump': ['trump'],
                       'elon': ['elon', 'musk']}

    def test_content_filter_basic(self):
        p = Post(title="Trump does something daft again", user_id=1)
        filtered = p.blocked_by_content_filter(self.content_filters, 2)
        assert filtered

    def test_content_filter_basic_same_author(self):
        p = Post(title="Trump does something daft again", user_id=1)
        filtered = p.blocked_by_content_filter(self.content_filters, 1)
        assert not filtered

    def test_content_filter_basic_not(self):
        p = Post(title="Turnip does something daft again", user_id=1)
        filtered = p.blocked_by_content_filter(self.content_filters, 2)
        assert not filtered

    def test_content_filter_square_brackets(self):
        p = Post(title="[Trump] does something daft again", user_id=1)
        filtered = p.blocked_by_content_filter(self.content_filters, 2)
        assert filtered


    def test_content_filter_square_bracket_filter(self):
        content_filters = {'trump': ['[Trump]'],
                           'elon': ['elon', 'musk']}
        p = Post(title="[Trump] does something daft again", user_id=1)
        filtered = p.blocked_by_content_filter(self.content_filters, 2)
        assert filtered



if __name__ == '__main__':
    unittest.main()