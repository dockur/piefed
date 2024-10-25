from datetime import datetime, timedelta
from time import sleep
from random import randint
from io import BytesIO

from flask import redirect, url_for, flash, request, make_response, session, Markup, current_app, abort, json, g, send_file
from flask_login import login_user, logout_user, current_user, login_required
from flask_babel import _, lazy_gettext as _l

from app import db, cache, celery
from app.activitypub.signature import post_request, default_context
from app.activitypub.util import find_actor_or_create
from app.auth.util import random_token
from app.community.util import save_icon_file, save_banner_file, retrieve_mods_and_backfill
from app.constants import *
from app.email import send_verification_email
from app.models import Post, Community, CommunityMember, User, PostReply, PostVote, Notification, utcnow, File, Site, \
    Instance, Report, UserBlock, CommunityBan, CommunityJoinRequest, CommunityBlock, Filter, Domain, DomainBlock, \
    InstanceBlock, NotificationSubscription, PostBookmark, PostReplyBookmark, read_posts, Topic
from app.user import bp
from app.user.forms import ProfileForm, SettingsForm, DeleteAccountForm, ReportUserForm, \
    FilterForm, KeywordFilterEditForm, RemoteFollowForm, ImportExportForm
from app.user.utils import purge_user_then_delete, unsubscribe_from_community
from app.utils import get_setting, render_template, markdown_to_html, user_access, markdown_to_text, shorten_string, \
    is_image_url, ensure_directory_exists, gibberish, file_get_contents, community_membership, user_filters_home, \
    user_filters_posts, user_filters_replies, moderating_communities, joined_communities, theme_list, blocked_instances, \
    allowlist_html, recently_upvoted_posts, recently_downvoted_posts, blocked_users, menu_topics, add_to_modlog, \
    blocked_communities, piefed_markdown_to_lemmy_markdown
from sqlalchemy import desc, or_, text
import os
import json as python_json


@bp.route('/people', methods=['GET', 'POST'])
@login_required
def show_people():
    return redirect(url_for('instance.instance_people', instance_domain=current_app.config['SERVER_NAME']))


@bp.route('/user/<int:user_id>', methods=['GET'])
def show_profile_by_id(user_id):
    user = User.query.get_or_404(user_id)
    return show_profile(user)


def show_profile(user):
    if (user.deleted or user.banned) and current_user.is_anonymous:
        abort(404)

    if user.banned:
        flash(_('This user has been banned.'), 'warning')
    if user.deleted:
        flash(_('This user has been deleted.'), 'warning')

    post_page = request.args.get('post_page', 1, type=int)
    replies_page = request.args.get('replies_page', 1, type=int)

    posts = Post.query.filter_by(user_id=user.id).filter(Post.deleted == False).order_by(desc(Post.posted_at)).paginate(page=post_page, per_page=50, error_out=False)
    moderates = Community.query.filter_by(banned=False).join(CommunityMember).filter(CommunityMember.user_id == user.id)\
        .filter(or_(CommunityMember.is_moderator, CommunityMember.is_owner))
    if current_user.is_authenticated and (user.id == current_user.get_id() or current_user.is_admin()):
        upvoted = Post.query.join(PostVote, PostVote.post_id == Post.id).filter(PostVote.effect > 0, PostVote.user_id == user.id).order_by(desc(PostVote.created_at)).limit(10).all()
    else:
        upvoted = []
    subscribed = Community.query.filter_by(banned=False).join(CommunityMember).filter(CommunityMember.user_id == user.id).all()
    if current_user.is_anonymous or (user.id != current_user.id and not current_user.is_admin()):
        moderates = moderates.filter(Community.private_mods == False)
        post_replies = PostReply.query.filter_by(user_id=user.id, deleted=False).order_by(desc(PostReply.posted_at)).paginate(page=replies_page, per_page=50, error_out=False)
    elif current_user.is_admin():
        post_replies = PostReply.query.filter_by(user_id=user.id).order_by(desc(PostReply.posted_at)).paginate(page=replies_page, per_page=50, error_out=False)
    elif current_user.id == user.id:
        post_replies = PostReply.query.filter_by(user_id=user.id).filter(or_(PostReply.deleted == False, PostReply.deleted_by == user.id)).order_by(desc(PostReply.posted_at)).paginate(page=replies_page, per_page=50, error_out=False)

    # profile info
    canonical = user.ap_public_url if user.ap_public_url else None
    description = shorten_string(markdown_to_text(user.about), 150) if user.about else None
    user.recalculate_post_stats()
    db.session.commit()

    # pagination urls
    post_next_url = url_for('activitypub.user_profile', actor=user.ap_id if user.ap_id is not None else user.user_name,
                       post_page=posts.next_num) if posts.has_next else None
    post_prev_url = url_for('activitypub.user_profile', actor=user.ap_id if user.ap_id is not None else user.user_name,
                       post_page=posts.prev_num) if posts.has_prev and post_page != 1 else None
    replies_next_url = url_for('activitypub.user_profile', actor=user.ap_id if user.ap_id is not None else user.user_name,
                       replies_page=post_replies.next_num) if post_replies.has_next else None
    replies_prev_url = url_for('activitypub.user_profile', actor=user.ap_id if user.ap_id is not None else user.user_name,
                       replies_page=post_replies.prev_num) if post_replies.has_prev and replies_page != 1 else None

    return render_template('user/show_profile.html', user=user, posts=posts, post_replies=post_replies,
                           moderates=moderates.all(), canonical=canonical, title=_('Posts by %(user_name)s',
                                                                                   user_name=user.user_name),
                           description=description, subscribed=subscribed, upvoted=upvoted, disable_voting=True,
                           post_next_url=post_next_url, post_prev_url=post_prev_url,
                           replies_next_url=replies_next_url, replies_prev_url=replies_prev_url,
                           noindex=not user.indexable, show_post_community=True, hide_vote_buttons=True,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
                           )


@bp.route('/u/<actor>/profile', methods=['GET', 'POST'])
@login_required
def edit_profile(actor):
    actor = actor.strip()
    user = User.query.filter_by(user_name=actor, deleted=False, banned=False, ap_id=None).first()
    if user is None:
        abort(404)
    if current_user.id != user.id:
        abort(401)
    form = ProfileForm()
    old_email = user.email
    if form.validate_on_submit() and not current_user.banned:
        current_user.title = form.title.data.strip()
        current_user.email = form.email.data.strip()
        # Email address has changed - request verification of new address
        if form.email.data.strip() != old_email:
            current_user.verified = False
            verification_token = random_token(16)
            current_user.verification_token = verification_token
            send_verification_email(current_user)
            flash(_('You have changed your email address so we need to verify it. Please check your email inbox for a verification link.'), 'warning')
        current_user.email = form.email.data.strip()
        if form.password_field.data.strip() != '':
            current_user.set_password(form.password_field.data)
        current_user.about = piefed_markdown_to_lemmy_markdown(form.about.data)
        current_user.about_html = markdown_to_html(form.about.data)
        current_user.matrix_user_id = form.matrixuserid.data
        current_user.bot = form.bot.data
        profile_file = request.files['profile_file']
        if profile_file and profile_file.filename != '':
            # remove old avatar
            if current_user.avatar_id:
                file = File.query.get(current_user.avatar_id)
                file.delete_from_disk()
                current_user.avatar_id = None
                db.session.delete(file)

            # add new avatar
            file = save_icon_file(profile_file, 'users')
            if file:
                current_user.avatar = file
                cache.delete_memoized(User.avatar_image, current_user)
                cache.delete_memoized(User.avatar_thumbnail, current_user)
        banner_file = request.files['banner_file']
        if banner_file and banner_file.filename != '':
            # remove old cover
            if current_user.cover_id:
                file = File.query.get(current_user.cover_id)
                file.delete_from_disk()
                current_user.cover_id = None
                db.session.delete(file)

            # add new cover
            file = save_banner_file(banner_file, 'users')
            if file:
                current_user.cover = file
                cache.delete_memoized(User.cover_image, current_user)

        db.session.commit()

        flash(_('Your changes have been saved.'), 'success')

        return redirect(url_for('user.edit_profile', actor=actor))
    elif request.method == 'GET':
        form.title.data = current_user.title
        form.email.data = current_user.email
        form.about.data = current_user.about
        form.matrixuserid.data = current_user.matrix_user_id
        form.password_field.data = ''

    return render_template('user/edit_profile.html', title=_('Edit profile'), form=form, user=current_user,
                           markdown_editor=current_user.markdown_editor,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
                           )


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
            cache.delete_memoized(User.avatar_image, current_user)
            cache.delete_memoized(User.avatar_thumbnail, current_user)
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
            user_dict['avatar'] = f"https://{current_app.config['SERVER_NAME']}/{user.avatar_image()}"
        if user.cover_image() != '':
            user_dict['banner'] = f"https://{current_app.config['SERVER_NAME']}/{user.cover_image()}"
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
            user_dict['avatar_image'] = f"https://{current_app.config['SERVER_NAME']}/{user.avatar_image()}"
        if user.cover_image() != '':
            user_dict['cover_image'] = f"https://{current_app.config['SERVER_NAME']}/{user.cover_image()}"
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
        ('ca', _l('Catalan')),
        ('en', _l('English')),
        ('fr', _l('French')),
        ('de', _l('German')),
        ('ja', _l('Japanese')),
    ]
    if form.validate_on_submit():
        propagate_indexable = form.indexable.data != current_user.indexable
        current_user.newsletter = form.newsletter.data
        current_user.searchable = form.searchable.data
        current_user.indexable = form.indexable.data
        current_user.hide_read_posts = form.hide_read_posts.data
        current_user.default_sort = form.default_sort.data
        current_user.affinity_minimum = form.affinity_minimum.data
        current_user.default_filter = form.default_filter.data
        current_user.theme = form.theme.data
        current_user.email_unread = form.email_unread.data
        current_user.markdown_editor = form.markdown_editor.data
        current_user.interface_language = form.interface_language.data
        session['ui_language'] = form.interface_language.data
        if form.vote_privately.data:
            if current_user.alt_user_name is None or current_user.alt_user_name == '':
                current_user.alt_user_name = gibberish(randint(8, 20))
        else:
            current_user.alt_user_name = ''
        if propagate_indexable:
            db.session.execute(text('UPDATE "post" set indexable = :indexable WHERE user_id = :user_id'),
                               {'user_id': current_user.id,
                                'indexable': current_user.indexable})

        db.session.commit()

        flash(_('Your changes have been saved.'), 'success')
        return redirect(url_for('user.user_settings'))
    elif request.method == 'GET':
        form.newsletter.data = current_user.newsletter
        form.email_unread.data = current_user.email_unread
        form.searchable.data = current_user.searchable
        form.indexable.data = current_user.indexable
        form.hide_read_posts.data =  current_user.hide_read_posts
        form.default_sort.data = current_user.default_sort
        form.affinity_minimum.data = current_user.affinity_minimum
        form.default_filter.data = current_user.default_filter
        form.theme.data = current_user.theme
        form.markdown_editor.data = current_user.markdown_editor
        form.interface_language.data = current_user.interface_language
        form.vote_privately.data = current_user.vote_privately()

    return render_template('user/edit_settings.html', title=_('Edit profile'), form=form, user=current_user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
                           )


@bp.route('/user/settings/import_export', methods=['GET', 'POST'])
@login_required
def user_settings_import_export():
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
            new_filename = gibberish(15) + '.json'

            directory = f'app/static/media/'

            # save the file
            final_place = os.path.join(directory, new_filename + file_ext)
            import_file.save(final_place)

            # import settings in background task
            import_settings(final_place)

            flash(_('Your subscriptions and blocks are being imported. If you have many it could take a few minutes.'))

        db.session.commit()

        flash(_('Your changes have been saved.'), 'success')
        return redirect(url_for('user.user_settings_import_export'))

    return render_template('user/import_export.html', title=_('Import & Export'), form=form, user=current_user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
                           )


@bp.route('/user/<int:user_id>/notification', methods=['GET', 'POST'])
@login_required
def user_notification(user_id: int):
    # Toggle whether the current user is subscribed to notifications about this user's posts or not
    user = User.query.get_or_404(user_id)
    existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == user.id,
                                                                  NotificationSubscription.user_id == current_user.id,
                                                                  NotificationSubscription.type == NOTIF_USER).first()
    if existing_notification:
        db.session.delete(existing_notification)
        db.session.commit()
    else:   # no subscription yet, so make one
        if user.id != current_user.id and not user.has_blocked_user(current_user.id):
            new_notification = NotificationSubscription(name=user.display_name(), user_id=current_user.id,
                                                        entity_id=user.id, type=NOTIF_USER)
            db.session.add(new_notification)
            db.session.commit()

    return render_template('user/_notification_toggle.html', user=user)


@bp.route('/u/<actor>/ban', methods=['GET'])
@login_required
def ban_profile(actor):
    if user_access('ban users', current_user.id):
        actor = actor.strip()
        user = User.query.filter_by(user_name=actor, deleted=False).first()
        if user is None:
            user = User.query.filter_by(ap_id=actor, deleted=False).first()
            if user is None:
                abort(404)

        if user.id == current_user.id:
            flash(_('You cannot ban yourself.'), 'error')
        else:
            user.banned = True
            db.session.commit()

            add_to_modlog('ban_user', link_text=user.display_name(), link=user.link())

            flash(f'{actor} has been banned.')
    else:
        abort(401)

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/u/<actor>/unban', methods=['GET'])
@login_required
def unban_profile(actor):
    if user_access('ban users', current_user.id):
        actor = actor.strip()
        user = User.query.filter_by(user_name=actor, deleted=False).first()
        if user is None:
            user = User.query.filter_by(ap_id=actor, deleted=False).first()
            if user is None:
                abort(404)

        if user.id == current_user.id:
            flash(_('You cannot unban yourself.'), 'error')
        else:
            user.banned = False
            db.session.commit()

            add_to_modlog('unban_user', link_text=user.display_name(), link=user.link())

            flash(f'{actor} has been unbanned.')
    else:
        abort(401)

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/u/<actor>/block', methods=['GET'])
@login_required
def block_profile(actor):
    actor = actor.strip()
    user = User.query.filter_by(user_name=actor, deleted=False).first()
    if user is None:
        user = User.query.filter_by(ap_id=actor, deleted=False).first()
        if user is None:
            abort(404)

    if user.id == current_user.id:
        flash(_('You cannot block yourself.'), 'error')
    else:
        existing_block = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=user.id).first()
        if not existing_block:
            block = UserBlock(blocker_id=current_user.id, blocked_id=user.id)
            db.session.add(block)
            db.session.execute(text('DELETE FROM "notification_subscription" WHERE entity_id = :current_user AND user_id = :user_id'),
                               {'current_user': current_user.id, 'user_id': user.id})
            db.session.commit()

        if not user.is_local():
            ...
            # federate block

        flash(f'{actor} has been blocked.')
        cache.delete_memoized(blocked_users, current_user.id)

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/u/<actor>/block_instance', methods=['GET', 'POST'])
@login_required
def user_block_instance(actor):
    actor = actor.strip()
    user = User.query.filter_by(user_name=actor, deleted=False).first()
    if user is None:
        user = User.query.filter_by(ap_id=actor, deleted=False).first()
        if user is None:
            abort(404)

    if user.instance_id == 1:
        flash(_('You cannot block your instance.'), 'error')
    else:
        existing = InstanceBlock.query.filter_by(user_id=current_user.id, instance_id=user.instance_id).first()
        if not existing:
            db.session.add(InstanceBlock(user_id=current_user.id, instance_id=user.instance_id))
            db.session.commit()
            cache.delete_memoized(blocked_instances, current_user.id)
        flash(_('Content from %(name)s will be hidden.', name=user.ap_domain))
    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/u/<actor>/unblock', methods=['GET'])
@login_required
def unblock_profile(actor):
    actor = actor.strip()
    user = User.query.filter_by(user_name=actor, deleted=False).first()
    if user is None:
        user = User.query.filter_by(ap_id=actor, deleted=False).first()
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

        flash(f'{actor} has been unblocked.')
        cache.delete_memoized(blocked_users, current_user.id)

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/u/<actor>/report', methods=['GET', 'POST'])
@login_required
def report_profile(actor):
    if '@' in actor:
        user: User = User.query.filter_by(ap_id=actor, deleted=False, banned=False).first()
    else:
        user: User = User.query.filter_by(user_name=actor, deleted=False, ap_id=None).first()
    form = ReportUserForm()

    if user and user.reports == -1:  # When a mod decides to ignore future reports, user.reports is set to -1
        flash(_('Moderators have already assessed reports regarding this person, no further reports are necessary.'), 'warning')

    if user and not user.banned:
        if form.validate_on_submit():

            if user.reports == -1:
                flash(_('%(user_name)s has already been reported, thank you!', user_name=actor))
                goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
                return redirect(goto)

            report = Report(reasons=form.reasons_to_string(form.reasons.data), description=form.description.data,
                            type=0, reporter_id=current_user.id, suspect_user_id=user.id, source_instance_id=1)
            db.session.add(report)

            # Notify site admin
            already_notified = set()
            for admin in Site.admins():
                if admin.id not in already_notified:
                    notify = Notification(title='Reported user', url='/admin/reports', user_id=admin.id, author_id=current_user.id)
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

    return render_template('user/user_report.html', title=_('Report user'), form=form, user=user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics = menu_topics(), site=g.site
                           )


@bp.route('/u/<actor>/delete', methods=['GET'])
@login_required
def delete_profile(actor):
    if user_access('manage users', current_user.id):
        actor = actor.strip()
        user:User = User.query.filter_by(user_name=actor, deleted=False).first()
        if user is None:
            user = User.query.filter_by(ap_id=actor, deleted=False).first()
            if user is None:
                abort(404)
        if user.id == current_user.id:
            flash(_('You cannot delete yourself.'), 'error')
        else:
            user.banned = True
            user.deleted = True
            user.deleted_by = current_user.id
            user.delete_dependencies()
            db.session.commit()

            add_to_modlog('delete_user', link_text=user.display_name(), link=user.link())

            flash(f'{actor} has been deleted.')
    else:
        abort(401)

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)

@bp.route('/instance/<int:instance_id>/unblock', methods=['GET'])
@login_required
def instance_unblock(instance_id):
    instance = Instance.query.get_or_404(instance_id)
    existing_block = InstanceBlock.query.filter_by(user_id=current_user.id, instance_id=instance.id).first()
    if existing_block:
        db.session.delete(existing_block)
        db.session.commit()
        cache.delete_memoized(blocked_instances, current_user.id)
        flash(f'{instance.domain} has been unblocked.')

    goto = request.args.get('redirect') if 'redirect' in request.args else url_for('user.user_settings_filters')
    return redirect(goto)


@bp.route('/user/community/<int:community_id>/unblock', methods=['GET'])
@login_required
def user_community_unblock(community_id):
    community = Community.query.get_or_404(community_id)
    existing_block = CommunityBlock.query.filter_by(user_id=current_user.id, community_id=community.id).first()
    if existing_block:
        db.session.delete(existing_block)
        db.session.commit()
        cache.delete_memoized(blocked_communities, current_user.id)
        flash(f'{community.display_name()} has been unblocked.')

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

        # to verify the deletes, remote servers will GET /u/<actor> so we can't fully delete the account until the POSTs are done
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
        ...

    return render_template('user/delete_account.html', title=_('Delete my account'), form=form, user=current_user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
                           )


@celery.task
def send_deletion_requests(user_id):
    user = User.query.get(user_id)
    if user:
        # unsubscribe
        communities = CommunityMember.query.filter_by(user_id=user_id).all()
        for membership in communities:
            community = Community.query.get(membership.community_id)
            unsubscribe_from_community(community, user)

        instances = Instance.query.filter(Instance.dormant == False).all()
        payload = {
            "@context": default_context(),
            "actor": user.public_url(),
            "id": f"{user.public_url()}#delete",
            "object": user.public_url(),
            "to": [
                "https://www.w3.org/ns/activitystreams#Public"
            ],
            "type": "Delete"
        }
        for instance in instances:
            if instance.inbox and instance.online() and instance.id != 1: # instance id 1 is always the current instance
                post_request(instance.inbox, payload, user.private_key, f"{user.public_url()}#main-key")

        sleep(5)

        user.banned = True
        user.deleted = True

        db.session.commit()


@bp.route('/u/<actor>/ban_purge', methods=['GET'])
@login_required
def ban_purge_profile(actor):
    if user_access('manage users', current_user.id):
        actor = actor.strip()
        user = User.query.filter_by(user_name=actor).first()
        if user is None:
            user = User.query.filter_by(ap_id=actor).first()
            if user is None:
                abort(404)

        if user.id == current_user.id:
            flash(_('You cannot purge yourself.'), 'error')
        else:
            user.banned = True
            # user.deleted = True # DO NOT set user.deleted until the deletion of their posts has been federated
            db.session.commit()

            # todo: empty relevant caches

            # federate deletion
            if user.is_local():
                user.deleted_by = current_user.id
                purge_user_then_delete(user.id)
                flash(f'{actor} has been banned, deleted and all their content deleted. This might take a few minutes.')
            else:
                user.deleted = True
                user.deleted_by = current_user.id
                user.delete_dependencies()
                user.purge_content()
                db.session.commit()
                flash(f'{actor} has been banned, deleted and all their content deleted.')

            add_to_modlog('delete_user', link_text=user.display_name(), link=user.link())

    else:
        abort(401)

    goto = request.args.get('redirect') if 'redirect' in request.args else f'/u/{actor}'
    return redirect(goto)


@bp.route('/notifications', methods=['GET', 'POST'])
@login_required
def notifications():
    """Remove notifications older than 90 days"""
    db.session.query(Notification).filter(
        Notification.created_at < utcnow() - timedelta(days=90)).delete()
    db.session.commit()

    # Update unread notifications count, just to be sure
    current_user.unread_notifications = Notification.query.filter_by(user_id=current_user.id, read=False).count()
    db.session.commit()

    notification_list = Notification.query.filter_by(user_id=current_user.id).order_by(desc(Notification.created_at)).all()

    return render_template('user/notifications.html', title=_('Notifications'), notifications=notification_list, user=current_user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
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
    return redirect(url_for('user.notifications'))


@bp.route('/notifications/all_read', methods=['GET', 'POST'])
@login_required
def notifications_all_read():
    db.session.execute(text('UPDATE notification SET read=true WHERE user_id = :user_id'), {'user_id': current_user.id})
    current_user.unread_notifications = 0
    db.session.commit()
    flash(_('All notifications marked as read.'))
    return redirect(url_for('user.notifications'))


def import_settings(filename):
    if current_app.debug:
        import_settings_task(current_user.id, filename)
    else:
        import_settings_task.delay(current_user.id, filename)


@celery.task
def import_settings_task(user_id, filename):
    user = User.query.get(user_id)
    contents = file_get_contents(filename)
    contents_json = json.loads(contents)

    # Follow communities
    for community_ap_id in contents_json['followed_communities'] if 'followed_communities' in contents_json else []:
        community = find_actor_or_create(community_ap_id, community_only=True)
        if community:
            if community.posts.count() == 0:
                if current_app.debug:
                    retrieve_mods_and_backfill(community.id)
                else:
                    retrieve_mods_and_backfill.delay(community.id)
            if community_membership(user, community) != SUBSCRIPTION_MEMBER and community_membership(
                    user, community) != SUBSCRIPTION_PENDING:
                if not community.is_local():
                    # send ActivityPub message to remote community, asking to follow. Accept message will be sent to our shared inbox
                    join_request = CommunityJoinRequest(user_id=user.id, community_id=community.id)
                    db.session.add(join_request)
                    member = CommunityMember(user_id=user.id, community_id=community.id)
                    db.session.add(member)
                    db.session.commit()
                    success = True
                    if not community.instance.gone_forever:
                        follow = {
                          "actor": current_user.public_url(),
                          "to": [community.public_url()],
                          "object": community.public_url(),
                          "type": "Follow",
                          "id": f"https://{current_app.config['SERVER_NAME']}/activities/follow/{join_request.id}"
                        }
                        success = post_request(community.ap_inbox_url, follow, user.private_key,
                                           user.public_url() + '#main-key')
                    if success is False or isinstance(success, str):
                        sleep(5)    # give them a rest
                else:  # for local communities, joining is instant
                    banned = CommunityBan.query.filter_by(user_id=user.id, community_id=community.id).first()
                    if not banned:
                        member = CommunityMember(user_id=user.id, community_id=community.id)
                        db.session.add(member)
                        db.session.commit()
                cache.delete_memoized(community_membership, current_user, community)

    for community_ap_id in contents_json['blocked_communities'] if 'blocked_communities' in contents_json else []:
        community = find_actor_or_create(community_ap_id, community_only=True)
        if community:
            existing_block = CommunityBlock.query.filter_by(user_id=user.id, community_id=community.id).first()
            if not existing_block:
                block = CommunityBlock(user_id=user.id, community_id=community.id)
                db.session.add(block)

    for user_ap_id in contents_json['blocked_users'] if 'blocked_users' in contents_json else []:
        blocked_user = find_actor_or_create(user_ap_id)
        if blocked_user:
            existing_block = UserBlock.query.filter_by(blocker_id=user.id, blocked_id=blocked_user.id).first()
            if not existing_block:
                user_block = UserBlock(blocker_id=user.id, blocked_id=blocked_user.id)
                db.session.add(user_block)
                if not blocked_user.is_local():
                    ...  # todo: federate block

    for instance_domain in contents_json['blocked_instances']:
        ...

    db.session.commit()


@bp.route('/user/settings/filters', methods=['GET', 'POST'])
@login_required
def user_settings_filters():
    form = FilterForm()
    if form.validate_on_submit():
        current_user.ignore_bots = form.ignore_bots.data
        current_user.hide_nsfw = form.hide_nsfw.data
        current_user.hide_nsfl = form.hide_nsfl.data
        current_user.reply_collapse_threshold = form.reply_collapse_threshold.data
        current_user.reply_hide_threshold = form.reply_hide_threshold.data
        db.session.commit()

        flash(_('Your changes have been saved.'), 'success')
        return redirect(url_for('user.user_settings_filters'))
    elif request.method == 'GET':
        form.ignore_bots.data = current_user.ignore_bots
        form.hide_nsfw.data = current_user.hide_nsfw
        form.hide_nsfl.data = current_user.hide_nsfl
        form.reply_collapse_threshold.data = current_user.reply_collapse_threshold
        form.reply_hide_threshold.data = current_user.reply_hide_threshold
    filters = Filter.query.filter_by(user_id=current_user.id).order_by(Filter.title).all()
    blocked_users = User.query.filter_by(deleted=False).join(UserBlock, UserBlock.blocked_id == User.id).\
        filter(UserBlock.blocker_id == current_user.id).order_by(User.user_name).all()
    blocked_communities = Community.query.join(CommunityBlock, CommunityBlock.community_id == Community.id).\
        filter(CommunityBlock.user_id == current_user.id).order_by(Community.title).all()
    blocked_domains = Domain.query.join(DomainBlock, DomainBlock.domain_id == Domain.id).\
        filter(DomainBlock.user_id == current_user.id).order_by(Domain.name).all()
    blocked_instances = Instance.query.join(InstanceBlock, InstanceBlock.instance_id == Instance.id).\
        filter(InstanceBlock.user_id == current_user.id).order_by(Instance.domain).all()
    return render_template('user/filters.html', form=form, filters=filters, user=current_user,
                           blocked_users=blocked_users, blocked_communities=blocked_communities,
                           blocked_domains=blocked_domains, blocked_instances=blocked_instances,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
                           )


@bp.route('/user/settings/filters/add', methods=['GET', 'POST'])
@login_required
def user_settings_filters_add():
    form = KeywordFilterEditForm()
    form.filter_replies.render_kw = {'disabled': True}
    if form.validate_on_submit():
        content_filter = Filter(title=form.title.data, filter_home=form.filter_home.data, filter_posts=form.filter_posts.data,
                                filter_replies=form.filter_replies.data, hide_type=form.hide_type.data, keywords=form.keywords.data,
                                expire_after=form.expire_after.data, user_id=current_user.id)
        db.session.add(content_filter)
        db.session.commit()
        cache.delete_memoized(user_filters_home, current_user.id)
        cache.delete_memoized(user_filters_posts, current_user.id)
        cache.delete_memoized(user_filters_replies, current_user.id)

        flash(_('Your changes have been saved.'), 'success')
        return redirect(url_for('user.user_settings_filters'))

    return render_template('user/edit_filters.html', title=_('Add filter'), form=form, user=current_user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
                           )


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

    return render_template('user/edit_filters.html', title=_('Edit filter'), form=form, content_filter=content_filter, user=current_user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
                           )


@bp.route('/user/settings/filters/<int:filter_id>/delete', methods=['GET', 'POST'])
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
    flash(_('Filter deleted.'))
    return redirect(url_for('user.user_settings_filters'))


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

    posts = Post.query.filter(Post.deleted == False).join(PostBookmark, PostBookmark.post_id == Post.id).\
        filter(PostBookmark.user_id == current_user.id).order_by(desc(PostBookmark.created_at))

    posts = posts.paginate(page=page, per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                           error_out=False)
    next_url = url_for('user.user_bookmarks', page=posts.next_num) if posts.has_next else None
    prev_url = url_for('user.user_bookmarks', page=posts.prev_num) if posts.has_prev and page != 1 else None

    return render_template('user/bookmarks.html', title=_('Bookmarks'), posts=posts, show_post_community=True,
                           low_bandwidth=low_bandwidth, user=current_user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site,
                           next_url=next_url, prev_url=prev_url)


@bp.route('/bookmarks/comments')
@login_required
def user_bookmarks_comments():
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    post_replies = PostReply.query.filter(PostReply.deleted == False).join(PostReplyBookmark, PostReplyBookmark.post_reply_id == PostReply.id).\
        filter(PostReplyBookmark.user_id == current_user.id).order_by(desc(PostReplyBookmark.created_at))

    post_replies = post_replies.paginate(page=page, per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                           error_out=False)
    next_url = url_for('user.user_bookmarks_comments', page=post_replies.next_num) if post_replies.has_next else None
    prev_url = url_for('user.user_bookmarks_comments', page=post_replies.prev_num) if post_replies.has_prev and page != 1 else None

    return render_template('user/bookmarks_comments.html', title=_('Comment bookmarks'), post_replies=post_replies, show_post_community=True,
                           low_bandwidth=low_bandwidth, user=current_user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site,
                           next_url=next_url, prev_url=prev_url)


@bp.route('/alerts')
@bp.route('/alerts/<type>/<filter>')
@login_required
def user_alerts(type='posts', filter='all'):
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    if type == 'comments':
        if filter == 'mine':
            entities = PostReply.query.filter_by(deleted=False, user_id=current_user.id).\
                        join(NotificationSubscription, NotificationSubscription.entity_id == PostReply.id).\
                        filter_by(type=NOTIF_REPLY, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        elif filter == 'others':
            entities = PostReply.query.filter(PostReply.deleted == False, PostReply.user_id != current_user.id).\
                        join(NotificationSubscription, NotificationSubscription.entity_id == PostReply.id).\
                        filter_by(type=NOTIF_REPLY, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        else:   # default to 'all' filter
            entities = PostReply.query.filter_by(deleted=False).\
                        join(NotificationSubscription, NotificationSubscription.entity_id == PostReply.id).\
                        filter_by(type=NOTIF_REPLY, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        title = _('Reply Alerts')

    elif type == 'communities':
        if filter == 'mine':
            entities = Community.query.\
                        join(CommunityMember, CommunityMember.community_id == Community.id).\
                        filter_by(user_id=current_user.id, is_moderator=True).\
                        join(NotificationSubscription, NotificationSubscription.entity_id == CommunityMember.community_id).\
                        filter_by(type=NOTIF_COMMUNITY, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        elif filter == 'others':
            entities = Community.query.\
                        join(CommunityMember, CommunityMember.community_id == Community.id).\
                        filter_by(user_id=current_user.id, is_moderator=False).\
                        join(NotificationSubscription, NotificationSubscription.entity_id == CommunityMember.community_id).\
                        filter_by(type=NOTIF_COMMUNITY, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        else:   # default to 'all' filter
            entities = Community.query.\
                        join(NotificationSubscription, NotificationSubscription.entity_id == Community.id).\
                        filter_by(type=NOTIF_COMMUNITY, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        title = _('Community Alerts')

    elif type == 'topics':
        # ignore filter
        entities = Topic.query.join(NotificationSubscription, NotificationSubscription.entity_id == Topic.id).\
                        filter_by(type=NOTIF_TOPIC, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        title = _('Topic Alerts')

    elif type == 'users':
        # ignore filter
        entities = User.query.join(NotificationSubscription, NotificationSubscription.entity_id == User.id).\
                        filter_by(type=NOTIF_USER, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        title = _('User Alerts')

    else:   # default to 'posts' type
        if filter == 'mine':
            entities = Post.query.filter_by(deleted=False, user_id=current_user.id).\
                        join(NotificationSubscription, NotificationSubscription.entity_id == Post.id).\
                        filter_by(type=NOTIF_POST, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        elif filter == 'others':
            entities = Post.query.filter(Post.deleted == False, Post.user_id != current_user.id).\
                        join(NotificationSubscription, NotificationSubscription.entity_id == Post.id).\
                        filter_by(type=NOTIF_POST, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        else:   # default to 'all' filter
            entities = Post.query.filter_by(deleted=False).\
                        join(NotificationSubscription, NotificationSubscription.entity_id == Post.id).\
                        filter_by(type=NOTIF_POST, user_id=current_user.id).order_by(desc(NotificationSubscription.created_at))
        title = _('Post Alerts')

    entities = entities.paginate(page=page, per_page=100 if not low_bandwidth else 50, error_out=False)
    next_url = url_for('user.user_alerts', page=entities.next_num, type=type, filter=filter) if entities.has_next else None
    prev_url = url_for('user.user_alerts', page=entities.prev_num, type=type, filter=filter) if entities.has_prev and page != 1 else None

    return render_template('user/alerts.html', title=title, entities=entities,
                           low_bandwidth=low_bandwidth, user=current_user, type=type, filter=filter,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site,
                           next_url=next_url, prev_url=prev_url)


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
                return render_template('user/fediverse_redirect.html', form=form, user=user, send_to='', current_app=current_app)
            elif form.instance_type.data == 'friendica':
                redirect_url = f'https://{form.instance_url.data}/search?q={user.user_name}@{current_app.config["SERVER_NAME"]}'
            elif form.instance_type.data == 'hubzilla':
                redirect_url = f'https://{form.instance_url.data}/search?q={user.user_name}@{current_app.config["SERVER_NAME"]}'
            elif form.instance_type.data == 'pixelfed':
                redirect_url = f'https://{form.instance_url.data}/i/results?q={user.user_name}@{current_app.config["SERVER_NAME"]}'

            resp = make_response(redirect(redirect_url))
            resp.set_cookie('remote_instance_url', form.instance_url.data, expires=datetime(year=2099, month=12, day=30))
            return resp
        else:
            send_to = ''
            if request.cookies.get('remote_instance_url'):
                send_to = request.cookies.get('remote_instance_url')
                form.instance_url.data = send_to
            return render_template('user/fediverse_redirect.html', form=form, user=user, send_to=send_to, current_app=current_app)


@bp.route('/read-posts')
@login_required
def user_read_posts():
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    posts = Post.query.filter(Post.deleted == False)

    if current_user.ignore_bots == 1:
        posts = posts.filter(Post.from_bot == False)
    if current_user.hide_nsfl == 1:
        posts = posts.filter(Post.nsfl == False)
    if current_user.hide_nsfw == 1:
        posts = posts.filter(Post.nsfw == False)

    # get the list of post.ids that the 
    # current_user has already read/voted on
    posts = posts.join(read_posts, read_posts.c.read_post_id == Post.id).filter(read_posts.c.user_id == current_user.id)

    posts = posts.paginate(page=page, per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                           error_out=False)
    next_url = url_for('user.user_read_posts', page=posts.next_num) if posts.has_next else None
    prev_url = url_for('user.user_read_posts', page=posts.prev_num) if posts.has_prev and page != 1 else None

    return render_template('user/read_posts.html', title=_('Read posts'), posts=posts, show_post_community=True,
                           low_bandwidth=low_bandwidth, user=current_user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site,
                           next_url=next_url, prev_url=prev_url)


@bp.route('/read-posts/delete')
@login_required
def user_read_posts_delete():
    db.session.execute(text('DELETE FROM "read_posts" WHERE user_id = :user_id'), {'user_id': current_user.id})
    db.session.commit()
    flash(_('Reading history has been deleted'))
    return redirect(url_for('user.user_read_posts'))
