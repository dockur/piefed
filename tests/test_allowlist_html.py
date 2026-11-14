import unittest

from app.utils import allowlist_html, community_link_to_href, feed_link_to_href, person_link_to_href, markdown_to_html


class TestAllowlistHtml(unittest.TestCase):

    def test_basic_html(self):
        """Test basic HTML with allowed tags is preserved"""
        html = "<p>Basic paragraph</p>"
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, html)

    def test_disallowed_tags(self):
        """Test that disallowed tags are removed"""
        html = "<p>Paragraph with <script>alert('xss')</script> script</p>"
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, "<p>Paragraph with  script</p>")

    def test_nested_tags(self):
        """Test that allowed nested tags are preserved"""
        html = "<blockquote><p>Nested content</p></blockquote>"
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, html)

    def test_attributes(self):
        """Test that allowed attributes are preserved and others removed"""
        html = '<a href="https://example.com" onclick="alert(\'xss\')" style="color:red">Link</a>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, '<a href="https://example.com" rel="nofollow ugc" target="_blank">Link</a>')

    def test_empty_input(self):
        """Test empty input"""
        self.assertEqual(allowlist_html(""), "")
        self.assertEqual(allowlist_html(None), "")

    def test_plain_text_urls(self):
        """Test that plain text URLs are converted to links"""
        markdown = "Visit https://example.com for more info."
        result = allowlist_html(markdown_to_html(markdown, test_env={'fn_string': 'fn-test'}), test_env={'fn_string': 'fn-test'})
        self.assertEqual(result,
                         '<p>Visit <a href="https://example.com" rel="nofollow ugc" target="_blank">https://example.com</a> for more info.</p>\n')

    def test_angle_brackets_in_text(self):
        """Test that angle brackets in plain text are escaped"""
        html = "<p>Text with <Book Title and Volume> needs escaping</p>"
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        print(f"Original: {html}")
        print(f"Result: {result}")
        self.assertTrue("&lt;Book Title and Volume&gt;" in result)

    def test_angle_brackets_in_blockquote(self):
        """Test that angle brackets in blockquote are escaped"""
        html = "<blockquote><p><Book Title and Volume> Review Goes Here [5/10]</p></blockquote>"
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        print(f"Original: {html}")
        print(f"Result: {result}")
        self.assertTrue("&lt;Book Title and Volume&gt;" in result)
    
    def test_community_link_basic_html(self):
        """Test link creation of !community@instance.tld"""
        text = "!community@instance.tld"
        correct_html = '<a href="https://instance.tld/community/lookup/community/instance.tld">!community@instance.tld</a>'
        result = community_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_community_link_markdown_link(self):
        """
        Ignore link creation inside a markdown-created link

        Test input came from parsing markdown: [Link to !community@instance.tld is not here](https://other_site.tld)
        """
        text = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                'Link to !community@instance.tld is not here</a>')
        correct_html = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                        'Link to !community@instance.tld is not here</a>')
        result = community_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_community_link_code_block(self):
        """Ignore link creation if in a <code> block"""
        text = "<code>!community@instance.tld</code>"
        correct_html = "<code>!community@instance.tld</code>"
        result = community_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)

    def test_community_link_masto_link(self):
        """
        Ignore link creation if preceded by a / (mastodon links are sometimes like this, more often for people links)
        """
        text = "https://masto.tld/!community@instance.tld/12345"
        correct_html = "https://masto.tld/!community@instance.tld/12345"
        result = community_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_feed_link_basic_html(self):
        """Test link creation of ~feed@instance.tld"""
        text = "~feed@instance.tld"
        correct_html = '<a href="https://instance.tld/feed/lookup/feed/instance.tld">~feed@instance.tld</a>'
        result = feed_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_feed_link_markdown_link(self):
        """
        Ignore link creation inside a markdown-created link

        Test input came from parsing markdown: [Link to ~feed@instance.tld is not here](https://other_site.tld)
        """
        text = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                'Link to ~feed@instance.tld is not here</a>')
        correct_html = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                        'Link to ~feed@instance.tld is not here</a>')
        result = feed_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_feed_link_code_block(self):
        """Ignore link creation if in a <code> block"""
        text = "<code>~feed@instance.tld</code>"
        correct_html = "<code>~feed@instance.tld</code>"
        result = feed_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)

    def test_feed_link_masto_link(self):
        """
        Ignore link creation if preceded by a / (mastodon links are sometimes like this, more often for people links)
        """
        text = "https://masto.tld/~feed@instance.tld/12345"
        correct_html = "https://masto.tld/~feed@instance.tld/12345"
        result = feed_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_person_link_basic_html(self):
        """Test link creation of @person@instance.tld"""
        text = "@person@instance.tld"
        correct_html = ('<a href="https://instance.tld/user/lookup/person/instance.tld" '
                        'rel="nofollow noindex">@person@instance.tld</a>')
        result = person_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_person_link_markdown_link(self):
        """
        Ignore link creation inside a markdown-created link

        Test input came from parsing markdown: [Link to @person@instance.tld is not here](https://other_site.tld)
        """
        text = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                'Link to @person@instance.tld is not here</a>')
        correct_html = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                        'Link to @person@instance.tld is not here</a>')
        result = person_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_person_link_code_block(self):
        """Ignore link creation if in a <code> block"""
        text = "<code>@person@instance.tld</code>"
        correct_html = "<code>@person@instance.tld</code>"
        result = person_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)

    def test_person_link_masto_link(self):
        """
        Ignore link creation if preceded by a / (mastodon links are sometimes like this, more often for people links)
        """
        text = "https://masto.tld/@person@instance.tld/12345"
        correct_html = "https://masto.tld/@person@instance.tld/12345"
        result = person_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)

    def test_enhanced_image_width_attribute(self):
        """Test that width attribute on img tag is preserved"""
        html = '<img src="cat.jpg" alt="Cat" width="200px"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('width="200px"', result)
        self.assertIn('src="cat.jpg"', result)
        self.assertIn('alt="Cat"', result)

    def test_enhanced_image_height_attribute(self):
        """Test that height attribute on img tag is preserved"""
        html = '<img src="cat.jpg" alt="Cat" height="150px"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('height="150px"', result)
        self.assertIn('src="cat.jpg"', result)

    def test_enhanced_image_align_attribute(self):
        """Test that align attribute on img tag is preserved"""
        html = '<img src="cat.jpg" alt="Cat" align="left"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('align="left"', result)
        self.assertIn('src="cat.jpg"', result)

    def test_enhanced_image_all_attributes(self):
        """Test that all enhanced image attributes are preserved"""
        html = '<img src="cat.jpg" alt="Cat" width="200px" height="150px" align="right" class="thumbnail"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('width="200px"', result)
        self.assertIn('height="150px"', result)
        self.assertIn('align="right"', result)
        self.assertIn('class="thumbnail"', result)
        self.assertIn('src="cat.jpg"', result)
        self.assertIn('alt="Cat"', result)

    def test_enhanced_image_title_attribute(self):
        """Test that title attribute on img tag is preserved"""
        html = '<img src="cat.jpg" alt="Cat" title="A cute cat"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('title="A cute cat"', result)
        self.assertIn('src="cat.jpg"', result)

    def test_enhanced_image_with_anchor(self):
        """Test that thumbnail wrapped in anchor preserves all attributes"""
        html = '<a href="full.jpg"><img src="thumb.jpg" alt="Cat" width="200px" align="left"/></a>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('<a href="full.jpg"', result)
        self.assertIn('width="200px"', result)
        self.assertIn('align="left"', result)
        self.assertIn('src="thumb.jpg"', result)
        # Anchor should have nofollow and target
        self.assertIn('rel="nofollow ugc"', result)
        self.assertIn('target="_blank"', result)

    def test_enhanced_image_disallowed_attributes_removed(self):
        """Test that disallowed attributes on img are removed"""
        html = '<img src="cat.jpg" alt="Cat" width="200px" onclick="alert(\'xss\')" style="border:1px"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('width="200px"', result)
        self.assertIn('src="cat.jpg"', result)
        # Dangerous attributes should be removed
        self.assertNotIn('onclick', result)
        self.assertNotIn('style', result)

    def test_enhanced_image_loading_lazy_added(self):
        """Test that loading=lazy is added to images"""
        html = '<img src="cat.jpg" alt="Cat" width="200px"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('loading="lazy"', result)
        self.assertIn('width="200px"', result)

    def test_ordered_list_start_attribute(self):
        """Test that the start attribute is preserved in ordered lists if present."""
        html = '<ol start="2"><li>first item</li><li>second item</li></ol>'
        result = allowlist_html(html, test_env={"fn_string": "fn-test"})
        self.assertIn('start="2"', result)

    def test_table_align_attributes(self):
        """Test that style attributes are preserved in tables to allow for alignment."""
        html = '<table><thead><tr><th style="text-align:center;">Heading</th><th style="text-align:center;">Heading</th></tr></thead><tbody><tr><td style="text-align:center;">1</td><td style="text-align:center;">2</td></tr></tbody></table>'
        result = allowlist_html(html, test_env={"fn_string": "fn-test"})
        self.assertIn('style="text-align:center;"', result)


if __name__ == '__main__':
    unittest.main()
