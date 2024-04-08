from datetime import datetime, timedelta
from time import sleep

from flask import redirect, url_for, flash, request, make_response, session, Markup, current_app, abort, json
from flask_login import login_user, logout_user, current_user, login_required
from flask_babel import _

from app import db, cache, celery
from app.activitypub.signature import post_request
from app.activitypub.util import default_context, find_actor_or_create
from app.community.util import save_icon_file, save_banner_file, retrieve_mods_and_backfill
from app.constants import SUBSCRIPTION_MEMBER, SUBSCRIPTION_PENDING
from app.models import Post, Community, CommunityMember, User, PostReply, PostVote, Notification, utcnow, File, Site, \
    Instance, Report, UserBlock, CommunityBan, CommunityJoinRequest, CommunityBlock, Filter, Domain, DomainBlock, \
    InstanceBlock
from app.user import bp
from app.user.forms import ProfileForm, SettingsForm, DeleteAccountForm, ReportUserForm, FilterEditForm
from app.user.utils import purge_user_then_delete
from app.utils import get_setting, render_template, markdown_to_html, user_access, markdown_to_text, shorten_string, \
    is_image_url, ensure_directory_exists, gibberish, file_get_contents, community_membership, user_filters_home, \
    user_filters_posts, user_filters_replies, moderating_communities, joined_communities, theme_list, blocked_instances, \
    allowlist_html
from sqlalchemy import desc, or_, text
import os


@bp.route('/people', methods=['GET', 'POST'])
@login_required
def show_people():
    if current_user.is_admin():
        people = User.query.filter_by(ap_id=None, deleted=False, banned=False).all()
    else:
        people = User.query.filter_by(ap_id=None, deleted=False, banned=False, searchable=True).all()
    return render_template('user/people.html', people=people, moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()), title=_('People'))


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

    posts = Post.query.filter_by(user_id=user.id).order_by(desc(Post.posted_at)).paginate(page=post_page, per_page=50, error_out=False)
    moderates = Community.query.filter_by(banned=False).join(CommunityMember).filter(CommunityMember.user_id == user.id)\
        .filter(or_(CommunityMember.is_moderator, CommunityMember.is_owner))
    if current_user.is_authenticated and (user.id == current_user.get_id() or current_user.is_admin()):
        upvoted = Post.query.join(PostVote, PostVote.post_id == Post.id).filter(PostVote.effect > 0, PostVote.user_id == user.id).order_by(desc(PostVote.created_at)).limit(10).all()
    else:
        upvoted = []
    subscribed = Community.query.filter_by(banned=False).join(CommunityMember).filter(CommunityMember.user_id == user.id).all()
    if current_user.is_anonymous or user.id != current_user.id:
        moderates = moderates.filter(Community.private_mods == False)
    post_replies = PostReply.query.filter_by(user_id=user.id).order_by(desc(PostReply.posted_at)).paginate(page=replies_page, per_page=50, error_out=False)

    # profile info
    canonical = user.ap_public_url if user.ap_public_url else None
    description = shorten_string(markdown_to_text(user.about), 150) if user.about else None

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
                           description=description, subscribed=subscribed, upvoted=upvoted,
                           post_next_url=post_next_url, post_prev_url=post_prev_url,
                           replies_next_url=replies_next_url, replies_prev_url=replies_prev_url,
                           noindex=not user.indexable, show_post_community=True,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id())
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
    if form.validate_on_submit() and not current_user.banned:
        current_user.title = form.title.data
        current_user.email = form.email.data
        if form.password_field.data.strip() != '':
            current_user.set_password(form.password_field.data)
        current_user.about = form.about.data
        current_user.about_html = allowlist_html(markdown_to_html(form.about.data))
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
        current_user.flush_cache()

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
                           joined_communities=joined_communities(current_user.get_id())
                           )


@bp.route('/user/settings', methods=['GET', 'POST'])
@login_required
def change_settings():
    user = User.query.filter_by(id=current_user.id, deleted=False, banned=False, ap_id=None).first()
    if user is None:
        abort(404)
    form = SettingsForm()
    form.theme.choices = theme_list()
    if form.validate_on_submit():
        propagate_indexable = form.indexable.data != current_user.indexable
        current_user.newsletter = form.newsletter.data
        current_user.ignore_bots = form.ignore_bots.data
        current_user.show_nsfw = form.nsfw.data
        current_user.show_nsfl = form.nsfl.data
        current_user.searchable = form.searchable.data
        current_user.indexable = form.indexable.data
        current_user.default_sort = form.default_sort.data
        current_user.theme = form.theme.data
        current_user.email_unread = form.email_unread.data
        current_user.markdown_editor = form.markdown_editor.data
        import_file = request.files['import_file']
        if propagate_indexable:
            db.session.execute(text('UPDATE "post" set indexable = :indexable WHERE user_id = :user_id'),
                               {'user_id': current_user.id,
                                'indexable': current_user.indexable})
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
        return redirect(url_for('user.change_settings'))
    elif request.method == 'GET':
        form.newsletter.data = current_user.newsletter
        form.email_unread.data = current_user.email_unread
        form.ignore_bots.data = current_user.ignore_bots
        form.nsfw.data = current_user.show_nsfw
        form.nsfl.data = current_user.show_nsfl
        form.searchable.data = current_user.searchable
        form.indexable.data = current_user.indexable
        form.default_sort.data = current_user.default_sort
        form.theme.data = current_user.theme
        form.markdown_editor.data = current_user.markdown_editor

    return render_template('user/edit_settings.html', title=_('Edit profile'), form=form, user=current_user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id())
                           )

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
            db.session.commit()

        if not user.is_local():
            ...
            # federate block

        flash(f'{actor} has been blocked.')

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
                           joined_communities=joined_communities(current_user.get_id())
                           )


@bp.route('/u/<actor>/delete', methods=['GET'])
@login_required
def delete_profile(actor):
    if user_access('manage users', current_user.id):
        actor = actor.strip()
        user = User.query.filter_by(user_name=actor, deleted=False).first()
        if user is None:
            user = User.query.filter_by(ap_id=actor, deleted=False).first()
            if user is None:
                abort(404)
        if user.id == current_user.id:
            flash(_('You cannot delete yourself.'), 'error')
        else:
            user.banned = True
            user.deleted = True
            user.delete_dependencies()
            db.session.commit()

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
                           joined_communities=joined_communities(current_user.get_id())
                           )


@celery.task
def send_deletion_requests(user_id):
    user = User.query.get(user_id)
    if user:
        instances = Instance.query.all()
        payload = {
            "@context": default_context(),
            "actor": user.profile_id(),
            "id": f"{user.profile_id()}#delete",
            "object": user.profile_id(),
            "to": [
                "https://www.w3.org/ns/activitystreams#Public"
            ],
            "type": "Delete"
        }
        for instance in instances:
            if instance.inbox and instance.online() and instance.id != 1: # instance id 1 is always the current instance
                post_request(instance.inbox, payload, user.private_key, f"{user.profile_id()}#main-key")

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
                purge_user_then_delete(user.id)
                flash(f'{actor} has been banned, deleted and all their content deleted. This might take a few minutes.')
            else:
                user.deleted = True
                user.delete_dependencies()
                user.purge_content()
                db.session.commit()
                flash(f'{actor} has been banned, deleted and all their content deleted.')
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
                           joined_communities=joined_communities(current_user.get_id())
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
                    db.session.commit()
                    follow = {
                        "actor": f"https://{current_app.config['SERVER_NAME']}/u/{user.user_name}",
                        "to": [community.ap_profile_id],
                        "object": community.ap_profile_id,
                        "type": "Follow",
                        "id": f"https://{current_app.config['SERVER_NAME']}/activities/follow/{join_request.id}"
                    }
                    success = post_request(community.ap_inbox_url, follow, user.private_key,
                                           user.profile_id() + '#main-key')
                    if not success:
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


@bp.route('/user/settings/filters', methods=['GET'])
@login_required
def user_settings_filters():
    filters = Filter.query.filter_by(user_id=current_user.id).order_by(Filter.title).all()
    blocked_users = User.query.join(UserBlock, UserBlock.blocked_id == User.id).\
        filter(UserBlock.blocker_id == current_user.id).order_by(User.user_name).all()
    blocked_communities = Community.query.join(CommunityBlock, CommunityBlock.community_id == Community.id).\
        filter(CommunityBlock.user_id == current_user.id).order_by(Community.title).all()
    blocked_domains = Domain.query.join(DomainBlock, DomainBlock.domain_id == Domain.id).\
        filter(DomainBlock.user_id == current_user.id).order_by(Domain.name).all()
    blocked_instances = Instance.query.join(InstanceBlock, InstanceBlock.instance_id == Instance.id).\
        filter(InstanceBlock.user_id == current_user.id).order_by(Instance.domain).all()
    return render_template('user/filters.html', filters=filters, user=current_user,
                           blocked_users=blocked_users, blocked_communities=blocked_communities,
                           blocked_domains=blocked_domains, blocked_instances=blocked_instances,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id())
                           )


@bp.route('/user/settings/filters/add', methods=['GET', 'POST'])
@login_required
def user_settings_filters_add():
    form = FilterEditForm()
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
                           joined_communities=joined_communities(current_user.get_id())
                           )


@bp.route('/user/settings/filters/<int:filter_id>/edit', methods=['GET', 'POST'])
@login_required
def user_settings_filters_edit(filter_id):

    content_filter = Filter.query.get_or_404(filter_id)
    if current_user.id != content_filter.user_id:
        abort(401)
    form = FilterEditForm()
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
                           joined_communities=joined_communities(current_user.get_id())
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
