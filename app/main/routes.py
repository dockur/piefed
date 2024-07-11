import os.path
from datetime import datetime, timedelta
from math import log
from random import randint
from time import sleep

import flask
import markdown2
import requests
from flask_caching import CachedResponse
from sqlalchemy.sql.operators import or_, and_

from app import db, cache
from app.activitypub.util import make_image_sizes_async, refresh_user_profile, find_actor_or_create, \
    refresh_community_profile_task, users_total, active_month, local_posts, local_communities, local_comments
from app.activitypub.signature import default_context
from app.constants import SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER, POST_TYPE_IMAGE, POST_TYPE_LINK, \
    SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR, POST_TYPE_VIDEO, POST_TYPE_POLL
from app.email import send_email, send_welcome_email
from app.inoculation import inoculation
from app.main import bp
from flask import g, session, flash, request, current_app, url_for, redirect, make_response, jsonify
from flask_moment import moment
from flask_login import current_user, login_required
from flask_babel import _, get_locale
from sqlalchemy import select, desc, text
from sqlalchemy_searchable import search
from app.utils import render_template, get_setting, gibberish, request_etag_matches, return_304, blocked_domains, \
    ap_datetime, ip_address, retrieve_block_list, shorten_string, markdown_to_text, user_filters_home, \
    joined_communities, moderating_communities, parse_page, theme_list, get_request, markdown_to_html, allowlist_html, \
    blocked_instances, communities_banned_from, topic_tree, recently_upvoted_posts, recently_downvoted_posts, \
    generate_image_from_video_url, blocked_users, microblog_content_to_title, menu_topics, languages_for_form, \
    make_cache_key
from app.models import Community, CommunityMember, Post, Site, User, utcnow, Domain, Topic, File, Instance, \
    InstanceRole, Notification, Language, community_language, PostReply, ModLog


@bp.route('/', methods=['HEAD', 'GET', 'POST'])
@bp.route('/home', methods=['GET', 'POST'])
@bp.route('/home/<sort>', methods=['GET', 'POST'])
@cache.cached(make_cache_key=make_cache_key)
def index(sort=None):
    if 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get(
            'Accept', ''):
        return activitypub_application()

    return CachedResponse(
        response=home_page('home', sort),
        timeout=50 if current_user.is_anonymous else 5,
    )


@bp.route('/media/<path:filename>')
def serve_media(filename):
    path = os.path.abspath(current_app.config["MEDIA_DIR"])
    file = os.path.join(path, filename)
    if os.path.exists(file):
        return flask.send_from_directory(path, filename)
    else:
        abort(404)


@bp.route('/popular', methods=['GET'])
@bp.route('/popular/<sort>', methods=['GET'])
@cache.cached(timeout=5, make_cache_key=make_cache_key)
def popular(sort=None):
    return CachedResponse(
        response=home_page('popular', sort),
        timeout=50 if current_user.is_anonymous else 5,
    )


@bp.route('/all', methods=['GET'])
@bp.route('/all/<sort>', methods=['GET'])
@cache.cached(timeout=5, make_cache_key=make_cache_key)
def all_posts(sort=None):
    return CachedResponse(
        response=home_page('all', sort),
        timeout=50 if current_user.is_anonymous else 5,
    )


def home_page(type, sort):
    verification_warning()

    if sort is None:
        sort = current_user.default_sort if current_user.is_authenticated else 'hot'

    # If nothing has changed since their last visit, return HTTP 304
    current_etag = f"{type}_{sort}_{hash(str(g.site.last_active))}"
    if current_user.is_anonymous and request_etag_matches(current_etag):
        return return_304(current_etag)

    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    if current_user.is_anonymous:
        flash(_('Create an account to tailor this feed to your interests.'))
        posts = Post.query.filter(Post.from_bot == False, Post.nsfw == False, Post.nsfl == False, Post.deleted == False)
        posts = posts.join(Community, Community.id == Post.community_id)
        if type == 'home':
            posts = posts.filter(Community.show_home == True)
        elif type == 'popular':
            posts = posts.filter(Community.show_popular == True).filter(Post.score > 100)
        elif type == 'all':
            posts = posts.filter(Community.show_all == True)
        content_filters = {}
    else:
        if type == 'home':
            posts = Post.query.join(CommunityMember, Post.community_id == CommunityMember.community_id).filter(
                CommunityMember.is_banned == False, Post.deleted == False)
            # posts = posts.join(User, CommunityMember.user_id == User.id).filter(User.id == current_user.id)
            posts = posts.filter(CommunityMember.user_id == current_user.id)
        elif type == 'popular':
            posts = Post.query.filter(Post.from_bot == False)
            posts = posts.join(Community, Community.id == Post.community_id)
            posts = posts.filter(Community.show_popular == True, Post.score > 100, Post.deleted == False)
        elif type == 'all':
            posts = Post.query
            posts = posts.join(Community, Community.id == Post.community_id)
            posts = posts.filter(Community.show_all == True, Post.deleted == False)

        if current_user.ignore_bots == 1:
            posts = posts.filter(Post.from_bot == False)
        if current_user.hide_nsfl == 1:
            posts = posts.filter(Post.nsfl == False)
        if current_user.hide_nsfw == 1:
            posts = posts.filter(Post.nsfw == False)

        domains_ids = blocked_domains(current_user.id)
        if domains_ids:
            posts = posts.filter(or_(Post.domain_id.not_in(domains_ids), Post.domain_id == None))
        instance_ids = blocked_instances(current_user.id)
        if instance_ids:
            posts = posts.filter(or_(Post.instance_id.not_in(instance_ids), Post.instance_id == None))
        # filter blocked users
        blocked_accounts = blocked_users(current_user.id)
        if blocked_accounts:
            posts = posts.filter(Post.user_id.not_in(blocked_accounts))
        content_filters = user_filters_home(current_user.id)

    # Sorting
    if sort == 'hot':
        posts = posts.order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort == 'top':
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=1)).order_by(desc(Post.up_votes - Post.down_votes))
    elif sort == 'new':
        posts = posts.order_by(desc(Post.posted_at))
    elif sort == 'active':
        posts = posts.order_by(desc(Post.last_active))

    # Pagination
    posts = posts.paginate(page=page, per_page=100 if current_user.is_authenticated and not low_bandwidth else 50, error_out=False)
    if type == 'home':
        next_url = url_for('main.index', page=posts.next_num, sort=sort) if posts.has_next else None
        prev_url = url_for('main.index', page=posts.prev_num, sort=sort) if posts.has_prev and page != 1 else None
    elif type == 'popular':
        next_url = url_for('main.popular', page=posts.next_num, sort=sort) if posts.has_next else None
        prev_url = url_for('main.popular', page=posts.prev_num, sort=sort) if posts.has_prev and page != 1 else None
    elif type == 'all':
        next_url = url_for('main.all_posts', page=posts.next_num, sort=sort) if posts.has_next else None
        prev_url = url_for('main.all_posts', page=posts.prev_num, sort=sort) if posts.has_prev and page != 1 else None

    # Active Communities
    active_communities = Community.query.filter_by(banned=False)
    if current_user.is_authenticated:   # do not show communities current user is banned from
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            active_communities = active_communities.filter(Community.id.not_in(banned_from))
    active_communities = active_communities.order_by(desc(Community.last_active)).limit(5).all()

    # Voting history
    if current_user.is_authenticated:
        recently_upvoted = recently_upvoted_posts(current_user.id)
        recently_downvoted = recently_downvoted_posts(current_user.id)
    else:
        recently_upvoted = []
        recently_downvoted = []

    return render_template('index.html', posts=posts, active_communities=active_communities, show_post_community=True,
                           POST_TYPE_IMAGE=POST_TYPE_IMAGE, POST_TYPE_LINK=POST_TYPE_LINK, POST_TYPE_VIDEO=POST_TYPE_VIDEO, POST_TYPE_POLL=POST_TYPE_POLL,
                           low_bandwidth=low_bandwidth, recently_upvoted=recently_upvoted,
                           recently_downvoted=recently_downvoted,
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           etag=f"{type}_{sort}_{hash(str(g.site.last_active))}", next_url=next_url, prev_url=prev_url,
                           #rss_feed=f"https://{current_app.config['SERVER_NAME']}/feed",
                           #rss_feed_name=f"Posts on " + g.site.name,
                           title=f"{g.site.name} - {g.site.description}",
                           description=shorten_string(markdown_to_text(g.site.sidebar), 150),
                           content_filters=content_filters, type=type, sort=sort,
                           announcement=allowlist_html(get_setting('announcement', '')),
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)])


@bp.route('/topics', methods=['GET'])
def list_topics():
    verification_warning()
    topics = topic_tree()

    return render_template('list_topics.html', topics=topics, title=_('Browse by topic'),
                           low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1',
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site)


@bp.route('/communities', methods=['GET'])
def list_communities():
    verification_warning()
    search_param = request.args.get('search', '')
    topic_id = int(request.args.get('topic_id', 0))
    language_id = int(request.args.get('language_id', 0))
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    sort_by = request.args.get('sort_by', 'post_reply_count desc')
    topics = Topic.query.order_by(Topic.name).all()
    languages = Language.query.order_by(Language.name).all()
    communities = Community.query.filter_by(banned=False)
    if search_param == '':
        pass
    else:
        communities = communities.filter(or_(Community.title.ilike(f"%{search_param}%"), Community.ap_id.ilike(f"%{search_param}%")))

    if topic_id != 0:
        communities = communities.filter_by(topic_id=topic_id)

    if language_id != 0:
        communities = communities.join(community_language).filter(community_language.c.language_id == language_id)

    if current_user.is_authenticated:
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))
        if current_user.hide_nsfw == 1:
            communities = communities.filter(Community.nsfw == False)
        if current_user.hide_nsfl == 1:
            communities = communities.filter(Community.nsfl == False)
    else:
        communities = communities.filter(and_(Community.nsfw == False, Community.nsfl == False))

    communities = communities.order_by(text('community.' + sort_by))

    # Pagination
    communities = communities.paginate(page=page, per_page=250 if current_user.is_authenticated and not low_bandwidth else 50,
                           error_out=False)
    next_url = url_for('main.list_communities', page=communities.next_num, sort_by=sort_by, language_id=language_id) if communities.has_next else None
    prev_url = url_for('main.list_communities', page=communities.prev_num, sort_by=sort_by, language_id=language_id) if communities.has_prev and page != 1 else None

    return render_template('list_communities.html', communities=communities, search=search_param, title=_('Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           next_url=next_url, prev_url=prev_url,
                           topics=topics, languages=languages, topic_id=topic_id, language_id=language_id, sort_by=sort_by,
                           low_bandwidth=low_bandwidth, moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site)


@bp.route('/communities/local', methods=['GET'])
def list_local_communities():
    verification_warning()
    search_param = request.args.get('search', '')
    topic_id = int(request.args.get('topic_id', 0))
    language_id = int(request.args.get('language_id', 0))
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    sort_by = request.args.get('sort_by', 'post_reply_count desc')
    topics = Topic.query.order_by(Topic.name).all()
    languages = Language.query.order_by(Language.name).all()
    communities = Community.query.filter_by(ap_id=None, banned=False)
    if search_param == '':
        pass
    else:
        communities = communities.filter(or_(Community.title.ilike(f"%{search_param}%"), Community.ap_id.ilike(f"%{search_param}%")))

    if topic_id != 0:
        communities = communities.filter_by(topic_id=topic_id)

    if language_id != 0:
        communities = communities.join(community_language).filter(community_language.c.language_id == language_id)

    if current_user.is_authenticated:
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))
        if current_user.hide_nsfw == 1:
            communities = communities.filter(Community.nsfw == False)
        if current_user.hide_nsfl == 1:
            communities = communities.filter(Community.nsfl == False)
    else:
        communities = communities.filter(and_(Community.nsfw == False, Community.nsfl == False))

    communities = communities.order_by(text('community.' + sort_by))

    # Pagination
    communities = communities.paginate(page=page, per_page=250 if current_user.is_authenticated and not low_bandwidth else 50,
                           error_out=False)
    next_url = url_for('main.list_communities', page=communities.next_num, sort_by=sort_by, language_id=language_id) if communities.has_next else None
    prev_url = url_for('main.list_communities', page=communities.prev_num, sort_by=sort_by, language_id=language_id) if communities.has_prev and page != 1 else None

    return render_template('list_communities.html', communities=communities, search=search_param, title=_('Local Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           next_url=next_url, prev_url=prev_url,
                           topics=topics, languages=languages, topic_id=topic_id, language_id=language_id, sort_by=sort_by,
                           low_bandwidth=low_bandwidth, moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site)


@bp.route('/communities/subscribed', methods=['GET'])
def list_subscribed_communities():
    verification_warning()
    search_param = request.args.get('search', '')
    topic_id = int(request.args.get('topic_id', 0))
    language_id = int(request.args.get('language_id', 0))
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    sort_by = request.args.get('sort_by', 'post_reply_count desc')
    topics = Topic.query.order_by(Topic.name).all()
    languages = Language.query.order_by(Language.name).all()
    if current_user.is_authenticated:
        communities = Community.query.filter_by(banned=False).join(CommunityMember).filter(CommunityMember.user_id == current_user.id)

        if search_param == '':
            pass
        else:
            communities = communities.filter(or_(Community.title.ilike(f"%{search_param}%"), Community.ap_id.ilike(f"%{search_param}%")))

        if topic_id != 0:
            communities = communities.filter_by(topic_id=topic_id)

        if language_id != 0:
            communities = communities.join(community_language).filter(community_language.c.language_id == language_id)

        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))

        communities = communities.order_by(text('community.' + sort_by))

        # Pagination
        communities = communities.paginate(page=page, per_page=250 if current_user.is_authenticated and not low_bandwidth else 50,
                           error_out=False)
        next_url = url_for('main.list_communities', page=communities.next_num, sort_by=sort_by, language_id=language_id) if communities.has_next else None
        prev_url = url_for('main.list_communities', page=communities.prev_num, sort_by=sort_by, language_id=language_id) if communities.has_prev and page != 1 else None

    else:
        communities = []
        next_url = None
        prev_url = None

    return render_template('list_communities.html', communities=communities, search=search_param, title=_('Joined Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           next_url=next_url, prev_url=prev_url,
                           topics=topics, languages=languages, topic_id=topic_id, language_id=language_id, sort_by=sort_by,
                           low_bandwidth=low_bandwidth, moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site)


@bp.route('/modlog', methods=['GET'])
def modlog():
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    # Admins can see all of the modlog, everyone else can only see public entries
    if current_user.is_authenticated:
        if current_user.is_admin() or current_user.is_staff():
            modlog_entries = ModLog.query.order_by(desc(ModLog.created_at))
        else:
            modlog_entries = ModLog.query.filter(ModLog.public == True).order_by(desc(ModLog.created_at))
    else:
        modlog_entries = ModLog.query.filter(ModLog.public == True).order_by(desc(ModLog.created_at))

    # Pagination
    modlog_entries = modlog_entries.paginate(page=page, per_page=100 if not low_bandwidth else 50, error_out=False)
    next_url = url_for('main.modlog', page=modlog_entries.next_num) if modlog_entries.has_next else None
    prev_url = url_for('main.modlog', page=modlog_entries.prev_num) if modlog_entries.has_prev and page != 1 else None

    return render_template('modlog.html',
                           title=_('Moderation Log'), modlog_entries=modlog_entries,
                           next_url=next_url, prev_url=prev_url, low_bandwidth=low_bandwidth,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)]
                           )


@bp.route('/donate')
def donate():
    return render_template('donate.html')


@bp.route('/about')
def about_page():

    user_amount = users_total()
    MAU = active_month()
    posts_amount = local_posts()

    admins = Site.admins()
    staff = Site.staff()
    domains_amount = db.session.execute(text('SELECT COUNT(id) as c FROM "domain" WHERE "banned" IS false')).scalar()
    community_amount = local_communities()
    instance = Instance.query.filter_by(id=1).first()
    
    return render_template('about.html', user_amount=user_amount, mau=MAU, posts_amount=posts_amount,
                           domains_amount=domains_amount, community_amount=community_amount, instance=instance,
                           admins=admins, staff=staff)


@bp.route('/privacy')
def privacy():
    return render_template('privacy.html')


@bp.route('/login')
def login():
    return redirect(url_for('auth.login'))


@bp.route('/robots.txt')
def robots():
    resp = make_response(render_template('robots.txt'))
    resp.mimetype = 'text/plain'
    return resp


@bp.route('/sitemap.xml')
@cache.cached(timeout=6000)
def sitemap():
    posts = Post.query.filter(Post.from_bot == False, Post.deleted == False)
    posts = posts.join(Community, Community.id == Post.community_id)
    posts = posts.filter(Community.show_all == True, Community.ap_id == None)   # sitemap.xml only includes local posts
    if not g.site.enable_nsfw:
        posts = posts.filter(Community.nsfw == False)
    if not g.site.enable_nsfl:
        posts = posts.filter(Community.nsfl == False)
    posts = posts.order_by(desc(Post.posted_at))

    resp = make_response(render_template('sitemap.xml', posts=posts, current_app=current_app))
    resp.mimetype = 'text/xml'
    return resp


@bp.route('/keyboard_shortcuts')
def keyboard_shortcuts():
    return render_template('keyboard_shortcuts.html')


def list_files(directory):
    for root, dirs, files in os.walk(directory):
        for file in files:
            yield os.path.join(root, file)


@bp.route('/test')
def test():

    x = languages_for_form()

    #for community in Community.query.filter(Community.content_retention != -1):
    #    for post in community.posts.filter(Post.posted_at < utcnow() - timedelta(days=Community.content_retention)):
    #        post.delete_dependencies()

    return 'done'


    md = "::: spoiler I'm all for ya having fun and your right to hurt yourself.\n\nI am a former racer, commuter, and professional Buyer for a chain of bike shops. I'm also disabled from the crash involving the 6th and 7th cars that have hit me in the last 170k+ miles of riding. I only barely survived what I simplify as a \"broken neck and back.\" Cars making U-turns are what will get you if you ride long enough, \n\nespecially commuting. It will look like just another person turning in front of you, you'll compensate like usual, and before your brain can even register what is really happening, what was your normal escape route will close and you're going to crash really hard. It is the only kind of crash that your intuition is useless against.\n:::"

    return markdown_to_html(md)

    users_to_notify = User.query.join(Notification, User.id == Notification.user_id).filter(
            User.ap_id == None,
            Notification.created_at > User.last_seen,
            Notification.read == False,
            User.email_unread_sent == False,    # they have not been emailed since last activity
            User.email_unread == True           # they want to be emailed
    ).all()

    for user in users_to_notify:
        notifications = Notification.query.filter(Notification.user_id == user.id, Notification.read == False,
                                                  Notification.created_at > user.last_seen).all()
        if notifications:
            # Also get the top 20 posts since their last login
            posts = Post.query.join(CommunityMember, Post.community_id == CommunityMember.community_id).filter(
                CommunityMember.is_banned == False)
            posts = posts.filter(CommunityMember.user_id == user.id)
            if user.ignore_bots == 1:
                posts = posts.filter(Post.from_bot == False)
            if user.hide_nsfl == 1:
                posts = posts.filter(Post.nsfl == False)
            if user.hide_nsfw == 1:
                posts = posts.filter(Post.nsfw == False)
            domains_ids = blocked_domains(user.id)
            if domains_ids:
                posts = posts.filter(or_(Post.domain_id.not_in(domains_ids), Post.domain_id == None))
            posts = posts.filter(Post.posted_at > user.last_seen).order_by(desc(Post.score))
            posts = posts.limit(20).all()

            # Send email!
            send_email(_('[PieFed] You have unread notifications'),
                       sender=f'{g.site.name} <{current_app.config["MAIL_FROM"]}>',
                       recipients=[user.email],
                       text_body=flask.render_template('email/unread_notifications.txt', user=user, notifications=notifications),
                       html_body=flask.render_template('email/unread_notifications.html', user=user,
                                                 notifications=notifications,
                                                 posts=posts,
                                                 domain=current_app.config['SERVER_NAME']))
            user.email_unread_sent = True
            db.session.commit()


    return 'ok'


@bp.route('/test_email')
def test_email():
    if current_user.is_anonymous:
        email = request.args.get('email')
    else:
        email = current_user.email
    send_email('This is a test email', f'{g.site.name} <{current_app.config["MAIL_FROM"]}>', [email],
               'This is a test email. If you received this, email sending is working!',
               '<p>This is a test email. If you received this, email sending is working!</p>')
    return f'Email sent to {email}.'


@bp.route('/find_voters')
def find_voters():
    user_ids = db.session.execute(text('SELECT id from "user" ORDER BY last_seen DESC LIMIT 5000')).scalars()
    voters = {}
    for user_id in user_ids:
        recently_downvoted = recently_downvoted_posts(user_id)
        if len(recently_downvoted) > 10:
            voters[user_id] = str(recently_downvoted)

    return str(find_duplicate_values(voters))


def find_duplicate_values(dictionary):
    # Create a dictionary to store the keys for each value
    value_to_keys = {}

    # Iterate through the input dictionary
    for key, value in dictionary.items():
        # If the value is not already in the dictionary, add it
        if value not in value_to_keys:
            value_to_keys[value] = [key]
        else:
            # If the value is already in the dictionary, append the key to the list
            value_to_keys[value].append(key)

    # Filter out the values that have only one key (i.e., unique values)
    duplicates = {value: keys for value, keys in value_to_keys.items() if len(keys) > 1}

    return duplicates

def verification_warning():
    if hasattr(current_user, 'verified') and current_user.verified is False:
        flash(_('Please click the link in your email inbox to verify your account.'), 'warning')


@cache.cached(timeout=6)
def activitypub_application():
    application_data = {
        '@context': default_context(),
        'type': 'Application',
        'id': f"https://{current_app.config['SERVER_NAME']}/",
        'name': 'PieFed',
        'summary': g.site.name + ' - ' + g.site.description,
        'published': ap_datetime(g.site.created_at),
        'updated': ap_datetime(g.site.updated),
        'inbox': f"https://{current_app.config['SERVER_NAME']}/site_inbox",
        'outbox': f"https://{current_app.config['SERVER_NAME']}/site_outbox",
        'icon': {
          'type': 'Image',
          'url': f"https://{current_app.config['SERVER_NAME']}/static/images/logo2.png"
        },
        'publicKey': {
          'id': f"https://{current_app.config['SERVER_NAME']}/#main-key",
          'owner': f"https://{current_app.config['SERVER_NAME']}/",
          'publicKeyPem': g.site.public_key
        }
    }
    resp = jsonify(application_data)
    resp.content_type = 'application/activity+json'
    return resp


# instance actor (literally uses the word 'actor' without the /u/)
# required for interacting with instances using 'secure mode' (aka authorized fetch)
@bp.route('/actor', methods=['GET'])
def instance_actor():
    application_data = {
        '@context': default_context(),
        'type': 'Application',
        'id': f"https://{current_app.config['SERVER_NAME']}/actor",
        'preferredUsername': f"{current_app.config['SERVER_NAME']}",
        'url': f"https://{current_app.config['SERVER_NAME']}/about",
        'manuallyApprovesFollowers': True,
        'inbox': f"https://{current_app.config['SERVER_NAME']}/actor/inbox",
        'outbox': f"https://{current_app.config['SERVER_NAME']}/actor/outbox",
        'publicKey': {
          'id': f"https://{current_app.config['SERVER_NAME']}/actor#main-key",
          'owner': f"https://{current_app.config['SERVER_NAME']}/actor",
          'publicKeyPem': g.site.public_key
        },
        'endpoints': {
          'sharedInbox': f"https://{current_app.config['SERVER_NAME']}/site_inbox",
        }
    }
    resp = jsonify(application_data)
    resp.content_type = 'application/activity+json'
    return resp
