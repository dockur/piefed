from __future__ import annotations

import html
import os
from datetime import timedelta, datetime, timezone
from random import randint
from typing import Union, Tuple, List

import httpx
import redis
from flask import current_app, request, g, url_for, json
from flask_babel import _
from sqlalchemy import text, func, desc
from sqlalchemy.exc import IntegrityError

from app import db, cache, constants, celery
from app.models import User, Post, Community, BannedInstances, File, PostReply, AllowedInstances, Instance, utcnow, \
    PostVote, PostReplyVote, ActivityPubLog, Notification, Site, CommunityMember, InstanceRole, Report, Conversation, \
    Language, Tag, Poll, PollChoice, UserFollower, CommunityBan, CommunityJoinRequest, NotificationSubscription, Licence
from app.activitypub.signature import signed_get_request, post_request
import time
from app.constants import *
from urllib.parse import urlparse, parse_qs
from PIL import Image, ImageOps
from io import BytesIO
import pytesseract

from app.utils import get_request, allowlist_html, get_setting, ap_datetime, markdown_to_html, \
    is_image_url, domain_from_url, gibberish, ensure_directory_exists, head_request, \
    shorten_string, remove_tracking_from_link, \
    microblog_content_to_title, is_video_url, \
    notification_subscribers, communities_banned_from, actor_contains_blocked_words, \
    html_to_text, add_to_modlog_activitypub, joined_communities, \
    moderating_communities, get_task_session, is_video_hosting_site, opengraph_parse

from sqlalchemy import or_


def public_key():
    if not os.path.exists('./public.pem'):
        os.system('openssl genrsa -out private.pem 2048')
        os.system('openssl rsa -in private.pem -outform PEM -pubout -out public.pem')
    else:
        publicKey = open('./public.pem', 'r').read()
        PUBLICKEY = publicKey.replace('\n', '\\n')  # JSON-LD doesn't want to work with linebreaks,
        # but needs the \n character to know where to break the line ;)
        return PUBLICKEY


def community_members(community_id):
    sql = 'SELECT COUNT(id) as c FROM "user" as u '
    sql += 'INNER JOIN community_member cm on u.id = cm.user_id '
    sql += 'WHERE u.banned is false AND u.deleted is false AND cm.is_banned is false and cm.community_id = :community_id'
    return db.session.execute(text(sql), {'community_id': community_id}).scalar()


def users_total():
    return db.session.execute(text(
        'SELECT COUNT(id) as c FROM "user" WHERE ap_id is null AND verified is true AND banned is false AND deleted is false')).scalar()


def active_half_year():
    return db.session.execute(text(
        "SELECT COUNT(id) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '6 months' AND ap_id is null AND verified is true AND banned is false AND deleted is false")).scalar()


def active_month():
    return db.session.execute(text(
        "SELECT COUNT(id) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 month' AND ap_id is null AND verified is true AND banned is false AND deleted is false")).scalar()


def active_week():
    return db.session.execute(text(
        "SELECT COUNT(id) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 week' AND ap_id is null AND verified is true AND banned is false AND deleted is false")).scalar()


def active_day():
    return db.session.execute(text(
        "SELECT COUNT(id) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 day' AND ap_id is null AND verified is true AND banned is false AND deleted is false")).scalar()


def local_posts():
    return db.session.execute(text('SELECT COUNT(id) as c FROM "post" WHERE instance_id = 1 AND deleted is false')).scalar()


def local_comments():
    return db.session.execute(text('SELECT COUNT(id) as c FROM "post_reply" WHERE instance_id = 1 and deleted is false')).scalar()


def local_communities():
    return db.session.execute(text('SELECT COUNT(id) as c FROM "community" WHERE instance_id = 1')).scalar()


def post_to_activity(post: Post, community: Community):
    # local PieFed posts do not have a create or announce id
    create_id = post.ap_create_id if post.ap_create_id else f"https://{current_app.config['SERVER_NAME']}/activities/create/{gibberish(15)}"
    announce_id = post.ap_announce_id if post.ap_announce_id else f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}"
    activity_data = {
        "actor": community.public_url(),
        "to": [
            "https://www.w3.org/ns/activitystreams#Public"
        ],
        "object": {
            "id": create_id,
            "actor": post.author.public_url(),
            "to": [
                "https://www.w3.org/ns/activitystreams#Public"
            ],
            "object": post_to_page(post),
            "cc": [
                community.public_url()
            ],
            "type": "Create",
            "audience": community.public_url()
        },
        "cc": [
            f"{community.public_url()}/followers"
        ],
        "type": "Announce",
        "id": announce_id
    }

    return activity_data


def post_to_page(post: Post):
    activity_data = {
        "type": "Page",
        "id": post.ap_id,
        "attributedTo": post.author.ap_public_url,
        "to": [
            post.community.public_url(),
            "https://www.w3.org/ns/activitystreams#Public"
        ],
        "name": post.title,
        "cc": [],
        "content": post.body_html if post.body_html else '',
        "mediaType": "text/html",
        "source": {"content": post.body if post.body else '', "mediaType": "text/markdown"},
        "attachment": [],
        "commentsEnabled": post.comments_enabled,
        "sensitive": post.nsfw or post.nsfl,
        "published": ap_datetime(post.created_at),
        "stickied": post.sticky,
        "audience": post.community.public_url(),
        "tag": post.tags_for_activitypub(),
        "replies": post_replies_for_ap(post.id),
        "language": {
            "identifier": post.language_code(),
            "name": post.language_name()
        },
    }
    if post.edited_at is not None:
        activity_data["updated"] = ap_datetime(post.edited_at)
    if (post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO) and post.url is not None:
        activity_data["attachment"] = [{"href": post.url, "type": "Link"}]
    if post.image_id is not None:
        activity_data["image"] = {"url": post.image.view_url(), "type": "Image"}
        if post.type == POST_TYPE_IMAGE:
            activity_data['attachment'] = [{'type': 'Image',
                                   'url': post.image.source_url,
                                   'name': post.image.alt_text}]
    if post.type == POST_TYPE_POLL:
        poll = Poll.query.filter_by(post_id=post.id).first()
        activity_data['type'] = 'Question'
        mode = 'oneOf' if poll.mode == 'single' else 'anyOf'
        choices = []
        for choice in PollChoice.query.filter_by(post_id=post.id).order_by(PollChoice.sort_order).all():
            choices.append({
                "type": "Note",
                "name": choice.choice_text,
                "replies": {
                    "type": "Collection",
                    "totalItems": choice.num_votes
                }
            })
        activity_data[mode] = choices
        activity_data['endTime'] = ap_datetime(poll.end_poll)
        activity_data['votersCount'] = poll.total_votes()
    if post.indexable:
        activity_data['searchableBy'] = 'https://www.w3.org/ns/activitystreams#Public'
    return activity_data


def post_replies_for_ap(post_id: int) -> List[dict]:
    replies = PostReply.query.\
        filter_by(post_id=post_id, deleted=False).\
        order_by(desc(PostReply.posted_at)).\
        limit(2000)
    return [comment_model_to_json(reply) for reply in replies]


def comment_model_to_json(reply: PostReply) -> dict:
    reply_data = {
        "@context": [
            "https://www.w3.org/ns/activitystreams",
            "https://w3id.org/security/v1",
        ],
        "type": "Note",
        "id": reply.ap_id,
        "attributedTo": reply.author.public_url(),
        "inReplyTo": reply.in_reply_to(),
        "to": [
            "https://www.w3.org/ns/activitystreams#Public",
            reply.to()
        ],
        "cc": [
            reply.community.public_url(),
            reply.author.followers_url()
        ],
        'content': reply.body_html,
        'mediaType': 'text/html',
        'source': {'content': reply.body, 'mediaType': 'text/markdown'},
        'published': ap_datetime(reply.created_at),
        'distinguished': False,
        'audience': reply.community.public_url(),
        'language': {
            'identifier': reply.language_code(),
            'name': reply.language_name()
        }
    }
    if reply.edited_at:
        reply_data['updated'] = ap_datetime(reply.edited_at)
    if reply.deleted:
        if reply.deleted_by == reply.user_id:
            reply_data['content'] = '<p>Deleted by author</p>'
            reply_data['source']['content'] = 'Deleted by author'
        else:
            reply_data['content'] = '<p>Deleted by moderator</p>'
            reply_data['source']['content'] = 'Deleted by moderator'
    return reply_data


def banned_user_agents():
    return []  # todo: finish this function


@cache.memoize(150)
def instance_blocked(host: str) -> bool:        # see also utils.instance_banned()
    if host is None or host == '':
        return True
    host = host.lower()
    if 'https://' in host or 'http://' in host:
        host = urlparse(host).hostname
    instance = BannedInstances.query.filter_by(domain=host.strip()).first()
    return instance is not None


@cache.memoize(150)
def instance_allowed(host: str) -> bool:
    if host is None or host == '':
        return True
    host = host.lower()
    if 'https://' in host or 'http://' in host:
        host = urlparse(host).hostname
    instance = AllowedInstances.query.filter_by(domain=host.strip()).first()
    return instance is not None


def find_actor_or_create(actor: str, create_if_not_found=True, community_only=False) -> Union[User, Community, None]:
    if isinstance(actor, dict):     # Discourse does this
        actor = actor['id']
    actor_url = actor.strip()
    actor = actor.strip().lower()
    user = None
    server = ''
    # actor parameter must be formatted as https://server/u/actor or https://server/c/actor

    # Initially, check if the user exists in the local DB already
    if current_app.config['SERVER_NAME'] + '/c/' in actor:
        return Community.query.filter(Community.ap_profile_id == actor).first()  # finds communities formatted like https://localhost/c/*

    if current_app.config['SERVER_NAME'] + '/u/' in actor:
        alt_user_name = actor_url.rsplit('/', 1)[-1]
        user = User.query.filter(or_(User.ap_profile_id == actor, User.alt_user_name == alt_user_name)).filter_by(ap_id=None, banned=False).first()  # finds local users
        if user is None:
            return None
    elif actor.startswith('https://'):
        server, address = extract_domain_and_actor(actor)
        if get_setting('use_allowlist', False):
            if not instance_allowed(server):
                return None
        else:
            if instance_blocked(server):
                return None
        if actor_contains_blocked_words(actor):
            return None
        user = User.query.filter(User.ap_profile_id == actor).first()  # finds users formatted like https://kbin.social/u/tables
        if (user and user.banned) or (user and user.deleted) :
            return None
        if user is None:
            user = Community.query.filter(Community.ap_profile_id == actor).first()
            if user and user.banned:
                # Try to find a non-banned copy of the community. Sometimes duplicates happen and one copy is banned.
                user = Community.query.filter(Community.ap_profile_id == actor).filter(Community.banned == False).first()
                if user is None:    # no un-banned version of this community exists, only the banned one. So it was banned for being bad, not for being a duplicate.
                    return None

    if user is not None:
        if not user.is_local() and (user.ap_fetched_at is None or user.ap_fetched_at < utcnow() - timedelta(days=7)):
            # To reduce load on remote servers, refreshing the user profile happens after a delay of 1 to 10 seconds. Meanwhile, subsequent calls to
            # find_actor_or_create() which happen to be for the same actor might queue up refreshes of the same user. To avoid this, set a flag to
            # indicate that user is currently being refreshed.
            refresh_in_progress = cache.get(f'refreshing_{user.id}')
            if not refresh_in_progress:
                cache.set(f'refreshing_{user.id}', True, timeout=300)
                if isinstance(user, User):
                    refresh_user_profile(user.id)
                elif isinstance(user, Community):
                    refresh_community_profile(user.id)
                    # refresh_instance_profile(user.instance_id) # disable in favour of cron job - see app.cli.daily_maintenance()
        if community_only and not isinstance(user, Community):
            return None
        return user
    else:   # User does not exist in the DB, it's going to need to be created from it's remote home instance
        if create_if_not_found:
            if actor.startswith('https://'):
                try:
                    actor_data = get_request(actor_url, headers={'Accept': 'application/activity+json'})
                except httpx.HTTPError:
                    time.sleep(randint(3, 10))
                    try:
                        actor_data = get_request(actor_url, headers={'Accept': 'application/activity+json'})
                    except httpx.HTTPError as e:
                        raise e
                        return None
                if actor_data.status_code == 200:
                    try:
                        actor_json = actor_data.json()
                    except Exception as e:
                        actor_data.close()
                        return None
                    actor_data.close()
                    actor_model = actor_json_to_model(actor_json, address, server)
                    if community_only and not isinstance(actor_model, Community):
                        return None
                    return actor_model
                elif actor_data.status_code == 401:
                    try:
                        site = Site.query.get(1)
                        actor_data = signed_get_request(actor_url, site.private_key,
                                        f"https://{current_app.config['SERVER_NAME']}/actor#main-key")
                        if actor_data.status_code == 200:
                            try:
                                actor_json = actor_data.json()
                            except Exception as e:
                                actor_data.close()
                                return None
                            actor_data.close()
                            actor_model = actor_json_to_model(actor_json, address, server)
                            if community_only and not isinstance(actor_model, Community):
                                return None
                            return actor_model
                    except Exception:
                        return None
            else:
                # retrieve user details via webfinger, etc
                try:
                    webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                                 params={'resource': f"acct:{address}@{server}"})
                except httpx.HTTPError:
                    time.sleep(randint(3, 10))
                    webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                                 params={'resource': f"acct:{address}@{server}"})
                if webfinger_data.status_code == 200:
                    webfinger_json = webfinger_data.json()
                    webfinger_data.close()
                    for links in webfinger_json['links']:
                        if 'rel' in links and links['rel'] == 'self':  # this contains the URL of the activitypub profile
                            type = links['type'] if 'type' in links else 'application/activity+json'
                            # retrieve the activitypub profile
                            try:
                                actor_data = get_request(links['href'], headers={'Accept': type})
                            except httpx.HTTPError:
                                time.sleep(randint(3, 10))
                                actor_data = get_request(links['href'], headers={'Accept': type})
                            # to see the structure of the json contained in actor_data, do a GET to https://lemmy.world/c/technology with header Accept: application/activity+json
                            if actor_data.status_code == 200:
                                actor_json = actor_data.json()
                                actor_data.close()
                                actor_model = actor_json_to_model(actor_json, address, server)
                                if community_only and not isinstance(actor_model, Community):
                                    return None
                                return actor_model
    return None


def find_language(code: str) -> Language | None:
    existing_language = Language.query.filter(Language.code == code).first()
    if existing_language:
        return existing_language
    else:
        return None


def find_language_or_create(code: str, name: str, session=None) -> Language:
    if session:
        existing_language: Language = session.query(Language).filter(Language.code == code).first()
    else:
        existing_language = Language.query.filter(Language.code == code).first()
    if existing_language:
        return existing_language
    else:
        new_language = Language(code=code, name=name)
        if session:
            session.add(new_language)
        else:
            db.session.add(new_language)
        return new_language


def find_licence_or_create(name: str) -> Licence:
    existing_licence = Licence.query.filter(Licence.name == name.strip()).first()
    if existing_licence:
        return existing_licence
    else:
        new_licence = Licence(name=name.strip())
        db.session.add(new_licence)
        return new_licence


def find_hashtag_or_create(hashtag: str) -> Tag:
    if hashtag is None or hashtag == '':
        return None

    hashtag = hashtag.strip()
    if hashtag[0] == '#':
        hashtag = hashtag[1:]

    existing_tag = Tag.query.filter(Tag.name == hashtag.lower()).first()
    if existing_tag:
        return existing_tag
    else:
        new_tag = Tag(name=hashtag.lower(), display_as=hashtag, post_count=1)
        db.session.add(new_tag)
        return new_tag


def extract_domain_and_actor(url_string: str):
    # Parse the URL
    parsed_url = urlparse(url_string)

    # Extract the server domain name
    server_domain = parsed_url.netloc

    # Extract the part of the string after the last '/' character
    actor = parsed_url.path.split('/')[-1]

    return server_domain, actor


def user_removed_from_remote_server(actor_url, is_piefed=False):
    result = False
    response = None
    try:
        if is_piefed:
            response = head_request(actor_url, headers={'Accept': 'application/activity+json'})
        else:
            response = get_request(actor_url, headers={'Accept': 'application/activity+json'})
        if response.status_code == 404 or response.status_code == 410:
            result = True
        else:
            result = False
    except:
        result = True
    finally:
        if response:
            response.close()
    return result


def refresh_user_profile(user_id):
    if current_app.debug:
        refresh_user_profile_task(user_id)
    else:
        refresh_user_profile_task.apply_async(args=(user_id,), countdown=randint(1, 10))


@celery.task
def refresh_user_profile_task(user_id):
    session = get_task_session()
    user: User = session.query(User).get(user_id)
    if user and user.instance_id and user.instance.online():
        try:
            actor_data = get_request(user.ap_public_url, headers={'Accept': 'application/activity+json'})
        except httpx.HTTPError:
            time.sleep(randint(3, 10))
            try:
                actor_data = get_request(user.ap_public_url, headers={'Accept': 'application/activity+json'})
            except httpx.HTTPError:
                return
        except:
            try:
                site = session.query(Site).get(1)
                actor_data = signed_get_request(user.ap_public_url, site.private_key,
                                f"https://{current_app.config['SERVER_NAME']}/actor#main-key")
            except:
                return
        if actor_data.status_code == 200:
            activity_json = actor_data.json()
            actor_data.close()

            # update indexible state on their posts, if necessary
            new_indexable = activity_json['indexable'] if 'indexable' in activity_json else True
            if new_indexable != user.indexable:
                session.execute(text('UPDATE "post" set indexable = :indexable WHERE user_id = :user_id'),
                                {'user_id': user.id,
                                'indexable': new_indexable})

            user.user_name = activity_json['preferredUsername'].strip()
            if 'name' in activity_json:
                user.title = activity_json['name'].strip() if activity_json['name'] else ''
            if 'summary' in activity_json:
                about_html = activity_json['summary']
                if about_html is not None and not about_html.startswith('<'):                    # PeerTube
                    about_html = '<p>' + about_html + '</p>'
                user.about_html = allowlist_html(about_html)
            else:
                user.about_html = ''
            if 'source' in activity_json and activity_json['source'].get('mediaType') == 'text/markdown':
                user.about = activity_json['source']['content']
                user.about_html = markdown_to_html(user.about)          # prefer Markdown if provided, overwrite version obtained from HTML
            else:
                user.about = html_to_text(user.about_html)
            if 'type' in activity_json:
                user.bot = True if activity_json['type'] == 'Service' else False
            user.ap_fetched_at = utcnow()
            user.public_key = activity_json['publicKey']['publicKeyPem']
            user.indexable = new_indexable

            avatar_changed = cover_changed = False
            if 'icon' in activity_json and activity_json['icon'] is not None:
                if isinstance(activity_json['icon'], dict) and 'url' in activity_json['icon']:
                    icon_entry = activity_json['icon']['url']
                elif isinstance(activity_json['icon'], list) and 'url' in activity_json['icon'][-1]:
                    icon_entry = activity_json['icon'][-1]['url']
                else:
                    icon_entry = None
                if icon_entry:
                    if user.avatar_id and icon_entry != user.avatar.source_url:
                        user.avatar.delete_from_disk()
                    if not user.avatar_id or (user.avatar_id and icon_entry != user.avatar.source_url):
                        avatar = File(source_url=icon_entry)
                        user.avatar = avatar
                        session.add(avatar)
                        avatar_changed = True
            if 'image' in activity_json and activity_json['image'] is not None:
                if user.cover_id and activity_json['image']['url'] != user.cover.source_url:
                    user.cover.delete_from_disk()
                if not user.cover_id or (user.cover_id and activity_json['image']['url'] != user.cover.source_url):
                    cover = File(source_url=activity_json['image']['url'])
                    user.cover = cover
                    session.add(cover)
                    cover_changed = True
            user.recalculate_post_stats()
            session.commit()
            if user.avatar_id and avatar_changed:
                make_image_sizes(user.avatar_id, 40, 250, 'users')
                cache.delete_memoized(User.avatar_image, user)
                cache.delete_memoized(User.avatar_thumbnail, user)
            if user.cover_id and cover_changed:
                make_image_sizes(user.cover_id, 700, 1600, 'users')
                cache.delete_memoized(User.cover_image, user)
            session.close()


def refresh_community_profile(community_id):
    if current_app.debug:
        refresh_community_profile_task(community_id)
    else:
        refresh_community_profile_task.apply_async(args=(community_id,), countdown=randint(1, 10))


@celery.task
def refresh_community_profile_task(community_id):
    session = get_task_session()
    community: Community = session.query(Community).get(community_id)
    if community and community.instance.online() and not community.is_local():
        try:
            actor_data = get_request(community.ap_public_url, headers={'Accept': 'application/activity+json'})
        except httpx.HTTPError:
            time.sleep(randint(3, 10))
            try:
                actor_data = get_request(community.ap_public_url, headers={'Accept': 'application/activity+json'})
            except Exception as e:
                return
        if actor_data.status_code == 200:
            activity_json = actor_data.json()
            actor_data.close()

            if 'attributedTo' in activity_json and isinstance(activity_json['attributedTo'], str):  # lemmy and mbin
                mods_url = activity_json['attributedTo']
            elif 'moderators' in activity_json:  # kbin
                mods_url = activity_json['moderators']
            else:
                mods_url = None

            community.nsfw = activity_json['sensitive'] if 'sensitive' in activity_json else False
            if 'nsfl' in activity_json and activity_json['nsfl']:
                community.nsfl = activity_json['nsfl']
            community.title = activity_json['name'].strip()
            community.restricted_to_mods = activity_json['postingRestrictedToMods'] if 'postingRestrictedToMods' in activity_json else False
            community.new_mods_wanted = activity_json['newModsWanted'] if 'newModsWanted' in activity_json else False
            community.private_mods = activity_json['privateMods'] if 'privateMods' in activity_json else False
            community.ap_moderators_url = mods_url
            community.ap_fetched_at = utcnow()
            community.public_key=activity_json['publicKey']['publicKeyPem']

            description_html = ''
            if 'summary' in activity_json:
                description_html = activity_json['summary']
            elif 'content' in activity_json:
                description_html = activity_json['content']
            else:
                description_html = ''

            if description_html is not None and description_html != '':
                if not description_html.startswith('<'):                    # PeerTube
                    description_html = '<p>' + description_html + '</p>'
                community.description_html = allowlist_html(description_html)
                if 'source' in activity_json and activity_json['source'].get('mediaType') == 'text/markdown':
                    community.description = activity_json['source']['content']
                    community.description_html = markdown_to_html(community.description)          # prefer Markdown if provided, overwrite version obtained from HTML
                else:
                    community.description = html_to_text(community.description_html)

            if 'rules' in activity_json:
                community.rules_html = allowlist_html(activity_json['rules'])
                community.rules = html_to_text(community.rules_html)

            icon_changed = cover_changed = False
            if 'icon' in activity_json:
                if isinstance(activity_json['icon'], dict) and 'url' in activity_json['icon']:
                    icon_entry = activity_json['icon']['url']
                elif isinstance(activity_json['icon'], list) and 'url' in activity_json['icon'][-1]:
                    icon_entry = activity_json['icon'][-1]['url']
                else:
                    icon_entry = None
                if icon_entry:
                    if community.icon_id and icon_entry != community.icon.source_url:
                        community.icon.delete_from_disk()
                    if not community.icon_id or (community.icon_id and icon_entry != community.icon.source_url):
                        icon = File(source_url=icon_entry)
                        community.icon = icon
                        session.add(icon)
                        icon_changed = True
            if 'image' in activity_json:
                if isinstance(activity_json['image'], dict) and 'url' in activity_json['image']:
                    image_entry = activity_json['image']['url']
                elif isinstance(activity_json['image'], list) and 'url' in activity_json['image'][0]:
                    image_entry = activity_json['image'][0]['url']
                else:
                    image_entry = None
                if image_entry:
                    if community.image_id and image_entry != community.image.source_url:
                        community.image.delete_from_disk()
                    if not community.image_id or (community.image_id and image_entry != community.image.source_url):
                        image = File(source_url=image_entry)
                        community.image = image
                        session.add(image)
                        cover_changed = True
            if 'language' in activity_json and isinstance(activity_json['language'], list) and not community.ignore_remote_language:
                for ap_language in activity_json['language']:
                    new_language = find_language_or_create(ap_language['identifier'], ap_language['name'], session)
                    if new_language not in community.languages:
                        community.languages.append(new_language)
            instance = session.query(Instance).get(community.instance_id)
            if instance and instance.software == 'peertube':
                community.restricted_to_mods = True
            session.commit()

            if community.icon_id and icon_changed:
                make_image_sizes(community.icon_id, 60, 250, 'communities')
            if community.image_id and cover_changed:
                make_image_sizes(community.image_id, 700, 1600, 'communities')

            if community.ap_moderators_url:
                mods_request = get_request(community.ap_moderators_url, headers={'Accept': 'application/activity+json'})
                if mods_request.status_code == 200:
                    mods_data = mods_request.json()
                    mods_request.close()
                    if mods_data and mods_data['type'] == 'OrderedCollection' and 'orderedItems' in mods_data:
                        for actor in mods_data['orderedItems']:
                            time.sleep(0.5)
                            user = find_actor_or_create(actor)
                            if user:
                                existing_membership = CommunityMember.query.filter_by(community_id=community.id,
                                                                                      user_id=user.id).first()
                                if existing_membership:
                                    existing_membership.is_moderator = True
                                    db.session.commit()
                                else:
                                    new_membership = CommunityMember(community_id=community.id, user_id=user.id,
                                                                     is_moderator=True)
                                    db.session.add(new_membership)
                                    db.session.commit()

                        # Remove people who are no longer mods
                        for member in CommunityMember.query.filter_by(community_id=community.id, is_moderator=True).all():
                            member_user = User.query.get(member.user_id)
                            is_mod = False
                            for actor in mods_data['orderedItems']:
                                if actor.lower() == member_user.profile_id().lower():
                                    is_mod = True
                                    break
                            if not is_mod:
                                db.session.query(CommunityMember).filter_by(community_id=community.id,
                                                                            user_id=member_user.id,
                                                                            is_moderator=True).delete()
                                db.session.commit()
    session.close()


def actor_json_to_model(activity_json, address, server):
    if activity_json['type'] == 'Person' or activity_json['type'] == 'Service':
        try:
            user = User(user_name=activity_json['preferredUsername'].strip(),
                        title=activity_json['name'].strip() if 'name' in activity_json and activity_json['name'] else None,
                        email=f"{address}@{server}",
                        matrix_user_id=activity_json['matrixUserId'] if 'matrixUserId' in activity_json else '',
                        indexable=activity_json['indexable'] if 'indexable' in activity_json else True,
                        searchable=activity_json['discoverable'] if 'discoverable' in activity_json else True,
                        created=activity_json['published'] if 'published' in activity_json else utcnow(),
                        ap_id=f"{address.lower()}@{server.lower()}",
                        ap_public_url=activity_json['id'],
                        ap_profile_id=activity_json['id'].lower(),
                        ap_inbox_url=activity_json['endpoints']['sharedInbox'] if 'endpoints' in activity_json else activity_json['inbox'] if 'inbox' in activity_json else '',
                        ap_followers_url=activity_json['followers'] if 'followers' in activity_json else None,
                        ap_preferred_username=activity_json['preferredUsername'],
                        ap_manually_approves_followers=activity_json['manuallyApprovesFollowers'] if 'manuallyApprovesFollowers' in activity_json else False,
                        ap_fetched_at=utcnow(),
                        ap_domain=server,
                        public_key=activity_json['publicKey']['publicKeyPem'],
                        bot=True if activity_json['type'] == 'Service' else False,
                        instance_id=find_instance_id(server)
                        # language=community_json['language'][0]['identifier'] # todo: language
                        )
        except KeyError as e:
            current_app.logger.error(f'KeyError for {address}@{server} while parsing ' + str(activity_json))
            return None

        if 'summary' in activity_json:
            about_html = activity_json['summary']
            if about_html is not None and not about_html.startswith('<'):                    # PeerTube
                about_html = '<p>' + about_html + '</p>'
            user.about_html = allowlist_html(about_html)
        else:
            user.about_html = ''
        if 'source' in activity_json and activity_json['source'].get('mediaType') == 'text/markdown':
            user.about = activity_json['source']['content']
            user.about_html = markdown_to_html(user.about)          # prefer Markdown if provided, overwrite version obtained from HTML
        else:
            user.about = html_to_text(user.about_html)

        if 'icon' in activity_json and activity_json['icon'] is not None:
            if isinstance(activity_json['icon'], dict) and 'url' in activity_json['icon']:
                icon_entry = activity_json['icon']['url']
            elif isinstance(activity_json['icon'], list) and 'url' in activity_json['icon'][-1]:
                icon_entry = activity_json['icon'][-1]['url']
            elif isinstance(activity_json['icon'], str):
                icon_entry = activity_json['icon']
            else:
                icon_entry = None
            if icon_entry:
                avatar = File(source_url=icon_entry)
                user.avatar = avatar
                db.session.add(avatar)
        if 'image' in activity_json and activity_json['image'] is not None and 'url' in activity_json['image']:
            cover = File(source_url=activity_json['image']['url'])
            user.cover = cover
            db.session.add(cover)
        try:
            db.session.add(user)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return User.query.filter_by(ap_profile_id=activity_json['id'].lower()).one()
        if user.avatar_id:
            make_image_sizes(user.avatar_id, 40, 250, 'users')
        if user.cover_id:
            make_image_sizes(user.cover_id, 878, None, 'users')
        return user
    elif activity_json['type'] == 'Group':
        if 'attributedTo' in activity_json and isinstance(activity_json['attributedTo'], str):  # lemmy and mbin
            mods_url = activity_json['attributedTo']
        elif 'moderators' in activity_json:  # kbin
            mods_url = activity_json['moderators']
        else:
            mods_url = None

        # only allow nsfw communities if enabled for this instance
        site = Site.query.get(1)    # can't use g.site because actor_json_to_model can be called from celery
        if 'sensitive' in activity_json and activity_json['sensitive'] and not site.enable_nsfw:
            return None
        if 'nsfl' in activity_json and activity_json['nsfl'] and not site.enable_nsfl:
            return None

        community = Community(name=activity_json['preferredUsername'].strip(),
                              title=activity_json['name'].strip(),
                              nsfw=activity_json['sensitive'] if 'sensitive' in activity_json else False,
                              restricted_to_mods=activity_json['postingRestrictedToMods'] if 'postingRestrictedToMods' in activity_json else False,
                              new_mods_wanted=activity_json['newModsWanted'] if 'newModsWanted' in activity_json else False,
                              private_mods=activity_json['privateMods'] if 'privateMods' in activity_json else False,
                              created_at=activity_json['published'] if 'published' in activity_json else utcnow(),
                              last_active=activity_json['updated'] if 'updated' in activity_json else utcnow(),
                              ap_id=f"{address[1:].lower()}@{server.lower()}" if address.startswith('!') else f"{address}@{server}",
                              ap_public_url=activity_json['id'],
                              ap_profile_id=activity_json['id'].lower(),
                              ap_followers_url=activity_json['followers'] if 'followers' in activity_json else None,
                              ap_inbox_url=activity_json['endpoints']['sharedInbox'] if 'endpoints' in activity_json else activity_json['inbox'],
                              ap_outbox_url=activity_json['outbox'],
                              ap_featured_url=activity_json['featured'] if 'featured' in activity_json else '',
                              ap_moderators_url=mods_url,
                              ap_fetched_at=utcnow(),
                              ap_domain=server,
                              public_key=activity_json['publicKey']['publicKeyPem'],
                              # language=community_json['language'][0]['identifier'] # todo: language
                              instance_id=find_instance_id(server),
                              low_quality='memes' in activity_json['preferredUsername']
                              )

        description_html = ''
        if 'summary' in activity_json:
            description_html = activity_json['summary']
        elif 'content' in activity_json:
            description_html = activity_json['content']
        else:
            description_html = ''

        if description_html is not None and description_html != '':
            if not description_html.startswith('<'):                    # PeerTube
                description_html = '<p>' + description_html + '</p>'
            community.description_html = allowlist_html(description_html)
            if 'source' in activity_json and activity_json['source'].get('mediaType') == 'text/markdown':
                community.description = activity_json['source']['content']
                community.description_html = markdown_to_html(community.description)          # prefer Markdown if provided, overwrite version obtained from HTML
            else:
                community.description = html_to_text(community.description_html)

        if 'rules' in activity_json:
            community.rules_html = allowlist_html(activity_json['rules'])
            community.rules = html_to_text(community.rules_html)

        if 'icon' in activity_json and activity_json['icon'] is not None:
            if isinstance(activity_json['icon'], dict) and 'url' in activity_json['icon']:
                icon_entry = activity_json['icon']['url']
            elif isinstance(activity_json['icon'], list) and 'url' in activity_json['icon'][-1]:
                icon_entry = activity_json['icon'][-1]['url']
            elif isinstance(activity_json['icon'], str):
                icon_entry = activity_json['icon']
            else:
                icon_entry = None
            if icon_entry:
                icon = File(source_url=icon_entry)
                community.icon = icon
                db.session.add(icon)
        if 'image' in activity_json and activity_json['image'] is not None:
            if isinstance(activity_json['image'], dict) and 'url' in activity_json['image']:
                image_entry = activity_json['image']['url']
            elif isinstance(activity_json['image'], list) and 'url' in activity_json['image'][0]:
                image_entry = activity_json['image'][0]['url']
            else:
                image_entry = None
            if image_entry:
                image = File(source_url=image_entry)
                community.image = image
                db.session.add(image)
        if 'language' in activity_json and isinstance(activity_json['language'], list):
            for ap_language in activity_json['language']:
                community.languages.append(find_language_or_create(ap_language['identifier'], ap_language['name']))
        try:
            db.session.add(community)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return Community.query.filter_by(ap_profile_id=activity_json['id'].lower()).one()
        if community.icon_id:
            make_image_sizes(community.icon_id, 60, 250, 'communities')
        if community.image_id:
            make_image_sizes(community.image_id, 700, 1600, 'communities')
        return community


def post_json_to_model(activity_log, post_json, user, community) -> Post:
    try:
        nsfl_in_title = '[NSFL]' in post_json['name'].upper() or '(NSFL)' in post_json['name'].upper()
        post = Post(user_id=user.id, community_id=community.id,
                    title=html.unescape(post_json['name']),
                    comments_enabled=post_json['commentsEnabled'] if 'commentsEnabled' in post_json else True,
                    sticky=post_json['stickied'] if 'stickied' in post_json else False,
                    nsfw=post_json['sensitive'],
                    nsfl=post_json['nsfl'] if 'nsfl' in post_json else nsfl_in_title,
                    ap_id=post_json['id'],
                    type=constants.POST_TYPE_ARTICLE,
                    posted_at=post_json['published'],
                    last_active=post_json['published'],
                    instance_id=user.instance_id,
                    indexable = user.indexable
                    )
        if 'content' in post_json:
            if post_json['mediaType'] == 'text/html':
                post.body_html = allowlist_html(post_json['content'])
                if 'source' in post_json and post_json['source']['mediaType'] == 'text/markdown':
                    post.body = post_json['source']['content']
                    post.body_html = markdown_to_html(post.body)          # prefer Markdown if provided, overwrite version obtained from HTML
                else:
                    post.body = html_to_text(post.body_html)
            elif post_json['mediaType'] == 'text/markdown':
                post.body = post_json['content']
                post.body_html = markdown_to_html(post.body)
        if 'attachment' in post_json and len(post_json['attachment']) > 0 and 'type' in post_json['attachment'][0]:
            alt_text = None
            if post_json['attachment'][0]['type'] == 'Link':
                post.url = post_json['attachment'][0]['href']                       # Lemmy < 0.19.4
            if post_json['attachment'][0]['type'] == 'Image':
                post.url = post_json['attachment'][0]['url']                        # PieFed, Lemmy >= 0.19.4
                if 'name' in post_json['attachment'][0]:
                    alt_text = post_json['attachment'][0]['name']
            if post.url:
                if is_image_url(post.url):
                    post.type = POST_TYPE_IMAGE
                    image = File(source_url=post.url)
                    if alt_text:
                        image.alt_text = alt_text
                    db.session.add(image)
                    post.image = image
                elif is_video_url(post.url):
                    post.type = POST_TYPE_VIDEO
                else:
                    post.type = POST_TYPE_LINK
                    post.url = remove_tracking_from_link(post.url)
                domain = domain_from_url(post.url)

                # notify about links to banned websites.
                already_notified = set()        # often admins and mods are the same people - avoid notifying them twice
                if domain:
                    if domain.notify_mods:
                        for community_member in post.community.moderators():
                            notify = Notification(title='Suspicious content', url=post.ap_id, user_id=community_member.user_id, author_id=user.id)
                            db.session.add(notify)
                            already_notified.add(community_member.user_id)

                    if domain.notify_admins:
                        for admin in Site.admins():
                            if admin.id not in already_notified:
                                notify = Notification(title='Suspicious content', url=post.ap_id, user_id=admin.id, author_id=user.id)
                                db.session.add(notify)
                                admin.unread_notifications += 1
                    if domain.banned:
                        post = None
                        activity_log.exception_message = domain.name + ' is blocked by admin'
                    if not domain.banned:
                        domain.post_count += 1
                        post.domain = domain

        if post is not None:
            if post_json['type'] == 'Video':
                post.type = POST_TYPE_VIDEO
                post.url = post_json['id']
                if 'icon' in post_json and isinstance(post_json['icon'], list):
                    icon = File(source_url=post_json['icon'][-1]['url'])
                    db.session.add(icon)
                    post.image = icon

            if 'language' in post_json:
                language = find_language_or_create(post_json['language']['identifier'], post_json['language']['name'])
                if language:
                    post.language_id = language.id

            if 'tag' in post_json:
                for json_tag in post_json['tag']:
                    if json_tag['type'] == 'Hashtag':
                        # Lemmy adds the community slug as a hashtag on every post in the community, which we want to ignore
                        if json_tag['name'][1:].lower() != community.name.lower():
                            hashtag = find_hashtag_or_create(json_tag['name'])
                            if hashtag:
                                post.tags.append(hashtag)

            if 'image' in post_json and post.image is None:
                image = File(source_url=post_json['image']['url'])
                db.session.add(image)
                post.image = image
            db.session.add(post)
            community.post_count += 1
            user.post_count += 1
            activity_log.result = 'success'
            db.session.commit()

            if post.image_id:
                make_image_sizes(post.image_id, 170, 512, 'posts')  # the 512 sized image is for masonry view

        return post
    except KeyError as e:
        current_app.logger.error(f'KeyError in post_json_to_model: ' + str(post_json))
        return None


# Save two different versions of a File, after downloading it from file.source_url. Set a width parameter to None to avoid generating one of that size
def make_image_sizes(file_id, thumbnail_width=50, medium_width=120, directory='posts', toxic_community=False):
    if current_app.debug:
        make_image_sizes_async(file_id, thumbnail_width, medium_width, directory, toxic_community)
    else:
        make_image_sizes_async.apply_async(args=(file_id, thumbnail_width, medium_width, directory, toxic_community), countdown=randint(1, 10))  # Delay by up to 10 seconds so servers do not experience a stampede of requests all in the same second


@celery.task
def make_image_sizes_async(file_id, thumbnail_width, medium_width, directory, toxic_community):
    session = get_task_session()
    file: File = session.query(File).get(file_id)
    if file and file.source_url:
        try:
            source_image_response = get_request(file.source_url)
        except:
            pass
        else:
            if source_image_response.status_code == 404 and '/api/v3/image_proxy' in file.source_url:
                source_image_response.close()
                # Lemmy failed to retrieve the image but we might have better luck. Example source_url: https://slrpnk.net/api/v3/image_proxy?url=https%3A%2F%2Fi.guim.co.uk%2Fimg%2Fmedia%2F24e87cb4d730141848c339b3b862691ca536fb26%2F0_164_3385_2031%2Fmaster%2F3385.jpg%3Fwidth%3D1200%26height%3D630%26quality%3D85%26auto%3Dformat%26fit%3Dcrop%26overlay-align%3Dbottom%252Cleft%26overlay-width%3D100p%26overlay-base64%3DL2ltZy9zdGF0aWMvb3ZlcmxheXMvdGctZGVmYXVsdC5wbmc%26enable%3Dupscale%26s%3D0ec9d25a8cb5db9420471054e26cfa63
                # The un-proxied image url is the query parameter called 'url'
                parsed_url = urlparse(file.source_url)
                query_params = parse_qs(parsed_url.query)
                if 'url' in query_params:
                    url_value = query_params['url'][0]
                    source_image_response = get_request(url_value)
                else:
                    source_image_response = None
            if source_image_response and source_image_response.status_code == 200:
                content_type = source_image_response.headers.get('content-type')
                if content_type:
                    if content_type.startswith('image') or (content_type == 'application/octet-stream' and file.source_url.endswith('.avif')):
                        source_image = source_image_response.content
                        source_image_response.close()

                        content_type_parts = content_type.split('/')
                        if content_type_parts:
                            # content type headers often are just 'image/jpeg' but sometimes 'image/jpeg;charset=utf8'

                            # Remove ;charset=whatever
                            main_part = content_type.split(';')[0]

                            # Split the main part on the '/' character and take the second part
                            file_ext = '.' + main_part.split('/')[1]
                            file_ext = file_ext.strip() # just to be sure

                            if file_ext == '.jpeg':
                                file_ext = '.jpg'
                            elif file_ext == '.svg+xml':
                                return  # no need to resize SVG images
                            elif file_ext == '.octet-stream':
                                file_ext = '.avif'
                        else:
                            file_ext = os.path.splitext(file.source_url)[1]
                            file_ext = file_ext.replace('%3f', '?')  # sometimes urls are not decoded properly
                            if '?' in file_ext:
                                file_ext = file_ext.split('?')[0]

                        new_filename = gibberish(15)

                        # set up the storage directory
                        directory = f'app/static/media/{directory}/' + new_filename[0:2] + '/' + new_filename[2:4]
                        ensure_directory_exists(directory)

                        # file path and names to store the resized images on disk
                        final_place = os.path.join(directory, new_filename + file_ext)
                        final_place_thumbnail = os.path.join(directory, new_filename + '_thumbnail.webp')

                        if file_ext == '.avif': # this is quite a big plugin so we'll only load it if necessary
                            import pillow_avif

                        # Load image data into Pillow
                        Image.MAX_IMAGE_PIXELS = 89478485
                        image = Image.open(BytesIO(source_image))
                        image = ImageOps.exif_transpose(image)
                        img_width = image.width
                        img_height = image.height

                        # Resize the image to medium
                        if medium_width:
                            if img_width > medium_width:
                                image.thumbnail((medium_width, medium_width))
                            image.save(final_place)
                            file.file_path = final_place
                            file.width = image.width
                            file.height = image.height

                        # Resize the image to a thumbnail (webp)
                        if thumbnail_width:
                            if img_width > thumbnail_width:
                                image.thumbnail((thumbnail_width, thumbnail_width))
                            image.save(final_place_thumbnail, format="WebP", quality=93)
                            file.thumbnail_path = final_place_thumbnail
                            file.thumbnail_width = image.width
                            file.thumbnail_height = image.height

                        session.commit()

                        # Alert regarding fascist meme content
                        if toxic_community and img_width < 2000:    # images > 2000px tend to be real photos instead of 4chan screenshots.
                            try:
                                image_text = pytesseract.image_to_string(Image.open(BytesIO(source_image)).convert('L'), timeout=30)
                            except Exception as e:
                                image_text = ''
                            if 'Anonymous' in image_text and ('No.' in image_text or ' N0' in image_text):   # chan posts usually contain the text 'Anonymous' and ' No.12345'
                                post = Post.query.filter_by(image_id=file.id).first()
                                notification = Notification(title='Review this',
                                                            user_id=1,
                                                            author_id=post.user_id,
                                                            url=url_for('activitypub.post_ap', post_id=post.id))
                                session.add(notification)
                                session.commit()


def find_reply_parent(in_reply_to: str) -> Tuple[int, int, int]:
    if 'comment' in in_reply_to:
        parent_comment = PostReply.get_by_ap_id(in_reply_to)
        if not parent_comment:
            return (None, None, None)
        parent_comment_id = parent_comment.id
        post_id = parent_comment.post_id
        root_id = parent_comment.root_id
    elif 'post' in in_reply_to:
        parent_comment_id = None
        post = Post.get_by_ap_id(in_reply_to)
        if not post:
            return (None, None, None)
        post_id = post.id
        root_id = None
    else:
        parent_comment_id = None
        root_id = None
        post_id = None
        post = Post.get_by_ap_id(in_reply_to)
        if post:
            post_id = post.id
        else:
            parent_comment = PostReply.get_by_ap_id(in_reply_to)
            if parent_comment:
                parent_comment_id = parent_comment.id
                post_id = parent_comment.post_id
                root_id = parent_comment.root_id
            else:
                return (None, None, None)

    return post_id, parent_comment_id, root_id


def find_liked_object(ap_id) -> Union[Post, PostReply, None]:
    post = Post.get_by_ap_id(ap_id)
    if post:
        return post
    else:
        post_reply = PostReply.get_by_ap_id(ap_id)
        if post_reply:
            return post_reply
    return None


def find_reported_object(ap_id) -> Union[User, Post, PostReply, None]:
    post = Post.get_by_ap_id(ap_id)
    if post:
        return post
    else:
        post_reply = PostReply.get_by_ap_id(ap_id)
        if post_reply:
            return post_reply
        else:
            user = find_actor_or_create(ap_id, create_if_not_found=False)
            if user:
                return user
    return None


def find_instance_id(server):
    server = server.strip().lower()
    instance = Instance.query.filter_by(domain=server).first()
    if instance:
        return instance.id
    else:
        # Our instance does not know about {server} yet. Initially, create a sparse row in the 'instance' table and spawn a background
        # task to update the row with more details later
        new_instance = Instance(domain=server, software='unknown', created_at=utcnow(), trusted=server == 'piefed.social')
        try:
            db.session.add(new_instance)
            db.session.commit()
        except IntegrityError:
            return Instance.query.filter_by(domain=server).one()

        # Spawn background task to fill in more details
        new_instance_profile(new_instance.id)

        return new_instance.id


def new_instance_profile(instance_id: int):
    if instance_id:
        if current_app.debug:
            new_instance_profile_task(instance_id)
        else:
            new_instance_profile_task.apply_async(args=(instance_id,), countdown=randint(1, 10))


@celery.task
def new_instance_profile_task(instance_id: int):
    session = get_task_session()
    instance: Instance = session.query(Instance).get(instance_id)
    try:
        instance_data = get_request(f"https://{instance.domain}", headers={'Accept': 'application/activity+json'})
    except:
        return
    if instance_data.status_code == 200:
        try:
            instance_json = instance_data.json()
            instance_data.close()
        except Exception as ex:
            instance_json = {}
        if 'type' in instance_json and instance_json['type'] == 'Application':
            instance.inbox = instance_json['inbox']
            instance.outbox = instance_json['outbox']
        else:   # it's pretty much always /inbox so just assume that it is for whatever this instance is running
            instance.inbox = f"https://{instance.domain}/inbox"
        instance.updated_at = utcnow()
        session.commit()

        # retrieve list of Admins from /api/v3/site, update InstanceRole
        try:
            response = get_request(f'https://{instance.domain}/api/v3/site')
        except:
            response = None

        if response and response.status_code == 200:
            try:
                instance_data = response.json()
            except:
                instance_data = None
            finally:
                response.close()

            if instance_data:
                if 'admins' in instance_data:
                    admin_profile_ids = []
                    for admin in instance_data['admins']:
                        admin_profile_ids.append(admin['person']['actor_id'].lower())
                        user = find_actor_or_create(admin['person']['actor_id'])
                        if user and not instance.user_is_admin(user.id):
                            new_instance_role = InstanceRole(instance_id=instance.id, user_id=user.id, role='admin')
                            session.add(new_instance_role)
                            session.commit()
                    # remove any InstanceRoles that are no longer part of instance-data['admins']
                    for instance_admin in InstanceRole.query.filter_by(instance_id=instance.id):
                        if instance_admin.user.profile_id() not in admin_profile_ids:
                            session.query(InstanceRole).filter(
                                    InstanceRole.user_id == instance_admin.user.id,
                                    InstanceRole.instance_id == instance.id,
                                    InstanceRole.role == 'admin').delete()
                            session.commit()
    elif instance_data.status_code == 406 or instance_data.status_code == 404:  # Mastodon and PeerTube do 406, a.gup.pe does 404
        instance.inbox = f"https://{instance.domain}/inbox"
        instance.updated_at = utcnow()
        session.commit()

    headers = {'User-Agent': 'PieFed/1.0', 'Accept': 'application/activity+json'}
    try:
        nodeinfo = get_request(f"https://{instance.domain}/.well-known/nodeinfo", headers=headers)
        if nodeinfo.status_code == 200:
            nodeinfo_json = nodeinfo.json()
            for links in nodeinfo_json['links']:
                if isinstance(links, dict) and 'rel' in links and (
                    links['rel'] == 'http://nodeinfo.diaspora.software/ns/schema/2.0' or    # most platforms except KBIN and Lemmy v0.19.4
                    links['rel'] == 'https://nodeinfo.diaspora.software/ns/schema/2.0' or   # KBIN
                    links['rel'] == 'http://nodeinfo.diaspora.software/ns/schema/2.1'):     # Lemmy v0.19.4+ (no 2.0 back-compat provided here)
                    try:
                        time.sleep(0.1)
                        node = get_request(links['href'], headers=headers)
                        if node.status_code == 200:
                            node_json = node.json()
                            if 'software' in node_json:
                                instance.software = node_json['software']['name'].lower()
                                instance.version = node_json['software']['version']
                                instance.nodeinfo_href = links['href']
                                session.commit()
                                break  # most platforms (except Lemmy v0.19.4) that provide 2.1 also provide 2.0 - there's no need to check both
                    except:
                        return
    except:
        return
    session.close()


# alter the effect of upvotes based on their instance. Default to 1.0
@cache.memoize(timeout=50)
def instance_weight(domain):
    if domain:
        instance = Instance.query.filter_by(domain=domain).first()
        if instance:
            return instance.vote_weight
    return 1.0


def is_activitypub_request():
    return 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get('Accept', '')


def delete_post_or_comment(deletor, to_delete, store_ap_json, request_json):
    id = request_json['id']
    community = to_delete.community
    reason = request_json['object']['summary'] if 'summary' in request_json['object'] else ''
    if to_delete.user_id == deletor.id or deletor.is_admin() or community.is_moderator(deletor) or community.is_instance_admin(deletor):
        if isinstance(to_delete, Post):
            to_delete.deleted = True
            to_delete.deleted_by = deletor.id
            community.post_count -= 1
            to_delete.author.post_count -= 1
            if to_delete.url and to_delete.cross_posts is not None:
                old_cross_posts = Post.query.filter(Post.id.in_(to_delete.cross_posts)).all()
                to_delete.cross_posts.clear()
                for ocp in old_cross_posts:
                    if ocp.cross_posts is not None and to_delete.id in ocp.cross_posts:
                        ocp.cross_posts.remove(to_delete.id)
            db.session.commit()
            if to_delete.author.id != deletor.id:
                add_to_modlog_activitypub('delete_post', deletor, community_id=community.id,
                                          link_text=shorten_string(to_delete.title), link=f'post/{to_delete.id}',
                                          reason=reason)
        elif isinstance(to_delete, PostReply):
            to_delete.deleted = True
            to_delete.deleted_by = deletor.id
            to_delete.author.post_reply_count -= 1
            community.post_reply_count -= 1
            if not to_delete.author.bot:
                to_delete.post.reply_count -= 1
            db.session.commit()
            if to_delete.author.id != deletor.id:
                add_to_modlog_activitypub('delete_post_reply', deletor, community_id=community.id,
                                          link_text=f'comment on {shorten_string(to_delete.post.title)}',
                                          link=f'post/{to_delete.post.id}#comment_{to_delete.id}',
                                          reason=reason)
        log_incoming_ap(id, APLOG_DELETE, APLOG_SUCCESS, request_json if store_ap_json else None)
    else:
        log_incoming_ap(id, APLOG_DELETE, APLOG_FAILURE, request_json if store_ap_json else None, 'Deletor did not have permisson')


def restore_post_or_comment(restorer, to_restore, store_ap_json, request_json):
    id = request_json['id']
    community = to_restore.community
    reason = request_json['object']['summary'] if 'summary' in request_json['object'] else ''
    if to_restore.user_id == restorer.id or restorer.is_admin() or community.is_moderator(restorer) or community.is_instance_admin(restorer):
        if isinstance(to_restore, Post):
            to_restore.deleted = False
            to_restore.deleted_by = None
            community.post_count += 1
            to_restore.author.post_count += 1
            if to_restore.url:
                new_cross_posts = Post.query.filter(Post.id != to_restore.id, Post.url == to_restore.url, Post.deleted == False,
                                                                Post.posted_at > utcnow() - timedelta(days=6)).all()
                for ncp in new_cross_posts:
                    if ncp.cross_posts is None:
                        ncp.cross_posts = [to_restore.id]
                    else:
                        ncp.cross_posts.append(to_restore.id)
                    if to_restore.cross_posts is None:
                        to_restore.cross_posts = [ncp.id]
                    else:
                        to_restore.cross_posts.append(ncp.id)
            db.session.commit()
            if to_restore.author.id != restorer.id:
                add_to_modlog_activitypub('restore_post', restorer, community_id=community.id,
                                          link_text=shorten_string(to_restore.title), link=f'post/{to_restore.id}',
                                          reason=reason)

        elif isinstance(to_restore, PostReply):
            to_restore.deleted = False
            to_restore.deleted_by = None
            if not to_restore.author.bot:
                to_restore.post.reply_count += 1
            to_restore.author.post_reply_count += 1
            db.session.commit()
            if to_restore.author.id != restorer.id:
                add_to_modlog_activitypub('restore_post_reply', restorer, community_id=community.id,
                                          link_text=f'comment on {shorten_string(to_restore.post.title)}',
                                          link=f'post/{to_restore.post_id}#comment_{to_restore.id}',
                                          reason=reason)
        log_incoming_ap(id, APLOG_UNDO_DELETE, APLOG_SUCCESS, request_json if store_ap_json else None)
    else:
        log_incoming_ap(id, APLOG_UNDO_DELETE, APLOG_FAILURE, request_json if store_ap_json else None, 'Restorer did not have permisson')


def site_ban_remove_data(blocker_id, blocked):
    replies = PostReply.query.filter_by(user_id=blocked.id, deleted=False)
    for reply in replies:
        reply.deleted = True
        reply.deleted_by = blocker_id
        if not blocked.bot:
            reply.post.reply_count -= 1
        reply.community.post_reply_count -= 1
    blocked.reply_count = 0
    db.session.commit()

    posts = Post.query.filter_by(user_id=blocked.id, deleted=False)
    for post in posts:
        post.deleted = True
        post.deleted_by = blocker_id
        post.community.post_count -= 1
        if post.url and post.cross_posts is not None:
            old_cross_posts = Post.query.filter(Post.id.in_(post.cross_posts)).all()
            post.cross_posts.clear()
            for ocp in old_cross_posts:
                if ocp.cross_posts is not None and post.id in ocp.cross_posts:
                    ocp.cross_posts.remove(post.id)
    blocked.post_count = 0
    db.session.commit()

    # Delete all their images to save moderators from having to see disgusting stuff.
    # Images attached to posts can't be restored, but site ban reversals don't have a 'removeData' field anyway.
    files = File.query.join(Post).filter(Post.user_id == blocked.id).all()
    for file in files:
        file.delete_from_disk()
        file.source_url = ''
    if blocked.avatar_id:
        blocked.avatar.delete_from_disk()
        blocked.avatar.source_url = ''
    if blocked.cover_id:
        blocked.cover.delete_from_disk()
        blocked.cover.source_url = ''

    db.session.commit()


def remove_data_from_banned_user(deletor_ap_id, user_ap_id, target):
    if current_app.debug:
        remove_data_from_banned_user_task(deletor_ap_id, user_ap_id, target)
    else:
        remove_data_from_banned_user_task.delay(deletor_ap_id, user_ap_id, target)


@celery.task
def remove_data_from_banned_user_task(deletor_ap_id, user_ap_id, target):
    deletor = find_actor_or_create(deletor_ap_id, create_if_not_found=False)
    user = find_actor_or_create(user_ap_id, create_if_not_found=False)
    community = Community.query.filter_by(ap_profile_id=target).first()

    if not deletor or not user:
        return

    # site bans by admins
    if deletor.instance.user_is_admin(deletor.id) and target == f"https://{deletor.instance.domain}/" and deletor.instance_id == user.instance_id:
        post_replies = PostReply.query.filter_by(user_id=user.id)
        posts = Post.query.filter_by(user_id=user.id)

    # community bans by mods or admins
    elif community and (community.is_moderator(deletor) or community.is_instance_admin(deletor)):
        post_replies = PostReply.query.filter_by(user_id=user.id, community_id=community.id, deleted=False)
        posts = Post.query.filter_by(user_id=user.id, community_id=community.id, deleted=False)
    else:
        return

    for post_reply in post_replies:
        if not user.bot:
            post_reply.post.reply_count -= 1
        post_reply.deleted = True
        post_reply.deleted_by = deletor.id
    db.session.commit()

    for post in posts:
        if post.cross_posts:
            old_cross_posts = Post.query.filter(Post.id.in_(post.cross_posts)).all()
            for ocp in old_cross_posts:
                if ocp.cross_posts is not None:
                    ocp.cross_posts.remove(post.id)
        post.delete_dependencies()
        post.deleted = True
        post.community.post_count -= 1
    db.session.commit()


def community_ban_remove_data(blocker_id, community_id, blocked):
    replies = PostReply.query.filter_by(user_id=blocked.id, deleted=False, community_id=community_id)
    for reply in replies:
        reply.deleted = True
        reply.deleted_by = blocker_id
        if not blocked.bot:
            reply.post.reply_count -= 1
        reply.community.post_reply_count -= 1
        blocked.post_reply_count -= 1
    db.session.commit()

    posts = Post.query.filter_by(user_id=blocked.id, deleted=False, community_id=community_id)
    for post in posts:
        post.deleted = True
        post.deleted_by = blocker_id
        post.community.post_count -= 1
        if post.url and post.cross_posts is not None:
            old_cross_posts = Post.query.filter(Post.id.in_(post.cross_posts)).all()
            post.cross_posts.clear()
            for ocp in old_cross_posts:
                if ocp.cross_posts is not None and post.id in ocp.cross_posts:
                    ocp.cross_posts.remove(post.id)
        blocked.post_count -= 1
    db.session.commit()

    # Delete attached images to save moderators from having to see disgusting stuff.
    files = File.query.join(Post).filter(Post.user_id == blocked.id, Post.community_id == community_id).all()
    for file in files:
        file.delete_from_disk()
        file.source_url = ''
    db.session.commit()


def ban_user(blocker, blocked, community, request_json):
    existing = CommunityBan.query.filter_by(community_id=community.id, user_id=blocked.id).first()
    if not existing:
        new_ban = CommunityBan(community_id=community.id, user_id=blocked.id, banned_by=blocker.id)
        if 'summary' in request_json['object']:
            new_ban.reason=request_json['object']['summary']
            reason = request_json['object']['summary']
        else:
            reason = ''
        if 'expires' in request_json and datetime.fromisoformat(request_json['object']['expires']) > datetime.now(timezone.utc):
            new_ban.ban_until = datetime.fromisoformat(request_json['object']['expires'])
        elif 'endTime' in request_json and datetime.fromisoformat(request_json['object']['endTime']) > datetime.now(timezone.utc):
            new_ban.ban_until = datetime.fromisoformat(request_json['object']['endTime'])
        db.session.add(new_ban)

        community_membership_record = CommunityMember.query.filter_by(community_id=community.id, user_id=blocked.id).first()
        if community_membership_record:
            community_membership_record.is_banned = True
        db.session.commit()

        if blocked.is_local():
            db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.community_id == community.id, CommunityJoinRequest.user_id == blocked.id).delete()

            # Notify banned person
            notify = Notification(title=shorten_string('You have been banned from ' + community.title),
                                  url=f'/notifications', user_id=blocked.id,
                                  author_id=blocker.id)
            db.session.add(notify)
            if not current_app.debug:                           # user.unread_notifications += 1 hangs app if 'user' is the same person
                blocked.unread_notifications += 1               # who pressed 'Re-submit this activity'.

            # Remove their notification subscription,  if any
            db.session.query(NotificationSubscription).filter(NotificationSubscription.entity_id == community.id,
                                                              NotificationSubscription.user_id == blocked.id,
                                                              NotificationSubscription.type == NOTIF_COMMUNITY).delete()
            db.session.commit()

            cache.delete_memoized(communities_banned_from, blocked.id)
            cache.delete_memoized(joined_communities, blocked.id)
            cache.delete_memoized(moderating_communities, blocked.id)

        add_to_modlog_activitypub('ban_user', blocker, community_id=community.id, link_text=blocked.display_name(), link=f'u/{blocked.link()}', reason=reason)


def unban_user(blocker, blocked, community, request_json):
    reason = request_json['object']['summary'] if 'summary' in request_json['object'] else ''
    db.session.query(CommunityBan).filter(CommunityBan.community_id == community.id, CommunityBan.user_id == blocked.id).delete()
    community_membership_record = CommunityMember.query.filter_by(community_id=community.id, user_id=blocked.id).first()
    if community_membership_record:
        community_membership_record.is_banned = False
    db.session.commit()

    if blocked.is_local():
        # Notify unbanned person
        notify = Notification(title=shorten_string('You have been unbanned from ' + community.title),
                              url=f'/notifications', user_id=blocked.id, author_id=blocker.id)
        db.session.add(notify)
        if not current_app.debug:                           # user.unread_notifications += 1 hangs app if 'user' is the same person
            blocked.unread_notifications += 1               # who pressed 'Re-submit this activity'.

        db.session.commit()

        cache.delete_memoized(communities_banned_from, blocked.id)
        cache.delete_memoized(joined_communities, blocked.id)
        cache.delete_memoized(moderating_communities, blocked.id)

    add_to_modlog_activitypub('unban_user', blocker, community_id=community.id, link_text=blocked.display_name(), link=f'u/{blocked.link()}', reason=reason)


def create_post_reply(store_ap_json, community: Community, in_reply_to, request_json: dict, user: User, announce_id=None) -> Union[PostReply, None]:
    id = request_json['id']
    if community.local_only:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, request_json if store_ap_json else None, 'Community is local only, reply discarded')
        return None
    post_id, parent_comment_id, root_id = find_reply_parent(in_reply_to)

    if post_id or parent_comment_id or root_id:
        # set depth to +1 of the parent depth
        if parent_comment_id:
            parent_comment = PostReply.query.get(parent_comment_id)
        else:
            parent_comment = None
        if post_id is None:
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, request_json if store_ap_json else None, 'Could not find parent post')
            return None
        post = Post.query.get(post_id)

        body = body_html = ''
        if 'content' in request_json['object']:   # Kbin, Mastodon, etc provide their posts as html
            if not (request_json['object']['content'].startswith('<p>') or request_json['object']['content'].startswith('<blockquote>')):
                request_json['object']['content'] = '<p>' + request_json['object']['content'] + '</p>'
            body_html = allowlist_html(request_json['object']['content'])
            if 'source' in request_json['object'] and isinstance(request_json['object']['source'], dict) and \
                    'mediaType' in request_json['object']['source'] and request_json['object']['source']['mediaType'] == 'text/markdown':
                body = request_json['object']['source']['content']
                body_html = markdown_to_html(body)          # prefer Markdown if provided, overwrite version obtained from HTML
            else:
                body = html_to_text(body_html)

        # Language - Lemmy uses 'language' while Mastodon uses 'contentMap'
        language_id = None
        if 'language' in request_json['object'] and isinstance(request_json['object']['language'], dict):
            language = find_language_or_create(request_json['object']['language']['identifier'],
                                               request_json['object']['language']['name'])
            language_id = language.id
        elif 'contentMap' in request_json['object'] and isinstance(request_json['object']['contentMap'], dict):
            language = find_language(next(iter(request_json['object']['contentMap'])))  # Combination of next and iter gets the first key in a dict
            language_id = language.id if language else None

        try:
            post_reply = PostReply.new(user, post, parent_comment, notify_author=True, body=body, body_html=body_html,
                                       language_id=language_id, request_json=request_json, announce_id=announce_id)
            return post_reply
        except Exception as ex:
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, request_json if store_ap_json else None, str(ex))
            return None
    else:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, request_json if store_ap_json else None, 'Unable to find parent post/comment')
        return None


def create_post(store_ap_json, community: Community, request_json: dict, user: User, announce_id=None) -> Union[Post, None]:
    id = request_json['id']
    if community.local_only:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, request_json if store_ap_json else None, 'Community is local only, post discarded')
        return None
    try:
        post = Post.new(user, community, request_json, announce_id)
        return post
    except Exception as ex:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, request_json if store_ap_json else None, str(ex))
        return None


def notify_about_post(post: Post):
    # todo: eventually this function could trigger a lot of DB activity. This function will need to be a celery task.

    # Send notifications based on subscriptions
    notifications_sent_to = set()
    send_notifs_to = set(notification_subscribers(post.user_id, NOTIF_USER) +
                         notification_subscribers(post.community_id, NOTIF_COMMUNITY) +
                         notification_subscribers(post.community.topic_id, NOTIF_TOPIC))
    for notify_id in send_notifs_to:
        if notify_id != post.user_id and notify_id not in notifications_sent_to:
            new_notification = Notification(title=shorten_string(post.title, 50), url=f"/post/{post.id}",
                                            user_id=notify_id, author_id=post.user_id)
            db.session.add(new_notification)
            user = User.query.get(notify_id)
            user.unread_notifications += 1
            db.session.commit()
            notifications_sent_to.add(notify_id)


def notify_about_post_reply(parent_reply: Union[PostReply, None], new_reply: PostReply):

    if parent_reply is None:  # This happens when a new_reply is a top-level comment, not a comment on a comment
        send_notifs_to = notification_subscribers(new_reply.post.id, NOTIF_POST)
        for notify_id in send_notifs_to:
            if new_reply.user_id != notify_id:
                new_notification = Notification(title=shorten_string(_('Reply to %(post_title)s',
                                                                       post_title=new_reply.post.title), 50),
                                                url=f"/post/{new_reply.post.id}#comment_{new_reply.id}",
                                                user_id=notify_id, author_id=new_reply.user_id)
                db.session.add(new_notification)
                user = User.query.get(notify_id)
                user.unread_notifications += 1
                db.session.commit()
    else:
        # Send notifications based on subscriptions
        send_notifs_to = set(notification_subscribers(parent_reply.id, NOTIF_REPLY))
        for notify_id in send_notifs_to:
            if new_reply.user_id != notify_id:
                if new_reply.depth <= THREAD_CUTOFF_DEPTH:
                    new_notification = Notification(title=shorten_string(_('Reply to comment on %(post_title)s',
                                                                           post_title=parent_reply.post.title), 50),
                                                    url=f"/post/{parent_reply.post.id}#comment_{new_reply.id}",
                                                    user_id=notify_id, author_id=new_reply.user_id)
                else:
                    new_notification = Notification(title=shorten_string(_('Reply to comment on %(post_title)s',
                                                                           post_title=parent_reply.post.title), 50),
                                                    url=f"/post/{parent_reply.post.id}/comment/{parent_reply.id}#comment_{new_reply.id}",
                                                    user_id=notify_id, author_id=new_reply.user_id)
                db.session.add(new_notification)
                user = User.query.get(notify_id)
                user.unread_notifications += 1
                db.session.commit()


def update_post_reply_from_activity(reply: PostReply, request_json: dict):
    if 'content' in request_json['object']:   # Kbin, Mastodon, etc provide their posts as html
        if not (request_json['object']['content'].startswith('<p>') or request_json['object']['content'].startswith('<blockquote>')):
            request_json['object']['content'] = '<p>' + request_json['object']['content'] + '</p>'
        reply.body_html = allowlist_html(request_json['object']['content'])
        if 'source' in request_json['object'] and isinstance(request_json['object']['source'], dict) and \
            'mediaType' in request_json['object']['source'] and request_json['object']['source']['mediaType'] == 'text/markdown':
            reply.body = request_json['object']['source']['content']
            reply.body_html = markdown_to_html(reply.body)          # prefer Markdown if provided, overwrite version obtained from HTML
        else:
            reply.body = html_to_text(reply.body_html)
    # Language
    if 'language' in request_json['object'] and isinstance(request_json['object']['language'], dict):
        language = find_language_or_create(request_json['object']['language']['identifier'], request_json['object']['language']['name'])
        reply.language_id = language.id
    reply.edited_at = utcnow()
    db.session.commit()


def update_post_from_activity(post: Post, request_json: dict):
    # redo body without checking if it's changed
    if 'content' in request_json['object'] and request_json['object']['content'] is not None:
        if 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/html':
            post.body_html = allowlist_html(request_json['object']['content'])
            if 'source' in request_json['object'] and isinstance(request_json['object']['source'], dict) and request_json['object']['source']['mediaType'] == 'text/markdown':
                post.body = request_json['object']['source']['content']
                post.body_html = markdown_to_html(post.body)          # prefer Markdown if provided, overwrite version obtained from HTML
            else:
                post.body = html_to_text(post.body_html)
        elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/markdown':
            post.body = request_json['object']['content']
            post.body_html = markdown_to_html(post.body)
        else:
            if not (request_json['object']['content'].startswith('<p>') or request_json['object']['content'].startswith('<blockquote>')):
                request_json['object']['content'] = '<p>' + request_json['object']['content'] + '</p>'
            post.body_html = allowlist_html(request_json['object']['content'])
            post.body = html_to_text(post.body_html)

    # title
    old_title = post.title
    if 'name' in request_json['object']:
        new_title = request_json['object']['name']
        post.microblog = False
    else:
        autogenerated_title = microblog_content_to_title(post.body_html)
        if len(autogenerated_title) < 20:
            new_title = '[Microblog] ' + autogenerated_title.strip()
        else:
            new_title = autogenerated_title.strip()
        post.microblog = True

    if old_title != new_title:
        post.title = new_title
        if '[NSFL]' in new_title.upper() or '(NSFL)' in new_title.upper():
            post.nsfl = True
        if '[NSFW]' in new_title.upper() or '(NSFW)' in new_title.upper():
            post.nsfw = True
    if 'sensitive' in request_json['object']:
        post.nsfw = request_json['object']['sensitive']
    if 'nsfl' in request_json['object']:
        post.nsfl = request_json['object']['nsfl']

    # Language
    old_language_id = post.language_id
    new_language = None
    if 'language' in request_json['object'] and isinstance(request_json['object']['language'], dict):
        new_language = find_language_or_create(request_json['object']['language']['identifier'], request_json['object']['language']['name'])
    elif 'contentMap' in request_json['object'] and isinstance(request_json['object']['contentMap'], dict):
        new_language = find_language(next(iter(request_json['object']['contentMap'])))
    if new_language and (new_language.id != old_language_id):
        post.language_id = new_language.id

    # Tags
    if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
        db.session.execute(text('DELETE FROM "post_tag" WHERE post_id = :post_id'), {'post_id': post.id})
        for json_tag in request_json['object']['tag']:
            if json_tag['type'] == 'Hashtag':
                if json_tag['name'][1:].lower() != post.community.name.lower():             # Lemmy adds the community slug as a hashtag on every post in the community, which we want to ignore
                    hashtag = find_hashtag_or_create(json_tag['name'])
                    if hashtag:
                        post.tags.append(hashtag)

    post.comments_enabled = request_json['object']['commentsEnabled'] if 'commentsEnabled' in request_json['object'] else True
    post.edited_at = utcnow()

    if request_json['object']['type'] == 'Video':
        # return now for PeerTube, otherwise rest of this function breaks the post
        # consider querying the Likes endpoint (that mostly seems to be what Updates are about)
        return

    # Links
    old_url = post.url
    new_url = None
    if ('attachment' in request_json['object'] and
        isinstance(request_json['object']['attachment'], list) and
        len(request_json['object']['attachment']) > 0 and
        'type' in request_json['object']['attachment'][0]):
        if request_json['object']['attachment'][0]['type'] == 'Link':
            new_url = request_json['object']['attachment'][0]['href']              # Lemmy < 0.19.4
        if request_json['object']['attachment'][0]['type'] == 'Document':
            new_url = request_json['object']['attachment'][0]['url']               # Mastodon
        if request_json['object']['attachment'][0]['type'] == 'Image':
            new_url = request_json['object']['attachment'][0]['url']               # PixelFed / PieFed / Lemmy >= 0.19.4
    if 'attachment' in request_json['object'] and isinstance(request_json['object']['attachment'], dict):   # Mastodon / a.gup.pe
        new_url = request_json['object']['attachment']['url']
    if new_url:
        new_url = remove_tracking_from_link(new_url)
        new_domain = domain_from_url(new_url)
        if new_domain.banned:
            db.session.commit()
            return                                                                  # reject change to url if new domain is banned
    old_db_entry_to_delete = None
    if old_url != new_url:
        if post.image:
            post.image.delete_from_disk()
            old_db_entry_to_delete = post.image_id
        if new_url:
            post.url = new_url
            image = None
            if is_image_url(new_url):
                post.type = POST_TYPE_IMAGE
                image = File(source_url=new_url)
                if 'name' in request_json['object']['attachment'][0] and request_json['object']['attachment'][0]['name'] is not None:
                    image.alt_text = request_json['object']['attachment'][0]['name']
            else:
                if 'image' in request_json['object'] and 'url' in request_json['object']['image']:
                    image = File(source_url=request_json['object']['image']['url'])
                else:
                    # Let's see if we can do better than the source instance did!
                    tn_url = new_url
                    if tn_url[:32] == 'https://www.youtube.com/watch?v=':
                        tn_url = 'https://youtu.be/' + tn_url[32:43]  # better chance of thumbnail from youtu.be than youtube.com
                    opengraph = opengraph_parse(tn_url)
                    if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                        filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                        if not filename.startswith('/'):
                            image = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                if is_video_hosting_site(new_url) or is_video_url(new_url):
                    post.type = POST_TYPE_VIDEO
                else:
                    post.type = POST_TYPE_LINK
            if image:
                db.session.add(image)
                db.session.commit()
                post.image = image
                make_image_sizes(image.id, 170, 512, 'posts')  # the 512 sized image is for masonry view
            else:
                old_db_entry_to_delete = None

            # url domain
            old_domain = domain_from_url(old_url) if old_url else None
            if old_domain != new_domain:
                # notify about links to banned websites.
                already_notified = set()  # often admins and mods are the same people - avoid notifying them twice
                if new_domain.notify_mods:
                    for community_member in post.community.moderators():
                        notify = Notification(title='Suspicious content', url=post.ap_id,
                                                  user_id=community_member.user_id,
                                                  author_id=1)
                        db.session.add(notify)
                        already_notified.add(community_member.user_id)
                if new_domain.notify_admins:
                    for admin in Site.admins():
                        if admin.id not in already_notified:
                            notify = Notification(title='Suspicious content',
                                                      url=post.ap_id, user_id=admin.id,
                                                      author_id=1)
                            db.session.add(notify)
                new_domain.post_count += 1
                post.domain = new_domain

            # Fix-up cross posts (Posts which link to the same url as other posts)
            if post.cross_posts is not None:
                old_cross_posts = Post.query.filter(Post.id.in_(post.cross_posts)).all()
                post.cross_posts.clear()
                for ocp in old_cross_posts:
                    if ocp.cross_posts is not None and post.id in ocp.cross_posts:
                        ocp.cross_posts.remove(post.id)

            new_cross_posts = Post.query.filter(Post.id != post.id, Post.url == new_url, Post.deleted == False,
                                    Post.posted_at > utcnow() - timedelta(days=6)).all()
            for ncp in new_cross_posts:
                if ncp.cross_posts is None:
                    ncp.cross_posts = [post.id]
                else:
                    ncp.cross_posts.append(post.id)
                if post.cross_posts is None:
                    post.cross_posts = [ncp.id]
                else:
                    post.cross_posts.append(ncp.id)

        else:
            post.type = POST_TYPE_ARTICLE
            post.url = ''
            post.image_id = None
            if post.cross_posts is not None:                    # unlikely, but not impossible
                old_cross_posts = Post.query.filter(Post.id.in_(post.cross_posts)).all()
                post.cross_posts.clear()
                for ocp in old_cross_posts:
                    if ocp.cross_posts is not None and post.id in ocp.cross_posts:
                        ocp.cross_posts.remove(post.id)

    db.session.commit()
    if old_db_entry_to_delete:
        File.query.filter_by(id=old_db_entry_to_delete).delete()
        db.session.commit()


def undo_downvote(activity_log, comment, post, target_ap_id, user):
    if '/comment/' in target_ap_id:
        comment = PostReply.query.filter_by(ap_id=target_ap_id).first()
    if '/post/' in target_ap_id:
        post = Post.query.filter_by(ap_id=target_ap_id).first()
    if (user and not user.is_local()) and post:
        existing_vote = PostVote.query.filter_by(user_id=user.id, post_id=post.id).first()
        if existing_vote:
            post.author.reputation -= existing_vote.effect
            post.down_votes -= 1
            post.score -= existing_vote.effect
            db.session.delete(existing_vote)
            activity_log.result = 'success'
    if (user and not user.is_local()) and comment:
        existing_vote = PostReplyVote.query.filter_by(user_id=user.id,
                                                      post_reply_id=comment.id).first()
        if existing_vote:
            comment.author.reputation -= existing_vote.effect
            comment.down_votes -= 1
            comment.score -= existing_vote.effect
            db.session.delete(existing_vote)
            activity_log.result = 'success'
    if user is None:
        activity_log.exception_message = 'Blocked or unfound user'
    if user and user.is_local():
        activity_log.exception_message = 'Activity about local content which is already present'
        activity_log.result = 'ignored'
    return post


def undo_vote(comment, post, target_ap_id, user):
    voted_on = find_liked_object(target_ap_id)
    if isinstance(voted_on, Post):
        post = voted_on
        existing_vote = PostVote.query.filter_by(user_id=user.id, post_id=post.id).first()
        if existing_vote:
            post.author.reputation -= existing_vote.effect
            if existing_vote.effect < 0:  # Lemmy sends 'like' for upvote and 'dislike' for down votes. Cool! When it undoes an upvote it sends an 'Undo Like'. Fine. When it undoes a downvote it sends an 'Undo Like' - not 'Undo Dislike'?!
                post.down_votes -= 1
            else:
                post.up_votes -= 1
            post.score -= existing_vote.effect
            db.session.delete(existing_vote)
            db.session.commit()
        return post
    if isinstance(voted_on, PostReply):
        comment = voted_on
        existing_vote = PostReplyVote.query.filter_by(user_id=user.id, post_reply_id=comment.id).first()
        if existing_vote:
            comment.author.reputation -= existing_vote.effect
            if existing_vote.effect < 0:  # Lemmy sends 'like' for upvote and 'dislike' for down votes. Cool! When it undoes an upvote it sends an 'Undo Like'. Fine. When it undoes a downvote it sends an 'Undo Like' - not 'Undo Dislike'?!
                comment.down_votes -= 1
            else:
                comment.up_votes -= 1
            comment.score -= existing_vote.effect
            db.session.delete(existing_vote)
            db.session.commit()
        return comment

    return None


def process_report(user, reported, request_json):
    if len(request_json['summary']) < 15:
        reasons = request_json['summary']
        description = ''
    else:
        reasons = request_json['summary'][:15]
        description = request_json['summary'][15:]
    if isinstance(reported, User):
        if reported.reports == -1:
            return
        type = 0
        report = Report(reasons=reasons, description=description,
                        type=type, reporter_id=user.id, suspect_user_id=reported.id, source_instance_id=user.instance_id)
        db.session.add(report)

        # Notify site admin
        already_notified = set()
        for admin in Site.admins():
            if admin.id not in already_notified:
                notify = Notification(title='Reported user', url='/admin/reports', user_id=admin.id,
                                      author_id=user.id)
                db.session.add(notify)
                admin.unread_notifications += 1
        reported.reports += 1
        db.session.commit()
    elif isinstance(reported, Post):
        if reported.reports == -1:
            return
        type = 1
        report = Report(reasons=reasons, description=description, type=type, reporter_id=user.id,
                        suspect_user_id=reported.author.id, suspect_post_id=reported.id,
                        suspect_community_id=reported.community.id, in_community_id=reported.community.id,
                        source_instance_id=user.instance_id)
        db.session.add(report)

        already_notified = set()
        for mod in reported.community.moderators():
            notification = Notification(user_id=mod.user_id, title=_('A post has been reported'),
                                        url=f"https://{current_app.config['SERVER_NAME']}/post/{reported.id}",
                                        author_id=user.id)
            db.session.add(notification)
            already_notified.add(mod.user_id)
        reported.reports += 1
        db.session.commit()
    elif isinstance(reported, PostReply):
        if reported.reports == -1:
            return
        type = 2
        post = Post.query.get(reported.post_id)
        report = Report(reasons=reasons, description=description, type=type, reporter_id=user.id, suspect_post_id=post.id,
                        suspect_community_id=post.community.id,
                        suspect_user_id=reported.author.id, suspect_post_reply_id=reported.id,
                        in_community_id=post.community.id,
                        source_instance_id=user.instance_id)
        db.session.add(report)
        # Notify moderators
        already_notified = set()
        for mod in post.community.moderators():
            notification = Notification(user_id=mod.user_id, title=_('A comment has been reported'),
                                        url=f"https://{current_app.config['SERVER_NAME']}/comment/{reported.id}",
                                        author_id=user.id)
            db.session.add(notification)
            already_notified.add(mod.user_id)
        reported.reports += 1
        db.session.commit()
    elif isinstance(reported, Community):
        ...
    elif isinstance(reported, Conversation):
        ...


def get_redis_connection() -> redis.Redis:
    connection_string = current_app.config['CACHE_REDIS_URL']
    if connection_string.startswith('unix://'):
        unix_socket_path, db, password = parse_redis_pipe_string(connection_string)
        return redis.Redis(unix_socket_path=unix_socket_path, db=db, password=password)
    else:
        host, port, db, password = parse_redis_socket_string(connection_string)
        return redis.Redis(host=host, port=port, db=db, password=password)


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

    # Extract username (if provided) and password
    if parsed_url.username:
        username = parsed_url.username
    else:
        username = None
    password = parsed_url.password

    # Extract host and port
    host = parsed_url.hostname
    port = parsed_url.port

    # Extract database number (default to 0 if not provided)
    db = int(parsed_url.path.lstrip('/') or 0)

    return host, port, db, password


def lemmy_site_data():
    site = g.site
    logo = site.logo if site.logo else '/static/images/logo2.png'
    data = {
      "site_view": {
        "site": {
          "id": 1,
          "name": site.name,
          "sidebar": site.sidebar,
          "published": site.created_at.isoformat(),
          "updated": site.updated.isoformat(),
          "icon": f"https://{current_app.config['SERVER_NAME']}{logo}",
          "banner": "",
          "description": site.description,
          "actor_id": f"https://{current_app.config['SERVER_NAME']}/",
          "last_refreshed_at": site.updated.isoformat(),
          "inbox_url": f"https://{current_app.config['SERVER_NAME']}/inbox",
          "public_key": site.public_key,
          "instance_id": 1
        },
        "local_site": {
          "id": 1,
          "site_id": 1,
          "site_setup": True,
          "enable_downvotes": site.enable_downvotes,
          "enable_nsfw": site.enable_nsfw,
          "enable_nsfl": site.enable_nsfl,
          "community_creation_admin_only": site.community_creation_admin_only,
          "require_email_verification": True,
          "application_question": site.application_question,
          "private_instance": False,
          "default_theme": "browser",
          "default_post_listing_type": "All",
          "hide_modlog_mod_names": True,
          "application_email_admins": True,
          "actor_name_max_length": 20,
          "federation_enabled": True,
          "captcha_enabled": True,
          "captcha_difficulty": "medium",
          "published": site.created_at.isoformat(),
          "updated": site.updated.isoformat(),
          "registration_mode": site.registration_mode,
          "reports_email_admins": site.reports_email_admins
        },
        "local_site_rate_limit": {
          "id": 1,
          "local_site_id": 1,
          "message": 999,
          "message_per_second": 60,
          "post": 50,
          "post_per_second": 600,
          "register": 20,
          "register_per_second": 3600,
          "image": 100,
          "image_per_second": 3600,
          "comment": 100,
          "comment_per_second": 600,
          "search": 999,
          "search_per_second": 600,
          "published": site.created_at.isoformat(),
        },
        "counts": {
          "id": 1,
          "site_id": 1,
          "users": users_total(),
          "posts": local_posts(),
          "comments": local_comments(),
          "communities": local_communities(),
          "users_active_day": active_day(),
          "users_active_week": active_week(),
          "users_active_month": active_month(),
          "users_active_half_year": active_half_year()
        }
      },
      "admins": [],
      "version": "1.0.0",
      "all_languages": [],
      "discussion_languages": [],
      "taglines": [],
      "custom_emojis": []
    }

    # Languages
    discussion_languages = []
    for language in Language.query.all():
        # hardcode English as the site language, for now. This will need to be an admin setting, soon.
        if language.code == 'und' or language.code == 'en':
            discussion_languages.append(language.id)
        data['all_languages'].append({
            'id': language.id,
            'code': language.code,
            'name': language.name
        })
    data['discussion_languages'] = discussion_languages

    # Admins
    for admin in Site.admins():
        person = {
            "id": admin.id,
            "name": admin.user_name,
            "display_name": admin.display_name(),
            "avatar": 'https://' + current_app.config['SERVER_NAME'] + admin.avatar_image(),
            "banned": admin.banned,
            "published": admin.created.isoformat() + 'Z',
            "updated": admin.created.isoformat() + 'Z',
            "actor_id": admin.public_url(),
            "local": True,
            "deleted": admin.deleted,
            "matrix_user_id": admin.matrix_user_id,
            "admin": True,
            "bot_account": admin.bot,
            "instance_id": 1
        }
        counts = {
            "id": admin.id,
            "person_id": admin.id,
            "post_count": 0,
            "post_score": 0,
            "comment_count": 0,
            "comment_score": 0
        }
        data['admins'].append({'person': person, 'counts': counts})
    return data


def ensure_domains_match(activity: dict) -> bool:
    if 'id' in activity:
        note_id = activity['id']
    else:
        note_id =  None

    note_actor = None
    if 'actor' in activity:
        note_actor = activity['actor']
    elif 'attributedTo' in activity and isinstance(activity['attributedTo'], str):
        note_actor = activity['attributedTo']
    elif 'attributedTo' in activity and isinstance(activity['attributedTo'], list):
        for a in activity['attributedTo']:
            if a['type'] == 'Person':
                note_actor = a['id']
                break

    if note_id and note_actor:
        parsed_url = urlparse(note_id)
        id_domain = parsed_url.netloc
        parsed_url = urlparse(note_actor)
        actor_domain = parsed_url.netloc

        if id_domain == actor_domain:
            return True

    return False


def can_edit(user_ap_id, post):
    user = find_actor_or_create(user_ap_id, create_if_not_found=False)
    if user:
        if post.user_id == user.id:
            return True
        if post.community.is_moderator(user) or post.community.is_owner(user) or post.community.is_instance_admin(user):
            return True
    return False


def can_delete(user_ap_id, post):
    return can_edit(user_ap_id, post)


def resolve_remote_post(uri: str, community_id: int, announce_actor=None, store_ap_json=False) -> Union[Post, PostReply, None]:
    post = Post.query.filter_by(ap_id=uri).first()
    if post:
        return post

    community = Community.query.get(community_id)
    site = Site.query.get(1)

    parsed_url = urlparse(uri)
    uri_domain = parsed_url.netloc
    if announce_actor:
        parsed_url = urlparse(announce_actor)
        announce_actor_domain = parsed_url.netloc
        if announce_actor_domain != 'a.gup.pe' and announce_actor_domain != uri_domain:
            return None
    actor_domain = None
    actor = None
    post_request = get_request(uri, headers={'Accept': 'application/activity+json'})
    if post_request.status_code == 200:
        post_data = post_request.json()
        post_request.close()
        # check again that it doesn't already exist (can happen with different but equivalent URLs)
        post = Post.query.filter_by(ap_id=post_data['id']).first()
        if post:
            return post
        if 'attributedTo' in post_data:
            if isinstance(post_data['attributedTo'], str):
                actor = post_data['attributedTo']
                parsed_url = urlparse(post_data['attributedTo'])
                actor_domain = parsed_url.netloc
            elif isinstance(post_data['attributedTo'], list):
                for a in post_data['attributedTo']:
                    if a['type'] == 'Person':
                        actor = a['id']
                        parsed_url = urlparse(a['id'])
                        actor_domain = parsed_url.netloc
                        break
        if uri_domain != actor_domain:
            return None

        if not announce_actor:
            # make sure that the post actually belongs in the community a user says it does
            remote_community = None
            if post_data['type'] == 'Page':                                          # lemmy
                remote_community = post_data['audience'] if 'audience' in post_data else None
                if remote_community and remote_community.lower() != community.ap_profile_id:
                    return None
            elif post_data['type'] == 'Video':                                       # peertube
                if 'attributedTo' in post_data and isinstance(post_data['attributedTo'], list):
                    for a in post_data['attributedTo']:
                        if a['type'] == 'Group':
                            remote_community = a['id']
                            break
                if remote_community and remote_community.lower() != community.ap_profile_id:
                    return None
            else:                                                                   # mastodon, etc
                if 'inReplyTo' not in post_data or post_data['inReplyTo'] != None:
                    return None
                community_found = False
                if not community_found and 'to' in post_data and isinstance(post_data['to'], str):
                    remote_community = post_data['to']
                    if remote_community.lower() == community.ap_profile_id:
                        community_found = True
                if not community_found and 'cc' in post_data and isinstance(post_data['cc'], str):
                    remote_community = post_data['cc']
                    if remote_community.lower() == community.ap_profile_id:
                        community_found = True
                if not community_found and 'to' in post_data and isinstance(post_data['to'], list):
                    for t in post_data['to']:
                        if t.lower() == community.ap_profile_id:
                            community_found = True
                            break
                if not community_found and 'cc' in post_data and isinstance(post_data['cc'], list):
                    for c in post_data['cc']:
                        if c.lower() == community.ap_profile_id:
                            community_found = True
                            break
                if not community_found:
                    return None

        user = find_actor_or_create(actor)
        if user and community and post_data:
            request_json = {
              'id': f"https://{uri_domain}/activities/create/{gibberish(15)}",
              'object': post_data
            }
            if 'inReplyTo' in request_json['object'] and request_json['object']['inReplyTo']:
                post_reply = create_post_reply(store_ap_json, community, request_json['object']['inReplyTo'], request_json, user)
                if post_reply:
                    if 'published' in post_data:
                        post_reply.posted_at = post_data['published']
                        post_reply.post.last_active = post_data['published']
                        post_reply.community.last_active = utcnow()
                        db.session.commit()
                    return post_reply
            else:
                post = create_post(store_ap_json, community, request_json, user)
                if post:
                    if 'published' in post_data:
                        post.posted_at=post_data['published']
                        post.last_active=post_data['published']
                        post.community.last_active = utcnow()
                        db.session.commit()
                    return post

    return None


def resolve_remote_post_from_search(uri: str) -> Union[Post, None]:
    post = Post.query.filter_by(ap_id=uri).first()
    if post:
        return post

    site = Site.query.get(1)

    parsed_url = urlparse(uri)
    uri_domain = parsed_url.netloc
    actor_domain = None
    actor = None
    post_request = get_request(uri, headers={'Accept': 'application/activity+json'})
    if post_request.status_code == 200:
        post_data = post_request.json()
        post_request.close()
        # check again that it doesn't already exist (can happen with different but equivalent URLs)
        post = Post.query.filter_by(ap_id=post_data['id']).first()
        if post:
            return post

        # find the author of the post. Make sure their domain matches the site hosting it to migitage impersonation attempts
        if 'attributedTo' in post_data:
            if isinstance(post_data['attributedTo'], str):
                actor = post_data['attributedTo']
                parsed_url = urlparse(post_data['attributedTo'])
                actor_domain = parsed_url.netloc
            elif isinstance(post_data['attributedTo'], list):
                for a in post_data['attributedTo']:
                    if a['type'] == 'Person':
                        actor = a['id']
                        parsed_url = urlparse(a['id'])
                        actor_domain = parsed_url.netloc
                        break
        if uri_domain != actor_domain:
            return None

        # find the community the post was submitted to
        community = None
        if not community and post_data['type'] == 'Page':                                         # lemmy
            if 'audience' in post_data:
                community_id = post_data['audience']
                community = find_actor_or_create(community_id, community_only=True)

        if not community and post_data['type'] == 'Video':                                        # peertube
            if 'attributedTo' in post_data and isinstance(post_data['attributedTo'], list):
                for a in post_data['attributedTo']:
                    if a['type'] == 'Group':
                        community_id = a['id']
                        community = find_actor_or_create(community_id, community_only=True)
                        if community:
                            break

        if not community:                                                                         # mastodon, etc
            if 'inReplyTo' not in post_data or post_data['inReplyTo'] != None:
                return None

        if not community and 'to' in post_data and isinstance(post_data['to'], str):
            community_id = post_data['to'].lower()
            if not community_id == 'https://www.w3.org/ns/activitystreams#Public' and not community_id.endswith('/followers'):
                community = Community.query.filter_by(ap_profile_id=community_id).first()
        if not community and 'cc' in post_data and isinstance(post_data['cc'], str):
            community_id = post_data['cc'].lower()
            if not community_id == 'https://www.w3.org/ns/activitystreams#Public' and not community_id.endswith('/followers'):
                community = Community.query.filter_by(ap_profile_id=community_id).first()
        if not community and 'to' in post_data and isinstance(post_data['to'], list):
            for t in post_data['to']:
                community_id = t.lower()
                if not community_id == 'https://www.w3.org/ns/activitystreams#Public' and not community_id.endswith('/followers'):
                    community = Community.query.filter_by(ap_profile_id=community_id).first()
                    if community:
                        break
        if not community and 'cc' in post_data and isinstance(post_data['to'], list):
            for c in post_data['cc']:
                community_id = c.lower()
                if not community_id == 'https://www.w3.org/ns/activitystreams#Public' and not community_id.endswith('/followers'):
                    community = Community.query.filter_by(ap_profile_id=community_id).first()
                    if community:
                        break

        if not community:
            return None

        activity_log = ActivityPubLog(direction='in', activity_id=post_data['id'], activity_type='Resolve Post', result='failure')
        if site.log_activitypub_json:
            activity_log.activity_json = json.dumps(post_data)
        db.session.add(activity_log)
        user = find_actor_or_create(actor)
        if user and community and post_data:
            request_json = {
              'id': f"https://{uri_domain}/activities/create/gibberish(15)",
              'object': post_data
            }
            post = create_post(activity_log, community, request_json, user)
            if post:
                if 'published' in post_data:
                    post.posted_at=post_data['published']
                    post.last_active=post_data['published']
                    db.session.commit()
                return post

    return None


# This is for followers on microblog apps
# Used to let them know a Poll has been updated with a new vote
# The plan is to also use it for activities on local user's posts that aren't understood by being Announced (anything beyond the initial Create)
# This would need for posts to have things like a 'Replies' collection and a 'Likes' collection, so these can be downloaded when the post updates
# Using collecions like this (as PeerTube does) circumvents the problem of not having a remote user's private key.
# The problem of what to do for remote user's activity on a remote user's post in a local community still exists (can't Announce it, can't inform of post update)
def inform_followers_of_post_update(post_id: int, sending_instance_id: int):
    if current_app.debug:
        inform_followers_of_post_update_task(post_id, sending_instance_id)
    else:
        inform_followers_of_post_update_task.delay(post_id, sending_instance_id)


@celery.task
def inform_followers_of_post_update_task(post_id: int, sending_instance_id: int):
    post = Post.query.get(post_id)
    page_json = post_to_page(post)
    page_json['updated'] = ap_datetime(utcnow())
    update_json = {
        'id': f"https://{current_app.config['SERVER_NAME']}/activities/update/{gibberish(15)}",
        'type': 'Update',
        'actor': post.author.public_url(),
        'audience': post.community.public_url(),
        'to': ['https://www.w3.org/ns/activitystreams#Public'],
        'published': ap_datetime(utcnow()),
        'cc': [
            post.author.followers_url(), post.community.ap_followers_url
        ],
        'object': page_json,
    }

    # inform user followers first
    followers = UserFollower.query.filter_by(local_user_id=post.user_id)
    if followers:
        instances = Instance.query.join(User, User.instance_id == Instance.id).join(UserFollower, UserFollower.remote_user_id == User.id)
        instances = instances.filter(UserFollower.local_user_id == post.user_id, Instance.software.in_(MICROBLOG_APPS))
        for i in instances:
            if sending_instance_id != i.id:
                try:
                    post_request(i.inbox, update_json, post.author.private_key, post.author.public_url() + '#main-key')
                except Exception:
                    pass

    # then community followers
    instances = Instance.query.join(User, User.instance_id == Instance.id).join(CommunityMember, CommunityMember.user_id == User.id)
    instances = instances.filter(CommunityMember.community_id == post.community.id, CommunityMember.is_banned == False)
    instances = instances.filter(Instance.software.in_(MICROBLOG_APPS))
    for i in instances:
        if sending_instance_id != i.id:
            try:
                post_request(i.inbox, update_json, post.author.private_key, post.author.public_url() + '#main-key')
            except Exception:
                pass


def log_incoming_ap(id, aplog_type, aplog_result, request_json, message=None):
    aplog_in = APLOG_IN

    if aplog_in and aplog_type[0] and aplog_result[0]:
        activity_log = ActivityPubLog(direction='in', activity_id=id, activity_type=aplog_type[1], result=aplog_result[1])
        if message:
            activity_log.exception_message = message
        if request_json:
            activity_log.activity_json = json.dumps(request_json)
        db.session.add(activity_log)
        db.session.commit()


def find_community_ap_id(request_json):
    locations = ['audience', 'cc', 'to']
    if 'object' in request_json and isinstance(request_json['object'], dict):
        rjs = [request_json, request_json['object']]
    else:
        rjs = [request_json]
    for rj in rjs:
        for location in locations:
            if location in rj:
                potential_id = rj[location]
                if isinstance(potential_id, str):
                    if not potential_id.startswith('https://www.w3.org') and not potential_id.endswith('/followers'):
                        potential_community = Community.query.filter_by(ap_profile_id=potential_id.lower()).first()
                        if potential_community:
                            return potential_id
                if isinstance(potential_id, list):
                    for c in potential_id:
                        if not c.startswith('https://www.w3.org') and not c.endswith('/followers'):
                            potential_community = Community.query.filter_by(ap_profile_id=c.lower()).first()
                            if potential_community:
                                return c

    if not 'object' in request_json:
        return None

    if 'inReplyTo' in request_json['object'] and request_json['object']['inReplyTo'] is not None:
        post_being_replied_to = Post.query.filter_by(ap_id=request_json['object']['inReplyTo']).first()
        if post_being_replied_to:
            return post_being_replied_to.community.ap_profile_id
        else:
            comment_being_replied_to = PostReply.query.filter_by(ap_id=request_json['object']['inReplyTo']).first()
            if comment_being_replied_to:
                return comment_being_replied_to.community.ap_profile_id

    if request_json['object']['type'] == 'Video': # PeerTube
        if 'attributedTo' in request_json['object'] and isinstance(request_json['object']['attributedTo'], list):
            for a in request_json['object']['attributedTo']:
                if a['type'] == 'Group':
                    return a['id']

    return None
