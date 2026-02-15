import os.path
import json
from datetime import timedelta, datetime
from random import randint

import flask
from pyld import jsonld
from sqlalchemy import or_, and_
from ua_parser import parse as uaparse

from app import db, cache
from app.activitypub.util import users_total, active_month, local_posts, local_communities, \
    lemmy_site_data, is_activitypub_request
from app.activitypub.signature import default_context, LDSignature, HttpSignature
from app.admin.util import topics_for_form
from app.api.alpha.utils.misc import get_resolve_object
from app.constants import SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR, \
    POST_STATUS_REVIEWING
from app.email import send_email, send_registration_approved_email
from app.inoculation import inoculation
from app.main import bp
from flask import g, flash, request, current_app, url_for, redirect, make_response, jsonify, send_file, abort
from flask_login import current_user
from flask_babel import _
from sqlalchemy import desc, text

from app.main.forms import ShareLinkForm, ContentWarningForm
from app.post.routes import show_post
from app.shared.tasks.maintenance import refresh_instance_chooser
from app.translation import LibreTranslateAPI
from app.utils import render_template, get_setting, request_etag_matches, return_304, blocked_domains, \
    ap_datetime, shorten_string, user_filters_home, \
    joined_communities, moderating_communities, markdown_to_html, \
    blocked_or_banned_instances, communities_banned_from, topic_tree, recently_upvoted_posts, recently_downvoted_posts, \
    menu_topics, blocked_communities, \
    permission_required, debug_mode_only, ip_address, menu_instance_feeds, menu_my_feeds, menu_subscribed_feeds, \
    feed_tree_public, gibberish, get_deduped_post_ids, paginate_post_ids, post_ids_to_models, html_to_text, \
    get_redis_connection, subscribed_feeds, joined_or_modding_communities, login_required_if_private_instance, \
    pending_communities, retrieve_image_hash, possible_communities, remove_tracking_from_link, reported_posts, \
    moderating_communities_ids, user_notes, login_required, safe_order_by, filtered_out_communities, \
    num_topics, referrer, block_honey_pot, user_pronouns, get_instance_stickies
from app.models import Community, CommunityMember, Post, Site, User, utcnow, Topic, Instance, \
    Notification, Language, community_language, ModLog, Feed, FeedItem, CmsPage
from app.ldap_utils import test_ldap_connection, sync_user_to_ldap, login_with_ldap


@bp.route('/', methods=['HEAD', 'GET', 'POST'])
@bp.route('/home', methods=['GET', 'POST'])
@bp.route('/home/<sort>', methods=['GET', 'POST'])
@bp.route('/home/<sort>/<view_filter>', methods=['GET', 'POST'])
@login_required_if_private_instance
def index(sort=None, view_filter=None):
    if 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get(
            'Accept', ''):
        return activitypub_application()

    if sort is None:
        sort = current_user.default_sort if current_user.is_authenticated else 'hot'

    if view_filter is None:
        view_filter = current_user.default_filter if current_user.is_authenticated else g.site.default_filter
        # anonymous users cannot use "subscribed"
        if current_user.is_anonymous and view_filter == 'subscribed':
            view_filter = 'popular'
        if view_filter is None:
            view_filter = 'subscribed' if current_user.is_authenticated else 'popular'

    # If nothing has changed since their last visit, return HTTP 304
    current_etag = f"{sort}_{view_filter}_{hash(str(g.site.last_active))}"
    if current_user.is_anonymous and request_etag_matches(current_etag):
        return return_304(current_etag)

    verification_warning()
    block_honey_pot()

    return home_page(sort, view_filter,
                     page=request.args.get('page', 0, type=int),
                     result_id=request.args.get('result_id', gibberish(15)) if current_user.is_authenticated else None,
                     low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1',
                     tag=request.args.get('tag', ''))


def home_page_cache_key(sort, view_filter, page, result_id, low_bandwidth, tag):
    # this is almost the same as cache.memoized except prepends current_user.get_id() which is 0 for all anonymous users
    return f"{current_user.get_id()}_{sort}_{view_filter}_{page}_{result_id}_{low_bandwidth}_{tag}"


@cache.cached(timeout=3, make_cache_key=home_page_cache_key)
def home_page(sort, view_filter, page, result_id, low_bandwidth, tag):

    page_length = 20 if low_bandwidth else current_app.config['PAGE_LENGTH']

    # view filter - subscribed/local/all
    community_ids = [-1]
    low_quality_filter = 'AND c.low_quality is false' if current_user.is_authenticated and current_user.hide_low_quality else ''
    if current_user.is_authenticated:
        modded_communities = moderating_communities_ids(current_user.id)
    else:
        modded_communities = []
    enable_mod_filter = len(modded_communities) > 0

    if view_filter == 'subscribed' and current_user.is_authenticated:
        community_ids = db.session.execute(text(
            'SELECT id FROM community as c INNER JOIN community_member as cm ON cm.community_id = c.id WHERE cm.is_banned is false AND cm.user_id = :user_id'),
                                           {'user_id': current_user.id}).scalars()
    elif view_filter == 'local':
        if current_user.is_anonymous:
            community_ids = db.session.execute(
                text(f'SELECT id FROM community as c WHERE c.private is false and c.instance_id = 1 {low_quality_filter}')).scalars()
        else:
            community_ids = db.session.execute(
                text(f'SELECT id FROM community as c WHERE c.instance_id = 1 {low_quality_filter}')).scalars()
    elif view_filter == 'popular':
        if current_user.is_anonymous:
            community_ids = db.session.execute(
                text('SELECT id FROM community as c WHERE c.show_popular is true and c.private is false AND c.low_quality is false')).scalars()
        else:
            community_ids = db.session.execute(
                text(f'SELECT id FROM community as c WHERE c.show_popular is true {low_quality_filter}')).scalars()
    elif view_filter == 'all' or current_user.is_anonymous:
        community_ids = [-1]  # Special value to indicate 'All'
    elif view_filter == 'moderating':
        community_ids = modded_communities
    
    community_ids = list(community_ids)

    post_ids = get_deduped_post_ids(result_id, community_ids, sort, tag)
    has_next_page = len(post_ids) > page + 1 * page_length
    post_ids = paginate_post_ids(post_ids, page, page_length=page_length)
    posts = post_ids_to_models(post_ids, sort)

    if page == 0:
        # First page of the home feed, include any instance-wide stickies if they are present and visible
        instance_stickies = get_instance_stickies(community_ids=community_ids, sort=sort)
    else:
        instance_stickies = []

    if current_user.is_anonymous:
        flash(_('Create an account to tailor this feed to your interests.'))
        content_filters = {'-1': {'trump', 'elon', 'musk'}}
    else:
        content_filters = user_filters_home(current_user.id)

    # Pagination
    next_url = url_for('main.index', page=page + 1, sort=sort, view_filter=view_filter,
                       result_id=result_id) if has_next_page else None
    prev_url = url_for('main.index', page=page - 1, sort=sort, view_filter=view_filter,
                       result_id=result_id) if page > 0 else None

    # Active Communities
    active_communities = Community.query.filter_by(banned=False).filter_by(nsfw=False).filter_by(nsfl=False).filter_by(private=False)
    if current_user.is_authenticated:  # do not show communities current user is banned from
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            active_communities = active_communities.filter(Community.id.not_in(banned_from))
        community_ids = blocked_communities(current_user.id)
        if community_ids:
            active_communities = active_communities.filter(Community.id.not_in(community_ids))
        active_communities = active_communities.filter(Community.instance_id.not_in(blocked_or_banned_instances(current_user.id)))

    active_communities = active_communities.order_by(desc(Community.last_active)).limit(5).all()

    # New Communities
    cutoff = utcnow() - timedelta(days=30)
    new_communities = Community.query.filter_by(banned=False).filter_by(nsfw=False).filter_by(nsfl=False).\
        filter_by(private=False).filter(Community.created_at > cutoff)
    if current_user.is_authenticated:  # do not show communities current user is banned from
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            new_communities = new_communities.filter(Community.id.not_in(banned_from))
        community_ids = blocked_communities(current_user.id)
        if community_ids:
            new_communities = new_communities.filter(Community.id.not_in(community_ids))
        new_communities = new_communities.filter(Community.instance_id.not_in(blocked_or_banned_instances(current_user.id)))
    new_communities = new_communities.order_by(desc(Community.first_federated_at)). \
        order_by(desc(Community.created_at)).limit(5).all()

    # Upcoming events
    upcoming_events = db.session.execute(text("""SELECT e.start, p.title, p.id FROM "event" e
                                                 INNER JOIN post p on e.post_id = p.id
                                                 WHERE e.start > now() AND p.deleted is false 
                                                 AND p.status > :reviewing
                                                 ORDER BY e.start LIMIT 5"""),
                                         {'reviewing': POST_STATUS_REVIEWING}).all()

    # Voting history and ban status
    if current_user.is_authenticated:
        recently_upvoted = recently_upvoted_posts(current_user.id)
        recently_downvoted = recently_downvoted_posts(current_user.id)
        communities_banned_from_list = communities_banned_from(current_user.id)
    else:
        recently_upvoted = []
        recently_downvoted = []
        communities_banned_from_list = []

    resp = make_response(render_template('index.html', posts=posts, active_communities=active_communities,
                           new_communities=new_communities, upcoming_events=upcoming_events,
                           show_post_community=True, low_bandwidth=low_bandwidth, recently_upvoted=recently_upvoted,
                           recently_downvoted=recently_downvoted,
                           communities_banned_from_list=communities_banned_from_list,
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           etag=f"{sort}_{view_filter}_{hash(str(g.site.last_active))}", next_url=next_url,
                           prev_url=prev_url, instance_stickies=instance_stickies,
                           title=f"{g.site.name} - {g.site.description}",
                           description=shorten_string(html_to_text(g.site.sidebar), 150),
                           content_filters=content_filters, sort=sort, view_filter=view_filter,
                           announcement=get_setting('announcement_html', get_setting('announcement')),
                           reported_posts=reported_posts(current_user.get_id(), g.admin_ids),
                           user_notes=user_notes(current_user.get_id()),
                           joined_communities=joined_or_modding_communities(current_user.get_id()),
                           moderated_community_ids=moderating_communities_ids(current_user.get_id()),
                           inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                           enable_mod_filter=enable_mod_filter,
                           has_topics=num_topics() > 0,
                           user_pronouns=user_pronouns()
                           ))
    resp.headers.set('Vary', 'Accept, Cookie, Accept-Language')
    if current_user.is_anonymous:
        resp.headers.set('Cache-Control', 'public, max-age=60')
    else:
        resp.headers.set('Cache-Control', 'private, max-age=15, must-revalidate')

    return resp


@bp.route('/topics', methods=['GET'])
@login_required_if_private_instance
def list_topics():
    verification_warning()
    topics = topic_tree()

    return render_template('list_topics.html', topics=topics, title=_('Browse by topic'),
                           low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1')


def _base_list_communities_context():
    create_admin_only = g.site.community_creation_admin_only
    default_user_add_remote = get_setting("allow_default_user_add_remote_community", True) 

    is_admin = current_user.is_authenticated and current_user.is_admin()
    is_staff = current_user.is_authenticated and current_user.is_staff()
    return {
        "SUBSCRIPTION_PENDING": SUBSCRIPTION_PENDING,
        "SUBSCRIPTION_MEMBER": SUBSCRIPTION_MEMBER,
        "SUBSCRIPTION_OWNER": SUBSCRIPTION_OWNER,
        "SUBSCRIPTION_MODERATOR": SUBSCRIPTION_MODERATOR,
        "current_user": current_user,
        "is_admin": is_admin,
        "is_staff": is_staff,
        "create_admin_only": create_admin_only,
        "default_user_add_remote": default_user_add_remote,
        "joined_communities": joined_or_modding_communities(current_user.get_id()),
        "pending_communities": pending_communities(current_user.get_id())
    } 


@bp.route('/communities', methods=['GET'])
@login_required_if_private_instance
def list_communities():
    verification_warning()
    search_param = request.args.get('search', '')
    home_select = request.args.get('home_select', 'any')
    subscribe_select = request.args.get('subscribe_select', 'any')
    topic_id = int(request.args.get('topic_id', 0))
    feed_id = int(request.args.get('feed_id', 0))
    language_id = int(request.args.get('language_id', 0))
    nsfw = request.args.get('nsfw', 'all')
    page = request.args.get('page', 1, type=int)
    instance = request.args.get('instance', '')
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    sort_by = request.args.get('sort_by', 'post_reply_count desc')

    if not g.site.enable_nsfw:
        nsfw = 'no'
        hide_nsfw = True
    else:
        hide_nsfw = False

    if request.args.get('prompt'):
        flash(_('You did not choose any topics. Would you like to choose individual communities instead?'))

    topics = topics_for_form(0)
    languages = Language.query.order_by(Language.name).all()
    communities = Community.query.filter_by(banned=False)

    # filter private communities: show only to members
    if current_user.is_authenticated:
        # for authenticated users, show non-private communities OR private communities where they are members
        member_check = db.session.query(CommunityMember.community_id).filter(
            CommunityMember.user_id == current_user.id,
            CommunityMember.is_banned == False
        ).subquery()
        communities = communities.filter(
            or_(
                Community.private == False,
                Community.id.in_(member_check)
            )
        )
    else:
        # For anonymous users, only show non-private communities
        communities = communities.filter_by(private=False)

    if search_param == '':
        pass
    else:
        communities = communities.filter(or_(Community.title.ilike(f"%{search_param}%"), Community.ap_id.ilike(f"%{search_param}%")))

    if topic_id > 0:
        communities = communities.filter_by(topic_id=topic_id)
    elif topic_id < 0:
        communities = communities.filter_by(topic_id=None)

    if language_id != 0:
        communities = communities.join(community_language).filter(community_language.c.language_id == language_id)
    
    if home_select == "local":
        communities = communities.filter(Community.ap_id == None)
    elif home_select == "remote":
        communities = communities.filter(Community.ap_id != None)
    
    if subscribe_select != "any":
        # get the user's joined communities
        user_joined_communities = joined_communities(current_user.get_id())
        user_moderating_communities = moderating_communities(current_user.get_id())
        # get the joined community ids list
        joined_ids = []
        for jc in user_joined_communities:
            joined_ids.append(jc.id)
        for mc in user_moderating_communities:
            joined_ids.append(mc.id)
        
        if subscribe_select == "subscribed":
            # filter down to just the joined communities
            communities = communities.filter(Community.id.in_(joined_ids))
        elif subscribe_select == "not_subscribed":
            # filter out the joined communities from all communities
            communities = communities.filter(Community.id.not_in(joined_ids))

    # default to no public feeds
    server_has_feeds = False
    # find all the feeds marked as public
    public_feeds = Feed.query.filter_by(public=True).order_by(Feed.title).all()
    if len(public_feeds) > 0:
        server_has_feeds = True

    create_admin_only = g.site.community_creation_admin_only

    is_admin = current_user.is_authenticated and current_user.is_admin()

    # if filtering by public feed 
    # get all the ids of the communities
    # then filter the communites to ones whose ids match the feed
    if feed_id != 0:
        feed_community_ids = []
        feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == feed_id).all()
        for item in feed_items:
            feed_community_ids.append(item.community_id)
        communities = communities.filter(Community.id.in_(feed_community_ids))
    
    # if filtering by home instance
    if instance:
        communities = communities.filter(Community.ap_domain == instance)
    
    hide_nsfw = False

    if current_user.is_authenticated:
        if current_user.hide_low_quality:
            communities = communities.filter(Community.low_quality == False)
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))
        if current_user.hide_nsfw == 1:
            nsfw = 'no'
            hide_nsfw = True
            communities = communities.filter(Community.nsfw == False)
        else:
            if nsfw == 'no':
                communities = communities.filter(Community.nsfw == False)
            elif nsfw == 'yes':
                communities = communities.filter(Community.nsfw == True)
        if current_user.hide_nsfl == 1:
            communities = communities.filter(Community.nsfl == False)
        instance_ids = blocked_or_banned_instances(current_user.id)
        if instance_ids:
            communities = communities.filter(or_(Community.instance_id.not_in(instance_ids), Community.instance_id == None))
        filtered_out_community_ids = filtered_out_communities(current_user)
        if len(filtered_out_community_ids):
            communities = communities.filter(Community.id.not_in(filtered_out_community_ids))

    else:
        communities = communities.filter(Community.nsfl == False)
        if nsfw == 'no':
            communities = communities.filter(and_(Community.nsfw == False))
        elif nsfw == 'yes':
            communities = communities.filter(and_(Community.nsfw == True))

    communities = communities.order_by(safe_order_by(sort_by, Community, {'title', 'subscriptions_count', 'post_count',
                                                                          'post_reply_count', 'last_active', 'created_at',
                                                                          'active_weekly', 'average_rating'}))

    # dict used for pagination query parameters
    args_dict = dict()
    args_dict["search"] = search_param
    args_dict["home_select"] = home_select
    args_dict["subscribe_select"] = subscribe_select
    args_dict["topic_id"] = topic_id
    args_dict["feed_id"] = feed_id
    args_dict["language_id"] = language_id
    args_dict["nsfw"] = nsfw
    args_dict["instance"] = instance

    # Pagination
    communities = communities.paginate(page=page,
                                       per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                                       error_out=False)
    context = _base_list_communities_context()
    context["next_url"] = url_for('main.list_communities', page=communities.next_num, sort_by=sort_by,
                       **args_dict) if communities.has_next else None
    context["prev_url"] = url_for('main.list_communities', page=communities.prev_num, sort_by=sort_by, 
                       **args_dict) if communities.has_prev and page != 1 else None

    context.update({
        "communities": communities,
        "search": search_param,
        "title": _('Communities'), 
        "intance": instance,
        "home_select": home_select,
        "topics": topics,
        "languages": languages,
        "topic_id": topic_id,
        "language_id": language_id,
        "sort_by": sort_by,
        "nsfw": nsfw,
        "subscribe_select": subscribe_select,
        "low_bandwidth": low_bandwidth,
        "feed_id": feed_id,
        "server_has_feeds": server_has_feeds,
        "public_feeds": public_feeds,
        "hide_nsfw": hide_nsfw,
        "create_admin_only": create_admin_only,
        "is_admin": is_admin,
    })

    return render_template('list_communities.html', **context)


@bp.route('/modlog', methods=['GET'])
def modlog():
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    mod_action = request.args.get('mod_action', '')
    suspect_user_name = request.args.get('suspect_user_name', '')
    community_id = request.args.get('communities', '0')
    community_id = int(community_id) if community_id != '' else 0
    user_name = request.args.get('user_name', '')
    can_see_names = False
    is_admin = False

    arg_dict = {"low_bandwidth": low_bandwidth,
                "mod_action": mod_action,
                "suspect_user_name": suspect_user_name,
                "communities": community_id,
                "user_name": user_name}

    # Admins can see all of the modlog, everyone else can only see public entries
    modlog_entries = ModLog.query
    if mod_action:
        modlog_entries = modlog_entries.filter(ModLog.action == mod_action)
    if suspect_user_name:
        if f"@{current_app.config['SERVER_NAME']}" in suspect_user_name:
            suspect_user_name = suspect_user_name.split('@')[0]
        user = User.query.filter_by(user_name=suspect_user_name, ap_id=None).first()
        if user is None:
            user = User.query.filter_by(ap_id=suspect_user_name).first()
        if user:
            modlog_entries = modlog_entries.filter(ModLog.target_user_id == user.id)
    if user_name:
        if f"@{current_app.config['SERVER_NAME']}" in user_name:
            user_name = user_name.split('@')[0]
        user = User.query.filter_by(user_name=user_name, ap_id=None).first()
        if user is None:
            user = User.query.filter_by(ap_id=user_name).first()
        if user:
            modlog_entries = modlog_entries.filter(ModLog.user_id == user.id)
    if community_id:
        modlog_entries = modlog_entries.filter(ModLog.community_id == community_id)

    if current_user.is_authenticated:
        if current_user.is_admin() or current_user.is_staff():
            is_admin = True
            modlog_entries = modlog_entries.order_by(desc(ModLog.created_at))
            can_see_names = True
        else:
            modlog_entries = modlog_entries.filter(ModLog.public == True).order_by(desc(ModLog.created_at))
    else:
        modlog_entries = modlog_entries.filter(ModLog.public == True).order_by(desc(ModLog.created_at))

    # Pagination
    modlog_entries = modlog_entries.paginate(page=page, per_page=100 if not low_bandwidth else 50, error_out=False)
    next_url = url_for('main.modlog', page=modlog_entries.next_num, **arg_dict) if modlog_entries.has_next else None
    prev_url = url_for('main.modlog', page=modlog_entries.prev_num, **arg_dict) if modlog_entries.has_prev and page != 1 else None

    instances = {instance.id: instance.domain for instance in Instance.query.all()}
    communities = {community.id: community.display_name() for community in Community.query.filter(Community.banned == False).all()}

    return render_template('modlog.html',
                           title=_('Moderation Log'), modlog_entries=modlog_entries, can_see_names=can_see_names,
                           next_url=next_url, prev_url=prev_url, low_bandwidth=low_bandwidth,
                           instances=instances, is_admin=is_admin, communities=communities,
                           mod_action=mod_action, suspect_user_name=suspect_user_name, community_id=community_id,
                           user_name=user_name,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                           )


@bp.route("/modlog/search_suggestions", methods=['POST'])
def modlog_search_suggestions():
    q = request.form.get("suspect_user_name", "").lower()
    if q == '':
        q = request.form.get("user_name", "").lower()
    results = User.query.filter(or_(User.ap_id.ilike(f"%{q}%"),
                                    User.user_name.ilike(f"%{q}%"),
                                    User.ap_profile_id.ilike(f"%{q}%"))
                                ).limit(5).all()
    html = "".join(f"<option value='{m.ap_id or m.user_name}'>" for m in results)
    return html


@bp.route('/about')
def about_page():
    user_amount = users_total()
    MAU = active_month()
    posts_amount = local_posts()

    admins = Site.admins()
    staff = Site.staff()
    domains_amount = db.session.execute(text('SELECT COUNT(*) as c FROM "domain" WHERE "banned" IS false')).scalar()
    community_amount = local_communities()
    instance = Instance.query.filter_by(id=1).first()

    cms_page = CmsPage.query.filter(CmsPage.url == '/about').first()

    return render_template('about.html', user_amount=user_amount, mau=MAU, posts_amount=posts_amount,
                           domains_amount=domains_amount, community_amount=community_amount, instance=instance,
                           admins=admins, staff=staff, cms_page=cms_page)


@bp.route('/privacy')
def privacy():
    cms_page = CmsPage.query.filter(CmsPage.url == '/privacy').first()
    if cms_page:
        return render_template('cms_page.html', page=cms_page)
    return render_template('privacy.html')


@bp.route('/login')
def login():
    return redirect(url_for('auth.login'))


@bp.route('/robots.txt')
def robots():
    resp = make_response(render_template('robots.txt', use_rsl=not current_app.config['ALLOW_AI_CRAWLERS']))
    resp.mimetype = 'text/plain'
    return resp


@bp.route('/.well-known/security.txt')
def security():
    resp = make_response(render_template('security.txt'))
    resp.mimetype = 'text/plain'
    return resp


@bp.route('/sitemap.xml')
@cache.cached(timeout=6000)
def sitemap():
    posts = Post.query.filter(Post.from_bot == False, Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                              Post.instance_id == 1, Post.indexable == True)
    posts = posts.order_by(desc(Post.posted_at)).limit(500)

    resp = make_response(render_template('sitemap.xml', posts=posts, current_app=current_app))
    resp.mimetype = 'text/xml'
    return resp


@bp.route('/rsl.xml')
def rsl():
    if current_app.config['ALLOW_AI_CRAWLERS']:
        abort(404)
    resp = make_response(render_template('rsl.xml'))
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


@bp.route('/honey')
@bp.route('/honey/<whatever>')
def honey_pot(whatever=None):
    if current_user.is_authenticated:
        return ''
    else:
        do_not_track = ['image', 'audio', 'video']
        if request.headers.get('Sec-Fetch-Dest', '') in do_not_track or request.headers.get('Accept', '').startswith('image/'):
            return ''
    from app import redis_client
    from time import time
    ip = ip_address()
    key = f"honeypot:{ip}"

    now = time()
    score = now
    member = str(now)  # unique enough for repeated entries

    added = redis_client.zadd(key, {member: score})

    if added == 1 and redis_client.ttl(key) == -1:
        redis_client.expire(key, 86400)  # auto-expire key after 24h of inactivity

    # Remove entries older than 24 hours
    redis_client.zremrangebyscore(key, 0, now - 86400)

    # Count recent events
    count = redis_client.zcount(key, now - 86400, now)

    if count >= 3:
        redis_client.set(f"ban:{ip}", 1, ex=86400 * 7)

    if whatever:
        try:
            return show_post(int(whatever), 'hot', False, False)
        except Exception:
            pass
    return ''


@bp.route('/test')
@debug_mode_only
def test():
    #refresh_instance_chooser()
    #p = Post.query.get(42)
    #p.delete_dependencies()
    #db.session.delete(p)
    #db.session.commit()
    return markdown_to_html('Testing!\n\n![an image :: width=50](https://piefed.social/static/media/logo_8p7en.svg, https://media.piefed.social/posts/up/TR/upTRjfvFt2ma0hz.webp)\n\nthere we go')

    from flask import json
    community = Community.query.get(33)
    announce_activity = {
        'actor': community.ap_profile_id,
        'id': f'xyz{gibberish()}',
        'object': {
            'actor': 'https://piefed.rimu.geek.nz/u/rimu',
            'id': f'xyz2{gibberish()}',
            'object': 'https://piefed.ngrok.app/u/rimu',
            'type': 'Like',
        },
        'type': 'Announce',
    }

    send_async = []
    send_async.append(HttpSignature.signed_request('https://piefed.ngrok.app/inbox', announce_activity,
                                                   community.private_key,
                                                   community.profile_id() + '#main-key',
                                                   send_via_async=True))

    from app import redis_client
    # send announce_activity via redis pub/sub to piefed_notifs service
    redis_client.publish("http_posts:activity", json.dumps({'urls': [url[0] for url in send_async],
                                                            'headers': [url[1] for url in send_async],
                                                            'data': send_async[0][2].decode('utf-8')}))

    return 'Done'
    import json
    user_id = 1
    r = get_redis_connection()
    r.publish(f"notifications:{user_id}", json.dumps({'num_notifs': randint(1, 100)}))
    current_user.unread_notifications = randint(1, 100)
    db.session.commit()
    return 'Done'

    user = User.query.get(1)
    send_registration_approved_email(user)

    markdown = """What light novels have you read in the past week? Something good? Bad? Let us know about it. 

And if you want to add your score to the database to help your fellow Bookworms find new reading materials you can use the following template:

><Book Title and Volume> Review Goes Here [5/10]"""

    return markdown_to_html(markdown)

    return ip_address()

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

    # for community in Community.query.filter(Community.content_retention != -1):
    #    for post in community.posts.filter(Post.posted_at < utcnow() - timedelta(days=Community.content_retention)):
    #        post.delete_dependencies()

    return 'done'

    md = "::: spoiler I'm all for ya having fun and your right to hurt yourself.\n\nI am a former racer, commuter, and professional Buyer for a chain of bike shops. I'm also disabled from the crash involving the 6th and 7th cars that have hit me in the last 170k+ miles of riding. I only barely survived what I simplify as a \"broken neck and back.\" Cars making U-turns are what will get you if you ride long enough, \n\nespecially commuting. It will look like just another person turning in front of you, you'll compensate like usual, and before your brain can even register what is really happening, what was your normal escape route will close and you're going to crash really hard. It is the only kind of crash that your intuition is useless against.\n:::"

    return markdown_to_html(md)

    users_to_notify = User.query.join(Notification, User.id == Notification.user_id).filter(
        User.ap_id == None,
        Notification.created_at > User.last_seen,
        Notification.read == False,
        User.email_unread_sent == False,  # they have not been emailed since last activity
        User.email_unread == True  # they want to be emailed
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
                       text_body=flask.render_template('email/unread_notifications.txt', user=user,
                                                       notifications=notifications),
                       html_body=flask.render_template('email/unread_notifications.html', user=user,
                                                       notifications=notifications,
                                                       posts=posts,
                                                       domain=current_app.config['SERVER_NAME']))
            user.email_unread_sent = True
            db.session.commit()

    return 'ok'


@bp.route('/communities_menu')
def communities_menu():
    return render_template('communities_menu.html',
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           is_admin=current_user.is_authenticated and current_user.is_admin(),
                           is_staff=current_user.is_authenticated and current_user.is_staff(),
                           default_user_add_remote=get_setting("allow_default_user_add_remote_community", True)
                           )


@bp.route('/explore_menu')
def explore_menu():
    return render_template('explore_menu.html', menu_topics=menu_topics(),
                           menu_instance_feeds=menu_instance_feeds(),
                           menu_my_feeds=menu_my_feeds(current_user.id) if current_user.is_authenticated else None,
                           menu_subscribed_feeds=menu_subscribed_feeds(current_user.id) if current_user.is_authenticated else None,
                           )


@bp.route('/topics_menu')
def topics_menu():
    return render_template('topics_menu.html', menu_topics=menu_topics(),
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()))


@bp.route('/feeds_menu')
def feeds_menu():
    return render_template('feeds_menu.html',
                           menu_instance_feeds=menu_instance_feeds(),
                           menu_my_feeds=menu_my_feeds(current_user.id) if current_user.is_authenticated else None,
                           menu_subscribed_feeds=menu_subscribed_feeds(current_user.id) if current_user.is_authenticated else None,
                           )


@bp.route('/share', methods=['GET', 'POST'])
def share():
    url = request.args.get('url')
    url = remove_tracking_from_link(url.strip())
    form = ShareLinkForm()
    form.which_community.choices = possible_communities()
    if form.validate_on_submit():
        community = Community.query.get_or_404(form.which_community.data)
        response = make_response(redirect(url_for('community.add_post', actor=community.link(), type='link', link=url,
                                                  title=request.args.get('title'))))
        response.set_cookie('cross_post_community_id', str(community.id), max_age=timedelta(days=28))
        response.delete_cookie('post_title')
        response.delete_cookie('post_description')
        response.delete_cookie('post_tags')
        return response

    if request.cookies.get('cross_post_community_id'):
        form.which_community.data = int(request.cookies.get('cross_post_community_id'))

    communities = Community.query.filter_by(banned=False).join(Post).filter(Post.url == url, Post.deleted == False,
                                                                            Post.status > POST_STATUS_REVIEWING).all()

    return render_template('share.html', form=form, title=request.args.get('title'), communities=communities)


@bp.route('/protocol_handler')
@login_required
def protocol_handler():
    """ handles the web+fedi:// protocol and redirects the viewer to the right place """
    q = request.args.get('to')
    if q:
        try:
            resp = get_resolve_object(None, {'q': q.replace('web+ap://', 'https://')}, user_id=current_user.id)
        except Exception:
            flash(_('Failed to look up %(url)s'))
            return redirect(url_for('main.index'))

        if 'post' in resp:
            post = Post.query.get(resp['post']['post']['id'])
            return redirect(post.slug if post.slug else url_for('activitypub.post_ap', post_id=post.id))
        if 'comment' in resp:
            return redirect(url_for('activitypub.comment_ap', comment_id=resp['comment']['comment']['id']))
        if 'community' in resp:
            return redirect(url_for('activitypub.community_profile', actor=resp['community']['community']['id']))
        if 'person' in resp:
            return redirect(url_for('activitypub.user_profile', actor=resp['person']['person']['id']))

    else:
        return render_template('protocol_handler.html', title=_('Protocol handler'))


@bp.route('/test_email')
@debug_mode_only
def test_email():
    if current_user.is_anonymous:
        email = request.args.get('email')
    else:
        email = current_user.email
    send_email(subject='This is a test email',
               sender=f'{g.site.name} <{current_app.config["MAIL_FROM"]}>',
               recipients=[email],
               text_body='This is a test email. If you received this, email sending is working!',
               html_body='<p>This is a test email. If you received this, email sending is working!</p>',
               reply_to=g.site.contact_email)
    return f'Email sent to {email}.'


@bp.route('/test_redis')
@debug_mode_only
def test_redis():
    from app import redis_client
    if redis_client and redis_client.memory_stats():
        return 'Redis connection is ok'
    else:
        return 'Redis error'


@bp.route('/test_ip')
@debug_mode_only
def test_ip():
    return ip_address() + ' ' + request.headers.get('CF-Connecting-IP', 'CF-Connecting-IP is empty')


@bp.route('/test_s3')
@debug_mode_only
def test_s3():
    import boto3
    boto3_session = boto3.session.Session()
    s3 = boto3_session.client(
        service_name='s3',
        region_name=current_app.config['S3_REGION'],
        endpoint_url=current_app.config['S3_ENDPOINT'],
        aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
        aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
    )
    s3.upload_file('babel.cfg', current_app.config['S3_BUCKET'], 'babel.cfg')
    s3.delete_object(Bucket=current_app.config['S3_BUCKET'], Key='babel.cfg')
    return 'Ok'


@bp.route('/test_hashing')
@debug_mode_only
def test_hashing():
    hash = retrieve_image_hash(f'{current_app.config["SERVER_URL"]}/static/images/apple-touch-icon.png')
    if hash:
        return 'Ok'
    else:
        return 'Error'


@bp.route('/test_ldap')
@debug_mode_only
def test_ldap():
    try:
        # Test LDAP connection
        connection_result = test_ldap_connection()
        if not connection_result:
            return 'LDAP test failed: Could not connect to LDAP server. Check configuration.'

        # Test user sync with dummy data using random password
        from random import randint
        random_password = f'testpass{randint(1000, 9999)}'
        sync_result = sync_user_to_ldap('testuser', 'test@example.com', random_password)

        return f'LDAP test successful. Connection: {connection_result}, Sync: {sync_result}'
    except Exception as e:
        return f'LDAP test failed: {str(e)}'


@bp.route('/test_ldap_login')
@debug_mode_only
def test_ldap_login():
    try:
        # Test user login with given user name and password
        login_result = login_with_ldap(request.args.get('user_name'), request.args.get('password'))

        return f'LDAP test results: {str(login_result is not False)}'
    except Exception as e:
        return f'LDAP test failed: {str(e)}'


@bp.route('/test_libretranslate')
@debug_mode_only
def test_libretranslate():
    lt = LibreTranslateAPI(current_app.config['TRANSLATE_ENDPOINT'], api_key=current_app.config['TRANSLATE_KEY'])
    return lt.translate('<p>Si vous lisez cela en anglais, alors la traduction a fonctionné!</p>', source='auto', target='en')


@bp.route('/find_voters')
@login_required
@permission_required('change instance settings')
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
        'id': f"{current_app.config['SERVER_URL']}/",
        'name': 'PieFed',
        'summary': g.site.name + ' - ' + g.site.description,
        'published': ap_datetime(g.site.created_at),
        'updated': ap_datetime(g.site.updated),
        'inbox': f"{current_app.config['SERVER_URL']}/inbox",
        'outbox': f"{current_app.config['SERVER_URL']}/site_outbox",
        'icon': {
            'type': 'Image',
            'url': f"{current_app.config['SERVER_URL']}/static/images/piefed_logo_icon_t_75.png"
        },
        'publicKey': {
            'id': f"{current_app.config['SERVER_URL']}/#main-key",
            'owner': f"{current_app.config['SERVER_URL']}/",
            'publicKeyPem': g.site.public_key
        }
    }
    resp = jsonify(application_data)
    resp.content_type = 'application/activity+json'
    resp.headers.set('Cache-Control', 'public, max-age=30')
    resp.headers.set('Vary', 'Accept')
    return resp


# instance actor (literally uses the word 'actor' without the /u/)
# required for interacting with instances using 'secure mode' (aka authorized fetch)
@bp.route('/actor', methods=['GET'])
def instance_actor():
    application_data = {
        '@context': default_context(),
        'type': 'Application',
        'id': f"{current_app.config['SERVER_URL']}/actor",
        'preferredUsername': f"{current_app.config['SERVER_NAME']}",
        'url': f"{current_app.config['SERVER_URL']}/about",
        'manuallyApprovesFollowers': True,
        'inbox': f"{current_app.config['SERVER_URL']}/actor/inbox",
        'outbox': f"{current_app.config['SERVER_URL']}/actor/outbox",
        'publicKey': {
            'id': f"{current_app.config['SERVER_URL']}/actor#main-key",
            'owner': f"{current_app.config['SERVER_URL']}/actor",
            'publicKeyPem': g.site.public_key
        },
        'endpoints': {
            'sharedInbox': f"{current_app.config['SERVER_URL']}/inbox",
        }
    }
    resp = jsonify(application_data)
    resp.content_type = 'application/activity+json'
    return resp


@bp.route('/service_worker.js', methods=['GET'])
def service_worker():
    js_path = os.path.join('static', 'service_worker.js')
    response = make_response(send_file(js_path, mimetype='text/javascript'))
    response.headers['Cache-Control'] = 'public, max-age=86400'  # cache for 1 day
    return response


# intercept requests for the PWA manifest.json and provide platform specific ones
@bp.route('/manifest.json', methods=['GET'])
@bp.route('/static/manifest.json', methods=['GET'])
def static_manifest():
    def get_manifest_for_os(os_family):
        base_dir = 'app/static/pwa_manifests'
        if os_family == 'mac os x':
            path = os.path.join(base_dir, 'ios', 'manifest.json')
        else:
            path = os.path.join(base_dir, os_family, 'manifest.json')
        return path if os.path.exists(path) else os.path.join(base_dir, 'default', 'manifest.json')

    try:
        res = uaparse(request.user_agent.string)
        manifest_path = get_manifest_for_os(res.os.family.lower())
    except Exception:
        manifest_path = os.path.join('app/static/pwa_manifests/default/manifest.json')

    with open(manifest_path, 'r') as f:
        manifest = json.load(f)

    # Modify manifest
    manifest['id'] = f'{current_app.config["SERVER_URL"]}'
    manifest['name'] = g.site.name if g.site.name else 'PieFed'
    manifest['description'] = g.site.description if g.site.description else ''
    
    # Update icons to use custom logos with fallbacks
    logo_512 = get_setting('logo_512', '')
    logo_192 = get_setting('logo_192', '')
    
    # Update the icons array
    for icon in manifest.get('icons', []):
        if icon.get('sizes') == '192x192':
            icon['src'] = logo_192 if logo_192 else '/static/images/piefed_logo_icon_t_192.png'
        elif icon.get('sizes') == '512x512':
            icon['src'] = logo_512 if logo_512 else '/static/images/piefed_logo_icon_t_512.png'

    # Build response with cache headers
    response = make_response(jsonify(manifest))
    # Cache for 1 hour on the client, prevent public/shared caching because we detect the user agent
    response.headers['Cache-Control'] = 'private, max-age=3600'

    return response


@bp.route('/feeds', methods=['GET', 'POST'])
@login_required_if_private_instance
def list_feeds():
    # default to no public feeds
    server_has_feeds = False
    search_param = request.args.get('search', '')

    if search_param == '':
        # find all the feeds marked as public
        public_feeds = feed_tree_public()
        
    else:
        # find all the feeds marked as public that match the search param
        public_feeds = feed_tree_public(search_param)

    if len(public_feeds) > 0:
        server_has_feeds = True

    # respond with json collection of public feeds for curl/AP requests
    if is_activitypub_request():
        site_data = lemmy_site_data()
        feeds_list = []
        for f in public_feeds:
            if f['feed'].is_local():
                feeds_list.append(f"{current_app.config['SERVER_URL']}/f/{f['feed'].machine_name}")
        site_data['site_view']['public_feeds'] = feeds_list
        site_data['site_view']['counts']['public_feeds'] = len(feeds_list)
        resp = jsonify(site_data)
        resp.content_type = 'application/activity+json'
        return resp
    else:
        # render the page
        return render_template('feed/public_feeds.html', server_has_feeds=server_has_feeds,
                               public_feeds_list=public_feeds,
                               subscribed_feeds=subscribed_feeds(current_user.get_id()),
                               search_hint=search_param)


@bp.route('/explore')
@login_required_if_private_instance
def explore():
    topics = topic_tree()
    return render_template('explore.html', topics=topics, menu_instance_feeds=menu_instance_feeds(),
                           menu_my_feeds=menu_my_feeds(current_user.id) if current_user.is_authenticated else None,
                           menu_subscribed_feeds=menu_subscribed_feeds(current_user.id) if current_user.is_authenticated else None,)


@bp.route('/content_warning', methods=['GET', 'POST'])
def content_warning():
    form = ContentWarningForm()
    if form.validate_on_submit():
        if form.next.data.startswith("/"):
            resp = make_response(redirect(form.next.data))
            resp.set_cookie('warned', 'yes', expires=datetime(year=2099, month=12, day=30))
            return resp
    form.next.data = referrer()
    message = """
    This website contains age-restricted materials including nudity and explicit depictions of sexual activity.

    By entering, you affirm that you are at least 18 years of age or the age of majority in the jurisdiction you are accessing the website from and you consent to viewing sexually explicit content.
    """
    return render_template('generic_form.html', title=_('Content warning'), message=message, form=form)


@bp.route('/my-year-in-review/<year>')
@login_required
def my_year_in_review(year):
    return render_template('generic_message.html', title=_('This page is intentionally left blank.'), message=_("We don't track you, so there's not much data to make graphs of."))


@bp.route('/health', methods=['HEAD', 'GET'])
def health():
    return 'Ok'


@bp.route('/health2', methods=['GET', 'HEAD'])
def health2():
    # Do some DB access to provide a picture of the performance of the instance
    # This is all busy-work to give an indication to the caller of the instance performance so there is a lot of # noqa comments to silence ruff.

    search_param = request.args.get('search', '')
    topic_id = int(request.args.get('topic_id', 0))
    feed_id = int(request.args.get('feed_id', 0))
    language_id = int(request.args.get('language_id', 0))
    nsfw = request.args.get('nsfw', None)
    sort_by = request.args.get('sort_by', 'post_reply_count desc')

    if not g.site.enable_nsfw:
        nsfw = None
    else:
        if nsfw is None:
            nsfw = 'all'

    if request.args.get('prompt'):
        flash(_('You did not choose any topics. Would you like to choose individual communities instead?'))

    topics = Topic.query.order_by(Topic.name).all()              # noqa f841
    languages = Language.query.order_by(Language.name).all()     # noqa f841
    communities = Community.query.filter_by(banned=False)
    if search_param == '':
        pass
    else:
        communities = communities.filter(
            or_(Community.title.ilike(f"%{search_param}%"), Community.ap_id.ilike(f"%{search_param}%")))

    if topic_id != 0:
        communities = communities.filter_by(topic_id=topic_id)

    if language_id != 0:
        communities = communities.join(community_language).filter(community_language.c.language_id == language_id)

    # find all the feeds marked as public
    public_feeds = Feed.query.filter_by(public=True).order_by(Feed.title).all() # noqa f841

    # if filtering by public feed
    # get all the ids of the communities
    # then filter the communites to ones whose ids match the feed
    if feed_id != 0:
        feed_community_ids = []
        feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == feed_id).all()
        for item in feed_items:
            feed_community_ids.append(item.community_id)
        communities = communities.filter(Community.id.in_(feed_community_ids))

    if current_user.is_authenticated:
        if current_user.hide_low_quality:
            communities = communities.filter(Community.low_quality == False)
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))
        if current_user.hide_nsfw == 1:
            nsfw = None
            communities = communities.filter(Community.nsfw == False)
        else:
            if nsfw == 'no':
                communities = communities.filter(Community.nsfw == False)
            elif nsfw == 'yes':
                communities = communities.filter(Community.nsfw == True)
        if current_user.hide_nsfl == 1:
            communities = communities.filter(Community.nsfl == False)
        instance_ids = blocked_or_banned_instances(current_user.id)
        if instance_ids:
            communities = communities.filter(
                or_(Community.instance_id.not_in(instance_ids), Community.instance_id == None))
        filtered_out_community_ids = filtered_out_communities(current_user)
        if len(filtered_out_community_ids):
            communities = communities.filter(Community.id.not_in(filtered_out_community_ids))

    else:
        communities = communities.filter(Community.nsfl == False)
        if nsfw == 'no':
            communities = communities.filter(and_(Community.nsfw == False))
        elif nsfw == 'yes':
            communities = communities.filter(and_(Community.nsfw == True))

    communities = communities.order_by(safe_order_by(sort_by, Community,
                                                     {'title', 'subscriptions_count', 'post_count', 'post_reply_count',
                                                      'last_active', 'created_at'})).limit(100)

    c = communities.all()   # noqa f841

    return ''
