import json as python_json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import BytesIO

from feedgen.feed import FeedGenerator
from flask import redirect, url_for, flash, request, make_response, session, current_app, abort, json, g, send_file
from flask_babel import _, lazy_gettext as _l
from flask_login import logout_user, current_user
from sqlalchemy import desc, or_, text, asc
from sqlalchemy.orm.exc import NoResultFound

from app import db, cache, celery
from app.activitypub.signature import default_context, send_post_request
from app.activitypub.util import find_actor_or_create, extract_domain_and_actor
from app.auth.util import random_token
from app.community.util import save_icon_file, save_banner_file, retrieve_mods_and_backfill, search_for_community
from app.constants import *
from app.email import send_verification_email
from app.ldap_utils import sync_user_to_ldap
from app.models import Post, Community, CommunityMember, User, PostReply, PostVote, Notification, utcnow, File, Site, \
    Instance, Report, UserBlock, CommunityBan, CommunityJoinRequest, CommunityBlock, Filter, Domain, DomainBlock, \
    InstanceBlock, NotificationSubscription, PostBookmark, PostReplyBookmark, read_posts, Topic, UserNote, \
    UserExtraField, Feed, FeedMember, IpBan, user_file, ArchivedPostReply
from app.shared.site import block_remote_instance
from app.shared.tasks import task_selector
from app.shared.upload import process_file_delete, process_upload
from app.shared.user import subscribe_user, ban_user, unban_user
from app.user import bp
from app.user.forms import ProfileForm, SettingsForm, DeleteAccountForm, ReportUserForm, \
    FilterForm, KeywordFilterEditForm, RemoteFollowForm, ImportExportForm, UserNoteForm, BanUserForm, DeleteFileForm, \
    UploadFileForm, BlockUserForm, BlockCommunityForm, BlockDomainForm, BlockInstanceForm, UnsubAllForm
from app.user.utils import purge_user_then_delete, unsubscribe_from_community, search_for_user, _get_user_moderates, \
    _get_user_upvoted_posts, _get_user_subscribed_communities, _get_user_posts, _get_user_post_replies, \
    _get_user_archived_replies, _get_user_posts_and_replies, _get_user_same_ip
from app.utils import render_template, markdown_to_html, user_access, markdown_to_text, shorten_string, \
    gibberish, file_get_contents, community_membership, user_filters_home, \
    user_filters_posts, user_filters_replies, theme_list, \
    blocked_users, add_to_modlog, \
    blocked_communities, piefed_markdown_to_lemmy_markdown, \
    read_language_choices, request_etag_matches, return_304, mimetype_from_url, notif_id_to_string, \
    login_required_if_private_instance, recently_upvoted_posts, recently_downvoted_posts, recently_upvoted_post_replies, \
    recently_downvoted_post_replies, reported_posts, user_notes, login_required, get_setting, filtered_out_communities, \
    moderating_communities_ids, is_valid_xml_utf8, blocked_or_banned_instances, blocked_domains, get_task_session, \
    patch_db_session, user_in_restricted_country, referrer, user_pronouns, community_membership_private, \
    intlist_to_strlist


@bp.route('/people', methods=['GET', 'POST'])
@login_required
def show_people():
    return redirect(url_for('instance.instance_people', instance_domain=current_app.config['SERVER_NAME']))


@bp.route('/user/<int:user_id>', methods=['GET'])
@login_required_if_private_instance
def show_profile_by_id(user_id):
    user = User.query.get_or_404(user_id)
    return show_profile(user)


@login_required_if_private_instance
def show_profile(user):
    if (user.deleted or user.banned) and current_user.is_anonymous:
        abort(404)

    if user.deleted:
        flash(_('This user has been deleted.'), 'warning')
    elif user.banned:
        flash(_('This user has been banned.'), 'warning')

    post_page = request.args.get('post_page', 1, type=int)
    replies_page = request.args.get('replies_page', 1, type=int)
    overview_page = request.args.get('overview_page', 1, type=int)

    # posts and replies
    moderates = _get_user_moderates(user)
    subscribed = _get_user_subscribed_communities(user)
    posts = _get_user_posts(user, post_page)
    post_replies = _get_user_post_replies(user, replies_page)
    archived_post_replies = _get_user_archived_replies(user)
    overview_items, overview_has_next_page = _get_user_posts_and_replies(user, overview_page)
    same_ip_address = _get_user_same_ip(user)

    # profile info
    canonical = user.ap_public_url if user.ap_public_url else None
    description = shorten_string(markdown_to_text(user.about), 150) if user.about else None

    # find all user feeds marked as public
    user_has_public_feeds = False
    user_public_feeds = Feed.query.filter_by(public=True).filter_by(user_id=user.id).all()

    if len(user_public_feeds) > 0:
        user_has_public_feeds = True

    # pagination urls
    post_next_url = url_for('activitypub.user_profile', actor=user.ap_id if user.ap_id is not None else user.user_name,
                            post_page=posts.next_num) if posts.has_next else None
    post_prev_url = url_for('activitypub.user_profile', actor=user.ap_id if user.ap_id is not None else user.user_name,
                            post_page=posts.prev_num) if posts.has_prev and post_page != 1 else None
    replies_next_url = url_for('activitypub.user_profile',
                               actor=user.ap_id if user.ap_id is not None else user.user_name,
                               replies_page=post_replies.next_num) if post_replies.has_next else None
    replies_prev_url = url_for('activitypub.user_profile',
                               actor=user.ap_id if user.ap_id is not None else user.user_name,
                               replies_page=post_replies.prev_num) if post_replies.has_prev and replies_page != 1 else None
    overview_next_url = url_for('activitypub.user_profile',
                                actor=user.ap_id if user.ap_id is not None else user.user_name,
                                overview_page=overview_page + 1) if overview_has_next_page else None
    overview_prev_url = url_for('activitypub.user_profile',
                                actor=user.ap_id if user.ap_id is not None else user.user_name,
                                overview_page=overview_page - 1) if overview_page != 1 else None

    return render_template('user/show_profile.html', user=user, posts=posts, post_replies=post_replies,
                           moderates=moderates, canonical=canonical, title=_('Posts by %(user_name)s',
                                                                             user_name=user.user_name),
                           description=description, subscribed=subscribed, disable_voting=True,
                           user_notes=user_notes(current_user.get_id()),
                           post_next_url=post_next_url, post_prev_url=post_prev_url,
                           replies_next_url=replies_next_url, replies_prev_url=replies_prev_url,
                           noindex=not user.indexable, show_post_community=True, hide_vote_buttons=True,
                           show_deleted=current_user.is_authenticated and current_user.is_admin_or_staff(),
                           reported_posts=reported_posts(current_user.get_id(), current_user.get_id() in g.admin_ids),
                           moderated_community_ids=moderating_communities_ids(current_user.get_id()),
                           rss_feed=f"{current_app.config['SERVER_URL']}/u/{user.link()}/feed" if user.post_count > 0 else None,
                           rss_feed_name=f"{user.display_name()} on {g.site.name}" if user.post_count > 0 else None,
                           user_has_public_feeds=user_has_public_feeds, user_public_feeds=user_public_feeds,
                           overview_items=overview_items, overview_next_url=overview_next_url,
                           overview_prev_url=overview_prev_url, same_ip_address=same_ip_address,
                           archived_post_replies=archived_post_replies)


@bp.route('/u/<actor>/upvotes')
@login_required_if_private_instance
def user_upvotes(actor):
    actor = actor.strip()
    if '@' in actor:
        user = find_actor_or_create(actor, create_if_not_found=False)
    else:
        user = find_actor_or_create(f'{current_app.config["SERVER_URL"]}/u/{actor}', create_if_not_found=False)

    upvoted = _get_user_upvoted_posts(user)

    if user is not None:
        return render_template('user/show_upvoted.html', user=user,
                               title=_('Posts upvoted by %(user_name)s', user_name=user.user_name),
                               upvoted=upvoted, disable_voting=True,
                               user_notes=user_notes(current_user.get_id()),
                               rss_feed=f"{current_app.config['SERVER_URL']}/u/{user.link()}/feed" if user.post_count > 0 else None,
                               rss_feed_name=f"{user.display_name()} on {g.site.name}" if user.post_count > 0 else None)


@bp.route('/u/<actor>/profile', methods=['GET', 'POST'])
@login_required
def edit_profile(actor):
    actor = actor.strip()
    user = User.query.filter_by(user_name=actor, deleted=False, banned=False, ap_id=None).first()
    if user is None:
        abort(404)
    if current_user.id != user.id:
        abort(401)
    delete_form = DeleteAccountForm()
    unsub_form = UnsubAllForm()
    form = ProfileForm()
    old_email = user.email
    if form.validate_on_submit() and not current_user.banned:
        current_user.title = form.title.data.strip()
        current_user.email = form.email.data.strip()
        # Email address has changed - request verification of new address
        if form.email.data.strip() != old_email and get_setting('email_verification', True):
            current_user.verified = False
            verification_token = random_token(16)
            current_user.verification_token = verification_token
            send_verification_email(current_user)
            flash(_('You have changed your email address so we need to verify it. Please check your email inbox for a verification link.'),
                  'warning')
        current_user.email = form.email.data.strip()
        password_updated = False
        if form.password.data.strip() != '':
            current_user.set_password(form.password.data)
            current_user.password_updated_at = utcnow()
            password_updated = True
        current_user.about = piefed_markdown_to_lemmy_markdown(form.about.data)
        current_user.about_html = markdown_to_html(form.about.data)
        current_user.matrix_user_id = form.matrixuserid.data
        current_user.extra_fields = []
        current_user.timezone = form.timezone.data
        if form.extra_label_1.data.strip() != '' and form.extra_text_1.data.strip() != '':
            current_user.extra_fields.append(
                UserExtraField(label=form.extra_label_1.data.strip(), text=form.extra_text_1.data.strip()))
        if form.extra_label_2.data.strip() != '' and form.extra_text_2.data.strip() != '':
            current_user.extra_fields.append(
                UserExtraField(label=form.extra_label_2.data.strip(), text=form.extra_text_2.data.strip()))
        if form.extra_label_3.data.strip() != '' and form.extra_text_3.data.strip() != '':
            current_user.extra_fields.append(
                UserExtraField(label=form.extra_label_3.data.strip(), text=form.extra_text_3.data.strip()))
        if form.extra_label_4.data.strip() != '' and form.extra_text_4.data.strip() != '':
            current_user.extra_fields.append(
                UserExtraField(label=form.extra_label_4.data.strip(), text=form.extra_text_4.data.strip()))
        current_user.bot = form.bot.data
        db.session.commit()

        profile_file = request.files['profile_file']
        if profile_file and profile_file.filename != '':
            # remove old avatar after adding the new one succeeds
            old_avatar = current_user.avatar_id

            # add new avatar
            file = save_icon_file(profile_file, 'users')
            if file:
                current_user.avatar = file
                if old_avatar:
                    old_file = File.query.get(old_avatar)
                    db.session.delete(old_file)
                db.session.commit()
                if old_avatar:
                    old_file.delete_from_disk()

        banner_file = request.files['banner_file']
        if banner_file and banner_file.filename != '':
            # remove old cover
            old_banner = current_user.cover_id

            # add new cover
            file = save_banner_file(banner_file, 'users')
            if file:
                current_user.cover = file
                cache.delete_memoized(User.cover_image, current_user)
                if old_banner:
                    old_banner_file = File.query.get(old_banner)
                    db.session.delete(old_banner_file)
                db.session.commit()
                if old_banner:
                    old_banner_file.delete_from_disk()

        # Sync to LDAP
        try:
            sync_user_to_ldap(
                current_user.user_name,
                current_user.email,
                form.password.data.strip() if password_updated else None
            )
        except Exception as e:
            # Log error but don't fail the profile update
            current_app.logger.error(f"LDAP sync failed for user {current_user.user_name}: {e}")

        cache.delete_memoized(user_pronouns)
        from app.api.alpha.views import user_view
        cache.delete_memoized(user_view)
        flash(_('Your changes have been saved.'), 'success')

        return redirect(url_for('user.edit_profile', actor=actor))
    elif request.method == 'GET':
        form.title.data = current_user.title
        form.email.data = current_user.email
        form.about.data = current_user.about
        form.timezone.data = current_user.timezone
        i = 1
        for extra_field in current_user.extra_fields:
            getattr(form, f"extra_label_{i}").data = extra_field.label
            getattr(form, f"extra_text_{i}").data = extra_field.text
            i += 1
        form.matrixuserid.data = current_user.matrix_user_id
        form.bot.data = current_user.bot
        form.password.data = ''

    return render_template('user/edit_profile.html', title=_('Edit profile'), form=form, user=current_user,
                           markdown_editor=current_user.markdown_editor, delete_form=delete_form, unsub_form=unsub_form)


@bp.route('/user/remove_avatar', methods=['GET', 'POST'])
@login_required
def remove_avatar():
    if current_user.avatar_id:
        current_user.avatar.delete_from_disk()
        if current_user.avatar_id:
            file = File.query.get(current_user.avatar_id)
            file.delete_from_disk()
            current_user.avatar_id = None
            db.session.delete(file)
            db.session.commit()
    return _('Avatar removed!')


@bp.route('/user/remove_cover', methods=['GET', 'POST'])
@login_required
def remove_cover():
    if current_user.cover_id:
        current_user.cover.delete_from_disk()
        if current_user.cover_id:
            file = File.query.get(current_user.cover_id)
            file.delete_from_disk()
            current_user.cover_id = None
            db.session.delete(file)
            db.session.commit()
            cache.delete_memoized(User.cover_image, current_user)
    return '<div> ' + _('Banner removed!') + '</div>'


# export settings function. used in the /user/settings for a user to export their own settings
def export_user_settings(user):
    # make the empty dict
    user_dict = {}

    # take the current_user already found
    # add user's settings to the dict for output
    # arranged to match the lemmy settings output order
    user_dict['display_name'] = user.title
    user_dict['bio'] = user.about
    if user.avatar_image() != '':
        user_dict['avatar'] = f"{current_app.config['SERVER_URL']}/{user.avatar_image()}"
    if user.cover_image() != '':
        user_dict['banner'] = f"{current_app.config['SERVER_URL']}/{user.cover_image()}"
    user_dict['matrix_id'] = user.matrix_user_id
    user_dict['bot_account'] = user.bot
    if user.hide_nsfw == 1:
        lemmy_show_nsfw = False
    else:
        lemmy_show_nsfw = True
    if user.ignore_bots == 1:
        lemmy_show_bot_accounts = False
    else:
        lemmy_show_bot_accounts = True
    user_dict['settings'] = {
        "email": f"{user.email}",
        "show_nsfw": lemmy_show_nsfw,
        "theme": user.theme,
        "default_sort_type": f'{user.default_sort}'.capitalize(),
        "default_listing_type": f'{user.default_filter}'.capitalize(),
        "interface_language": user.interface_language,
        "show_bot_accounts": lemmy_show_bot_accounts,
        # the below items are needed for lemmy to do the import
        # the "id" and "person_id" are just set to 42
        # as they expect an int, but it does not override the
        # existing user's "id"  and "public_id"
        "id": 42,
        "person_id": 42,
        "show_avatars": True,
        "send_notifications_to_email": False,
        "show_scores": True,
        "show_read_posts": True,
        "email_verified": False,
        "accepted_application": True,
        "open_links_in_new_tab": False,
        "blur_nsfw": True,
        "auto_expand": False,
        "infinite_scroll_enabled": False,
        "admin": False,
        "post_listing_mode": "List",
        "totp_2fa_enabled": False,
        "enable_keyboard_navigation": False,
        "enable_animated_images": True,
        "collapse_bot_comments": False

    }
    # get the user subscribed communities' ap_profile_id
    user_subscribed_communities = []
    for c in user.communities():
        if c.ap_profile_id is None:
            continue
        else:
            user_subscribed_communities.append(c.ap_profile_id)
    user_dict['followed_communities'] = user_subscribed_communities

    # get bookmarked/saved posts
    bookmarked_posts = []
    post_bookmarks = PostBookmark.query.filter_by(user_id=user.id).all()
    for pb in post_bookmarks:
        p = Post.query.filter_by(id=pb.post_id).first()
        bookmarked_posts.append(p.ap_id)
    user_dict['saved_posts'] = bookmarked_posts

    # get bookmarked/saved comments
    saved_comments = []
    post_reply_bookmarks = PostReplyBookmark.query.filter_by(user_id=user.id).all()
    for prb in post_reply_bookmarks:
        pr = PostReply.query.filter_by(id=prb.post_reply_id).first()
        saved_comments.append(pr.ap_id)
    user_dict['saved_comments'] = saved_comments

    # get blocked communities
    blocked_communities = []
    community_blocks = CommunityBlock.query.filter_by(user_id=user.id).all()
    for cb in community_blocks:
        c = Community.query.filter_by(id=cb.community_id).first()
        blocked_communities.append(c.ap_public_url)
    user_dict['blocked_communities'] = blocked_communities

    # get blocked users
    blocked_users = []
    user_blocks = UserBlock.query.filter_by(blocker_id=user.id).all()
    for ub in user_blocks:
        blocked_user = User.query.filter_by(id=ub.blocked_id).first()
        blocked_users.append(blocked_user.ap_public_url)
    user_dict['blocked_users'] = blocked_users

    # get blocked instances
    blocked_instances = []
    instance_blocks = InstanceBlock.query.filter_by(user_id=user.id).all()
    for ib in instance_blocks:
        i = Instance.query.filter_by(id=ib.instance_id).first()
        blocked_instances.append(i.domain)
    user_dict['blocked_instances'] = blocked_instances

    notes = []
    for user_note in UserNote.query.filter(UserNote.user_id == user.id):
        target = User.query.get(user_note.target_id)
        if target:
            notes.append({'target': target.profile_id(), 'note': user_note.body})
    user_dict['user_notes'] = notes

    # piefed versions of (most of) the same settings
    # TO-DO: adjust the piefed side import method to just take the doubled
    # settings from the lemmy formatted output. Then remove the duplicate
    # items here.
    user_dict['user_name'] = user.user_name
    user_dict['title'] = user.title
    user_dict['email'] = user.email
    user_dict['about'] = user.about
    user_dict['about_html'] = user.about_html
    user_dict['keywords'] = user.keywords
    user_dict['matrix_user_id'] = user.matrix_user_id
    user_dict['hide_nsfw'] = user.hide_nsfw
    user_dict['hide_nsfl'] = user.hide_nsfl
    user_dict['receive_message_mode'] = user.receive_message_mode
    user_dict['bot'] = user.bot
    user_dict['ignore_bots'] = user.ignore_bots
    user_dict['default_sort'] = user.default_sort
    user_dict['default_filter'] = user.default_filter
    user_dict['theme'] = user.theme
    user_dict['markdown_editor'] = user.markdown_editor
    user_dict['interface_language'] = user.interface_language
    user_dict['reply_collapse_threshold'] = user.reply_collapse_threshold
    if user.avatar_image() != '':
        user_dict['avatar_image'] = f"{current_app.config['SERVER_URL']}/{user.avatar_image()}"
    if user.cover_image() != '':
        user_dict['cover_image'] = f"{current_app.config['SERVER_URL']}/{user.cover_image()}"
    user_dict['user_blocks'] = blocked_users

    # setup the BytesIO buffer
    buffer = BytesIO()
    buffer.write(str(python_json.dumps(user_dict)).encode('utf-8'))
    buffer.seek(0)

    # pass the buffer back to the calling function, so it can be given to the
    # user for downloading
    return buffer


@bp.route('/user/settings', methods=['GET', 'POST'])
@login_required
def user_settings():
    user = User.query.filter_by(id=current_user.id, deleted=False, banned=False, ap_id=None).first()
    if user is None:
        abort(404)
    form = SettingsForm()
    form.theme.choices = theme_list()
    form.interface_language.choices = [
        ('', _l('Auto-detect')),
        ('eu', _l('Basque')),
        ('ca', _l('Catalan')),
        ('zh', _l('Chinese')),
        ('en', _l('English')),
        ('fi', _l('Finnish')),
        ('fr', _l('French')),
        ('de', _l('German')),
        ('hi', _l('Hindi')),
        ('ja', _l('Japanese')),
        ('es', _l('Spanish')),
        ('pl', _l('Polish')),
        ('uk', _l('Ukrainian')),
    ]
    form.read_languages.choices = read_language_choices()
    if form.validate_on_submit():
        propagate_indexable = form.indexable.data != current_user.indexable
        current_user.newsletter = form.newsletter.data
        current_user.searchable = form.searchable.data
        current_user.indexable = form.indexable.data
        current_user.hide_read_posts = form.hide_read_posts.data
        current_user.default_sort = form.default_sort.data
        current_user.default_comment_sort = form.default_comment_sort.data
        current_user.default_filter = form.default_filter.data
        current_user.theme = form.theme.data
        current_user.allow_community_themes = form.allow_community_themes.data
        current_user.email_unread = form.email_unread.data
        current_user.markdown_editor = form.markdown_editor.data
        current_user.interface_language = form.interface_language.data
        current_user.feed_auto_follow = form.feed_auto_follow.data
        current_user.feed_auto_leave = form.feed_auto_leave.data
        current_user.read_language_ids = form.read_languages.data
        current_user.accept_private_messages = form.accept_private_messages.data
        current_user.font = form.font.data
        current_user.code_style = form.code_style.data
        current_user.additional_css = form.additional_css.data
        session['ui_language'] = form.interface_language.data
        current_user.vote_privately = not form.federate_votes.data
        current_user.show_subscribed_communities = form.show_subscribed_communities.data
        if propagate_indexable:
            db.session.execute(text('UPDATE "post" set indexable = :indexable WHERE user_id = :user_id'),
                               {'user_id': current_user.id,
                                'indexable': current_user.indexable})

        db.session.commit()
        from app.api.alpha.views import user_view
        cache.delete_memoized(user_view)

        flash(_('Your changes have been saved.'), 'success')

        resp = make_response(redirect(url_for('user.user_settings')))
        
        # Handle max_hours_per_day changes with restriction logic
        current_max_hours = request.cookies.get('max_hours_per_day', '')
        new_max_hours = form.max_hours_per_day.data
        new_restriction_setting = form.max_hours_change_restriction.data
        
        resp.set_cookie('max_hours_change_restriction', new_restriction_setting,
                        expires=datetime(year=2099, month=12, day=30))
        
        # Only handle restriction date logic if max_hours_per_day has actually changed
        if str(new_max_hours) != str(current_max_hours):
            restriction_cookie = request.cookies.get('max_hours_restriction_date')
            current_date = datetime.now()
            
            if restriction_cookie and current_max_hours and int(current_max_hours) > 0:
                restriction_date = datetime.fromisoformat(restriction_cookie)

                # Check if restriction period has passed
                if current_date < restriction_date:
                    # Still restricted, don't allow change
                    flash(_('You cannot change the daily usage limit until %(date)s',
                           date=restriction_date.strftime('%Y-%m-%d %H:%M')), 'warning')
                    # Revert to old value
                    new_max_hours = current_max_hours
                else:
                    # Restriction period has passed, allow change and set new restriction
                    if new_restriction_setting != 'anytime':
                        future_date = _calculate_future_date(new_restriction_setting)
                        resp.set_cookie('max_hours_restriction_date', future_date.isoformat(),
                                        expires=datetime(year=2099, month=12, day=30))
            else:
                # No current restriction or no current limit set, set restriction if needed
                if new_restriction_setting != 'anytime' and new_max_hours and int(new_max_hours) > 0:
                    future_date = _calculate_future_date(new_restriction_setting)
                    resp.set_cookie('max_hours_restriction_date', future_date.isoformat(), 
                                    expires=datetime(year=2099, month=12, day=30))
        
        # Set the max_hours_per_day cookie (either new value or reverted old value)
        if new_max_hours:
            resp.set_cookie('max_hours_per_day', str(new_max_hours), expires=datetime(year=2099, month=12, day=30))
        else:
            resp.set_cookie('max_hours_per_day', '', expires=datetime.min)
            # Clear restriction cookies if no limit is set
            resp.set_cookie('max_hours_restriction_date', '', expires=datetime.min)
            resp.set_cookie('max_hours_change_restriction', '', expires=datetime.min)
        
        resp.set_cookie('compact_level', form.compaction.data, expires=datetime(year=2099, month=12, day=30))
        resp.set_cookie('low_bandwidth', '1' if form.low_bandwidth_mode.data else '0',
                        expires=datetime(year=2099, month=12, day=30))
        return resp

    elif request.method == 'GET':
        form.newsletter.data = current_user.newsletter
        form.email_unread.data = current_user.email_unread
        form.searchable.data = current_user.searchable
        form.indexable.data = current_user.indexable
        form.hide_read_posts.data = current_user.hide_read_posts
        form.default_sort.data = current_user.default_sort
        form.default_comment_sort.data = current_user.default_comment_sort
        form.default_filter.data = current_user.default_filter
        form.theme.data = current_user.theme
        form.allow_community_themes.data = current_user.allow_community_themes
        form.markdown_editor.data = current_user.markdown_editor
        form.low_bandwidth_mode.data = request.cookies.get('low_bandwidth', '0') == '1'
        form.interface_language.data = current_user.interface_language
        form.federate_votes.data = not current_user.vote_privately
        form.feed_auto_follow.data = current_user.feed_auto_follow
        form.feed_auto_leave.data = current_user.feed_auto_leave
        form.read_languages.data = current_user.read_language_ids
        if request.cookies.get('compact_level', None) is None and current_app.config['HTTP_PROTOCOL'] == 'mixed':
            form.compaction.data = 'compact-min compact-max'
        else:
            form.compaction.data = request.cookies.get('compact_level', '')
        form.accept_private_messages.data = current_user.accept_private_messages
        form.font.data = current_user.font
        form.code_style.data = current_user.code_style or 'fruity'
        form.additional_css.data = current_user.additional_css
        form.show_subscribed_communities.data = current_user.show_subscribed_communities
        form.max_hours_per_day.data = request.cookies.get('max_hours_per_day', '')
        form.max_hours_change_restriction.data = request.cookies.get('max_hours_change_restriction', 'anytime')

    return render_template('user/edit_settings.html', title=_('Change settings'), form=form, user=current_user)


@bp.route('/user/connect_oauth', methods=['GET', 'POST'])
@login_required
def connect_oauth():
    user = User.query.filter_by(id=current_user.id, deleted=False, banned=False, ap_id=None).first()
    if user is None:
        abort(404)

    # Check if any OAuth providers are connected
    oauth_connections = {
        'google': user.google_oauth_id is not None,
        'discord': user.discord_oauth_id is not None,
        'mastodon': user.mastodon_oauth_id is not None
    }

    oauth_providers = {
        'google': current_app.config["GOOGLE_OAUTH_CLIENT_ID"] != '',
        'mastodon': current_app.config["MASTODON_OAUTH_CLIENT_ID"] != '',
        'discord': current_app.config["DISCORD_OAUTH_CLIENT_ID"] != ''
    }

    # Handle disconnect requests
    if request.method == 'POST':
        provider = request.form.get('disconnect_provider')
        if provider in oauth_connections:
            if provider == 'google':
                user.google_oauth_id = None
            elif provider == 'discord':
                user.discord_oauth_id = None
            elif provider == 'mastodon':
                user.mastodon_oauth_id = None

            db.session.commit()
            flash(_('Your %(provider)s account has been disconnected.', provider=provider.capitalize()), 'success')
            return redirect(url_for('user.connect_oauth'))

    return render_template('user/connect_oauth.html', title=_('Connect OAuth'), user=user,
                           oauth_providers=oauth_providers,
                           oauth_connections=oauth_connections)


@bp.route('/user/settings/import_export', methods=['GET', 'POST'])
@login_required
def user_settings_import_export():
    from app import redis_client
    user = User.query.filter_by(id=current_user.id, deleted=False, banned=False, ap_id=None).first()
    if user is None:
        abort(404)
    form = ImportExportForm()
    # separate if to handle just the 'Export' button being clicked
    if form.export_settings.data and form.validate():
        # get the user settings for this user
        buffer = export_user_settings(user)

        # confirmation displayed to user when the page loads up again
        flash(_('Export Complete.'))

        # send the file to the user as a download
        # the as_attachment=True results in flask
        # redirecting to the current page, so no
        # url_for needed here
        return send_file(buffer, download_name=f'{user.user_name}_piefed_settings.json', as_attachment=True,
                         mimetype='application/json')
    elif form.validate_on_submit():
        import_file = request.files['import_file']
        if import_file and import_file.filename != '':
            file_ext = os.path.splitext(import_file.filename)[1]
            if file_ext.lower() != '.json':
                abort(400)
            
            redis_key = f"import:{user.id}:{gibberish(15)}"
            imported_data = import_file.stream.read()
            
            redis_client.set(redis_key, imported_data, ex=3600)

            # import settings in background task
            import_settings(redis_key)

            flash(_('Your subscriptions and blocks are being imported. If you have many it could take a few minutes.'))

        db.session.commit()

        flash(_('Your changes have been saved.'), 'success')
        return redirect(url_for('user.user_settings_import_export'))

    return render_template('user/import_export.html', title=_('Import & Export'), form=form, user=current_user)


@bp.route('/user/<int:user_id>/notification', methods=['GET', 'POST'])
@login_required
def user_notification(user_id: int):
    try:
        return subscribe_user(user_id, None, SRC_WEB)
    except NoResultFound:
        abort(404)


@bp.route('/u/<actor>/ban', methods=['GET', 'POST'])
@login_required
def ban_profile(actor):
    form = BanUserForm()
    if user_access('ban users', current_user.id) or user_access('manage users', current_user.id):
        actor = actor.strip()
        if '@' in actor:
            user = find_actor_or_create(actor, create_if_not_found=False)
        else:
            user = find_actor_or_create(f'{current_app.config["SERVER_URL"]}/u/{actor}', create_if_not_found=False)
        if user is None:
            abort(404)

        if user.id == current_user.id:
            flash(_('You cannot ban yourself.'), 'error')
            goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
            return redirect(goto)
        else:
            if form.validate_on_submit():
                form.person_id = user.id
                ban_user(form, SRC_WEB, None)
                goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
                return redirect(goto)

            form.ip_address.data = True
            form.purge.data = True
            if not user_access('manage users', current_user.id):
                form.purge.render_kw = {'disabled': True}
                form.purge.data = False
            if user.ip_address is None or user.ip_address == '':
                form.ip_address.render_kw = {'disabled': True}
                form.ip_address.data = False

            return render_template('user/user_ban.html', form=form, title=_('Ban %(name)s', name=actor), user=user)
    else:
        abort(401)


@bp.route('/u/<actor>/unban', methods=['POST'])
@login_required
def unban_profile(actor):
    if user_access('ban users', current_user.id):
        actor = actor.strip()
        if '@' in actor:
            user = find_actor_or_create(actor, create_if_not_found=False, allow_banned=True)
        else:
            user = find_actor_or_create(f"{current_app.config['SERVER_URL']}/u/{actor}",
                                        create_if_not_found=False, allow_banned=True)
        if user is None:
            abort(404)

        if user.id == current_user.id:
            flash(_('You cannot unban yourself.'), 'error')
        else:
            unban_user({'person_id': user.id}, SRC_WEB, None)
            flash(_('%(actor)s has been unbanned.', actor=actor))
    else:
        abort(401)

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/u/<actor>/block', methods=['POST'])
@login_required
def block_profile(actor):
    actor = actor.strip()
    if '@' in actor:
        user = find_actor_or_create(actor, create_if_not_found=False)
    else:
        user = find_actor_or_create(f'{current_app.config["SERVER_URL"]}/u/{actor}', create_if_not_found=False)
    if user is None:
        abort(404)

    if user.id == current_user.id:
        flash(_('You cannot block yourself.'), 'error')
    else:
        existing_block = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=user.id).first()
        if not existing_block:
            block = UserBlock(blocker_id=current_user.id, blocked_id=user.id)
            db.session.add(block)
            db.session.execute(
                text('DELETE FROM "notification_subscription" WHERE entity_id = :current_user AND user_id = :user_id'),
                {'current_user': current_user.id, 'user_id': user.id})
            db.session.commit()

        if not user.is_local():
            ...
            # federate block

        flash(_('%(actor)s has been blocked.', actor=actor))
        cache.delete_memoized(blocked_users, current_user.id)

    if request.headers.get('HX-Request'):
        resp = make_response()
        curr_url = request.headers.get('HX-Current-Url')

        if "/user/" in curr_url:
            resp.headers['HX-Redirect'] = curr_url
        else:
            resp.headers['HX-Redirect'] = url_for("main.index")

        return resp

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/u/<actor>/block_instance', methods=['POST'])
@login_required
def user_block_instance(actor):
    actor = actor.strip()
    if '@' in actor:
        user = find_actor_or_create(actor, create_if_not_found=False)
    else:
        user = find_actor_or_create(f'{current_app.config["SERVER_URL"]}/u/{actor}', create_if_not_found=False)
    if user is None:
        abort(404)
    block_remote_instance(user.instance_id, SRC_WEB)
    flash(_('Content from %(name)s will be hidden.', name=user.ap_domain))

    if request.headers.get('HX-Request'):
        resp = make_response()
        curr_url = request.headers.get('HX-Current-Url')

        if user.ap_domain in curr_url:
            resp.headers["HX-Redirect"] = url_for("main.index")
        elif "/u/" in curr_url:
            resp.headers["HX-Redirect"] = url_for("main.index")
        else:
            resp.headers["HX-Redirect"] = curr_url

        return resp

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/u/<actor>/unblock', methods=['POST'])
@login_required
def unblock_profile(actor):
    actor = actor.strip()
    if '@' in actor:
        user = find_actor_or_create(actor, create_if_not_found=False)
    else:
        user = find_actor_or_create(f'{current_app.config["SERVER_URL"]}/u/{actor}', create_if_not_found=False)
    if user is None:
        abort(404)

    if user.id == current_user.id:
        flash(_('You cannot unblock yourself.'), 'error')
    else:
        existing_block = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=user.id).first()
        if existing_block:
            db.session.delete(existing_block)
            db.session.commit()

        if not user.is_local():
            ...
            # federate unblock

        flash(_('%(actor)s has been unblocked.', actor=actor))
        cache.delete_memoized(blocked_users, current_user.id)

    if request.headers.get('HX-Request'):
        resp = make_response()
        curr_url = request.headers.get('HX-Current-Url')
        resp.headers['HX-Redirect'] = curr_url

        return resp

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/u/<actor>/report', methods=['GET', 'POST'])
@login_required
def report_profile(actor):
    if '@' in actor:
        user = find_actor_or_create(actor, create_if_not_found=False)
    else:
        user = find_actor_or_create(f'{current_app.config["SERVER_URL"]}/u/{actor}', create_if_not_found=False)
    if user is None:
        abort(404)
    form = ReportUserForm()

    if user and user.reports == -1:  # When a mod decides to ignore future reports, user.reports is set to -1
        flash(_('Moderators have already assessed reports regarding this person, no further reports are necessary.'),
              'warning')

    if user and not user.banned:
        if form.validate_on_submit():

            if user.reports == -1:
                flash(_('%(user_name)s has already been reported, thank you!', user_name=actor))
                goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
                return redirect(goto)

            source_instance = Instance.query.get(user.instance_id)
            targets_data = {'gen': '0',
                            'suspect_user_id': user.id,
                            'suspect_user_user_name': user.ap_id if user.ap_id else user.user_name,
                            'source_instance_id': user.instance_id,
                            'source_instance_domain': source_instance.domain,
                            'reporter_id': current_user.id,
                            'reporter_user_name': current_user.user_name
                            }
            report = Report(reasons=form.reasons_to_string(form.reasons.data),
                            description=form.description.data,
                            type=REPORT_TYPE_USER,
                            reporter_id=current_user.id,
                            suspect_user_id=user.id, 
                            source_instance_id=1,
                            targets=targets_data)
            db.session.add(report)

            # Notify site admin
            already_notified = set()
            for admin in Site.admins():
                if admin.id not in already_notified:
                    notify = Notification(title='Reported user', url='/admin/reports', user_id=admin.id,
                                          author_id=current_user.id, notif_type=NOTIF_REPORT,
                                          subtype='user_reported',
                                          targets=targets_data)
                    db.session.add(notify)
                    admin.unread_notifications += 1
            user.reports += 1
            db.session.commit()

            # todo: federate report to originating instance
            if not user.is_local() and form.report_remote.data:
                ...

            flash(_('%(user_name)s has been reported, thank you!', user_name=actor))
            goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
            return redirect(goto)
        elif request.method == 'GET':
            form.report_remote.data = True

    return render_template('user/user_report.html', title=_('Report user'), form=form, user=user)


@bp.route('/u/<actor>/delete', methods=['POST'])
@login_required
def delete_profile(actor):
    if user_access('manage users', current_user.id):
        actor = actor.strip()
        if '@' in actor:
            user = find_actor_or_create(actor, create_if_not_found=False)
        else:
            user = find_actor_or_create(f'{current_app.config["SERVER_URL"]}/u/{actor}',
                                        create_if_not_found=False)
        if user is None:
            abort(404)
        if user.id == current_user.id:
            flash(_('You cannot delete yourself.'), 'error')
        else:
            if user.id == 1:
                flash('This user cannot be deleted.')
                return redirect(request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}')
            user.banned = True
            user.deleted = True
            user.deleted_by = current_user.id
            user.delete_dependencies()
            db.session.commit()

            add_to_modlog('delete_user', actor=current_user, target_user=user, link_text=user.display_name(), link=user.link())

            if user.is_instance_admin():
                flash(_('Deleted user was a remote instance admin.'), 'warning')
            if user.is_admin() or user.is_staff():
                flash(_('Deleted user with role permissions.'), 'warning')
            flash(_('%(actor)s has been deleted.', actor=actor))
    else:
        abort(401)

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/user/community/<int:community_id>/unblock', methods=['POST'])
@login_required
def user_community_unblock(community_id):
    community = Community.query.get_or_404(community_id)
    existing_block = CommunityBlock.query.filter_by(user_id=current_user.id, community_id=community.id).first()
    if existing_block:
        db.session.delete(existing_block)
        db.session.commit()
        cache.delete_memoized(blocked_communities, current_user.id)
        flash(_('%(community_name)s has been unblocked.', community_name=community.display_name()))

    if request.headers.get('HX-Request'):
        resp = make_response()
        curr_url = request.headers.get('HX-Current-Url')

        if "/user/" in curr_url:
            resp.headers['HX-Redirect'] = curr_url
        else:
            resp.headers['HX-Redirect'] = url_for("main.index")

        return resp

    goto = request.args.get('redirect') if 'redirect' in request.args else url_for('user.user_settings_filters')
    return redirect(goto)


@bp.route('/delete_account', methods=['GET', 'POST'])
@login_required
def delete_account():
    form = DeleteAccountForm()
    if form.validate_on_submit():
        files = File.query.join(Post).filter(Post.user_id == current_user.id).all()
        for file in files:
            file.delete_from_disk()
            file.source_url = ''
        if current_user.avatar_id:
            current_user.avatar.delete_from_disk()
            current_user.avatar.source_url = ''
        if current_user.cover_id:
            current_user.cover.delete_from_disk()
            current_user.cover.source_url = ''

        current_user.banned = True
        current_user.email = f'deleted_{current_user.id}@deleted.com'
        current_user.deleted_by = current_user.id
        db.session.commit()

        if current_app.debug:
            send_deletion_requests(current_user.id)
        else:
            send_deletion_requests.delay(current_user.id)

        logout_user()
        flash(_('Account deletion in progress. Give it a few minutes.'), 'success')
        return redirect(url_for('main.index'))
    elif request.method == 'GET':
        if current_user.id == 1:
            flash('This user cannot be deleted.')
            return redirect(url_for('main.index'))

    return render_template('user/delete_account.html', title=_('Delete my account'), form=form, user=current_user)


@celery.task
def send_deletion_requests(user_id):
    user = User.query.get(user_id)
    if user:
        # unsubscribe
        communities = CommunityMember.query.filter_by(user_id=user_id).all()
        for membership in communities:
            community = Community.query.get(membership.community_id)
            unsubscribe_from_community(community, user)

        instances = Instance.query.filter(Instance.dormant == False, Instance.gone_forever == False).all()
        payload = {
            "@context": default_context(),
            "actor": user.public_url(),
            "id": f"{current_app.config['SERVER_URL']}/activities/delete/{gibberish(15)}",
            "object": user.public_url(),
            "to": [
                "https://www.w3.org/ns/activitystreams#Public"
            ],
            "removeData": True,
            "type": "Delete"
        }
        for instance in instances:
            if instance.inbox and instance.online() and instance.id != 1:  # instance id 1 is always the current instance
                send_post_request(instance.inbox, payload, user.private_key, f"{user.public_url()}#main-key")

        user.banned = True
        user.deleted = True

        db.session.commit()


@bp.route('/notifications', methods=['GET', 'POST'])
@login_required
def notifications():
    # Update unread notifications count, just to be sure
    current_user.unread_notifications = Notification.query.filter_by(user_id=current_user.id, read=False).count()
    db.session.commit()

    type_ = request.args.get('type', '')
    current_filter = type_
    has_notifications = False

    notification_types = defaultdict(int)
    notification_links = defaultdict(set)
    notification_list = Notification.query.filter_by(user_id=current_user.id).order_by(
        desc(Notification.created_at)).limit(50).all()

    # Build a list of the types of notifications this person has, by going through all their notifications
    unread = 0
    for notification in notification_list:
        has_notifications = True
        if notification.notif_type != NOTIF_DEFAULT:
            if notification.read:
                notification_types[notif_id_to_string(notification.notif_type)] += 0
            else:
                notification_types[notif_id_to_string(notification.notif_type)] += 1
                unread += 1
            notification_links[notif_id_to_string(notification.notif_type)].add(notification.notif_type)

    notification_list = Notification.query.filter_by(user_id=current_user.id)

    # filter by type
    if type_:
        if type_ == 'Unread':
            notification_list = notification_list.filter(Notification.read == False)
        else:
            type_ = tuple(int(x.strip()) for x in type_.strip('{}').split(','))  # convert '{41, 10}' to a tuple containing 41 and 10
            notification_list = notification_list.filter(Notification.notif_type.in_(type_))

    notification_list = notification_list.order_by(desc(Notification.created_at)).limit(50)

    return render_template('user/notifications.html', title=_('Notifications'), notifications=notification_list,
                           notification_types=notification_types, has_notifications=has_notifications, unread=unread,
                           user=current_user, notification_links=notification_links, current_filter=current_filter,
                           site=g.site, markdown_to_html=markdown_to_html,
                           )


@bp.route('/notification/<int:notification_id>/goto', methods=['GET', 'POST'])
@login_required
def notification_goto(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id == current_user.id:
        if not notification.read:
            current_user.unread_notifications -= 1
        notification.read = True
        db.session.commit()
        return redirect(notification.url)
    else:
        abort(403)


@bp.route('/notification/<int:notification_id>/delete', methods=['GET', 'POST'])
@login_required
def notification_delete(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id == current_user.id:
        if not notification.read:
            current_user.unread_notifications -= 1
        db.session.delete(notification)
        db.session.commit()
        if request.headers.get("HX-Request"):
            return ""
    return redirect(url_for('user.notifications'))


@bp.route('/notification/<int:notification_id>/read', methods=['POST'])
@login_required
def notification_read(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id == current_user.id:
        if not notification.read:
            current_user.unread_notifications -= 1
        notification.read = True
        db.session.commit()
        return render_template(f"user/notifs/{notification.notif_type}.html", notification=notification,
                               markdown_to_html=markdown_to_html)
    else:
        abort(403)


@bp.route('/notification/<int:notification_id>/unread', methods=['POST'])
@login_required
def notification_unread(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id == current_user.id:
        if notification.read:
            current_user.unread_notifications += 1
        notification.read = False
        db.session.commit()
        return render_template(f"user/notifs/{notification.notif_type}.html", notification=notification,
                               markdown_to_html=markdown_to_html)
    else:
        abort(403)


@bp.route('/notifications/all_read', methods=['GET', 'POST'])
@login_required
def notifications_all_read():
    notif_type = request.args.get('type', '')
    original_notif_type = notif_type
    if notif_type == '' or notif_type == 'Unread':
        db.session.execute(text('UPDATE notification SET read=true WHERE user_id = :user_id'),
                           {'user_id': current_user.id})
    else:
        notif_type = tuple(int(x.strip()) for x in
                           notif_type.strip('{}').split(','))  # convert '{41, 10}' to a tuple containing 41 and 10
        db.session.execute(
            text('UPDATE notification SET read=true WHERE notif_type IN :notif_type AND user_id = :user_id'),
            {'notif_type': notif_type, 'user_id': current_user.id})
    db.session.commit()
    flash(_('All notifications marked as read.'))
    return redirect(url_for('user.notifications', type=original_notif_type))


def import_settings(redis_key):
    if current_app.debug:
        import_settings_task(current_user.id, redis_key)
    else:
        import_settings_task.delay(current_user.id, redis_key)


@celery.task
def import_settings_task(user_id, redis_key):
    from app.api.alpha.utils.misc import get_resolve_object

    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                from app import redis_client
                user = session.query(User).get(user_id)
                contents = redis_client.get(redis_key)
                contents_json = json.loads(contents)

                # Follow communities
                for community_ap_id in contents_json['followed_communities'] if 'followed_communities' in contents_json else []:
                    community = find_actor_or_create(community_ap_id, community_only=True)
                    if community:
                        if community.posts.count() == 0:
                            server, name = extract_domain_and_actor(community.ap_profile_id)
                            if current_app.debug:
                                retrieve_mods_and_backfill(community.id, server, name)
                            else:
                                retrieve_mods_and_backfill.delay(community.id, server, name)
                        if community_membership(user, community) != SUBSCRIPTION_MEMBER and community_membership(
                                user, community) != SUBSCRIPTION_PENDING:
                            if not community.is_local():
                                # send ActivityPub message to remote community, asking to follow. Accept message will be sent to our shared inbox
                                join_request = CommunityJoinRequest(user_id=user.id, community_id=community.id)
                                session.add(join_request)
                                existing_member = session.query(CommunityMember).filter_by(user_id=user.id,
                                                                                  community_id=community.id).first()
                                if not existing_member:
                                    member = CommunityMember(user_id=user.id, community_id=community.id)
                                    session.add(member)
                                    if community.subscriptions_count is None:
                                        community.subscriptions_count = 0
                                    if community.total_subscriptions_count is None:
                                        community.total_subscriptions_count = 0
                                    community.subscriptions_count += 1
                                    community.total_subscriptions_count += 1
                                    session.commit()
                                if not community.instance.gone_forever:
                                    follow = {
                                        "actor": user.public_url(),
                                        "to": [community.public_url()],
                                        "object": community.public_url(),
                                        "type": "Follow",
                                        "id": f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
                                    }
                                    send_post_request(community.ap_inbox_url, follow, user.private_key,
                                                      user.public_url() + '#main-key')
                            else:  # for local communities, joining is instant
                                banned = session.query(CommunityBan).filter_by(user_id=user.id, community_id=community.id).first()
                                if not banned:
                                    existing_member = session.query(CommunityMember).filter_by(user_id=user.id,
                                                                                      community_id=community.id).first()
                                    if not existing_member:
                                        member = CommunityMember(user_id=user.id, community_id=community.id)
                                        session.add(member)
                                        community.subscriptions_count += 1
                                        community.total_subscriptions_count += 1
                                        session.commit()
                            cache.delete_memoized(community_membership, user, community)

                for community_ap_id in contents_json['blocked_communities'] if 'blocked_communities' in contents_json else []:
                    community = find_actor_or_create(community_ap_id, community_only=True)
                    if community:
                        existing_block = session.query(CommunityBlock).filter_by(user_id=user.id, community_id=community.id).first()
                        if not existing_block:
                            block = CommunityBlock(user_id=user.id, community_id=community.id)
                            session.add(block)


                for user_ap_id in contents_json['blocked_users'] if 'blocked_users' in contents_json else []:
                    blocked_user = find_actor_or_create(user_ap_id)
                    if blocked_user:
                        existing_block = session.query(UserBlock).filter_by(blocker_id=user.id, blocked_id=blocked_user.id).first()
                        if not existing_block:
                            user_block = UserBlock(blocker_id=user.id, blocked_id=blocked_user.id)
                            session.add(user_block)
                            if not blocked_user.is_local():
                                ...  # todo: federate block

                for user_note in contents_json['user_notes'] if 'user_notes' in contents_json else []:
                    note_target = find_actor_or_create(user_note['target'])
                    if note_target:
                        session.add(UserNote(user_id=user.id, target_id=note_target.id, body=user_note['body']))

                cache.delete_memoized(user_notes, user.id)

                for instance_domain in contents_json['blocked_instances'] if 'blocked_instances' in contents_json else []:
                    instance = Instance.query.filter(Instance.domain == instance_domain).first()
                    if instance:
                        session.add(InstanceBlock(user_id=user.id, instance_id=instance.id))

                for ap_id in contents_json['saved_posts'] if 'saved_posts' in contents_json else []:
                    try:
                        post = get_resolve_object(None, {"q": ap_id}, user_id=user.id, recursive=True)
                        if post:
                            existing_bookmark = session.query(PostBookmark).filter_by(post_id=post.id, user_id=user.id).first()
                            if not existing_bookmark:
                                session.add(PostBookmark(post_id=post.id, user_id=user.id))
                    except Exception:
                        continue

                for ap_id in contents_json['saved_comments'] if 'saved_comments' in contents_json else []:
                    try:
                        reply = get_resolve_object(None, {"q": ap_id}, user_id=user.id, recursive=True)
                        if reply:
                            existing_bookmark = session.query(PostReplyBookmark).filter_by(post_reply_id=reply.id, user_id=user.id).first()
                            if not existing_bookmark:
                                session.add(PostReplyBookmark(post_reply_id=reply.id, user_id=user.id))
                    except Exception:
                        continue

                session.commit()
                cache.delete_memoized(blocked_communities, user.id)
                cache.delete_memoized(blocked_or_banned_instances, user.id)
                cache.delete_memoized(blocked_users, user.id)
                cache.delete_memoized(blocked_domains, user.id)
                from app.api.alpha.views import user_view
                cache.delete_memoized(user_view)

                redis_client.delete(redis_key)

        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@bp.route('/user/settings/filters', methods=['GET', 'POST'])
@login_required
def user_settings_filters():
    form = FilterForm()
    if form.validate_on_submit():
        if (form.hide_nsfw.data != 1 or form.hide_nsfl.data != 1) and user_in_restricted_country(current_user):
            flash(_('NSFW content will be hidden due to legal restrictions in your country.'))
        current_user.ignore_bots = form.ignore_bots.data
        current_user.hide_nsfw = form.hide_nsfw.data
        current_user.hide_nsfl = form.hide_nsfl.data
        current_user.hide_gen_ai = form.hide_gen_ai.data
        current_user.reply_collapse_threshold = form.reply_collapse_threshold.data
        current_user.reply_hide_threshold = form.reply_hide_threshold.data
        current_user.hide_low_quality = form.hide_low_quality.data
        current_user.community_keyword_filter = form.community_keyword_filter.data
        if current_user.ip_address_country and user_in_restricted_country(current_user):
            current_user.hide_nsfw = 1  # Hide nsfw
            current_user.hide_nsfl = 1

        db.session.commit()

        cache.delete_memoized(filtered_out_communities, current_user)

        flash(_('Your changes have been saved.'), 'success')
        return redirect(url_for('user.user_settings_filters'))

    elif request.method == 'GET':
        form.ignore_bots.data = current_user.ignore_bots
        form.hide_nsfw.data = current_user.hide_nsfw
        form.hide_nsfl.data = current_user.hide_nsfl
        form.hide_gen_ai.data = current_user.hide_gen_ai
        form.reply_collapse_threshold.data = current_user.reply_collapse_threshold
        form.reply_hide_threshold.data = current_user.reply_hide_threshold
        form.hide_low_quality.data = current_user.hide_low_quality
        form.community_keyword_filter.data = current_user.community_keyword_filter
    filters = Filter.query.filter_by(user_id=current_user.id).order_by(Filter.title).all()
    blocked_users = User.query.filter_by(deleted=False).join(UserBlock, UserBlock.blocked_id == User.id). \
        filter(UserBlock.blocker_id == current_user.id).order_by(User.user_name).all()
    blocked_communities = Community.query.join(CommunityBlock, CommunityBlock.community_id == Community.id). \
        filter(CommunityBlock.user_id == current_user.id).order_by(Community.title).all()
    blocked_domains = Domain.query.join(DomainBlock, DomainBlock.domain_id == Domain.id). \
        filter(DomainBlock.user_id == current_user.id).order_by(Domain.name).all()
    blocked_instances = Instance.query.join(InstanceBlock, InstanceBlock.instance_id == Instance.id). \
        filter(InstanceBlock.user_id == current_user.id).order_by(Instance.domain).all()

    return render_template('user/filters.html', title=_('Filters'), form=form, filters=filters, user=current_user,
                           blocked_users=blocked_users, blocked_communities=blocked_communities,
                           blocked_domains=blocked_domains, blocked_instances=blocked_instances)


@bp.route('/user/settings/filters/add', methods=['GET', 'POST'])
@login_required
def user_settings_filters_add():
    form = KeywordFilterEditForm()
    form.filter_replies.render_kw = {'disabled': True}
    if form.validate_on_submit():
        content_filter = Filter(title=form.title.data, filter_home=form.filter_home.data,
                                filter_posts=form.filter_posts.data,
                                filter_replies=form.filter_replies.data, hide_type=form.hide_type.data,
                                keywords=form.keywords.data,
                                expire_after=form.expire_after.data, user_id=current_user.id)
        db.session.add(content_filter)
        db.session.commit()
        cache.delete_memoized(user_filters_home, current_user.id)
        cache.delete_memoized(user_filters_posts, current_user.id)
        cache.delete_memoized(user_filters_replies, current_user.id)
        cache.delete_memoized(filtered_out_communities, current_user)

        flash(_('Your changes have been saved.'), 'success')
        return redirect(url_for('user.user_settings_filters'))

    return render_template('user/edit_filters.html', title=_('Add filter'), form=form, user=current_user)


@bp.route('/user/settings/filters/<int:filter_id>/edit', methods=['GET', 'POST'])
@login_required
def user_settings_filters_edit(filter_id):
    content_filter = Filter.query.get_or_404(filter_id)
    if current_user.id != content_filter.user_id:
        abort(401)
    form = KeywordFilterEditForm()
    form.filter_replies.render_kw = {'disabled': True}
    if form.validate_on_submit():
        content_filter.title = form.title.data
        content_filter.filter_home = form.filter_home.data
        content_filter.filter_posts = form.filter_posts.data
        content_filter.filter_replies = form.filter_replies.data
        content_filter.hide_type = form.hide_type.data
        content_filter.keywords = form.keywords.data
        content_filter.expire_after = form.expire_after.data
        db.session.commit()
        cache.delete_memoized(user_filters_home, current_user.id)
        cache.delete_memoized(user_filters_posts, current_user.id)
        cache.delete_memoized(user_filters_replies, current_user.id)
        cache.delete_memoized(filtered_out_communities, current_user)

        flash(_('Your changes have been saved.'), 'success')

        return redirect(url_for('user.user_settings_filters'))
    elif request.method == 'GET':
        form.title.data = content_filter.title
        form.filter_home.data = content_filter.filter_home
        form.filter_posts.data = content_filter.filter_posts
        form.filter_replies.data = content_filter.filter_replies
        form.hide_type.data = content_filter.hide_type
        form.keywords.data = content_filter.keywords
        form.expire_after.data = content_filter.expire_after

    return render_template('user/edit_filters.html', title=_('Edit filter'), form=form, content_filter=content_filter,
                           user=current_user)


@bp.route('/user/settings/filters/<int:filter_id>/delete', methods=['POST'])
@login_required
def user_settings_filters_delete(filter_id):
    content_filter = Filter.query.get_or_404(filter_id)
    if current_user.id != content_filter.user_id:
        abort(401)
    db.session.delete(content_filter)
    db.session.commit()
    cache.delete_memoized(user_filters_home, current_user.id)
    cache.delete_memoized(user_filters_posts, current_user.id)
    cache.delete_memoized(user_filters_replies, current_user.id)
    cache.delete_memoized(filtered_out_communities, current_user)
    flash(_('Filter deleted.'))
    return redirect(url_for('user.user_settings_filters'))


@bp.route('/user/settings/block/user', methods=['GET', 'POST'])
@login_required
def user_settings_block_user():
    form = BlockUserForm()
    if form.validate_on_submit():
        username = form.username.data.strip()

        # Try to find the user - handle both local usernames and ActivityPub IDs
        user_to_block = None

        # Check if it's an ActivityPub ID (contains @ or is a URL)
        if '@' in username or username.startswith('http'):
            # Try to find or create the remote user
            user_to_block = find_actor_or_create(username)
            if user_to_block and not isinstance(user_to_block, User):
                user_to_block = None
        else:
            # Local username lookup
            user_to_block = User.query.filter_by(user_name=username, deleted=False, ap_id=None).first()

        if not user_to_block:
            flash(_('User not found: %(username)s', username=username), 'error')
            return render_template('user/block_user.html', form=form, user=current_user)

        if user_to_block.id == current_user.id:
            flash(_('You cannot block yourself.'), 'error')
            return render_template('user/block_user.html', form=form, user=current_user)

        # Check if already blocked
        existing = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=user_to_block.id).first()
        if existing:
            flash(_('%(name)s is already blocked.', name=user_to_block.display_name()), 'warning')
        else:
            db.session.add(UserBlock(blocker_id=current_user.id, blocked_id=user_to_block.id))
            db.session.commit()
            cache.delete_memoized(blocked_users, current_user.id)
            flash(_('%(name)s has been blocked.', name=user_to_block.display_name()), 'success')

        return redirect(url_for('user.user_settings_filters'))

    return render_template('user/block_user.html', form=form, user=current_user)


@bp.route('/user/settings/block/community', methods=['GET', 'POST'])
@login_required
def user_settings_block_community():
    form = BlockCommunityForm()
    if form.validate_on_submit():
        community_name = form.community_name.data.strip()

        # Try to find the community
        community_to_block = None

        # Check if it's an ActivityPub ID (contains ! or @ or is a URL)
        if '!' in community_name or '@' in community_name or community_name.startswith('http'):
            if community_name.startswith('!') or ('@' in community_name and not community_name.startswith('http')):
                if not community_name.startswith('!'):
                    community_name = '!' + community_name
                # Will work for !comm@instance.tld as well as comm@instance.tld
                community_to_block = search_for_community(community_name, allow_fetch=False)
            else:
                # Try to find or create the remote community
                community_to_block = find_actor_or_create(community_name, create_if_not_found=False, community_only=True)
            if community_to_block and not isinstance(community_to_block, Community):
                community_to_block = None
        else:
            # Local community lookup
            community_to_block = Community.query.filter_by(name=community_name).first()

        if not community_to_block:
            flash(_('Community not found: %(name)s', name=community_name), 'error')
            return render_template('user/block_community.html', form=form, user=current_user)

        # Check if already blocked
        existing = CommunityBlock.query.filter_by(user_id=current_user.id, community_id=community_to_block.id).first()
        if existing:
            flash(_('%(name)s is already blocked.', name=community_to_block.display_name()), 'warning')
        else:
            db.session.add(CommunityBlock(user_id=current_user.id, community_id=community_to_block.id))
            db.session.commit()
            cache.delete_memoized(blocked_communities, current_user.id)
            flash(_('Posts in %(name)s will be hidden.', name=community_to_block.display_name()), 'success')

        return redirect(url_for('user.user_settings_filters'))

    return render_template('user/block_community.html', form=form, user=current_user)


@bp.route('/user/settings/block/domain', methods=['GET', 'POST'])
@login_required
def user_settings_block_domain():
    form = BlockDomainForm()
    if form.validate_on_submit():
        domain_name = form.domain_name.data.strip().lower()

        # Remove protocol if present
        domain_name = domain_name.replace('https://', '').replace('http://', '')
        # Remove trailing slash if present
        domain_name = domain_name.rstrip('/')

        # Find or create the domain
        domain = Domain.query.filter_by(name=domain_name).first()
        if not domain:
            domain = Domain(name=domain_name)
            db.session.add(domain)
            db.session.commit()

        # Check if already blocked
        existing = DomainBlock.query.filter_by(user_id=current_user.id, domain_id=domain.id).first()
        if existing:
            flash(_('%(name)s is already blocked.', name=domain_name), 'warning')
        else:
            db.session.add(DomainBlock(user_id=current_user.id, domain_id=domain.id))
            db.session.commit()
            cache.delete_memoized(blocked_domains, current_user.id)
            flash(_('Posts linking to %(name)s will be hidden.', name=domain_name), 'success')

        return redirect(url_for('user.user_settings_filters'))

    return render_template('user/block_domain.html', form=form, user=current_user)


@bp.route('/user/settings/block/instance', methods=['GET', 'POST'])
@login_required
def user_settings_block_instance():
    form = BlockInstanceForm()
    if form.validate_on_submit():
        instance_domain = form.instance_domain.data.strip().lower()

        # Remove protocol if present
        instance_domain = instance_domain.replace('https://', '').replace('http://', '')
        # Remove trailing slash if present
        instance_domain = instance_domain.rstrip('/')

        # Find the instance
        instance = Instance.query.filter_by(domain=instance_domain).first()
        if not instance:
            flash(_('Instance not found: %(domain)s', domain=instance_domain), 'error')
            return render_template('user/block_instance.html', form=form, user=current_user)

        # Use the existing block_remote_instance function
        try:
            block_remote_instance(instance.id, SRC_WEB)
            flash(_('Content from %(name)s will be hidden.', name=instance_domain), 'success')
        except Exception as e:
            flash(_('Error blocking instance: %(error)s', error=str(e)), 'error')

        return redirect(url_for('user.user_settings_filters'))

    return render_template('user/block_instance.html', form=form, user=current_user)


@bp.route('/user/newsletter/<int:user_id>/<token>/unsubscribe', methods=['GET', 'POST'])
def user_newsletter_unsubscribe(user_id, token):
    user = User.query.filter(User.id == user_id, User.verification_token == token).first()
    if user:
        user.newsletter = False
        db.session.commit()
    return render_template('user/newsletter_unsubscribed.html')


@bp.route('/user/email_notifs/<int:user_id>/<token>/unsubscribe', methods=['GET', 'POST'])
def user_email_notifs_unsubscribe(user_id, token):
    user = User.query.filter(User.id == user_id, User.verification_token == token).first()
    if user:
        user.email_unread = False
        db.session.commit()
    return render_template('user/email_notifs_unsubscribed.html')


@bp.route('/bookmarks')
@login_required
def user_bookmarks():
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    posts = Post.query.filter(Post.status > POST_STATUS_REVIEWING).join(PostBookmark, PostBookmark.post_id == Post.id). \
        filter(PostBookmark.user_id == current_user.id).order_by(desc(PostBookmark.created_at))

    posts = posts.paginate(page=page, per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                           error_out=False)
    next_url = url_for('user.user_bookmarks', page=posts.next_num) if posts.has_next else None
    prev_url = url_for('user.user_bookmarks', page=posts.prev_num) if posts.has_prev and page != 1 else None

    # Voting history
    recently_upvoted = recently_upvoted_posts(current_user.id)
    recently_downvoted = recently_downvoted_posts(current_user.id)

    return render_template('user/bookmarks.html', title=_('Bookmarks'), posts=posts, show_post_community=True,
                           low_bandwidth=low_bandwidth, user=current_user,
                           recently_upvoted=recently_upvoted, recently_downvoted=recently_downvoted,
                           next_url=next_url, prev_url=prev_url)


@bp.route('/bookmarks/comments')
@login_required
def user_bookmarks_comments():
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    post_replies = PostReply.query.filter(PostReply.deleted == False).join(PostReplyBookmark,
                                                                           PostReplyBookmark.post_reply_id == PostReply.id). \
        filter(PostReplyBookmark.user_id == current_user.id).order_by(desc(PostReplyBookmark.created_at))

    post_replies = post_replies.paginate(page=page,
                                         per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                                         error_out=False)
    next_url = url_for('user.user_bookmarks_comments', page=post_replies.next_num) if post_replies.has_next else None
    prev_url = url_for('user.user_bookmarks_comments',
                       page=post_replies.prev_num) if post_replies.has_prev and page != 1 else None

    # Voting history
    recently_upvoted_replies = recently_upvoted_post_replies(current_user.id)
    recently_downvoted_replies = recently_downvoted_post_replies(current_user.id)

    return render_template('user/bookmarks_comments.html', title=_('Comment bookmarks'), post_replies=post_replies,
                           show_post_community=True,
                           low_bandwidth=low_bandwidth, user=current_user,
                           recently_upvoted_replies=recently_upvoted_replies,
                           recently_downvoted_replies=recently_downvoted_replies,
                           next_url=next_url, prev_url=prev_url)


@bp.route('/alerts')
@bp.route('/alerts/<type>/<filter>')
@login_required
def user_alerts(type='posts', filter='all'):
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    if type == 'comments':
        if filter == 'mine':
            entities = PostReply.query.filter_by(deleted=False, user_id=current_user.id). \
                join(NotificationSubscription, NotificationSubscription.entity_id == PostReply.id). \
                filter_by(type=NOTIF_REPLY, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        elif filter == 'others':
            entities = PostReply.query.filter(PostReply.deleted == False, PostReply.user_id != current_user.id). \
                join(NotificationSubscription, NotificationSubscription.entity_id == PostReply.id). \
                filter_by(type=NOTIF_REPLY, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        else:  # default to 'all' filter
            entities = PostReply.query.filter_by(deleted=False). \
                join(NotificationSubscription, NotificationSubscription.entity_id == PostReply.id). \
                filter_by(type=NOTIF_REPLY, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        title = _('Reply Alerts')

    elif type == 'communities':
        if filter == 'mine':
            entities = Community.query. \
                join(CommunityMember, CommunityMember.community_id == Community.id). \
                filter_by(user_id=current_user.id, is_moderator=True). \
                join(NotificationSubscription, NotificationSubscription.entity_id == CommunityMember.community_id). \
                filter_by(type=NOTIF_COMMUNITY, user_id=current_user.id).order_by(
                desc(NotificationSubscription.created_at))
        elif filter == 'others':
            entities = Community.query. \
                join(CommunityMember, CommunityMember.community_id == Community.id). \
                filter_by(user_id=current_user.id, is_moderator=False). \
                join(NotificationSubscription, NotificationSubscription.entity_id == CommunityMember.community_id). \
                filter_by(type=NOTIF_COMMUNITY, user_id=current_user.id).order_by(
                desc(NotificationSubscription.created_at))
        else:  # default to 'all' filter
            entities = Community.query. \
                join(NotificationSubscription, NotificationSubscription.entity_id == Community.id). \
                filter_by(type=NOTIF_COMMUNITY, user_id=current_user.id).order_by(
                desc(NotificationSubscription.created_at))
        title = _('Community Alerts')

    elif type == 'topics':
        # ignore filter
        entities = Topic.query.join(NotificationSubscription, NotificationSubscription.entity_id == Topic.id). \
            filter_by(type=NOTIF_TOPIC, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        title = _('Topic Alerts')

    elif type == 'feeds':
        # ignore filter
        entities = Feed.query.join(NotificationSubscription, NotificationSubscription.entity_id == Feed.id). \
            filter_by(type=NOTIF_FEED, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        title = _('Feed Alerts')

    elif type == 'users':
        # ignore filter
        entities = User.query.filter_by(deleted=False). \
            join(NotificationSubscription, NotificationSubscription.entity_id == User.id). \
            filter_by(type=NOTIF_USER, user_id=current_user.id). \
            order_by(desc(NotificationSubscription.created_at))
        title = _('User Alerts')

    else:  # default to 'posts' type
        if filter == 'mine':
            entities = Post.query.filter_by(deleted=False, user_id=current_user.id). \
                join(NotificationSubscription, NotificationSubscription.entity_id == Post.id). \
                filter_by(type=NOTIF_POST, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        elif filter == 'others':
            entities = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                         Post.user_id != current_user.id). \
                join(NotificationSubscription, NotificationSubscription.entity_id == Post.id). \
                filter_by(type=NOTIF_POST, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        else:  # default to 'all' filter
            entities = Post.query.filter_by(deleted=False). \
                join(NotificationSubscription, NotificationSubscription.entity_id == Post.id). \
                filter_by(type=NOTIF_POST, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        title = _('Post Alerts')

    entities = entities.paginate(page=page, per_page=100 if not low_bandwidth else 50, error_out=False)
    next_url = url_for('user.user_alerts', page=entities.next_num, type=type,
                       filter=filter) if entities.has_next else None
    prev_url = url_for('user.user_alerts', page=entities.prev_num, type=type,
                       filter=filter) if entities.has_prev and page != 1 else None

    return render_template('user/alerts.html', title=title, entities=entities,
                           low_bandwidth=low_bandwidth, user=current_user, type=type, filter=filter,
                           next_url=next_url, prev_url=prev_url)


@bp.route('/scheduled_posts')
@login_required
def user_scheduled_posts(type='posts', filter='all'):
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    entities = Post.query.filter(Post.deleted == False, Post.status == POST_STATUS_SCHEDULED, Post.user_id == current_user.id).order_by(Post.scheduled_for).all()
    title = _('Scheduled posts')

    return render_template('user/scheduled_posts.html', title=title, entities=entities,
                           low_bandwidth=low_bandwidth, user=current_user, site=g.site,
                           )


@bp.route('/hidden_posts')
@login_required
def user_hidden_posts():
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    page = request.args.get('page', 1, type=int)
    title = _('Hidden posts')
    post_ids = db.session.execute(text('SELECT hidden_post_id FROM "hidden_posts" WHERE user_id = :user_id'),
                               {"user_id": current_user.id}).all()
    post_ids = [post_id[0] for post_id in post_ids]
    posts = Post.query.filter(Post.id.in_(post_ids))

    posts = posts.paginate(page=page, per_page=100, error_out=False)
    next_url = url_for('user.user_hidden_posts', page=posts.next_num) if posts.has_next else None
    prev_url = url_for('user.user_hidden_posts', page=posts.prev_num) if posts.has_prev and page != 1 else None

    return render_template('user/hidden_posts.html', title=title, posts=posts, low_bandwidth=low_bandwidth,
                           user=current_user, site=g.site, next_url=next_url, prev_url=prev_url)


@bp.route('/u/<actor>/fediverse_redirect', methods=['GET', 'POST'])
def fediverse_redirect(actor):
    actor = actor.strip()
    user = User.query.filter_by(user_name=actor, deleted=False, ap_id=None).first()
    if user and user.is_local():
        form = RemoteFollowForm()
        if form.validate_on_submit():
            redirect_url = ''
            if form.instance_type.data == 'mastodon':
                redirect_url = f'https://{form.instance_url.data}/@{user.user_name}@{current_app.config["SERVER_NAME"]}'
            elif form.instance_type.data == 'lemmy':
                flash(_("Lemmy can't follow profiles, sorry"), 'error')
                return render_template('user/fediverse_redirect.html', form=form, user=user, send_to='',
                                       current_app=current_app)
            elif form.instance_type.data == 'friendica':
                redirect_url = f'https://{form.instance_url.data}/search?q={user.user_name}@{current_app.config["SERVER_NAME"]}'
            elif form.instance_type.data == 'hubzilla':
                redirect_url = f'https://{form.instance_url.data}/search?q={user.user_name}@{current_app.config["SERVER_NAME"]}'
            elif form.instance_type.data == 'pixelfed':
                redirect_url = f'https://{form.instance_url.data}/i/results?q={user.user_name}@{current_app.config["SERVER_NAME"]}'

            resp = make_response(redirect(redirect_url))
            resp.set_cookie('remote_instance_url', form.instance_url.data,
                            expires=datetime(year=2099, month=12, day=30))
            return resp
        else:
            send_to = ''
            if request.cookies.get('remote_instance_url'):
                send_to = request.cookies.get('remote_instance_url')
                form.instance_url.data = send_to
            return render_template('user/fediverse_redirect.html', form=form, user=user, send_to=send_to,
                                   current_app=current_app)


@bp.route('/read-posts')
@bp.route('/read-posts/<sort>', methods=['GET', 'POST'])
@login_required
def user_read_posts(sort=None):
    if sort is None:
        sort = current_user.default_sort if current_user.is_authenticated else 'hot'
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING)

    if current_user.ignore_bots == 1:
        posts = posts.filter(Post.from_bot == False)
    if current_user.hide_nsfl == 1:
        posts = posts.filter(Post.nsfl == False)
    if current_user.hide_nsfw == 1:
        posts = posts.filter(Post.nsfw == False)

    # get the list of post.ids that the 
    # current_user has already read/voted on
    posts = posts.join(read_posts, read_posts.c.read_post_id == Post.id).filter(read_posts.c.user_id == current_user.id)

    # sort the posts
    if sort == 'hot':
        posts = posts.order_by(desc(Post.sticky)).order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort == 'top':
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=7)).order_by(desc(Post.sticky)).order_by(
            desc(Post.up_votes - Post.down_votes))
    elif sort == 'new':
        posts = posts.order_by(desc(Post.posted_at))
    elif sort == 'oldest':
        posts = posts.order_by(asc(Post.posted_at))
    elif sort == 'active':
        posts = posts.order_by(desc(Post.sticky)).order_by(desc(Post.last_active))

    posts = posts.paginate(page=page, per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                           error_out=False)
    next_url = url_for('user.user_read_posts', page=posts.next_num) if posts.has_next else None
    prev_url = url_for('user.user_read_posts', page=posts.prev_num) if posts.has_prev and page != 1 else None

    return render_template('user/read_posts.html', title=_('Read posts'), posts=posts, show_post_community=True,
                           sort=sort, low_bandwidth=low_bandwidth, user=current_user,
                           next_url=next_url, prev_url=prev_url)


@bp.route('/read-posts/delete', methods=['POST'])
@login_required
def user_read_posts_delete():
    db.session.execute(text('DELETE FROM "read_posts" WHERE user_id = :user_id'), {'user_id': current_user.id})
    db.session.commit()
    flash(_('Reading history has been deleted'))
    return redirect(url_for('user.user_read_posts'))


@bp.route('/u/<actor>/note', methods=['GET', 'POST'])
@login_required
def edit_user_note(actor):
    actor = actor.strip()
    return_to = request.args.get('return_to', '').strip()
    if return_to.startswith('http'):
        abort(401)
    if '@' in actor:
        user: User = User.query.filter_by(ap_id=actor, deleted=False).first()
    else:
        user: User = User.query.filter_by(user_name=actor, deleted=False, ap_id=None).first()
    if user is None:
        abort(404)
    form = UserNoteForm()
    if form.validate_on_submit() and not current_user.banned:
        text = form.note.data.strip()
        usernote = UserNote.query.filter(UserNote.target_id == user.id, UserNote.user_id == current_user.id).first()
        if usernote:
            usernote.body = text
        else:
            usernote = UserNote(target_id=user.id, user_id=current_user.id, body=text)
            db.session.add(usernote)
        db.session.commit()
        from app.api.alpha.views import user_view
        cache.delete_memoized(user_view)
        cache.delete_memoized(user_notes, current_user.id)

        flash(_('Your changes have been saved.'), 'success')
        if return_to:
            return redirect(return_to)
        else:
            return redirect(f'/u/{actor}')

    elif request.method == 'GET':
        form.note.data = user.get_note(current_user)

    return render_template('user/edit_note.html', title=_('Edit note'), form=form, user=user, return_to=return_to)


@bp.route('/user/<int:user_id>/preview')
def user_preview(user_id):
    user = User.query.get_or_404(user_id)
    return_to = request.args.get('return_to')
    if (user.deleted or user.banned) and current_user.is_anonymous:
        abort(404)
    return render_template('user/user_preview.html', user=user, return_to=return_to)


@bp.route('/user/lookup/<person>/<domain>')
def lookup(person, domain):
    if domain == current_app.config['SERVER_NAME']:
        return redirect('/u/' + person)

    exists = User.query.filter_by(user_name=person, ap_domain=domain).first()
    if exists:
        return redirect('/u/' + person + '@' + domain)
    else:
        address = '@' + person + '@' + domain
        if current_user.is_authenticated:
            new_person = None

            try:
                new_person = search_for_user(address)
            except Exception as e:
                if 'is blocked.' in str(e):
                    flash(_('Sorry, that instance is blocked, check https://gui.fediseer.com/ for reasons.'), 'warning')
            if not new_person or new_person.banned:
                flash(_('That person could not be retrieved or is banned from %(site)s.', site=g.site.name), 'warning')
                referrer = request.headers.get('Referer', None)
                if referrer is not None:
                    return redirect(referrer)
                else:
                    return redirect('/')

            return redirect('/u/' + new_person.ap_id)
        else:
            # send them back where they came from
            flash(_('Searching for remote people requires login'), 'error')
            referrer = request.headers.get('Referer', None)
            if referrer is not None:
                return redirect(referrer)
            else:
                return redirect('/')


# ----- user feed related routes

@bp.route('/u/<actor>/myfeeds', methods=['GET'])
@login_required
def user_myfeeds(actor):
    # this will show a user's personal feeds
    user_has_feeds = False
    if current_user.is_authenticated and len(Feed.query.filter_by(user_id=current_user.id).all()) > 0:
        user_has_feeds = True
    current_user_feeds = Feed.query.filter_by(user_id=current_user.id)

    # this is for feeds the user is subscribed to
    user_has_feed_subscriptions = False
    if current_user.is_authenticated and len(Feed.query.join(FeedMember, Feed.id == FeedMember.feed_id).filter(
            FeedMember.user_id == current_user.id).all()) > 0:
        user_has_feed_subscriptions = True
    subbed_feeds = Feed.query.join(FeedMember, Feed.id == FeedMember.feed_id).filter(
        FeedMember.user_id == current_user.id).filter_by(is_owner=False)

    return render_template('user/user_feeds.html', user_has_feeds=user_has_feeds, user_feeds_list=current_user_feeds,
                           user_has_feed_subscriptions=user_has_feed_subscriptions,
                           subbed_feeds=subbed_feeds,
                           )


@bp.route('/u/<actor>/feeds', methods=['GET'])
def user_feeds(actor):
    # this will show a specific user's public feeds
    user_has_public_feeds = False

    actor = actor.strip()
    if '@' in actor:
        user = find_actor_or_create(actor, create_if_not_found=False)
    else:
        user = find_actor_or_create(f'{current_app.config["SERVER_URL"]}/u/{actor}', create_if_not_found=False)

    if user is None:
        abort(404)

    # find all user feeds marked as public
    user_public_feeds = Feed.query.filter_by(public=True).filter_by(user_id=user.id).all()

    if len(user_public_feeds) > 0:
        user_has_public_feeds = True

    return render_template('user/user_public_feeds.html', user_has_public_feeds=user_has_public_feeds,
                           creator_name=user.user_name, user_feeds_list=user_public_feeds
                           )


# RSS feed of the community
@bp.route('/u/<actor>/feed', methods=['GET'])
@cache.cached(timeout=600)
def show_profile_rss(actor):
    actor = actor.strip()
    if '@' in actor:
        user = find_actor_or_create(actor, create_if_not_found=False)
    else:
        user = find_actor_or_create(f'{current_app.config["SERVER_URL"]}/u/{actor}', create_if_not_found=False)

    if user is not None:
        # If nothing has changed since their last visit, return HTTP 304
        current_etag = f"{user.id}_{hash(user.last_seen)}"
        if request_etag_matches(current_etag):
            return return_304(current_etag, 'application/rss+xml')

        posts = user.posts.filter(Post.from_bot == False, Post.deleted == False,
                                  Post.status > POST_STATUS_REVIEWING).order_by(desc(Post.created_at)).limit(20).all()
        description = shorten_string(user.about, 150) if user.about else None
        og_image = user.avatar_image() if user.avatar_id else None
        fg = FeedGenerator()
        fg.id(f"{current_app.config['SERVER_URL']}/c/{actor}")
        fg.title(f'{user.display_name()} on {g.site.name}')
        fg.link(href=f"{current_app.config['SERVER_URL']}/c/{actor}", rel='alternate')
        if og_image:
            fg.logo(og_image)
        else:
            fg.logo(f"{current_app.config['SERVER_URL']}/static/images/apple-touch-icon.png")
        if description:
            fg.subtitle(description)
        else:
            fg.subtitle(' ')
        fg.link(href=f"{current_app.config['SERVER_URL']}/c/{actor}/feed", rel='self')
        fg.language('en')

        already_added = set()
        for post in posts:
            # Validate title and body - skip this post if invalid
            if not is_valid_xml_utf8(post.title.strip()):
                continue
            if post.body_html is None:
                continue
            if post.body_html.strip() and not is_valid_xml_utf8(post.body_html.strip()):
                continue
            
            fe = fg.add_entry()
            fe.title(post.title.strip())
            if post.slug:
                fe.link(href=f"{current_app.config['SERVER_URL']}{post.slug}")
            else:
                fe.link(href=f"{current_app.config['SERVER_URL']}/post/{post.id}")
            if post.url:
                if post.url in already_added:
                    continue
                type = mimetype_from_url(post.url)
                if type and not type.startswith('text/'):
                    fe.enclosure(post.url, type=type)
                already_added.add(post.url)
            if post.body_html.strip():
                fe.description(post.body_html.strip())
            fe.guid(post.profile_id(), permalink=True)
            fe.author(name=post.author.user_name)
            fe.pubDate(post.created_at.replace(tzinfo=timezone.utc))

        response = make_response(fg.rss_str())
        response.headers.set('Content-Type', 'application/rss+xml')
        response.headers.add_header('ETag', f"{user.id}_{hash(user.last_seen)}")
        response.headers.add_header('Cache-Control', 'no-cache, max-age=600, must-revalidate')
        return response
    else:
        abort(404)


@bp.route('/user/files', methods=['GET', 'POST'])
@login_required
def user_files():
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    total_size = 0
    file_size_dict = defaultdict(int)
    file_sizes = db.session.execute(text('SELECT file_id, size FROM "user_file" WHERE user_id = :user_id'),
                                    {'user_id': current_user.id}).all()
    for fs in file_sizes:
        total_size += fs[1]
        file_size_dict[fs[0]] = fs[1]

    files = File.query.join(user_file).filter(user_file.c.file_id == File.id, user_file.c.user_id == current_user.id)

    files = files.paginate(page=page, per_page=100, error_out=False)
    next_url = url_for('user.user_files', page=files.next_num) if files.has_next else None
    prev_url = url_for('user.user_files', page=files.prev_num) if files.has_prev and page != 1 else None

    return render_template('user/files.html', title=_('Files'), files=files, file_sizes=file_size_dict,
                           total_size=total_size, max_size=current_app.config['FILE_UPLOAD_QUOTA'],
                           low_bandwidth=low_bandwidth, user=current_user,
                           next_url=next_url, prev_url=prev_url)


@bp.route('/user/files/delete/<int:file_id>', methods=['GET', 'POST'])
@login_required
def user_file_delete(file_id):
    file = File.query.get_or_404(file_id)
    form = DeleteFileForm()
    if form.validate_on_submit():
        process_file_delete(file.source_url, current_user.id)
        return redirect(form.referrer.data)

    form.referrer.data = referrer(url_for('user.user_files'))

    return render_template('user/file_delete.html', form=form, file=file, user=current_user)


@bp.route('/user/files/upload', methods=['GET', 'POST'])
@login_required
def user_file_upload():
    form = UploadFileForm()
    if form.validate_on_submit():
        if form.urls.data.strip() != '':
            urls = form.urls.data.strip().split('\n')
            for url in urls:
                if url and url.strip() != '':
                    file = File(source_url=url)
                    db.session.add(file)
                    db.session.commit()
                    db.session.execute(
                        text('INSERT INTO "user_file" (file_id, user_id, size) VALUES (:file_id, :user_id, :size)'),
                        {'file_id': file.id, 'user_id': current_user.id, 'size': 0})
                    db.session.commit()

        if form.file1.data:
            process_upload(form.file1.data, user_id=current_user.id)
        if form.file2.data:
            process_upload(form.file2.data, user_id=current_user.id)
        if form.file3.data:
            process_upload(form.file3.data, user_id=current_user.id)
        if form.file4.data:
            process_upload(form.file4.data, user_id=current_user.id)
        if form.file5.data:
            process_upload(form.file5.data, user_id=current_user.id)
        if form.file6.data:
            process_upload(form.file6.data, user_id=current_user.id)
        if form.file7.data:
            process_upload(form.file7.data, user_id=current_user.id)
        if form.file8.data:
            process_upload(form.file8.data, user_id=current_user.id)
        if form.file9.data:
            process_upload(form.file9.data, user_id=current_user.id)
        if form.file10.data:
            process_upload(form.file10.data, user_id=current_user.id)

        return redirect(form.referrer.data)

    total_size = 0
    file_sizes = db.session.execute(text('SELECT file_id, size FROM "user_file" WHERE user_id = :user_id'),
                                    {'user_id': current_user.id}).all()
    for fs in file_sizes:
        total_size += fs[1]

    if total_size > current_app.config['FILE_UPLOAD_QUOTA']:
        flash(_('You have exceeded your storage quota.', 'error'))
        return redirect(referrer())

    form.referrer.data = referrer(url_for('user.user_files'))

    return render_template('user/file_upload.html', form=form, user=current_user)


def _calculate_future_date(restriction_setting):
    """Calculate future date based on restriction setting"""
    current_date = datetime.now()
    if restriction_setting == '1day':
        return current_date + timedelta(days=1)
    elif restriction_setting == '1month':
        # Add 1 month by getting same day next month
        if current_date.month == 12:
            return current_date.replace(year=current_date.year + 1, month=1)
        else:
            return current_date.replace(month=current_date.month + 1)
    elif restriction_setting == '2months':
        # Add 2 months
        new_month = current_date.month + 2
        new_year = current_date.year
        if new_month > 12:
            new_month -= 12
            new_year += 1
        return current_date.replace(year=new_year, month=new_month)
    else:
        return current_date
