import os.path
import json
from datetime import timedelta
from random import randint

import flask
from pyld import jsonld
from sqlalchemy.sql.operators import or_, and_
from ua_parser import parse as uaparse

from app import db, cache
from app.activitypub.util import users_total, active_month, local_posts, local_communities, \
    lemmy_site_data, is_activitypub_request
from app.activitypub.signature import default_context, LDSignature
from app.community.util import retrieve_mods_and_backfill
from app.constants import SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR, \
    POST_STATUS_REVIEWING, POST_TYPE_LINK
from app.email import send_email, send_registration_approved_email
from app.inoculation import inoculation
from app.main import bp
from flask import g, flash, request, current_app, url_for, redirect, make_response, jsonify, send_file
from flask_login import current_user
from flask_babel import _, get_locale
from sqlalchemy import desc, text

from app.main.forms import ShareLinkForm
from app.utils import render_template, get_setting, request_etag_matches, return_304, blocked_domains, \
    ap_datetime, shorten_string, user_filters_home, \
    joined_communities, moderating_communities, markdown_to_html, allowlist_html, \
    blocked_instances, communities_banned_from, topic_tree, recently_upvoted_posts, recently_downvoted_posts, \
    menu_topics, blocked_communities, \
    permission_required, debug_mode_only, ip_address, menu_instance_feeds, menu_my_feeds, menu_subscribed_feeds, \
    feed_tree_public, gibberish, get_deduped_post_ids, paginate_post_ids, post_ids_to_models, html_to_text, \
    get_redis_connection, subscribed_feeds, joined_or_modding_communities, login_required_if_private_instance, \
    pending_communities, retrieve_image_hash, possible_communities, remove_tracking_from_link, reported_posts, \
    moderating_communities_ids, user_notes, login_required, safe_order_by, filtered_out_communities
from app.models import Community, CommunityMember, Post, Site, User, utcnow, Topic, Instance, \
    Notification, Language, community_language, ModLog, Feed, FeedItem, CmsPage
from app.ldap_utils import test_ldap_connection, sync_user_to_ldap


@bp.route('/', methods=['HEAD', 'GET', 'POST'])
@bp.route('/home', methods=['GET', 'POST'])
@bp.route('/home/<sort>', methods=['GET', 'POST'])
@bp.route('/home/<sort>/<view_filter>', methods=['GET', 'POST'])
@login_required_if_private_instance
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
        view_filter = current_user.default_filter if current_user.is_authenticated else g.site.default_filter
        if view_filter is None:
            view_filter = 'subscribed'

    # If nothing has changed since their last visit, return HTTP 304
    current_etag = f"{sort}_{view_filter}_{hash(str(g.site.last_active))}"
    if current_user.is_anonymous and request_etag_matches(current_etag):
        return return_304(current_etag)

    page = request.args.get('page', 0, type=int)
    result_id = request.args.get('result_id', gibberish(15)) if current_user.is_authenticated else None
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
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
        community_ids = db.session.execute(
            text(f'SELECT id FROM community as c WHERE c.instance_id = 1 {low_quality_filter}')).scalars()
    elif view_filter == 'popular':
        if current_user.is_anonymous:
            community_ids = db.session.execute(
                text('SELECT id FROM community as c WHERE c.show_popular is true AND c.low_quality is false')).scalars()
        else:
            community_ids = db.session.execute(
                text(f'SELECT id FROM community as c WHERE c.show_popular is true {low_quality_filter}')).scalars()
    elif view_filter == 'all' or current_user.is_anonymous:
        community_ids = [-1]  # Special value to indicate 'All'
    elif view_filter == 'moderating':
        community_ids = modded_communities

    post_ids = get_deduped_post_ids(result_id, list(community_ids), sort)
    has_next_page = len(post_ids) > page + 1 * page_length
    post_ids = paginate_post_ids(post_ids, page, page_length=page_length)
    posts = post_ids_to_models(post_ids, sort)

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
    active_communities = Community.query.filter_by(banned=False).filter_by(nsfw=False).filter_by(nsfl=False)
    if current_user.is_authenticated:  # do not show communities current user is banned from
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            active_communities = active_communities.filter(Community.id.not_in(banned_from))
        community_ids = blocked_communities(current_user.id)
        if community_ids:
            active_communities = active_communities.filter(Community.id.not_in(community_ids))
    active_communities = active_communities.order_by(desc(Community.last_active)).limit(5).all()

    # New Communities
    cutoff = utcnow() - timedelta(days=30)
    new_communities = Community.query.filter_by(banned=False).filter_by(nsfw=False).filter_by(nsfl=False).filter(Community.created_at > cutoff)
    if current_user.is_authenticated:  # do not show communities current user is banned from
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            new_communities = new_communities.filter(Community.id.not_in(banned_from))
        community_ids = blocked_communities(current_user.id)
        if community_ids:
            new_communities = new_communities.filter(Community.id.not_in(community_ids))
    new_communities = new_communities.order_by(desc(Community.created_at)).limit(5).all()

    # Voting history and ban status
    if current_user.is_authenticated:
        recently_upvoted = recently_upvoted_posts(current_user.id)
        recently_downvoted = recently_downvoted_posts(current_user.id)
        communities_banned_from_list = communities_banned_from(current_user.id)
    else:
        recently_upvoted = []
        recently_downvoted = []
        communities_banned_from_list = []

    return render_template('index.html', posts=posts, active_communities=active_communities,
                           new_communities=new_communities,
                           show_post_community=True, low_bandwidth=low_bandwidth, recently_upvoted=recently_upvoted,
                           recently_downvoted=recently_downvoted,
                           communities_banned_from_list=communities_banned_from_list,
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           etag=f"{sort}_{view_filter}_{hash(str(g.site.last_active))}", next_url=next_url,
                           prev_url=prev_url,
                           title=f"{g.site.name} - {g.site.description}",
                           description=shorten_string(html_to_text(g.site.sidebar), 150),
                           content_filters=content_filters, sort=sort, view_filter=view_filter,
                           announcement=allowlist_html(get_setting('announcement', '')),
                           reported_posts=reported_posts(current_user.get_id(), g.admin_ids),
                           user_notes=user_notes(current_user.get_id()),
                           joined_communities=joined_or_modding_communities(current_user.get_id()),
                           inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                           enable_mod_filter=enable_mod_filter,
                           )


@bp.route('/topics', methods=['GET'])
@login_required_if_private_instance
def list_topics():
    verification_warning()
    topics = topic_tree()

    return render_template('list_topics.html', topics=topics, title=_('Browse by topic'),
                           low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1')


@bp.route('/communities', methods=['GET'])
@login_required_if_private_instance
def list_communities():
    verification_warning()
    search_param = request.args.get('search', '')
    topic_id = int(request.args.get('topic_id', 0))
    feed_id = int(request.args.get('feed_id', 0))
    language_id = int(request.args.get('language_id', 0))
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    sort_by = request.args.get('sort_by', 'post_reply_count desc')

    if request.args.get('prompt'):
        flash(_('You did not choose any topics. Would you like to choose individual communities instead?'))

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

    # default to no public feeds
    server_has_feeds = False
    # find all the feeds marked as public
    public_feeds = Feed.query.filter_by(public=True).order_by(Feed.title).all()
    if len(public_feeds) > 0:
        server_has_feeds = True

    try:
        site = g.site
    except:
        site = Site.query.get(1)
    create_admin_only = site.community_creation_admin_only

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

    if current_user.is_authenticated:
        if current_user.hide_low_quality:
            communities = communities.filter(Community.low_quality == False)
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
        filtered_out_community_ids = filtered_out_communities(current_user)
        if len(filtered_out_community_ids):
            communities = communities.filter(Community.id.not_in(filtered_out_community_ids))

    else:
        communities = communities.filter(and_(Community.nsfw == False, Community.nsfl == False))

    communities = communities.order_by(safe_order_by(sort_by, Community, {'title', 'subscriptions_count', 'post_count', 'post_reply_count', 'last_active'}))

    # Pagination
    communities = communities.paginate(page=page,
                                       per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                                       error_out=False)
    next_url = url_for('main.list_communities', page=communities.next_num, sort_by=sort_by,
                       language_id=language_id) if communities.has_next else None
    prev_url = url_for('main.list_communities', page=communities.prev_num, sort_by=sort_by,
                       language_id=language_id) if communities.has_prev and page != 1 else None

    return render_template('list_communities.html', communities=communities, search=search_param,
                           title=_('Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           next_url=next_url, prev_url=prev_url, current_user=current_user,
                           create_admin_only=create_admin_only, is_admin=is_admin,
                           topics=topics, languages=languages, topic_id=topic_id, language_id=language_id,
                           sort_by=sort_by,
                           joined_communities=joined_or_modding_communities(current_user.get_id()),
                           pending_communities=pending_communities(current_user.get_id()),
                           low_bandwidth=low_bandwidth,
                           feed_id=feed_id,
                           server_has_feeds=server_has_feeds, public_feeds=public_feeds,
                           )


@bp.route('/communities/local', methods=['GET'])
def list_local_communities():
    verification_warning()
    search_param = request.args.get('search', '')
    topic_id = int(request.args.get('topic_id', 0))
    feed_id = int(request.args.get('feed_id', 0))
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

    # default to no public feeds
    server_has_feeds = False
    # find all the feeds marked as public
    public_feeds = Feed.query.filter_by(public=True).order_by(Feed.title).all()
    if len(public_feeds) > 0:
        server_has_feeds = True

    try:
        site = g.site
    except:
        site = Site.query.get(1)
    create_admin_only = site.community_creation_admin_only

    is_admin = current_user.is_authenticated and current_user.is_admin()

    # if filtering by public feed
    # get all the ids of the communities
    # then filter the communities to ones whose ids match the feed
    if feed_id != 0:
        feed_community_ids = []
        feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == feed_id).all()
        for item in feed_items:
            feed_community_ids.append(item.community_id)
        communities = communities.filter(Community.id.in_(feed_community_ids))

    if current_user.is_authenticated:
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))
        if current_user.hide_nsfw == 1:
            communities = communities.filter(Community.nsfw == False)
        if current_user.hide_nsfl == 1:
            communities = communities.filter(Community.nsfl == False)
        filtered_out_community_ids = filtered_out_communities(current_user)
        if len(filtered_out_community_ids):
            communities = communities.filter(Community.id.not_in(filtered_out_community_ids))
    else:
        communities = communities.filter(and_(Community.nsfw == False, Community.nsfl == False))

    communities = communities.order_by(safe_order_by(sort_by, Community, {'title', 'subscriptions_count', 'post_count', 'post_reply_count', 'last_active'}))

    # Pagination
    communities = communities.paginate(page=page,
                                       per_page=250 if current_user.is_authenticated and not low_bandwidth else 50,
                                       error_out=False)
    next_url = url_for('main.list_local_communities', page=communities.next_num, sort_by=sort_by,
                       language_id=language_id) if communities.has_next else None
    prev_url = url_for('main.list_local_communities', page=communities.prev_num, sort_by=sort_by,
                       language_id=language_id) if communities.has_prev and page != 1 else None

    return render_template('list_communities.html', communities=communities, search=search_param,
                           title=_('Local Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           next_url=next_url, prev_url=prev_url, current_user=current_user,
                           create_admin_only=create_admin_only, is_admin=is_admin,
                           topics=topics, languages=languages, topic_id=topic_id, language_id=language_id,
                           sort_by=sort_by,
                           joined_communities=joined_or_modding_communities(current_user.get_id()),
                           pending_communities=pending_communities(current_user.get_id()),
                           low_bandwidth=low_bandwidth,

                           feed_id=feed_id, server_has_feeds=server_has_feeds, public_feeds=public_feeds,
                           )


@bp.route('/communities/subscribed', methods=['GET'])
@login_required
def list_subscribed_communities():
    verification_warning()
    search_param = request.args.get('search', '')
    topic_id = int(request.args.get('topic_id', 0))
    feed_id = int(request.args.get('feed_id', 0))
    language_id = int(request.args.get('language_id', 0))
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    sort_by = request.args.get('sort_by', 'post_reply_count desc')
    topics = Topic.query.order_by(Topic.name).all()
    languages = Language.query.order_by(Language.name).all()
    # get all the communities
    all_communities = Community.query.filter_by(banned=False)
    # get the user's joined communities
    user_joined_communities = joined_communities(current_user.id)
    user_moderating_communities = moderating_communities(current_user.id)
    # get the joined community ids list
    joined_ids = []
    for jc in user_joined_communities:
        joined_ids.append(jc.id)
    for mc in user_moderating_communities:
        joined_ids.append(mc.id)
    # filter down to just the joined communities
    communities = all_communities.filter(Community.id.in_(joined_ids))

    if search_param == '':
        pass
    else:
        communities = communities.filter(or_(Community.title.ilike(f"%{search_param}%"), Community.ap_id.ilike(f"%{search_param}%")))

    if topic_id != 0:
        communities = communities.filter_by(topic_id=topic_id)

    if language_id != 0:
        communities = communities.join(community_language).filter(community_language.c.language_id == language_id)

    # default to no public feeds
    server_has_feeds = False
    # find all the feeds marked as public
    public_feeds = Feed.query.filter_by(public=True).order_by(Feed.title).all()
    if len(public_feeds) > 0:
        server_has_feeds = True

    try:
        site = g.site
    except:
        site = Site.query.get(1)
    create_admin_only = site.community_creation_admin_only

    is_admin = current_user.is_authenticated and current_user.is_admin()

    # if filtering by public feed
    # get all the ids of the communities
    # then filter the communities to ones whose ids match the feed
    if feed_id != 0:
        feed_community_ids = []
        feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == feed_id).all()
        for item in feed_items:
            feed_community_ids.append(item.community_id)
        communities = communities.filter(Community.id.in_(feed_community_ids))

    banned_from = communities_banned_from(current_user.id)
    if banned_from:
        communities = communities.filter(Community.id.not_in(banned_from))

    communities = communities.order_by(safe_order_by(sort_by, Community, {'title', 'subscriptions_count', 'post_count', 'post_reply_count', 'last_active'}))

    # Pagination
    communities = communities.paginate(page=page,
                                       per_page=250 if current_user.is_authenticated and not low_bandwidth else 50,
                                       error_out=False)
    next_url = url_for('main.list_subscribed_communities', page=communities.next_num, sort_by=sort_by,
                       language_id=language_id) if communities.has_next else None
    prev_url = url_for('main.list_subscribed_communities', page=communities.prev_num, sort_by=sort_by,
                       language_id=language_id) if communities.has_prev and page != 1 else None

    return render_template('list_communities.html', communities=communities, search=search_param,
                           title=_('Joined Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           next_url=next_url, prev_url=prev_url, current_user=current_user,
                           create_admin_only=create_admin_only, is_admin=is_admin,
                           topics=topics, languages=languages, topic_id=topic_id, language_id=language_id,
                           sort_by=sort_by,
                           joined_communities=joined_or_modding_communities(current_user.get_id()),
                           pending_communities=pending_communities(current_user.get_id()),
                           low_bandwidth=low_bandwidth,
                           feed_id=feed_id,
                           server_has_feeds=server_has_feeds, public_feeds=public_feeds,
                           )


@bp.route('/communities/notsubscribed', methods=['GET'])
@login_required
def list_not_subscribed_communities():
    verification_warning()
    search_param = request.args.get('search', '')
    topic_id = int(request.args.get('topic_id', 0))
    feed_id = int(request.args.get('feed_id', 0))
    language_id = int(request.args.get('language_id', 0))
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    sort_by = request.args.get('sort_by', 'post_reply_count desc')
    topics = Topic.query.order_by(Topic.name).all()
    languages = Language.query.order_by(Language.name).all()
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

    # default to no public feeds
    server_has_feeds = False
    # find all the feeds marked as public
    public_feeds = Feed.query.filter_by(public=True).order_by(Feed.title).all()
    if len(public_feeds) > 0:
        server_has_feeds = True

    try:
        site = g.site
    except:
        site = Site.query.get(1)
    create_admin_only = site.community_creation_admin_only

    is_admin = current_user.is_authenticated and current_user.is_admin()

    # if filtering by public feed
    # get all the ids of the communities
    # then filter the communities to ones whose ids match the feed
    if feed_id != 0:
        feed_community_ids = []
        feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == feed_id).all()
        for item in feed_items:
            feed_community_ids.append(item.community_id)
        communities = communities.filter(Community.id.in_(feed_community_ids))

    banned_from = communities_banned_from(current_user.id)
    if banned_from:
        communities = communities.filter(Community.id.not_in(banned_from))
    if current_user.hide_nsfw == 1:
        communities = communities.filter(Community.nsfw == False)
    if current_user.hide_nsfl == 1:
        communities = communities.filter(Community.nsfl == False)

    communities = communities.order_by(safe_order_by(sort_by, Community, {'title', 'subscriptions_count', 'post_count', 'post_reply_count', 'last_active'}))

    # Pagination
    communities = communities.paginate(page=page,
                                       per_page=250 if current_user.is_authenticated and not low_bandwidth else 50,
                                       error_out=False)
    next_url = url_for('main.list_not_subscribed_communities', page=communities.next_num, sort_by=sort_by,
                       language_id=language_id) if communities.has_next else None
    prev_url = url_for('main.list_not_subscribed_communities', page=communities.prev_num, sort_by=sort_by,
                       language_id=language_id) if communities.has_prev and page != 1 else None

    return render_template('list_communities.html', communities=communities, search=search_param,
                           title=_('Not Joined Communities'),
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           next_url=next_url, prev_url=prev_url, current_user=current_user,
                           create_admin_only=create_admin_only, is_admin=is_admin,
                           topics=topics, languages=languages, topic_id=topic_id, language_id=language_id,
                           sort_by=sort_by,
                           joined_communities=joined_or_modding_communities(current_user.get_id()),
                           pending_communities=pending_communities(current_user.get_id()),
                           low_bandwidth=low_bandwidth,
                           feed_id=feed_id, server_has_feeds=server_has_feeds, public_feeds=public_feeds)


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

    instances = {}
    for instance in Instance.query.all():
        instances[instance.id] = instance.domain

    return render_template('modlog.html',
                           title=_('Moderation Log'), modlog_entries=modlog_entries, can_see_names=can_see_names,
                           next_url=next_url, prev_url=prev_url, low_bandwidth=low_bandwidth,
                           instances=instances,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                           )


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
    resp = make_response(render_template('robots.txt'))
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
@debug_mode_only
def test():
    retrieve_mods_and_backfill(32, 'piefed.rimu.geek.nz', 'test2')
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
    return render_template('communities_menu.html', menu_topics=menu_topics(),
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
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


@bp.route('/test_email')
@debug_mode_only
def test_email():
    if current_user.is_anonymous:
        email = request.args.get('email')
    else:
        email = current_user.email
    send_email('This is a test email', f'{g.site.name} <{current_app.config["MAIL_FROM"]}>', [email],
               'This is a test email. If you received this, email sending is working!',
               '<p>This is a test email. If you received this, email sending is working!</p>')
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
    hash = retrieve_image_hash(f'https://{current_app.config["SERVER_NAME"]}/static/images/apple-touch-icon.png')
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
        'id': f"https://{current_app.config['SERVER_NAME']}/",
        'name': 'PieFed',
        'summary': g.site.name + ' - ' + g.site.description,
        'published': ap_datetime(g.site.created_at),
        'updated': ap_datetime(g.site.updated),
        'inbox': f"https://{current_app.config['SERVER_NAME']}/inbox",
        'outbox': f"https://{current_app.config['SERVER_NAME']}/site_outbox",
        'icon': {
            'type': 'Image',
            'url': f"https://{current_app.config['SERVER_NAME']}/static/images/piefed_logo_icon_t_75.png"
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
            'sharedInbox': f"https://{current_app.config['SERVER_NAME']}/inbox",
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
    manifest['id'] = f'https://{current_app.config["SERVER_NAME"]}'
    manifest['name'] = g.site.name if g.site.name else 'PieFed'
    manifest['description'] = g.site.description if g.site.description else ''

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

    # find all the feeds marked as public
    public_feeds = feed_tree_public()

    if len(public_feeds) > 0:
        server_has_feeds = True

    # respond with json collection of public feeds for curl/AP requests
    if is_activitypub_request():
        site_data = lemmy_site_data()
        feeds_list = []
        for f in public_feeds:
            if f['feed'].is_local():
                feeds_list.append(f"https://{current_app.config['SERVER_NAME']}/f/{f['feed'].machine_name}")
        site_data['site_view']['public_feeds'] = feeds_list
        site_data['site_view']['counts']['public_feeds'] = len(feeds_list)
        resp = jsonify(site_data)
        resp.content_type = 'application/activity+json'
        return resp
    else:
        # render the page
        return render_template('feed/public_feeds.html', server_has_feeds=server_has_feeds,
                               public_feeds_list=public_feeds,
                               subscribed_feeds=subscribed_feeds(current_user.get_id()))


@bp.route('/health', methods=['HEAD', 'GET'])
def health():
    return 'Ok'
