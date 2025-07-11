import unittest

from app.utils import markdown_to_html


class TestMarkdownToHtml(unittest.TestCase):

    def test_basic_markdown(self):
        """Test basic markdown formatting"""
        markdown = "**Bold** and *italic* text"
        result = markdown_to_html(markdown)
        self.assertEqual(result, "<p><strong>Bold</strong> and <em>italic</em> text</p>\n")

    def test_paragraphs(self):
        """Test paragraph formatting"""
        markdown = "First paragraph\n\nSecond paragraph"
        result = markdown_to_html(markdown)
        self.assertEqual(result, "<p>First paragraph</p>\n<p>Second paragraph</p>\n")

    def test_links(self):
        """Test links formatting"""
        markdown = "[Link text](https://example.com)"
        result = markdown_to_html(markdown)
        self.assertEqual(result,
                         '<p><a href="https://example.com" rel="nofollow ugc" target="_blank">Link text</a></p>\n')

    def test_code_blocks(self):
        """Test code blocks formatting"""
        markdown = "```\ncode block\n```"
        result = markdown_to_html(markdown)
        self.assertTrue("<pre><code>code block" in result)

    def test_blockquote(self):
        """Test blockquote formatting"""
        markdown = "> This is a quote"
        result = markdown_to_html(markdown)
        self.assertTrue("<blockquote>\n<p>This is a quote</p>\n</blockquote>" in result)

    def test_lists(self):
        """Test unordered and ordered lists"""
        markdown = "* Item 1\n* Item 2\n\n1. First\n2. Second"
        result = markdown_to_html(markdown)
        self.assertTrue("<ul>\n<li>Item 1</li>\n<li>Item 2</li>\n</ul>" in result)
        self.assertTrue("<ol>\n<li>First</li>\n<li>Second</li>\n</ol>" in result)

    def test_angle_brackets(self):
        """Test that angle brackets are properly escaped"""
        markdown = "Text with <tags> should be escaped"
        result = markdown_to_html(markdown)
        self.assertTrue("&lt;tags&gt;" in result)

    def test_angle_brackets_in_blockquote(self):
        """Test that angle brackets in blockquotes are properly escaped"""
        markdown = "> <Book Title and Volume> Review Goes Here [5/10]"
        result = markdown_to_html(markdown)
        self.assertTrue("&lt;Book Title and Volume&gt;" in result)

    def test_gt_lt_in_code(self):
        """Test usage of angle brackets in code block"""
        markdown = "Normal text `code block > something else` normal text again"
        result = markdown_to_html(markdown)
        self.assertEqual(result, "<p>Normal text <code>code block &gt; something else</code> normal text again</p>\n")

    def test_gt_lt_in_code_block(self):
        """Test usage of angle brackets in large code block"""
        markdown = "Normal text\n\n```\n<html>\n```\n\nnormal text again"
        result = markdown_to_html(markdown)
        self.assertEqual(result,
                         "<p>Normal text</p>\n<pre><code>&lt;html&gt;\n</code></pre>\n<p>normal text again</p>\n")

    def test_complex_markdown_with_angle_brackets(self):
        """Test a more complex markdown sample with angle brackets"""
        markdown = """What light novels have you read in the past week? Something good? Bad? Let us know about it.

And if you want to add your score to the database to help your fellow Bookworms find new reading materials you can use the following template:

><Book Title and Volume> Review Goes Here [5/10]
"""
        result = markdown_to_html(markdown)
        # print("Result: " + result)
        self.assertTrue("&lt;Book Title and Volume&gt;" in result)
        self.assertTrue("<blockquote>" in result)

    def test_disallowed_tags(self):
        """Test that disallowed tags are removed"""
        markdown = "Paragraph with <script>alert('xss')</script> script."
        result = markdown_to_html(markdown)
        self.assertEqual(result, "<p>Paragraph with &lt;script&gt;alert('xss')&lt;/script&gt; script.</p>\n")

    def test_double_bold(self):
        """Test a variety of cases where bold markdown has caused problems in the past"""
        markdown = "Two **bold** words in one **bold** sentence."
        result = markdown_to_html(markdown)
        correct_html = "<p>Two <strong>bold</strong> words in one <strong>bold</strong> sentence.</p>\n"
        self.assertEqual(result, correct_html)

        markdown = "Links with underscores still work: https://en.wikipedia.org/wiki/Rick_Astley"
        result = markdown_to_html(markdown)
        correct_html = (
            '<p>Links with underscores still work: <a href="https://en.wikipedia.org/wiki/Rick_Astley" '
            'rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Rick_Astley</a></p>\n')
        self.assertEqual(result, correct_html)

        markdown = "Double **bold** and ***italics* words** in *one* sentence."
        result = markdown_to_html(markdown)
        correct_html = ('<p>Double <strong>bold</strong> and <strong><em>italics</em> words</strong> in '
            '<em>one</em> sentence.</p>\n')
        self.assertEqual(result, correct_html)

        markdown = "Ignore `**bold**` words in code block with **bold** markdown."
        result = markdown_to_html(markdown)
        correct_html = '<p>Ignore <code>**bold**</code> words in code block with <strong>bold</strong> markdown.</p>\n'
        self.assertEqual(result, correct_html)

        markdown = "What about ignoring **bold** words inside a\n\n```\nfenced **code** block?\n```"
        result = markdown_to_html(markdown)
        correct_html = (
            '<p>What about ignoring <strong>bold</strong> words inside a</p>\n<pre><code>fenced **code** block?\n'
            '</code></pre>\n')
        self.assertEqual(result, correct_html)

        markdown = "[Bold in **part of** a link](https://en.wikipedia.org/wiki/Rick_Astley)"
        result = markdown_to_html(markdown)
        correct_html = (
            '<p><a href="https://en.wikipedia.org/wiki/Rick_Astley" rel="nofollow ugc" target="_blank">Bold in '
            '<strong>part of</strong> a link</a></p>\n')
        self.assertEqual(result, correct_html)


if __name__ == '__main__':
    unittest.main()
