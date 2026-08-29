# -*- coding: utf-8 -*-
#
# Copyright 2026
#

'''
Extends FeedGenerator to support Slash comments for RSS

See below for details
https://www.rssboard.org/rss-profile#namespace-elements-slash-comments
http://purl.org/rss/1.0/modules/slash/
'''

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

    def extend_rss(self, entry):
        if self._comments:
            _set_value(entry, 'comments', self._comments)

    def comments(self, value):
        if type(value) is not int or value < 0:
            raise ValueError('Invalid comment count')
        self._comments = str(value)
