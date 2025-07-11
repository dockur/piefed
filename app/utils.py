from __future__ import annotations

import base64
import bisect
import hashlib
import mimetypes
import math
import random
import urllib
import warnings
from collections import defaultdict, OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, date
from functools import wraps, lru_cache
from json import JSONDecodeError
from time import sleep
from typing import List, Tuple, Optional
from urllib.parse import urlparse, parse_qs, urlencode
from zoneinfo import available_timezones

import flask
import httpx
import jwt
import markdown2
import redis
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
import os
from furl import furl
from flask import current_app, json, redirect, url_for, request, make_response, Response, g, flash, abort
from flask_babel import _, lazy_gettext as _l
from flask_login import current_user, logout_user
from flask_wtf.csrf import validate_csrf
from sqlalchemy import text, or_, desc, asc, event
from sqlalchemy.orm import Session
from wtforms.fields import SelectMultipleField, StringField
from wtforms.widgets import ListWidget, CheckboxInput, TextInput
from wtforms.validators import ValidationError
from markupsafe import Markup
import boto3
from app import db, cache, httpx_client, celery
from app.constants import *
import re
from PIL import Image, ImageOps

from captcha.audio import AudioCaptcha
from captcha.image import ImageCaptcha

from app.models import Settings, Domain, Instance, BannedInstances, User, Community, DomainBlock, IpBan, \
    Site, Post, utcnow, Filter, CommunityMember, InstanceBlock, CommunityBan, Topic, UserBlock, Language, \
    File, ModLog, CommunityBlock, Feed, FeedMember, CommunityFlair, CommunityJoinRequest, Notification, UserNote


# Flask's render_template function, with support for themes added
def render_template(template_name: str, **context) -> Response:
    theme = current_theme()
    if theme != '' and os.path.exists(f'app/templates/themes/{theme}/{template_name}'):
        content = flask.render_template(f'themes/{theme}/{template_name}', **context)
    else:
        content = flask.render_template(template_name, **context)

    # Browser caching using ETags and Cache-Control
    resp = make_response(content)
    if current_user.is_anonymous:
        if 'etag' in context:
            resp.headers.add_header('ETag', context['etag'])
        resp.headers.add_header('Cache-Control', 'no-cache, max-age=600, must-revalidate')
    return resp


def request_etag_matches(etag):
    if 'If-None-Match' in request.headers:
        old_etag = request.headers['If-None-Match']
        return old_etag == etag
    return False


def return_304(etag, content_type=None):
    resp = make_response('', 304)
    resp.headers.add_header('ETag', request.headers['If-None-Match'])
    resp.headers.add_header('Cache-Control', 'no-cache, max-age=600, must-revalidate')
    resp.headers.add_header('Vary', 'Accept, Cookie, Accept-Language')
    if content_type:
        resp.headers.set('Content-Type', content_type)
    return resp


# Jinja: when a file was modified. Useful for cache-busting
def getmtime(filename):
    if os.path.exists('app/static/' + filename):
        return os.path.getmtime('app/static/' + filename)


# do a GET request to a uri, return the result
def get_request(uri, params=None, headers=None) -> httpx.Response:
    timeout = 15 if 'washingtonpost.com' in uri else 10  # Washington Post is really slow on og:image for some reason
    if headers is None:
        headers = {'User-Agent': f'PieFed/{current_app.config["VERSION"]}; +https://{current_app.config["SERVER_NAME"]}'}
    else:
        headers.update({'User-Agent': f'PieFed/{current_app.config["VERSION"]}; +https://{current_app.config["SERVER_NAME"]}'})
    if params and '/webfinger' in uri:
        payload_str = urllib.parse.urlencode(params, safe=':@')
    else:
        payload_str = urllib.parse.urlencode(params) if params else None
    try:
        response = httpx_client.get(uri, params=payload_str, headers=headers, timeout=timeout, follow_redirects=True)
    except ValueError as ex:
        # Convert to a more generic error we handle
        raise httpx.HTTPError(f"HTTPError: {str(ex)}") from None
    except httpx.ReadError as connection_error:
        try:  # retry, this time with a longer timeout
            sleep(random.randint(3, 10))
            response = httpx_client.get(uri, params=payload_str, headers=headers, timeout=timeout * 2,
                                        follow_redirects=True)
        except Exception as e:
            current_app.logger.info(f"{uri} {connection_error}")
            raise httpx_client.ReadError(f"HTTPReadError: {str(e)}") from connection_error
    except httpx.HTTPError as read_timeout:
        try:  # retry, this time with a longer timeout
            sleep(random.randint(3, 10))
            response = httpx_client.get(uri, params=payload_str, headers=headers, timeout=timeout * 2,
                                        follow_redirects=True)
        except Exception as e:
            current_app.logger.info(f"{uri} {read_timeout}")
            raise httpx.HTTPError(f"HTTPError: {str(e)}") from read_timeout

    return response


# Same as get_request except updates instance on failure and does not raise any exceptions
def get_request_instance(uri, instance: Instance, params=None, headers=None) -> httpx.Response:
    try:
        return get_request(uri, params, headers)
    except:
        instance.failures += 1
        instance.update_dormant_gone()
        db.session.commit()
        return httpx.Response(status_code=500)


# do a HEAD request to a uri, return the result
def head_request(uri, params=None, headers=None) -> httpx.Response:
    if headers is None:
        headers = {'User-Agent': f'PieFed/{current_app.config["VERSION"]}; +https://{current_app.config["SERVER_NAME"]}'}
    else:
        headers.update({'User-Agent': f'PieFed/{current_app.config["VERSION"]}; +https://{current_app.config["SERVER_NAME"]}'})
    try:
        response = httpx_client.head(uri, params=params, headers=headers, timeout=5, allow_redirects=True)
    except httpx.HTTPError as er:
        current_app.logger.info(f"{uri} {er}")
        raise httpx.HTTPError(f"HTTPError: {str(er)}") from er

    return response


# Saves an arbitrary object into a persistent key-value store. cached.
# Similar to g.site.* except g.site.* is populated on every single page load so g.site is best for settings that are
# accessed very often (e.g. every page load)
@cache.memoize(timeout=50)
def get_setting(name: str, default=None):
    setting = Settings.query.filter_by(name=name).first()
    if setting is None:
        return default
    else:
        try:
            return json.loads(setting.value)
        except JSONDecodeError:
            return default


# retrieves arbitrary object from persistent key-value store
def set_setting(name: str, value):
    setting = Settings.query.filter_by(name=name).first()
    if setting is None:
        db.session.add(Settings(name=name, value=json.dumps(value)))
    else:
        setting.value = json.dumps(value)
    db.session.commit()
    cache.delete_memoized(get_setting)


# Return the contents of a file as a string. Inspired by PHP's function of the same name.
def file_get_contents(filename):
    with open(filename, 'r') as file:
        contents = file.read()
    return contents


random_chars = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'


def gibberish(length: int = 10) -> str:
    return "".join([random.choice(random_chars) for x in range(length)])


# used by @cache.cached() for home page and post caching
def make_cache_key(sort=None, post_id=None, view_filter=None):
    if current_user.is_anonymous:
        return f'{request.url}_{sort}_{post_id}_anon_{request.headers.get("Accept")}_{request.headers.get("Accept-Language")}'  # The Accept header differentiates between activitypub requests and everything else
    else:
        return f'{request.url}_{sort}_{post_id}_user_{current_user.id}'


def is_image_url(url):
    common_image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.avif', '.svg+xml',
                               '.svg+xml; charset=utf-8']
    mime_type = mime_type_using_head(url)
    if mime_type:
        mime_type_parts = mime_type.split('/')
        return f'.{mime_type_parts[1]}' in common_image_extensions
    else:
        parsed_url = urlparse(url)
        path = parsed_url.path.lower()
        return any(path.endswith(extension) for extension in common_image_extensions)


def is_local_image_url(url):
    if not is_image_url(url):
        return False
    f = furl(url)
    return f.host in ["127.0.0.1", current_app.config["SERVER_NAME"], current_app.config['S3_PUBLIC_URL']]


def is_video_url(url: str) -> bool:
    common_video_extensions = ['.mp4', '.webm']
    mime_type = mime_type_using_head(url)
    if mime_type:
        mime_type_parts = mime_type.split('/')
        return f'.{mime_type_parts[1]}' in common_video_extensions
    else:
        parsed_url = urlparse(url)
        path = parsed_url.path.lower()
        return any(path.endswith(extension) for extension in common_video_extensions)


def is_video_hosting_site(url: str) -> bool:
    if url is None or url == '':
        return False
    video_hosting_sites = ['https://youtube.com', 'https://www.youtube.com', 'https://youtu.be',
                           'https://www.vimeo.com', 'https://vimeo.com', 'https://streamable.com',
                           'https://www.redgifs.com/watch/']
    for starts_with in video_hosting_sites:
        if url.startswith(starts_with):
            return True

    if 'videos/watch' in url:  # PeerTube
        return True

    return False


@cache.memoize(timeout=10)
def mime_type_using_head(url):
    # Find the mime type of a url by doing a HEAD request - this is the same as GET except only the HTTP headers are transferred
    try:
        response = httpx_client.head(url, timeout=5)
        response.raise_for_status()  # Raise an exception for HTTP errors
        content_type = response.headers.get('Content-Type')
        if content_type:
            if content_type == 'application/octet-stream':
                return ''
            return content_type
        else:
            return ''
    except httpx.HTTPError:
        return ''


allowed_tags = ['p', 'strong', 'a', 'ul', 'ol', 'li', 'em', 'blockquote', 'cite', 'br', 'h1', 'h2', 'h3', 'h4', 'h5',
                'h6', 'pre',
                'code', 'img', 'details', 'summary', 'table', 'tr', 'td', 'th', 'tbody', 'thead', 'hr', 'span', 'small',
                'sub', 'sup',
                's', 'tg-spoiler', 'ruby', 'rt', 'rp']


# sanitise HTML using an allow list
def allowlist_html(html: str, a_target='_blank') -> str:
    # RUN THE TESTS in tests/test_allowlist_html.py whenever you alter this function, it's fragile and bugs are hard to spot.
    if html is None or html == '':
        return ''

    # Pre-escape angle brackets that aren't valid HTML tags before BeautifulSoup parsing
    # We need to distinguish between:
    # 1. Valid HTML tags (allowed or disallowed) - let BeautifulSoup handle them
    # 2. Invalid/non-HTML content in angle brackets - escape them
    def escape_non_html_brackets(match):
        tag_content = match.group(1).strip().lower()
        # Handle closing tags by removing the leading slash before extracting tag name
        if tag_content.startswith('/'):
            tag_name = tag_content[1:].split()[0]
        else:
            tag_name = tag_content.split()[0]
        
        # Check if this looks like a valid HTML tag (allowed or not)
        # Valid HTML tags have specific patterns
        html_tags = ['a', 'abbr', 'acronym', 'address', 'area', 'article', 'aside', 'audio', 'b', 'bdi', 'bdo', 'big',
                     'blockquote', 'body', 'br', 'button', 'canvas', 'caption', 'center', 'cite', 'code', 'col',
                     'colgroup', 'data', 'datalist', 'dd', 'del', 'details', 'dfn', 'dialog', 'dir', 'div', 'dl', 'dt',
                     'em', 'embed', 'fieldset', 'figcaption', 'figure', 'font', 'footer', 'form', 'frame', 'frameset',
                     'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'head', 'header', 'hr', 'html', 'i', 'iframe', 'img', 'input',
                     'ins', 'kbd', 'label', 'legend', 'li', 'link', 'main', 'map', 'mark', 'meta', 'meter', 'nav',
                     'noframes', 'noscript', 'object', 'ol', 'optgroup', 'option', 'output', 'p', 'param', 'picture',
                     'pre', 'progress', 'q', 'rp', 'rt', 'ruby', 's', 'samp', 'script', 'section', 'select', 'small',
                     'source', 'span', 'strike', 'strong', 'style', 'sub', 'summary', 'sup', 'svg', 'table', 'tbody',
                     'tg-spoiler', 'td', 'template', 'textarea', 'tfoot', 'th', 'thead', 'time', 'title', 'tr', 'track',
                     'tt', 'u', 'ul', 'var', 'video', 'wbr']
        
        if tag_name in html_tags:
            # This is a valid HTML tag - let BeautifulSoup handle it (it will remove if not allowed)
            return match.group(0)
        else:
            # This doesn't look like a valid HTML tag - escape it
            return f"&lt;{match.group(1)}&gt;"
    
    html = re.sub(r'<([^<>]+?)>', escape_non_html_brackets, html)

    # Parse the HTML using BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')

    # Find all plain text links, convert to <a> tags
    re_url = re.compile(r'(http[s]?://[!-~]+)')  # http(s):// followed by chars in ASCII range 33 to 126
    for tag in soup.find_all(text=True):
        tags = []
        url = False
        for t in re_url.split(tag.string):
            if re_url.match(t):
                # Avoid picking up trailing punctuation for raw URLs in text
                href = t[:-1] if t[-1] in ['.', ',', '!', ':', ';', '?'] else t
                if not '(' in t:
                    href = t[:-1] if t[-1] == ')' else t
                a = soup.new_tag("a", href=href)
                a.string = href
                tags.append(a)
                if href != t:
                    tags.append(t[-1])
                url = True
            else:
                tags.append(t)
        if url:
            for t in tags:
                tag.insert_before(t)
            tag.extract()

    # Filter tags, leaving only safe ones
    for tag in soup.find_all():
        # If the tag is not in the allowed_tags list, remove it and its contents
        if tag.name not in allowed_tags:
            tag.extract()
        else:
            # Filter and sanitize attributes
            for attr in list(tag.attrs):
                if attr not in ['href', 'src', 'alt', 'class']:
                    del tag[attr]
            # Remove some mastodon guff - spans with class "invisible"
            if tag.name == 'span' and 'class' in tag.attrs and 'invisible' in tag.attrs['class']:
                tag.extract()
            # Add nofollow and target=_blank to anchors
            if tag.name == 'a':
                tag.attrs['rel'] = 'nofollow ugc'
                tag.attrs['target'] = a_target
            # Add loading=lazy to images
            if tag.name == 'img':
                tag.attrs['loading'] = 'lazy'
            if tag.name == 'table':
                tag.attrs['class'] = 'table'

    clean_html = str(soup)

    # avoid returning empty anchors
    re_empty_anchor = re.compile(r'<a href="(.*?)" rel="nofollow ugc" target="_blank"><\/a>')
    clean_html = re_empty_anchor.sub(r'<a href="\1" rel="nofollow ugc" target="_blank">\1</a>', clean_html)

    # replace lemmy's spoiler markdown left in HTML
    clean_html = clean_html.replace('<h2>:::</h2>', '<p>:::</p>')   # this is needed for lemmy.world/c/hardware's sidebar, for some reason.
    re_spoiler = re.compile(r':{3}\s*?spoiler\s+?(\S.+?)(?:\n|</p>)(.+?)(?:\n|<p>):{3}', re.S)
    clean_html = re_spoiler.sub(r'<details><summary>\1</summary><p>\2</p></details>', clean_html)

    # replace strikethough markdown left in HTML
    re_strikethough = re.compile(r'~~(.*)~~')
    clean_html = re_strikethough.sub(r'<s>\1</s>', clean_html)

    # replace subscript markdown left in HTML
    re_subscript = re.compile(r'~(\S+)~')
    clean_html = re_subscript.sub(r'<sub>\1</sub>', clean_html)

    # replace superscript markdown left in HTML
    re_superscript = re.compile(r'\^(\S+)\^')
    clean_html = re_superscript.sub(r'<sup>\1</sup>', clean_html)

    # replace <img src> for mp4 with <video> - treat them like a GIF (autoplay, but initially muted)
    re_embedded_mp4 = re.compile(r'<img .*?src="(https://.*?\.mp4)".*?/>')
    clean_html = re_embedded_mp4.sub(
        r'<video class="responsive-video" controls preload="auto" autoplay muted loop playsinline disablepictureinpicture><source src="\1" type="video/mp4"></video>',
        clean_html)

    # replace <img src> for webm with <video> - treat them like a GIF (autoplay, but initially muted)
    re_embedded_webm = re.compile(r'<img .*?src="(https://.*?\.webm)".*?/>')
    clean_html = re_embedded_webm.sub(
        r'<video class="responsive-video" controls preload="auto" autoplay muted loop playsinline disablepictureinpicture><source src="\1" type="video/webm"></video>',
        clean_html)

    # replace <img src> for mp3 with <audio>
    re_embedded_mp3 = re.compile(r'<img .*?src="(https://.*?\.mp3)".*?/>')
    clean_html = re_embedded_mp3.sub(r'<audio controls><source src="\1" type="audio/mp3"></audio>', clean_html)

    # replace the 'static' for images hotlinked to fandom sites with 'vignette'
    re_fandom_hotlink = re.compile(r'<img alt="(.*?)" loading="lazy" src="https://static.wikia.nocookie.net')
    clean_html = re_fandom_hotlink.sub(r'<img alt="\1" loading="lazy" src="https://vignette.wikia.nocookie.net',
                                       clean_html)

    # replace ruby markdown like {漢字|かんじ}
    re_ruby = re.compile(r'\{(.+?)\|(.+?)\}')
    clean_html = re_ruby.sub(r'<ruby>\1<rp>(</rp><rt>\2</rt><rp>)</rp></ruby>', clean_html)

    return clean_html


def escape_non_html_angle_brackets(text: str) -> str:
    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets = []

    def store_code(match):
        code_snippets.append(match.group(0))
        return f"__CODE_PLACEHOLDER_{len(code_snippets) - 1}__"

    # Fenced code blocks (```...```)
    text = re.sub(r'```[\s\S]*?```', store_code, text)
    # Inline code (`...`)
    text = re.sub(r'`[^`\n]+`', store_code, text)

    # Step 2: Escape <...> unless they look like valid HTML tags
    def escape_tag(match):
        tag_content = match.group(1).strip().lower()
        # Handle closing tags by removing the leading slash before extracting tag name
        if tag_content.startswith('/'):
            tag_name = tag_content[1:].split()[0]
        else:
            tag_name = tag_content.split()[0]
        if tag_name in allowed_tags:
            return match.group(0)
        else:
            return f"&lt;{match.group(1)}&gt;"

    text = re.sub(r'<([^<>]+?)>', escape_tag, text)

    # Step 3: Restore code blocks
    for i, code in enumerate(code_snippets):
        text = text.replace(f"__CODE_PLACEHOLDER_{i}__", code)

    return text

def handle_double_bolds(text: str) -> str:
    """
    Handles properly assigning <strong> tags to **bolded** words in markdown even if there are **two** of them in the
    same sentence.
    """

    # Step 1: Extract inline and block code, replacing with placeholders
    code_snippets = []

    def store_code(match):
        code_snippets.append(match.group(0))
        return f"__CODE_PLACEHOLDER_{len(code_snippets) - 1}__"

    # Fenced code blocks (```...```)
    text = re.sub(r'```[\s\S]*?```', store_code, text)
    # Inline code (`...`)
    text = re.sub(r'`[^`\n]+`', store_code, text)

    # Step 2: Wrap **bold** sections with <strong></strong>
    # Regex is slightly modified from markdown2 source code
    re_bold = re.compile(r"(\*\*)(?=\S)(.+?[*]?)(?<=\S)\1")
    text = re_bold.sub(r"<strong>\2</strong>", text)

    # Step 3: Restore code blocks
    for i, code in enumerate(code_snippets):
        text = text.replace(f"__CODE_PLACEHOLDER_{i}__", code)

    return text


# use this for Markdown irrespective of origin, as it can deal with both soft break newlines ('\n' used by PieFed) and hard break newlines ('  \n' or ' \\n')
# ' \\n' will create <br /><br /> instead of just <br />, but hopefully that's acceptable.
def markdown_to_html(markdown_text, anchors_new_tab=True) -> str:
    if markdown_text:

        # Escape <...> if it’s not a real HTML tag
        markdown_text = escape_non_html_angle_brackets(
            markdown_text)  # To handle situations like https://ani.social/comment/9666667
        
        markdown_text = handle_double_bolds(markdown_text)  # To handle bold in two places in a sentence

        try:
            raw_html = markdown2.markdown(markdown_text,
                                          extras={'middle-word-em': False, 'tables': True, 'fenced-code-blocks': True, 'strike': True,
                                                  'tg-spoiler': True,
                                                  'breaks': {'on_newline': True, 'on_backslash': True},
                                                  'tag-friendly': True})
        except TypeError:
            # weird markdown, like https://mander.xyz/u/tty1 and https://feddit.uk/comment/16076443,
            # causes "markdown2.Markdown._color_with_pygments() argument after ** must be a mapping, not bool" error, so try again without fenced-code-blocks extra
            try:
                raw_html = markdown2.markdown(markdown_text,
                                              extras={'middle-word-em': False, 'tables': True, 'strike': True,
                                                      'tg-spoiler': True,
                                                      'breaks': {'on_newline': True, 'on_backslash': True},
                                                      'tag-friendly': True})
            except TypeError:
                raw_html = ''
        return allowlist_html(raw_html, a_target='_blank' if anchors_new_tab else '')
    else:
        return ''


# this function lets local users use the more intuitive soft-breaks for newlines, but actually stores the Markdown in Lemmy-compatible format
# Reasons for this:
# 1. it's what any adapted Lemmy apps using an API would expect
# 2. we've reverted to sending out Markdown in 'source' because:
#    a. Lemmy doesn't convert '<details><summary>' back into its '::: spoiler' format
#    b. anything coming from another PieFed instance would get reduced with html_to_text()
#    c. raw 'https' strings in code blocks are being converted into <a> links for HTML that Lemmy then converts back into []()
def piefed_markdown_to_lemmy_markdown(piefed_markdown: str):
    # only difference is newlines for soft breaks.
    re_breaks = re.compile(r'(\S)(\r\n)')
    lemmy_markdown = re_breaks.sub(r'\1  \2', piefed_markdown)
    return lemmy_markdown


def markdown_to_text(markdown_text) -> str:
    if not markdown_text or markdown_text == '':
        return ''
    return markdown_text.replace("# ", '')


def html_to_text(html) -> str:
    if html is None or html == '':
        return ''
    soup = BeautifulSoup(html, 'html.parser')
    return soup.get_text()


def mastodon_extra_field_link(extra_field: str) -> str:
    soup = BeautifulSoup(extra_field, 'html.parser')
    for tag in soup.find_all('a'):
        return tag['href']


def microblog_content_to_title(html: str) -> str:
    title = ''
    if '<p>' in html:
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup.find_all('p'):
            title = tag.get_text(separator=" ")
            if title and title.strip() != '' and len(title.strip()) >= 5:
                break
    else:
        title = html_to_text(html)

    period_index = title.find('.')
    question_index = title.find('?')
    exclamation_index = title.find('!')

    # Find the earliest occurrence of either '.' or '?' or '!'
    end_index = min(period_index if period_index != -1 else float('inf'),
                    question_index if question_index != -1 else float('inf'),
                    exclamation_index if exclamation_index != -1 else float('inf'))

    # there's no recognised punctuation
    if end_index == float('inf'):
        if len(title) >= 10:
            title = title.replace(' @ ', '').replace(' # ', '')
            title = shorten_string(title, 197)
        else:
            title = '(content in post body)'
        return title.strip()

    if end_index != -1:
        if question_index != -1 and question_index == end_index:
            end_index += 1  # Add the ? back on
        if exclamation_index != -1 and exclamation_index == end_index:
            end_index += 1  # Add the ! back on
        title = title[:end_index]

    if len(title) > 150:
        for i in range(149, -1, -1):
            if title[i] == ' ':
                break
        title = title[:i] + ' ...' if i > 0 else ''

    return title.strip()


def first_paragraph(html):
    soup = BeautifulSoup(html, 'html.parser')
    first_para = soup.find('p')
    if first_para:
        if first_para.text.strip() == 'Summary' or \
                first_para.text.strip() == '*Summary*' or \
                first_para.text.strip() == 'Comments' or \
                first_para.text.lower().startswith('cross-posted from:'):
            second_paragraph = first_para.find_next('p')
            if second_paragraph:
                return f'<p>{second_paragraph.text}</p>'
        return f'<p>{first_para.text}</p>'
    else:
        return ''


def community_link_to_href(link: str) -> str:
    pattern = r"!([a-zA-Z0-9_.-]*)@([a-zA-Z0-9_.-]*)\b"
    server = r'<a href=https://' + current_app.config['SERVER_NAME'] + r'/community/lookup/'
    return re.sub(pattern, server + r'\g<1>/\g<2>>' + r'!\g<1>@\g<2></a>', link)


def feed_link_to_href(link: str) -> str:
    pattern = r"~([a-zA-Z0-9_.-]*)@([a-zA-Z0-9_.-]*)\b"
    server = r'<a href=https://' + current_app.config['SERVER_NAME'] + r'/feed/lookup/'
    return re.sub(pattern, server + r'\g<1>/\g<2>>' + r'~\g<1>@\g<2></a>', link)


def person_link_to_href(link: str) -> str:
    pattern = r"@([a-zA-Z0-9_.-]*)@([a-zA-Z0-9_.-]*)\b"
    server = f'https://{current_app.config["SERVER_NAME"]}/user/lookup/'
    replacement = (r'<a href="' + server + r'\g<1>/\g<2>" rel="nofollow noindex">@\g<1>@\g<2></a>')
    return re.sub(pattern, replacement, link)


def domain_from_url(url: str, create=True) -> Domain:
    parsed_url = urlparse(url.lower().replace('www.', ''))
    if parsed_url and parsed_url.hostname:
        find_this = parsed_url.hostname.lower()
        if find_this == 'youtu.be':
            find_this = 'youtube.com'
        domain = Domain.query.filter_by(name=find_this).first()
        if create and domain is None:
            domain = Domain(name=find_this)
            db.session.add(domain)
            db.session.commit()
        return domain
    else:
        return None


def domain_from_email(email: str) -> str:
    if email is None or email.strip() == '':
        return ''
    else:
        if '@' in email:
            parts = email.split('@')
            return parts[-1]
        else:
            return ''


def shorten_string(input_str, max_length=50):
    if input_str:
        if len(input_str) <= max_length:
            return input_str
        else:
            return input_str[:max_length - 3] + '…'
    else:
        return ''


def shorten_url(input: str, max_length=20):
    if input:
        return shorten_string(input.replace('https://', '').replace('http://', ''))
    else:
        ''


def remove_images(html) -> str:
    # Parse the HTML content
    soup = BeautifulSoup(html, 'html.parser')

    # Remove all <img> tags
    for img in soup.find_all('img'):
        img.decompose()

    # Remove all <video> tags
    for video in soup.find_all('video'):
        video.decompose()

    # Return the modified HTML
    return str(soup)


# the number of digits in a number. e.g. 1000 would be 4
def digits(input: int) -> int:
    return len(shorten_number(input))


@cache.memoize(timeout=50)
def user_access(permission: str, user_id: int) -> bool:
    if user_id == 0:
        return False
    if user_id == 1:
        return True
    has_access = db.session.execute(text('SELECT * FROM "role_permission" as rp ' +
                                         'INNER JOIN user_role ur on rp.role_id = ur.role_id ' +
                                         'WHERE ur.user_id = :user_id AND rp.permission = :permission'),
                                    {'user_id': user_id, 'permission': permission}).first()
    return has_access is not None


def role_access(permission: str, role_id: int) -> bool:
    has_access = db.session.execute(text('SELECT * FROM "role_permission" as rp ' +
                                         'WHERE rp.role_id = :role_id AND rp.permission = :permission'),
                                    {'role_id': role_id, 'permission': permission}).first()
    return has_access is not None


@cache.memoize(timeout=10)
def community_membership(user: User, community: Community) -> int:
    if community is None:
        return False
    return user.subscribed(community.id)


@cache.memoize(timeout=10)
def feed_membership(user: User, feed: Feed) -> int:
    if feed is None:
        return False
    return feed.subscribed(user.id)


@cache.memoize(timeout=86400)
def communities_banned_from(user_id: int) -> List[int]:
    if user_id == 0:
        return []
    community_bans = CommunityBan.query.filter(CommunityBan.user_id == user_id).all()
    return [cb.community_id for cb in community_bans]


@cache.memoize(timeout=86400)
def blocked_domains(user_id) -> List[int]:
    if user_id == 0:
        return []
    blocks = DomainBlock.query.filter_by(user_id=user_id)
    return [block.domain_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_communities(user_id) -> List[int]:
    if user_id == 0:
        return []
    blocks = CommunityBlock.query.filter_by(user_id=user_id)
    return [block.community_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_instances(user_id) -> List[int]:
    if user_id == 0:
        return []
    blocks = InstanceBlock.query.filter_by(user_id=user_id)
    return [block.instance_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_users(user_id) -> List[int]:
    if user_id == 0:
        return []
    blocks = UserBlock.query.filter_by(blocker_id=user_id)
    return [block.blocked_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_phrases() -> List[str]:
    site = Site.query.get(1)
    if site.blocked_phrases:
        blocked_phrases = []
        for phrase in site.blocked_phrases.split('\n'):
            if phrase != '':
                if phrase.endswith('\r'):
                    blocked_phrases.append(phrase[:-1])
                else:
                    blocked_phrases.append(phrase)
        return blocked_phrases
    else:
        return []


@cache.memoize(timeout=86400)
def blocked_referrers() -> List[str]:
    site = Site.query.get(1)
    if site.auto_decline_referrers:
        return [referrer for referrer in site.auto_decline_referrers.split('\n') if referrer != '']
    else:
        return []


def retrieve_block_list():
    try:
        response = httpx_client.get('https://raw.githubusercontent.com/rimu/no-qanon/master/domains.txt', timeout=1)
    except:
        return None
    if response and response.status_code == 200:
        return response.text


def retrieve_peertube_block_list():
    try:
        response = httpx_client.get('https://peertube_isolation.frama.io/list/peertube_isolation.json', timeout=1)
    except:
        return None
    list = ''
    if response and response.status_code == 200:
        response_data = response.json()
        for row in response_data['data']:
            list += row['value'] + "\n"
    response.close()
    return list.strip()


def ensure_directory_exists(directory):
    """Ensure a directory exists and is writable, creating it if necessary."""
    parts = directory.split('/')
    rebuild_directory = ''
    for part in parts:
        rebuild_directory += part
        if not os.path.isdir(rebuild_directory):
            os.mkdir(rebuild_directory)
        rebuild_directory += '/'

    # Check if the final directory is writable
    if not os.access(directory, os.W_OK):
        current_app.logger.warning(f"Directory '{directory}' is not writable")


def mimetype_from_url(url):
    parsed_url = urlparse(url)
    path = parsed_url.path.split('?')[0]  # Strip off anything after '?'
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type


def is_activitypub_request():
    return 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get('Accept', '')


def validation_required(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if current_user.verified or not get_setting('email_verification', True):
            return func(*args, **kwargs)
        else:
            return redirect(url_for('auth.validation_required'))

    return decorated_view


def login_required_if_private_instance(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if (g.site.private_instance and current_user.is_authenticated) or is_activitypub_request() or g.site.private_instance is False:
            return func(*args, **kwargs)
        else:
            return redirect(url_for('auth.login'))

    return decorated_view


def permission_required(permission):
    def decorator(func):
        @wraps(func)
        def decorated_view(*args, **kwargs):
            if user_access(permission, current_user.get_id()):
                return func(*args, **kwargs)
            else:
                # Handle the case where the user doesn't have the required permission
                return redirect(url_for('auth.permission_denied'))

        return decorated_view

    return decorator


def login_required(csrf=True):
    def decorator(func):
        @wraps(func)
        def decorated_view(*args, **kwargs):
            if request.method in {"OPTIONS"} or current_app.config.get("LOGIN_DISABLED"):
                pass
            elif not current_user.is_authenticated:
                return current_app.login_manager.unauthorized()

            # Validate CSRF token for POST requests
            if request.method == 'POST' and csrf:
                validate_csrf(request.form.get('csrf_token', request.headers.get('x-csrftoken')))

            # flask 1.x compatibility
            # current_app.ensure_sync is only available in Flask >= 2.0
            if callable(getattr(current_app, "ensure_sync", None)):
                return current_app.ensure_sync(func)(*args, **kwargs)
            return func(*args, **kwargs)

        return decorated_view

    # Handle both @login_required and @login_required()
    if callable(csrf):
        # Called as @login_required (csrf is actually the function)
        func = csrf
        csrf = True
        return decorator(func)
    else:
        # Called as @login_required(csrf=False)
        return decorator


def debug_mode_only(func):
    @wraps(func)
    def decorated_function(*args, **kwargs):
        if current_app.debug:
            return func(*args, **kwargs)
        else:
            return abort(403,
                         description="Not available in production mode. Set the FLASK_DEBUG environment variable to 1.")

    return decorated_function


def block_bots(func):
    @wraps(func)
    def decorated_function(*args, **kwargs):
        if not is_bot(request.user_agent.string):
            return func(*args, **kwargs)
        else:
            return abort(403, description="Do not index this.")

    return decorated_function


def is_bot(user_agent) -> bool:
    user_agent = user_agent.lower()
    if 'bot' in user_agent:
        return True
    if 'meta-externalagent' in user_agent:
        return True
    return False


# sends the user back to where they came from
def back(default_url):
    # Get the referrer from the request headers
    referrer = request.referrer

    # If the referrer exists and is not the same as the current request URL, redirect to the referrer
    if referrer and referrer != request.url:
        return redirect(referrer)

    # If referrer is not available or is the same as the current request URL, redirect to the default URL
    return redirect(default_url)


# format a datetime in a way that is used in ActivityPub
def ap_datetime(date_time: datetime) -> str:
    return date_time.isoformat() + '+00:00'


class MultiCheckboxField(SelectMultipleField):
    widget = ListWidget(prefix_label=False)
    option_widget = CheckboxInput()


def ip_address() -> str:
    ip = request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For') or request.remote_addr
    if ',' in ip:  # Remove all but first ip addresses
        ip = ip[:ip.index(',')].strip()
    return ip


def user_ip_banned() -> bool:
    current_ip_address = ip_address()
    if current_ip_address:
        return current_ip_address in banned_ip_addresses()


@cache.memoize(timeout=150)
def instance_banned(domain: str) -> bool:  # see also activitypub.util.instance_blocked()
    session = get_task_session()
    try:
        if domain is None or domain == '':
            return False
        domain = domain.lower().strip()
        if 'https://' in domain or 'http://' in domain:
            domain = urlparse(domain).hostname
        banned = session.query(BannedInstances).filter_by(domain=domain).first()
        if banned is not None:
            return True

        # Mastodon sometimes bans with a * in the domain name, meaning "any letter", e.g. "cum.**mp"
        regex_patterns = [re.compile(f"^{cond.domain.replace('*', '[a-zA-Z0-9]')}$") for cond in
                          session.query(BannedInstances).filter(BannedInstances.domain.like('%*%')).all()]
        return any(pattern.match(domain) for pattern in regex_patterns)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@cache.memoize(timeout=150)
def instance_online(domain: str) -> bool:
    if domain is None or domain == '':
        return False
    domain = domain.lower().strip()
    if 'https://' in domain or 'http://' in domain:
        domain = urlparse(domain).hostname
    session = get_task_session()
    try:
        instance = session.query(Instance).filter_by(domain=domain).first()
        if instance is not None:
            return instance.online()
        else:
            return False
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@cache.memoize(timeout=150)
def instance_gone_forever(domain: str) -> bool:
    if domain is None or domain == '':
        return False
    domain = domain.lower().strip()
    if 'https://' in domain or 'http://' in domain:
        domain = urlparse(domain).hostname
    session = get_task_session()
    try:
        instance = Instance.query.filter_by(domain=domain).first()
        if instance is not None:
            return instance.gone_forever
        else:
            return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def user_cookie_banned() -> bool:
    cookie = request.cookies.get('sesion', None)
    return cookie is not None


@cache.memoize(timeout=30)
def banned_ip_addresses() -> List[str]:
    session = get_task_session()
    try:
        ips = session.query(IpBan).all()
        return [ip.ip_address for ip in ips]
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def guess_mime_type(file_path: str) -> str:
    content_type = mimetypes.guess_type(file_path)
    if content_type is None:
        ext = os.path.splitext(file_path)[1].lower().lstrip('.')  # get extension without dot
        content_type = f'image/{ext}' if ext else 'application/octet-stream'
    else:
        if content_type[0] is None:
            ext = os.path.splitext(file_path)[1].lower().lstrip('.')  # get extension without dot
            return f'image/{ext}' if ext else 'application/octet-stream'
        content_type = content_type[0]
    return content_type


def can_downvote(user, community: Community, communities_banned_from_list=None) -> bool:
    if user is None or community is None or user.banned or user.bot:
        return False

    try:
        site = g.site
    except:
        site = Site.query.get(1)

    if not site.enable_downvotes:
        return False

    if community.local_only and not user.is_local():
        return False

    if (user.attitude is not None and user.attitude < 0.0) or user.reputation < -10:
        return False

    if community.downvote_accept_mode != DOWNVOTE_ACCEPT_ALL:
        if community.downvote_accept_mode == DOWNVOTE_ACCEPT_MEMBERS:
            if not community.is_member(user):
                return False
        elif community.downvote_accept_mode == DOWNVOTE_ACCEPT_INSTANCE:
            if user.instance_id != community.instance_id:
                return False
        elif community.downvote_accept_mode == DOWNVOTE_ACCEPT_TRUSTED:
            if community.instance_id == user.instance_id:
                pass
            else:
                if user.instance_id not in trusted_instance_ids():
                    return False

    if communities_banned_from_list is not None:
        if community.id in communities_banned_from_list:
            return False
    else:
        if community.id in communities_banned_from(user.id):
            return False

    return True


def can_upvote(user, community: Community, communities_banned_from_list=None) -> bool:
    if user is None or community is None or user.banned or user.bot:
        return False

    if communities_banned_from_list is not None:
        if community.id in communities_banned_from_list:
            return False
    else:
        if community.id in communities_banned_from(user.id):
            return False

    return True


def can_create_post(user, content: Community) -> bool:
    if content is None:
        return False

    if user is None or content is None or user.banned:
        return False

    if user.ban_posts:
        return False

    if content.is_moderator(user) or user.is_admin():
        return True

    if content.restricted_to_mods:
        return False

    if content.local_only and not user.is_local():
        return False

    if content.id in communities_banned_from(user.id):
        return False

    return True


def can_create_post_reply(user, content: Community) -> bool:
    if user is None or content is None or user.banned:
        return False

    if user.ban_comments:
        return False

    if content.is_moderator(user) or user.is_admin():
        return True

    if content.local_only and not user.is_local():
        return False

    if content.id in communities_banned_from(user.id):
        return False

    return True


def reply_already_exists(user_id, post_id, parent_id, body) -> bool:
    if parent_id is None:
        num_matching_replies = db.session.execute(text(
            'SELECT COUNT(id) as c FROM "post_reply" WHERE deleted is false and user_id = :user_id AND post_id = :post_id AND parent_id is null AND body = :body'),
            {'user_id': user_id, 'post_id': post_id, 'body': body}).scalar()
    else:
        num_matching_replies = db.session.execute(text(
            'SELECT COUNT(id) as c FROM "post_reply" WHERE deleted is false and user_id = :user_id AND post_id = :post_id AND parent_id = :parent_id AND body = :body'),
            {'user_id': user_id, 'post_id': post_id, 'parent_id': parent_id, 'body': body}).scalar()
    return num_matching_replies != 0


def reply_is_just_link_to_gif_reaction(body) -> bool:
    tmp_body = body.strip()
    if tmp_body.startswith('https://media.tenor.com/') or \
            tmp_body.startswith('https://media1.tenor.com/') or \
            tmp_body.startswith('https://media2.tenor.com/') or \
            tmp_body.startswith('https://media3.tenor.com/') or \
            tmp_body.startswith('https://i.giphy.com/') or \
            tmp_body.startswith('https://i.imgflip.com') or \
            tmp_body.startswith('https://media1.giphy.com/') or \
            tmp_body.startswith('https://media2.giphy.com/') or \
            tmp_body.startswith('https://media3.giphy.com/') or \
            tmp_body.startswith('https://media4.giphy.com/'):
        return True
    else:
        return False


def reply_is_stupid(body) -> bool:
    lower_body = body.lower().strip()
    if lower_body == 'this' or lower_body == 'this.' or lower_body == 'this!':
        return True
    return False


@cache.memoize(timeout=10)
def trusted_instance_ids() -> List[int]:
    return [instance.id for instance in Instance.query.filter(Instance.trusted == True)]


def inbox_domain(inbox: str) -> str:
    inbox = inbox.lower()
    if 'https://' in inbox or 'http://' in inbox:
        inbox = urlparse(inbox).hostname
    return inbox


def awaken_dormant_instance(instance):
    if instance and not instance.gone_forever:
        if instance.dormant:
            if instance.start_trying_again is None:
                instance.start_trying_again = utcnow() + timedelta(seconds=instance.failures ** 4)
                db.session.commit()
            else:
                if instance.start_trying_again < utcnow():
                    instance.dormant = False
                    db.session.commit()
        # give up after ~5 days of trying
        if instance.start_trying_again and utcnow() + timedelta(days=5) < instance.start_trying_again:
            instance.gone_forever = True
            instance.dormant = True
            db.session.commit()


def shorten_number(number):
    if number < 1000:
        return str(number)
    elif number < 1000000:
        return f'{number / 1000:.1f}k'
    else:
        return f'{number / 1000000:.1f}M'


@cache.memoize(timeout=300)
def user_filters_home(user_id):
    filters = Filter.query.filter_by(user_id=user_id, filter_home=True).filter(
        or_(Filter.expire_after > date.today(), Filter.expire_after == None))
    result = defaultdict(set)
    for filter in filters:
        keywords = [keyword.strip().lower() for keyword in filter.keywords.splitlines()]
        if filter.hide_type == 0:
            result[filter.title].update(keywords)
        else:  # type == 1 means hide completely. These posts are excluded from output by the jinja template
            result['-1'].update(keywords)
    return result


@cache.memoize(timeout=300)
def user_filters_posts(user_id):
    filters = Filter.query.filter_by(user_id=user_id, filter_posts=True).filter(
        or_(Filter.expire_after > date.today(), Filter.expire_after == None))
    result = defaultdict(set)
    for filter in filters:
        keywords = [keyword.strip().lower() for keyword in filter.keywords.splitlines()]
        if filter.hide_type == 0:
            result[filter.title].update(keywords)
        else:
            result['-1'].update(keywords)
    return result


@cache.memoize(timeout=300)
def user_filters_replies(user_id):
    filters = Filter.query.filter_by(user_id=user_id, filter_replies=True).filter(
        or_(Filter.expire_after > date.today(), Filter.expire_after == None))
    result = defaultdict(set)
    for filter in filters:
        keywords = [keyword.strip().lower() for keyword in filter.keywords.splitlines()]
        if filter.hide_type == 0:
            result[filter.title].update(keywords)
        else:
            result['-1'].update(keywords)
    return result


@cache.memoize(timeout=300)
def moderating_communities(user_id) -> List[Community]:
    if user_id is None or user_id == 0:
        return []
    communities = Community.query.join(CommunityMember, Community.id == CommunityMember.community_id). \
        filter(Community.banned == False). \
        filter(or_(CommunityMember.is_moderator == True, CommunityMember.is_owner == True)). \
        filter(CommunityMember.is_banned == False). \
        filter(CommunityMember.user_id == user_id).order_by(Community.title).all()

    # Track display names to identify duplicates
    display_name_counts = {}
    for community in communities:
        display_name = community.title
        display_name_counts[display_name] = display_name_counts.get(display_name, 0) + 1

    # Flag communities as duplicates if their display name appears more than once
    for community in communities:
        community.is_duplicate = display_name_counts[community.title] > 1

    return communities


@cache.memoize(timeout=300)
def moderating_communities_ids(user_id) -> List[int]:
    """
    Raw SQL version of moderating_communities() that returns community IDs instead of full objects.
    """
    if user_id is None or user_id == 0:
        return []

    sql = text("""
        SELECT c.id
        FROM community c
        JOIN community_member cm ON c.id = cm.community_id
        WHERE c.banned = false
          AND (cm.is_moderator = true OR cm.is_owner = true)
          AND cm.is_banned = false
          AND cm.user_id = :user_id
        ORDER BY c.title
    """)

    return db.session.execute(sql, {'user_id': user_id}).scalars().all()


@cache.memoize(timeout=300)
def joined_communities(user_id) -> List[Community]:
    if user_id is None or user_id == 0:
        return []
    communities = Community.query.join(CommunityMember, Community.id == CommunityMember.community_id). \
        filter(Community.banned == False). \
        filter(CommunityMember.is_moderator == False, CommunityMember.is_owner == False). \
        filter(CommunityMember.is_banned == False). \
        filter(CommunityMember.user_id == user_id).order_by(Community.title).all()

    # track display names to identify duplicates
    display_name_counts = {}
    for community in communities:
        display_name = community.title
        display_name_counts[display_name] = display_name_counts.get(display_name, 0) + 1

    # flag communities as duplicates if their display name appears more than once
    for community in communities:
        community.is_duplicate = display_name_counts[community.title] > 1

    return communities


def joined_or_modding_communities(user_id):
    if user_id is None or user_id == 0:
        return []
    return db.session.execute(text(
        'SELECT c.id FROM "community" as c INNER JOIN "community_member" as cm on c.id = cm.community_id WHERE c.banned = false AND cm.user_id = :user_id'),
                              {'user_id': user_id}).scalars().all()


def pending_communities(user_id):
    if user_id is None or user_id == 0:
        return []
    result = []
    for join_request in CommunityJoinRequest.query.filter_by(user_id=user_id).all():
        result.append(join_request.community_id)
    return result


@cache.memoize(timeout=3000)
def menu_topics():
    return Topic.query.filter(Topic.parent_id == None).order_by(Topic.name).all()


@cache.memoize(timeout=3000)
def menu_instance_feeds():
    return Feed.query.filter(Feed.parent_feed_id == None).filter(Feed.is_instance_feed == True).order_by(
        Feed.name).all()


# @cache.memoize(timeout=3000)
def menu_my_feeds(user_id):
    return Feed.query.filter(Feed.parent_feed_id == None).filter(Feed.user_id == user_id).order_by(Feed.name).all()


@cache.memoize(timeout=3000)
def menu_subscribed_feeds(user_id):
    return Feed.query.join(FeedMember, Feed.id == FeedMember.feed_id).filter(FeedMember.user_id == user_id).filter_by(
        is_owner=False).all()


# @cache.memoize(timeout=3000)
def subscribed_feeds(user_id: int) -> List[int]:
    if user_id is None or user_id == 0:
        return []
    return [feed.id for feed in
            Feed.query.join(FeedMember, Feed.id == FeedMember.feed_id).filter(FeedMember.user_id == user_id)]


@cache.memoize(timeout=300)
def community_moderators(community_id):
    mods = CommunityMember.query.filter((CommunityMember.community_id == community_id) &
                                        (or_(
                                            CommunityMember.is_owner,
                                            CommunityMember.is_moderator
                                        ))
                                        ).all()
    community = Community.query.get(community_id)
    if community.user_id not in [mod.user_id for mod in mods]:
        mods.append(CommunityMember(user_id=community.user_id, is_owner=True, community_id=community.id))
    return mods


def finalize_user_setup(user):
    from app.activitypub.signature import RsaKeys
    user.verified = True
    user.last_seen = utcnow()
    if user.private_key is None and user.public_key is None:
        private_key, public_key = RsaKeys.generate_keypair()
        user.private_key = private_key
        user.public_key = public_key

    # Only set AP profile IDs if they haven't been set already
    if user.ap_profile_id is None:
        user.ap_profile_id = f"https://{current_app.config['SERVER_NAME']}/u/{user.user_name}".lower()
        user.ap_public_url = f"https://{current_app.config['SERVER_NAME']}/u/{user.user_name}"
        user.ap_inbox_url = f"https://{current_app.config['SERVER_NAME']}/u/{user.user_name.lower()}/inbox"

    # find all notifications from this registration and mark them as read
    reg_notifs = Notification.query.filter_by(notif_type=NOTIF_REGISTRATION, author_id=user.id)
    for rn in reg_notifs:
        rn.read = True

    db.session.commit()


def notification_subscribers(entity_id: int, entity_type: int) -> List[int]:
    return list(db.session.execute(
        text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :entity_id AND type = :type '),
        {'entity_id': entity_id, 'type': entity_type}).scalars())


# topics, in a tree
def topic_tree() -> List:
    topics = Topic.query.order_by(Topic.name)

    topics_dict = {topic.id: {'topic': topic, 'children': []} for topic in topics.all()}

    for topic in topics:
        if topic.parent_id is not None:
            parent_comment = topics_dict.get(topic.parent_id)
            if parent_comment:
                parent_comment['children'].append(topics_dict[topic.id])

    return [topic for topic in topics_dict.values() if topic['topic'].parent_id is None]


# feeds, in a tree
def feed_tree(user_id) -> List[dict]:
    feeds = Feed.query.filter(Feed.user_id == user_id).order_by(Feed.name)

    feeds_dict = {feed.id: {'feed': feed, 'children': []} for feed in feeds.all()}

    for feed in feeds:
        if feed.parent_feed_id is not None:
            parent_comment = feeds_dict.get(feed.parent_feed_id)
            if parent_comment:
                parent_comment['children'].append(feeds_dict[feed.id])

    return [feed for feed in feeds_dict.values() if feed['feed'].parent_feed_id is None]


def feed_tree_public() -> List[dict]:
    feeds = Feed.query.filter(Feed.public == True).order_by(Feed.title)

    feeds_dict = {feed.id: {'feed': feed, 'children': []} for feed in feeds.all()}

    for feed in feeds:
        if feed.parent_feed_id is not None:
            parent_comment = feeds_dict.get(feed.parent_feed_id)
            if parent_comment:
                parent_comment['children'].append(feeds_dict[feed.id])

    return [feed for feed in feeds_dict.values() if feed['feed'].parent_feed_id is None]


def opengraph_parse(url):
    if '?' in url:
        url = url.split('?')
        url = url[0]
    try:
        return parse_page(url)
    except Exception:
        return None


def url_to_thumbnail_file(filename) -> File:
    try:
        timeout = 15 if 'washingtonpost.com' in filename else 5  # Washington Post is really slow for some reason
        response = httpx_client.get(filename, timeout=timeout)
    except:
        return None

    if response.status_code == 200:
        content_type = response.headers.get('content-type')
        if content_type and content_type.startswith('image'):
            # Generate file extension from mime type
            if ';' in content_type:
                content_type_parts = content_type.split(';')
                content_type = content_type_parts[0]
            content_type_parts = content_type.split('/')
            if content_type_parts:
                file_extension = '.' + content_type_parts[-1]
                if file_extension == '.jpeg':
                    file_extension = '.jpg'
            else:
                file_extension = os.path.splitext(filename)[1]
                file_extension = file_extension.replace('%3f', '?')  # sometimes urls are not decoded properly
                if '?' in file_extension:
                    file_extension = file_extension.split('?')[0]

            new_filename = gibberish(15)
            if store_files_in_s3():
                directory = 'app/static/tmp'
            else:
                directory = 'app/static/media/posts/' + new_filename[0:2] + '/' + new_filename[2:4]
            ensure_directory_exists(directory)
            temp_file_path = os.path.join(directory, new_filename + file_extension)

            with open(temp_file_path, 'wb') as f:
                f.write(response.content)
            response.close()

            # Use environment variables to determine URL thumbnail

            medium_image_format = current_app.config['MEDIA_IMAGE_MEDIUM_FORMAT']
            medium_image_quality = current_app.config['MEDIA_IMAGE_MEDIUM_QUALITY']

            final_ext = file_extension

            if medium_image_format == 'AVIF':
                import pillow_avif  # NOQA

            Image.MAX_IMAGE_PIXELS = 89478485
            with Image.open(temp_file_path) as img:
                img = ImageOps.exif_transpose(img)
                img = img.convert('RGB' if (medium_image_format == 'JPEG' or final_ext in ['.jpg', '.jpeg']) else 'RGBA')

                # Create 170px thumbnail
                img_170 = img.copy()
                img_170.thumbnail((170, 170), resample=Image.LANCZOS)

                kwargs = {}
                if medium_image_format:
                    kwargs['format'] = medium_image_format.upper()
                    final_ext = '.' + medium_image_format.lower()
                    temp_file_path = os.path.splitext(temp_file_path)[0] + final_ext
                if medium_image_quality:
                    kwargs['quality'] = int(medium_image_quality)

                img_170.save(temp_file_path, optimize=True, **kwargs)
                thumbnail_width = img_170.width
                thumbnail_height = img_170.height

                # Create 512px thumbnail
                img_512 = img.copy()
                img_512.thumbnail((512, 512), resample=Image.LANCZOS)

                # Create filename for 512px thumbnail
                temp_file_path_512 = os.path.splitext(temp_file_path)[0] + '_512' + final_ext
                img_512.save(temp_file_path_512, optimize=True, **kwargs)
                thumbnail_512_width = img_512.width
                thumbnail_512_height = img_512.height

            if store_files_in_s3():
                content_type = guess_mime_type(temp_file_path)
                boto3_session = boto3.session.Session()
                s3 = boto3_session.client(
                    service_name='s3',
                    region_name=current_app.config['S3_REGION'],
                    endpoint_url=current_app.config['S3_ENDPOINT'],
                    aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                    aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
                )
                # Upload 170px thumbnail
                s3.upload_file(temp_file_path, current_app.config['S3_BUCKET'], 'posts/' +
                               new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext,
                               ExtraArgs={'ContentType': content_type})
                # Upload 512px thumbnail
                s3.upload_file(temp_file_path_512, current_app.config['S3_BUCKET'], 'posts/' +
                               new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + '_512' + final_ext,
                               ExtraArgs={'ContentType': content_type})
                os.unlink(temp_file_path)
                os.unlink(temp_file_path_512)
                thumbnail_170_url = f"https://{current_app.config['S3_PUBLIC_URL']}/posts/{new_filename[0:2]}/{new_filename[2:4]}" + \
                                    '/' + new_filename + final_ext
                thumbnail_512_url = f"https://{current_app.config['S3_PUBLIC_URL']}/posts/{new_filename[0:2]}/{new_filename[2:4]}" + \
                                    '/' + new_filename + '_512' + final_ext
            else:
                # For local storage, use the temp file paths as final URLs
                thumbnail_170_url = temp_file_path
                thumbnail_512_url = temp_file_path_512
            return File(file_path=thumbnail_512_url, thumbnail_width=thumbnail_width, width=thumbnail_512_width,
                        height=thumbnail_512_height,
                        thumbnail_height=thumbnail_height, thumbnail_path=thumbnail_170_url,
                        source_url=filename)


# By no means is this a complete list, but it is very easy to search for the ones you need later.
KNOWN_OPENGRAPH_TAGS = [
    "og:site_name",
    "og:title",
    "og:locale",
    "og:type",
    "og:image",
    "og:url",
    "og:image:url",
    "og:image:secure_url",
    "og:image:type",
    "og:image:width",
    "og:image:height",
    "og:image:alt",
]


def parse_page(page_url, tags_to_search=KNOWN_OPENGRAPH_TAGS, fallback_tags=None):
    '''
    Parses a page, returns a JSON style dictionary of all OG tags found on that page.

    Passing in tags_to_search is optional. By default it will search through KNOWN_OPENGRAPH_TAGS constant, but for the sake of efficiency, you may want to only search for 1 or 2 tags

    Returns False if page is unreadable
    '''
    # read the html from the page
    response = get_request(page_url)

    if response.status_code != 200:
        return False

    # set up beautiful soup
    soup = BeautifulSoup(response.content, 'html.parser')

    # loop through the known list of opengraph tags, searching for each and appending a dictionary as we go.
    found_tags = {}

    for og_tag in tags_to_search:
        new_found_tag = soup.find("meta", property=og_tag)
        if new_found_tag is not None:
            found_tags[new_found_tag["property"]] = new_found_tag["content"]
        elif fallback_tags is not None and og_tag in fallback_tags:
            found_tags[og_tag] = soup.find(fallback_tags[og_tag]).text

    return found_tags


def current_theme():
    """ The theme the current user has set, falling back to the site default if none specified or user is not logged in """
    if hasattr(g, 'site'):
        site = g.site
    else:
        site = Site.query.get(1)
    if current_user.is_authenticated:
        if current_user.theme is not None and current_user.theme != '':
            return current_user.theme
        else:
            return site.default_theme if site.default_theme is not None else 'piefed'
    else:
        return site.default_theme if site.default_theme is not None else 'piefed'


def theme_list():
    """ All the themes available, by looking in the templates/themes directory """
    result = [('piefed', 'PieFed')]
    for root, dirs, files in os.walk('app/templates/themes'):
        for dir in dirs:
            if os.path.exists(f'app/templates/themes/{dir}/{dir}.json'):
                theme_settings = json.loads(file_get_contents(f'app/templates/themes/{dir}/{dir}.json'))
                result.append((dir, theme_settings['name']))
    return result


def sha256_digest(input_string):
    """
    Compute the SHA-256 hash digest of a given string.

    Args:
    - input_string: The string to compute the hash digest for.

    Returns:
    - A hexadecimal string representing the SHA-256 hash digest.
    """
    sha256_hash = hashlib.sha256()
    sha256_hash.update(input_string.encode('utf-8'))
    return sha256_hash.hexdigest()


# still used to hint to a local user that a post to a URL has already been submitted
def remove_tracking_from_link(url):
    parsed_url = urlparse(url)

    if parsed_url.netloc == 'youtu.be':
        # Extract video ID
        video_id = parsed_url.path[1:]  # Remove leading slash

        # Preserve 't' parameter if it exists
        query_params = parse_qs(parsed_url.query)
        if 't' in query_params:
            new_query_params = {'t': query_params['t']}
            new_query_string = urlencode(new_query_params, doseq=True)
        else:
            new_query_string = ''

        cleaned_url = f"https://youtube.com/watch?v={video_id}"
        if new_query_string:
            new_query_string = new_query_string.replace('t=', 'start=')
            cleaned_url += f"&{new_query_string}"

        return cleaned_url
    else:
        return url


# Fixes URLs so we're more likely to get a thumbnail from youtube, and more posts from streaming sites are embedded
# Also duplicates link tracking removal from the function above.
def fixup_url(url):
    thumbnail_url = embed_url = url
    parsed_url = urlparse(url)

    # fixup embed_url for peertube videos shared outside of the channel
    if len(url) > 25 and url[-25:][:3] == '/w/':
        peertube_domains = db.session.execute(text("SELECT domain FROM instance WHERE software = 'peertube'")).scalars()
        if parsed_url.netloc in peertube_domains:
            try:
                response = get_request(url, headers={'Accept': 'application/activity+json'})
                if response.status_code == 200:
                    try:
                        video_json = response.json()
                        if 'id' in video_json:
                            embed_url = video_json['id']
                        response.close()
                    except:
                        response.close()
            except:
                pass

    youtube_domains = ['www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtube.com', 'youtu.be']

    if not parsed_url.netloc in youtube_domains:
        return thumbnail_url, embed_url
    else:
        video_id = timestamp = None
        path = parsed_url.path
        query_params = parse_qs(parsed_url.query)
        if path:
            if path.startswith('/shorts/') and len(path) > 8:
                video_id = path[8:]
            elif path == '/watch' and 'v' in query_params:
                video_id = query_params['v'][0]
            else:
                video_id = path[1:]
        if not video_id:
            return thumbnail_url, embed_url
        if 'start' in query_params:
            timestamp = query_params['start'][0]
        elif 't' in query_params:
            timestamp = query_params['t'][0]

        thumbnail_url = 'https://youtu.be/' + video_id
        embed_url = 'https://www.youtube.com/watch?v=' + video_id
        if timestamp:
            timestamp_param = {'start': timestamp}
            timestamp_query = urlencode(timestamp_param, doseq=True)
            embed_url += f"&{timestamp_query}"

        return thumbnail_url, embed_url


def show_ban_message():
    flash(_('You have been banned.'), 'error')
    logout_user()
    resp = make_response(redirect(url_for('main.index')))
    resp.set_cookie('sesion', '17489047567495', expires=datetime(year=2099, month=12, day=30))
    return resp


# search a sorted list using a binary search. Faster than using 'in' with a unsorted list.
def in_sorted_list(arr, target):
    index = bisect.bisect_left(arr, target)
    return index < len(arr) and arr[index] == target


@cache.memoize(timeout=600)
def recently_upvoted_posts(user_id) -> List[int]:
    post_ids = db.session.execute(
        text('SELECT post_id FROM "post_vote" WHERE user_id = :user_id AND effect > 0 ORDER BY id DESC LIMIT 300'),
        {'user_id': user_id}).scalars()
    return sorted(post_ids)  # sorted so that in_sorted_list can be used


@cache.memoize(timeout=600)
def recently_downvoted_posts(user_id) -> List[int]:
    post_ids = db.session.execute(
        text('SELECT post_id FROM "post_vote" WHERE user_id = :user_id AND effect < 0 ORDER BY id DESC LIMIT 300'),
        {'user_id': user_id}).scalars()
    return sorted(post_ids)


@cache.memoize(timeout=600)
def recently_upvoted_post_replies(user_id) -> List[int]:
    reply_ids = db.session.execute(text(
        'SELECT post_reply_id FROM "post_reply_vote" WHERE user_id = :user_id AND effect > 0 ORDER BY id DESC LIMIT 300'),
                                   {'user_id': user_id}).scalars()
    return sorted(reply_ids)  # sorted so that in_sorted_list can be used


@cache.memoize(timeout=600)
def recently_downvoted_post_replies(user_id) -> List[int]:
    reply_ids = db.session.execute(text(
        'SELECT post_reply_id FROM "post_reply_vote" WHERE user_id = :user_id AND effect < 0 ORDER BY id DESC LIMIT 300'),
                                   {'user_id': user_id}).scalars()
    return sorted(reply_ids)


def languages_for_form(all=False):
    used_languages = []
    other_languages = []
    if current_user.is_authenticated:
        # if they've defined which languages they read, only present those as options for writing.
        # otherwise, present their most recently used languages
        if current_user.read_language_ids is None or len(current_user.read_language_ids) == 0:
            recently_used_language_ids = db.session.execute(text("""SELECT language_id
                                                                    FROM (
                                                                        SELECT language_id, posted_at
                                                                        FROM "post"
                                                                        WHERE user_id = :user_id
                                                                        UNION ALL
                                                                        SELECT language_id, posted_at
                                                                        FROM "post_reply"
                                                                        WHERE user_id = :user_id
                                                                    ) AS subquery
                                                                    GROUP BY language_id
                                                                    ORDER BY MAX(posted_at) DESC
                                                                    LIMIT 10"""),
                                                            {'user_id': current_user.id}).scalars().all()

            # note: recently_used_language_ids is now a List, ordered with the most recently used at the top
            # but Language.query.filter(Language.id.in_(recently_used_language_ids)) isn't guaranteed to return
            # language results in the same order as that List :(
            for language_id in recently_used_language_ids:
                if language_id is not None:
                    used_languages.append((language_id, ""))
        else:
            for language in Language.query.filter(Language.id.in_(tuple(current_user.read_language_ids))).order_by(Language.name).all():
                used_languages.append((language.id, language.name))

        if not used_languages:
            id = site_language_id()
            if id:
                used_languages.append((id, ""))

    other_languages = []
    for language in Language.query.order_by(Language.name).all():
        try:
            i = used_languages.index((language.id, ""))
            used_languages[i] = (language.id, language.name)
        except:
            if all and language.code != "und":
                other_languages.append((language.id, language.name))

    return used_languages + other_languages


def flair_for_form(community_id):
    result = []
    for flair in CommunityFlair.query.filter(CommunityFlair.community_id == community_id).order_by(CommunityFlair.flair):
        result.append((flair.id, flair.flair))
    return result


def find_flair_id(flair: str, community_id: int) -> int | None:
    flair = CommunityFlair.query.filter(CommunityFlair.community_id == community_id,
                                        CommunityFlair.flair == flair.strip()).first()
    if flair:
        return flair.id
    else:
        return None


def site_language_id(site=None):
    if site is not None and site.language_id:
        return site.language_id
    if g and hasattr(g, 'site') and g.site.language_id:
        return g.site.language_id
    else:
        english = Language.query.filter(Language.code == 'en').first()
        return english.id if english else None


def read_language_choices() -> List[tuple]:
    result = []
    for language in Language.query.order_by(Language.name).all():
        result.append((language.id, language.name))
    return result


def actor_contains_blocked_words(actor: str):
    actor = actor.lower().strip()
    blocked_words = get_setting('actor_blocked_words')
    if blocked_words and blocked_words.strip() != '':
        for blocked_word in blocked_words.split('\n'):
            blocked_word = blocked_word.lower().strip()
            if blocked_word in actor:
                return True
    return False


def actor_profile_contains_blocked_words(user: User) -> bool:
    if user is None or not isinstance(user, User):
        return False
    blocked_words = get_setting('actor_bio_blocked_words')
    if blocked_words and blocked_words.strip() != '':
        for blocked_word in blocked_words.split('\n'):
            blocked_word = blocked_word.lower().strip()
            if user.about_html and blocked_word in user.about_html.lower():
                return True
    return False


def add_to_modlog(action: str, actor: User, target_user: User = None, reason: str = '',
                  community: Community = None, post: Post = None, reply: PostReply = None,
                  link: str = '', link_text: str = ''):
    """ Adds a new entry to the Moderation Log """
    if action not in ModLog.action_map.keys():
        raise Exception('Invalid action: ' + action)
    if actor.is_instance_admin() or actor.is_admin() or actor.is_staff():
        action_type = 'admin'
    else:
        action_type = 'mod'
    community_id = community.id if community else None
    target_user_id = target_user.id if target_user else None
    post_id = post.id if post else None
    reply_id = reply.id if reply else None
    reason = shorten_string(reason, 512)
    db.session.add(ModLog(user_id=actor.id, type=action_type, action=action, target_user_id=target_user_id,
                          community_id=community_id, post_id=post_id, reply_id=reply_id,
                          reason=reason, link=link, link_text=link_text, public=get_setting('public_modlog', False)))
    db.session.commit()


def authorise_api_user(auth, return_type=None, id_match=None) -> User | int:
    if not auth:
        raise Exception('incorrect_login')
    token = auth[7:]  # remove 'Bearer '

    if current_app.debug and request.host == 'piefed.ngrok.app':
        if return_type and return_type == 'model':
            return User.query.get(1)
        else:
            return 1

    decoded = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
    if decoded:
        user_id = decoded['sub']
        user = User.query.filter_by(id=user_id, ap_id=None, verified=True, banned=False, deleted=False).one()
        if user.password_updated_at:
            issued_at_time = decoded['iat']
            password_updated_time = int(user.password_updated_at.timestamp())
            if issued_at_time < password_updated_time:
                raise Exception('incorrect_login')
        if id_match and user.id != id_match:
            raise Exception('incorrect_login')
        if return_type and return_type == 'model':
            return user
        else:
            return user.id


# Set up a new SQLAlchemy session specifically for Celery tasks
def get_task_session() -> Session:
    # Use the same engine as the main app, but create an independent session
    return Session(bind=db.engine)


@contextmanager
def patch_db_session(task_session):
    """Temporarily replace db.session with task_session for functions that use it internally"""
    from app import db
    from flask import has_request_context
    
    # Only patch if we're not in a Flask request context (i.e., in a Celery worker)
    if has_request_context():
        # In Flask request context, don't patch - just use the existing session
        yield
        return
    
    original_session = db.session
    
    # Create a wrapper that makes the task session work with Flask-SQLAlchemy's Model.query
    class SessionWrapper:
        def __init__(self, session):
            self._session = session
            
        def __call__(self):
            return self._session
            
        def __getattr__(self, name):
            # Handle scoped session methods that don't exist on regular Session
            if name == 'remove':
                # For task sessions, we don't want to remove since we manage the lifecycle
                return lambda: None
            return getattr(self._session, name)
    
    db.session = SessionWrapper(task_session)
    try:
        yield
    finally:
        db.session = original_session


def get_redis_connection(connection_string=None) -> redis.Redis:
    if connection_string is None:
        connection_string = current_app.config['CACHE_REDIS_URL']
    if connection_string.startswith('unix://'):
        unix_socket_path, db, password = parse_redis_pipe_string(connection_string)
        return redis.Redis(unix_socket_path=unix_socket_path, db=db, password=password, decode_responses=True)
    else:
        host, port, db, password = parse_redis_socket_string(connection_string)
        return redis.Redis(host=host, port=port, db=db, password=password, decode_responses=True)


def parse_redis_pipe_string(connection_string: str):
    if connection_string.startswith('unix://'):
        # Parse the connection string
        parsed_url = urlparse(connection_string)

        # Extract the path (Unix socket path)
        unix_socket_path = parsed_url.path

        # Extract query parameters (if any)
        query_params = parse_qs(parsed_url.query)

        # Extract database number (default to 0 if not provided)
        db = int(query_params.get('db', [0])[0])

        # Extract password (if provided)
        password = query_params.get('password', [None])[0]

        return unix_socket_path, db, password


def parse_redis_socket_string(connection_string: str):
    # Parse the connection string
    parsed_url = urlparse(connection_string)

    # Extract password
    password = parsed_url.password

    # Extract host and port
    host = parsed_url.hostname
    port = parsed_url.port

    # Extract database number (default to 0 if not provided)
    db_num = int(parsed_url.path.lstrip('/') or 0)

    return host, port, db_num, password


def download_defeds(defederation_subscription_id: int, domain: str):
    if current_app.debug:
        download_defeds_worker(defederation_subscription_id, domain)
    else:
        download_defeds_worker.delay(defederation_subscription_id, domain)


@celery.task
def download_defeds_worker(defederation_subscription_id: int, domain: str):
    session = get_task_session()
    for defederation_url in retrieve_defederation_list(domain):
        session.add(BannedInstances(domain=defederation_url, reason='auto', subscription_id=defederation_subscription_id))
    session.commit()
    session.close()


def retrieve_defederation_list(domain: str) -> List[str]:
    result = []
    software = instance_software(domain)
    if software == 'lemmy' or software == 'piefed':
        try:
            response = get_request(f'https://{domain}/api/v3/federated_instances')
        except:
            response = None
        if response and response.status_code == 200:
            instance_data = response.json()
            for row in instance_data['federated_instances']['blocked']:
                result.append(row['domain'])
    else:  # Assume mastodon-compatible API
        try:
            response = get_request(f'https://{domain}/api/v1/instance/domain_blocks')
        except:
            response = None
        if response and response.status_code == 200:
            instance_data = response.json()
            for row in instance_data:
                result.append(row['domain'])

    return result


def instance_software(domain: str):
    instance = Instance.query.filter(Instance.domain == domain).first()
    return instance.software.lower() if instance else ''


# ----------------------------------------------------------------------
# Return contents of referrer with a fallback
def referrer(default: str = None) -> str:
    if request.args.get('next'):
        return request.args.get('next')
    if request.referrer and current_app.config['SERVER_NAME'] in request.referrer:
        return request.referrer
    if default:
        return default
    return url_for('main.index')


def create_captcha(length=4):
    code = ""
    for i in range(length):
        code += str(random.choice(['2', '3', '4', '5', '6', '8', '9']))

    imagedata = ImageCaptcha().generate(code)
    image = "data:image/jpeg;base64," + base64.encodebytes(imagedata.read()).decode()

    audiodata = AudioCaptcha().generate(code)
    audio = "data:audio/wav;base64," + base64.encodebytes(audiodata).decode()

    uuid = os.urandom(12).hex()

    redis_client = get_redis_connection()
    redis_client.set("captcha_" + uuid, code, ex=30 * 60)

    return {"uuid": uuid, "audio": audio, "image": image}


def decode_captcha(uuid: str, code: str):
    re_uuid = re.compile(r'^([a-fA-F0-9]{24})$')
    try:
        if not re.fullmatch(re_uuid, uuid):
            return False
    except TypeError:
        return False

    redis_client = get_redis_connection()
    saved_code = redis_client.get("captcha_" + uuid)
    redis_client.delete("captcha_" + uuid)
    if saved_code is not None:
        if code.lower() == saved_code.lower():
            return True
    return False


class CaptchaField(StringField):
    widget = TextInput()

    def __call__(self, *args, **kwargs):
        self.data = ''
        captcha = create_captcha()
        input_field_html = super(CaptchaField, self).__call__(*args, **kwargs)
        return Markup("""<input type="hidden" name="captcha_uuid" value="{uuid}" id="captcha-uuid">
                         <img src="{image}" class="border mb-2" id="captcha-image">
                         <audio src="{audio}" type="audio/wav" controls></audio>
                         <!--<button type="button" id="captcha-refresh-button">Refresh</button>-->
                         <br />
                      """).format(uuid=captcha["uuid"], image=captcha["image"],
                                  audio=captcha["audio"]) + input_field_html

    def post_validate(self, form, validation_stopped):
        if decode_captcha(request.form.get('captcha_uuid', None), self.data):
            pass
        else:
            raise ValidationError(_l('Wrong Captcha text.'))


user2_cache = {}


def wilson_confidence_lower_bound(ups, downs, z = 1.281551565545) -> float:
    if ups is None or ups < 0:
        ups = 0
    if downs is None or downs < 0:
        downs = 0
    n = ups + downs
    if n == 0:
        return 0.0
    p = float(ups) / n
    left = p + 1 / (2 * n) * z * z
    right = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    under = 1 + 1 / n * z * z
    return (left - right) / under


def jaccard_similarity(user1_upvoted: set, user2_id: int):
    if user2_id not in user2_cache:
        user2_upvoted_posts = ['post/' + str(id) for id in recently_upvoted_posts(user2_id)]
        user2_upvoted_replies = ['reply/' + str(id) for id in recently_upvoted_post_replies(user2_id)]
        user2_cache[user2_id] = set(user2_upvoted_posts + user2_upvoted_replies)

    user2_upvoted = user2_cache[user2_id]

    if len(user2_upvoted) > 12:
        intersection = len(user1_upvoted.intersection(user2_upvoted))
        union = len(user1_upvoted.union(user2_upvoted))

        return (intersection / union) * 100
    else:
        return 0


def dedupe_post_ids(post_ids: List[Tuple[int, Optional[List[int]], int, int]], is_all_view: bool) -> List[int]:
    # Remove duplicate posts based on cross-posting rules
    # post_ids is a list of tuples: (post_id, cross_post_ids, user_id, reply_count)
    result = []
    if post_ids is None or len(post_ids) == 0:
        return result

    seen_before = set()  # Track which post IDs we've already processed to avoid duplicates
    priority = set()     # Track post IDs that should be prioritized (kept over their cross-posts)
    lvp = low_value_reposters()

    for post_id in post_ids:
        # If this post has cross-posts AND the author is a low-value reposter
        # Only applies to 'All' feed to avoid suppressing posts when alternatives aren't viewable to user
        if post_id[1] and is_all_view and post_id[2] in lvp:
            # Mark this post as seen (will be filtered out)
            seen_before.add(post_id[0])
            # Find the cross-post with the most replies and prioritize only that one
            cross_posts = list(post_id[1])
            if cross_posts:
                # Find reply counts for each cross-post from remaining tuples
                cross_post_replies = {}
                for other_post in post_ids:
                    if other_post[0] in cross_posts:
                        cross_post_replies[other_post[0]] = other_post[3]

                # Prioritize the cross-post with most replies
                best_cross_post = max(cross_posts, key=lambda x: cross_post_replies.get(x, 0))
                priority.add(best_cross_post)
                # Mark all other cross-posts as seen to avoid duplicates
                seen_before.update(cp for cp in cross_posts if cp != best_cross_post)
        # If this post has cross-posts AND it's not already prioritized or seen
        elif post_id[1] and post_id[0] not in priority and post_id[0] not in seen_before:
            # Mark all its cross-posts as seen (they'll be filtered out)
            seen_before.update(post_id[1])
            # Remove cross-posts from priority set (this post takes precedence)
            priority.difference_update(post_id[1])

        # Only add the post to results if we haven't seen it before
        if post_id[0] not in seen_before:
            result.append(post_id[0])
    return result


def paginate_post_ids(post_ids, page: int, page_length: int):
    start = page * page_length
    end = start + page_length
    return post_ids[start:end]


def get_deduped_post_ids(result_id: str, community_ids: List[int], sort: str) -> List[int]:
    from app import redis_client
    if community_ids is None or len(community_ids) == 0:
        return []
    if result_id:
        if redis_client.exists(result_id):
            return json.loads(redis_client.get(result_id))

    if community_ids[0] == -1:  # A special value meaning to get posts from all communities
        post_id_sql = 'SELECT p.id, p.cross_posts, p.user_id, p.reply_count FROM "post" as p\nINNER JOIN "community" as c on p.community_id = c.id\n'
        post_id_where = ['c.banned is false AND c.show_all is true']
        if current_user.is_authenticated and current_user.hide_low_quality:
            post_id_where.append('c.low_quality is false')
        params = {}
    else:
        post_id_sql = 'SELECT p.id, p.cross_posts, p.user_id, p.reply_count FROM "post" as p\nINNER JOIN "community" as c on p.community_id = c.id\n'
        post_id_where = ['c.id IN :community_ids AND c.banned is false ']
        params = {'community_ids': tuple(community_ids)}

    # filter out posts in communities where the community name is objectionable to them
    if current_user.is_authenticated:
        filtered_out_community_ids = filtered_out_communities(current_user)
        if len(filtered_out_community_ids):
            post_id_where.append('c.id NOT IN :filtered_out_community_ids ')
            params['filtered_out_community_ids'] = tuple(filtered_out_community_ids)

    # filter out nsfw and nsfl if desired
    if current_user.is_anonymous:
        post_id_where.append('p.from_bot is false AND p.nsfw is false AND p.nsfl is false AND p.deleted is false AND p.status > 0 ')
    else:
        if current_user.ignore_bots == 1:
            post_id_where.append('p.from_bot is false ')
        if current_user.hide_nsfl == 1:
            post_id_where.append('p.nsfl is false ')
        if current_user.hide_nsfw == 1:
            post_id_where.append('p.nsfw is false')
        if current_user.hide_read_posts:
            post_id_where.append('p.id NOT IN (SELECT read_post_id FROM "read_posts" WHERE user_id = :user_id) ')
            params['user_id'] = current_user.id

        # Language filter
        if current_user.read_language_ids and len(current_user.read_language_ids) > 0:
            post_id_where.append('(p.language_id IN :read_language_ids OR p.language_id is null) ')
            params['read_language_ids'] = tuple(current_user.read_language_ids)

        post_id_where.append('p.deleted is false AND p.status > 0 ')

        # filter blocked domains and instances
        domains_ids = blocked_domains(current_user.id)
        if domains_ids:
            post_id_where.append('(p.domain_id NOT IN :domain_ids OR p.domain_id is null) ')
            params['domain_ids'] = tuple(domains_ids)
        instance_ids = blocked_instances(current_user.id)
        if instance_ids:
            post_id_where.append('(p.instance_id NOT IN :instance_ids OR p.instance_id is null) ')
            params['instance_ids'] = tuple(instance_ids)
        blocked_community_ids = blocked_communities(current_user.id)
        if blocked_community_ids:
            post_id_where.append('p.community_id NOT IN :blocked_community_ids ')
            params['blocked_community_ids'] = tuple(blocked_community_ids)
        # filter blocked users
        blocked_accounts = blocked_users(current_user.id)
        if blocked_accounts:
            post_id_where.append('p.user_id NOT IN :blocked_accounts ')
            params['blocked_accounts'] = tuple(blocked_accounts)
        # filter communities banned from
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            post_id_where.append('p.community_id NOT IN :banned_from ')
            params['banned_from'] = tuple(banned_from)
    # sorting
    post_id_sort = ''
    if sort == '' or sort == 'hot':
        post_id_sort = 'ORDER BY p.ranking DESC, p.posted_at DESC'
    elif sort == 'scaled':
        post_id_sort = 'ORDER BY p.ranking_scaled DESC, p.ranking DESC, p.posted_at DESC'
        post_id_where.append('p.ranking_scaled is not null ')
    elif sort.startswith('top'):
        if sort != 'top_all':
            post_id_where.append('p.posted_at > :top_cutoff ')
        post_id_sort = 'ORDER BY p.up_votes - p.down_votes DESC'
        if sort == 'top_1h':
            params['top_cutoff'] = utcnow() - timedelta(hours=1)
        elif sort == 'top_6h':
            params['top_cutoff'] = utcnow() - timedelta(hours=6)
        elif sort == 'top_12h':
            params['top_cutoff'] = utcnow() - timedelta(hours=12)
        elif sort == 'top':
            params['top_cutoff'] = utcnow() - timedelta(hours=24)
        elif sort == 'top_1w':
            params['top_cutoff'] = utcnow() - timedelta(days=7)
        elif sort == 'top_1m':
            params['top_cutoff'] = utcnow() - timedelta(days=28)
        elif sort == 'top_1y':
            params['top_cutoff'] = utcnow() - timedelta(days=365)
        elif sort != 'top_all':
            params['top_cutoff'] = utcnow() - timedelta(days=1)
    elif sort == 'new':
        post_id_sort = 'ORDER BY p.posted_at DESC'
    elif sort == 'old':
        post_id_sort = 'ORDER BY p.posted_at ASC'
    elif sort == 'active':
        post_id_sort = 'ORDER BY p.last_active DESC'
    final_post_id_sql = f"{post_id_sql} WHERE {' AND '.join(post_id_where)}\n{post_id_sort}\nLIMIT 1000"
    post_ids = db.session.execute(text(final_post_id_sql), params).all()
    post_ids = dedupe_post_ids(post_ids, community_ids[0] == -1)

    if current_user.is_authenticated:
        redis_client.set(result_id, json.dumps(post_ids), ex=86400)  # 86400 is 1 day
    return post_ids


def post_ids_to_models(post_ids: List[int], sort: str):
    posts = Post.query.filter(Post.id.in_([p for p in post_ids]))
    # Final sorting
    if sort == '' or sort == 'hot':
        posts = posts.order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort == 'scaled':
        posts = posts.order_by(desc(Post.ranking_scaled)).order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort.startswith('top'):
        posts = posts.order_by(desc(Post.up_votes - Post.down_votes))
    elif sort == 'new':
        posts = posts.order_by(desc(Post.posted_at))
    elif sort == 'old':
        posts = posts.order_by(asc(Post.posted_at))
    elif sort == 'active':
        posts = posts.order_by(desc(Post.last_active))
    return posts


def store_files_in_s3():
    return current_app.config['S3_ACCESS_KEY'] != '' and current_app.config['S3_ACCESS_SECRET'] != '' and \
        current_app.config['S3_ENDPOINT'] != ''


def move_file_to_s3(file_id, s3):
    if store_files_in_s3():
        file: File = File.query.get(file_id)
        if file:
            if file.thumbnail_path and not file.thumbnail_path.startswith('http') and file.thumbnail_path.startswith(
                    'app/static/media'):
                if os.path.isfile(file.thumbnail_path):
                    content_type = guess_mime_type(file.thumbnail_path)
                    new_path = file.thumbnail_path.replace('app/static/media/', "")
                    s3.upload_file(file.thumbnail_path, current_app.config['S3_BUCKET'], new_path,
                                   ExtraArgs={'ContentType': content_type})
                    os.unlink(file.thumbnail_path)
                    file.thumbnail_path = f"https://{current_app.config['S3_PUBLIC_URL']}/{new_path}"
                    db.session.commit()

            if file.file_path and not file.file_path.startswith('http') and file.file_path.startswith(
                    'app/static/media'):
                if os.path.isfile(file.file_path):
                    content_type = guess_mime_type(file.file_path)
                    new_path = file.file_path.replace('app/static/media/', "")
                    s3.upload_file(file.file_path, current_app.config['S3_BUCKET'], new_path,
                                   ExtraArgs={'ContentType': content_type})
                    os.unlink(file.file_path)
                    file.file_path = f"https://{current_app.config['S3_PUBLIC_URL']}/{new_path}"
                    db.session.commit()

            if file.source_url and not file.source_url.startswith('http') and file.source_url.startswith(
                    'app/static/media'):
                if os.path.isfile(file.source_url):
                    content_type = guess_mime_type(file.source_url)
                    new_path = file.source_url.replace('app/static/media/', "")
                    s3.upload_file(file.source_url, current_app.config['S3_BUCKET'], new_path,
                                   ExtraArgs={'ContentType': content_type})
                    os.unlink(file.source_url)
                    file.source_url = f"https://{current_app.config['S3_PUBLIC_URL']}/{new_path}"
                    db.session.commit()


def days_to_add_for_next_month(today):
    # Calculate the new month and year
    new_month = today.month + 1
    new_year = today.year

    if new_month > 12:
        new_month = 1
        new_year += 1

    # Get the last day of the new month
    if new_month in {1, 3, 5, 7, 8, 10, 12}:
        last_day = 31
    elif new_month in {4, 6, 9, 11}:
        last_day = 30
    else:  # February
        # Check for leap year
        if (new_year % 4 == 0 and new_year % 100 != 0) or (new_year % 400 == 0):
            last_day = 29
        else:
            last_day = 28

    # Calculate the new day
    new_day = min(today.day, last_day)

    # Calculate the number of days to add
    days_to_add = (datetime(new_year, new_month, new_day) - today).days + 1

    return days_to_add


def find_next_occurrence(post: Post) -> timedelta:
    if post.repeat is not None and post.repeat != 'none':
        if post.repeat == 'daily':
            return timedelta(days=1)
        elif post.repeat == 'weekly':
            return timedelta(days=7)
        elif post.repeat == 'monthly':
            days_to_add = days_to_add_for_next_month(utcnow())
            return timedelta(days=days_to_add)

    return timedelta(seconds=0)


def notif_id_to_string(notif_id) -> str:
    # -- user level ---
    if notif_id == NOTIF_USER:
        return _('User')
    if notif_id == NOTIF_COMMUNITY:
        return _('Community')
    if notif_id == NOTIF_TOPIC:
        return _('Topic/feed')
    if notif_id == NOTIF_POST:
        return _('Comment')
    if notif_id == NOTIF_REPLY:
        return _('Comment')
    if notif_id == NOTIF_FEED:
        return _('Topic/feed')
    if notif_id == NOTIF_MENTION:
        return _('Comment')
    if notif_id == NOTIF_MESSAGE:
        return _('Chat')
    if notif_id == NOTIF_BAN:
        return _('Admin')
    if notif_id == NOTIF_UNBAN:
        return _('Admin')
    if notif_id == NOTIF_NEW_MOD:
        return _('Admin')

    # --- mod/admin level ---
    if notif_id == NOTIF_REPORT:
        return _('Admin')

    # --- admin level ---
    if notif_id == NOTIF_REPORT_ESCALATION:
        return _('Admin')
    if notif_id == NOTIF_REGISTRATION:
        return _('Admin')

    # --model/db default--
    if notif_id == NOTIF_DEFAULT:
        return _('All')


@cache.memoize(timeout=6000)
def filtered_out_communities(user: User) -> List[int]:
    if user.community_keyword_filter:
        communities = Community.query
        for community_filter in user.community_keyword_filter.split(','):
            if community_filter.strip():
                communities = communities.filter(or_(Community.name.ilike(f"%{community_filter}%"),
                                                     Community.title.ilike(f"%{community_filter}%"))
                                                 )

        return [community.id for community in communities.all()]
    else:
        return []


@cache.memoize(timeout=300)
def retrieve_image_hash(image_url):
    def fetch_hash(retries_left):
        try:
            response = get_request(current_app.config['IMAGE_HASHING_ENDPOINT'], {'image_url': image_url})
            if response.status_code == 200:
                result = response.json()
                if result.get('quality', 0) >= 70:
                    return result.get('pdq_hash_binary', '')
            elif response.status_code == 429 and retries_left > 0:
                sleep(random.uniform(1, 3))
                return fetch_hash(retries_left - 1)
        except httpx.HTTPError as e:
            current_app.logger.warning(f"Error retrieving image hash: {e}")
        except httpx.ReadError as e:
            current_app.logger.warning(f"Error retrieving image hash: {e}")
        finally:
            try:
                response.close()
            except:
                pass
        return None

    return fetch_hash(retries_left=2)


BINARY_RE = re.compile(r'^[01]+$')  # used in hash_matches_blocked_image()


def hash_matches_blocked_image(hash: str) -> bool:
    # calculate hamming distance between the provided hash and the hashes of all the blocked images.
    # the hamming distance is a value between 0 and 256 indicating how many bits are different.
    # 15 is the number of different bits we will accept. Anything less than that and we consider the images to be the same.

    # only accept a string with 0 and 1 in it. This makes it safe to use sql injection-prone code below, which greatly simplifies the conversion of binary strings
    if not BINARY_RE.match(hash):
        current_app.logger.warning(f"Invalid binary hash: {hash}")
        return False

    sql = f"""SELECT id FROM blocked_image WHERE length(replace((hash # B'{hash}')::text, '0', '')) < 15;"""
    blocked_images = db.session.execute(text(sql)).scalars().first()
    return blocked_images is not None


def posts_with_blocked_images() -> List[int]:
    sql = """
    SELECT DISTINCT post.id
    FROM post
    JOIN file ON post.image_id = file.id
    JOIN blocked_image ON (
        length(replace((file.hash # blocked_image.hash)::text, '0', ''))
    ) < 15
    WHERE post.deleted = false AND file.hash is not null
    """

    return list(db.session.execute(text(sql)).scalars())


def notify_admin(title, url, author_id, notif_type, subtype, targets):
    for admin in Site.admins():
        notify = Notification(title=title, url=url,
                              user_id=admin.id,
                              author_id=author_id, notif_type=notif_type,
                              subtype=subtype,
                              targets=targets)
        admin.unread_notifications += 1
        db.session.add(notify)
    db.session.commit()


def reported_posts(user_id, admin_ids) -> List[int]:
    if user_id is None:
        return []
    if user_id in admin_ids:
        post_ids = list(db.session.execute(text('SELECT id FROM "post" WHERE reports > 0')).scalars())
    else:
        community_ids = [community.id for community in moderating_communities(user_id)]
        if len(community_ids) > 0:
            post_ids = list(db.session.execute(text('SELECT id FROM "post" WHERE reports > 0 AND community_id IN :community_ids'),
                                               {'community_ids': tuple(community_ids)}).scalars())
        else:
            return []
    return post_ids


def reported_post_replies(user_id, admin_ids) -> List[int]:
    if user_id is None:
        return []
    if user_id in admin_ids:
        post_reply_ids = list(db.session.execute(text('SELECT id FROM "post_reply" WHERE reports > 0')).scalars())
    else:
        community_ids = [community.id for community in moderating_communities(user_id)]
        post_reply_ids = list(db.session.execute(text('SELECT id FROM "post_reply" WHERE reports > 0 AND community_id IN :community_ids'),
                                                 {'community_ids': community_ids}).scalars())
    return post_reply_ids


def possible_communities():
    which_community = {}
    joined = joined_communities(current_user.get_id())
    moderating = moderating_communities(current_user.get_id())
    comms = []
    already_added = set()
    for c in moderating:
        if c.id not in already_added:
            comms.append((c.id, c.display_name()))
            already_added.add(c.id)
    if len(comms) > 0:
        which_community['Moderating'] = comms
    comms = []
    for c in joined:
        if c.id not in already_added:
            comms.append((c.id, c.display_name()))
            already_added.add(c.id)
    if len(comms) > 0:
        which_community['Joined communities'] = comms
    comms = []
    for c in db.session.query(Community.id, Community.ap_id, Community.title, Community.ap_domain).filter(
            Community.banned == False).order_by(Community.title).all():
        if c.id not in already_added:
            if c.ap_id is None:
                display_name = c.title
            else:
                display_name = f"{c.title}@{c.ap_domain}"
            comms.append((c.id, display_name))
            already_added.add(c.id)
    if len(comms) > 0:
        which_community['Others'] = comms
    return which_community


def user_notes(user_id):
    if user_id is None:
        return {}
    result = {}
    for note in UserNote.query.filter(UserNote.user_id == user_id).all():
        result[note.target_id] = note.body
    return result


@event.listens_for(User.unread_notifications, 'set')
def on_unread_notifications_set(target, value, oldvalue, initiator):
    if value != oldvalue and current_app.config['NOTIF_SERVER']:
        publish_sse_event(f"notifications:{target.id}", json.dumps({'num_notifs': value}))


def publish_sse_event(key, value):
    r = get_redis_connection()
    r.publish(key, value)


def apply_feed_url_rules(self):
    if '-' in self.url.data.strip():
        self.url.errors.append(_l('- cannot be in Url. Use _ instead?'))
        return False

    if not self.public.data and not '/' in self.url.data.strip():
        self.url.data = self.url.data.strip().lower() + '/' + current_user.user_name.lower()
    elif self.public.data and '/' in self.url.data.strip():
        self.url.data = self.url.data.strip().split('/', 1)[0]
    else:
        self.url.data = self.url.data.strip().lower()

    # Allow alphanumeric characters and underscores (a-z, A-Z, 0-9, _)
    if self.public.data:
        regex = r'^[a-zA-Z0-9_]+$'
    else:
        regex = r'^[a-zA-Z0-9_]+(?:/' + current_user.user_name.lower() + ')?$'
    if not re.match(regex, self.url.data):
        self.url.errors.append(_l('Feed urls can only contain letters, numbers, and underscores.'))
        return False

    try:
        self.feed_id
    except AttributeError:
        feed = Feed.query.filter(Feed.name == self.url.data).first()
    else:
        feed = Feed.query.filter(Feed.name == self.url.data).filter(Feed.id != self.feed_id).first()
    if feed is not None:
        self.url.errors.append(_l('A Feed with this url already exists.'))
        return False
    return True


# notification destination user helper function to make sure the
# notification text is stored in the database using the language of the
# recipient, rather than the language of the originator
def get_recipient_language(user_id):
    lang_to_use = ''

    # look up the user in the db based on the id
    recipient = User.query.get(user_id)

    # if the user has language_id set, use that
    if recipient.language_id:
        lang = Language.query.get(recipient.language_id)
        lang_to_use = lang.code

    # else if the user has interface_language use that
    elif recipient.interface_language:
        lang_to_use = recipient.interface_language

    # else default to english
    else:
        lang_to_use = 'en'

    return lang_to_use


def safe_order_by(sort_param: str, model, allowed_fields: set):
    """
    Returns a SQLAlchemy order_by clause for a given model and sort parameter. Guards against SQL injection.

    Parameters:
        sort_param (str): The user-supplied sort string (e.g., 'name desc').
        model (db.Model): The SQLAlchemy model class to sort on.
        allowed_fields (set): A set of allowed field names (str) from the model.

    Returns:
        A SQLAlchemy order_by clause (asc/desc column expression).

    Example usage:
        sort_param = request.args.get('sort_by', 'post_reply_count desc')
        allowed_fields = {'name', 'created_at', 'post_reply_count'}

        communities = communities.order_by(
            safe_order_by(sort_param, Community, allowed_fields)
        )
    """
    parts = sort_param.strip().split()
    field_name = parts[0]
    direction = parts[1].lower() if len(parts) > 1 else 'asc'

    if field_name in allowed_fields and hasattr(model, field_name):
        column = getattr(model, field_name)
        if direction == 'desc':
            return desc(column)
        else:
            return asc(column)
    else:
        # Return a default safe order if invalid input
        default_field = next(iter(allowed_fields))
        return desc(getattr(model, default_field))



def render_from_tpl(tpl: str) -> str:
    """
    Replace tags in `template` like {% week %}, {%day%}, {% month %}, {%year%}
    with the corresponding values.
    """
    date = utcnow()

    # Words to replace
    replacements = {
        "week": f"{date.isocalendar()[1]:02d}",
        "day": f"{date.day:02d}",
        "month": f"{date.month:02d}",
        "year": str(date.year)
    }

    # Regex to find {%   word   %}, spaces will be ignored
    pattern = re.compile(r"\{\%\s*(week|day|month|year)\s*\%\}")

    # Substitute each match with its replacement
    def _sub(match):
        key = match.group(1)
        return replacements.get(key, match.group(0))

    return pattern.sub(_sub, tpl)


@lru_cache(maxsize=None)
def get_timezones():
    """
    returns an OrderedDict of timezones:
    {
       'Africa': [('Africa/Abidjan','Africa/Abidjan'), ...],
       'America': [('America/New_York','America/New_York'), ...],
       ...
    }
    """
    by_region = OrderedDict()
    for tz in sorted(available_timezones()):
        if '/' in tz:
            region, _ = tz.split('/', 1)
            if region in ['Arctic', 'Atlantic', 'Etc', 'Other']:
                continue
            by_region.setdefault(region, []).append((tz, tz))
    return by_region


@cache.memoize(timeout=6000)
def low_value_reposters() -> List[int]:
    result = db.session.execute(text('SELECT id FROM "user" WHERE bot = true or bot_override = true or suppress_crossposts = true')).scalars()
    return list(result)


def is_valid_xml_utf8(pystring):
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
