from datetime import datetime, timedelta
from threading import Thread
from time import sleep
from random import randint
from typing import List
import requests
from PIL import Image, ImageOps
from flask import request, abort, g, current_app, json
from flask_login import current_user
from pillow_heif import register_heif_opener

from app import db, cache, celery
from app.activitypub.signature import post_request, default_context
from app.activitypub.util import find_actor_or_create, actor_json_to_model, post_json_to_model, ensure_domains_match, \
    find_hashtag_or_create
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_LINK, POST_TYPE_IMAGE, POST_TYPE_VIDEO, NOTIF_POST, \
    POST_TYPE_POLL
from app.models import Community, File, BannedInstances, PostReply, PostVote, Post, utcnow, CommunityMember, Site, \
    Instance, Notification, User, ActivityPubLog, NotificationSubscription, Language, Tag, PollChoice, Poll
from app.utils import get_request, gibberish, markdown_to_html, domain_from_url, allowlist_html, \
    is_image_url, ensure_directory_exists, inbox_domain, post_ranking, shorten_string, parse_page, \
    remove_tracking_from_link, ap_datetime, instance_banned, blocked_phrases, url_to_thumbnail_file, opengraph_parse
from sqlalchemy import func, desc, text
import os


allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.mpo']


def search_for_community(address: str):
    if address.startswith('!'):
        name, server = address[1:].split('@')

        banned = BannedInstances.query.filter_by(domain=server).first()
        if banned:
            reason = f" Reason: {banned.reason}" if banned.reason is not None else ''
            raise Exception(f"{server} is blocked.{reason}")  # todo: create custom exception class hierarchy

        already_exists = Community.query.filter_by(ap_id=address[1:]).first()
        if already_exists:
            return already_exists

        # Look up the profile address of the community using WebFinger
        try:
            webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                         params={'resource': f"acct:{address[1:]}"})
        except requests.exceptions.ReadTimeout:
            sleep(randint(3, 10))
            try:
                webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                            params={'resource': f"acct:{address[1:]}"})
            except requests.exceptions.RequestException:
                return None
        except requests.exceptions.RequestException:
            return None
        if webfinger_data.status_code == 200:
            webfinger_json = webfinger_data.json()
            for links in webfinger_json['links']:
                if 'rel' in links and links['rel'] == 'self':  # this contains the URL of the activitypub profile
                    type = links['type'] if 'type' in links else 'application/activity+json'
                    # retrieve the activitypub profile
                    community_data = get_request(links['href'], headers={'Accept': type})
                    # to see the structure of the json contained in community_data, do a GET to https://lemmy.world/c/technology with header Accept: application/activity+json
                    if community_data.status_code == 200:
                        community_json = community_data.json()
                        community_data.close()
                        if community_json['type'] == 'Group':
                            community = actor_json_to_model(community_json, name, server)
                            if community:
                                if community.ap_profile_id == f"https://{server}/video-channels/{name}":
                                    if current_app.debug:
                                        retrieve_peertube_mods_and_backfill(community.id, community_json['attributedTo'])
                                    else:
                                        retrieve_peertube_mods_and_backfill.delay(community.id, community_json['attributedTo'])
                                    return community
                                if current_app.debug:
                                    retrieve_mods_and_backfill(community.id)
                                else:
                                    retrieve_mods_and_backfill.delay(community.id)
                            return community
        return None


@celery.task
def retrieve_peertube_mods_and_backfill(community_id: int, mods: list):
    community = Community.query.get(community_id)
    site = Site.query.get(1)
    for m in mods:
        user = find_actor_or_create(m['id'])
        if user:
            existing_membership = CommunityMember.query.filter_by(community_id=community.id, user_id=user.id).first()
            if existing_membership:
                existing_membership.is_moderator = True
            else:
                new_membership = CommunityMember(community_id=community.id, user_id=user.id, is_moderator=True)
                db.session.add(new_membership)
    community.restricted_to_mods = True
    db.session.commit()

    if community.ap_public_url:
        outbox_request = get_request(community.ap_outbox_url, headers={'Accept': 'application/activity+json'})
        if outbox_request.status_code == 200:
            outbox_data = outbox_request.json()
            outbox_request.close()
            if 'totalItems' in outbox_data and outbox_data['totalItems'] > 0:
                page1_request = get_request(outbox_data['first'], headers={'Accept': 'application/activity+json'})
                if page1_request.status_code == 200:
                    page1_data = page1_request.json()
                    page1_request.close()
                    if 'type' in page1_data and page1_data['type'] == 'OrderedCollectionPage' and 'orderedItems' in page1_data:
                        # only 10 posts per page for PeerTube
                        for activity in page1_data['orderedItems']:
                            video_request = get_request(activity['object'], headers={'Accept': 'application/activity+json'})
                            if video_request.status_code == 200:
                                video_data = video_request.json()
                                video_request.close()
                                activity_log = ActivityPubLog(direction='in', activity_id=video_data['id'], activity_type='Video', result='failure')
                                if site.log_activitypub_json:
                                    activity_log.activity_json = json.dumps(video_data)
                                db.session.add(activity_log)
                                if not ensure_domains_match(video_data):
                                    activity_log.exception_message = 'Domains do not match'
                                    db.session.commit()
                                    continue
                                if user and user.is_local():
                                    activity_log.exception_message = 'Activity about local content which is already present'
                                    db.session.commit()
                                    continue
                                if user:
                                    post = post_json_to_model(activity_log, video_data, user, community)
                                    post.ap_announce_id = activity['id']
                                    post.ranking = post_ranking(post.score, post.posted_at)
                                else:
                                    activity_log.exception_message = 'Could not find or create actor'
                                    db.session.commit()
                        if community.post_count > 0:
                            community.last_active = Post.query.filter(Post.community_id == community_id).order_by(desc(Post.posted_at)).first().posted_at
                        db.session.commit()


@celery.task
def retrieve_mods_and_backfill(community_id: int):
    with current_app.app_context():
        community = Community.query.get(community_id)
        site = Site.query.get(1)
        if community.ap_moderators_url:
            mods_request = get_request(community.ap_moderators_url, headers={'Accept': 'application/activity+json'})
            if mods_request.status_code == 200:
                mods_data = mods_request.json()
                mods_request.close()
                if mods_data and mods_data['type'] == 'OrderedCollection' and 'orderedItems' in mods_data:
                    for actor in mods_data['orderedItems']:
                        sleep(0.5)
                        user = find_actor_or_create(actor)
                        if user:
                            existing_membership = CommunityMember.query.filter_by(community_id=community.id, user_id=user.id).first()
                            if existing_membership:
                                existing_membership.is_moderator = True
                            else:
                                new_membership = CommunityMember(community_id=community.id, user_id=user.id, is_moderator=True)
                                db.session.add(new_membership)
                    db.session.commit()

        # only backfill nsfw if nsfw communities are allowed
        if (community.nsfw and not site.enable_nsfw) or (community.nsfl and not site.enable_nsfl):
            return

        # download 50 old posts
        if community.ap_public_url:
            outbox_request = get_request(community.ap_outbox_url, headers={'Accept': 'application/activity+json'})
            if outbox_request.status_code == 200:
                outbox_data = outbox_request.json()
                outbox_request.close()
                if 'type' in outbox_data and outbox_data['type'] == 'OrderedCollection' and 'orderedItems' in outbox_data:
                    activities_processed = 0
                    for activity in outbox_data['orderedItems']:
                        activity_log = ActivityPubLog(direction='in', activity_id=activity['id'], activity_type='Announce', result='failure')
                        if site.log_activitypub_json:
                            activity_log.activity_json = json.dumps(activity)
                        db.session.add(activity_log)
                        if 'object' in activity and 'object' in activity['object']:
                            if not ensure_domains_match(activity['object']['object']):
                                activity_log.exception_message = 'Domains do not match'
                                db.session.commit()
                                continue
                        user = find_actor_or_create(activity['object']['actor'])
                        if user and user.is_local():
                                activity_log.exception_message = 'Activity about local content which is already present'
                                db.session.commit()
                                continue
                        if user:
                            post = post_json_to_model(activity_log, activity['object']['object'], user, community)
                            if post:
                                post.ap_create_id = activity['object']['id']
                                post.ap_announce_id = activity['id']
                                post.ranking = post_ranking(post.score, post.posted_at)
                                if post.url:
                                    other_posts = Post.query.filter(Post.id != post.id, Post.url == post.url,
                                                                    Post.posted_at > post.posted_at - timedelta(days=3),
                                                                    Post.posted_at < post.posted_at + timedelta(days=3)).all()
                                    for op in other_posts:
                                        if op.cross_posts is None:
                                            op.cross_posts = [post.id]
                                        else:
                                            op.cross_posts.append(post.id)
                                        if post.cross_posts is None:
                                            post.cross_posts = [op.id]
                                        else:
                                            post.cross_posts.append(op.id)
                                db.session.commit()
                        else:
                            activity_log.exception_message = 'Could not find or create actor'
                            db.session.commit()

                        activities_processed += 1
                        if activities_processed >= 50:
                            break
                    c = Community.query.get(community.id)
                    if c.post_count > 0:
                        c.last_active = Post.query.filter(Post.community_id == community_id).order_by(desc(Post.posted_at)).first().posted_at
                    db.session.commit()
                if community.ap_featured_url:
                    featured_request = get_request(community.ap_featured_url, headers={'Accept': 'application/activity+json'})
                    if featured_request.status_code == 200:
                        featured_data = featured_request.json()
                        featured_request.close()
                        if featured_data['type'] == 'OrderedCollection' and 'orderedItems' in featured_data:
                            for item in featured_data['orderedItems']:
                                featured_id = item['id']
                                p = Post.query.filter(Post.ap_id == featured_id).first()
                                if p:
                                    p.sticky = True
                                    db.session.commit()


def actor_to_community(actor) -> Community:
    actor = actor.strip()
    if '@' in actor:
        community = Community.query.filter_by(banned=False, ap_id=actor).first()
    else:
        community = Community.query.filter(func.lower(Community.name) == func.lower(actor)).filter_by(banned=False, ap_id=None).first()
    return community


def save_post(form, post: Post, type: int):
    post.indexable = current_user.indexable
    post.sticky = form.sticky.data
    post.nsfw = form.nsfw.data
    post.nsfl = form.nsfl.data
    post.notify_author = form.notify_author.data
    post.language_id = form.language_id.data
    current_user.language_id = form.language_id.data
    post.title = form.title.data
    post.body = form.body.data
    post.body_html = markdown_to_html(post.body)
    if not type or type == POST_TYPE_ARTICLE:
        post.type = POST_TYPE_ARTICLE
    elif type == POST_TYPE_LINK:
        url_changed = post.id is None or form.link_url.data != post.url
        post.url = remove_tracking_from_link(form.link_url.data.strip())
        post.type = POST_TYPE_LINK
        domain = domain_from_url(form.link_url.data)
        domain.post_count += 1
        post.domain = domain

        if url_changed:
            if post.image_id:
                remove_old_file(post.image_id)
                post.image_id = None

            if post.url.endswith('.mp4') or post.url.endswith('.webm'):
                post.type = POST_TYPE_VIDEO
                file = File(source_url=form.link_url.data)  # make_image_sizes() will take care of turning this into a still image
                post.image = file
                db.session.add(file)
            else:
                unused, file_extension = os.path.splitext(form.link_url.data)
                # this url is a link to an image - turn it into a image post
                if file_extension.lower() in allowed_extensions:
                    file = File(source_url=form.link_url.data)
                    post.image = file
                    db.session.add(file)
                    post.type = POST_TYPE_IMAGE
                else:
                    # check opengraph tags on the page and make a thumbnail if an image is available in the og:image meta tag
                    if not post.type == POST_TYPE_VIDEO:
                        opengraph = opengraph_parse(form.link_url.data)
                        if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                            filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                            if not filename.startswith('/'):
                                file = url_to_thumbnail_file(filename)
                                if file:
                                    file.alt_text = shorten_string(opengraph.get('og:title'), 295)
                                    post.image = file
                                    db.session.add(file)

    elif type == POST_TYPE_IMAGE:
        post.type = POST_TYPE_IMAGE
        alt_text = form.image_alt_text.data if form.image_alt_text.data else form.title.data
        uploaded_file = request.files['image_file']
        if uploaded_file and uploaded_file.filename != '':
            if post.image_id:
                remove_old_file(post.image_id)
                post.image_id = None

            # check if this is an allowed type of file
            file_ext = os.path.splitext(uploaded_file.filename)[1]
            if file_ext.lower() not in allowed_extensions:
                abort(400)
            new_filename = gibberish(15)

            # set up the storage directory
            directory = os.path.join(current_app.config["MEDIA_DIR"], 'posts', new_filename[0:2], new_filename[2:4])
            ensure_directory_exists(directory)

            # save the file
            final_place = os.path.join(directory, new_filename + file_ext)
            final_place_medium = os.path.join(directory, new_filename + '_medium.webp')
            final_place_thumbnail = os.path.join(directory, new_filename + '_thumbnail.webp')
            uploaded_file.seek(0)
            uploaded_file.save(final_place)

            if file_ext.lower() == '.heic':
                register_heif_opener()

            Image.MAX_IMAGE_PIXELS = 89478485

            # resize if necessary
            img = Image.open(final_place)
            if '.' + img.format.lower() in allowed_extensions:
                img = ImageOps.exif_transpose(img)
                img_width = img.width
                img_height = img.height
                img.thumbnail((2000, 2000))
                img.save(final_place)
                if img.width > 512 or img.height > 512:
                    img.thumbnail((512, 512))
                    img.save(final_place_medium, format="WebP", quality=93)
                    img_width = img.width
                    img_height = img.height
                # save a second, smaller, version as a thumbnail
                img.thumbnail((170, 170))
                img.save(final_place_thumbnail, format="WebP", quality=93)
                thumbnail_width = img.width
                thumbnail_height = img.height

                file = File(file_path=final_place_medium, file_name=new_filename + file_ext, alt_text=alt_text,
                            width=img_width, height=img_height, thumbnail_width=thumbnail_width,
                            thumbnail_height=thumbnail_height, thumbnail_path=final_place_thumbnail,
                            source_url = url_for('main.serve_media', filename=final_place.removeprefix('app/static/media/'), _external=True))
                db.session.add(file)
                db.session.commit()
                post.image_id = file.id
    elif type == POST_TYPE_VIDEO:
        form.video_url.data = form.video_url.data.strip()
        url_changed = post.id is None or form.video_url.data != post.url
        post.url = remove_tracking_from_link(form.video_url.data.strip())
        post.type = POST_TYPE_VIDEO
        domain = domain_from_url(form.video_url.data)
        domain.post_count += 1
        post.domain = domain

        if url_changed:
            if post.image_id:
                remove_old_file(post.image_id)
                post.image_id = None
            if form.video_url.data.endswith('.mp4') or form.video_url.data.endswith('.webm'):
                file = File(source_url=form.video_url.data)  # make_image_sizes() will take care of turning this into a still image
                post.image = file
                db.session.add(file)
            else:
                # check opengraph tags on the page and make a thumbnail if an image is available in the og:image meta tag
                opengraph = opengraph_parse(form.video_url.data)
                if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                    filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                    if not filename.startswith('/'):
                        file = url_to_thumbnail_file(filename)
                        if file:
                            file.alt_text = shorten_string(opengraph.get('og:title'), 295)
                            post.image = file
                            db.session.add(file)

    elif type == POST_TYPE_POLL:
        post.body = form.title.data + '\n' + form.body.data if post.title not in form.body.data else form.body.data
        post.body_html = markdown_to_html(post.body)
        post.type = POST_TYPE_POLL
    else:
        raise Exception('invalid post type')

    if post.id is None:
        if current_user.reputation > 100:
            post.up_votes = 1
            post.score = 1
        if current_user.reputation < -100:
            post.score = -1
        post.ranking = post_ranking(post.score, utcnow())

        # Filter by phrase
        blocked_phrases_list = blocked_phrases()
        for blocked_phrase in blocked_phrases_list:
            if blocked_phrase in post.title:
                abort(401)
                return
        if post.body:
            for blocked_phrase in blocked_phrases_list:
                if blocked_phrase in post.body:
                    abort(401)
                    return

        db.session.add(post)
    else:
        db.session.execute(text('DELETE FROM "post_tag" WHERE post_id = :post_id'), {'post_id': post.id})
    post.tags = tags_from_string(form.tags.data)
    db.session.commit()

    # Save poll choices. NB this will delete all votes whenever a poll is edited. Partially because it's easier to code but also to stop malicious alterations to polls after people have already voted
    if type == POST_TYPE_POLL:
        db.session.execute(text('DELETE FROM "poll_choice_vote" WHERE post_id = :post_id'), {'post_id': post.id})
        db.session.execute(text('DELETE FROM "poll_choice" WHERE post_id = :post_id'), {'post_id': post.id})
        for i in range(1, 10):
            choice_data = getattr(form, f"choice_{i}").data.strip()
            if choice_data != '':
                db.session.add(PollChoice(post_id=post.id, choice_text=choice_data, sort_order=i))

        poll = Poll.query.filter_by(post_id=post.id).first()
        if poll is None:
            poll = Poll(post_id=post.id)
            db.session.add(poll)
        poll.mode = form.mode.data
        if form.finish_in:
            poll.end_poll = end_poll_date(form.finish_in.data)
        poll.local_only = form.local_only.data
        poll.latest_vote = None
        db.session.commit()

    # Notify author about replies
    # Remove any subscription that currently exists
    existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == post.id,
                                                                  NotificationSubscription.user_id == current_user.id,
                                                                  NotificationSubscription.type == NOTIF_POST).first()
    if existing_notification:
        db.session.delete(existing_notification)

    # Add subscription if necessary
    if form.notify_author.data:
        new_notification = NotificationSubscription(name=post.title, user_id=current_user.id, entity_id=post.id,
                                                    type=NOTIF_POST)
        db.session.add(new_notification)

    g.site.last_active = utcnow()
    db.session.commit()


def end_poll_date(end_choice):
    delta_mapping = {
        '30m': timedelta(minutes=30),
        '1h': timedelta(hours=1),
        '6h': timedelta(hours=6),
        '12h': timedelta(hours=12),
        '1d': timedelta(days=1),
        '3d': timedelta(days=3),
        '7d': timedelta(days=7)
    }

    if end_choice in delta_mapping:
        return datetime.utcnow() + delta_mapping[end_choice]
    else:
        raise ValueError("Invalid choice")


def tags_from_string(tags: str) -> List[Tag]:
    return_value = []
    tags = tags.strip()
    if tags == '':
        return []
    tag_list = tags.split(',')
    tag_list = [tag.strip() for tag in tag_list]
    for tag in tag_list:
        if tag[0] == '#':
            tag = tag[1:]
        tag_to_append = find_hashtag_or_create(tag)
        if tag_to_append:
            return_value.append(tag_to_append)
    return return_value


def delete_post_from_community(post_id):
    if current_app.debug:
        delete_post_from_community_task(post_id)
    else:
        delete_post_from_community_task.delay(post_id)


@celery.task
def delete_post_from_community_task(post_id):
    post = Post.query.get(post_id)
    community = post.community
    post.delete_dependencies()
    post.deleted = True
    db.session.commit()

    if not community.local_only:
        delete_json = {
            'id': f"https://{current_app.config['SERVER_NAME']}/activities/delete/{gibberish(15)}",
            'type': 'Delete',
            'actor': current_user.public_url(),
            'audience': post.community.public_url(),
            'to': [post.community.public_url(), 'https://www.w3.org/ns/activitystreams#Public'],
            'published': ap_datetime(utcnow()),
            'cc': [
                current_user.followers_url()
            ],
            'object': post.ap_id,
        }

        if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
            success = post_request(post.community.ap_inbox_url, delete_json, current_user.private_key,
                                   current_user.public_url() + '#main-key')
        else:  # local community - send it to followers on remote instances
            announce = {
                "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}",
                "type": 'Announce',
                "to": [
                    "https://www.w3.org/ns/activitystreams#Public"
                ],
                "actor": post.community.ap_profile_id,
                "cc": [
                    post.community.ap_followers_url
                ],
                '@context': default_context(),
                'object': delete_json
            }

            for instance in post.community.following_instances():
                if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(
                        instance.domain):
                    send_to_remote_instance(instance.id, post.community.id, announce)


def delete_post_reply_from_community(post_reply_id):
    if current_app.debug:
        delete_post_reply_from_community_task(post_reply_id)
    else:
        delete_post_reply_from_community_task.delay(post_reply_id)


@celery.task
def delete_post_reply_from_community_task(post_reply_id):
    post_reply = PostReply.query.get(post_reply_id)
    post = post_reply.post
    community = post.community
    if post_reply.user_id == current_user.id or community.is_moderator():
        if post_reply.has_replies():
            post_reply.body = 'Deleted by author' if post_reply.author.id == current_user.id else 'Deleted by moderator'
            post_reply.body_html = markdown_to_html(post_reply.body)
        else:
            post_reply.delete_dependencies()
            post_reply.deleted = True
        db.session.commit()

        # federate delete
        if not post.community.local_only:
            delete_json = {
                'id': f"https://{current_app.config['SERVER_NAME']}/activities/delete/{gibberish(15)}",
                'type': 'Delete',
                'actor': current_user.public_url(),
                'audience': post.community.public_url(),
                'to': [post.community.public_url(), 'https://www.w3.org/ns/activitystreams#Public'],
                'published': ap_datetime(utcnow()),
                'cc': [
                    current_user.followers_url()
                ],
                'object': post_reply.ap_id,
            }

            if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
                success = post_request(post.community.ap_inbox_url, delete_json, current_user.private_key,
                                       current_user.public_url() + '#main-key')

            else:  # local community - send it to followers on remote instances
                announce = {
                    "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}",
                    "type": 'Announce',
                    "to": [
                        "https://www.w3.org/ns/activitystreams#Public"
                    ],
                    "actor": post.community.ap_profile_id,
                    "cc": [
                        post.community.ap_followers_url
                    ],
                    '@context': default_context(),
                    'object': delete_json
                }

                for instance in post.community.following_instances():
                    if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(
                            instance.domain):
                        send_to_remote_instance(instance.id, post.community.id, announce)


def remove_old_file(file_id):
    remove_file = File.query.get(file_id)
    remove_file.delete_from_disk()


def save_icon_file(icon_file, directory='communities') -> File:
    # check if this is an allowed type of file
    file_ext = os.path.splitext(icon_file.filename)[1]
    if file_ext.lower() not in allowed_extensions:
        abort(400)
    new_filename = gibberish(15)

    # set up the storage directory
    directory = os.path.join(current_app.config["MEDIA_DIR"], directory, new_filename[0:2], new_filename[2:4])
    ensure_directory_exists(directory)

    # save the file
    final_place = os.path.join(directory, new_filename + file_ext)
    final_place_thumbnail = os.path.join(directory, new_filename + '_thumbnail.webp')
    icon_file.save(final_place)

    if file_ext.lower() == '.heic':
        register_heif_opener()

    # resize if necessary
    Image.MAX_IMAGE_PIXELS = 89478485
    img = Image.open(final_place)
    if '.' + img.format.lower() in allowed_extensions:
        img = ImageOps.exif_transpose(img)
        img_width = img.width
        img_height = img.height
        if img.width > 250 or img.height > 250:
            img.thumbnail((250, 250))
            img.save(final_place)
            img_width = img.width
            img_height = img.height
        # save a second, smaller, version as a thumbnail
        img.thumbnail((40, 40))
        img.save(final_place_thumbnail, format="WebP", quality=93)
        thumbnail_width = img.width
        thumbnail_height = img.height

        file = File(file_path=final_place, file_name=new_filename + file_ext, alt_text=f'{directory} icon',
                    width=img_width, height=img_height, thumbnail_width=thumbnail_width,
                    thumbnail_height=thumbnail_height, thumbnail_path=final_place_thumbnail)
        db.session.add(file)
        return file
    else:
        abort(400)


def save_banner_file(banner_file, directory='communities') -> File:
    # check if this is an allowed type of file
    file_ext = os.path.splitext(banner_file.filename)[1]
    if file_ext.lower() not in allowed_extensions:
        abort(400)
    new_filename = gibberish(15)

    # set up the storage directory
    directory = os.path.join(current_app.config["MEDIA_DIR"], directory, new_filename[0:2], new_filename[2:4])
    ensure_directory_exists(directory)

    # save the file
    final_place = os.path.join(directory, new_filename + file_ext)
    final_place_thumbnail = os.path.join(directory, new_filename + '_thumbnail.webp')
    banner_file.save(final_place)

    if file_ext.lower() == '.heic':
        register_heif_opener()

    # resize if necessary
    Image.MAX_IMAGE_PIXELS = 89478485
    img = Image.open(final_place)
    if '.' + img.format.lower() in allowed_extensions:
        img = ImageOps.exif_transpose(img)
        img_width = img.width
        img_height = img.height
        if img.width > 1600 or img.height > 600:
            img.thumbnail((1600, 600))
            img.save(final_place)
            img_width = img.width
            img_height = img.height

        # save a second, smaller, version as a thumbnail
        img.thumbnail((878, 500))
        img.save(final_place_thumbnail, format="WebP", quality=93)
        thumbnail_width = img.width
        thumbnail_height = img.height

        file = File(file_path=final_place, file_name=new_filename + file_ext, alt_text=f'{directory} banner',
                    width=img_width, height=img_height, thumbnail_path=final_place_thumbnail,
                    thumbnail_width=thumbnail_width, thumbnail_height=thumbnail_height)
        db.session.add(file)
        return file
    else:
        abort(400)


# NB this always signs POSTs as the community so is only suitable for Announce activities
def send_to_remote_instance(instance_id: int, community_id: int, payload):
    if current_app.debug:
        send_to_remote_instance_task(instance_id, community_id, payload)
    else:
        send_to_remote_instance_task.delay(instance_id, community_id, payload)


@celery.task
def send_to_remote_instance_task(instance_id: int, community_id: int, payload):
    community = Community.query.get(community_id)
    if community:
        instance = Instance.query.get(instance_id)
        if instance.inbox:
            if post_request(instance.inbox, payload, community.private_key, community.ap_profile_id + '#main-key'):
                instance.last_successful_send = utcnow()
                instance.failures = 0
            else:
                instance.failures += 1
                instance.most_recent_attempt = utcnow()
                instance.start_trying_again = utcnow() + timedelta(seconds=instance.failures ** 4)
                if instance.failures > 10:
                    instance.dormant = True
            db.session.commit()


def community_in_list(community_id, community_list):
    for tup in community_list:
        if community_id == tup[0]:
            return True
    return False
