import os
from datetime import datetime, timedelta
from io import BytesIO
from time import sleep

from flask import request, flash, json, url_for, current_app, redirect, g, abort
from flask_login import login_required, current_user
from flask_babel import _
from slugify import slugify
from sqlalchemy import text, desc, or_
from PIL import Image

from app import db, celery, cache
from app.activitypub.routes import process_inbox_request, process_delete_request
from app.activitypub.signature import post_request, default_context
from app.activitypub.util import instance_allowed, instance_blocked
from app.admin.forms import FederationForm, SiteMiscForm, SiteProfileForm, EditCommunityForm, EditUserForm, \
    EditTopicForm, SendNewsletterForm, AddUserForm
from app.admin.util import unsubscribe_from_everything_then_delete, unsubscribe_from_community, send_newsletter, \
    topics_for_form
from app.community.util import save_icon_file, save_banner_file
from app.constants import REPORT_STATE_NEW, REPORT_STATE_ESCALATED
from app.email import send_welcome_email
from app.models import AllowedInstances, BannedInstances, ActivityPubLog, utcnow, Site, Community, CommunityMember, \
    User, Instance, File, Report, Topic, UserRegistration, Role, Post, PostReply, Language
from app.utils import render_template, permission_required, set_setting, get_setting, gibberish, markdown_to_html, \
    moderating_communities, joined_communities, finalize_user_setup, theme_list, blocked_phrases, blocked_referrers, \
    topic_tree, languages_for_form, menu_topics, ensure_directory_exists
from app.admin import bp


@bp.route('/', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_home():
    return render_template('admin/home.html', title=_('Admin'), moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site)


@bp.route('/site', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_site():
    form = SiteProfileForm()
    site = Site.query.get(1)
    if site is None:
        site = Site()
    if form.validate_on_submit():
        site.name = form.name.data
        site.description = form.description.data #tagline
        site.about = form.about.data
        site.sidebar = form.sidebar.data
        site.legal_information = form.legal_information.data
        site.updated = utcnow()
        site.contact_email = form.contact_email.data
        if site.id is None:
            db.session.add(site)
        # Save site icon
        uploaded_icon = request.files['icon']
        if uploaded_icon and uploaded_icon.filename != '':
            allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp']
            file_ext = os.path.splitext(uploaded_icon.filename)[1]
            if file_ext.lower() not in allowed_extensions:
                abort(400)
            directory = 'app/static/media'
            ensure_directory_exists(directory)

            # Remove existing logo files
            if os.path.isfile(f'app{site.logo}'):
                os.unlink(f'app{site.logo}')
            if os.path.isfile(f'app{site.logo_152}'):
                os.unlink(f'app{site.logo_152}')
            if os.path.isfile(f'app{site.logo_32}'):
                os.unlink(f'app{site.logo_32}')
            if os.path.isfile(f'app{site.logo_16}'):
                os.unlink(f'app{site.logo_16}')

            # Save logo file
            base_filename = f'logo_{gibberish(5)}'
            uploaded_icon.save(f'{directory}/{base_filename}{file_ext}')
            img = Image.open(f'{directory}/{base_filename}{file_ext}')
            if img.width > 100:
                img.thumbnail((100, 100))
                img.save(f'{directory}/{base_filename}_100{file_ext}')
                site.logo = f'/static/media/{base_filename}_100{file_ext}'
                delete_original = True
            else:
                site.logo = f'/static/media/{base_filename}{file_ext}'
                delete_original = False

            # Save multiple copies of the logo - different sizes
            img = Image.open(f'{directory}/{base_filename}{file_ext}')
            img.thumbnail((152, 152))
            img.save(f'{directory}/{base_filename}_152{file_ext}')
            site.logo_152 = f'/static/media/{base_filename}_152{file_ext}'

            img = Image.open(f'{directory}/{base_filename}{file_ext}')
            img.thumbnail((32, 32))
            img.save(f'{directory}/{base_filename}_32{file_ext}')
            site.logo_32 = f'/static/media/{base_filename}_32{file_ext}'

            img = Image.open(f'{directory}/{base_filename}{file_ext}')
            img.thumbnail((16, 16))
            img.save(f'{directory}/{base_filename}_16{file_ext}')
            site.logo_16 = f'/static/media/{base_filename}_16{file_ext}'

            if delete_original:
                os.unlink(f'app/static/media/{base_filename}{file_ext}')

        db.session.commit()
        set_setting('announcement', form.announcement.data)
        flash('Settings saved.')
    elif request.method == 'GET':
        form.name.data = site.name
        form.description.data = site.description
        form.about.data = site.about
        form.sidebar.data = site.sidebar
        form.legal_information.data = site.legal_information
        form.contact_email.data = site.contact_email
        form.announcement.data = get_setting('announcement', '')
    return render_template('admin/site.html', title=_('Site profile'), form=form,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/misc', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_misc():
    form = SiteMiscForm()
    site = Site.query.get(1)
    if site is None:
        site = Site()
    form.default_theme.choices = theme_list()
    if form.validate_on_submit():
        site.enable_downvotes = form.enable_downvotes.data
        site.allow_local_image_posts = form.allow_local_image_posts.data
        site.remote_image_cache_days = form.remote_image_cache_days.data
        site.enable_nsfw = form.enable_nsfw.data
        site.enable_nsfl = form.enable_nsfl.data
        site.community_creation_admin_only = form.community_creation_admin_only.data
        site.reports_email_admins = form.reports_email_admins.data
        site.registration_mode = form.registration_mode.data
        site.application_question = form.application_question.data
        site.auto_decline_referrers = form.auto_decline_referrers.data
        site.log_activitypub_json = form.log_activitypub_json.data
        site.updated = utcnow()
        site.default_theme = form.default_theme.data
        if site.id is None:
            db.session.add(site)
        db.session.commit()
        cache.delete_memoized(blocked_referrers)
        flash('Settings saved.')
    elif request.method == 'GET':
        form.enable_downvotes.data = site.enable_downvotes
        form.allow_local_image_posts.data = site.allow_local_image_posts
        form.remote_image_cache_days.data = site.remote_image_cache_days
        form.enable_nsfw.data = site.enable_nsfw
        form.enable_nsfl.data = site.enable_nsfl
        form.community_creation_admin_only.data = site.community_creation_admin_only
        form.reports_email_admins.data = site.reports_email_admins
        form.registration_mode.data = site.registration_mode
        form.application_question.data = site.application_question
        form.auto_decline_referrers.data = site.auto_decline_referrers
        form.log_activitypub_json.data = site.log_activitypub_json
        form.default_theme.data = site.default_theme if site.default_theme is not None else ''
    return render_template('admin/misc.html', title=_('Misc settings'), form=form,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/federation', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_federation():
    form = FederationForm()
    site = Site.query.get(1)
    if site is None:
        site = Site()
    # todo: finish form
    site.updated = utcnow()
    if form.validate_on_submit():
        if form.use_allowlist.data:
            set_setting('use_allowlist', True)
            db.session.execute(text('DELETE FROM allowed_instances'))
            for allow in form.allowlist.data.split('\n'):
                if allow.strip():
                    db.session.add(AllowedInstances(domain=allow.strip()))
                    cache.delete_memoized(instance_allowed, allow.strip())
        if form.use_blocklist.data:
            set_setting('use_allowlist', False)
            db.session.execute(text('DELETE FROM banned_instances'))
            for banned in form.blocklist.data.split('\n'):
                if banned.strip():
                    db.session.add(BannedInstances(domain=banned.strip()))
                    cache.delete_memoized(instance_blocked, banned.strip())
        site.blocked_phrases = form.blocked_phrases.data
        set_setting('actor_blocked_words', form.blocked_actors.data)
        cache.delete_memoized(blocked_phrases)
        cache.delete_memoized(get_setting, 'actor_blocked_words')
        db.session.commit()

        flash(_('Admin settings saved'))

    elif request.method == 'GET':
        form.use_allowlist.data = get_setting('use_allowlist', False)
        form.use_blocklist.data = not form.use_allowlist.data
        instances = BannedInstances.query.all()
        form.blocklist.data = '\n'.join([instance.domain for instance in instances])
        instances = AllowedInstances.query.all()
        form.allowlist.data = '\n'.join([instance.domain for instance in instances])
        form.blocked_phrases.data = site.blocked_phrases
        form.blocked_actors.data = get_setting('actor_blocked_words', '88')

    return render_template('admin/federation.html', title=_('Federation settings'), form=form,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/activities', methods=['GET'])
@login_required
@permission_required('change instance settings')
def admin_activities():
    db.session.query(ActivityPubLog).filter(
        ActivityPubLog.created_at < utcnow() - timedelta(days=3)).delete()
    db.session.commit()

    page = request.args.get('page', 1, type=int)
    result_filter = request.args.get('result', type=str)

    if result_filter:
        activities = ActivityPubLog.query.order_by(desc(ActivityPubLog.created_at)).filter(ActivityPubLog.result == result_filter).paginate(page=page, per_page=1000, error_out=False)
    else:
        activities = ActivityPubLog.query.order_by(desc(ActivityPubLog.created_at)).paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_activities', page=activities.next_num, result=result_filter) if activities.has_next else None
    prev_url = url_for('admin.admin_activities', page=activities.prev_num, result=result_filter) if activities.has_prev and page != 1 else None

    return render_template('admin/activities.html', title=_('ActivityPub Log'), next_url=next_url, prev_url=prev_url,
                           activities=activities,
                           site=g.site)


@bp.route('/activity_json/<int:activity_id>')
@login_required
@permission_required('change instance settings')
def activity_json(activity_id):
    activity = ActivityPubLog.query.get_or_404(activity_id)
    return render_template('admin/activity_json.html', title=_('Activity JSON'),
                           activity_json_data=json.loads(activity.activity_json), activity=activity,
                           current_app=current_app,
                           site=g.site)


@bp.route('/activity_json/<int:activity_id>/replay')
@login_required
@permission_required('change instance settings')
def activity_replay(activity_id):
    activity = ActivityPubLog.query.get_or_404(activity_id)
    request_json = json.loads(activity.activity_json)
    if 'type' in request_json and request_json['type'] == 'Delete' and request_json['id'].endswith('#delete'):
        process_delete_request(request_json, activity.id, None)
    else:
        process_inbox_request(request_json, activity.id, None)
    return 'Ok'


@bp.route('/communities', methods=['GET'])
@login_required
@permission_required('administer all communities')
def admin_communities():

    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')

    communities = Community.query
    if search:
        communities = communities.filter(Community.title.ilike(f"%{search}%"))
    communities = communities.order_by(Community.title).paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_communities', page=communities.next_num) if communities.has_next else None
    prev_url = url_for('admin.admin_communities', page=communities.prev_num) if communities.has_prev and page != 1 else None

    return render_template('admin/communities.html', title=_('Communities'), next_url=next_url, prev_url=prev_url,
                           communities=communities, moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site)


@bp.route('/communities/no-topic', methods=['GET'])
@login_required
@permission_required('administer all communities')
def admin_communities_no_topic():

    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')

    communities = Community.query.filter(Community.topic_id == None)
    if search:
        communities = communities.filter(Community.title.ilike(f"%{search}%"))
    communities = communities.order_by(-Community.post_count).paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_communities_no_topic', page=communities.next_num) if communities.has_next else None
    prev_url = url_for('admin.admin_communities_no_topic', page=communities.prev_num) if communities.has_prev and page != 1 else None

    return render_template('admin/communities.html', title=_('Communities with no topic'), next_url=next_url, prev_url=prev_url,
                           communities=communities, moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site)


@bp.route('/community/<int:community_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('administer all communities')
def admin_community_edit(community_id):
    form = EditCommunityForm()
    community = Community.query.get_or_404(community_id)
    form.topic.choices = topics_for_form(0)
    form.languages.choices = languages_for_form()
    if form.validate_on_submit():
        community.name = form.url.data
        community.title = form.title.data
        community.description = form.description.data
        community.description_html = markdown_to_html(form.description.data)
        community.rules = form.rules.data
        community.rules_html = markdown_to_html(form.rules.data)
        community.nsfw = form.nsfw.data
        community.banned = form.banned.data
        community.local_only = form.local_only.data
        community.restricted_to_mods = form.restricted_to_mods.data
        community.new_mods_wanted = form.new_mods_wanted.data
        community.show_home = form.show_home.data
        community.show_popular = form.show_popular.data
        community.show_all = form.show_all.data
        community.low_quality = form.low_quality.data
        community.content_retention = form.content_retention.data
        community.topic_id = form.topic.data if form.topic.data != 0 else None
        community.default_layout = form.default_layout.data
        community.posting_warning = form.posting_warning.data
        community.ignore_remote_language = form.ignore_remote_language.data

        icon_file = request.files['icon_file']
        if icon_file and icon_file.filename != '':
            if community.icon_id:
                community.icon.delete_from_disk()
            file = save_icon_file(icon_file)
            if file:
                community.icon = file
        banner_file = request.files['banner_file']
        if banner_file and banner_file.filename != '':
            if community.image_id:
                community.image.delete_from_disk()
            file = save_banner_file(banner_file)
            if file:
                community.image = file

        # Languages of the community
        db.session.execute(text('DELETE FROM "community_language" WHERE community_id = :community_id'),
                           {'community_id': community_id})
        for language_choice in form.languages.data:
            community.languages.append(Language.query.get(language_choice))
        # Always include the undetermined language, so posts with no language will be accepted
        community.languages.append(Language.query.filter(Language.code == 'und').first())

        db.session.commit()
        if community.topic_id:
            community.topic.num_communities = community.topic.communities.count()
        db.session.commit()
        flash(_('Saved'))
        return redirect(url_for('admin.admin_communities'))
    else:
        if not community.is_local():
            flash(_('This is a remote community - most settings here will be regularly overwritten with data from the original server.'), 'warning')
        form.url.data = community.name
        form.title.data = community.title
        form.description.data = community.description
        form.rules.data = community.rules
        form.nsfw.data = community.nsfw
        form.banned.data = community.banned
        form.local_only.data = community.local_only
        form.new_mods_wanted.data = community.new_mods_wanted
        form.restricted_to_mods.data = community.restricted_to_mods
        form.show_home.data = community.show_home
        form.show_popular.data = community.show_popular
        form.show_all.data = community.show_all
        form.low_quality.data = community.low_quality
        form.content_retention.data = community.content_retention
        form.topic.data = community.topic_id if community.topic_id else None
        form.default_layout.data = community.default_layout
        form.posting_warning.data = community.posting_warning
        form.languages.data = community.language_ids()
        form.ignore_remote_language.data = community.ignore_remote_language
    return render_template('admin/edit_community.html', title=_('Edit community'), form=form, community=community,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/community/<int:community_id>/delete', methods=['GET'])
@login_required
@permission_required('administer all communities')
def admin_community_delete(community_id):
    community = Community.query.get_or_404(community_id)

    community.banned = True  # Unsubscribing everyone could take a long time so until that is completed hide this community from the UI by banning it.
    community.last_active = utcnow()
    db.session.commit()

    unsubscribe_everyone_then_delete(community.id)

    flash(_('Community deleted'))
    return redirect(url_for('admin.admin_communities'))


def unsubscribe_everyone_then_delete(community_id):
    if current_app.debug:
        unsubscribe_everyone_then_delete_task(community_id)
    else:
        unsubscribe_everyone_then_delete_task.delay(community_id)


@celery.task
def unsubscribe_everyone_then_delete_task(community_id):
    community = Community.query.get_or_404(community_id)
    if not community.is_local():
        members = CommunityMember.query.filter_by(community_id=community_id).all()
        for member in members:
            user = User.query.get(member.user_id)
            unsubscribe_from_community(community, user)
    else:
        # todo: federate delete of local community out to all following instances
        ...

    sleep(5)
    community.delete_dependencies()
    db.session.delete(community)    # todo: when a remote community is deleted it will be able to be re-created by using the 'Add remote' function. Not ideal. Consider soft-delete.
    db.session.commit()


@bp.route('/topics', methods=['GET'])
@login_required
@permission_required('administer all communities')
def admin_topics():
    topics = topic_tree()
    return render_template('admin/topics.html', title=_('Topics'), topics=topics,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/topic/add', methods=['GET', 'POST'])
@login_required
@permission_required('administer all communities')
def admin_topic_add():
    form = EditTopicForm()
    form.parent_id.choices = topics_for_form(0)
    if form.validate_on_submit():
        topic = Topic(name=form.name.data, machine_name=slugify(form.machine_name.data.strip()), num_communities=0)
        if form.parent_id.data:
            topic.parent_id = form.parent_id.data
        else:
            topic.parent_id = None
        db.session.add(topic)
        db.session.commit()
        cache.delete_memoized(menu_topics)

        flash(_('Saved'))
        return redirect(url_for('admin.admin_topics'))

    return render_template('admin/edit_topic.html', title=_('Add topic'), form=form,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )

@bp.route('/topic/<int:topic_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('administer all communities')
def admin_topic_edit(topic_id):
    form = EditTopicForm()
    topic = Topic.query.get_or_404(topic_id)
    form.parent_id.choices = topics_for_form(topic_id)
    if form.validate_on_submit():
        topic.name = form.name.data
        topic.num_communities = topic.communities.count()
        topic.machine_name = form.machine_name.data
        if form.parent_id.data:
            topic.parent_id = form.parent_id.data
        else:
            topic.parent_id = None
        db.session.commit()
        cache.delete_memoized(menu_topics)
        flash(_('Saved'))
        return redirect(url_for('admin.admin_topics'))
    else:
        form.name.data = topic.name
        form.machine_name.data = topic.machine_name
        form.parent_id.data = topic.parent_id
    return render_template('admin/edit_topic.html', title=_('Edit topic'), form=form, topic=topic,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/topic/<int:topic_id>/delete', methods=['GET'])
@login_required
@permission_required('administer all communities')
def admin_topic_delete(topic_id):
    topic = Topic.query.get_or_404(topic_id)
    topic.num_communities = topic.communities.count()
    if topic.num_communities == 0:
        db.session.delete(topic)
        flash(_('Topic deleted'))
    else:
        flash(_('Cannot delete topic with communities assigned to it.', 'error'))
    db.session.commit()

    cache.delete_memoized(menu_topics)

    return redirect(url_for('admin.admin_topics'))


@bp.route('/users', methods=['GET'])
@login_required
@permission_required('administer all users')
def admin_users():

    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    local_remote = request.args.get('local_remote', '')

    users = User.query.filter_by(deleted=False)
    if local_remote == 'local':
        users = users.filter_by(ap_id=None)
    if local_remote == 'remote':
        users = users.filter(User.ap_id != None)
    if search:
        users = users.filter(User.email.ilike(f"%{search}%"))
    users = users.order_by(User.user_name).paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_users', page=users.next_num) if users.has_next else None
    prev_url = url_for('admin.admin_users', page=users.prev_num) if users.has_prev and page != 1 else None

    return render_template('admin/users.html', title=_('Users'), next_url=next_url, prev_url=prev_url, users=users,
                           local_remote=local_remote, search=search,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/users/trash', methods=['GET'])
@login_required
@permission_required('administer all users')
def admin_users_trash():

    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    local_remote = request.args.get('local_remote', '')
    type = request.args.get('type', 'bad_rep')

    users = User.query.filter_by(deleted=False)
    if local_remote == 'local':
        users = users.filter_by(ap_id=None)
    if local_remote == 'remote':
        users = users.filter(User.ap_id != None)
    if search:
        users = users.filter(User.email.ilike(f"%{search}%"))

    if type == '' or type == 'bad_rep':
        users = users.filter(User.last_seen > utcnow() - timedelta(days=7))
        users = users.filter(User.reputation < -10)
        users = users.order_by(User.reputation).paginate(page=page, per_page=1000, error_out=False)
    elif type == 'bad_attitude':
        users = users.filter(User.last_seen > utcnow() - timedelta(days=7))
        users = users.filter(User.attitude < 0.0).filter(User.reputation < -10)
        users = users.order_by(User.attitude).paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_users_trash', page=users.next_num, search=search, local_remote=local_remote, type=type) if users.has_next else None
    prev_url = url_for('admin.admin_users_trash', page=users.prev_num, search=search, local_remote=local_remote, type=type) if users.has_prev and page != 1 else None

    return render_template('admin/users_trash.html', title=_('Problematic users'), next_url=next_url, prev_url=prev_url, users=users,
                           local_remote=local_remote, search=search, type=type,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/content/trash', methods=['GET'])
@login_required
@permission_required('administer all users')
def admin_content_trash():

    page = request.args.get('page', 1, type=int)

    posts = Post.query.filter(Post.posted_at > utcnow() - timedelta(days=3), Post.deleted == False).order_by(Post.score)
    posts = posts.paginate(page=page, per_page=100, error_out=False)

    next_url = url_for('admin.admin_content_trash', page=posts.next_num) if posts.has_next else None
    prev_url = url_for('admin.admin_content_trash', page=posts.prev_num) if posts.has_prev and page != 1 else None

    return render_template('admin/posts.html', title=_('Bad posts'), next_url=next_url, prev_url=prev_url, posts=posts,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           show_post_community=True, menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/content/spam', methods=['GET'])
@login_required
@permission_required('administer all users')
def admin_content_spam():
    # Essentially the same as admin_content_trash() except only shows heavily downvoted posts by new users - who are usually spammers
    page = request.args.get('page', 1, type=int)
    replies_page = request.args.get('replies_page', 1, type=int)

    posts = Post.query.join(User, User.id == Post.user_id).\
        filter(User.created > utcnow() - timedelta(days=3)).\
        filter(Post.posted_at > utcnow() - timedelta(days=3)).\
        filter(Post.deleted == False).\
        filter(Post.score <= 0).order_by(Post.score)
    posts = posts.paginate(page=page, per_page=100, error_out=False)

    post_replies = PostReply.query.join(User, User.id == PostReply.user_id). \
        filter(User.created > utcnow() - timedelta(days=3)). \
        filter(PostReply.posted_at > utcnow() - timedelta(days=3)). \
        filter(PostReply.deleted == False). \
        filter(PostReply.score <= 0).order_by(PostReply.score)
    post_replies = post_replies.paginate(page=replies_page, per_page=100, error_out=False)

    next_url = url_for('admin.admin_content_spam', page=posts.next_num) if posts.has_next else None
    prev_url = url_for('admin.admin_content_spam', page=posts.prev_num) if posts.has_prev and page != 1 else None
    next_url_replies = url_for('admin.admin_content_spam', replies_page=post_replies.next_num) if post_replies.has_next else None
    prev_url_replies = url_for('admin.admin_content_spam', replies_page=post_replies.prev_num) if post_replies.has_prev and replies_page != 1 else None

    return render_template('admin/spam_posts.html', title=_('Likely spam'),
                           next_url=next_url, prev_url=prev_url,
                           next_url_replies=next_url_replies, prev_url_replies=prev_url_replies,
                           posts=posts, post_replies=post_replies,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           show_post_community=True, menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/content/deleted', methods=['GET'])
@login_required
@permission_required('administer all users')
def admin_content_deleted():
    # Shows all soft deleted posts
    page = request.args.get('page', 1, type=int)
    replies_page = request.args.get('replies_page', 1, type=int)

    posts = Post.query.\
        filter(Post.deleted == True).\
        order_by(Post.posted_at)
    posts = posts.paginate(page=page, per_page=100, error_out=False)

    post_replies = PostReply.query. \
        filter(PostReply.deleted == True). \
        order_by(PostReply.posted_at)
    post_replies = post_replies.paginate(page=replies_page, per_page=100, error_out=False)

    next_url = url_for('admin.admin_content_deleted', page=posts.next_num) if posts.has_next else None
    prev_url = url_for('admin.admin_content_deleted', page=posts.prev_num) if posts.has_prev and page != 1 else None
    next_url_replies = url_for('admin.admin_content_deleted', replies_page=post_replies.next_num) if post_replies.has_next else None
    prev_url_replies = url_for('admin.admin_content_deleted', replies_page=post_replies.prev_num) if post_replies.has_prev and replies_page != 1 else None

    return render_template('admin/deleted_posts.html', title=_('Deleted content'),
                           next_url=next_url, prev_url=prev_url,
                           next_url_replies=next_url_replies, prev_url_replies=prev_url_replies,
                           posts=posts, post_replies=post_replies,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           show_post_community=True, menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/approve_registrations', methods=['GET'])
@login_required
@permission_required('approve registrations')
def admin_approve_registrations():
    registrations = UserRegistration.query.filter_by(status=0).order_by(UserRegistration.created_at).all()
    recently_approved = UserRegistration.query.filter_by(status=1).order_by(desc(UserRegistration.approved_at)).limit(30)
    return render_template('admin/approve_registrations.html',
                           registrations=registrations,
                           recently_approved=recently_approved,
                           site=g.site)


@bp.route('/approve_registrations/<int:user_id>/approve', methods=['GET'])
@login_required
@permission_required('approve registrations')
def admin_approve_registrations_approve(user_id):
    user = User.query.get_or_404(user_id)
    registration = UserRegistration.query.filter_by(status=0, user_id=user_id).first()
    if registration:
        registration.status = 1
        registration.approved_at = utcnow()
        registration.approved_by = current_user.id
        db.session.commit()
        if user.verified:
            finalize_user_setup(user)
            send_welcome_email(user, True)

        flash(_('Registration approved.'))

    return redirect(url_for('admin.admin_approve_registrations'))


@bp.route('/user/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('administer all users')
def admin_user_edit(user_id):
    form = EditUserForm()
    user = User.query.get_or_404(user_id)
    if form.validate_on_submit():
        user.bot = form.bot.data
        user.banned = form.banned.data
        user.hide_nsfw = form.hide_nsfw.data
        user.hide_nsfl = form.hide_nsfl.data
        if form.verified.data and not user.verified:
            finalize_user_setup(user)
        user.verified = form.verified.data
        if form.remove_avatar.data and user.avatar_id:
            file = File.query.get(user.avatar_id)
            file.delete_from_disk()
            user.avatar_id = None
            db.session.delete(file)

        if form.remove_banner.data and user.cover_id:
            file = File.query.get(user.cover_id)
            file.delete_from_disk()
            user.cover_id = None
            db.session.delete(file)

        # Update user roles. The UI only lets the user choose 1 role but the DB structure allows for multiple roles per user.
        db.session.execute(text('DELETE FROM user_role WHERE user_id = :user_id'), {'user_id': user.id})
        user.roles.append(Role.query.get(form.role.data))
        if form.role.data == 4:
            flash(_("Permissions are cached for 50 seconds so new admin roles won't take effect immediately."))

        db.session.commit()

        flash(_('Saved'))
        return redirect(url_for('admin.admin_users', local_remote='local' if user.is_local() else 'remote'))
    else:
        if not user.is_local():
            flash(_('This is a remote user - most settings here will be regularly overwritten with data from the original server.'), 'warning')
        form.bot.data = user.bot
        form.verified.data = user.verified
        form.banned.data = user.banned
        form.hide_nsfw.data = user.hide_nsfw
        form.hide_nsfl.data = user.hide_nsfl
        if user.roles and user.roles.count() > 0:
            form.role.data = user.roles[0].id

    return render_template('admin/edit_user.html', title=_('Edit user'), form=form, user=user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/users/add', methods=['GET', 'POST'])
@login_required
@permission_required('administer all users')
def admin_users_add():
    form = AddUserForm()
    user = User()
    if form.validate_on_submit():
        user.user_name = form.user_name.data
        user.title = form.user_name.data
        user.set_password(form.password.data)
        user.about = form.about.data
        user.email = form.email.data
        user.about_html = markdown_to_html(form.about.data)
        user.matrix_user_id = form.matrix_user_id.data
        user.bot = form.bot.data
        profile_file = request.files['profile_file']
        if profile_file and profile_file.filename != '':
            # remove old avatar
            if user.avatar_id:
                file = File.query.get(user.avatar_id)
                file.delete_from_disk()
                user.avatar_id = None
                db.session.delete(file)

            # add new avatar
            file = save_icon_file(profile_file, 'users')
            if file:
                user.avatar = file
        banner_file = request.files['banner_file']
        if banner_file and banner_file.filename != '':
            # remove old cover
            if user.cover_id:
                file = File.query.get(user.cover_id)
                file.delete_from_disk()
                user.cover_id = None
                db.session.delete(file)

            # add new cover
            file = save_banner_file(banner_file, 'users')
            if file:
                user.cover = file
        user.newsletter = form.newsletter.data
        user.ignore_bots = form.ignore_bots.data
        user.hide_nsfw = form.hide_nsfw.data
        user.hide_nsfl = form.hide_nsfl.data

        user.instance_id = 1
        user.roles.append(Role.query.get(form.role.data))
        db.session.add(user)
        db.session.commit()
        finalize_user_setup(user)

        flash(_('User added'))
        return redirect(url_for('admin.admin_users', local_remote='local'))

    return render_template('admin/add_user.html', title=_('Add user'), form=form, user=user,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/user/<int:user_id>/delete', methods=['GET'])
@login_required
@permission_required('administer all users')
def admin_user_delete(user_id):
    user = User.query.get_or_404(user_id)

    user.banned = True  # Unsubscribing everyone could take a long time so until that is completed hide this user from the UI by banning it.
    user.last_active = utcnow()
    db.session.commit()

    if user.is_local():
        unsubscribe_from_everything_then_delete(user.id)
    else:
        user.deleted = True
        user.delete_dependencies()
        db.session.commit()

    flash(_('User deleted'))
    return redirect(url_for('admin.admin_users'))


@bp.route('/reports', methods=['GET'])
@login_required
@permission_required('administer all users')
def admin_reports():

    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    local_remote = request.args.get('local_remote', '')

    reports = Report.query.filter(or_(Report.status == REPORT_STATE_NEW, Report.status == REPORT_STATE_ESCALATED))
    if local_remote == 'local':
        reports = reports.filter_by(source_instance_id=1)
    if local_remote == 'remote':
        reports = reports.filter(Report.source_instance_id != 1)
    reports = reports.order_by(desc(Report.created_at)).paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_reports', page=reports.next_num) if reports.has_next else None
    prev_url = url_for('admin.admin_reports', page=reports.prev_num) if reports.has_prev and page != 1 else None

    return render_template('admin/reports.html', title=_('Reports'), next_url=next_url, prev_url=prev_url, reports=reports,
                           local_remote=local_remote, search=search,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )


@bp.route('/newsletter', methods=['GET', 'POST'])
@login_required
@permission_required('administer all users')
def newsletter():
    form = SendNewsletterForm()
    if form.validate_on_submit():
        send_newsletter(form)
        flash('Newsletter sent')
        return redirect(url_for('admin.newsletter'))

    return render_template("admin/newsletter.html", form=form, title=_('Send newsletter'),
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(),
                           site=g.site
                           )
