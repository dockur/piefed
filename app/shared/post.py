from __future__ import annotations

import os
from typing import List
from zoneinfo import ZoneInfo
from datetime import datetime

import boto3
from PIL import Image, ImageOps
from flask import flash, request, current_app, g
from flask_babel import _, force_locale, gettext
from flask_login import current_user
from pillow_heif import register_heif_opener
from sqlalchemy import text, Integer

from app import db, cache, plugins
from app.activitypub.util import make_image_sizes, notify_about_post
from app.community.util import tags_from_string_old, end_poll_date, flair_from_form, flairs_from_string
from app.constants import *
from app.models import File, Notification, NotificationSubscription, Poll, PollChoice, Post, PostBookmark, PostVote, \
    Report, Site, User, utcnow, Instance, Event, Community
from app.shared.tasks import task_selector
from app.utils import render_template, authorise_api_user, shorten_string, gibberish, ensure_directory_exists, \
    piefed_markdown_to_lemmy_markdown, markdown_to_html, fixup_url, domain_from_url, \
    opengraph_parse, url_to_thumbnail_file, can_create_post, is_video_hosting_site, recently_upvoted_posts, \
    is_image_url, add_to_modlog, store_files_in_s3, guess_mime_type, retrieve_image_hash, \
    hash_matches_blocked_image, can_upvote, can_downvote, get_recipient_language, to_srgb, can_upload_video, \
    is_video_url


def vote_for_post(post_id: int, vote_direction, federate: bool, emoji: str, src, auth=None):
    if src == SRC_API:
        post = db.session.query(Post).get(post_id)
        user = authorise_api_user(auth, return_type='model')
        if vote_direction == 'upvote' and not can_upvote(user, post.community):
            return user.id
        elif vote_direction == 'downvote' and not can_downvote(user, post.community):
            return user.id
    else:
        post = db.session.query(Post).get_or_404(post_id)
        user = current_user

        if (vote_direction == 'upvote' and not can_upvote(user, post.community)) or (
                vote_direction == 'downvote' and not can_downvote(user, post.community)):
            template = 'post/_post_voting_buttons.html' if request.args.get('style',
                                                                            '') == '' else 'post/_post_voting_buttons_masonry.html'
            return render_template(template, post=post, community=post.community, recently_upvoted=[],
                                   recently_downvoted=[])

    undo = post.vote(user, vote_direction, emoji)

    task_selector('vote_for_post', user_id=user.id, post_id=post_id, vote_to_undo=undo, vote_direction=vote_direction, federate=federate, emoji=emoji)

    mark_post_read([post.id], True, user.id)

    if src == SRC_API:
        return user.id
    else:
        recently_upvoted = []
        recently_downvoted = []
        if vote_direction == 'upvote' and undo is None:
            recently_upvoted = [post_id]
        elif vote_direction == 'downvote' and undo is None:
            recently_downvoted = [post_id]

        template = 'post/_post_voting_buttons.html' if request.args.get('style', '') == '' else 'post/_post_voting_buttons_masonry.html'
        return render_template(template, post=post, community=post.community, recently_upvoted=recently_upvoted,
                               recently_downvoted=recently_downvoted)


def bookmark_post(post_id: int, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    mark_post_read([post_id], True, user_id)

    existing_bookmark = PostBookmark.query.filter_by(post_id=post_id, user_id=user_id).first()
    if not existing_bookmark:
        db.session.add(PostBookmark(post_id=post_id, user_id=user_id))
        db.session.commit()
    else:
        msg = 'This post has already been bookmarked.'
        if src == SRC_API:
            raise Exception(msg)
        else:
            flash(_(msg))

    if src == SRC_API:
        return user_id


def remove_bookmark_post(post_id: int, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    existing_bookmark = PostBookmark.query.filter_by(post_id=post_id, user_id=user_id).first()
    if existing_bookmark:
        db.session.delete(existing_bookmark)
        db.session.commit()
    else:
        msg = 'This post was not bookmarked.'
        if src == SRC_API:
            raise Exception(msg)
        else:
            flash(_(msg))

    if src == SRC_API:
        return user_id


def subscribe_post(post_id: int, subscribe, src, auth=None):
    post = db.session.query(Post).filter_by(id=post_id, deleted=False).one()
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    if src == SRC_WEB:
        subscribe = False if post.notify_new_replies(user_id) else True

    existing_notification = NotificationSubscription.query.filter_by(entity_id=post_id, user_id=user_id,
                                                                     type=NOTIF_POST).first()
    if subscribe == False:
        if existing_notification:
            db.session.delete(existing_notification)
            db.session.commit()
        else:
            msg = 'A subscription for this post did not exist.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))

    else:
        if existing_notification:
            msg = 'A subscription for this post already existed.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))
        else:
            new_notification = NotificationSubscription(
                name=shorten_string(_('Replies to my post %(post_title)s', post_title=post.title)),
                user_id=user_id, entity_id=post_id, type=NOTIF_POST)
            db.session.add(new_notification)
            db.session.commit()

    if src == SRC_API:
        return user_id
    else:
        return render_template('post/_post_notification_toggle.html', post=post)


def extra_rate_limit_check(user):
    """
    The plan for this function is to do some extra limiting for an author who passes the rate limit for the route
    but who's posts are really unpopular and are probably spam
    """
    return False


def make_post(input, community, type, src, auth=None, uploaded_file=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
        if extra_rate_limit_check(user):
            raise Exception('rate_limited')
        title = input['title']
        url = input['url']
        language_id = input['language_id']
    else:
        user = current_user
        title = input.title.data.strip()
        if type == POST_TYPE_LINK:
            url = input.link_url.data.strip()
        elif type == POST_TYPE_VIDEO:
            url = input.video_url.data.strip()
        else:
            url = None
        language_id = input.language_id.data

    # taking values from one JSON to put in another JSON to put in a DB to put in another JSON feels bad
    # instead, make_post shares code with edit_post
    # ideally, a similar change could be made for incoming activitypub (create_post() and update_post_from_activity() could share code)
    # once this happens, and post.new() just does the minimum before being passed off to an update function, post.new() can be used here again.

    if not can_create_post(user, community):
        raise Exception('You are not permitted to make posts in this community')

    if url:
        url = url.strip()
        domain = domain_from_url(url)
        if domain:
            if domain.banned or domain.name.endswith('.pages.dev'):
                raise Exception(domain.name + ' is blocked by admin')

    if uploaded_file and uploaded_file.filename != '':
        # check if this is an allowed type of file
        allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.mpo', '.avif', '.svg']
        if type == POST_TYPE_VIDEO and can_upload_video():
            allowed_extensions.extend(['.mp4', '.webm', '.mov'])
        file_ext = os.path.splitext(uploaded_file.filename)[1]
        if file_ext.lower() not in allowed_extensions:
            raise Exception('filetype not allowed')

    post = Post(user_id=user.id, community_id=community.id, instance_id=user.instance_id, from_bot=user.bot or user.bot_override,
                posted_at=utcnow(), ap_id=gibberish(), title=title, language_id=language_id)
    db.session.add(post)
    db.session.commit()

    post.up_votes = 1
    effect = 1.0
    post.score = post.up_votes * effect
    post.ranking = post.post_ranking(post.score, post.posted_at)
    post.ranking_scaled = int(post.ranking + community.scale_by())
    cache.delete_memoized(recently_upvoted_posts, user.id)

    community.post_count += 1
    community.last_active = g.site.last_active = utcnow()
    user.post_count += 1
    user.last_seen = utcnow()

    post.generate_ap_id(community)

    vote = PostVote(user_id=user.id, post_id=post.id, author_id=user.id, effect=1)
    db.session.add(vote)
    db.session.commit()

    try:    # federation is done in edit_post
        post = edit_post(input, post, type, src, user, auth, uploaded_file, from_scratch=True)
    except Exception as e:
        db.session.delete(vote)
        db.session.delete(post)
        db.session.commit()
        raise e

    if post.status == POST_STATUS_PUBLISHED:
        notify_about_post(post)

    plugins.fire_hook('after_post_create', post)

    if src == SRC_API:
        return user.id, post
    else:
        return post


# 'from_scratch == True' means that it's not really a user edit, we're just re-using code for make_post()
def edit_post(input, post: Post, type, src, user=None, auth=None, uploaded_file=None, from_scratch=False, hash=None):
    if src == SRC_API:
        if not user:
           user = authorise_api_user(auth, return_type='model', id_match=post.user_id)
        title = input['title'].strip()
        body = input['body']
        url = input['url']
        nsfw = input['nsfw']
        ai_generated = input['ai_generated']
        notify_author = input['notify_author']
        language_id = input['language_id']
        timezone = input['timezone'] if 'timezone' in input else user.timezone
        image_alt_text = input['image_alt_text'] if 'image_alt_text' in input else ''
        if image_alt_text is None:
            image_alt_text = ''
        if 'tags' in input:
            tags = tags_from_string_old(input['tags'])
        else:
            tags = []
        if 'flair' in input:
            flair = flairs_from_string(input['flair'], post.community_id)
        else:
            flair = []
        scheduled_for = None
        repeat = None

        # Parse event data from API
        event_data = input.get('event', None)
        if event_data:
            # Parse datetime strings to datetime objects
            if 'start' in event_data:
                if isinstance(event_data['start'], str):
                    event_data['start'] = datetime.fromisoformat(event_data['start'].replace('Z', '+00:00')).replace(tzinfo=None)
                else:
                    event_data['start'] = event_data['start']
            if 'end' in event_data and event_data['end']:
                if isinstance(event_data['end'], str):
                    event_data['end'] = datetime.fromisoformat(event_data['end'].replace('Z', '+00:00')).replace(tzinfo=None)
                else:
                    event_data['end'] = event_data['end']

        # Parse poll data from API
        poll_data = input.get('poll', None)
        if poll_data:
            # Extract all poll fields
            parsed_poll = {
                'mode': poll_data.get('mode', 'single'),
                'local_only': poll_data.get('local_only', False),
                'choices': poll_data.get('choices', [])
            }
            if 'end_poll' in poll_data and poll_data['end_poll']:
                if isinstance(poll_data['end_poll'], str):
                    parsed_poll['end_poll'] = datetime.fromisoformat(poll_data['end_poll'].replace('Z', '+00:00'))
                else:
                    parsed_poll['end_poll'] = poll_data['end_poll']

            poll_data = parsed_poll
    else:
        if not user:
            user = current_user
        title = input.title.data.strip()
        body = input.body.data
        if type == POST_TYPE_LINK:
            url = input.link_url.data.strip()
        elif type == POST_TYPE_VIDEO:
            url = input.video_url.data.strip()
        elif type == POST_TYPE_IMAGE and not from_scratch:
            url = post.url
        else:
            url = None
        nsfw = input.nsfw.data
        ai_generated = input.ai_generated.data
        notify_author = input.notify_author.data
        language_id = input.language_id.data
        tags = tags_from_string_old(input.tags.data)
        if input.flair:
            flair = flair_from_form(input.flair.data)
        else:
            flair = []
        scheduled_for = input.scheduled_for.data
        repeat = input.repeat.data
        timezone = input.timezone.data
        image_alt_text = input.image_alt_text.data if hasattr(input, 'image_alt_text') and input.image_alt_text else ''

        # Extract poll data from form
        if type == POST_TYPE_POLL:
            poll_choices = []
            for i in range(1, 16):
                choice_text = getattr(input, f"choice_{i}").data
                if choice_text:
                    poll_choices.append({'choice_text': choice_text.strip(), 'sort_order': i})
            poll_data = {
                'mode': input.mode.data,
                'local_only': input.local_only.data,
                'choices': poll_choices
            }
            if input.finish_in:
                poll_data['end_poll'] = end_poll_date(input.finish_in.data)
        else:
            poll_data = None

        # Extract event data from form
        if type == POST_TYPE_EVENT:
            local_tz = ZoneInfo(input.event_timezone.data)
            local_start = input.start_datetime.data.replace(tzinfo=local_tz)
            local_end = input.end_datetime.data.replace(tzinfo=local_tz)
            utc_start = local_start.astimezone(ZoneInfo('UTC'))
            utc_end = local_end.astimezone(ZoneInfo('UTC'))

            event_data = {
                'start': utc_start.replace(tzinfo=None),
                'end': utc_end.replace(tzinfo=None),
                'timezone': input.event_timezone.data,
                'max_attendees': input.max_attendees.data,
                'online': input.online.data,
                'online_link': input.online_link.data,
                'join_mode': input.join_mode.data,
                'location': {
                    'address': input.irl_address.data,
                    'city': input.irl_city.data,
                    'country': input.irl_country.data
                }
            }
        else:
            event_data = None

    # WARNING: beyond this point do not use the input variable as it can be either a dict or a form object!

    post.indexable = user.indexable
    if post.community.is_moderator(user) or post.community.is_owner(user) or user.is_admin():
        post.sticky = False if src == SRC_API else input.sticky.data
    post.nsfw = nsfw
    post.nsfl = False if src == SRC_API else input.nsfl.data
    post.ai_generated = ai_generated
    post.notify_author = notify_author
    post.language_id = language_id
    user.language_id = language_id
    post.title = title
    post.body = piefed_markdown_to_lemmy_markdown(body)
    post.body_html = markdown_to_html(post.body)
    post.type = type
    post.scheduled_for = scheduled_for
    post.repeat = repeat
    post.timezone = timezone

    if post.url:
        if post.url.startswith('https://pixelfed.social/') or post.url.startswith('https://pixelfed.uno/'):
            post.type = POST_TYPE_IMAGE
        elif post.url.startswith('https://loops.video/'):
            post.type = POST_TYPE_VIDEO
        elif is_video_url(post.url):
            post.type = POST_TYPE_VIDEO
        elif is_image_url(post.url):
            post.type = POST_TYPE_IMAGE

    if scheduled_for:
        date_with_tz = post.scheduled_for.replace(tzinfo=ZoneInfo(post.timezone))
        if date_with_tz.astimezone(ZoneInfo('UTC')) > utcnow(naive=False):
            post.status = POST_STATUS_SCHEDULED
    
    url_changed = False
    hash = None

    if not from_scratch:
        # Remove any subscription that currently exists
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == post.id,
                                                                      NotificationSubscription.user_id == user.id,
                                                                      NotificationSubscription.type == NOTIF_POST).first()
        if existing_notification:
            db.session.delete(existing_notification)

        # Remove any poll votes that currently exists
        # Partially because it's easier to code but also to stop malicious alterations to polls after people have already voted
        db.session.execute(text('DELETE FROM "poll_choice_vote" WHERE post_id = :post_id'), {'post_id': post.id})
        db.session.execute(text('DELETE FROM "poll_choice" WHERE post_id = :post_id'), {'post_id': post.id})

        # Remove any old images, and set url_changed
        if url != post.url or uploaded_file:
            url_changed = True
            if post.image_id:
                remove_file = File.query.get(post.image_id)
                if remove_file:
                    remove_file.delete_from_disk()
                post.image_id = None
            if post.url:
                domain = domain_from_url(post.url)
                if domain:
                    domain.post_count -= 1
                if post.type == POST_TYPE_VIDEO and store_files_in_s3() and post.url.startswith(f'https://{current_app.config["S3_PUBLIC_URL"]}'):
                    from app.shared.tasks.maintenance import delete_from_s3
                    if current_app.debug:
                        delete_from_s3([post.url])
                    else:
                        delete_from_s3.delay([post.url])

        # remove any old tags
        post.tags.clear()
        post.flair.clear()

        post.edited_at = utcnow()

        db.session.commit()

    if uploaded_file and uploaded_file.filename != '':
        # check if this is an allowed type of file
        allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.mpo', '.avif', '.svg']
        if type == POST_TYPE_VIDEO and can_upload_video():
            allowed_extensions.extend(['.mp4', '.webm', '.mov'])
        file_ext = os.path.splitext(uploaded_file.filename)[1]
        if file_ext.lower() not in allowed_extensions:
            raise Exception('filetype not allowed')

        new_filename = gibberish(15)
        # set up the storage directory
        if store_files_in_s3():
            directory = 'app/static/tmp'
        else:
            directory = 'app/static/media/posts/' + new_filename[0:2] + '/' + new_filename[2:4]
        ensure_directory_exists(directory)

        # save the file
        final_place = os.path.join(directory, new_filename + file_ext.lower())
        uploaded_file.seek(0)
        uploaded_file.save(final_place)

        final_ext = file_ext.lower()  # track file extension for conversion

        if final_ext == '.heic':
            register_heif_opener()
        if final_ext == '.avif':
            import pillow_avif  # NOQA  # do not remove

        Image.MAX_IMAGE_PIXELS = 89478485

        # Use environment variables to determine image max dimension, format, and quality
        image_max_dimension = current_app.config['MEDIA_IMAGE_MAX_DIMENSION']
        image_format = current_app.config['MEDIA_IMAGE_FORMAT']
        image_quality = current_app.config['MEDIA_IMAGE_QUALITY']

        if image_format == 'AVIF':
            import pillow_avif  # NOQA  # do not remove

        if not final_place.endswith('.svg') and not final_place.endswith('.gif') and not is_video_url(final_place):
            img = Image.open(final_place)
            if '.' + img.format.lower() in allowed_extensions:
                img = ImageOps.exif_transpose(img)
                if (image_format == 'JPEG' or final_ext in ['.jpg', '.jpeg']):
                    img = to_srgb(img)
                else:
                    img = img.convert('RGBA')
                img.thumbnail((image_max_dimension, image_max_dimension), resample=Image.LANCZOS)

                kwargs = {}
                if image_format:
                    kwargs['format'] = image_format.upper()
                    final_ext = '.' + image_format.lower()
                    final_place = os.path.splitext(final_place)[0] + final_ext
                if image_quality:
                    kwargs['quality'] = int(image_quality)

                img.save(final_place, optimize=True, **kwargs)
            else:
                raise Exception('filetype not allowed')

        url = f"{current_app.config['SERVER_URL']}/{final_place.replace('app/', '')}"

        if current_app.config['IMAGE_HASHING_ENDPOINT'] and not is_video_url(final_place):
            hash = retrieve_image_hash(url)
            if hash and hash_matches_blocked_image(hash):
                raise Exception('This image is blocked')

        # Move uploaded file to S3
        if store_files_in_s3():
            session = boto3.session.Session()
            extra_args = {'ContentType': guess_mime_type(final_place)}
            if current_app.config.get('S3_STORAGE_CLASS'):
                extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
            s3 = session.client(
                service_name='s3',
                region_name=current_app.config['S3_REGION'],
                endpoint_url=current_app.config['S3_ENDPOINT'],
                aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
            )
            s3.upload_file(final_place, current_app.config['S3_BUCKET'], 'posts/' +
                           new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext,
                           ExtraArgs=extra_args)
            url = f"https://{current_app.config['S3_PUBLIC_URL']}/posts/" + \
                  new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext
            s3.close()
            os.unlink(final_place)

    if url and (from_scratch or url_changed):
        domain = domain_from_url(url)
        if domain:
            if domain.banned or domain.name.endswith('.pages.dev'):
                raise Exception(domain.name + ' is blocked by admin')
            post.domain = domain
            domain.post_count += 1
            already_notified = set()  # often admins and mods are the same people - avoid notifying them twice
            targets_data = {'gen': '0',
                            'post_id': post.id,
                            'orig_post_title': post.title,
                            'orig_post_body': post.body,
                            'orig_post_domain': post.domain,
                            'author_user_name': user.ap_id if user.ap_id else user.user_name
                            }
            if domain.notify_mods:
                for community_member in post.community.moderators():
                    if community_member.is_local():
                        notify = Notification(title='Suspicious content', url=post.ap_id,
                                              user_id=community_member.user_id, author_id=user.id,
                                              notif_type=NOTIF_REPORT,
                                              subtype='post_from_suspicious_domain',
                                              targets=targets_data)
                        db.session.add(notify)
                        already_notified.add(community_member.user_id)
            if domain.notify_admins:
                for admin in Site.admins():
                    if admin.id not in already_notified:
                        notify = Notification(title='Suspicious content', url=post.ap_id,
                                              user_id=admin.id, author_id=user.id,
                                              notif_type=NOTIF_REPORT,
                                              subtype='post_from_suspicious_domain',
                                              targets=targets_data)
                        db.session.add(notify)

        thumbnail_url, embed_url = fixup_url(url)
        if is_image_url(url):
            file = File(source_url=url, hash=hash)
            if (uploaded_file and type == POST_TYPE_IMAGE) or type == POST_TYPE_LINK:
                # change this line when uploaded_file is supported in API
                file.alt_text = image_alt_text
            db.session.add(file)
            db.session.commit()
            post.image_id = file.id

            
            # For events, uploaded images are banners - don't change post type or URL
            if type == POST_TYPE_EVENT:
                post.url = None  # Events don't have URLs when they have banner images
                make_image_sizes(post.image_id, 170, 2000, 'posts', post.community.low_quality)
            else:
                make_image_sizes(post.image_id, 512, 1200, 'posts', post.community.low_quality)
                post.type = POST_TYPE_IMAGE
                post.url = url
        elif url.startswith('https://pixelfed.social') or url.startswith('pixelfed.uno'):
            post.type = POST_TYPE_IMAGE
            opengraph = opengraph_parse(thumbnail_url)
            if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                if not filename.startswith('/'):
                    file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                    post.image = file
                    db.session.add(file)
            post.url = url
            post.body += '\n\nSource: '
        elif url.startswith('https://loops.video'):
            post.type = POST_TYPE_VIDEO
            opengraph = opengraph_parse(thumbnail_url)
            if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                if not filename.startswith('/'):
                    filename = filename.replace('.jpg', '.720p.mp4')
                    file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                    post.image = file
                    db.session.add(file)
            post.url = url
        else:
            opengraph = opengraph_parse(thumbnail_url)
            if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                if not filename.startswith('/'):
                    file = url_to_thumbnail_file(filename)
                    if file:
                        file.alt_text = shorten_string(opengraph.get('og:title'), 295)
                        post.image = file
                        db.session.add(file)

            post.url = embed_url

            if is_video_url(url) or url.endswith('.mp4') or url.endswith('.webm') or is_video_hosting_site(embed_url):
                post.type = POST_TYPE_VIDEO
            else:
                post.type = POST_TYPE_LINK

        post.calculate_cross_posts(url_changed=url_changed)
    elif url and is_video_hosting_site(url):
        post.type = POST_TYPE_VIDEO
    
    if url and post.image:
        file = File.query.get(post.image_id)
        if file:
            file.alt_text = image_alt_text

    federate = True
    if type == POST_TYPE_POLL and poll_data:
        post.type = POST_TYPE_POLL

        # Add poll choices
        if 'choices' in poll_data:
            for choice in poll_data['choices']:
                if 'choice_text' in choice and choice['choice_text'].strip():
                    db.session.add(PollChoice(
                        post_id=post.id,
                        choice_text=choice['choice_text'].strip(),
                        sort_order=choice.get('sort_order', 1)
                    ))

        poll = Poll.query.filter_by(post_id=post.id).first()
        if poll is None:
            poll = Poll(post_id=post.id)
            db.session.add(poll)
        poll.mode = poll_data.get('mode', 'single')
        if 'end_poll' in poll_data and poll_data['end_poll']:
            poll.end_poll = poll_data['end_poll']
        poll.local_only = poll_data.get('local_only', False)
        poll.latest_vote = None
        db.session.commit()

        if poll.local_only:
            federate = False

    if type == POST_TYPE_EVENT and event_data:
        post.type = POST_TYPE_EVENT
        event = Event.query.filter_by(post_id=post.id).first()
        if event is None:
            event = Event(post_id=post.id)
            db.session.add(event)

        if 'start' in event_data:
            event.start = event_data['start']
        if 'end' in event_data and event_data['end']:
            event.end = event_data['end']

        event.timezone = event_data.get('timezone', 'UTC')
        event.max_attendees = event_data.get('max_attendees', 0)
        event.participant_count = event_data.get('participant_count', 0)
        event.full = event_data.get('full', False)
        event.online = event_data.get('online', False)
        event.online_link = event_data.get('online_link')
        event.join_mode = event_data.get('join_mode', 'free')
        event.external_participation_url = event_data.get('external_participation_url')
        event.anonymous_participation = event_data.get('anonymous_participation', False)
        event.buy_tickets_link = event_data.get('buy_tickets_link')
        event.event_fee_currency = event_data.get('event_fee_currency')
        event.event_fee_amount = event_data.get('event_fee_amount', 0)
        if 'location' in event_data:
            event.location = event_data['location']
        db.session.commit()

    # add tags & flair
    post.tags = tags
    post.flair = flair

    # Add subscription if necessary
    if notify_author:
        new_notification = NotificationSubscription(name=post.title, user_id=user.id, entity_id=post.id,
                                                    type=NOTIF_POST)
        db.session.add(new_notification)

    db.session.commit()

    if post.status < POST_STATUS_PUBLISHED:
        federate = False

    if from_scratch:
        if federate:
            task_selector('make_post', post_id=post.id)
    elif federate:
        task_selector('edit_post', post_id=post.id)

    if src == SRC_API:
        if from_scratch:
            return post
        else:
            return user.id, post
    elif from_scratch:
        return post


# just for deletes by owner (mod deletes are classed as 'remove')
def delete_post(post_id: int, federate_deletion, src, auth):
    if src == SRC_API:
        post = db.session.query(Post).get(post_id)
        user_id = authorise_api_user(auth, id_match=post.user_id)
    else:
        if current_user:
            user_id = current_user.id
        else:
            user_id = 1     # for remove_old_community_content()

    from app import redis_client
    with redis_client.lock(f"lock:post:{post_id}", timeout=10, blocking_timeout=6):
        post = db.session.query(Post).get(post_id)
        if post.url:
            post.calculate_cross_posts(delete_only=True)

        post.deleted = True
        post.deleted_by = user_id
        post.author.post_count -= 1
        post.community.post_count -= 1
        db.session.commit()

    if federate_deletion and post.status == POST_STATUS_PUBLISHED:
        task_selector('delete_post', user_id=user_id, post_id=post.id)

    # remove any notifications about the post
    notifs = db.session.query(Notification).filter(Notification.targets.op("->>")("post_id").cast(Integer) == post.id)
    for notif in notifs:
        # dont delete report notifs
        if notif.notif_type == NOTIF_REPORT or notif.notif_type == NOTIF_REPORT_ESCALATION:
            continue
        db.session.delete(notif)
    db.session.commit()

    if src == SRC_API:
        return user_id, post
    else:
        return


def restore_post(post_id: int, src, auth):
    if src == SRC_API:
        post = db.session.query(Post).get(post_id)
        user_id = authorise_api_user(auth, id_match=post.user_id)
    else:
        user_id = current_user.id

    post = db.session.query(Post).get(post_id)
    if post.url:
        post.calculate_cross_posts()

    post.deleted = False
    post.deleted_by = None
    post.author.post_count += 1
    post.community.post_count += 1
    db.session.commit()

    task_selector('restore_post', user_id=user_id, post_id=post.id)

    if src == SRC_API:
        return user_id, post
    else:
        return


def report_post(post: Post, input, src, auth=None):
    if src == SRC_API:
        reporter_user = authorise_api_user(auth, return_type='model')
        suspect_user = User.query.filter_by(id=post.user_id).one()
        source_instance = Instance.query.filter_by(id=post.instance_id).one()
        reason = input['reason']
        description = input['description']
        notify_admins = (any(x in reason.lower() for x in ['Minor abuse', 'doxing']) or
                        any(x in description.lower() for x in ['Minor abuse', 'doxing']) or
                         (reason == 'AI content that needs flair' and post.community.instance.software.lower() != 'piefed'))
        report_remote = input['report_remote']
    else:
        reporter_user = current_user
        suspect_user = User.query.get(post.user_id)
        source_instance = Instance.query.get(suspect_user.instance_id)
        reason = input.reasons_to_string(input.reasons.data)
        description = input.description.data
        notify_admins = ('5' in input.reasons.data or '6' in input.reasons.data or ('17' in input.reasons.data and post.community.instance.software.lower() != 'piefed'))
        report_remote = input.report_remote.data

    targets_data = {
        'gen': '0',
        'suspect_post_id': post.id,
        'suspect_user_id': post.user_id,
        'suspect_user_user_name': suspect_user.ap_id if suspect_user.ap_id else suspect_user.user_name,
        'source_instance_id': source_instance.id,
        'source_instance_domain': source_instance.domain,
        'reporter_id': reporter_user.id,
        'reporter_user_name': reporter_user.user_name,
        'orig_post_title': post.title,
        'orig_post_body': post.body
    }
    # report.type 1 = 'post'
    report = Report(reasons=reason[:255], description=description[:255], type=1, reporter_id=reporter_user.id, suspect_post_id=post.id,
                    suspect_community_id=post.community_id,
                    suspect_user_id=post.user_id, in_community_id=post.community_id, source_instance_id=reporter_user.instance_id,
                    targets=targets_data)
    db.session.add(report)

    # Notify local moderators, and send Flag to remote moderators
    # if user has not selected 'report_remote', just send to remote mods not on community's or suspect_users's instances
    already_notified = set()
    remote_instance_ids = set()
    for mod in post.community.moderators():
        moderator = User.query.get(mod.user_id)
        if moderator:
            if moderator.is_local():
                with force_locale(get_recipient_language(moderator.id)):
                    notification = Notification(user_id=mod.user_id, title=gettext('A post has been reported'),
                                                url=f"{current_app.config['SERVER_URL']}/post/{post.id}",
                                                author_id=reporter_user.id, notif_type=NOTIF_REPORT,
                                                subtype='post_reported',
                                                targets=targets_data)
                    db.session.add(notification)
                    already_notified.add(mod.user_id)
            else:
                if not report_remote:
                    if moderator.instance_id != suspect_user.instance_id and moderator.instance_id != post.community.instance_id:
                        remote_instance_ids.add(moderator.instance_id)
                else:
                    remote_instance_ids.add(moderator.instance_id)

    if notify_admins:
        for admin in Site.admins():
            if admin.id not in already_notified:
                notify = Notification(title='Suspicious content', url='/admin/reports', user_id=admin.id,
                                      author_id=reporter_user.id, notif_type=NOTIF_REPORT,
                                      subtype='post_reported',
                                      targets=targets_data)
                db.session.add(notify)
                admin.unread_notifications += 1
    else:
        print('no notify_admins', notify_admins)

    # Lemmy doesn't process or generate Announce / Flag, so Flags also have to be sent from here to user's and community's instances
    if report_remote:
        if not post.community.is_local():
            if post.community_id not in remote_instance_ids: # very unlikely, since it will typically have mods on same instance.
                remote_instance_ids.add(post.community.instance_id)
        if not suspect_user.is_local():
            if suspect_user.instance_id not in remote_instance_ids:
                remote_instance_ids.add(suspect_user.instance_id)

    post.reports += 1
    db.session.commit()

    if remote_instance_ids:
        summary = reason
        if description:
            summary += ' - ' + description

        task_selector('report_post', user_id=reporter_user.id, post_id=post.id, summary=summary, instance_ids=list(remote_instance_ids))

    if src == SRC_API:
        return reporter_user.id, report
    else:
        return


def lock_post(post_id: int, locked, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = db.session.query(Post).get(post_id)
    if locked:
        comments_enabled = False
        modlog_type = 'lock_post'
    else:
        comments_enabled = True
        modlog_type = 'unlock_post'

    if post.community.is_moderator(user) or post.community.is_admin_or_staff(user):
        post.comments_enabled = comments_enabled
        db.session.commit()
        add_to_modlog(modlog_type, actor=user, target_user=post.author, reason='',
                      community=post.community, post=post,
                      link_text=shorten_string(post.title), link=f'post/{post.id}')

        if locked:
            if src == SRC_WEB:
                flash(_('%(name)s has been locked.', name=post.title))
            task_selector('lock_post', user_id=user.id, post_id=post_id)
        else:
            if src == SRC_WEB:
                flash(_('%(name)s has been unlocked.', name=post.title))
            task_selector('unlock_post', user_id=user.id, post_id=post_id)

    if src == SRC_API:
        return user.id, post


def move_post(post_id: int, target_id: int, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = db.session.query(Post).get(post_id)

    if post.community.is_moderator(user) or post.community.is_instance_admin(user) or user.is_admin_or_staff():
        old_community_id = post.community_id
        target_community = db.session.query(Community).get(target_id)

        post.move_to(target_community)
        db.session.commit()

        add_to_modlog('move_post', actor=user, target_user=post.author, reason='',
                      community=target_community, post=post,
                      link_text=shorten_string(post.title), link=f'post/{post.id}')

        if src == SRC_WEB:
            flash(_('%(name)s has been moved.', name=post.title))

        task_selector('move_post', user_id=user.id, old_community_id=old_community_id,
                      new_community_id=target_community.id, post_id=post_id)

    if src == SRC_API:
        return user.id, post


def sticky_post(post_id: int, featured: bool, src: int, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = db.session.query(Post).get(post_id)
    community = post.community

    if post.community.is_moderator(user) or post.community.is_instance_admin(user) or user.is_admin_or_staff():
        post.sticky = featured
        if featured:
            modlog_type = 'featured_post'
        else:
            modlog_type = 'unfeatured_post'
        if not community.ap_featured_url:
            community.ap_featured_url = community.ap_profile_id + '/featured'
        db.session.commit()
        add_to_modlog(modlog_type, actor=user, target_user=post.author, reason='',
                      community=post.community, post=post,
                      link_text=shorten_string(post.title), link=f'post/{post.id}')

    if featured:
        task_selector('sticky_post', user_id=user.id, post_id=post_id)
    else:
        task_selector('unsticky_post', user_id=user.id, post_id=post_id)

    return user.id, post


def hide_post(post_id: int, hidden: bool, src: int, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = db.session.query(Post).get(post_id)

    if hidden:
        user.mark_post_as_hidden(post)
    else:
        db.session.execute(text('DELETE FROM "hidden_posts" WHERE user_id = :user_id AND hidden_post_id = :post_id'),
                           {'user_id': user.id, 'post_id': post.id})
    db.session.commit()

    return user.id, post


# mod deletes
def mod_remove_post(post_id: int, reason, src, auth):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    from app import redis_client
    with redis_client.lock(f"lock:post:{post_id}", timeout=10, blocking_timeout=6):
        post = db.session.query(Post).get(post_id)

        if not post.community.is_moderator(user) and not user.is_admin_or_staff():
            raise Exception('Does not have permission')

        if post.url:
            post.calculate_cross_posts(delete_only=True)

        post.deleted = True
        post.deleted_by = user.id
        post.author.post_count -= 1
        post.community.post_count -= 1
        db.session.commit()

    add_to_modlog('delete_post', actor=user, target_user=post.author, reason=reason,
                  community=post.community, post=post,
                  link_text=shorten_string(post.title), link=f'post/{post.id}')

    task_selector('delete_post', user_id=user.id, post_id=post.id, reason=reason)

    # remove any notifications about the post
    notifs = db.session.query(Notification).filter(Notification.targets.op("->>")("post_id").cast(Integer) == post.id)
    for notif in notifs:
        # dont delete report notifs
        if notif.notif_type == NOTIF_REPORT or notif.notif_type == NOTIF_REPORT_ESCALATION:
            continue
        db.session.delete(notif)
    db.session.commit()

    if src == SRC_API:
        return user.id, post
    else:
        return


def mod_restore_post(post_id: int, reason, src, auth):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    from app import redis_client
    with redis_client.lock(f"lock:post:{post_id}", timeout=10, blocking_timeout=6):
        post = db.session.query(Post).get(post_id)
        if not post.community.is_moderator(user) and not user.is_admin_or_staff():
            raise Exception('Does not have permission')

        if post.url:
            post.calculate_cross_posts()

        post.deleted = False
        post.deleted_by = None
        post.author.post_count += 1
        post.community.post_count += 1
        db.session.commit()

    add_to_modlog('restore_post', actor=user, target_user=post.author, reason=reason,
                  community=post.community, post=post,
                  link_text=shorten_string(post.title), link=f'post/{post.id}')

    task_selector('restore_post', user_id=user.id, post_id=post.id, reason=reason)

    if src == SRC_API:
        return user.id, post
    else:
        return


def mark_post_read(post_ids: List[int], read: bool, user_id: int):
    if read is True:
        for post_id in post_ids:
            db.session.execute(text(
                'INSERT INTO "read_posts" (user_id, read_post_id, interacted_at) VALUES (:user_id, :post_id, :stamp) ON CONFLICT (user_id, read_post_id) DO UPDATE SET interacted_at = EXCLUDED.interacted_at'),
                {"user_id": user_id, "post_id": post_id, "stamp": utcnow()})
        db.session.commit()
    else:
        for post_id in post_ids:
            db.session.execute(
                text('DELETE FROM "read_posts" WHERE user_id = :user_id AND read_post_id = :post_id'),
                {"user_id": user_id, "post_id": post_id})
        db.session.commit()


def get_post_flair_list(post: Post | int) -> list:
    if isinstance(post, int):
        post = db.session.query(Post).filter_by(id=post).one()
    
    if not post.flair:
        # In case flair is null
        flair_list = []
    else:
        flair_list = post.flair
    
    return flair_list


def vote_for_poll(post_id, votes, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user
    
    if isinstance(votes, int):
        votes = [votes]

    poll = Poll.query.get_or_404(post_id)
    if poll.mode == 'single':
        if len(votes) != 1:
            if src == SRC_API:
                raise Exception("Poll is in single vote mode, only a single choice is allowed.")
        if not poll.has_voted(user.id):
            poll.vote_for_choice(votes[0], user.id)
            task_selector('vote_for_poll', post_id=post_id, user_id=user.id,
                        choice_text=PollChoice.query.get(votes[0]).choice_text)
        else:
            if src == SRC_API:
                raise Exception("User has already voted.")
    else:
        for choice_id in votes:
            poll.vote_for_choice(int(choice_id), user.id)
            task_selector('vote_for_poll', post_id=post_id, user_id=user.id,
                          choice_text=PollChoice.query.get(int(choice_id)).choice_text)
