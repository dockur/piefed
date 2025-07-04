from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from json import JSONDecodeError
from random import randint
from typing import Union, Tuple, List
from urllib.parse import urlparse, parse_qs

import arrow
import boto3
import httpx
import pytesseract
from PIL import Image, ImageOps
from flask import current_app, request, g, url_for, json
from flask_babel import _, force_locale, gettext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db, cache, celery
from app.activitypub.signature import signed_get_request
from app.constants import *
from app.models import User, Post, Community, File, PostReply, AllowedInstances, Instance, utcnow, \
    PostVote, PostReplyVote, ActivityPubLog, Notification, Site, CommunityMember, InstanceRole, Report, Conversation, \
    Language, Tag, Poll, PollChoice, CommunityBan, CommunityJoinRequest, NotificationSubscription, \
    Licence, UserExtraField, Feed, FeedMember, FeedItem, CommunityFlair, UserFlair, Topic
from app.utils import get_request, allowlist_html, get_setting, ap_datetime, markdown_to_html, \
    is_image_url, domain_from_url, gibberish, ensure_directory_exists, head_request, \
    shorten_string, fixup_url, \
    microblog_content_to_title, is_video_url, \
    notification_subscribers, communities_banned_from, html_to_text, add_to_modlog_activitypub, joined_communities, \
    moderating_communities, get_task_session, is_video_hosting_site, opengraph_parse, mastodon_extra_field_link, \
    blocked_users, piefed_markdown_to_lemmy_markdown, store_files_in_s3, guess_mime_type, get_recipient_language


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
    return db.session.execute(
        text('SELECT COUNT(id) as c FROM "post" WHERE instance_id = 1 AND deleted is false')).scalar()


def local_comments():
    return db.session.execute(
        text('SELECT COUNT(id) as c FROM "post_reply" WHERE instance_id = 1 and deleted is false')).scalar()


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
    replies = PostReply.query.filter_by(post_id=post_id, deleted=False).order_by(PostReply.posted_at).limit(2000)
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
        'distinguished': reply.distinguished,
        'audience': reply.community.public_url(),
        'language': {
            'identifier': reply.language_code(),
            'name': reply.language_name()
        },
        'flair': reply.author.community_flair(reply.community_id)
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
def instance_allowed(host: str) -> bool:
    if host is None or host == '':
        return True
    host = host.lower()
    if 'https://' in host or 'http://' in host:
        host = urlparse(host).hostname
    instance = AllowedInstances.query.filter_by(domain=host.strip()).first()
    return instance is not None


def find_actor_or_create(actor: str, create_if_not_found=True, community_only=False, feed_only=False) -> Union[User, Community, Feed, None]:
    """Find an actor by URL or webfinger, optionally creating it if not found.
    """
    from app.activitypub.actor import find_actor_by_url, validate_remote_actor
    if isinstance(actor, dict):
        actor = actor['id']

    actor_url = actor.strip()
    if not validate_remote_actor(actor_url):
        return None

    # Find the actor
    actor_obj = find_actor_by_url(actor_url, community_only, feed_only)

    if actor_obj is False:  # banned or deleted actor was found
        return None

    if actor_obj:
        # Schedule a refresh if needed
        from app.activitypub.actor import schedule_actor_refresh
        schedule_actor_refresh(actor_obj)
        return actor_obj
    elif create_if_not_found:
        # Create the actor from remote data
        from app.activitypub.actor import create_actor_from_remote
        return create_actor_from_remote(actor_url, community_only, feed_only)
    else:
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


def find_flair_or_create(flair: dict, community_id: int) -> CommunityFlair:
    existing_flair = CommunityFlair.query.filter(CommunityFlair.flair == flair['display_name'].strip(),
                                                 CommunityFlair.community_id == community_id).first()
    if existing_flair:
        return existing_flair
    else:
        new_flair = CommunityFlair(flair=flair['display_name'].strip(), community_id=community_id,
                                   text_color=flair['text_color'], background_color=flair['background_color'],
                                   blur_images=flair['blur_images'] if 'blur_images' in flair else False)
        return new_flair


def extract_domain_and_actor(url_string: str):
    # Parse the URL
    if url_string.endswith('/'):  # WordPress
        url_string = url_string[:-1]
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
    try:
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
                try:
                    activity_json = actor_data.json()
                    actor_data.close()
                except JSONDecodeError:
                    user.instance.failures += 1
                    session.commit()
                    return

                # update indexible state on their posts, if necessary
                new_indexable = activity_json['indexable'] if 'indexable' in activity_json else True
                if new_indexable != user.indexable:
                    session.execute(text('UPDATE "post" set indexable = :indexable WHERE user_id = :user_id'),
                                    {'user_id': user.id,
                                     'indexable': new_indexable})

                # fix ap_id for WordPress actors
                if user.ap_id.startswith('@'):
                    server, address = extract_domain_and_actor(user.ap_profile_id)
                    user.ap_id = f"{address.lower()}@{server.lower()}"

                user.user_name = activity_json['preferredUsername'].strip()
                if 'name' in activity_json:
                    user.title = activity_json['name'].strip() if activity_json['name'] else ''
                if 'summary' in activity_json:
                    about_html = activity_json['summary']
                    if about_html is not None and not about_html.startswith('<'):  # PeerTube
                        about_html = '<p>' + about_html + '</p>'
                    user.about_html = allowlist_html(about_html)
                else:
                    user.about_html = ''
                if 'source' in activity_json and activity_json['source'].get('mediaType') == 'text/markdown':
                    user.about = activity_json['source']['content']
                    user.about_html = markdown_to_html(user.about)  # prefer Markdown if provided, overwrite version obtained from HTML
                else:
                    user.about = html_to_text(user.about_html)
                if 'attachment' in activity_json and isinstance(activity_json['attachment'], list):
                    user.extra_fields = []
                    for field_data in activity_json['attachment']:
                        if field_data['type'] == 'PropertyValue':
                            if '<a ' in field_data['value']:
                                field_data['value'] = mastodon_extra_field_link(field_data['value'])
                            user.extra_fields.append(UserExtraField(label=field_data['name'].strip(), text=field_data['value'].strip()))
                if 'type' in activity_json:
                    user.bot = True if activity_json['type'] == 'Service' else False
                user.ap_fetched_at = utcnow()
                user.public_key = activity_json['publicKey']['publicKeyPem']
                user.accept_private_messages = activity_json['acceptPrivateMessages'] if 'acceptPrivateMessages' in activity_json else 3
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
                if user.cover_id and cover_changed:
                    make_image_sizes(user.cover_id, 700, 1600, 'users')
                    cache.delete_memoized(User.cover_image, user)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def refresh_community_profile(community_id, activity_json=None):
    if current_app.debug:
        refresh_community_profile_task(community_id, activity_json)
    else:
        refresh_community_profile_task.apply_async(args=(community_id, activity_json), countdown=randint(1, 10))


@celery.task
def refresh_community_profile_task(community_id, activity_json):
    session = get_task_session()
    try:
        community: Community = session.query(Community).get(community_id)
        if community and community.instance.online() and not community.is_local():
            if not activity_json:
                try:
                    actor_data = get_request(community.ap_public_url, headers={'Accept': 'application/activity+json'})
                except httpx.HTTPError:
                    time.sleep(randint(3, 10))
                    try:
                        actor_data = get_request(community.ap_public_url, headers={'Accept': 'application/activity+json'})
                    except Exception:
                        return
                if actor_data.status_code == 200:
                    activity_json = actor_data.json()
                    actor_data.close()

            if activity_json:
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
                community.posting_warning = activity_json['postingWarning'] if 'postingWarning' in activity_json else None
                community.restricted_to_mods = activity_json['postingRestrictedToMods'] if 'postingRestrictedToMods' in activity_json else False
                community.new_mods_wanted = activity_json['newModsWanted'] if 'newModsWanted' in activity_json else False
                community.private_mods = activity_json['privateMods'] if 'privateMods' in activity_json else False
                community.ap_moderators_url = mods_url
                if 'followers' in activity_json:
                    community.ap_followers_url = activity_json['followers']
                community.ap_fetched_at = utcnow()
                community.public_key = activity_json['publicKey']['publicKeyPem']

                if 'summary' in activity_json:
                    description_html = activity_json['summary']
                elif 'content' in activity_json:
                    description_html = activity_json['content']
                else:
                    description_html = ''

                if description_html is not None and description_html != '':
                    if not description_html.startswith('<'):  # PeerTube
                        description_html = '<p>' + description_html + '</p>'
                    community.description_html = allowlist_html(description_html)
                    if 'source' in activity_json and activity_json['source'].get('mediaType') == 'text/markdown':
                        community.description = activity_json['source']['content']
                        community.description_html = markdown_to_html(community.description)          # prefer Markdown if provided, overwrite version obtained from HTML
                    else:
                        community.description = html_to_text(community.description_html)

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

                if 'lemmy:tagsForPosts' in activity_json and isinstance(activity_json['lemmy:tagsForPosts'], list):
                    if len(community.flair) == 0:  # for now, all we do is populate community flair if there is not yet any. simpler.
                        for flair in activity_json['lemmy:tagsForPosts']:
                            flair_dict = {'display_name': flair['display_name']}
                            if 'text_color' in flair:
                                flair_dict['text_color'] = flair['text_color']
                            if 'background_color' in flair:
                                flair_dict['background_color'] = flair['background_color']
                            if 'blur_images' in flair:
                                flair_dict['blur_images'] = flair['blur_images']
                            community.flair.append(find_flair_or_create(flair_dict, community.id))
                        session.commit()

                if community.icon_id and icon_changed:
                    make_image_sizes(community.icon_id, 60, 250, 'communities')
                if community.image_id and cover_changed:
                    make_image_sizes(community.image_id, 700, 1600, 'communities')

                if community.ap_moderators_url:
                    mods_request = get_request(community.ap_moderators_url,
                                               headers={'Accept': 'application/activity+json'})
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

                if community.ap_followers_url:
                    followers_request = get_request(community.ap_followers_url, headers={'Accept': 'application/activity+json'})
                    if followers_request.status_code == 200:
                        followers_data = followers_request.json()
                        followers_request.close()
                        if followers_data and followers_data['type'] == 'Collection' and 'totalItems' in followers_data:
                            community.total_subscriptions_count = followers_data['totalItems']
                            session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def refresh_feed_profile(feed_id):
    if current_app.debug:
        refresh_feed_profile_task(feed_id)
    else:
        refresh_feed_profile_task.apply_async(args=(feed_id,), countdown=randint(1, 10))


@celery.task
def refresh_feed_profile_task(feed_id):
    session = get_task_session()
    try:
        feed: Feed = session.query(Feed).get(feed_id)
        if feed and feed.instance.online() and not feed.is_local():
            try:
                actor_data = get_request(feed.ap_public_url, headers={'Accept': 'application/activity+json'})
            except httpx.HTTPError:
                time.sleep(randint(3, 10))
                try:
                    actor_data = get_request(feed.ap_public_url, headers={'Accept': 'application/activity+json'})
                except Exception:
                    return
            if actor_data.status_code == 200:
                activity_json = actor_data.json()
                actor_data.close()

                if 'attributedTo' in activity_json and isinstance(activity_json['attributedTo'], str):  # lemmy, mbin, and our feeds
                    owners_url = activity_json['attributedTo']
                elif 'moderators' in activity_json:  # kbin, and our feeds
                    owners_url = activity_json['moderators']
                else:
                    owners_url = None

                feed.nsfw = activity_json['sensitive'] if 'sensitive' in activity_json else False
                if 'nsfl' in activity_json and activity_json['nsfl']:
                    feed.nsfl = activity_json['nsfl']
                feed.title = activity_json['name'].strip()
                feed.ap_moderators_url = owners_url
                feed.ap_fetched_at = utcnow()
                feed.public_key = activity_json['publicKey']['publicKeyPem']

                description_html = ''
                if 'summary' in activity_json:
                    description_html = activity_json['summary']
                elif 'content' in activity_json:
                    description_html = activity_json['content']
                else:
                    description_html = ''

                if description_html is not None and description_html != '':
                    if not description_html.startswith('<'):  # PeerTube
                        description_html = '<p>' + description_html + '</p>'
                    feed.description_html = allowlist_html(description_html)
                    if 'source' in activity_json and activity_json['source'].get('mediaType') == 'text/markdown':
                        feed.description = activity_json['source']['content']
                        feed.description_html = markdown_to_html(feed.description)          # prefer Markdown if provided, overwrite version obtained from HTML
                    else:
                        feed.description = html_to_text(feed.description_html)

                icon_changed = cover_changed = False
                if 'icon' in activity_json:
                    if isinstance(activity_json['icon'], dict) and 'url' in activity_json['icon']:
                        icon_entry = activity_json['icon']['url']
                    elif isinstance(activity_json['icon'], list) and 'url' in activity_json['icon'][-1]:
                        icon_entry = activity_json['icon'][-1]['url']
                    else:
                        icon_entry = None
                    if icon_entry:
                        if feed.icon_id and icon_entry != feed.icon.source_url:
                            feed.icon.delete_from_disk()
                        if not feed.icon_id or (feed.icon_id and icon_entry != feed.icon.source_url):
                            icon = File(source_url=icon_entry)
                            feed.icon = icon
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
                        if feed.image_id and image_entry != feed.image.source_url:
                            feed.image.delete_from_disk()
                        if not feed.image_id or (feed.image_id and image_entry != feed.image.source_url):
                            image = File(source_url=image_entry)
                            feed.image = image
                            session.add(image)
                            cover_changed = True
                session.commit()

                if feed.icon_id and icon_changed:
                    make_image_sizes(feed.icon_id, 60, 250, 'feeds')
                if feed.image_id and cover_changed:
                    make_image_sizes(feed.image_id, 700, 1600, 'feeds')

                if feed.ap_moderators_url:
                    owners_request = get_request(feed.ap_moderators_url,
                                                 headers={'Accept': 'application/activity+json'})
                    if owners_request.status_code == 200:
                        owners_data = owners_request.json()
                        owners_request.close()
                        if owners_data and owners_data['type'] == 'OrderedCollection' and 'orderedItems' in owners_data:
                            for actor in owners_data['orderedItems']:
                                time.sleep(0.5)
                                user = find_actor_or_create(actor)
                                if user:
                                    existing_membership = FeedMember.query.filter_by(feed_id=feed.id,
                                                                                     user_id=user.id).first()
                                    if existing_membership:
                                        existing_membership.is_owner = True
                                        db.session.commit()
                                    else:
                                        new_membership = FeedMember(feed_id=feed.id, user_id=user.id,
                                                                    is_owner=True)
                                        db.session.add(new_membership)
                                        db.session.commit()

                            # Remove people who are no longer mods
                            # this should not get triggered as feeds just have the one owner
                            # right now, but that may change later so this is here for 
                            # future proofing
                            for member in FeedMember.query.filter_by(feed_id=feed.id, is_owner=True).all():
                                member_user = User.query.get(member.user_id)
                                is_owner = False
                                for actor in owners_data['orderedItems']:
                                    if actor.lower() == member_user.profile_id().lower():
                                        is_owner = True
                                        break
                                if not is_owner:
                                    db.session.query(FeedMember).filter_by(feed_id=feed.id,
                                                                           user_id=member_user.id,
                                                                           is_owner=True).delete()
                                    db.session.commit()

                # also make sure we have all the feeditems from the /following collection
                res = get_request(feed.ap_following_url)
                following_collection = res.json()

                # for each of those get the communities and make feeditems
                for fci in following_collection['items']:
                    community_ap_id = fci
                    community = find_actor_or_create(community_ap_id, community_only=True)
                    if community and isinstance(community, Community):
                        feed_item = FeedItem(feed_id=feed.id, community_id=community.id)
                        db.session.add(feed_item)
                        db.session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def actor_json_to_model(activity_json, address, server):
    if 'type' not in activity_json:  # some Akkoma instances return an empty actor?! e.g. https://donotsta.re/users/april
        return None
    if activity_json['type'] == 'Person' or activity_json['type'] == 'Service':
        user = User.query.filter(User.ap_profile_id == activity_json['id'].lower()).first()
        if user:
            return user
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
                        instance_id=find_instance_id(server),
                        accept_private_messages=activity_json['acceptPrivateMessages'] if 'acceptPrivateMessages' in activity_json else 3
                        # language=community_json['language'][0]['identifier'] # todo: language
                        )
        except KeyError:
            current_app.logger.error(f'KeyError for {address}@{server} while parsing ' + str(activity_json))
            return None

        if 'summary' in activity_json:
            about_html = activity_json['summary']
            if about_html is not None and not about_html.startswith('<'):  # PeerTube
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
        if 'attachment' in activity_json and isinstance(activity_json['attachment'], list):
            user.extra_fields = []
            for field_data in activity_json['attachment']:
                if field_data['type'] == 'PropertyValue':
                    if '<a ' in field_data['value']:
                        field_data['value'] = mastodon_extra_field_link(field_data['value'])
                    user.extra_fields.append(UserExtraField(label=shorten_string(field_data['name'].strip()),
                                                            text=field_data['value'].strip()))
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
        community = Community.query.filter(Community.ap_profile_id == activity_json['id'].lower()).first()
        if community:
            return community
        if 'attributedTo' in activity_json and isinstance(activity_json['attributedTo'], str):  # lemmy and mbin
            mods_url = activity_json['attributedTo']
        elif 'moderators' in activity_json:  # kbin
            mods_url = activity_json['moderators']
        else:
            mods_url = None

        # only allow nsfw communities if enabled for this instance
        site = Site.query.get(1)  # can't use g.site because actor_json_to_model can be called from celery
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
                              posting_warning=activity_json['postingWarning'] if 'postingWarning' in activity_json else None,
                              ap_id=f"{address[1:].lower()}@{server.lower()}" if address.startswith('!') else f"{address.lower()}@{server.lower()}",
                              ap_public_url=activity_json['id'],
                              ap_profile_id=activity_json['id'].lower(),
                              ap_followers_url=activity_json['followers'] if 'followers' in activity_json else None,
                              ap_inbox_url=activity_json['endpoints']['sharedInbox'] if 'endpoints' in activity_json else activity_json['inbox'],
                              ap_outbox_url=activity_json['outbox'],
                              ap_featured_url=activity_json['featured'] if 'featured' in activity_json else '',
                              ap_moderators_url=mods_url,
                              ap_fetched_at=utcnow(),
                              ap_domain=server.lower(),
                              public_key=activity_json['publicKey']['publicKeyPem'],
                              # language=community_json['language'][0]['identifier'] # todo: language
                              instance_id=find_instance_id(server)
                              )
        if get_setting('meme_comms_low_quality', False):
            community.low_quality = 'memes' in activity_json['preferredUsername'] or 'shitpost' in activity_json['preferredUsername']
        description_html = ''
        if 'summary' in activity_json:
            description_html = activity_json['summary']
        elif 'content' in activity_json:
            description_html = activity_json['content']
        else:
            description_html = ''

        if description_html is not None and description_html != '':
            if not description_html.startswith('<'):  # PeerTube
                description_html = '<p>' + description_html + '</p>'
            community.description_html = allowlist_html(description_html)
            if 'source' in activity_json and activity_json['source'].get('mediaType') == 'text/markdown':
                community.description = activity_json['source']['content']
                community.description_html = markdown_to_html(community.description)          # prefer Markdown if provided, overwrite version obtained from HTML
            else:
                community.description = html_to_text(community.description_html)

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
        if 'lemmy:tagsForPosts' in activity_json and isinstance(activity_json['lemmy:tagsForPosts'], list):
            for flair in activity_json['lemmy:tagsForPosts']:
                flair_dict = {'display_name': flair['display_name']}
                if 'text_color' in flair:
                    flair_dict['text_color'] = flair['text_color']
                if 'background_color' in flair:
                    flair_dict['background_color'] = flair['background_color']
                community.flair.append(find_flair_or_create(flair_dict, community.id))
            db.session.commit()
        if community.icon_id:
            make_image_sizes(community.icon_id, 60, 250, 'communities')
        if community.image_id:
            make_image_sizes(community.image_id, 700, 1600, 'communities')
        return community
    elif activity_json['type'] == 'Feed':
        feed = Feed.query.filter(Feed.ap_profile_id == activity_json['id'].lower()).first()
        if feed:
            return feed
        if 'attributedTo' in activity_json and isinstance(activity_json['attributedTo'], str):  # lemmy, mbin, and our feeds
            owners_url = activity_json['attributedTo']
        elif 'moderators' in activity_json:  # kbin, and our feeds
            owners_url = activity_json['moderators']
        else:
            owners_url = None

        # only allow nsfw communities if enabled for this instance
        site = Site.query.get(1)  # can't use g.site because actor_json_to_model can be called from celery
        if 'sensitive' in activity_json and activity_json['sensitive'] and not site.enable_nsfw:
            return None
        if 'nsfl' in activity_json and activity_json['nsfl'] and not site.enable_nsfl:
            return None

        # get the owners list
        # these users will be added to feedmember db entries at the bottom of this function
        owner_users = []
        owners_data = get_request(owners_url, headers={'Accept': 'application/activity+json'})
        if owners_data.status_code == 200:
            owners_json = owners_data.json()
            for owner in owners_json['orderedItems']:
                owner_user = find_actor_or_create(owner)
                owner_users.append(owner_user)

        # also get the communities in the remote feed's /following list 
        feed_following = []
        following_data = get_request(activity_json['following'], headers={'Accept': 'application/activity+json'})
        if following_data.status_code == 200:
            following_json = following_data.json()
            for c_ap_id in following_json['items']:
                community = find_actor_or_create(c_ap_id, community_only=True)
                feed_following.append(community)

        feed = Feed(name=activity_json['preferredUsername'].strip(),
                    user_id=owner_users[0].id,
                    title=activity_json['name'].strip(),
                    nsfw=activity_json['sensitive'] if 'sensitive' in activity_json else False,
                    machine_name=activity_json['preferredUsername'],
                    description_html=activity_json['summary'] if 'summary' in activity_json else '',
                    description=piefed_markdown_to_lemmy_markdown(activity_json['source']['content']) if 'source' in activity_json else '',
                    created_at=activity_json['published'] if 'published' in activity_json else utcnow(),
                    last_edit=activity_json['updated'] if 'updated' in activity_json else utcnow(),
                    num_communities=0,
                    ap_id=f"{address[1:].lower()}@{server.lower()}" if address.startswith('~') else f"{address.lower()}@{server.lower()}",
                    ap_public_url=activity_json['id'],
                    ap_profile_id=activity_json['id'].lower(),
                    ap_followers_url=activity_json['followers'] if 'followers' in activity_json else None,
                    ap_following_url=activity_json['following'] if 'following' in activity_json else None,
                    ap_inbox_url=activity_json['endpoints']['sharedInbox'] if 'endpoints' in activity_json else activity_json['inbox'],
                    ap_outbox_url=activity_json['outbox'],
                    ap_moderators_url=owners_url,
                    ap_fetched_at=utcnow(),
                    ap_domain=server.lower(),
                    public_key=activity_json['publicKey']['publicKeyPem'],
                    instance_id=find_instance_id(server),
                    public=True
                    )

        description_html = ''
        if 'summary' in activity_json:
            description_html = activity_json['summary']
        elif 'content' in activity_json:
            description_html = activity_json['content']
        else:
            description_html = ''

        if description_html is not None and description_html != '':
            if not description_html.startswith('<'):  # PeerTube
                description_html = '<p>' + description_html + '</p>'
            feed.description_html = allowlist_html(description_html)
            if 'source' in activity_json and activity_json['source'].get('mediaType') == 'text/markdown':
                feed.description = activity_json['source']['content']
                feed.description_html = markdown_to_html(feed.description)  # prefer Markdown if provided, overwrite version obtained from HTML
            else:
                feed.description = html_to_text(feed.description_html)

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
                feed.icon = icon
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
                feed.image = image
                db.session.add(image)

        try:
            db.session.add(feed)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return Feed.query.filter_by(ap_profile_id=activity_json['id'].lower()).one()

        # add the owners as feedmembers
        for ou in owner_users:
            fm = FeedMember(feed_id=feed.id, user_id=ou.id, is_owner=True)
            db.session.add(fm)
            db.session.commit()

        # add the communities from the remote /following list as feeditems
        for c in feed_following:
            fi = FeedItem(feed_id=feed.id, community_id=c.id)
            feed.num_communities += 1
            db.session.add(fi)
            db.session.commit()

        if feed.icon_id:
            make_image_sizes(feed.icon_id, 60, 250, 'feeds')
        if feed.image_id:
            make_image_sizes(feed.image_id, 700, 1600, 'feeds')

        if 'childFeeds' in activity_json:
            for child_feed in activity_json['childFeeds']:
                populate_child_feed(feed.id, child_feed)
        return feed


# Save two different versions of a File, after downloading it from file.source_url. Set a width parameter to None to avoid generating one of that size
def make_image_sizes(file_id, thumbnail_width=50, medium_width=120, directory='posts', toxic_community=False):
    if current_app.debug:
        make_image_sizes_async(file_id, thumbnail_width, medium_width, directory, toxic_community)
    else:
        make_image_sizes_async.apply_async(args=(file_id, thumbnail_width, medium_width, directory, toxic_community),
                                           countdown=randint(1, 10))  # Delay by up to 10 seconds so servers do not experience a stampede of requests all in the same second


@celery.task
def make_image_sizes_async(file_id, thumbnail_width, medium_width, directory, toxic_community):
    with current_app.app_context():
        original_directory = directory
        session = get_task_session()
        try:
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
                                    file_ext = file_ext.strip()  # just to be sure

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
                                if store_files_in_s3():
                                    directory = 'app/static/tmp'
                                else:
                                    directory = f'app/static/media/{directory}/' + new_filename[0:2] + '/' + new_filename[2:4]
                                ensure_directory_exists(directory)

                                # file path and names to store the resized images on disk
                                final_place = os.path.join(directory, new_filename + file_ext)
                                final_place_thumbnail = os.path.join(directory, new_filename + '_thumbnail.webp')

                                if file_ext == '.avif':  # this is quite a big package so we'll only load it if necessary
                                    import pillow_avif  # NOQA

                                # Load image data into Pillow
                                Image.MAX_IMAGE_PIXELS = 89478485
                                image = Image.open(BytesIO(source_image))
                                image = ImageOps.exif_transpose(image)
                                img_width = image.width

                                boto3_session = None
                                s3 = None

                                # Use environment variables to determine medium and thumbnail format and quality

                                medium_image_format = current_app.config['MEDIA_IMAGE_MEDIUM_FORMAT']
                                medium_image_quality = current_app.config['MEDIA_IMAGE_MEDIUM_QUALITY']
                                thumbnail_image_format = current_app.config['MEDIA_IMAGE_THUMBNAIL_FORMAT']
                                thumbnail_image_quality = current_app.config['MEDIA_IMAGE_THUMBNAIL_QUALITY']

                                final_ext = file_ext  # track file extension for conversion
                                thumbnail_ext = file_ext

                                if medium_image_format == 'AVIF' or thumbnail_image_format == 'AVIF':
                                    import pillow_avif  # NOQA

                                # Resize the image to medium
                                if medium_width:
                                    if img_width > medium_width or medium_image_format:
                                        image = image.convert('RGB' if (
                                                    medium_image_format == 'JPEG' or final_ext in ['.jpg',
                                                                                                   '.jpeg']) else 'RGBA')
                                        image.thumbnail((medium_width, sys.maxsize), resample=Image.LANCZOS)

                                    kwargs = {}
                                    if medium_image_format:
                                        kwargs['format'] = medium_image_format.upper()
                                        final_ext = '.' + medium_image_format.lower()
                                        final_place = os.path.splitext(final_place)[0] + final_ext
                                    if medium_image_quality:
                                        kwargs['quality'] = int(medium_image_quality)

                                    image.save(final_place, optimize=True, **kwargs)

                                    if store_files_in_s3():
                                        content_type = guess_mime_type(final_place)
                                        boto3_session = boto3.session.Session()
                                        s3 = boto3_session.client(
                                            service_name='s3',
                                            region_name=current_app.config['S3_REGION'],
                                            endpoint_url=current_app.config['S3_ENDPOINT'],
                                            aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                                            aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
                                        )
                                        s3.upload_file(final_place, current_app.config['S3_BUCKET'],
                                                       original_directory + '/' +
                                                       new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext,
                                                       ExtraArgs={'ContentType': content_type})
                                        os.unlink(final_place)
                                        final_place = f"https://{current_app.config['S3_PUBLIC_URL']}/{original_directory}/{new_filename[0:2]}/{new_filename[2:4]}" + \
                                                      '/' + new_filename + final_ext

                                    file.file_path = final_place
                                    file.width = image.width
                                    file.height = image.height

                                # Resize the image to a thumbnail (webp)
                                if thumbnail_width:
                                    if img_width > thumbnail_width:
                                        image = image.convert('RGB' if thumbnail_image_format == 'JPEG' else 'RGBA')
                                        image.thumbnail((thumbnail_width, thumbnail_width), resample=Image.LANCZOS)

                                    kwargs = {}
                                    if thumbnail_image_format:
                                        kwargs['format'] = thumbnail_image_format.upper()
                                        thumbnail_ext = '.' + thumbnail_image_format.lower()
                                        final_place_thumbnail = os.path.splitext(final_place_thumbnail)[0] + thumbnail_ext
                                    if thumbnail_image_quality:
                                        kwargs['quality'] = int(thumbnail_image_quality)

                                    image.save(final_place_thumbnail, optimize=True, **kwargs)

                                    if store_files_in_s3():
                                        content_type = guess_mime_type(final_place_thumbnail)
                                        if boto3_session is None and s3 is None:
                                            boto3_session = boto3.session.Session()
                                            s3 = boto3_session.client(
                                                service_name='s3',
                                                region_name=current_app.config['S3_REGION'],
                                                endpoint_url=current_app.config['S3_ENDPOINT'],
                                                aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                                                aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
                                            )
                                        s3.upload_file(final_place_thumbnail, current_app.config['S3_BUCKET'],
                                                       original_directory + '/' +
                                                       new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + '_thumbnail' + thumbnail_ext,
                                                       ExtraArgs={'ContentType': content_type})
                                        os.unlink(final_place_thumbnail)
                                        final_place_thumbnail = f"https://{current_app.config['S3_PUBLIC_URL']}/{original_directory}/{new_filename[0:2]}/{new_filename[2:4]}" + \
                                                                '/' + new_filename + '_thumbnail' + thumbnail_ext
                                    file.thumbnail_path = final_place_thumbnail
                                    file.thumbnail_width = image.width
                                    file.thumbnail_height = image.height

                                if s3:
                                    s3.close()
                                session.commit()

                                site = Site.query.get(1)
                                if site is None:
                                    site = Site()

                                # Alert regarding fascist meme content
                                if site.enable_chan_image_filter and toxic_community and img_width < 2000:  # images > 2000px tend to be real photos instead of 4chan screenshots.
                                    if os.environ.get('ALLOW_4CHAN', None) is None:
                                        try:
                                            image_text = pytesseract.image_to_string(
                                                Image.open(BytesIO(source_image)).convert('L'), timeout=30)
                                        except Exception:
                                            image_text = ''
                                        if 'Anonymous' in image_text and (
                                                'No.' in image_text or ' N0' in image_text):  # chan posts usually contain the text 'Anonymous' and ' No.12345'
                                            post = Post.query.filter_by(image_id=file.id).first()
                                            targets_data = {'gen': '0',
                                                            'post_id': post.id,
                                                            'orig_post_title': post.title,
                                                            'orig_post_body': post.body
                                                            }
                                            notification = Notification(title='Review this',
                                                                        user_id=1,
                                                                        author_id=post.user_id,
                                                                        url=url_for('activitypub.post_ap',
                                                                                    post_id=post.id),
                                                                        notif_type=NOTIF_REPORT,
                                                                        subtype='post_with_suspicious_image',
                                                                        targets=targets_data)
                                            session.add(notification)
                                            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def find_reply_parent(in_reply_to: str) -> Tuple[int, int, int]:
    parent_comment = post = None
    post_id = parent_comment_id = root_id = None

    # 'comment' is hint that in_reply_to was another comment
    if 'comment' in in_reply_to:
        parent_comment = PostReply.get_by_ap_id(in_reply_to)
        if parent_comment:
            parent_comment_id = parent_comment.id
            post_id = parent_comment.post_id
            root_id = parent_comment.root_id

    # 'post' is hint that in_reply_to was a post
    if not parent_comment and 'post' in in_reply_to:
        post = Post.get_by_ap_id(in_reply_to)
        if post:
            post_id = post.id

    # no hint in in_reply_to, or it was misleading (e.g. replies to nodebb comments have '/post/' in them)
    if not parent_comment and not post:
        parent_comment = PostReply.get_by_ap_id(in_reply_to)
        if parent_comment:
            parent_comment_id = parent_comment.id
            post_id = parent_comment.post_id
            root_id = parent_comment.root_id
        else:
            post = Post.get_by_ap_id(in_reply_to)
            if post:
                post_id = post.id

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
        new_instance = Instance(domain=server, software='unknown', inbox=f'https://{server}/inbox', created_at=utcnow())

        try:
            db.session.add(new_instance)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
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
    try:
        instance: Instance = session.query(Instance).get(instance_id)
        try:
            instance_data = get_request(f"https://{instance.domain}", headers={'Accept': 'application/activity+json'})
        except:
            return
        if instance_data.status_code == 200:
            try:
                instance_json = instance_data.json()
                instance_data.close()
            except Exception:
                instance_json = {}
            if 'type' in instance_json and instance_json['type'] == 'Application':
                instance.inbox = instance_json['inbox'] if 'inbox' in instance_json else f"https://{instance.domain}/inbox"
                instance.outbox = instance_json['outbox']
            else:  # it's pretty much always /inbox so just assume that it is for whatever this instance is running
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

        headers = {'Accept': 'application/activity+json'}
        try:
            nodeinfo = get_request(f"https://{instance.domain}/.well-known/nodeinfo", headers=headers)
            if nodeinfo.status_code == 200:
                nodeinfo_json = nodeinfo.json()
                for links in nodeinfo_json['links']:
                    if isinstance(links, dict) and 'rel' in links and (
                            links['rel'] == 'http://nodeinfo.diaspora.software/ns/schema/2.0' or  # most platforms except KBIN and Lemmy v0.19.4
                            links['rel'] == 'https://nodeinfo.diaspora.software/ns/schema/2.0' or  # KBIN
                            links['rel'] == 'http://nodeinfo.diaspora.software/ns/schema/2.1'):  # Lemmy v0.19.4+ (no 2.0 back-compat provided here)
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
    except Exception:
        session.rollback()
        raise
    finally:
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


def delete_post_or_comment(deletor, to_delete, store_ap_json, request_json, reason):
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    community = to_delete.community
    if (to_delete.user_id == deletor.id or
            (deletor.instance_id == to_delete.author.instance_id and deletor.is_instance_admin()) or
            community.is_moderator(deletor) or
            community.is_instance_admin(deletor)):
        if isinstance(to_delete, Post):
            to_delete.deleted = True
            to_delete.deleted_by = deletor.id
            community.post_count -= 1
            to_delete.author.post_count -= 1
            if to_delete.url and to_delete.cross_posts is not None:
                to_delete.calculate_cross_posts(delete_only=True)
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
            if to_delete.path:
                db.session.execute(text('update post_reply set child_count = child_count - 1 where id in :parents'),
                                   {'parents': tuple(to_delete.path[:-1])})
            db.session.commit()
            if to_delete.author.id != deletor.id:
                add_to_modlog_activitypub('delete_post_reply', deletor, community_id=community.id,
                                          link_text=f'comment on {shorten_string(to_delete.post.title)}',
                                          link=f'post/{to_delete.post.id}#comment_{to_delete.id}',
                                          reason=reason)
        log_incoming_ap(id, APLOG_DELETE, APLOG_SUCCESS, saved_json)
    else:
        log_incoming_ap(id, APLOG_DELETE, APLOG_FAILURE, saved_json, 'Deletor did not have permisson')


def restore_post_or_comment(restorer, to_restore, store_ap_json, request_json, reason):
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    community = to_restore.community
    if (to_restore.user_id == restorer.id or
            (restorer.instance_id == to_restore.author.instance_id and restorer.is_instance_admin()) or
            community.is_moderator(restorer) or
            community.is_instance_admin(restorer)):
        if isinstance(to_restore, Post):
            to_restore.deleted = False
            to_restore.deleted_by = None
            community.post_count += 1
            to_restore.author.post_count += 1
            if to_restore.url:
                to_restore.calculate_cross_posts()
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
            if to_restore.path:
                db.session.execute(text('update post_reply set child_count = child_count + 1 where id in :parents'),
                                   {'parents': tuple(to_restore.path[:-1])})
            db.session.commit()
            if to_restore.author.id != restorer.id:
                add_to_modlog_activitypub('restore_post_reply', restorer, community_id=community.id,
                                          link_text=f'comment on {shorten_string(to_restore.post.title)}',
                                          link=f'post/{to_restore.post_id}#comment_{to_restore.id}',
                                          reason=reason)
        log_incoming_ap(id, APLOG_UNDO_DELETE, APLOG_SUCCESS, saved_json)
    else:
        log_incoming_ap(id, APLOG_UNDO_DELETE, APLOG_FAILURE, saved_json, 'Restorer did not have permisson')


def site_ban_remove_data(blocker_id, blocked):
    replies = PostReply.query.filter_by(user_id=blocked.id, deleted=False)
    for reply in replies:
        reply.deleted = True
        reply.deleted_by = blocker_id
        if not blocked.bot:
            reply.post.reply_count -= 1
        reply.community.post_reply_count -= 1
        if reply.path:
            db.session.execute(text('update post_reply set child_count = child_count - 1 where id in :parents'),
                               {'parents': tuple(reply.path[:-1])})
    blocked.reply_count = 0
    db.session.commit()

    posts = Post.query.filter_by(user_id=blocked.id, deleted=False)
    for post in posts:
        post.deleted = True
        post.deleted_by = blocker_id
        post.community.post_count -= 1
        if post.url and post.cross_posts is not None:
            post.calculate_cross_posts(delete_only=True)
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


def community_ban_remove_data(blocker_id, community_id, blocked):
    replies = PostReply.query.filter_by(user_id=blocked.id, deleted=False, community_id=community_id)
    for reply in replies:
        reply.deleted = True
        reply.deleted_by = blocker_id
        if not blocked.bot:
            reply.post.reply_count -= 1
        reply.community.post_reply_count -= 1
        blocked.post_reply_count -= 1
        if reply.path:
            db.session.execute(text('update post_reply set child_count = child_count - 1 where id in :parents'),
                               {'parents': tuple(reply.path[:-1])})
    db.session.commit()

    posts = Post.query.filter_by(user_id=blocked.id, deleted=False, community_id=community_id)
    for post in posts:
        post.deleted = True
        post.deleted_by = blocker_id
        post.community.post_count -= 1
        if post.url and post.cross_posts is not None:
            post.calculate_cross_posts(delete_only=True)
        blocked.post_count -= 1
    db.session.commit()

    # Delete attached images to save moderators from having to see disgusting stuff.
    files = File.query.join(Post).filter(Post.user_id == blocked.id, Post.community_id == community_id).all()
    for file in files:
        file.delete_from_disk()
        file.source_url = ''
    db.session.commit()


def ban_user(blocker, blocked, community, core_activity):
    existing = CommunityBan.query.filter_by(community_id=community.id, user_id=blocked.id).first()
    if not existing:
        new_ban = CommunityBan(community_id=community.id, user_id=blocked.id, banned_by=blocker.id)
        if 'summary' in core_activity:
            reason = core_activity['summary']
        else:
            reason = ''
        new_ban.reason = shorten_string(reason, 255)

        ban_until = None
        if 'expires' in core_activity:
            try:
                ban_until = datetime.fromisoformat(core_activity['expires'])
            except ValueError:
                ban_until = arrow.get(core_activity['expires']).datetime
        elif 'endTime' in core_activity:
            try:
                ban_until = datetime.fromisoformat(core_activity['endTime'])
            except ValueError:
                ban_until = arrow.get(core_activity['endTime']).datetime

        if ban_until and ban_until > datetime.now(timezone.utc):
            new_ban.ban_until = ban_until

        db.session.add(new_ban)

        community_membership_record = CommunityMember.query.filter_by(community_id=community.id,
                                                                      user_id=blocked.id).first()
        if community_membership_record:
            community_membership_record.is_banned = True
        db.session.commit()

        if blocked.is_local():
            db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.community_id == community.id,
                                                          CommunityJoinRequest.user_id == blocked.id).delete()

            # Notify banned person
            targets_data = {'gen': '0', 'community_id': community.id}
            notify = Notification(title=shorten_string('You have been banned from ' + community.title),
                                  url=f'/chat/ban_from_mod/{blocked.id}/{community.id}', user_id=blocked.id,
                                  author_id=blocker.id, notif_type=NOTIF_BAN, subtype='user_banned_from_community',
                                  targets=targets_data)
            db.session.add(notify)
            if not current_app.debug:  # user.unread_notifications += 1 hangs app if 'user' is the same person
                blocked.unread_notifications += 1  # who pressed 'Re-submit this activity'.

            # Remove their notification subscription,  if any
            db.session.query(NotificationSubscription).filter(NotificationSubscription.entity_id == community.id,
                                                              NotificationSubscription.user_id == blocked.id,
                                                              NotificationSubscription.type == NOTIF_COMMUNITY).delete()
            db.session.commit()

            cache.delete_memoized(communities_banned_from, blocked.id)
            cache.delete_memoized(joined_communities, blocked.id)
            cache.delete_memoized(moderating_communities, blocked.id)

        add_to_modlog_activitypub('ban_user', blocker, community_id=community.id, link_text=blocked.display_name(),
                                  link=f'u/{blocked.link()}', reason=reason)


def unban_user(blocker, blocked, community, core_activity):
    if 'object' in core_activity and 'summary' in core_activity['object']:
        reason = core_activity['object']['summary']
    else:
        reason = ''
    db.session.query(CommunityBan).filter(CommunityBan.community_id == community.id,
                                          CommunityBan.user_id == blocked.id).delete()
    community_membership_record = CommunityMember.query.filter_by(community_id=community.id, user_id=blocked.id).first()
    if community_membership_record:
        community_membership_record.is_banned = False
    db.session.commit()

    if blocked.is_local():
        # Notify unbanned person
        targets_data = {'gen': '0', 'community_id': community.id}
        notify = Notification(title=shorten_string('You have been unbanned from ' + community.display_name()),
                              url=f'/chat/ban_from_mod/{blocked.id}/{community.id}', user_id=blocked.id,
                              author_id=blocker.id, notif_type=NOTIF_UNBAN,
                              subtype='user_unbanned_from_community',
                              targets=targets_data)
        db.session.add(notify)
        if not current_app.debug:  # user.unread_notifications += 1 hangs app if 'user' is the same person
            blocked.unread_notifications += 1  # who pressed 'Re-submit this activity'.

        db.session.commit()

        cache.delete_memoized(communities_banned_from, blocked.id)
        cache.delete_memoized(joined_communities, blocked.id)
        cache.delete_memoized(moderating_communities, blocked.id)

    add_to_modlog_activitypub('unban_user', blocker, community_id=community.id, link_text=blocked.display_name(),
                              link=f'u/{blocked.link()}', reason=reason)


def create_post_reply(store_ap_json, community: Community, in_reply_to, request_json: dict, user: User,
                      announce_id=None) -> Union[PostReply, None]:
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    if community.local_only:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Community is local only, reply discarded')
        return None
    post_id, parent_comment_id, root_id = find_reply_parent(in_reply_to)

    if post_id or parent_comment_id or root_id:
        # set depth to +1 of the parent depth
        if parent_comment_id:
            parent_comment = PostReply.query.get(parent_comment_id)
            if parent_comment.author.has_blocked_user(user.id) or parent_comment.author.has_blocked_instance(user.instance_id):
                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Parent comment author blocked replier')
                return None
        else:
            parent_comment = None
        if post_id is None:
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Could not find parent post')
            return None
        post = Post.query.get(post_id)

        if post.author.has_blocked_user(user.id) or post.author.has_blocked_instance(user.instance_id):
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Post author blocked replier')
            return None

        body = body_html = ''
        if 'content' in request_json['object']:  # Kbin, Mastodon, etc provide their posts as html
            if not (request_json['object']['content'].startswith('<p>') or request_json['object']['content'].startswith('<blockquote>')):
                request_json['object']['content'] = '<p>' + request_json['object']['content'] + '</p>'
            body_html = allowlist_html(request_json['object']['content'])
            if 'source' in request_json['object'] and isinstance(request_json['object']['source'], dict) and \
                    'mediaType' in request_json['object']['source'] and request_json['object']['source']['mediaType'] == 'text/markdown':
                body = request_json['object']['source']['content']
                body_html = markdown_to_html(body)  # prefer Markdown if provided, overwrite version obtained from HTML
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
        else:
            from app.utils import site_language_id
            language_id = site_language_id()

        distinguished = request_json['object']['distinguished'] if 'distinguished' in request_json['object'] else False

        if 'attachment' in request_json['object']:
            attachment_list = []
            if isinstance(request_json['object']['attachment'], dict):
                attachment_list.append(request_json['object']['attachment'])
            elif isinstance(request_json['object']['attachment'], list):
                attachment_list = request_json['object']['attachment']
            for attachment in attachment_list:
                url = alt_text = ''
                if 'href' in attachment:
                    url = attachment['href']
                if 'url' in attachment:
                    url = attachment['url']
                if 'name' in attachment:
                    alt_text = attachment['name']
                if url:
                    body = body + f"\n\n![{alt_text}]({url})"
            if attachment_list:
                body_html = markdown_to_html(body)

        # Check for Mentions of local users
        reply_parent = parent_comment if parent_comment else post
        local_users_to_notify = []
        if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
            for json_tag in request_json['object']['tag']:
                if 'type' in json_tag and json_tag['type'] == 'Mention':
                    profile_id = json_tag['href'] if 'href' in json_tag else None
                    if profile_id and isinstance(profile_id, str) and profile_id.startswith('https://' + current_app.config['SERVER_NAME']):
                        profile_id = profile_id.lower()
                        if profile_id != reply_parent.author.ap_profile_id:
                            local_users_to_notify.append(profile_id)

        if 'flair' in request_json['object'] and request_json['object']['flair']:
            existing_flair = UserFlair.query.filter(UserFlair.user_id == user.id,
                                                    UserFlair.community_id == community.id).first()
            if existing_flair:
                existing_flair.flair = request_json['object']['flair']
            else:
                db.session.add(UserFlair(user_id=user.id, community_id=community.id,
                                         flair=request_json['object']['flair'].strip()))
            db.session.commit()
        try:
            post_reply = PostReply.new(user, post, parent_comment, notify_author=False, body=body, body_html=body_html,
                                       language_id=language_id, distinguished=distinguished, request_json=request_json,
                                       announce_id=announce_id)
            for lutn in local_users_to_notify:
                recipient = User.query.filter_by(ap_profile_id=lutn, ap_id=None).first()
                if recipient:
                    blocked_senders = blocked_users(recipient.id)
                    if post_reply.user_id not in blocked_senders:
                        author = User.query.get(post_reply.user_id)
                        targets_data = {'gen': '0',
                                        'post_id': post_reply.post_id,
                                        'comment_id': post_reply.id,
                                        'comment_body': post_reply.body,
                                        'author_user_name': author.ap_id if author.ap_id else author.user_name
                                        }
                        with force_locale(get_recipient_language(recipient.id)):
                            notification = Notification(user_id=recipient.id, title=gettext(
                                f"You have been mentioned in comment {post_reply.id}"),
                                                        url=f"https://{current_app.config['SERVER_NAME']}/comment/{post_reply.id}",
                                                        author_id=user.id, notif_type=NOTIF_MENTION,
                                                        subtype='comment_mention',
                                                        targets=targets_data)
                            recipient.unread_notifications += 1
                            db.session.add(notification)
                            db.session.commit()

            return post_reply
        except Exception as ex:
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, str(ex))
            return None
    else:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Unable to find parent post/comment')
        return None


def create_post(store_ap_json, community: Community, request_json: dict, user: User, announce_id=None) -> Union[Post, None]:
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    if community.local_only:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Community is local only, post discarded')
        return None
    try:
        post = Post.new(user, community, request_json, announce_id)
        return post
    except Exception as ex:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, str(ex))
        return None


def notify_about_post(post: Post):
    if current_app.debug:
        notify_about_post_task(post.id)
    else:
        notify_about_post_task.delay(post.id)


@celery.task
def notify_about_post_task(post_id):
    try:
        # get the post by id
        post = Post.query.get(post_id)

        # get the author
        author = User.query.get(post.user_id)

        # get the community
        community = Community.query.get(post.community_id)

        # Send notifications based on subscriptions
        notifications_sent_to = set()

        # NOTIF_USER 
        user_send_notifs_to = notification_subscribers(post.user_id, NOTIF_USER)
        for notify_id in user_send_notifs_to:
            if notify_id != post.user_id and notify_id not in notifications_sent_to:
                targets_data = {'gen': '0',
                                'post_id': post.id,
                                'post_title': post.title,
                                'community_name': community.ap_id if community.ap_id else community.name,
                                'author_id': post.user_id,
                                'author_user_name': author.ap_id if author.ap_id else author.user_name}
                new_notification = Notification(title=shorten_string(post.title, 150), url=f"/post/{post.id}",
                                                user_id=notify_id, author_id=post.user_id,
                                                notif_type=NOTIF_USER,
                                                subtype='new_post_from_followed_user',
                                                targets=targets_data)
                db.session.add(new_notification)
                user = User.query.get(notify_id)
                user.unread_notifications += 1
                db.session.commit()
                notifications_sent_to.add(notify_id)

        # NOTIF_COMMUNITY
        community_send_notifs_to = notification_subscribers(post.community_id, NOTIF_COMMUNITY)
        for notify_id in community_send_notifs_to:
            if notify_id != post.user_id and notify_id not in notifications_sent_to:
                targets_data = {'gen': '0',
                                'post_id': post.id,
                                'post_title': post.title,
                                'community_name': community.ap_id if community.ap_id else community.name,
                                'community_id': post.community_id}
                new_notification = Notification(title=shorten_string(post.title, 150), url=f"/post/{post.id}",
                                                user_id=notify_id, author_id=post.user_id,
                                                notif_type=NOTIF_COMMUNITY,
                                                subtype='new_post_in_followed_community',
                                                targets=targets_data)
                db.session.add(new_notification)
                user = User.query.get(notify_id)
                user.unread_notifications += 1
                db.session.commit()
                notifications_sent_to.add(notify_id)

        # NOTIF_TOPIC    
        topic_send_notifs_to = notification_subscribers(post.community.topic_id, NOTIF_TOPIC)
        if post.community.topic_id:
            topic = Topic.query.get(post.community.topic_id)
        for notify_id in topic_send_notifs_to:
            if notify_id != post.user_id and notify_id not in notifications_sent_to:
                targets_data = {'gen': '0',
                                'post_id': post.id,
                                'post_title': post.title,
                                'community_name': community.ap_id if community.ap_id else community.name,
                                'topic_name': topic.machine_name,
                                'topic_machine_name': topic.name,
                                'author_id': post.user_id}
                new_notification = Notification(title=shorten_string(post.title, 150), url=f"/post/{post.id}",
                                                user_id=notify_id, author_id=post.user_id,
                                                notif_type=NOTIF_TOPIC,
                                                subtype='new_post_in_followed_topic',
                                                targets=targets_data)
                db.session.add(new_notification)
                user = User.query.get(notify_id)
                user.unread_notifications += 1
                db.session.commit()
                notifications_sent_to.add(notify_id)

        # NOTIF_FEED
        # Get all the feeds that the post's community is in
        community_feeds = Feed.query.join(FeedItem, FeedItem.feed_id == Feed.id).filter(
            FeedItem.community_id == post.community_id).all()

        for feed in community_feeds:
            feed_send_notifs_to = notification_subscribers(feed.id, NOTIF_FEED)
            for notify_id in feed_send_notifs_to:
                if notify_id != post.user_id and notify_id not in notifications_sent_to:
                    targets_data = {'gen': '0',
                                    'post_id': post.id,
                                    'post_title': post.title,
                                    'community_name': community.ap_id if community.ap_id else community.name,
                                    'feed_id': feed.id,
                                    'feed_name': feed.title
                                    }
                    new_notification = Notification(title=shorten_string(post.title, 150), url=f"/post/{post.id}",
                                                    user_id=notify_id, author_id=post.user_id,
                                                    notif_type=NOTIF_FEED,
                                                    subtype='new_post_in_followed_feed',
                                                    targets=targets_data)
                    db.session.add(new_notification)
                    user = User.query.get(notify_id)
                    user.unread_notifications += 1
                    db.session.commit()
                notifications_sent_to.add(notify_id)
    except Exception:
        db.session.rollback()
        raise
    finally:
        db.session.remove()


def notify_about_post_reply(parent_reply: Union[PostReply, None], new_reply: PostReply):
    if parent_reply is None:  # This happens when a new_reply is a top-level comment, not a comment on a comment
        send_notifs_to = notification_subscribers(new_reply.post.id, NOTIF_POST)
        post = Post.query.get(new_reply.post.id)
        community = Community.query.get(post.community_id)
        author = User.query.get(new_reply.user_id)
        for notify_id in send_notifs_to:
            if new_reply.user_id != notify_id:
                targets_data = {'gen': '0',
                                'post_id': new_reply.post.id,
                                'post_title': post.title,
                                'community_name': community.ap_id if community.ap_id else community.name,
                                'author_user_name': author.ap_id if author.ap_id else author.user_name,
                                'comment_id': new_reply.id,
                                'comment_body': new_reply.body}
                new_notification = Notification(title=shorten_string(_('Reply to %(post_title)s',
                                                                       post_title=new_reply.post.title), 150),
                                                url=f"/post/{new_reply.post.id}#comment_{new_reply.id}",
                                                user_id=notify_id, author_id=new_reply.user_id,
                                                notif_type=NOTIF_POST,
                                                subtype='top_level_comment_on_followed_post',
                                                targets=targets_data)
                db.session.add(new_notification)
                user = User.query.get(notify_id)
                user.unread_notifications += 1
                db.session.commit()
    else:
        # Send notifications based on subscriptions
        send_notifs_to = set(notification_subscribers(parent_reply.id, NOTIF_REPLY))
        for notify_id in send_notifs_to:
            if new_reply.user_id != notify_id:
                author = User.query.get(new_reply.user_id)
                if new_reply.depth <= THREAD_CUTOFF_DEPTH:
                    targets_data = {'gen': '0',
                                    'post_id': parent_reply.post.id,
                                    'parent_reply_body': parent_reply.body,
                                    'comment_id': new_reply.id,
                                    'comment_body': new_reply.body,
                                    'author_id': new_reply.user_id,
                                    'author_user_name': author.ap_id if author.ap_id else author.user_name, }
                    with force_locale(get_recipient_language(notify_id)):
                        new_notification = Notification(
                            title=shorten_string(gettext('Reply to comment on %(post_title)s',
                                                         post_title=parent_reply.post.title), 150),
                            url=f"/post/{parent_reply.post.id}#comment_{new_reply.id}",
                            user_id=notify_id, author_id=new_reply.user_id,
                            notif_type=NOTIF_REPLY,
                            subtype='new_reply_on_followed_comment',
                            targets=targets_data)
                else:
                    targets_data = {'gen': '0',
                                    'post_id': parent_reply.post.id,
                                    'parent_comment_id': parent_reply.id,
                                    'parent_reply_body': parent_reply.body,
                                    'comment_id': new_reply.id,
                                    'comment_body': new_reply.body,
                                    'author_id': new_reply.user_id,
                                    'author_user_name': author.ap_id if author.ap_id else author.user_name, }
                    with force_locale(get_recipient_language(notify_id)):
                        new_notification = Notification(
                            title=shorten_string(gettext('Reply to comment on %(post_title)s',
                                                         post_title=parent_reply.post.title), 150),
                            url=f"/post/{parent_reply.post.id}/comment/{parent_reply.id}#comment_{new_reply.id}",
                            user_id=notify_id, author_id=new_reply.user_id,
                            notif_type=NOTIF_REPLY,
                            subtype='new_reply_on_followed_comment',
                            targets=targets_data)
                db.session.add(new_notification)
                user = User.query.get(notify_id)
                user.unread_notifications += 1
                db.session.commit()


def update_post_reply_from_activity(reply: PostReply, request_json: dict):
    # Check if this update is more recent than what we currently have - activities can arrive in the wrong order
    if 'updated' in request_json['object'] and reply.ap_updated is not None:
        try:
            new_updated = datetime.fromisoformat(request_json['object']['updated'])
        except ValueError:
            new_updated = datetime.now(timezone.utc)
        if reply.ap_updated.replace(tzinfo=timezone.utc) > new_updated.replace(tzinfo=timezone.utc):
            return
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
        language = find_language_or_create(request_json['object']['language']['identifier'],
                                           request_json['object']['language']['name'])
        reply.language_id = language.id

    # Distinguished
    if 'distinguished' in request_json['object']:
        reply.distinguished = request_json['object']['distinguished']

    reply.edited_at = utcnow()

    if 'attachment' in request_json['object']:
        attachment_list = []
        if isinstance(request_json['object']['attachment'], dict):
            attachment_list.append(request_json['object']['attachment'])
        elif isinstance(request_json['object']['attachment'], list):
            attachment_list = request_json['object']['attachment']
        for attachment in attachment_list:
            url = alt_text = ''
            if 'href' in attachment:
                url = attachment['href']
            if 'url' in attachment:
                url = attachment['url']
            if 'name' in attachment:
                alt_text = attachment['name']
            if url:
                reply.body = reply.body + f"\n\n![{alt_text}]({url})"
        if attachment_list:
            reply.body_html = markdown_to_html(reply.body)

    try:
        reply.ap_updated = datetime.fromisoformat(request_json['object']['updated']) if 'updated' in request_json['object'] else utcnow()
    except ValueError:
        reply.ap_updated = utcnow()

    # Check for Mentions of local users (that weren't in the original)
    if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
        for json_tag in request_json['object']['tag']:
            if 'type' in json_tag and json_tag['type'] == 'Mention':
                profile_id = json_tag['href'] if 'href' in json_tag else None
                if profile_id and isinstance(profile_id, str) and profile_id.startswith('https://' + current_app.config['SERVER_NAME']):
                    profile_id = profile_id.lower()
                    if reply.parent_id:
                        reply_parent = PostReply.query.get(reply.parent_id)
                    else:
                        reply_parent = reply.post
                    if reply_parent and profile_id != reply_parent.author.ap_profile_id:
                        recipient = User.query.filter_by(ap_profile_id=profile_id, ap_id=None).first()
                        if recipient:
                            blocked_senders = blocked_users(recipient.id)
                            if reply.user_id not in blocked_senders:
                                existing_notification = Notification.query.filter(Notification.user_id == recipient.id,
                                                                                  Notification.url == f"https://{current_app.config['SERVER_NAME']}/comment/{reply.id}").first()
                                if not existing_notification:
                                    author = User.query.get(reply.user_id)
                                    targets_data = {'gen': '0',
                                                    'post_id': reply.post_id,
                                                    'comment_id': reply.id,
                                                    'comment_body': reply.body,
                                                    'author_user_name': author.ap_id if author.ap_id else author.user_name
                                                    }
                                    with force_locale(get_recipient_language(recipient.id)):
                                        notification = Notification(user_id=recipient.id, title=gettext(f"You have been mentioned in comment {reply.id}"),
                                                                    url=f"https://{current_app.config['SERVER_NAME']}/comment/{reply.id}",
                                                                    author_id=reply.user_id, notif_type=NOTIF_MENTION,
                                                                    subtype='comment_mention',
                                                                    targets=targets_data)
                                        recipient.unread_notifications += 1
                                        db.session.add(notification)

    db.session.commit()


def update_post_from_activity(post: Post, request_json: dict):
    # Check if this update is more recent than what we currently have - activities can arrive in the wrong order if we have been offline
    if 'updated' in request_json['object'] and post.ap_updated is not None:
        try:
            new_updated = datetime.fromisoformat(request_json['object']['updated'])
        except ValueError:
            new_updated = datetime.now(timezone.utc)
        if post.ap_updated.replace(tzinfo=timezone.utc) > new_updated.replace(tzinfo=timezone.utc):
            return

    # redo body without checking if it's changed
    if 'content' in request_json['object'] and request_json['object']['content'] is not None:
        # prefer Markdown in 'source' in provided
        if 'source' in request_json['object'] and isinstance(request_json['object']['source'], dict) and \
                request_json['object']['source']['mediaType'] == 'text/markdown':
            post.body = request_json['object']['source']['content']
            post.body_html = markdown_to_html(post.body)
        elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/html':
            post.body_html = allowlist_html(request_json['object']['content'])
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
        new_language = find_language_or_create(request_json['object']['language']['identifier'],
                                               request_json['object']['language']['name'])
    elif 'contentMap' in request_json['object'] and isinstance(request_json['object']['contentMap'], dict):
        new_language = find_language(next(iter(request_json['object']['contentMap'])))
    if new_language and (new_language.id != old_language_id):
        post.language_id = new_language.id

    # Tags
    if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
        post.tags.clear()
        # change back when lemmy supports flairs
        # post.flair.clear()
        flair_tags = []
        for json_tag in request_json['object']['tag']:
            if json_tag['type'] == 'Hashtag':
                if json_tag['name'][
                   1:].lower() != post.community.name.lower():  # Lemmy adds the community slug as a hashtag on every post in the community, which we want to ignore
                    hashtag = find_hashtag_or_create(json_tag['name'])
                    if hashtag:
                        post.tags.append(hashtag)
            if json_tag['type'] == 'lemmy:CommunityTag':
                # change back when lemmy supports flairs
                # flair = find_flair_or_create(json_tag, post.community_id)
                # if flair:
                #    post.flair.append(flair)
                flair_tags.append(json_tag)
            if 'type' in json_tag and json_tag['type'] == 'Mention':
                profile_id = json_tag['href'] if 'href' in json_tag else None
                if profile_id and isinstance(profile_id, str) and profile_id.startswith('https://' + current_app.config['SERVER_NAME']):
                    profile_id = profile_id.lower()
                    recipient = User.query.filter_by(ap_profile_id=profile_id, ap_id=None).first()
                    if recipient:
                        blocked_senders = blocked_users(recipient.id)
                        if post.user_id not in blocked_senders:
                            existing_notification = Notification.query.filter(Notification.user_id == recipient.id,
                                                                              Notification.url == f"https://{current_app.config['SERVER_NAME']}/post/{post.id}").first()
                            if not existing_notification:
                                author = User.query.get(post.user_id)
                                targets_data = {'gen': '0',
                                                'post_id': post.id,
                                                'post_title': post.title,
                                                'post_body': post.body,
                                                'author_user_name': author.ap_id if author.ap_id else author.user_name
                                                }
                                notification = Notification(user_id=recipient.id,
                                                            title=_(f"You have been mentioned in post {post.id}"),
                                                            url=f"https://{current_app.config['SERVER_NAME']}/post/{post.id}",
                                                            author_id=post.user_id, notif_type=NOTIF_MENTION,
                                                            subtype='post_mention',
                                                            targets=targets_data)
                                recipient.unread_notifications += 1
                                db.session.add(notification)
        # remove when lemmy supports flairs
        # for now only clear tags if there's new ones or if maybe another PieFed instance is trying to remove them
        if len(flair_tags) > 0 or post.instance.software == 'piefed':
            post.flair.clear()
            for ft in flair_tags:
                flair = find_flair_or_create(ft, post.community_id)
                if flair:
                    post.flair.append(flair)

    post.comments_enabled = request_json['object']['commentsEnabled'] if 'commentsEnabled' in request_json['object'] else True
    try:
        post.ap_updated = datetime.fromisoformat(request_json['object']['updated']) if 'updated' in request_json['object'] else utcnow()
    except ValueError:
        post.ap_updated = utcnow()
    post.edited_at = utcnow()

    if request_json['object']['type'] == 'Video':
        # fetching individual user details to attach to votes is probably too convoluted, so take the instance's word for it
        upvotes = 1  # from OP
        downvotes = 0
        endpoints = ['likes', 'dislikes']
        for endpoint in endpoints:
            if endpoint in request_json['object']:
                try:
                    object_request = get_request(request_json['object'][endpoint], headers={'Accept': 'application/activity+json'})
                except httpx.HTTPError:
                    time.sleep(3)
                    try:
                        object_request = get_request(request_json['object'][endpoint], headers={'Accept': 'application/activity+json'})
                    except httpx.HTTPError:
                        object_request = None
                if object_request and object_request.status_code == 200:
                    try:
                        object = object_request.json()
                    except:
                        object_request.close()
                        object = None
                    object_request.close()
                    if object and 'totalItems' in object:
                        if endpoint == 'likes':
                            upvotes += object['totalItems']
                        if endpoint == 'dislikes':
                            downvotes += object['totalItems']

        # this uses the instance the post is from, rather the instances of individual votes. Useful for promoting PeerTube vids over Lemmy posts.
        multiplier = post.instance.vote_weight
        if not multiplier:
            multiplier = 1.0
        post.up_votes = upvotes * multiplier
        post.down_votes = downvotes
        post.score = upvotes - downvotes
        post.ranking = post.post_ranking(post.score, post.posted_at)
        post.ranking_scaled = int(post.ranking + post.community.scale_by())
        # return now for PeerTube, otherwise rest of this function breaks the post
        db.session.commit()
        return

    if request_json['object']['type'] == 'Question':
        # an Update is probably just informing us of new totals, but it could be an Edit to the Poll itself (totalItems for all choices will be 0)
        mode = 'single'
        if 'oneOf' in request_json['object']:
            votes = request_json['object']['oneOf']
        elif 'anyOf' in request_json['object']:
            votes = request_json['object']['anyOf']
            mode = 'multiple'
        else:
            return

        total_vote_count = 0
        for vote in votes:
            if not 'name' in vote:
                continue
            if not 'replies' in vote:
                continue
            if not 'totalItems' in vote['replies']:
                continue

            total_vote_count += vote['replies']['totalItems']

        if total_vote_count == 0:  # Edit, not a totals update
            poll = Poll.query.filter_by(post_id=post.id).first()
            if poll:
                if not 'endTime' in request_json['object']:
                    return
                poll.end_poll = request_json['object']['endTime']
                poll.mode = mode

                db.session.execute(text('DELETE FROM "poll_choice_vote" WHERE post_id = :post_id'),
                                   {'post_id': post.id})
                db.session.execute(text('DELETE FROM "poll_choice" WHERE post_id = :post_id'), {'post_id': post.id})

                i = 1
                for vote in votes:
                    new_choice = PollChoice(post_id=post.id, choice_text=vote['name'], sort_order=i)
                    db.session.add(new_choice)
                    i += 1
                db.session.commit()
            return

        # totals Update
        for vote in votes:
            choice = PollChoice.query.filter_by(post_id=post.id, choice_text=vote['name']).first()
            if choice:
                choice.num_votes = vote['replies']['totalItems']
        db.session.commit()
        # no URLs in Polls to worry about, so return now
        return

    # Links
    old_url = post.url
    new_url = None
    if ('attachment' in request_json['object'] and
            isinstance(request_json['object']['attachment'], list) and
            len(request_json['object']['attachment']) > 0 and
            'type' in request_json['object']['attachment'][0]):

        if request_json['object']['attachment'][0]['type'] == 'Link':
            if 'href' in request_json['object']['attachment'][0]:
                new_url = request_json['object']['attachment'][0]['href']  # Lemmy < 0.19.4
            elif 'url' in request_json['object']['attachment'][0]:
                new_url = request_json['object']['attachment'][0]['url']  # NodeBB

        if request_json['object']['attachment'][0]['type'] == 'Document':
            new_url = request_json['object']['attachment'][0]['url']  # Mastodon

        if request_json['object']['attachment'][0]['type'] == 'Image':
            new_url = request_json['object']['attachment'][0]['url']  # PixelFed / PieFed / Lemmy >= 0.19.4

        if request_json['object']['attachment'][0]['type'] == 'Audio':  # WordPress podcast
            new_url = request_json['object']['attachment'][0]['url']
            if 'name' in request_json['object']['attachment'][0]:
                post.title = request_json['object']['attachment'][0]['name']

    if 'attachment' in request_json['object'] and isinstance(request_json['object']['attachment'],
                                                             dict):  # Mastodon / a.gup.pe
        new_url = request_json['object']['attachment']['url']
    if new_url:
        new_domain = domain_from_url(new_url)
        if new_domain.banned:
            db.session.commit()
            return  # reject change to url if new domain is banned
    old_db_entry_to_delete = None
    if old_url != new_url:
        if post.image:
            post.image.delete_from_disk()
            old_db_entry_to_delete = post.image_id
        if new_url:
            thumbnail_url, embed_url = fixup_url(new_url)
            post.url = embed_url
            image = None
            if is_image_url(new_url):
                post.type = POST_TYPE_IMAGE
                image = File(source_url=new_url)
                if isinstance(request_json['object']['attachment'], list) and \
                        'name' in request_json['object']['attachment'][0] and request_json['object']['attachment'][0]['name'] is not None:
                    image.alt_text = request_json['object']['attachment'][0]['name']
            else:
                if 'image' in request_json['object'] and 'url' in request_json['object']['image']:
                    image = File(source_url=request_json['object']['image']['url'])
                else:
                    # Let's see if we can do better than the source instance did!
                    opengraph = opengraph_parse(thumbnail_url)
                    if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                        filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                        if not filename.startswith('/'):
                            image = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                if is_video_hosting_site(embed_url) or is_video_url(new_url):
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
                targets_data = {'gen': '0',
                                'post_id': post.id,
                                'orig_post_title': post.title,
                                'orig_post_body': post.body,
                                'orig_post_domain': post.domain,
                                }
                if new_domain.notify_mods:
                    for community_member in post.community.moderators():
                        notify = Notification(title='Suspicious content', url=post.ap_id,
                                              user_id=community_member.user_id,
                                              author_id=1, notif_type=NOTIF_REPORT,
                                              subtype='post_from_suspicious_domain',
                                              targets=targets_data)
                        db.session.add(notify)
                        already_notified.add(community_member.user_id)
                if new_domain.notify_admins:
                    for admin in Site.admins():
                        if admin.id not in already_notified:
                            targets_data = {'gen': '0',
                                            'post_id': post.id,
                                            'orig_post_title': post.title,
                                            'orig_post_body': post.body,
                                            'orig_post_domain': post.domain,
                                            }
                            notify = Notification(title='Suspicious content',
                                                  url=post.ap_id, user_id=admin.id,
                                                  author_id=1, notif_type=NOTIF_REPORT,
                                                  subtype='post_from_suspicious_domain',
                                                  targets=targets_data)
                            db.session.add(notify)
                new_domain.post_count += 1
                post.domain = new_domain

            # Fix-up cross posts (Posts which link to the same url as other posts)
            if post.cross_posts is not None:
                post.calculate_cross_posts(url_changed=True)

        else:
            post.type = POST_TYPE_ARTICLE
            post.url = ''
            post.image_id = None
            if post.cross_posts is not None:  # unlikely, but not impossible
                post.calculate_cross_posts(delete_only=True)

    db.session.commit()
    if old_db_entry_to_delete:
        File.query.filter_by(id=old_db_entry_to_delete).delete()
        db.session.commit()


def undo_vote(comment, post, target_ap_id, user):
    voted_on = find_liked_object(target_ap_id)
    if isinstance(voted_on, Post):
        post = voted_on
        existing_vote = PostVote.query.filter_by(user_id=user.id, post_id=post.id).first()
        if existing_vote:
            with db.session.begin_nested():
                db.session.execute(text('UPDATE "user" SET reputation = reputation - :effect WHERE id = :user_id'),
                                   {'effect': existing_vote.effect, 'user_id': post.user_id})
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
            with db.session.begin_nested():
                db.session.execute(text('UPDATE "user" SET reputation = reputation - :effect WHERE id = :user_id'),
                                   {'effect': existing_vote.effect, 'user_id': comment.user_id})
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
    if 'summary' not in request_json:  # reports from peertube have no summary
        reasons = ''
        description = ''
        if 'content' in request_json:
            reasons = request_json['content']
    else:
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
                        type=type, reporter_id=user.id, suspect_user_id=reported.id,
                        source_instance_id=user.instance_id)
        db.session.add(report)

        # Notify site admin
        already_notified = set()
        source_instance = Instance.query.get(user.instance_id)
        for admin in Site.admins():
            if admin.id not in already_notified:
                targets_data = {'gen': '0',
                                'suspect_user_id': reported.id,
                                'suspect_user_user_name': reported.ap_id if reported.ap_id else reported.user_name,
                                'reporter_id': user.id,
                                'reporter_user_name': user.ap_id if user.ap_id else user.user_name,
                                'source_instance_id': user.instance_id,
                                'source_instance_domain': source_instance.domain,
                                'reasons': reasons,
                                'description': description
                                }
                notify = Notification(title='Reported user', url='/admin/reports', user_id=admin.id,
                                      author_id=user.id, notif_type=NOTIF_REPORT,
                                      subtype='user_reported',
                                      targets=targets_data)
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
        suspect_author = User.query.get(reported.author.id)
        source_instance = Instance.query.get(user.instance_id)
        for mod in reported.community.moderators():
            targets_data = {'gen': '0',
                            'suspect_post_id': reported.id,
                            'suspect_user_id': reported.author.id,
                            'suspect_user_user_name': suspect_author.ap_id if suspect_author.ap_id else suspect_author.user_name,
                            'reporter_id': user.id,
                            'reporter_user_name': user.ap_id if user.ap_id else user.user_name,
                            'source_instance_id': user.instance_id,
                            'source_instance_domain': source_instance.domain,
                            'orig_post_title': reported.title,
                            'orig_post_body': reported.body
                            }
            notification = Notification(user_id=mod.user_id, title=_('A post has been reported'),
                                        url=f"https://{current_app.config['SERVER_NAME']}/post/{reported.id}",
                                        author_id=user.id, notif_type=NOTIF_REPORT,
                                        subtype='post_reported',
                                        targets=targets_data)
            db.session.add(notification)
            already_notified.add(mod.user_id)
        reported.reports += 1
        db.session.commit()
    elif isinstance(reported, PostReply):
        if reported.reports == -1:
            return
        type = 2
        post = Post.query.get(reported.post_id)
        report = Report(reasons=reasons, description=description, type=type, reporter_id=user.id,
                        suspect_post_id=post.id,
                        suspect_community_id=post.community.id,
                        suspect_user_id=reported.author.id, suspect_post_reply_id=reported.id,
                        in_community_id=post.community.id,
                        source_instance_id=user.instance_id)
        db.session.add(report)
        # Notify moderators
        already_notified = set()
        suspect_author = User.query.get(reported.author.id)
        source_instance = Instance.query.get(user.instance_id)
        for mod in post.community.moderators():
            targets_data = {'gen': '0',
                            'suspect_comment_id': reported.id,
                            'suspect_user_id': reported.author.id,
                            'suspect_user_user_name': suspect_author.ap_id if suspect_author.ap_id else suspect_author.user_name,
                            'reporter_id': user.id,
                            'reporter_user_name': user.ap_id if user.ap_id else user.name,
                            'source_instance_id': user.instance_id,
                            'source_instance_domain': source_instance.domain,
                            'orig_comment_body': reported.body
                            }
            notification = Notification(user_id=mod.user_id, title=_('A comment has been reported'),
                                        url=f"https://{current_app.config['SERVER_NAME']}/comment/{reported.id}",
                                        author_id=user.id, notif_type=NOTIF_REPORT,
                                        subtype='comment_reported',
                                        targets=targets_data)
            db.session.add(notification)
            already_notified.add(mod.user_id)
        reported.reports += 1
        db.session.commit()
    elif isinstance(reported, Community):
        ...
    elif isinstance(reported, Conversation):
        ...


def lemmy_site_data():
    site = g.site
    logo = site.logo if site.logo else '/static/images/piefed_logo_icon_t_75.png'
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
                "captcha_enabled": get_setting('captcha_enabled', True),
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
        "version": current_app.config['VERSION'],
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
        data['admins'].append({'person': person, 'counts': counts, 'is_admin': True})
    return data


def ensure_domains_match(activity: dict) -> bool:
    if 'id' in activity:
        note_id = activity['id']
    else:
        note_id = None

    note_actor = None
    if 'actor' in activity:
        note_actor = activity['actor']
    elif 'attributedTo' in activity:
        attributed_to = activity['attributedTo']
        if isinstance(attributed_to, str):
            note_actor = attributed_to
        elif isinstance(attributed_to, list):
            for a in attributed_to:
                if isinstance(a, dict) and a.get('type') == 'Person':
                    note_actor = a.get('id')
                    break
                elif isinstance(a, str):
                    note_actor = a
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


def remote_object_to_json(uri):
    try:
        object_request = get_request(uri, headers={'Accept': 'application/activity+json'})
    except httpx.HTTPError:
        time.sleep(3)
        try:
            object_request = get_request(uri, headers={'Accept': 'application/activity+json'})
        except httpx.HTTPError:
            return None
    if object_request.status_code == 200:
        try:
            object = object_request.json()
            return object
        except:
            object_request.close()
            return None
        object_request.close()
    elif object_request.status_code == 401:
        site = Site.query.get(1)
        try:
            object_request = signed_get_request(uri, site.private_key, f"https://{current_app.config['SERVER_NAME']}/actor#main-key")
        except httpx.HTTPError:
            time.sleep(3)
            try:
                object_request = signed_get_request(uri, site.private_key, f"https://{current_app.config['SERVER_NAME']}/actor#main-key")
            except httpx.HTTPError:
                return None
        try:
            object = object_request.json()
            return object
        except:
            object_request.close()
            return None
        object_request.close()
    else:
        return None


# called from incoming activitypub, when the object in an Announce is just a URL
# despite the name, it works for both posts and replies
def resolve_remote_post(uri: str, community, announce_id, store_ap_json, nodebb=False) -> Union[Post, PostReply, None]:
    parsed_url = urlparse(uri)
    uri_domain = parsed_url.netloc
    announce_actor = community.ap_profile_id
    parsed_url = urlparse(announce_actor)
    announce_actor_domain = parsed_url.netloc
    if announce_actor_domain != 'a.gup.pe' and not nodebb and announce_actor_domain != uri_domain:
        return None

    post_data = remote_object_to_json(uri)
    if not post_data:
        return None

    return create_resolved_object(uri, post_data, uri_domain, community, announce_id, store_ap_json)


def create_resolved_object(uri, post_data, uri_domain, community, announce_id, store_ap_json):
    # find the author. Make sure their domain matches the site hosting it to mitigate impersonation attempts
    actor_domain = None
    actor = None
    if 'attributedTo' in post_data:
        attributed_to = post_data['attributedTo']
        if isinstance(attributed_to, str):
            actor = attributed_to
            parsed_url = urlparse(actor)
            actor_domain = parsed_url.netloc
        elif isinstance(attributed_to, list):
            for a in attributed_to:
                if isinstance(a, dict) and a.get('type') == 'Person':
                    actor = a.get('id')
                    if isinstance(actor, str):  # Ensure `actor` is a valid string
                        parsed_url = urlparse(actor)
                        actor_domain = parsed_url.netloc
                    break
                elif isinstance(a, str):
                    actor = a
                    parsed_url = urlparse(actor)
                    actor_domain = parsed_url.netloc
                    break
    if uri_domain != actor_domain:
        return None

    user = find_actor_or_create(actor)
    if user and community and post_data:
        activity = 'update' if 'updated' in post_data else 'create'
        request_json = {'id': f"https://{uri_domain}/activities/{activity}/{gibberish(15)}", 'object': post_data}
        if 'inReplyTo' in request_json['object'] and request_json['object']['inReplyTo']:
            if activity == 'update':
                post_reply = PostReply.get_by_ap_id(uri)
                if post_reply:
                    update_post_reply_from_activity(post_reply, request_json)
                else:
                    activity = 'create'
            if activity == 'create':
                post_reply = create_post_reply(store_ap_json, community, request_json['object']['inReplyTo'], request_json, user)
                if post_reply:
                    if 'published' in post_data:
                        post_reply.posted_at = post_data['published']
                        post_reply.post.last_active = post_data['published']
                        post_reply.community.last_active = utcnow()
                        db.session.commit()
            if post_reply:
                return post_reply
        else:
            if activity == 'update':
                post = Post.get_by_ap_id(uri)
                if post:
                    update_post_from_activity(post, request_json)
                else:
                    activity = 'create'
            if activity == 'create':
                post = create_post(store_ap_json, community, request_json, user, announce_id)
                if post:
                    if 'published' in post_data:
                        post.posted_at = post_data['published']
                        post.last_active = post_data['published']
                        post.community.last_active = utcnow()
                        db.session.commit()
            if post:
                return post

    return None


@celery.task
def get_nodebb_replies_in_background(replies_uri_list, community_id):
    try:
        max = 10 if not current_app.debug else 2  # magic number alert
        community = Community.query.get(community_id)
        if not community:
            return
        reply_count = 0
        for uri in replies_uri_list:
            reply_count += 1
            resolve_remote_post(uri, community, None, False, nodebb=True)
            if reply_count >= max:
                break
    except Exception:
        db.session.rollback()
        raise
    finally:
        db.session.remove()


def populate_child_feed(feed_id, child_feed):
    if current_app.debug:
        populate_child_feed_worker(feed_id, child_feed)
    else:
        populate_child_feed_worker.delay(feed_id, child_feed)


@celery.task
def populate_child_feed_worker(feed_id, child_feed):
    try:
        from app.feed.util import search_for_feed
        server, feed = extract_domain_and_actor(child_feed)
        new_feed = search_for_feed('~' + feed + '@' + server)
        new_feed.parent_feed_id = feed_id
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        db.session.remove()


# called from UI, via 'search' option in navbar, or 'Retrieve a post from the original server' in community sidebar
def resolve_remote_post_from_search(uri: str) -> Union[Post, None]:
    post = Post.get_by_ap_id(uri)
    if post:
        return post

    parsed_url = urlparse(uri)
    uri_domain = parsed_url.netloc
    actor_domain = None
    actor = None

    post_data = remote_object_to_json(uri)
    if not post_data:
        return None

    # nodebb. the post is the first entry in orderedItems of a topic, and the replies are the remaining entries
    # just gets orderedItems[0] to retrieve the post, and then replies are retrieved in the background
    topic_post_data = post_data
    nodebb = False
    if ('type' in post_data and post_data['type'] == 'Conversation' and
            'posts' in post_data and isinstance(post_data['posts'], str)):
        post_data = remote_object_to_json(post_data['posts'])
        if not post_data:
            return None
        topic_post_data = post_data
    if ('type' in post_data and post_data['type'] == 'OrderedCollection' and
            'totalItems' in post_data and post_data['totalItems'] > 0 and
            'orderedItems' in post_data and isinstance(post_data['orderedItems'], list)):
        nodebb = True
        uri = post_data['orderedItems'][0]
        parsed_url = urlparse(uri)
        uri_domain = parsed_url.netloc
        post_data = remote_object_to_json(uri)
        if not post_data:
            return None

    # check again that it doesn't already exist (can happen with different but equivalent URLs)
    post = Post.get_by_ap_id(post_data['id'])
    if post:
        return post

    # find the author of the post. Make sure their domain matches the site hosting it to mitigate impersonation attempts
    if 'attributedTo' in post_data:
        attributed_to = post_data['attributedTo']
        if isinstance(attributed_to, str):
            actor = attributed_to
            parsed_url = urlparse(actor)
            actor_domain = parsed_url.netloc
        elif isinstance(attributed_to, list):
            for a in attributed_to:
                if isinstance(a, dict) and a.get('type') == 'Person':
                    actor = a.get('id')
                    if isinstance(actor, str):  # Ensure `actor` is a valid string
                        parsed_url = urlparse(actor)
                        actor_domain = parsed_url.netloc
                    break
                elif isinstance(a, str):
                    actor = a
                    parsed_url = urlparse(actor)
                    actor_domain = parsed_url.netloc
                    break
    if uri_domain != actor_domain:
        return None

    # find the community the post was submitted to
    community = find_community(post_data)
    if not community and nodebb:
        community = find_community(topic_post_data)  # use 'audience' from topic if post has no info for how it got there
    # find the post's author
    user = find_actor_or_create(actor)
    if user and community and post_data:
        request_json = {'id': f"https://{uri_domain}/activities/create/{gibberish(15)}", 'object': post_data}
        # not really what this function is intended for, but get comment or fail if comment URL is searched for
        if 'inReplyTo' in post_data and post_data['inReplyTo'] is not None:
            in_reply_to = post_data['inReplyTo']
            object = create_post_reply(False, community, in_reply_to, request_json, user)
        else:
            in_reply_to = None
            object = create_post(False, community, request_json, user)
        if object:
            if 'published' in post_data:
                object.posted_at = post_data['published']
                if not in_reply_to:
                    object.last_active = post_data['published']
                db.session.commit()
            if nodebb and topic_post_data['totalItems'] > 1:
                if current_app.debug:
                    get_nodebb_replies_in_background(topic_post_data['orderedItems'][1:], community.id)
                else:
                    get_nodebb_replies_in_background.delay(topic_post_data['orderedItems'][1:], community.id)
            return object if not in_reply_to else object.post

    return None


# called from activitypub/routes if something is posted to us without any kind of signature (typically from PeerTube)
def verify_object_from_source(request_json):
    uri = request_json['object']
    uri_domain = urlparse(uri).netloc
    if not uri_domain:
        return None

    create_domain = urlparse(request_json['actor']).netloc
    if create_domain != uri_domain:
        return None

    try:
        object_request = get_request(uri, headers={'Accept': 'application/activity+json'})
    except httpx.HTTPError:
        time.sleep(3)
        try:
            object_request = get_request(uri, headers={'Accept': 'application/activity+json'})
        except httpx.HTTPError:
            return None
    if object_request.status_code == 200:
        try:
            object = object_request.json()
        except:
            object_request.close()
            return None
        object_request.close()
    elif object_request.status_code == 401:
        site = Site.query.get(1)
        try:
            object_request = signed_get_request(uri, site.private_key, f"https://{current_app.config['SERVER_NAME']}/actor#main-key")
        except httpx.HTTPError:
            time.sleep(3)
            try:
                object_request = signed_get_request(uri, site.private_key, f"https://{current_app.config['SERVER_NAME']}/actor#main-key")
            except httpx.HTTPError:
                return None
        try:
            object = object_request.json()
        except:
            object_request.close()
            return None
        object_request.close()
    else:
        return None

    if not 'id' in object or not 'type' in object or not 'attributedTo' in object:
        return None

    actor_domain = ''
    if isinstance(object['attributedTo'], str):
        actor_domain = urlparse(object['attributedTo']).netloc
    elif isinstance(object['attributedTo'], dict) and 'id' in object['attributedTo']:
        actor_domain = urlparse(object['attributedTo']['id']).netloc
    elif isinstance(object['attributedTo'], list):
        for a in object['attributedTo']:
            if isinstance(a, str):
                actor_domain = urlparse(a).netloc
                break
            elif isinstance(a, dict) and a.get('type') == 'Person':
                actor = a.get('id')
                if isinstance(actor, str):
                    parsed_url = urlparse(actor)
                    actor_domain = parsed_url.netloc
                break
    else:
        return None

    if uri_domain != actor_domain:
        return None

    request_json['object'] = object
    return request_json


def log_incoming_ap(id, aplog_type, aplog_result, saved_json, message=None):
    aplog_in = APLOG_IN

    if aplog_in and aplog_type[0] and aplog_result[0]:
        if current_app.config['LOG_ACTIVITYPUB_TO_DB']:
            activity_log = ActivityPubLog(direction='in', activity_id=id, activity_type=aplog_type[1], result=aplog_result[1])
            if message:
                activity_log.exception_message = message
            if saved_json:
                activity_log.activity_json = json.dumps(saved_json)
            db.session.add(activity_log)
            db.session.commit()

        if current_app.config['LOG_ACTIVITYPUB_TO_FILE']:
            current_app.logger.info(f'piefed.social activity: {id} Type: {aplog_type[1]}, Result: {aplog_result[1]}, {message}')


def find_community(request_json):
    # Create/Update from platform that included Community in 'audience', 'cc', or 'to' in outer or inner object
    # Also works for manually retrieved posts
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
                            return potential_community
                if isinstance(potential_id, list):
                    for c in potential_id:
                        if not c.startswith('https://www.w3.org') and not c.endswith('/followers'):
                            potential_community = Community.query.filter_by(ap_profile_id=c.lower()).first()
                            if potential_community:
                                return potential_community

    rj = request_json['object'] if 'object' in request_json else request_json

    # Create/Update Note from platform that didn't include the Community in 'audience', 'cc', or 'to' (e.g. Mastodon reply to Lemmy post)
    if 'inReplyTo' in rj and rj['inReplyTo'] is not None:
        post_being_replied_to = Post.get_by_ap_id(rj['inReplyTo'])
        if post_being_replied_to:
            return post_being_replied_to.community
        else:
            comment_being_replied_to = PostReply.get_by_ap_id(rj['inReplyTo'])
            if comment_being_replied_to:
                return comment_being_replied_to.community

    # Update / Video from PeerTube (possibly an edit, more likely an invite to query Likes / Replies endpoints)
    if rj['type'] == 'Video':
        if 'attributedTo' in rj and isinstance(rj['attributedTo'], list):
            for a in rj['attributedTo']:
                if a['type'] == 'Group':
                    potential_community = Community.query.filter_by(ap_profile_id=a['id'].lower()).first()
                    if potential_community:
                        return potential_community

    return None


def normalise_actor_string(actor: str) -> Tuple[str, str]:
    # Turns something like whatever@server.tld into tuple(whatever, server.tld)
    actor = actor.strip()
    if actor[0] == '@' or actor[0] == '!' or actor[0] == '~':
        actor = actor[1:]

    if '@' in actor:
        parts = actor.split('@')
        return parts[0].lower(), parts[1].lower()
