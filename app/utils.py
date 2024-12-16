from __future__ import annotations

import bisect
import hashlib
import mimetypes
import random
import urllib
from collections import defaultdict
from datetime import datetime, timedelta, date
from time import sleep
from typing import List, Literal, Union

import httpx
import markdown2
from urllib.parse import urlparse, parse_qs, urlencode
from functools import wraps
import flask
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
import warnings
import jwt


warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
import os
from flask import current_app, json, redirect, url_for, request, make_response, Response, g, flash
from flask_babel import _
from flask_login import current_user, logout_user
from sqlalchemy import text, or_
from sqlalchemy.orm import Session
from wtforms.fields  import SelectField, SelectMultipleField
from wtforms.widgets import Select, html_params, ListWidget, CheckboxInput
from app import db, cache, httpx_client
import re
from PIL import Image, ImageOps

from app.models import Settings, Domain, Instance, BannedInstances, User, Community, DomainBlock, ActivityPubLog, IpBan, \
    Site, Post, PostReply, utcnow, Filter, CommunityMember, InstanceBlock, CommunityBan, Topic, UserBlock, Language, \
    File, ModLog, CommunityBlock


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
    if os.path.exists('static/' + filename):
        return os.path.getmtime('static/' + filename)


# do a GET request to a uri, return the result
def get_request(uri, params=None, headers=None) -> httpx.Response:
    timeout = 15 if 'washingtonpost.com' in uri else 5  # Washington Post is really slow on og:image for some reason
    if headers is None:
        headers = {'User-Agent': 'PieFed/1.0'}
    else:
        headers.update({'User-Agent': 'PieFed/1.0'})
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
        try:    # retry, this time with a longer timeout
            sleep(random.randint(3, 10))
            response = httpx_client.get(uri, params=payload_str, headers=headers, timeout=timeout * 2, follow_redirects=True)
        except Exception as e:
            current_app.logger.info(f"{uri} {connection_error}")
            raise httpx_client.ReadError(f"HTTPReadError: {str(e)}") from connection_error
    except httpx.HTTPError as read_timeout:
        try:    # retry, this time with a longer timeout
            sleep(random.randint(3, 10))
            response = httpx_client.get(uri, params=payload_str, headers=headers, timeout=timeout * 2, follow_redirects=True)
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
        headers = {'User-Agent': 'PieFed/1.0'}
    else:
        headers.update({'User-Agent': 'PieFed/1.0'})
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
        return json.loads(setting.value)


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
    common_image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.avif', '.svg+xml', '.svg+xml; charset=utf-8']
    mime_type = mime_type_using_head(url)
    if mime_type:
        mime_type_parts = mime_type.split('/')
        return f'.{mime_type_parts[1]}' in common_image_extensions
    else:
        parsed_url = urlparse(url)
        path = parsed_url.path.lower()
        return any(path.endswith(extension) for extension in common_image_extensions)


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
    video_hosting_sites = ['https://youtube.com', 'https://www.youtube.com', 'https://youtu.be', 'https://www.vimeo.com', 'https://www.redgifs.com/watch/']
    for starts_with in video_hosting_sites:
        if url.startswith(starts_with):
            return True

    if 'videos/watch' in url:   # PeerTube
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
    except httpx.HTTPError as e:
        return ''


# sanitise HTML using an allow list
def allowlist_html(html: str, a_target='_blank') -> str:
    if html is None or html == '':
        return ''
    allowed_tags = ['p', 'strong', 'a', 'ul', 'ol', 'li', 'em', 'blockquote', 'cite', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre',
                    'code', 'img', 'details', 'summary', 'table', 'tr', 'td', 'th', 'tbody', 'thead', 'hr', 'span', 'small', 'sub', 'sup',
                    's']
    # Parse the HTML using BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')

    # Find all plain text links, convert to <a> tags
    re_url = re.compile(r'(http[s]?://[!-~]+)')   # http(s):// followed by chars in ASCII range 33 to 126
    for tag in soup.find_all(text=True):
        tags = []
        url = False
        for t in re_url.split(tag.string):
            if re_url.match(t):
                # Avoid picking up trailing punctuation for raw URLs in text
                href = t[:-1] if t[-1] in ['.', ',', ')', '!', ':', ';', '?'] else t
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

    # avoid wrapping anchors around existing anchors (e.g. if raw URL already wrapped by remote PieFed instance)
    re_double_anchor = re.compile(r'<a href=".*?" rel="nofollow ugc" target="_blank">(<a href=".*?" rel="nofollow ugc" target="_blank">.*?<\/a>)<\/a>')
    clean_html = re_double_anchor.sub(r'\1', clean_html)

    # avoid returning empty anchors
    re_empty_anchor = re.compile(r'<a href="(.*?)" rel="nofollow ugc" target="_blank"><\/a>')
    clean_html = re_empty_anchor.sub(r'<a href="\1" rel="nofollow ugc" target="_blank">\1</a>', clean_html)

    # replace lemmy's spoiler markdown left in HTML
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
    clean_html = re_embedded_mp4.sub(r'<video class="responsive-video" controls preload="auto" autoplay muted loop playsinline disablepictureinpicture><source src="\1" type="video/mp4"></video>', clean_html)

    # replace <img src> for webm with <video> - treat them like a GIF (autoplay, but initially muted)
    re_embedded_webm = re.compile(r'<img .*?src="(https://.*?\.webm)".*?/>')
    clean_html = re_embedded_webm.sub(r'<video class="responsive-video" controls preload="auto" autoplay muted loop playsinline disablepictureinpicture><source src="\1" type="video/webm"></video>', clean_html)

    # replace <img src> for mp3 with <audio>
    re_embedded_mp3 = re.compile(r'<img .*?src="(https://.*?\.mp3)".*?/>')
    clean_html = re_embedded_mp3.sub(r'<audio controls><source src="\1" type="audio/mp3"></audio>', clean_html)

    # replace the 'static' for images hotlinked to fandom sites with 'vignette'
    re_fandom_hotlink = re.compile(r'<img alt="(.*?)" loading="lazy" src="https://static.wikia.nocookie.net')
    clean_html = re_fandom_hotlink.sub(r'<img alt="\1" loading="lazy" src="https://vignette.wikia.nocookie.net', clean_html)

    return clean_html


# use this for Markdown irrespective of origin, as it can deal with both soft break newlines ('\n' used by PieFed) and hard break newlines ('  \n' or ' \\n')
# ' \\n' will create <br /><br /> instead of just <br />, but hopefully that's acceptable.
def markdown_to_html(markdown_text, anchors_new_tab=True) -> str:
    if markdown_text:
        raw_html = markdown2.markdown(markdown_text,
                    extras={'middle-word-em': False, 'tables': True, 'fenced-code-blocks': True, 'strike': True, 'breaks': {'on_newline': True, 'on_backslash': True}})
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


def shorten_string(input_str, max_length=50):
    if input_str:
        if len(input_str) <= max_length:
            return input_str
        else:
            return input_str[:max_length - 3] + '…'
    else:
        return ''


def shorten_url(input: str, max_length=20):
    return shorten_string(input.replace('https://', '').replace('http://', ''))


# the number of digits in a number. e.g. 1000 would be 4
def digits(input: int) -> int:
    return len(shorten_number(input))


@cache.memoize(timeout=50)
def user_access(permission: str, user_id: int) -> bool:
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


@cache.memoize(timeout=86400)
def communities_banned_from(user_id: int) -> List[int]:
    community_bans = CommunityBan.query.filter(CommunityBan.user_id == user_id).all()
    return [cb.community_id for cb in community_bans]


@cache.memoize(timeout=86400)
def blocked_domains(user_id) -> List[int]:
    blocks = DomainBlock.query.filter_by(user_id=user_id)
    return [block.domain_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_communities(user_id) -> List[int]:
    blocks = CommunityBlock.query.filter_by(user_id=user_id)
    return [block.community_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_instances(user_id) -> List[int]:
    blocks = InstanceBlock.query.filter_by(user_id=user_id)
    return [block.instance_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_users(user_id) -> List[int]:
    blocks = UserBlock.query.filter_by(blocker_id=user_id)
    return [block.blocked_id for block in blocks]


@cache.memoize(timeout=86400)
def blocked_phrases() -> List[str]:
    site = Site.query.get(1)
    if site.blocked_phrases:
        return [phrase for phrase in site.blocked_phrases.split('\n') if phrase != '']
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
    parts = directory.split('/')
    rebuild_directory = ''
    for part in parts:
        rebuild_directory += part
        if not os.path.isdir(rebuild_directory):
            os.mkdir(rebuild_directory)
        rebuild_directory += '/'


def mimetype_from_url(url):
    parsed_url = urlparse(url)
    path = parsed_url.path.split('?')[0]  # Strip off anything after '?'
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type


def validation_required(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if current_user.verified:
            return func(*args, **kwargs)
        else:
            return redirect(url_for('auth.validation_required'))
    return decorated_view


def permission_required(permission):
    def decorator(func):
        @wraps(func)
        def decorated_view(*args, **kwargs):
            if user_access(permission, current_user.id):
                return func(*args, **kwargs)
            else:
                # Handle the case where the user doesn't have the required permission
                return redirect(url_for('auth.permission_denied'))

        return decorated_view

    return decorator


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
    ip = request.headers.get('X-Forwarded-For') or request.remote_addr
    if ',' in ip:  # Remove all but first ip addresses
        ip = ip[:ip.index(',')].strip()
    return ip


def user_ip_banned() -> bool:
    current_ip_address = ip_address()
    if current_ip_address:
        return current_ip_address in banned_ip_addresses()


@cache.memoize(timeout=60)
def instance_banned(domain: str) -> bool:   # see also activitypub.util.instance_blocked()
    if domain is None or domain == '':
        return False
    banned = BannedInstances.query.filter_by(domain=domain).first()
    return banned is not None


def user_cookie_banned() -> bool:
    cookie = request.cookies.get('sesion', None)
    return cookie is not None


@cache.memoize(timeout=30)
def banned_ip_addresses() -> List[str]:
    ips = IpBan.query.all()
    return [ip.ip_address for ip in ips]


def can_downvote(user, community: Community, site=None) -> bool:
    if user is None or community is None or user.banned or user.bot:
        return False

    if site is None:
        try:
            site = g.site
        except:
            site = Site.query.get(1)

    if not site.enable_downvotes:
        return False

    if community.local_only and not user.is_local():
        return False

    if user.attitude < -0.40 or user.reputation < -10:  # this should exclude about 3.7% of users.
        return False

    if community.id in communities_banned_from(user.id):
        return False

    return True


def can_upvote(user, community: Community) -> bool:
    if user is None or community is None or user.banned or user.bot:
        return False

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
    filters = Filter.query.filter_by(user_id=user_id, filter_home=True).filter(or_(Filter.expire_after > date.today(), Filter.expire_after == None))
    result = defaultdict(set)
    for filter in filters:
        keywords = [keyword.strip().lower() for keyword in filter.keywords.splitlines()]
        if filter.hide_type == 0:
            result[filter.title].update(keywords)
        else:   # type == 1 means hide completely. These posts are excluded from output by the jinja template
            result['-1'].update(keywords)
    return result


@cache.memoize(timeout=300)
def user_filters_posts(user_id):
    filters = Filter.query.filter_by(user_id=user_id, filter_posts=True).filter(or_(Filter.expire_after > date.today(), Filter.expire_after == None))
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
    filters = Filter.query.filter_by(user_id=user_id, filter_replies=True).filter(or_(Filter.expire_after > date.today(), Filter.expire_after == None))
    result = defaultdict(set)
    for filter in filters:
        keywords = [keyword.strip().lower() for keyword in filter.keywords.splitlines()]
        if filter.hide_type == 0:
            result[filter.title].update(keywords)
        else:
            result['-1'].update(keywords)
    return result


@cache.memoize(timeout=300)
def moderating_communities(user_id):
    if user_id is None or user_id == 0:
        return []
    return Community.query.join(CommunityMember, Community.id == CommunityMember.community_id).\
        filter(Community.banned == False).\
        filter(or_(CommunityMember.is_moderator == True, CommunityMember.is_owner == True)). \
        filter(CommunityMember.is_banned == False). \
        filter(CommunityMember.user_id == user_id).order_by(Community.title).all()


@cache.memoize(timeout=300)
def joined_communities(user_id):
    if user_id is None or user_id == 0:
        return []
    return Community.query.join(CommunityMember, Community.id == CommunityMember.community_id).\
        filter(Community.banned == False). \
        filter(CommunityMember.is_moderator == False, CommunityMember.is_owner == False). \
        filter(CommunityMember.is_banned == False). \
        filter(CommunityMember.user_id == user_id).order_by(Community.title).all()


@cache.memoize(timeout=3000)
def menu_topics():
    return Topic.query.filter(Topic.parent_id == None).order_by(Topic.name).all()


@cache.memoize(timeout=300)
def community_moderators(community_id):
    return CommunityMember.query.filter((CommunityMember.community_id == community_id) &
                                        (or_(
                                            CommunityMember.is_owner,
                                            CommunityMember.is_moderator
                                        ))
                                        ).all()


def finalize_user_setup(user):
    from app.activitypub.signature import RsaKeys
    user.verified = True
    user.last_seen = utcnow()
    if user.private_key is None and user.public_key is None:
        private_key, public_key = RsaKeys.generate_keypair()
        user.private_key = private_key
        user.public_key = public_key
    user.ap_profile_id = f"https://{current_app.config['SERVER_NAME']}/u/{user.user_name}".lower()
    user.ap_public_url = f"https://{current_app.config['SERVER_NAME']}/u/{user.user_name}"
    user.ap_inbox_url = f"https://{current_app.config['SERVER_NAME']}/u/{user.user_name.lower()}/inbox"
    db.session.commit()


def notification_subscribers(entity_id: int, entity_type: int) -> List[int]:
    return list(db.session.execute(text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :entity_id AND type = :type '),
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


def opengraph_parse(url):
    if '?' in url:
        url = url.split('?')
        url = url[0]
    try:
        return parse_page(url)
    except Exception as ex:
        return None


def url_to_thumbnail_file(filename) -> File:
    try:
        timeout = 15 if 'washingtonpost.com' in filename else 5 # Washington Post is really slow for some reason
        response = httpx_client.get(filename, timeout=timeout)
    except:
        return None
    if response.status_code == 200:
        content_type = response.headers.get('content-type')
        if content_type and content_type.startswith('image'):
            # Generate file extension from mime type
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
            directory = 'app/static/media/posts/' + new_filename[0:2] + '/' + new_filename[2:4]
            ensure_directory_exists(directory)
            final_place = os.path.join(directory, new_filename + file_extension)
            with open(final_place, 'wb') as f:
                f.write(response.content)
            response.close()
            Image.MAX_IMAGE_PIXELS = 89478485
            with Image.open(final_place) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((170, 170))
                img.save(final_place)
                thumbnail_width = img.width
                thumbnail_height = img.height
            return File(file_name=new_filename + file_extension, thumbnail_width=thumbnail_width,
                        thumbnail_height=thumbnail_height, thumbnail_path=final_place,
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


def parse_page(page_url, tags_to_search = KNOWN_OPENGRAPH_TAGS, fallback_tags = None):
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
        new_found_tag = soup.find("meta",  property=og_tag)
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
            return site.default_theme if site.default_theme is not None else ''
    else:
        return site.default_theme if site.default_theme is not None else ''


def theme_list():
    """ All the themes available, by looking in the templates/themes directory """
    result = [('', 'PieFed')]
    for root, dirs, files in os.walk('app/templates/themes'):
        for dir in dirs:
            if os.path.exists(f'app/templates/themes/{dir}/{dir}.json'):
                theme_settings = json.loads(file_get_contents(f'app/templates/themes/{dir}/{dir}.json'))
                if 'debug' in theme_settings and theme_settings['debug'] == True and not current_app.debug:
                  continue
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


def show_ban_message():
    flash('You have been banned.', 'error')
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
    post_ids = db.session.execute(text('SELECT post_id FROM "post_vote" WHERE user_id = :user_id AND effect > 0 ORDER BY id DESC LIMIT 1000'),
                               {'user_id': user_id}).scalars()
    return sorted(post_ids)     # sorted so that in_sorted_list can be used


@cache.memoize(timeout=600)
def recently_downvoted_posts(user_id) -> List[int]:
    post_ids = db.session.execute(text('SELECT post_id FROM "post_vote" WHERE user_id = :user_id AND effect < 0 ORDER BY id DESC LIMIT 1000'),
                               {'user_id': user_id}).scalars()
    return sorted(post_ids)


@cache.memoize(timeout=600)
def recently_upvoted_post_replies(user_id) -> List[int]:
    reply_ids = db.session.execute(text('SELECT post_reply_id FROM "post_reply_vote" WHERE user_id = :user_id AND effect > 0 ORDER BY id DESC LIMIT 1000'),
                               {'user_id': user_id}).scalars()
    return sorted(reply_ids)     # sorted so that in_sorted_list can be used


@cache.memoize(timeout=600)
def recently_downvoted_post_replies(user_id) -> List[int]:
    reply_ids = db.session.execute(text('SELECT post_reply_id FROM "post_reply_vote" WHERE user_id = :user_id AND effect < 0 ORDER BY id DESC LIMIT 1000'),
                               {'user_id': user_id}).scalars()
    return sorted(reply_ids)


def languages_for_form():
    used_languages = []
    other_languages = []
    if current_user.is_authenticated:
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

        # use 'English' as a default for brand new users (no posts or replies yet)
        # not great, but better than them accidently using 'Afaraf' (the first in a alphabetical list of languages)
        # FIXME: use site language when it is settable by admins, or anything that avoids hardcoding 'English' in
        if not used_languages:
            id = english_language_id()
            if id:
                used_languages.append((id, ""))

    for language in Language.query.order_by(Language.name).all():
        try:
            i = used_languages.index((language.id, ""))
            used_languages[i] = (language.id, language.name)
        except:
            if language.code != "und":
                other_languages.append((language.id, language.name))

    return used_languages + other_languages


def english_language_id():
    english = Language.query.filter(Language.code == 'en').first()
    return english.id if english else None


def actor_contains_blocked_words(actor):
    actor = actor.lower().strip()
    blocked_words = get_setting('actor_blocked_words')
    if blocked_words and blocked_words.strip() != '':
        for blocked_word in blocked_words.split('\n'):
            blocked_word = blocked_word.lower().strip()
            if blocked_word in actor:
                return True
    return False


def add_to_modlog(action: str, community_id: int = None, reason: str = '', link: str = '', link_text: str = ''):
    """ Adds a new entry to the Moderation Log """
    if action not in ModLog.action_map.keys():
        raise Exception('Invalid action: ' + action)
    if current_user.is_admin() or current_user.is_staff():
        action_type = 'admin'
    else:
        action_type = 'mod'
    db.session.add(ModLog(user_id=current_user.id, community_id=community_id, type=action_type, action=action,
                          reason=reason, link=link, link_text=link_text, public=get_setting('public_modlog', False)))
    db.session.commit()


def add_to_modlog_activitypub(action: str, actor: User, community_id: int = None, reason: str = '', link: str = '',
                              link_text: str = ''):
    """ Adds a new entry to the Moderation Log - identical to above except has an 'actor' parameter """
    if action not in ModLog.action_map.keys():
        raise Exception('Invalid action: ' + action)
    if actor.is_instance_admin():
        action_type = 'admin'
    else:
        action_type = 'mod'
    db.session.add(ModLog(user_id=actor.id, community_id=community_id, type=action_type, action=action,
                          reason=reason, link=link, link_text=link_text, public=get_setting('public_modlog', False)))
    db.session.commit()


def authorise_api_user(auth, return_type=None, id_match=None):
    if not auth:
        raise Exception('incorrect_login')
    token = auth[7:]     # remove 'Bearer '

    decoded = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
    if decoded:
        user_id = decoded['sub']
        issued_at = decoded['iat']      # use to check against blacklisted JWTs
        user = User.query.filter_by(id=user_id, ap_id=None, verified=True, banned=False, deleted=False).one()
        if id_match and user.id != id_match:
            raise Exception('incorrect_login')
        if return_type and return_type == 'model':
            return user
        else:
            return user.id


@cache.memoize(timeout=86400)
def community_ids_from_instances(instance_ids) -> List[int]:
    communities = Community.query.join(Instance, Instance.id == Community.instance_id).filter(Instance.id.in_(instance_ids))
    return [community.id for community in communities]


# Set up a new SQLAlchemy session specifically for Celery tasks
def get_task_session() -> Session:
    # Use the same engine as the main app, but create an independent session
    return Session(bind=db.engine)
