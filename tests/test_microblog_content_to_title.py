import unittest

from app.utils import microblog_content_to_link, microblog_content_to_title


class TestMicroblogContentToTitle(unittest.TestCase):

    def test_h1_plain_text(self):
        """A <h1> heading becomes the title, with no link"""
        title, link = microblog_content_to_title('<h1>Hello World</h1>')
        self.assertEqual(title, 'Hello World')
        self.assertEqual(link, '')

    def test_h1_with_link(self):
        """A link inside the <h1> is returned alongside the title"""
        title, link = microblog_content_to_title(
            '<h1><a href="https://example.com/page">My Great Heading</a></h1>')
        self.assertEqual(title, 'My Great Heading')
        self.assertEqual(link, 'https://example.com/page')

    def test_h1_too_short_is_ignored(self):
        """A <h1> shorter than 5 characters does not become a title"""
        title, link = microblog_content_to_title('<h1>Hi</h1>')
        self.assertEqual(title, '(content in post body)')
        self.assertEqual(link, '')

    def test_paragraph_trailing_period_stripped(self):
        """A paragraph's trailing period is dropped from the title"""
        title, link = microblog_content_to_title('<p>This is a paragraph of text.</p>')
        self.assertEqual(title, 'This is a paragraph of text')
        self.assertEqual(link, '')

    def test_paragraph_question_mark_kept(self):
        """The title ends at the first question mark, keeping the '?'"""
        title, link = microblog_content_to_title('<p>Is this a question? Yes it is.</p>')
        self.assertEqual(title, 'Is this a question?')
        self.assertEqual(link, '')

    def test_paragraph_exclamation_kept(self):
        """The title ends at the first exclamation mark, keeping the '!'"""
        title, link = microblog_content_to_title('<p>Wow this is great! More text here.</p>')
        self.assertEqual(title, 'Wow this is great!')
        self.assertEqual(link, '')

    def test_first_substantial_paragraph_used(self):
        """A short leading paragraph is skipped in favour of the first substantial one"""
        title, link = microblog_content_to_title('<p>abc</p><p>This is the real content here.</p>')
        self.assertEqual(title, 'This is the real content here')
        self.assertEqual(link, '')

    def test_plain_text_no_tags(self):
        """Content without recognised tags is used verbatim as the title"""
        title, link = microblog_content_to_title('Just some plain text without any tags')
        self.assertEqual(title, 'Just some plain text without any tags')
        self.assertEqual(link, '')

    def test_empty_string(self):
        """Empty content falls back to the placeholder title"""
        title, link = microblog_content_to_title('')
        self.assertEqual(title, '(content in post body)')
        self.assertEqual(link, '')

    def test_short_content_falls_back_to_placeholder(self):
        """Content shorter than 10 characters with no punctuation uses the placeholder"""
        title, link = microblog_content_to_title('<p>tiny</p>')
        self.assertEqual(title, '(content in post body)')
        self.assertEqual(link, '')

    def test_at_and_hash_markers_removed(self):
        """' @ ' and ' # ' markers are stripped when there is no sentence punctuation"""
        title, link = microblog_content_to_title(
            '<p>Email me @ home and tag # this please today friend</p>')
        self.assertEqual(title, 'Email mehome and tagthis please today friend')
        self.assertEqual(link, '')

    def test_long_title_is_truncated(self):
        """A long title (past 150 chars) is cut at a word boundary with an ellipsis"""
        html = '<p>' + ('word ' * 40) + 'final ending phrase here.' + '</p>'
        title, link = microblog_content_to_title(html)
        self.assertTrue(len(title) <= 153)
        self.assertTrue(title.endswith(' ...'))
        self.assertEqual(link, '')


class TestMicroblogContentToLink(unittest.TestCase):

    def test_returns_first_external_link(self):
        """A link whose host differs from the excluded host is returned"""
        link = microblog_content_to_link(
            '<p>see <a href="https://example.com/page">here</a></p>', 'mastodon.social')
        self.assertEqual(link, 'https://example.com/page')

    def test_excluded_host_is_skipped(self):
        """A link on the excluded host is not returned"""
        link = microblog_content_to_link(
            '<p>see <a href="https://example.com/page">here</a></p>', 'example.com')
        self.assertIsNone(link)

    def test_first_non_excluded_link_chosen(self):
        """Links on the excluded host are skipped in favour of the first external one"""
        link = microblog_content_to_link(
            '<p><a href="https://example.com/a">a</a> <a href="https://other.org/b">b</a></p>',
            'example.com')
        self.assertEqual(link, 'https://other.org/b')

    def test_no_links_returns_none(self):
        """Content with no links returns None"""
        link = microblog_content_to_link('<p>no links here</p>', 'example.com')
        self.assertIsNone(link)

    def test_relative_link_returned(self):
        """A relative link (no host) is not excluded and is returned"""
        link = microblog_content_to_link('<p><a href="/relative/path">rel</a></p>', 'example.com')
        self.assertEqual(link, '/relative/path')

    def test_real_content(self):
        """Use some real html from Mastodon"""
        html = """<p>So wonderful to dance in celebration of renaming <a href=\"https://mastodon.social/tags/Yellowknife\" class=\"mention hashtag\" rel=\"tag\">#<span>Yellowknife</span></a>&#39;s main street from Franklin Ave to Wiiliideh Ave, from the name of a white guy who spent a day or two here in 1820 to the name of this area that has been occupied by the now Yellowknives Dene First Nation for at least 8000 years. <a href=\"https://www.cbc.ca/news/canada/north/wiiliideh-avenue-9.7243325\" target=\"_blank\" rel=\"nofollow noopener\" translate=\"no\"><span class=\"invisible\">https://www.</span><span class=\"ellipsis\">cbc.ca/news/canada/north/wiili</span><span class=\"invisible\">ideh-avenue-9.7243325</span></a></p><p><a href=\"https://mastodon.social/tags/NationalIndigenousPeoplesDay\" class=\"mention hashtag\" rel=\"tag\">#<span>NationalIndigenousPeoplesDay</span></a> <a href=\"https://mastodon.social/tags/Canada\" class=\"mention hashtag\" rel=\"tag\">#<span>Canada</span></a> <a href=\"https://mastodon.social/tags/reconciliation\" class=\"mention hashtag\" rel=\"tag\">#<span>reconciliation</span></a> <a href=\"https://mastodon.social/tags/history\" class=\"mention hashtag\" rel=\"tag\">#<span>history</span></a></p>"""
        link = microblog_content_to_link(html, 'mastodon.social')
        self.assertEqual(link, 'https://www.cbc.ca/news/canada/north/wiiliideh-avenue-9.7243325')


if __name__ == '__main__':
    unittest.main()
