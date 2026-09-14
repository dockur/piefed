from datetime import timezone
from urllib.parse import urlsplit

from feedgen.feed import FeedGenerator
from app.utils import mimetype_from_url, is_video_hosting_site
# could be used later for instance checks: from app.models import User, Community, Post, PostReply

####################################################################################################
# this is intended to be upstreamed
from feedgen.ext.base import BaseExtension
from feedgen.util import xml_elem

SLASH_NS = 'http://purl.org/rss/1.0/modules/slash/'

def _set_value(entry, name, value):
    if value:
        newelem = xml_elem('{%s}' % SLASH_NS + name, entry)
        newelem.text = value

class SlashExtension(BaseExtension):
    def extend_ns(self):
        return {'slash': SLASH_NS}

class SlashEntryExtension(BaseExtension):
    def __init__(self):
        self._comments = None

    def extend_atom(self, entry):  # TODO
        return self.extend_rss(entry)

    def extend_rss(self, entry):
        if self._comments:
            _set_value(entry, 'comments', self._comments)
        return entry

    def comments(self, value):
        if type(value) is not int or value < 0:
            raise ValueError('Slash Extension: Invalid comment count')
        self._comments = str(value)
####################################################################################################


def _is_valid_xml_utf8(pystring):
    """Check if a string is like valid UTF-8 XML content."""
    if isinstance(pystring, str):
        pystring = pystring.encode('utf-8', errors='ignore')

    s = pystring
    c_end = len(s)
    i = 0

    while i < c_end - 2:
        if s[i] & 0x80:
            # Check for forbidden characters
            if i + 2 < c_end:
                next3 = (s[i] << 16) | (s[i + 1] << 8) | s[i + 2]
                # 0xefbfbe and 0xefbfbf are utf-8 encodings of forbidden characters \ufffe and \uffff
                if next3 == 0xefbfbe or next3 == 0xefbfbf:
                    return False
                # 0xeda080 and 0xedbfbf are utf-8 encodings of \ud800 and \udfff (surrogate blocks)
                if 0xeda080 <= next3 <= 0xedbfbf:
                    return False
        elif s[i] < 9 or s[i] == 11 or s[i] == 12 or (14 <= s[i] <= 31) or s[i] == 127:
            return False  # invalid ascii char
        i += 1

    while i < c_end:
        if not (s[i] & 0x80) and (s[i] < 9 or s[i] == 11 or s[i] == 12 or (14 <= s[i] <= 31) or s[i] == 127):
            return False  # invalid ascii char
        i += 1

    return True


class RSSFeed:
    """ A mixin of proxying FeedGenerator and dealing with the Post class/data model.
    Feed channel data goes into __init__, DB model and related stuff into method arguments.
    Can be easily adapted to handle comments (PostReply class) should those be syndicated some day.
    """

    def __init__(self, *,  title: str, link: str, description: str, logo: str|None = None, \
                   self_link: str|None = None, language: str|None = None):
        fg = FeedGenerator()
        fg.load_extension('media', rss=True)
        fg.register_extension('slash', SlashExtension, SlashEntryExtension)

        fg.title(title)
        fg.link(href=link, rel='alternate')
        fg.subtitle(description)
        if logo:
            fg.logo(logo)
        if self_link:
            fg.link(href=self_link, rel='self')
        if language:
            fg.language(language)

        self._fg = fg

    def create_feed(self, posts, server_url):
        # the reversed below is so that newer entries will be first in the feed wrt document order,
        # which depending on the RSS client can be quite useful.
        # However, this also depends on ordering done by the caller,
        # and ultimately on FeedGenerator itself, so it's a heuristic only.
        # list() because callers may pass a Query or other iterable which reversed() can't handle
        for post in reversed(list(posts)):
           self._add_post(post, self._fg, server_url)

        return self._fg.rss_str()

    @classmethod
    def _add_post(cls, post, feed, server_url):
         # Validate title and body - skip this post if invalid
        # TODO can also be fixed with RegExes, feedgen could take care of it?
        # @see https://www.w3.org/TR/REC-xml/#charsets
        if not _is_valid_xml_utf8(post.title.strip()):
            return
        content = post.body_html.strip() if post.body_html else None
        if content and not _is_valid_xml_utf8(content):
            return

        fe = feed.add_entry()
        fe.title(post.title.strip())
        link = f"{server_url}{post.slug}" if post.slug else f"{server_url}/post/{post.id}"
        fe.link(href=link)
        fe.guid(post.profile_id(), permalink=True)

        fe.content(content, type='CDATA')  # feedgen takes care if this is empty
        medium = cls._media_content(post.url, post.image)
        if medium:
            fe.media.content(medium)

        if post.author:
            # @see post.author.email
            fe.author(email=cls._email_from_public_url(post.author.ap_public_url))

        fe.pubDate(post.created_at.replace(tzinfo=timezone.utc))
        # reply_count_cross_posted is nullable and can drift negative, so fall back and clamp
        comment_count = post.reply_count_cross_posted
        if comment_count is None:
            comment_count = post.reply_count
        fe.slash.comments(max(0, comment_count or 0))

        if post.community:
            fe.category(term=post.community.name)
        for cat in set([flair.flair for flair in post.flair] + [tag.display_as for tag in post.tags]):
            if cat:
                fe.category(term=cat, scheme='flair/tag')

    @staticmethod
    def _email_from_public_url(url):
        comps = urlsplit(url)
        user = comps.path.replace('/u/', '', 1)
        return f"{user}@{comps.netloc}"

    @staticmethod
    def _media_content(url, image):
        """ Return a dictionary of fields for media:content, or None when that's not possible.

        For better user experience, we attach post.url even when its type is unknown.
        Since MIME type determination is tricky (*mimetype_from_url* uses the path suffix only,
        *mime_type_using_head* depends on the server and calling it on every feed generation
        introduces overhead),
        we use media:content instead of RSS'es enclosure since it mandates only a URL.

        We make best effort guesses to fill in other fields.

        :url    string, or None
        :image  File (from post.image), or None
        :return dict or None. The dictionary has fields intended for feedgen's media extension
        """
        if not url:
            return None
        medium = {'url': url}

        type = mimetype_from_url(url)
        if not type and is_video_hosting_site(url):
            type = "text/html"
        if type:
            medium['type'] = type

        if image and url == image.source_url:
            if not type:  # may be None, e.g. for lemmy's image_proxy URLs
                medium['medium'] = 'Image'
            size = image.filesize()
            if size > 0:
                medium['fileSize'] = str(size)

        return medium
