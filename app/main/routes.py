import os.path
from datetime import timedelta
from random import randint

import flask
from pyld import jsonld
from sqlalchemy.sql.operators import or_, and_

from app import db, cache
from app.activitypub.util import users_total, active_month, local_posts, local_communities
from app.activitypub.signature import default_context, LDSignature
from app.constants import SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR
from app.email import send_email
from app.inoculation import inoculation
from app.main import bp
from flask import g, session, flash, request, current_app, url_for, redirect, make_response, jsonify
from flask_login import current_user, login_required
from flask_babel import _, get_locale
from sqlalchemy import desc, text
from app.utils import render_template, get_setting, request_etag_matches, return_304, blocked_domains, \
    ap_datetime, shorten_string, markdown_to_text, user_filters_home, \
    joined_communities, moderating_communities, markdown_to_html, allowlist_html, \
    blocked_instances, communities_banned_from, topic_tree, recently_upvoted_posts, recently_downvoted_posts, \
    blocked_users, menu_topics, blocked_communities, get_request
from app.models import Community, CommunityMember, Post, Site, User, utcnow, Topic, Instance, \
    Notification, Language, community_language, ModLog, read_posts


@bp.route('/', methods=['HEAD', 'GET', 'POST'])
@bp.route('/home', methods=['GET', 'POST'])
@bp.route('/home/<sort>', methods=['GET', 'POST'])
@bp.route('/home/<sort>/<view_filter>', methods=['GET', 'POST'])
def index(sort=None, view_filter=None):
    if 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get(
            'Accept', ''):
        return activitypub_application()

    return home_page(sort, view_filter)


def home_page(sort, view_filter):
    verification_warning()

    if sort is None:
        sort = current_user.default_sort if current_user.is_authenticated else 'hot'

    if view_filter is None:
        view_filter = current_user.default_filter if current_user.is_authenticated else 'popular'
        if view_filter is None:
            view_filter = 'subscribed'

    # If nothing has changed since their last visit, return HTTP 304
    current_etag = f"{sort}_{view_filter}_{hash(str(g.site.last_active))}"
    if current_user.is_anonymous and request_etag_matches(current_etag):
        return return_304(current_etag)

    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    if current_user.is_anonymous:
        flash(_('Create an account to tailor this feed to your interests.'))
        posts = Post.query.filter(Post.from_bot == False, Post.nsfw == False, Post.nsfl == False, Post.deleted == False)
        content_filters = {'trump': {'trump', 'elon', 'musk'}}
    else:
        posts = Post.query.filter(Post.deleted == False)

        if current_user.ignore_bots == 1:
            posts = posts.filter(Post.from_bot == False)
        if current_user.hide_nsfl == 1:
            posts = posts.filter(Post.nsfl == False)
        if current_user.hide_nsfw == 1:
            posts = posts.filter(Post.nsfw == False)
        if current_user.hide_read_posts:
            posts = posts.outerjoin(read_posts, (Post.id == read_posts.c.read_post_id) & (read_posts.c.user_id == current_user.id))
            posts = posts.filter(read_posts.c.read_post_id.is_(None))  # Filter where there is no corresponding read post for the current user

        domains_ids = blocked_domains(current_user.id)
        if domains_ids:
            posts = posts.filter(or_(Post.domain_id.not_in(domains_ids), Post.domain_id == None))
        instance_ids = blocked_instances(current_user.id)
        if instance_ids:
            posts = posts.filter(or_(Post.instance_id.not_in(instance_ids), Post.instance_id == None))
        community_ids = blocked_communities(current_user.id)
        if community_ids:
            posts = posts.filter(Post.community_id.not_in(community_ids))
        # filter blocked users
        blocked_accounts = blocked_users(current_user.id)
        if blocked_accounts:
            posts = posts.filter(Post.user_id.not_in(blocked_accounts))
        content_filters = user_filters_home(current_user.id)

    # view filter - subscribed/local/all
    if view_filter == 'subscribed' and current_user.is_authenticated:
        posts = posts.join(CommunityMember, Post.community_id == CommunityMember.community_id).filter(CommunityMember.is_banned == False)
        posts = posts.filter(CommunityMember.user_id == current_user.id)
    elif view_filter == 'local':
        posts = posts.join(Community, Community.id == Post.community_id)
        posts = posts.filter(Community.instance_id == 1)
    elif view_filter == 'popular':
        posts = posts.join(Community, Community.id == Post.community_id)
        posts = posts.filter(Community.show_popular == True, Post.score > 100)
        if current_user.is_anonymous:
            posts = posts.filter(Community.low_quality == False)
    elif view_filter == 'all' or current_user.is_anonymous:
        posts = posts.join(Community, Community.id == Post.community_id)
        posts = posts.filter(Community.show_all == True)

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
    next_url = url_for('main.index', page=posts.next_num, sort=sort, view_filter=view_filter) if posts.has_next else None
    prev_url = url_for('main.index', page=posts.prev_num, sort=sort, view_filter=view_filter) if posts.has_prev and page != 1 else None

    # Active Communities
    active_communities = Community.query.filter_by(banned=False)
    if current_user.is_authenticated:   # do not show communities current user is banned from
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            active_communities = active_communities.filter(Community.id.not_in(banned_from))
        community_ids = blocked_communities(current_user.id)
        if community_ids:
            active_communities = active_communities.filter(Community.id.not_in(community_ids))
    active_communities = active_communities.order_by(desc(Community.last_active)).limit(5).all()

    # Voting history
    if current_user.is_authenticated:
        recently_upvoted = recently_upvoted_posts(current_user.id)
        recently_downvoted = recently_downvoted_posts(current_user.id)
    else:
        recently_upvoted = []
        recently_downvoted = []

    return render_template('index.html', posts=posts, active_communities=active_communities, show_post_community=True,
                           low_bandwidth=low_bandwidth, recently_upvoted=recently_upvoted,
                           recently_downvoted=recently_downvoted,
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           etag=f"{sort}_{view_filter}_{hash(str(g.site.last_active))}", next_url=next_url, prev_url=prev_url,
                           #rss_feed=f"https://{current_app.config['SERVER_NAME']}/feed",
                           #rss_feed_name=f"Posts on " + g.site.name,
                           title=f"{g.site.name} - {g.site.description}",
                           description=shorten_string(markdown_to_text(g.site.sidebar), 150),
                           content_filters=content_filters, sort=sort, view_filter=view_filter,
                           announcement=allowlist_html(get_setting('announcement', '')),
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None
                           )


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
        instance_ids = blocked_instances(current_user.id)
        if instance_ids:
            communities = communities.filter(or_(Community.instance_id.not_in(instance_ids), Community.instance_id == None))
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
                           next_url=next_url, prev_url=prev_url, current_user=current_user,
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
    next_url = url_for('main.list_local_communities', page=communities.next_num, sort_by=sort_by, language_id=language_id) if communities.has_next else None
    prev_url = url_for('main.list_local_communities', page=communities.prev_num, sort_by=sort_by, language_id=language_id) if communities.has_prev and page != 1 else None

    return render_template('list_communities.html', communities=communities, search=search_param, title=_('Local Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           next_url=next_url, prev_url=prev_url, current_user=current_user,
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
        next_url = url_for('main.list_subscribed_communities', page=communities.next_num, sort_by=sort_by, language_id=language_id) if communities.has_next else None
        prev_url = url_for('main.list_subscribed_communities', page=communities.prev_num, sort_by=sort_by, language_id=language_id) if communities.has_prev and page != 1 else None

    else:
        communities = []
        next_url = None
        prev_url = None

    return render_template('list_communities.html', communities=communities, search=search_param, title=_('Joined Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           next_url=next_url, prev_url=prev_url, current_user=current_user,
                           topics=topics, languages=languages, topic_id=topic_id, language_id=language_id, sort_by=sort_by,
                           low_bandwidth=low_bandwidth, moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site)


@bp.route('/communities/notsubscribed', methods=['GET'])
def list_not_subscribed_communities():
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
        # get all communities
        all_communities = Community.query.filter_by(banned=False)
        # get the user's joined communities
        joined_communities = Community.query.filter_by(banned=False).join(CommunityMember).filter(CommunityMember.user_id == current_user.id)
        # get the joined community ids list
        joined_ids = []
        for jc in joined_communities:
            joined_ids.append(jc.id)
        # filter out the joined communities from all communities
        communities = all_communities.filter(Community.id.not_in(joined_ids))

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
        if current_user.hide_nsfw == 1:
            communities = communities.filter(Community.nsfw == False)
        if current_user.hide_nsfl == 1:
            communities = communities.filter(Community.nsfl == False)

        communities = communities.order_by(text('community.' + sort_by))

        # Pagination
        communities = communities.paginate(page=page, per_page=250 if current_user.is_authenticated and not low_bandwidth else 50,
                           error_out=False)
        next_url = url_for('main.list_not_subscribed_communities', page=communities.next_num, sort_by=sort_by, language_id=language_id) if communities.has_next else None
        prev_url = url_for('main.list_not_subscribed_communities', page=communities.prev_num, sort_by=sort_by, language_id=language_id) if communities.has_prev and page != 1 else None

    else:
        communities = []
        next_url = None
        prev_url = None

    return render_template('list_communities.html', communities=communities, search=search_param, title=_('Not Joined Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           next_url=next_url, prev_url=prev_url, current_user=current_user,
                           topics=topics, languages=languages, topic_id=topic_id, language_id=language_id, sort_by=sort_by,
                           low_bandwidth=low_bandwidth, moderating_communities=moderating_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site)

@bp.route('/modlog', methods=['GET'])
def modlog():
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    can_see_names = False

    # Admins can see all of the modlog, everyone else can only see public entries
    if current_user.is_authenticated:
        if current_user.is_admin() or current_user.is_staff():
            modlog_entries = ModLog.query.order_by(desc(ModLog.created_at))
            can_see_names = True
        else:
            modlog_entries = ModLog.query.filter(ModLog.public == True).order_by(desc(ModLog.created_at))
    else:
        modlog_entries = ModLog.query.filter(ModLog.public == True).order_by(desc(ModLog.created_at))

    # Pagination
    modlog_entries = modlog_entries.paginate(page=page, per_page=100 if not low_bandwidth else 50, error_out=False)
    next_url = url_for('main.modlog', page=modlog_entries.next_num) if modlog_entries.has_next else None
    prev_url = url_for('main.modlog', page=modlog_entries.prev_num) if modlog_entries.has_prev and page != 1 else None

    return render_template('modlog.html',
                           title=_('Moderation Log'), modlog_entries=modlog_entries, can_see_names=can_see_names,
                           next_url=next_url, prev_url=prev_url, low_bandwidth=low_bandwidth,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None
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


@bp.route('/replay_inbox')
@login_required
def replay_inbox():
    from app.activitypub.routes import replay_inbox_request

    request_json = {}
    """
    request_json = {"@context": ["https://join-lemmy.org/context.json", "https://www.w3.org/ns/activitystreams"],
                    "actor": "https://lemmy.lemmy/u/doesnotexist",
                    "cc": [],
                    "id": "https://lemmy.lemmy/activities/delete/5d42c8bf-cc60-4d2c-a3b5-673ddb7ce64b",
                    "object": "https://lemmy.lemmy/u/doesnotexist",
                    "to": ["https://www.w3.org/ns/activitystreams#Public"],
                    "type": "Delete"}
    """

    replay_inbox_request(request_json)

    return 'ok'


@bp.route('/test')
def test():

    response = get_request('https://rimu.geek.nz')
    x = ''
    if response.status_code == 200:
        x =response.content
    response.close()
    return x

    json = {
      "@context": "https://www.w3.org/ns/activitystreams",
      "actor": "https://ioc.exchange/users/haiviittech",
      "id": "https://ioc.exchange/users/haiviittech#delete",
      "object": "https://ioc.exchange/users/haiviittech",
      "to": [
        "https://www.w3.org/ns/activitystreams#Public"
      ],
      "type": "Delete"
    }

    r = User.query.get(1)

    jsonld.set_document_loader(jsonld.requests_document_loader(timeout=5))

    ld = LDSignature.create_signature(json, r.private_key, r.public_url() + '#main-key')
    json.update(ld)

    LDSignature.verify_signature(json, r.public_key)

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
