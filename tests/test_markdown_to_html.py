import unittest

from app.utils import markdown_to_html


class TestMarkdownToHtml(unittest.TestCase):

    def test_basic_markdown(self):
        """Test basic markdown formatting"""
        markdown = "**Bold** and *italic* text"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, "<p><strong>Bold</strong> and <em>italic</em> text</p>\n")

        markdown = "**Bold**, *italics*, __underscore bold__, and _underscore italics_, each next to a punctuation mark."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><strong>Bold</strong>, <em>italics</em>, <strong>underscore bold</strong>, and <em>underscore italics</em>, each next to a punctuation mark.</p>\n'
        self.assertEqual(target_html, result)

    def test_paragraphs(self):
        """Test paragraph formatting"""
        markdown = "First paragraph\n\nSecond paragraph"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, "<p>First paragraph</p>\n<p>Second paragraph</p>\n")

    def test_links(self):
        """Test links formatting"""
        markdown = "[Link text](https://example.com)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result,
                         '<p><a href="https://example.com" rel="nofollow ugc" target="_blank">Link text</a></p>\n')

    def test_links_w_periods(self):
        """Test links formatting with a period on the end"""
        markdown = "This is a test link https://pizza.com. Will it work?"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result,
                         '<p>This is a test link <a href="https://pizza.com" rel="nofollow ugc" target="_blank">https://pizza.com</a>. Will it work?</p>\n')
    
    def test_links_w_tilde(self):
        """Test links that have a tilde in them."""
        markdown = "This link has a tilde: https://site.tld/~user"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>This link has a tilde: <a href="https://site.tld/~user" rel="nofollow ugc" target="_blank">https://site.tld/~user</a></p>\n'
        self.assertEqual(result, correct_html)

    def test_links_no_http(self):
        """Test links that have no https:// on the front"""
        markdown = "My website www.nihilore.com"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>My website <a href="https://www.nihilore.com" rel="nofollow ugc" target="_blank">www.nihilore.com</a></p>\n'
        self.assertEqual(result, correct_html)
    
    def test_links_w_parens(self):
        """Test links with parentheses."""
        markdown = "https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p><a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a></p>\n'
        self.assertEqual(result, correct_html)

        markdown = "(https://en.wikipedia.org/wiki/Venison_Creek_(Ontario))"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>(<a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a>)</p>\n'
        self.assertEqual(result, correct_html)

        markdown = "(here is a link: https://en.wikipedia.org/wiki/Drosophila)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>(here is a link: <a href="https://en.wikipedia.org/wiki/Drosophila" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Drosophila</a>)</p>\n'
        self.assertEqual(result, correct_html)

        markdown = "(here is a link: https://en.wikipedia.org/wiki/Venison_Creek_(Ontario))"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>(here is a link: <a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a>)</p>\n'
        self.assertEqual(result, correct_html)

        # Link embedded in a sentence with trailing period
        markdown = "Check out https://en.wikipedia.org/wiki/Venison_Creek_(Ontario) for more info."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Check out <a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a> for more info.</p>\n'
        self.assertEqual(result, correct_html)

        # Link followed by bold text — next_sibling is a Tag, not a NavigableString (caused a crash before fix)
        markdown = "https://en.wikipedia.org/wiki/Venison_Creek_(Ontario) **bold text**"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p><a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a> <strong>bold text</strong></p>\n'
        self.assertEqual(result, correct_html)

        # Parenthetical sentence containing a link with parens in path
        markdown = "(See https://en.wikipedia.org/wiki/Venison_Creek_(Ontario) for details)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>(See <a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a> for details)</p>\n'
        self.assertEqual(result, correct_html)

        # Markdown-style named link with parens in URL
        markdown = "[Ontario Creek](https://en.wikipedia.org/wiki/Venison_Creek_(Ontario))"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p><a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">Ontario Creek</a></p>\n'
        self.assertEqual(result, correct_html)

        # Multiple links with parens in one sentence
        markdown = "Compare https://en.wikipedia.org/wiki/Venison_Creek_(Ontario) and https://en.wikipedia.org/wiki/Python_(programming_language) side by side."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Compare <a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a> and <a href="https://en.wikipedia.org/wiki/Python_(programming_language)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Python_(programming_language)</a> side by side.</p>\n'
        self.assertEqual(result, correct_html)

        # Link with parens followed by a comma
        markdown = "Visit https://en.wikipedia.org/wiki/Venison_Creek_(Ontario), then read more."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Visit <a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a>, then read more.</p>\n'
        self.assertEqual(result, correct_html)

    def test_code_blocks(self):
        """Test code blocks formatting"""
        markdown = "```\ncode block\n```"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("<pre><code>code block" in result)

    def test_blockquote(self):
        """Test blockquote formatting"""
        markdown = "> This is a quote"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("<blockquote>\n<p>This is a quote</p>\n</blockquote>" in result)
    
    def test_nested_blockquotes(self):
        """Test the handling of nesting blockquotes"""
        markdown = "> This is a quote\n> > This is a nested quote *with* __formatting__.\n> \n> Ending the quote."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<blockquote>\n<p>This is a quote</p>\n<blockquote>\n<p>This is a nested quote <em>with</em> <strong>formatting</strong>.</p>\n</blockquote>\n<p>Ending the quote.</p>\n</blockquote>\n'
        self.assertEqual(correct_html, result)
        
        markdown = "> This is a quote\n> \n> ::: spoiler Summary\n> > This is a **formatted** blockquote in a spoiler in a blockquote.\n> :::\n> \n> Ending the quote."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<blockquote>\n<p>This is a quote</p>\n<details><summary>Summary</summary><div class="spoiler_block symmetric">\n<blockquote>\n<p>This is a <strong>formatted</strong> blockquote in a spoiler in a blockquote.</p>\n</blockquote>\n</div></details>\n<p>Ending the quote.</p>\n</blockquote>\n'
        self.assertEqual(correct_html, result)

    def test_lists(self):
        """Test unordered and ordered lists"""
        markdown = "* Item 1\n* Item 2\n\n1. First\n2. Second"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("<ul>\n<li>Item 1</li>\n<li>Item 2</li>\n</ul>" in result)
        self.assertTrue("<ol>\n<li>First</li>\n<li>Second</li>\n</ol>" in result)

        markdown = "Line of text:\n- first bullet\n- second bullet"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Line of text:</p>\n<ul>\n<li>first bullet</li>\n<li>second bullet</li>\n</ul>\n'
        self.assertEqual(correct_html, result)

        markdown = "Line of text:\n1. first item\n2. second item"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Line of text:</p>\n<ol>\n<li>first item</li>\n<li>second item</li>\n</ol>\n'
        self.assertEqual(correct_html, result)

    def test_javascript_links(self):
        """Test that bad links are nuked"""
        markdown = "here is some text [click](javascript:alert(1)) here is some more text"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("javascript" not in result)

    def test_angle_brackets(self):
        """Test that angle brackets are properly escaped"""
        markdown = "Text with <tags> should be escaped"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("&lt;tags&gt;" in result)

    def test_angle_brackets_in_blockquote(self):
        """Test that angle brackets in blockquotes are properly escaped"""
        markdown = "> <Book Title and Volume> Review Goes Here [5/10]"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("&lt;Book Title and Volume&gt;" in result)

        markdown = "This is an emoticon with an angled bracket :<\n\nThis is a paragraph\n\n> This is a block quote."
        correct_html = '<p>This is an emoticon with an angled bracket :&lt;</p>\n<p>This is a paragraph</p>\n<blockquote>\n<p>This is a block quote.</p>\n</blockquote>\n'
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(correct_html, result)

    def test_gt_lt_in_code(self):
        """Test usage of angle brackets in code block"""
        markdown = "Normal text `code block > something else` normal text again"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, "<p>Normal text <code>code block &gt; something else</code> normal text again</p>\n")

    def test_gt_lt_in_code_block(self):
        """Test usage of angle brackets in large code block"""
        markdown = "Normal text\n\n```\n<html>\n```\n\nnormal text again"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result,
                         "<p>Normal text</p>\n<pre><code>&lt;html&gt;\n</code></pre>\n<p>normal text again</p>\n")

    def test_complex_markdown_with_angle_brackets(self):
        """Test a more complex markdown sample with angle brackets"""
        markdown = """What light novels have you read in the past week? Something good? Bad? Let us know about it.

And if you want to add your score to the database to help your fellow Bookworms find new reading materials you can use the following template:

><Book Title and Volume> Review Goes Here [5/10]
"""
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("&lt;Book Title and Volume&gt;" in result)
        self.assertTrue("<blockquote>" in result)

    def test_disallowed_tags(self):
        """Test that disallowed tags are removed"""
        markdown = "Paragraph with <script>alert('xss')</script> script."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, "<p>Paragraph with &lt;script&gt;alert(’xss’)&lt;/script&gt; script.</p>\n")
    
    def test_double_bold(self):
        """Test a variety of cases where bold markdown has caused problems in the past"""
        markdown = "Two **bold** words in one **bold** sentence."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = "<p>Two <strong>bold</strong> words in one <strong>bold</strong> sentence.</p>\n"
        self.assertEqual(result, correct_html)

        markdown = "Links with underscores still work: https://en.wikipedia.org/wiki/Rick_Astley"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = (
            '<p>Links with underscores still work: <a href="https://en.wikipedia.org/wiki/Rick_Astley" '
            'rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Rick_Astley</a></p>\n')
        self.assertEqual(result, correct_html)

        markdown = "Double **bold** and ***italics* words** in *one* sentence."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = ('<p>Double <strong>bold</strong> and <strong><em>italics</em> words</strong> in '
            '<em>one</em> sentence.</p>\n')
        self.assertEqual(result, correct_html)

        markdown = "Ignore `**bold**` words in code block with **bold** markdown."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Ignore <code>**bold**</code> words in code block with <strong>bold</strong> markdown.</p>\n'
        self.assertEqual(result, correct_html)

        markdown = "What about ignoring **bold** words inside a\n\n```\nfenced **code** block?\n```"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = (
            '<p>What about ignoring <strong>bold</strong> words inside a</p>\n<pre><code>fenced **code** block?\n'
            '</code></pre>\n')
        self.assertEqual(result, correct_html)

        markdown = "[Bold in **part of** a link](https://en.wikipedia.org/wiki/Rick_Astley)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = (
            '<p><a href="https://en.wikipedia.org/wiki/Rick_Astley" rel="nofollow ugc" target="_blank">Bold in '
            '<strong>part of</strong> a link</a></p>\n')
        self.assertEqual(result, correct_html)

    def test_strikethrough_in_inline_code(self):
        """Don't strikethrough text in inline code."""
        markdown = "`don't ~~strikethrough~~`"
        correct_html = "<p><code>don't ~~strikethrough~~</code></p>\n"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, correct_html)
    
    def test_strikethrough_in_fenced_code(self):
        """Don't strikethrough text in fenced code block."""
        markdown = "```\ndon't ~~strikethrough~~\n```"
        correct_html = "<pre><code>don't ~~strikethrough~~\n</code></pre>\n"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, correct_html)
    
    def test_spoiler_in_fenced_code(self):
        """Don't format spoiler block in fenced code block."""
        markdown = "```\n::: spoiler Spoiler Title\ndon't ~~strikethrough~~\n:::\n```"
        correct_html = "<pre><code>::: spoiler Spoiler Title\ndon't ~~strikethrough~~\n:::\n</code></pre>\n"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, correct_html)

    def test_code_block_link(self):
        """Test code blocks formatting containing a link"""

        markdown = "```\ncode block with link: https://example.com/ \n```"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual("<pre><code>code block with link: https://example.com/ \n</code></pre>\n", result)
    
    def test_en_dash(self):
        """Test converting -- to an en dash"""

        markdown = "Using--an en dash."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual("<p>Using–an en dash.</p>\n", result)
    
    def test_em_dash(self):
        """Test converting --- to an em dash"""

        markdown = "Em-dashes are fairly idiosyncratic---strange, really---to use regularly."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual("<p>Em-dashes are fairly idiosyncratic—strange, really—to use regularly.</p>\n", result)
    
    def test_em_dash_hr(self):
        """Test converting --- to an em dash while also having a horizontal rule"""

        markdown = "Writing em---dashes is\n\n---\n\nkind of annoying"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p>Writing em—dashes is</p>\n<hr/>\n<p>kind of annoying</p>\n', result)
    
    def test_ellipsis(self):
        """Test converting ... to an ellipsis character"""

        markdown = "Thinking about an ellipsis..."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual("<p>Thinking about an ellipsis…</p>\n", result)
    
    def test_ignore_smartypants_inline_code(self):
        """Test that checks en- and em-dashes as well as ellipses are not in inline code"""

        markdown = "No `en--dash`, nor `em---dash`, nor `ellipsis...` here."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(
            '<p>No <code>en--dash</code>, nor <code>em---dash</code>, nor <code>ellipsis...</code> here.</p>\n', result)
    
    def test_ignore_smartypants_code_block(self):
        """Test that checks en- and em-dashes as well as ellipses are not in code blocks"""

        markdown = "```\nNo en--dash, nor em---dash, nor ellipsis... here.\n```"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual("<pre><code>No en--dash, nor em---dash, nor ellipsis... here.\n</code></pre>\n", result)
    
    def test_bracketed_links(self):
        """Test that urls in angle brackets are turned into links"""

        markdown = "<https://piefed.social>"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(
            '<p><a href="https://piefed.social" rel="nofollow ugc" target="_blank">https://piefed.social</a></p>\n',
            result)
    
    def test_bracketed_links_inline_code(self):
        """Test that bracketed links are ignored in inline code"""

        markdown = "`<https://piefed.social>`"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p><code>&lt;https://piefed.social&gt;</code></p>\n', result)
    
    def test_bracketed_links_code_block(self):
        """Test that bracketed links are ignored in code blocks"""

        markdown = "```\n<https://piefed.social>\n```"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<pre><code>&lt;https://piefed.social&gt;\n</code></pre>\n', result)
    
    def test_lemmy_autocomplete_community(self):
        """Test that lemmy-formatted autocomplete community names drop the markdown link formatting"""

        markdown = "[!community@instance.tld](https://instance.tld/c/community)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p>!community@instance.tld</p>\n', result)
    
    def test_lemmy_autocomplete_person(self):
        """Test that lemmy-formatted autocomplete person names drop the markdown link formatting"""

        markdown = "[@user@instance.tld](https://instance.tld/u/user)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p>@user@instance.tld</p>\n', result)
    
    def test_lemmy_autocomplete_feed(self):
        """
        Test that lemmy-formatted autocomplete feed names drop the markdown link formatting. Note that lemmy does not
        currently have autocomplete for feeds, this is really just a means to keep formatting consistent with people
        and communities.
        """

        markdown = "[~feed@instance.tld](https://instance.tld/f/feed)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p>~feed@instance.tld</p>\n', result)

    def test_lemmy_autocomplete_multiple_links(self):
        """Test that multiple lemmy autocomplete links are all handled correctly"""

        markdown = "Check out [!community@instance.tld](https://instance.tld/c/community) and [@user@instance.tld](https://instance.tld/u/user)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p>Check out !community@instance.tld and @user@instance.tld</p>\n', result)
    
    def test_footnotes(self):
        """Test the footnotes extra"""

        # Testing basic functionality
        markdown = "This is a paragraph with a footnote[^1].\n\n[^1]: This is the footnote."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>This is a paragraph with a footnote<sup class="footnote-ref" id="fnref-1-fn-test"><a href="#fn-1-fn-test">1</a></sup>.</p>\n<div class="footnotes">\n<hr/>\n<ol>\n<li id="fn-1-fn-test">\n<p>This is the footnote.\xa0<a class="footnoteBackLink" href="#fnref-1-fn-test">↩</a></p>\n</li>\n</ol>\n</div>\n'
        self.assertEqual(target_html, result)

        # Testing multiple footnotes with different names
        markdown = "Here is a footnote ref[^1]. Here is another[^note].\n\n[^1]: First footnote\n\n[^note]: Second footnote"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>Here is a footnote ref<sup class="footnote-ref" id="fnref-1-fn-test"><a href="#fn-1-fn-test">1</a></sup>. Here is another<sup class="footnote-ref" id="fnref-note-fn-test"><a href="#fn-note-fn-test">2</a></sup>.</p>\n<div class="footnotes">\n<hr/>\n<ol>\n<li id="fn-1-fn-test">\n<p>First footnote\xa0<a class="footnoteBackLink" href="#fnref-1-fn-test">↩</a></p>\n</li>\n<li id="fn-note-fn-test">\n<p>Second footnote\xa0<a class="footnoteBackLink" href="#fnref-note-fn-test">↩</a></p>\n</li>\n</ol>\n</div>\n'
        self.assertEqual(target_html, result)

        # Testing multiline footnote with formatting
        markdown = "Here is a footnote ref[^1].\n\n[^1]:\n    indented *line*\n    **formatted** line with `code` and || spoilers ||"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>Here is a footnote ref<sup class="footnote-ref" id="fnref-1-fn-test"><a href="#fn-1-fn-test">1</a></sup>.</p>\n<div class="footnotes">\n<hr/>\n<ol>\n<li id="fn-1-fn-test">\n<p>indented <em>line</em>\n<strong>formatted</strong> line with <code>code</code> and <tg-spoiler>spoilers</tg-spoiler>\xa0<a class="footnoteBackLink" href="#fnref-1-fn-test">↩</a></p>\n</li>\n</ol>\n</div>\n'
        self.assertEqual(target_html, result)
    
    def test_double_underscore_bold(self):
        """Test using double underscores to signify bold"""

        # Basic functionality
        markdown = "Here is a __bold__ word."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>Here is a <strong>bold</strong> word.</p>\n'
        self.assertEqual(target_html, result)

        # Multiple places in a sentence
        markdown = "Here are __two__ bold __words__."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>Here are <strong>two</strong> bold <strong>words</strong>.</p>\n'
        self.assertEqual(target_html, result)

        # Bold and italics
        markdown = "***This*** is ___bold and italics___."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><em><strong>This</strong></em> is <em><strong>bold and italics</strong></em>.</p>\n'
        self.assertEqual(target_html, result)
    
    def test_spoiler_blocks(self):
        """Test various functionality with spoiler blocks."""

        # Basic functionality
        markdown = "::: spoiler Summary\nThis is a spoiler.\n:::"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<details><summary>Summary</summary><div class="spoiler_block symmetric">\n<p>This is a spoiler.</p>\n</div></details>\n'
        self.assertEqual(target_html, result)

        # Naked spoiler
        markdown = "::: spoiler\nThis is a spoiler.\n:::"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<details><summary>Spoiler</summary><div class="spoiler_block symmetric">\n<p>This is a spoiler.</p>\n</div></details>\n'
        self.assertEqual(target_html, result)

        # Bulleted list immediately after spoiler opening
        markdown = "::: spoiler Summary\n- one\n- two\n:::"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<details><summary>Summary</summary><div class="spoiler_block symmetric">\n<ul>\n<li>one</li>\n<li>two</li>\n</ul>\n</div></details>\n'
        self.assertEqual(target_html, result)

        # Nested spoilers
        markdown = "::: spoiler First Summary\n::: spoiler Second Summary\nSpoiler content\n:::\n:::"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<details><summary>First Summary</summary><div class="spoiler_block symmetric">\n<details><summary>Second Summary</summary><div class="spoiler_block symmetric">\n<p>Spoiler content</p>\n</div></details>\n</div></details>\n'
        self.assertEqual(target_html, result)

        # Asymmetric spoiler formatting, fallback to old spoiler block behavior
        markdown = "::: spoiler Summary 1\n::: spoiler Summary 2\nThis is a spoiler with no closing\n:::\n"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><details><summary>Summary 1</summary><div class="spoiler_block"><p>\n</p></div></details> spoiler Summary 2</p>\n<p>This is a spoiler with no closing</p>\n<p>:::</p>\n'
        self.assertEqual(target_html, result)
    
    def test_video_embeds(self):
        """Tests embedded video markdown."""

        # mp4 video
        markdown = "test ![alt text here](https://site.tld/video.mp4)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>test <video class="responsive-video" controls="" loop="" muted="" playsinline="" preload="metadata"><source src="https://site.tld/video.mp4" type="video/mp4"/> Your browser does not support playing HTML5 video. <a href="https://site.tld/video.mp4" rel="nofollow ugc" target="_blank">You can download a copy of the file instead.</a> Here is a description of the content: alt text here</video></p>\n'
        self.assertEqual(target_html, result)

        # webm video
        markdown = "test ![alt text here](https://site.tld/video.webm)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>test <video class="responsive-video" controls="" loop="" muted="" playsinline="" preload="metadata"><source src="https://site.tld/video.webm" type="video/webm"/> Your browser does not support playing HTML5 video. <a href="https://site.tld/video.webm" rel="nofollow ugc" target="_blank">You can download a copy of the file instead.</a> Here is a description of the content: alt text here</video></p>\n'
        self.assertEqual(target_html, result)

        # other, unsupported video, just treat it like any other image markdown
        markdown = "test ![alt text here](https://site.tld/video.mov)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>test <img alt="alt text here" loading="lazy" src="https://site.tld/video.mov"/></p>\n'
        self.assertEqual(target_html, result)

        # make sure images still work right
        markdown = "test ![alt text here](https://site.tld/image.png)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>test <img alt="alt text here" loading="lazy" src="https://site.tld/image.png"/></p>\n'
        self.assertEqual(target_html, result)
    
    def test_inline_spoilers(self):
        """Tests inline spoiler functionality."""

        # Basic functionality
        # telegram/discord format: || like this ||
        markdown = "|| spoiler here ||"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # reddit format: >! like this !<
        markdown = ">! spoiler here !<"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # with no space after spoiler delineator
        # telegram/discord:
        markdown = "||spoiler here||"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # reddit:
        markdown = ">!spoiler here!<"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # Multiple occurrences
        # telegram/discord:
        markdown = "|| spoiler here || and || another ||"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler> and <tg-spoiler>another</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # reddit:
        markdown = ">! spoiler here !< and >! another !<"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler> and <tg-spoiler>another</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # Mixed formats
        markdown = "|| spoiler here || and >! another !<"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler> and <tg-spoiler>another</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)


if __name__ == '__main__':
    unittest.main()
