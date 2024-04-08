import os.path
from datetime import datetime, timedelta
from math import log
from random import randint

import flask
import markdown2
import requests
from sqlalchemy.sql.operators import or_, and_

from app import db, cache
from app.activitypub.util import default_context, make_image_sizes_async, refresh_user_profile, find_actor_or_create, \
    refresh_community_profile_task
from app.constants import SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER, POST_TYPE_IMAGE, POST_TYPE_LINK, \
    SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR
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
    blocked_instances, communities_banned_from, topic_tree
from app.models import Community, CommunityMember, Post, Site, User, utcnow, Domain, Topic, File, Instance, \
    InstanceRole, Notification
from PIL import Image
import pytesseract


@bp.route('/', methods=['HEAD', 'GET', 'POST'])
@bp.route('/home', methods=['GET', 'POST'])
@bp.route('/home/<sort>', methods=['GET', 'POST'])
def index(sort=None):
    if 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get(
            'Accept', ''):
        return activitypub_application()

    return home_page('home', sort)


@bp.route('/popular', methods=['GET'])
@bp.route('/popular/<sort>', methods=['GET'])
def popular(sort=None):
    return home_page('popular', sort)


@bp.route('/all', methods=['GET'])
@bp.route('/all/<sort>', methods=['GET'])
def all_posts(sort=None):
    return home_page('all', sort)


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
        posts = Post.query.filter(Post.from_bot == False, Post.nsfw == False, Post.nsfl == False)
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
                CommunityMember.is_banned == False)
            # posts = posts.join(User, CommunityMember.user_id == User.id).filter(User.id == current_user.id)
            posts = posts.filter(CommunityMember.user_id == current_user.id)
        elif type == 'popular':
            posts = Post.query.filter(Post.from_bot == False)
            posts = posts.join(Community, Community.id == Post.community_id)
            posts = posts.filter(Community.show_popular == True, Post.score > 100)
        elif type == 'all':
            posts = Post.query
            posts = posts.join(Community, Community.id == Post.community_id)
            posts = posts.filter(Community.show_all == True)

        if current_user.ignore_bots:
            posts = posts.filter(Post.from_bot == False)
        if current_user.show_nsfl is False:
            posts = posts.filter(Post.nsfl == False)
        if current_user.show_nsfw is False:
            posts = posts.filter(Post.nsfw == False)

        domains_ids = blocked_domains(current_user.id)
        if domains_ids:
            posts = posts.filter(or_(Post.domain_id.not_in(domains_ids), Post.domain_id == None))
        instance_ids = blocked_instances(current_user.id)
        if instance_ids:
            posts = posts.filter(or_(Post.instance_id.not_in(instance_ids), Post.instance_id == None))
        content_filters = user_filters_home(current_user.id)

    # Sorting
    if sort == 'hot':
        posts = posts.order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort == 'top':
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=1)).order_by(desc(Post.score))
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

    return render_template('index.html', posts=posts, active_communities=active_communities, show_post_community=True,
                           POST_TYPE_IMAGE=POST_TYPE_IMAGE, POST_TYPE_LINK=POST_TYPE_LINK,
                           low_bandwidth=low_bandwidth,
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           etag=f"{type}_{sort}_{hash(str(g.site.last_active))}", next_url=next_url, prev_url=prev_url,
                           #rss_feed=f"https://{current_app.config['SERVER_NAME']}/feed",
                           #rss_feed_name=f"Posts on " + g.site.name,
                           title=f"{g.site.name} - {g.site.description}",
                           description=shorten_string(markdown_to_text(g.site.sidebar), 150),
                           content_filters=content_filters, type=type, sort=sort,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           inoculation=inoculation[randint(0, len(inoculation) - 1)])


@bp.route('/topics', methods=['GET'])
def list_topics():
    verification_warning()
    topics = topic_tree()

    return render_template('list_topics.html', topics=topics, title=_('Browse by topic'),
                           low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1',
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()))


@bp.route('/communities', methods=['GET'])
def list_communities():
    verification_warning()
    search_param = request.args.get('search', '')
    topic_id = int(request.args.get('topic_id', 0))
    sort_by = text('community.' + request.args.get('sort_by') if request.args.get('sort_by') else 'community.post_reply_count desc')
    topics = Topic.query.order_by(Topic.name).all()
    communities = Community.query.filter_by(banned=False)
    if search_param == '':
        pass
    else:
        communities = communities.filter(or_(Community.title.ilike(f"%{search_param}%"), Community.ap_id.ilike(f"%{search_param}%")))
        #query = search(select(Community), search_param, sort=True)  # todo: exclude banned communities from search
        #communities = db.session.scalars(query).all()


    if topic_id != 0:
        communities = communities.filter_by(topic_id=topic_id)

    if current_user.is_authenticated:
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))

    return render_template('list_communities.html', communities=communities.order_by(sort_by).all(), search=search_param, title=_('Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           topics=topics, topic_id=topic_id, sort_by=sort_by,
                           low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1', moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()))


@bp.route('/communities/local', methods=['GET'])
def list_local_communities():
    verification_warning()
    sort_by = text('community.' + request.args.get('sort_by') if request.args.get('sort_by') else 'community.post_reply_count desc')
    communities = Community.query.filter_by(ap_id=None, banned=False)
    return render_template('list_communities.html', communities=communities.order_by(sort_by).all(), title=_('Local communities'), sort_by=sort_by,
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1', moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()))


@bp.route('/communities/subscribed', methods=['GET'])
def list_subscribed_communities():
    verification_warning()
    sort_by = text('community.' + request.args.get('sort_by') if request.args.get('sort_by') else 'community.post_reply_count desc')
    if current_user.is_authenticated:
        communities = Community.query.filter_by(banned=False).join(CommunityMember).filter(CommunityMember.user_id == current_user.id).order_by(sort_by).all()
    else:
        communities = []
    return render_template('list_communities.html', communities=communities, title=_('Joined communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER, sort_by=sort_by,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1', moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()))


@bp.route('/donate')
def donate():
    return render_template('donate.html')

@bp.route('/about')
def about_page():

    users = User.query.filter_by(ap_id=None, deleted=False, banned=False).all()
    user_amount = len(users)
    # Todo, figure out how to filter the user list with the list of user_role user_id == 4
    #admins = users.filter()
    # Todo, figure out how to filter the user list with the list of user_role user_id == 4
    #staff = users.filter()
    
    domains_amount = len(Domain.query.filter_by(banned=False).all())
    community_amount = len(Community.query.all())
    instance = Instance.query.filter_by(id=1).first()
    

    return render_template('about.html', user_amount=user_amount, domains_amount=domains_amount, community_amount=community_amount, instance=instance)#, admins=admins)


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
    posts = Post.query.filter(Post.from_bot == False)
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
    return ''

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
            if user.ignore_bots:
                posts = posts.filter(Post.from_bot == False)
            if user.show_nsfl is False:
                posts = posts.filter(Post.nsfl == False)
            if user.show_nsfw is False:
                posts = posts.filter(Post.nsfw == False)
            domains_ids = blocked_domains(user.id)
            if domains_ids:
                posts = posts.filter(or_(Post.domain_id.not_in(domains_ids), Post.domain_id == None))
            posts = posts.filter(Post.posted_at > user.last_seen).order_by(desc(Post.score))
            posts = posts.limit(20).all()

            # Send email!
            send_email(_('[PieFed] You have unread notifications'),
                       sender=f'PieFed <noreply@{current_app.config["SERVER_NAME"]}>',
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
@login_required
def test_email():
    send_email('This is a test email', f'{g.site.name} <{current_app.config["MAIL_FROM"]}>', [current_user.email],
               'This is a test email. If you received this, email sending is working!',
               '<p>This is a test email. If you received this, email sending is working!</p>')
    return f'Email sent to {current_user.email}.'


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
    }
    resp = jsonify(application_data)
    resp.content_type = 'application/activity+json'
    return resp
