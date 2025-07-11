import os
from zoneinfo import ZoneInfo

import boto3
from PIL import Image, ImageOps
from flask import flash, request, current_app, g
from flask_babel import _, force_locale, gettext
from flask_login import current_user
from pillow_heif import register_heif_opener
from sqlalchemy import text

from app import db, cache
from app.activitypub.util import make_image_sizes, notify_about_post
from app.community.util import tags_from_string_old, end_poll_date, flair_from_form
from app.constants import *
from app.models import File, Notification, NotificationSubscription, Poll, PollChoice, Post, PostBookmark, PostVote, \
    Report, Site, User, utcnow
from app.shared.tasks import task_selector
from app.utils import render_template, authorise_api_user, shorten_string, gibberish, ensure_directory_exists, \
    piefed_markdown_to_lemmy_markdown, markdown_to_html, fixup_url, domain_from_url, \
    opengraph_parse, url_to_thumbnail_file, can_create_post, is_video_hosting_site, recently_upvoted_posts, \
    is_image_url, add_to_modlog, store_files_in_s3, guess_mime_type, retrieve_image_hash, \
    hash_matches_blocked_image, can_upvote, can_downvote, get_recipient_language


def vote_for_post(post_id: int, vote_direction, federate: bool, src, auth=None):
    if src == SRC_API:
        post = Post.query.filter_by(id=post_id).one()
        user = authorise_api_user(auth, return_type='model')
        if vote_direction == 'upvote' and not can_upvote(user, post.community):
            return user.id
        elif vote_direction == 'downvote' and not can_downvote(user, post.community):
            return user.id
    else:
        post = Post.query.get_or_404(post_id)
        user = current_user

        if (vote_direction == 'upvote' and not can_upvote(user, post.community)) or (
                vote_direction == 'downvote' and not can_downvote(user, post.community)):
            template = 'post/_post_voting_buttons.html' if request.args.get('style',
                                                                            '') == '' else 'post/_post_voting_buttons_masonry.html'
            return render_template(template, post=post, community=post.community, recently_upvoted=[],
                                   recently_downvoted=[])

    undo = post.vote(user, vote_direction)

    # mark the post as read for the user
    user.mark_post_as_read(post)

    task_selector('vote_for_post', user_id=user.id, post_id=post_id, vote_to_undo=undo, vote_direction=vote_direction, federate=federate)

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
    Post.query.filter_by(id=post_id, deleted=False).one()
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

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
    Post.query.filter_by(id=post_id, deleted=False).one()
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
    post = Post.query.filter_by(id=post_id, deleted=False).one()
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
        domain = domain_from_url(url)
        if domain:
            if domain.banned or domain.name.endswith('.pages.dev'):
                raise Exception(domain.name + ' is blocked by admin')

    if uploaded_file and uploaded_file.filename != '':
        # check if this is an allowed type of file
        allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.mpo', '.avif', '.svg']
        file_ext = os.path.splitext(uploaded_file.filename)[1]
        if file_ext.lower() not in allowed_extensions:
            raise Exception('filetype not allowed')

    post = Post(user_id=user.id, community_id=community.id, instance_id=user.instance_id, posted_at=utcnow(),
                ap_id=gibberish(), title=title, language_id=language_id)
    db.session.add(post)
    db.session.commit()

    post.up_votes = 1
    effect = user.instance.vote_weight
    post.score = post.up_votes * effect
    post.ranking = post.post_ranking(post.score, post.posted_at)
    post.ranking_scaled = int(post.ranking + community.scale_by())
    cache.delete_memoized(recently_upvoted_posts, user.id)

    community.post_count += 1
    community.last_active = g.site.last_active = utcnow()
    user.post_count += 1

    post.ap_id = f"https://{current_app.config['SERVER_NAME']}/post/{post.id}"
    vote = PostVote(user_id=user.id, post_id=post.id, author_id=user.id, effect=1)
    db.session.add(vote)
    db.session.commit()

    try:
        post = edit_post(input, post, type, src, user, auth, uploaded_file, from_scratch=True)
    except Exception as e:
        if str(e) == 'This image is blocked':
            db.session.delete(vote)
            db.session.delete(post)
            db.session.commit()
            raise e

    if post.status == POST_STATUS_PUBLISHED:
        notify_about_post(post)

    if src == SRC_API:
        return user.id, post
    else:
        return post


# 'from_scratch == True' means that it's not really a user edit, we're just re-using code for make_post()
def edit_post(input, post, type, src, user=None, auth=None, uploaded_file=None, from_scratch=False, hash=None):
    if src == SRC_API:
        if not user:
            user = authorise_api_user(auth, return_type='model')
        title = input['title']
        body = input['body']
        url = input['url']
        nsfw = input['nsfw']
        notify_author = input['notify_author']
        language_id = input['language_id']
        timezone = input['timezone'] if 'timezone' in input else user.timezone
        tags = []
        flair = []
        scheduled_for = None
        repeat = None
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
    post.indexable = user.indexable
    post.sticky = False if src == SRC_API else input.sticky.data
    post.nsfw = nsfw
    post.nsfl = False if src == SRC_API else input.nsfl.data
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

        # remove any old tags
        post.tags.clear()
        post.flair.clear()

        post.edited_at = utcnow()

        db.session.commit()

    if uploaded_file and uploaded_file.filename != '':
        # check if this is an allowed type of file
        allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.mpo', '.avif', '.svg']
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
        final_place = os.path.join(directory, new_filename + file_ext)
        uploaded_file.seek(0)
        uploaded_file.save(final_place)

        final_ext = file_ext  # track file extension for conversion

        if file_ext.lower() == '.heic':
            register_heif_opener()
        if file_ext.lower() == '.avif':
            import pillow_avif  # NOQA  # do not remove

        Image.MAX_IMAGE_PIXELS = 89478485

        # Use environment variables to determine image max dimension, format, and quality
        image_max_dimension = current_app.config['MEDIA_IMAGE_MAX_DIMENSION']
        image_format = current_app.config['MEDIA_IMAGE_FORMAT']
        image_quality = current_app.config['MEDIA_IMAGE_QUALITY']

        if image_format == 'AVIF':
            import pillow_avif  # NOQA  # do not remove

        if not final_place.endswith('.svg') and not final_place.endswith('.gif'):
            img = Image.open(final_place)
            if '.' + img.format.lower() in allowed_extensions:
                img = ImageOps.exif_transpose(img)
                img = img.convert('RGB' if (image_format == 'JPEG' or final_ext in ['.jpg', '.jpeg']) else 'RGBA')
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

        if final_place.endswith('.gif'):
            gif_image = Image.open(final_place)
            gif_image.save(final_place[:-4] + ".webp", format="WEBP", save_all=True, loop=0)
            os.remove(final_place)
            final_place = final_place[:-4] + ".webp"
            final_ext = '.webp'

        url = f"{current_app.config['HTTP_PROTOCOL']}://{current_app.config['SERVER_NAME']}/{final_place.replace('app/', '')}"

        if current_app.config['IMAGE_HASHING_ENDPOINT']:  # and not user.trustworthy():
            hash = retrieve_image_hash(url)
            if hash and hash_matches_blocked_image(hash):
                raise Exception('This image is blocked')

        # Move uploaded file to S3
        if store_files_in_s3():
            session = boto3.session.Session()
            s3 = session.client(
                service_name='s3',
                region_name=current_app.config['S3_REGION'],
                endpoint_url=current_app.config['S3_ENDPOINT'],
                aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
            )
            s3.upload_file(final_place, current_app.config['S3_BUCKET'], 'posts/' +
                           new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext,
                           ExtraArgs={'ContentType': guess_mime_type(final_place)})
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
            post.type = POST_TYPE_IMAGE
            if uploaded_file:
                # change this line when uploaded_file is supported in API
                file.alt_text = input.image_alt_text.data if input.image_alt_text.data else title
            db.session.add(file)
            db.session.commit()
            post.image_id = file.id
            make_image_sizes(post.image_id, 512, 1200, 'posts', post.community.low_quality)
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

            if url.endswith('.mp4') or url.endswith('.webm') or is_video_hosting_site(embed_url):
                post.type = POST_TYPE_VIDEO
            else:
                post.type = POST_TYPE_LINK

        post.calculate_cross_posts(url_changed=url_changed)
    elif url and is_video_hosting_site(url):
        post.type = POST_TYPE_VIDEO

    federate = True
    if type == POST_TYPE_POLL:
        post.type = POST_TYPE_POLL
        for i in range(1, 10):
            # change this line when polls are supported in API
            choice_data = getattr(input, f"choice_{i}").data.strip()
            if choice_data != '':
                db.session.add(PollChoice(post_id=post.id, choice_text=choice_data, sort_order=i))

        poll = Poll.query.filter_by(post_id=post.id).first()
        if poll is None:
            poll = Poll(post_id=post.id)
            db.session.add(poll)
        poll.mode = input.mode.data
        if input.finish_in:
            poll.end_poll = end_poll_date(input.finish_in.data)
        poll.local_only = input.local_only.data
        poll.latest_vote = None
        db.session.commit()

        if poll.local_only:
            federate = False

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
def delete_post(post_id, src, auth):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    post = Post.query.filter_by(id=post_id, user_id=user_id, deleted=False).one()
    if post.url:
        post.calculate_cross_posts(delete_only=True)

    post.deleted = True
    post.deleted_by = user_id
    post.author.post_count -= 1
    post.community.post_count -= 1
    db.session.commit()
    if src == SRC_WEB:
        flash(_('Post deleted.'))

    task_selector('delete_post', user_id=user_id, post_id=post.id)

    if src == SRC_API:
        return user_id, post
    else:
        return


def restore_post(post_id, src, auth):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    post = Post.query.filter_by(id=post_id, user_id=user_id, deleted=True).one()
    if post.url:
        post.calculate_cross_posts()

    post.deleted = False
    post.deleted_by = None
    post.author.post_count -= 1
    post.community.post_count -= 1
    db.session.commit()
    if src == SRC_WEB:
        flash(_('Post restored.'))

    task_selector('restore_post', user_id=user_id, post_id=post.id)

    if src == SRC_API:
        return user_id, post
    else:
        return


def report_post(post_id, input, src, auth=None):
    if src == SRC_API:
        post = Post.query.filter_by(id=post_id).one()
        user_id = authorise_api_user(auth)
        reason = input['reason']
        description = input['description']
        report_remote = input['report_remote']
    else:
        post = Post.query.get_or_404(post_id)
        user_id = current_user.id
        reason = input.reasons_to_string(input.reasons.data)
        description = input.description.data
        report_remote = input.report_remote.data

    if post.reports == -1:  # When a mod decides to ignore future reports, post.reports is set to -1
        if src == SRC_API:
            raise Exception('already_reported')
        else:
            flash(_('Post has already been reported, thank you!'))
            return

    report = Report(reasons=reason, description=description, type=1, reporter_id=user_id, suspect_post_id=post.id,
                    suspect_community_id=post.community_id,
                    suspect_user_id=post.user_id, in_community_id=post.community_id, source_instance_id=1)
    db.session.add(report)

    # Notify moderators
    already_notified = set()
    suspect_user = User.query.get(post.user_id)
    reporter_user = User.query.get(user_id)
    targets_data = {'gen': '0',
                    'suspect_post_id': post.id,
                    'suspect_user_id': post.user_id,
                    'suspect_user_user_name': suspect_user.ap_id if suspect_user.ap_id else suspect_user.user_name,
                    'reporter_id': user_id,
                    'reporter_user_name': reporter_user.ap_id if reporter_user.ap_id else reporter_user.user_name,
                    'orig_post_title': post.title,
                    'orig_post_body': post.body
                    }
    for mod in post.community.moderators():
        moderator = User.query.get(mod.user_id)
        if moderator and moderator.is_local():
            with force_locale(get_recipient_language(moderator.id)):
                notification = Notification(user_id=mod.user_id, title=gettext('A post has been reported'),
                                            url=f"https://{current_app.config['SERVER_NAME']}/post/{post.id}",
                                            author_id=user_id, notif_type=NOTIF_REPORT,
                                            subtype='post_reported',
                                            targets=targets_data)
                db.session.add(notification)
                already_notified.add(mod.user_id)
    post.reports += 1
    # todo: only notify admins for certain types of report
    for admin in Site.admins():
        if admin.id not in already_notified:
            notify = Notification(title='Suspicious content', url='/admin/reports', user_id=admin.id,
                                  author_id=user_id, notif_type=NOTIF_REPORT,
                                  subtype='post_reported',
                                  targets=targets_data)
            db.session.add(notify)
            admin.unread_notifications += 1
    db.session.commit()

    # federate report to originating instance
    if not post.community.is_local() and report_remote:
        summary = reason
        if description:
            summary += ' - ' + description

        task_selector('report_post', user_id=user_id, post_id=post_id, summary=summary)

    if src == SRC_API:
        return user_id, report
    else:
        return


def lock_post(post_id, locked, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = Post.query.filter_by(id=post_id).one()
    if locked:
        comments_enabled = False
        modlog_type = 'lock_post'
    else:
        comments_enabled = True
        modlog_type = 'unlock_post'

    if post.community.is_moderator(user) or post.community.is_instance_admin(user):
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


def sticky_post(post_id: int, featured: bool, src: int, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = Post.query.filter_by(id=post_id).one()
    community = post.community

    if post.community.is_moderator(user) or post.community.is_instance_admin(user) or user.is_admin():
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


# mod deletes
def mod_remove_post(post_id, reason, src, auth):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = Post.query.filter_by(id=post_id, user_id=user.id, deleted=False).one()
    if not post.community.is_moderator(user) and not post.community.is_instance_admin(user):
        raise Exception('Does not have permission')

    if post.url:
        post.calculate_cross_posts(delete_only=True)

    post.deleted = True
    post.deleted_by = user.id
    post.author.post_count -= 1
    post.community.post_count -= 1
    db.session.commit()
    if src == SRC_WEB:
        flash(_('Post deleted.'))

    add_to_modlog('delete_post', actor=user, target_user=post.author, reason=reason,
                  community=post.community, post=post,
                  link_text=shorten_string(post.title), link=f'post/{post.id}')

    task_selector('delete_post', user_id=user.id, post_id=post.id, reason=reason)

    if src == SRC_API:
        return user.id, post
    else:
        return


def mod_restore_post(post_id, reason, src, auth):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = Post.query.filter_by(id=post_id, user_id=user.id, deleted=True).one()
    if not post.community.is_moderator(user) and not post.community.is_instance_admin(user):
        raise Exception('Does not have permission')

    if post.url:
        post.calculate_cross_posts()

    post.deleted = False
    post.deleted_by = None
    post.author.post_count -= 1
    post.community.post_count -= 1
    db.session.commit()
    if src == SRC_WEB:
        flash(_('Post restored.'))

    add_to_modlog('restore_post', actor=user, target_user=post.author, reason=reason,
                  community=post.community, post=post,
                  link_text=shorten_string(post.title), link=f'post/{post.id}')

    task_selector('restore_post', user_id=user.id, post_id=post.id, reason=reason)

    if src == SRC_API:
        return user.id, post
    else:
        return
