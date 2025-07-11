from collections import namedtuple

from random import randint

import flask
from bs4 import BeautifulSoup

from flask import redirect, url_for, flash, request, make_response, session, Markup, current_app, abort, g, json
from flask_login import current_user
from flask_babel import _, force_locale, gettext
from slugify import slugify
from sqlalchemy import or_, asc, desc, text
from sqlalchemy.orm.exc import NoResultFound

from app import db, cache, celery, httpx_client, limiter, plugins
from app.activitypub.signature import RsaKeys, post_request, send_post_request
from app.activitypub.util import extract_domain_and_actor, find_actor_or_create
from app.chat.util import send_message
from app.community.forms import SearchRemoteCommunity, CreateDiscussionForm, CreateImageForm, CreateLinkForm, \
    ReportCommunityForm, \
    DeleteCommunityForm, AddCommunityForm, EditCommunityForm, AddModeratorForm, BanUserCommunityForm, \
    EscalateReportForm, ResolveReportForm, CreateVideoForm, CreatePollForm, EditCommunityWikiPageForm, \
    InviteCommunityForm, MoveCommunityForm, EditCommunityFlairForm, SetMyFlairForm, FindAndBanUserCommunityForm
from app.community.util import search_for_community, actor_to_community, \
    save_icon_file, save_banner_file, \
    delete_post_from_community, delete_post_reply_from_community, community_in_list, find_local_users, \
    find_potential_moderators, hashtags_used_in_community
from app.constants import SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, POST_TYPE_LINK, POST_TYPE_ARTICLE, POST_TYPE_IMAGE, \
    SUBSCRIPTION_PENDING, SUBSCRIPTION_MODERATOR, REPORT_STATE_NEW, REPORT_STATE_ESCALATED, REPORT_STATE_RESOLVED, \
    REPORT_STATE_DISCARDED, POST_TYPE_VIDEO, NOTIF_COMMUNITY, NOTIF_POST, POST_TYPE_POLL, MICROBLOG_APPS, SRC_WEB, \
    NOTIF_REPORT, NOTIF_NEW_MOD, NOTIF_BAN, NOTIF_UNBAN, NOTIF_REPORT_ESCALATION, NOTIF_MENTION, POST_STATUS_REVIEWING
from app.email import send_email
from app.inoculation import inoculation
from app.models import User, Community, CommunityMember, CommunityJoinRequest, CommunityBan, Post, Site, \
    File, PostVote, utcnow, Report, Notification, ActivityPubLog, Topic, Conversation, PostReply, \
    NotificationSubscription, UserFollower, Instance, Language, Poll, PollChoice, ModLog, CommunityWikiPage, \
    CommunityWikiPageRevision, read_posts, Feed, FeedItem, CommunityBlock, CommunityFlair, post_flair, UserFlair, \
    post_tag, Tag
from app.community import bp
from app.post.util import tags_to_string
from app.shared.community import invite_with_chat, invite_with_email, subscribe_community, add_mod_to_community, \
    remove_mod_from_community
from app.utils import get_setting, render_template, allowlist_html, markdown_to_html, validation_required, \
    shorten_string, gibberish, community_membership, ap_datetime, \
    request_etag_matches, return_304, can_upvote, can_downvote, user_filters_posts, \
    joined_communities, moderating_communities, blocked_domains, mimetype_from_url, blocked_instances, \
    community_moderators, communities_banned_from, show_ban_message, recently_upvoted_posts, recently_downvoted_posts, \
    blocked_users, languages_for_form, menu_topics, add_to_modlog, \
    blocked_communities, remove_tracking_from_link, piefed_markdown_to_lemmy_markdown, \
    instance_software, domain_from_email, referrer, flair_for_form, find_flair_id, login_required_if_private_instance, \
    possible_communities, reported_posts, user_notes, login_required, get_task_session, patch_db_session
from app.shared.post import make_post, sticky_post
from app.shared.tasks import task_selector
from app.utils import get_recipient_language
from feedgen.feed import FeedGenerator
from datetime import timezone, timedelta


@bp.route('/add_local', methods=['GET', 'POST'])
@login_required
def add_local():
    if current_user.banned:
        return show_ban_message()

    try:
        site = g.site
    except:
        site = Site.query.get(1)

    if not current_user.is_admin() and site.community_creation_admin_only:
        flash(_('Community creation has been restricted to admins on this site'))
        return redirect(url_for('main.list_communities'))

    form = AddCommunityForm()
    if g.site.enable_nsfw is False:
        form.nsfw.render_kw = {'disabled': True}

    form.languages.choices = languages_for_form(all=True)

    if form.validate_on_submit():
        if form.url.data.strip().lower().startswith('/c/'):
            form.url.data = form.url.data[3:]
        form.url.data = slugify(form.url.data.strip(), separator='_').lower()
        private_key, public_key = RsaKeys.generate_keypair()
        community = Community(title=form.community_name.data, name=form.url.data,
                              description=piefed_markdown_to_lemmy_markdown(form.description.data),
                              nsfw=form.nsfw.data, private_key=private_key,
                              public_key=public_key, description_html=markdown_to_html(form.description.data),
                              local_only=form.local_only.data, posting_warning=form.posting_warning.data,
                              ap_profile_id='https://' + current_app.config['SERVER_NAME'] + '/c/' + form.url.data.lower(),
                              ap_public_url='https://' + current_app.config['SERVER_NAME'] + '/c/' + form.url.data,
                              ap_followers_url='https://' + current_app.config['SERVER_NAME'] + '/c/' + form.url.data + '/followers',
                              ap_moderators_url='https://' + current_app.config['SERVER_NAME'] + '/c/' + form.url.data + '/moderators',
                              ap_domain=current_app.config['SERVER_NAME'],
                              subscriptions_count=1, instance_id=1, low_quality='memes' in form.url.data)
        icon_file = request.files['icon_file']
        if icon_file and icon_file.filename != '':
            file = save_icon_file(icon_file)
            if file:
                community.icon = file
        banner_file = request.files['banner_file']
        if banner_file and banner_file.filename != '':
            file = save_banner_file(banner_file)
            if file:
                community.image = file
        db.session.add(community)
        db.session.commit()
        membership = CommunityMember(user_id=current_user.id, community_id=community.id, is_moderator=True,
                                     is_owner=True)
        db.session.add(membership)
        # Languages of the community
        for language_choice in form.languages.data:
            community.languages.append(Language.query.get(language_choice))
        # Always include the undetermined language, so posts with no language will be accepted
        community.languages.append(Language.query.filter(Language.code == 'und').first())
        db.session.commit()
        flash(_('Your new community has been created.'))
        cache.delete_memoized(community_membership, current_user, community)
        cache.delete_memoized(joined_communities, current_user.id)
        cache.delete_memoized(moderating_communities, current_user.id)
        return redirect('/c/' + community.name)

    return render_template('community/add_local.html', title=_('Create community'), form=form,
                           current_app=current_app)


@bp.route('/add_remote', methods=['GET', 'POST'])
@login_required
def add_remote():
    if current_user.banned:
        return show_ban_message()
    form = SearchRemoteCommunity()
    new_community = None
    if form.validate_on_submit():
        address = form.address.data.strip().lower()
        if address.startswith('!') and '@' in address:
            try:
                new_community = search_for_community(address)
            except Exception as e:
                if 'is blocked.' in str(e):
                    flash(_('Sorry, that instance is blocked, check https://gui.fediseer.com/ for reasons.'), 'warning')
        elif address.startswith('@') and '@' in address[1:]:
            # todo: the user is searching for a person instead
            ...
        elif '@' in address:
            new_community = search_for_community('!' + address)
        elif address.startswith('https://'):
            server, community = extract_domain_and_actor(address)
            new_community = search_for_community('!' + community + '@' + server)
        else:
            message = Markup('Accepted address formats: !community@server.name or https://server.name/{c|m}/community. Search on <a href="https://lemmyverse.net/communities">Lemmyverse.net</a> to find some.')
            flash(message, 'error')
        if new_community is None:
            if g.site.enable_nsfw:
                flash(_('Community not found.'), 'warning')
            else:
                flash(_('Community not found. If you are searching for a nsfw community it is blocked by this instance.'),
                      'warning')
        else:
            if new_community.banned:
                flash(_('That community is banned from %(site)s.', site=g.site.name), 'warning')

    return render_template('community/add_remote.html',
                           title=_('Add remote community'), form=form, new_community=new_community,
                           subscribed=community_membership(current_user, new_community) >= SUBSCRIPTION_MEMBER,
                           )


# endpoint used by htmx in the add_remote.html
@bp.route('/search-names', methods=['GET'])
def community_name_search():
    # if nsfw is enabled load the all_communities json, otherwise load the sfw one
    # if they dont exist, just make an empty list
    communities_list = []
    try:
        if g.site.enable_nsfw:
            with open('app/static/tmp/all_communities.json', 'r') as acj:
                all_communities_json = json.load(acj)
                communities_list = all_communities_json['all_communities']
        else:
            with open('app/static/tmp/all_sfw_communities.json', 'r') as asfwcj:
                all_sfw_communities_json = json.load(asfwcj)
                communities_list = all_sfw_communities_json['all_sfw_communities']
    except:
        communities_list = []

    if request.args.get('address'):
        search_term = request.args.get('address')
        searched_community_names = ''
        for c in communities_list:
            if isinstance(c, str):
                if search_term in c:
                    searched_community_names = searched_community_names + _make_community_results_datalist_html(c)
        return searched_community_names
    else:
        return ''


# returns a string with html in it for the add_remote search function above
def _make_community_results_datalist_html(community_name):
    return f'<option value="{community_name}"></option>'


# @bp.route('/c/<actor>', methods=['GET']) - defined in activitypub/routes.py, which calls this function for user requests. A bit weird.
@login_required_if_private_instance
def show_community(community: Community):
    if community.banned:
        abort(404)

    # If current user is logged in check if they have any feeds
    # if they have feeds, find the first feed that contains
    # this community
    user_has_feeds = False
    current_feed_id = 0
    current_feed_title = "None"
    if current_user.is_authenticated and len(Feed.query.filter_by(user_id=current_user.id).all()) > 0:
        user_has_feeds = True
        current_feed = Feed.query.filter(Feed.user_id == current_user.id).join(FeedItem, FeedItem.feed_id == Feed.id).filter(
            FeedItem.community_id == community.id).first()
        if current_feed is not None:
            current_feed_id = current_feed.id
            current_feed_title = current_feed.title

    page = request.args.get('page', 1, type=int)
    sort = request.args.get('sort', '' if current_user.is_anonymous else current_user.default_sort)
    content_type = request.args.get('content_type', 'posts')
    flair = request.args.get('flair', '')
    tag = request.args.get('tag', '')
    if sort is None:
        sort = ''
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    if low_bandwidth:
        post_layout = None
    else:
        if community.default_layout is not None and community.default_layout != '':
            post_layout = request.args.get('layout', community.default_layout)
        else:
            post_layout = request.args.get('layout', 'list')

    # If nothing has changed since their last visit, return HTTP 304
    current_etag = f"{community.id}{sort}{post_layout}_{hash(community.last_active)}"
    if current_user.is_anonymous and request_etag_matches(current_etag):
        return return_304(current_etag)

    mods = community_moderators(community.id)

    if current_user.is_authenticated and community.id not in communities_banned_from(current_user.id):
        is_moderator = any(mod.user_id == current_user.id for mod in mods)
        is_owner = any(mod.user_id == current_user.id and mod.is_owner == True for mod in mods)
        is_admin = current_user.is_admin()
    else:
        is_moderator = False
        is_owner = False
        is_admin = False

    banned_from_community = False
    if current_user.is_authenticated and community.id in communities_banned_from(current_user.id):
        ban_details = CommunityBan.query.filter(CommunityBan.user_id == current_user.id,
                                                CommunityBan.community_id == community.id).first()
        banned_from_community = True
        if ban_details:
            if ban_details.ban_until:
                flash(_('You have been banned from this community until %(when)s.', when=ban_details.ban_until.date()))
            else:
                flash(_('You have been banned from this community.'))

    # Build list of moderators and set un-moderated flag
    mod_user_ids = [mod.user_id for mod in mods]
    un_moderated = False
    if community.private_mods:
        mod_list = []
        inactive_mods = User.query.filter(User.id.in_(mod_user_ids),
                                          User.last_seen < utcnow() - timedelta(days=60)).all()
    else:
        mod_list = User.query.filter(User.id.in_(mod_user_ids)).all()
        inactive_mods = []
        for mod in mod_list:
            if mod.last_seen < utcnow() - timedelta(days=60):
                inactive_mods.append(mod)
    if current_user.is_authenticated and (current_user.is_admin() or current_user.is_staff()):
        un_moderated = len(mod_user_ids) == len(inactive_mods)

    # user flair in sidebar and teasers
    user_flair = {}
    for u_flair in UserFlair.query.filter(UserFlair.community_id == community.id):
        user_flair[u_flair.user_id] = u_flair.flair

    sticky_posts = None
    posts = None
    comments = None
    if content_type == 'posts':
        posts = community.posts

        # filter out nsfw and nsfl if desired
        if current_user.is_anonymous:
            posts = posts.filter(Post.from_bot == False, Post.nsfw == False, Post.nsfl == False, Post.deleted == False,
                                 Post.status > POST_STATUS_REVIEWING, Post.status > POST_STATUS_REVIEWING)
            content_filters = {}
            user = None
        else:
            user = current_user
            if current_user.ignore_bots == 1:
                posts = posts.filter(Post.from_bot == False)
            if current_user.hide_nsfl == 1:
                posts = posts.filter(Post.nsfl == False)
            if current_user.hide_nsfw == 1:
                posts = posts.filter(Post.nsfw == False)
            if current_user.hide_read_posts and not tag:
                posts = posts.outerjoin(read_posts, (Post.id == read_posts.c.read_post_id) & (
                        read_posts.c.user_id == current_user.id))
                posts = posts.filter(read_posts.c.read_post_id.is_(None))  # Filter where there is no corresponding read post for the current user
            content_filters = user_filters_posts(current_user.id)
            posts = posts.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING)

            # filter domains and instances
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

        # Filter by post flair
        if flair:
            flair_id = find_flair_id(flair, community.id)
            if flair_id:
                posts = posts.join(post_flair).filter(post_flair.c.flair_id == flair_id)

        # Filter by post tag
        if tag:
            tag_record = Tag.query.filter(Tag.name == tag.strip()).first()
            if tag_record:
                posts = posts.join(post_tag).filter(post_tag.c.tag_id == tag_record.id)

        sticky_posts = posts.filter(Post.sticky == True)
        posts = posts.filter(Post.sticky == False)

        if sort == '' or sort == 'hot':
            sticky_posts = sticky_posts.order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
            posts = posts.order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
        elif sort == "top_12h":
            sticky_posts = sticky_posts.order_by(desc(Post.up_votes - Post.down_votes))
            posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=12)).\
                order_by(desc(Post.up_votes - Post.down_votes))
        elif sort == 'top':
            sticky_posts = sticky_posts.order_by(desc(Post.up_votes - Post.down_votes))
            posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=24)).\
                order_by(desc(Post.up_votes - Post.down_votes))
        elif sort == 'top_1w':
            sticky_posts = sticky_posts.order_by(desc(Post.up_votes - Post.down_votes))
            posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=7)).\
                order_by(desc(Post.up_votes - Post.down_votes))
        elif sort == 'top_1m':
            sticky_posts = sticky_posts.order_by(desc(Post.up_votes - Post.down_votes))
            posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=28)).\
                order_by(desc(Post.up_votes - Post.down_votes))
        elif sort == 'top_1y':
            sticky_posts = sticky_posts.order_by(desc(Post.up_votes - Post.down_votes))
            posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=365)).\
                order_by(desc(Post.up_votes - Post.down_votes))
        elif sort == 'top_all':
            sticky_posts = sticky_posts.order_by(desc(Post.up_votes - Post.down_votes))
            posts = posts.order_by(desc(Post.up_votes - Post.down_votes))
        elif sort == 'new':
            sticky_posts = sticky_posts.order_by(desc(Post.posted_at))
            posts = posts.order_by(desc(Post.posted_at))
        elif sort == 'old':
            sticky_posts = sticky_posts.order_by(asc(Post.posted_at))
            posts = posts.order_by(asc(Post.posted_at))
        elif sort == 'active':
            sticky_posts = sticky_posts.order_by(desc(Post.sticky)).order_by(desc(Post.last_active))
            posts = posts.order_by(desc(Post.sticky)).order_by(desc(Post.last_active))
        per_page = 20 if low_bandwidth else current_app.config['PAGE_LENGTH']
        if post_layout == 'masonry':
            per_page = 200
        elif post_layout == 'masonry_wide':
            per_page = 300
        posts = posts.paginate(page=page, per_page=per_page, error_out=False)
        sticky_posts = sticky_posts.all()
    else:
        content_filters = {}
        comments = community.replies

        # filter out nsfw and nsfl if desired
        if current_user.is_anonymous:
            comments = comments.filter(PostReply.from_bot == False, PostReply.nsfw == False, PostReply.deleted == False)
            user = None
        else:
            user = current_user
            if current_user.ignore_bots == 1:
                comments = comments.filter(PostReply.from_bot == False)
            if current_user.hide_nsfw == 1:
                comments = comments.filter(PostReply.nsfw == False)

            comments = comments.filter(PostReply.deleted == False)

            # filter instances
            instance_ids = blocked_instances(current_user.id)
            if instance_ids:
                comments = comments.filter(or_(PostReply.instance_id.not_in(instance_ids), PostReply.instance_id == None))

            # filter blocked users
            blocked_accounts = blocked_users(current_user.id)
            if blocked_accounts:
                comments = comments.filter(PostReply.user_id.not_in(blocked_accounts))

        if sort == '' or sort == 'hot':
            comments = comments.order_by(desc(PostReply.ranking)).order_by(desc(PostReply.posted_at))
        elif sort == 'top_12h':
            comments = comments.filter(PostReply.posted_at > utcnow() - timedelta(hours=12)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'top':
            comments = comments.filter(PostReply.posted_at > utcnow() - timedelta(hours=24)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'top_1w':
            comments = comments.filter(PostReply.posted_at > utcnow() - timedelta(days=7)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'top_1m':
            comments = comments.filter(PostReply.posted_at > utcnow() - timedelta(days=28)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'top_1y':
            comments = comments.filter(PostReply.posted_at > utcnow() - timedelta(days=365)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'top_all':
            comments = comments.order_by(desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'new' or sort == 'active':
            comments = comments.order_by(desc(PostReply.posted_at))
        elif sort == 'old':
            comments = comments.order_by(asc(PostReply.posted_at))
        per_page = 100
        comments = comments.paginate(page=page, per_page=per_page, error_out=False)

    community_feeds = Feed.query.join(FeedItem, FeedItem.feed_id == Feed.id).\
        filter(FeedItem.community_id == community.id).filter(Feed.public == True).all()

    community_flair = CommunityFlair.query.filter(CommunityFlair.community_id == community.id).\
        order_by(CommunityFlair.flair).all()

    breadcrumbs = []
    breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
    breadcrumb.text = _('Home')
    breadcrumb.url = '/'
    breadcrumbs.append(breadcrumb)

    if community.topic_id:
        related_communities = Community.query.filter_by(topic_id=community.topic_id). \
            filter(Community.id != community.id, Community.banned == False).order_by(Community.name)
        topics = []
        previous_topic = Topic.query.get(community.topic_id)
        topics.append(previous_topic)
        while previous_topic.parent_id:
            topic = Topic.query.get(previous_topic.parent_id)
            topics.append(topic)
            previous_topic = topic
        topics = list(reversed(topics))

        breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
        breadcrumb.text = _('Topics')
        breadcrumb.url = '/topics'
        breadcrumbs.append(breadcrumb)

        existing_url = '/topic'
        for topic in topics:
            breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
            breadcrumb.text = topic.name
            breadcrumb.url = f"{existing_url}/{topic.machine_name}"
            breadcrumbs.append(breadcrumb)
            existing_url = breadcrumb.url
    else:
        related_communities = []
        if len(community_feeds) == 0:
            breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
            breadcrumb.text = _('Communities')
            breadcrumb.url = '/communities'
            breadcrumbs.append(breadcrumb)
        else:
            breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
            breadcrumb.text = _('Feeds')
            breadcrumb.url = '/feeds'
            breadcrumbs.append(breadcrumb)

            feeds = []
            previous_feed = community_feeds[0]
            feeds.append(previous_feed)
            while previous_feed.parent_feed_id:
                feed = Feed.query.get(previous_feed.parent_feed_id)
                feeds.append(feed)
                previous_feed = feed
            feeds = list(reversed(feeds))

            for feed in feeds:
                breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
                breadcrumb.text = feed.title
                breadcrumb.url = f"/f/{feed.link()}"
                breadcrumbs.append(breadcrumb)

    description = shorten_string(community.description, 150) if community.description else None
    og_image = community.image.source_url if community.image_id else None

    if content_type == 'posts':
        next_url = url_for('activitypub.community_profile',
                           actor=community.ap_id if community.ap_id is not None else community.name,
                           page=posts.next_num, sort=sort, layout=post_layout,
                           content_type=content_type) if posts.has_next else None
        prev_url = url_for('activitypub.community_profile',
                           actor=community.ap_id if community.ap_id is not None else community.name,
                           page=posts.prev_num, sort=sort, layout=post_layout,
                           content_type=content_type) if posts.has_prev and page != 1 else None
    else:
        next_url = url_for('activitypub.community_profile',
                           actor=community.ap_id if community.ap_id is not None else community.name,
                           page=comments.next_num, sort=sort, layout=post_layout,
                           content_type=content_type) if comments.has_next else None
        prev_url = url_for('activitypub.community_profile',
                           actor=community.ap_id if community.ap_id is not None else community.name,
                           page=comments.prev_num, sort=sort, layout=post_layout,
                           content_type=content_type) if comments.has_prev and page != 1 else None

    # Voting history
    if current_user.is_authenticated:
        recently_upvoted = recently_upvoted_posts(current_user.id)
        recently_downvoted = recently_downvoted_posts(current_user.id)
    else:
        recently_upvoted = []
        recently_downvoted = []

    return render_template('community/community.html', community=community, title=community.title,
                           breadcrumbs=breadcrumbs,
                           is_moderator=is_moderator, is_owner=is_owner, is_admin=is_admin, mods=mod_list, posts=posts,
                           comments=comments,
                           description=description, og_image=og_image, POST_TYPE_IMAGE=POST_TYPE_IMAGE,
                           POST_TYPE_LINK=POST_TYPE_LINK,
                           POST_TYPE_VIDEO=POST_TYPE_VIDEO, POST_TYPE_POLL=POST_TYPE_POLL,
                           SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING,
                           SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER,
                           SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           etag=f"{community.id}{sort}{post_layout}_{hash(community.last_active)}",
                           related_communities=related_communities,
                           next_url=next_url, prev_url=prev_url, low_bandwidth=low_bandwidth, un_moderated=un_moderated,
                           community_flair=community_flair,
                           recently_upvoted=recently_upvoted, recently_downvoted=recently_downvoted,
                           community_feeds=community_feeds,
                           canonical=community.profile_id(), can_upvote_here=can_upvote(user, community),
                           can_downvote_here=can_downvote(user, community),
                           rss_feed=f"https://{current_app.config['SERVER_NAME']}/community/{community.link()}/feed",
                           rss_feed_name=f"{community.title} on {g.site.name}",
                           content_filters=content_filters, sort=sort, flair=flair, show_post_community=False,
                           tags=hashtags_used_in_community(community.id, content_filters),
                           reported_posts=reported_posts(current_user.get_id(), g.admin_ids),
                           user_notes=user_notes(current_user.get_id()), banned_from_community=banned_from_community,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                           post_layout=post_layout, content_type=content_type, current_app=current_app,
                           user_has_feeds=user_has_feeds, current_feed_id=current_feed_id,
                           current_feed_title=current_feed_title, user_flair=user_flair, sticky_posts=sticky_posts)


# RSS feed of the community
@bp.route('/<actor>/feed', methods=['GET'])
@cache.cached(timeout=600)
def show_community_rss(actor):
    actor = actor.strip()
    if '@' in actor:
        community: Community = Community.query.filter_by(ap_id=actor, banned=False).first()
    else:
        community: Community = Community.query.filter_by(name=actor, banned=False, ap_id=None).first()
    if community is not None:
        # If nothing has changed since their last visit, return HTTP 304
        current_etag = f"{community.id}_{hash(community.last_active)}"
        if request_etag_matches(current_etag):
            return return_304(current_etag, 'application/rss+xml')

        posts = community.posts.filter(Post.from_bot == False, Post.deleted == False,
                                       Post.status > POST_STATUS_REVIEWING).order_by(desc(Post.created_at)).limit(20).all()
        description = shorten_string(community.description, 150) if community.description else None
        og_image = community.image.source_url if community.image_id else None
        fg = FeedGenerator()
        fg.id(f"https://{current_app.config['SERVER_NAME']}/c/{actor}")
        fg.title(f'{community.title} on {g.site.name}')
        fg.link(href=f"https://{current_app.config['SERVER_NAME']}/c/{actor}", rel='alternate')
        if og_image:
            fg.logo(og_image)
        else:
            fg.logo(f"https://{current_app.config['SERVER_NAME']}/static/images/apple-touch-icon.png")
        if description:
            fg.subtitle(description)
        else:
            fg.subtitle(' ')
        fg.link(href=f"https://{current_app.config['SERVER_NAME']}/c/{actor}/feed", rel='self')
        fg.language('en')

        for post in posts:
            fe = fg.add_entry()
            fe.title(post.title)
            fe.link(href=f"https://{current_app.config['SERVER_NAME']}/post/{post.id}")
            if post.url:
                type = mimetype_from_url(post.url)
                if type and not type.startswith('text/'):
                    fe.enclosure(post.url, type=type)
            fe.description(post.body_html)
            fe.guid(post.profile_id(), permalink=True)
            fe.author(name=post.author.user_name)
            fe.pubDate(post.created_at.replace(tzinfo=timezone.utc))

        response = make_response(fg.rss_str())
        response.headers.set('Content-Type', 'application/rss+xml')
        response.headers.add_header('ETag', f"{community.id}_{hash(community.last_active)}")
        response.headers.add_header('Cache-Control', 'no-cache, max-age=600, must-revalidate')
        return response
    else:
        abort(404)


@bp.route('/<actor>/subscribe', methods=['GET', 'POST'])
@login_required
@validation_required
def subscribe(actor):
    # POST is used by htmx, GET when JS is disabled
    do_subscribe(actor, current_user.id, admin_preload=request.method == 'POST')
    if request.method == 'POST':
        community = actor_to_community(actor)
        return render_template('community/_leave_button.html', community=community)
    else:
        referrer = request.headers.get('Referer', None)
        if referrer is not None and current_app.config['SERVER_NAME'] in referrer:
            return redirect(referrer)
        else:
            return redirect('/c/' + actor)


# this is separated out from the subscribe route so it can be used by the 
# admin.admin_federation.preload_form and feed subscription process as well
@celery.task
def do_subscribe(actor, user_id, admin_preload=False, joined_via_feed=False):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                remote = False
                actor = actor.strip()
                user = User.query.get(user_id)
                pre_load_message = {}
                if '@' in actor:
                    community = Community.query.filter_by(ap_id=actor).first()
                    if community is None:
                        community = search_for_community(f'!{actor}' if '!' not in actor else actor)
                    if community.banned:
                        community = None
                    remote = True
                else:
                    community = Community.query.filter_by(name=actor, banned=False, ap_id=None).first()

                if community is not None:
                    pre_load_message['community'] = community.ap_id
                    if community.id in communities_banned_from(user.id):
                        if not admin_preload:
                            abort(401)
                        else:
                            pre_load_message['user_banned'] = True
                    if community_membership(user, community) != SUBSCRIPTION_MEMBER and community_membership(user, community) != SUBSCRIPTION_PENDING:
                        banned = CommunityBan.query.filter_by(user_id=user.id, community_id=community.id).first()
                        if banned:
                            if not admin_preload:
                                if current_user and current_user.id == user_id:
                                    flash(_('You cannot join this community'))
                            else:
                                pre_load_message['community_banned_by_local_instance'] = True
                        # for local communities, joining is instant
                        existing_membership = CommunityMember.query.filter_by(user_id=user.id, community_id=community.id).first()
                        if not existing_membership:
                            member = CommunityMember(user_id=user.id, community_id=community.id, joined_via_feed=joined_via_feed)
                            db.session.add(member)
                            community.subscriptions_count += 1
                            db.session.commit()
                            cache.delete_memoized(community_membership, user, community)

                        if remote:
                            # send ActivityPub message to remote community, asking to follow. Accept message will be sent to our shared inbox
                            join_request = CommunityJoinRequest(user_id=user.id, community_id=community.id,
                                                                joined_via_feed=joined_via_feed)

                            db.session.add(join_request)
                            db.session.commit()
                            if community.instance.online():
                                follow = {
                                    "actor": user.public_url(),
                                    "to": [community.public_url()],
                                    "object": community.public_url(),
                                    "type": "Follow",
                                    "id": f"https://{current_app.config['SERVER_NAME']}/activities/follow/{join_request.uuid}"
                                }
                                send_post_request(community.ap_inbox_url, follow, user.private_key, user.public_url() + '#main-key', timeout=10)

                        if not admin_preload:
                            if current_user and current_user.is_authenticated and current_user.id == user_id:
                                flash(Markup(_('You joined %(community_name)s',
                                               community_name=f'<a href="/c/{community.link()}">{community.display_name()}</a>')))
                        else:
                            pre_load_message['status'] = 'joined'
                    else:
                        if admin_preload:
                            pre_load_message['status'] = 'already subscribed, or subscription pending'

                    cache.delete_memoized(community_membership, user, community)
                    cache.delete_memoized(joined_communities, user.id)
                    if admin_preload:
                        return pre_load_message
                else:
                    if not admin_preload:
                        abort(404)
                    else:
                        pre_load_message['community'] = actor
                        pre_load_message['status'] = 'community not found'
                        return pre_load_message
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@bp.route('/<actor>/unsubscribe', methods=['GET', 'POST'])
@login_required
def unsubscribe(actor):
    # POST is used by htmx, GET when JS is disabled
    community = actor_to_community(actor)

    if community is not None:
        subscription = community_membership(current_user, community)
        if subscription:
            if subscription != SUBSCRIPTION_OWNER:
                # Undo the Follow
                if '@' in actor:  # this is a remote community, so activitypub is needed
                    if not community.instance.gone_forever:
                        follow_id = f"https://{current_app.config['SERVER_NAME']}/activities/follow/{gibberish(15)}"
                        if community.instance.domain == 'a.gup.pe':
                            join_request = CommunityJoinRequest.query.filter_by(user_id=current_user.id,
                                                                                community_id=community.id).first()
                            if join_request:
                                follow_id = f"https://{current_app.config['SERVER_NAME']}/activities/follow/{join_request.uuid}"
                        undo_id = f"https://{current_app.config['SERVER_NAME']}/activities/undo/" + gibberish(15)
                        follow = {
                            "actor": current_user.public_url(),
                            "to": [community.public_url()],
                            "object": community.public_url(),
                            "type": "Follow",
                            "id": follow_id
                        }
                        undo = {
                            'actor': current_user.public_url(),
                            'to': [community.public_url()],
                            'type': 'Undo',
                            'id': undo_id,
                            'object': follow
                        }
                        send_post_request(community.ap_inbox_url, undo, current_user.private_key,
                                          current_user.public_url() + '#main-key', timeout=10)

                db.session.query(CommunityMember).filter_by(user_id=current_user.id, community_id=community.id).delete()
                db.session.query(CommunityJoinRequest).filter_by(user_id=current_user.id, community_id=community.id).delete()
                community.subscriptions_count -= 1
                db.session.commit()

                if request.method == 'GET':
                    flash(Markup(_('You left %(community_name)s',
                                   community_name=f'<a href="/c/{community.link()}">{community.display_name()}</a>')))
                cache.delete_memoized(community_membership, current_user, community)
                cache.delete_memoized(joined_communities, current_user.id)
            else:
                # todo: community deletion
                flash(_('You need to make someone else the owner before unsubscribing.'), 'warning')

        if request.method == 'POST':
            return render_template('community/_join_button.html', community=community)
        else:
            # send them back where they came from
            referrer = request.headers.get('Referer', None)
            if referrer is not None and current_app.config['SERVER_NAME'] in referrer:
                return redirect(referrer)
            else:
                return redirect('/c/' + actor)
    else:
        abort(404)


@bp.route('/<actor>/join_then_add', methods=['GET', 'POST'])
@login_required
@validation_required
def join_then_add(actor):
    community = actor_to_community(actor)
    if not current_user.subscribed(community.id):
        if not community.is_local():
            # send ActivityPub message to remote community, asking to follow. Accept message will be sent to our shared inbox
            join_request = CommunityJoinRequest(user_id=current_user.id, community_id=community.id)
            db.session.add(join_request)
            db.session.commit()
            if not community.instance.gone_forever:
                follow = {
                    "actor": current_user.public_url(),
                    "to": [community.public_url()],
                    "object": community.public_url(),
                    "type": "Follow",
                    "id": f"https://{current_app.config['SERVER_NAME']}/activities/follow/{join_request.uuid}"
                }
                send_post_request(community.ap_inbox_url, follow, current_user.private_key,
                                  current_user.public_url() + '#main-key')
        existing_member = CommunityMember.query.filter_by(user_id=current_user.id, community_id=community.id).first()
        if not existing_member:
            member = CommunityMember(user_id=current_user.id, community_id=community.id)
            db.session.add(member)
            db.session.commit()
        flash(Markup(_('You joined %(community_name)s',
                       community_name=f'<a href="/c/{community.link()}">{community.display_name()}</a>')))
    if not community.user_is_banned(current_user):
        return redirect(url_for('community.add_post', actor=community.link(), type='discussion'))
    else:
        abort(401)


@bp.route('/<actor>/submit/<string:type>', methods=['GET', 'POST'])
@bp.route('/<actor>/submit', defaults={'type': 'discussion'}, methods=['GET', 'POST'])
@login_required
@validation_required
def add_post(actor, type):
    if current_user.banned or current_user.ban_posts:
        return show_ban_message()
    if request.method == 'GET':
        community = actor_to_community(actor)
    else:
        if request.form.get('communities'):
            community = Community.query.get_or_404(request.form.get('communities'))
        else:
            community = actor_to_community(actor)

    post_type = POST_TYPE_ARTICLE
    if type == 'discussion':
        form = CreateDiscussionForm()
    elif type == 'link':
        post_type = POST_TYPE_LINK
        form = CreateLinkForm()
    elif type == 'image':
        post_type = POST_TYPE_IMAGE
        form = CreateImageForm()
    elif type == 'video':
        post_type = POST_TYPE_VIDEO
        form = CreateVideoForm()
    elif type == 'poll':
        post_type = POST_TYPE_POLL
        form = CreatePollForm()
    else:
        abort(404)

    if community.nsfw:
        form.nsfw.data = True
        form.nsfw.render_kw = {'disabled': True}
    if community.nsfl:
        form.nsfl.data = True
        form.nsfw.render_kw = {'disabled': True}
    if not (community.is_moderator() or community.is_owner() or current_user.is_admin()):
        form.sticky.render_kw = {'disabled': True}

    form.communities.choices = possible_communities()

    form.language_id.choices = languages_for_form()
    flair_choices = flair_for_form(community.id)
    if len(flair_choices):
        form.flair.choices = flair_choices
    else:
        del form.flair

    if form.validate_on_submit():
        try:
            # Fire before_post_create hook for plugins
            post_data = {
                'title': form.title.data,
                'content': form.body.data if hasattr(form, 'body') else '',
                'community': community.name,
                'post_type': post_type,
                'user_id': current_user.id
            }
            plugins.fire_hook('before_post_create', post_data)
            
            uploaded_file = request.files['image_file'] if type == 'image' else None
            post = make_post(form, community, post_type, SRC_WEB, uploaded_file=uploaded_file)
        except Exception as ex:
            flash(_('Your post was not accepted because %(reason)s', reason=str(ex)), 'error')
            return redirect(url_for('activitypub.community_profile',
                                    actor=community.ap_id if community.ap_id is not None else community.name))

        if form.timezone.data:
            db.session.execute(text('UPDATE "user" SET timezone = :timezone WHERE id = :user_id'),
                               {'user_id': current_user.id, 'timezone': form.timezone.data})
            db.session.commit()

        if post.sticky:
            sticky_post(post.id, True, SRC_WEB)  # federating post's stickiness is separate from creating it

        resp = make_response(redirect(f"/post/{post.id}"))
        # remove cookies used to maintain state when switching post type
        resp.delete_cookie('post_title')
        resp.delete_cookie('post_description')
        resp.delete_cookie('post_tags')
        return resp
    else:  # GET
        form.communities.data = community.id
        form.notify_author.data = True
        if post_type == POST_TYPE_POLL:
            form.finish_in.data = '3d'
        if community.posting_warning:
            flash(community.posting_warning)

        form.timezone.data = current_user.timezone

        # The source query parameter is used when cross-posting - load the source post's content into the form
        if (post_type == POST_TYPE_LINK or post_type == POST_TYPE_VIDEO) and request.args.get('source'):
            source_post = Post.query.get(request.args.get('source'))
            if source_post.deleted:
                abort(404)
            form.title.data = source_post.title
            form.body.data = source_post.body
            form.nsfw.data = source_post.nsfw
            form.nsfl.data = source_post.nsfl
            form.language_id.data = source_post.language_id
            if post_type == POST_TYPE_LINK:
                form.link_url.data = source_post.url
            elif post_type == POST_TYPE_VIDEO:
                form.video_url.data = source_post.url
            form.tags.data = tags_to_string(source_post)

        if (post_type == POST_TYPE_LINK or post_type == POST_TYPE_VIDEO) and request.args.get('link'):
            if post_type == POST_TYPE_LINK:
                form.link_url.data = request.args.get('link')
            elif post_type == POST_TYPE_VIDEO:
                form.video_url.data = request.args.get('link')
            form.title.data = request.args.get('title')

    # empty post to pass since add_post.html extends edit_post.html 
    # and that one checks for a post.image_id for editing image posts
    post = None

    return render_template('community/add_post.html', title=_('Add post to community'), form=form,
                           post_type=post_type, community=community, post=post, hide_community_actions=True,
                           markdown_editor=current_user.markdown_editor, low_bandwidth=False, actor=actor,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                           )


@bp.route('/community/<int:community_id>/report', methods=['GET', 'POST'])
@login_required
def community_report(community_id: int):
    community = Community.query.get_or_404(community_id)
    form = ReportCommunityForm()
    if form.validate_on_submit():
        report = Report(reasons=form.reasons_to_string(form.reasons.data), description=form.description.data,
                        type=1, reporter_id=current_user.id, suspect_community_id=community.id, source_instance_id=1)
        db.session.add(report)

        # Notify admin
        # todo: find all instance admin(s). for now just load User.id == 1
        admins = [User.query.get_or_404(1)]
        targets_data = {'gen': '0', 'suspect_community_id': community.id, 'reporter_id': current_user.id}
        for admin in admins:
            with force_locale(get_recipient_language(admin.id)):
                notification = Notification(user_id=admin.id, title=gettext('A community has been reported'),
                                            url=community.local_url(),
                                            author_id=current_user.id, notif_type=NOTIF_REPORT,
                                            subtype='community_reported',
                                            targets=targets_data)
                db.session.add(notification)
                admin.unread_notifications += 1
        db.session.commit()

        # todo: federate report to originating instance
        if not community.is_local() and form.report_remote.data:
            ...

        flash(_('Community has been reported, thank you!'))
        return redirect(community.local_url())

    return render_template('community/community_report.html', title=_('Report community'), form=form,
                           community=community)


@bp.route('/community/<int:community_id>/edit', methods=['GET', 'POST'])
@login_required
def community_edit(community_id: int):
    from app.admin.util import topics_for_form
    if current_user.banned:
        return show_ban_message()
    community = Community.query.get_or_404(community_id)
    old_topic_id = community.topic_id if community.topic_id else None
    if community.is_owner() or current_user.is_admin() or community.is_moderator():
        form = EditCommunityForm()
        form.topic.choices = topics_for_form(0)
        form.languages.choices = languages_for_form(all=True)
        if g.site.enable_nsfw is False:
            form.nsfw.render_kw = {'disabled': True}
        if form.validate_on_submit():
            community.title = form.title.data
            community.description = piefed_markdown_to_lemmy_markdown(form.description.data)
            community.description_html = markdown_to_html(form.description.data, anchors_new_tab=False)
            community.posting_warning = form.posting_warning.data
            community.nsfw = form.nsfw.data
            community.local_only = form.local_only.data
            community.restricted_to_mods = form.restricted_to_mods.data
            community.new_mods_wanted = form.new_mods_wanted.data
            community.topic_id = form.topic.data if form.topic.data != 0 else None
            community.default_layout = form.default_layout.data
            community.downvote_accept_mode = form.downvote_accept_mode.data

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
                    cache.delete_memoized(Community.header_image, community)

            # Languages of the community
            db.session.execute(text('DELETE FROM "community_language" WHERE community_id = :community_id'),
                               {'community_id': community_id})
            for language_choice in form.languages.data:
                community.languages.append(Language.query.get(language_choice))
            # Always include the undetermined language, so posts with no language will be accepted
            community.languages.append(Language.query.filter(Language.code == 'und').first())
            db.session.commit()

            if community.topic_id != old_topic_id:
                if community.topic_id:
                    community.topic.num_communities = community.topic.communities.count()
                if old_topic_id:
                    topic = Topic.query.get(old_topic_id)
                    if topic:
                        topic.num_communities = topic.communities.count()
                db.session.commit()
            flash(_('Saved'))

            cache.delete_memoized(moderating_communities, current_user.id)
            cache.delete_memoized(joined_communities, current_user.id)

            # just borrow federation code for now (replacing most of this function with a call to edit_community in app.shared.community can be done "later")
            task_selector('edit_community', user_id=current_user.id, community_id=community.id)
            return redirect(url_for('activitypub.community_profile',
                                    actor=community.ap_id if community.ap_id is not None else community.name))
        else:
            form.title.data = community.title
            form.description.data = community.description
            form.posting_warning.data = community.posting_warning
            form.nsfw.data = community.nsfw
            form.local_only.data = community.local_only
            form.new_mods_wanted.data = community.new_mods_wanted
            form.restricted_to_mods.data = community.restricted_to_mods
            form.topic.data = community.topic_id if community.topic_id else None
            form.languages.data = community.language_ids()
            form.default_layout.data = community.default_layout
            form.downvote_accept_mode.data = community.downvote_accept_mode
        return render_template('community/community_edit.html', title=_('Edit community'), form=form,
                               current_app=current_app, current="edit_settings",
                               community=community)
    else:
        abort(401)


@bp.route('/community/<int:community_id>/remove_icon', methods=['POST'])
@login_required
def remove_icon(community_id):
    community = Community.query.get_or_404(community_id)
    if community.icon_id:
        community.icon.delete_from_disk()
        if community.icon_id:
            file = File.query.get(community.icon_id)
            file.delete_from_disk()
            community.icon_id = None
            db.session.delete(file)
            db.session.commit()
    return _('Icon removed!')


@bp.route('/community/<int:community_id>/remove_header', methods=['POST'])
@login_required
def remove_header(community_id):
    community = Community.query.get_or_404(community_id)
    if community.image_id:
        community.image.delete_from_disk()
        if community.image_id:
            file = File.query.get(community.image_id)
            file.delete_from_disk()
            community.image_id = None
            db.session.delete(file)
            db.session.commit()
            cache.delete_memoized(Community.header_image, community)
    return '<div> ' + _('Banner removed!') + '</div>'


@bp.route('/community/<int:community_id>/delete', methods=['GET', 'POST'])
@login_required
def community_delete(community_id: int):
    if current_user.banned:
        return show_ban_message()
    community = Community.query.get_or_404(community_id)
    if community.is_owner() or current_user.is_admin():
        form = DeleteCommunityForm()
        if form.validate_on_submit():
            if community.is_local():
                community.banned = True
                # todo: federate deletion out to all instances. At end of federation process, delete_dependencies() and delete community

            # record for modlog
            reason = f"Community {community.name} deleted by {current_user.user_name}"
            add_to_modlog('delete_community', actor=current_user, reason=reason, community=community)

            # actually delete the community
            community.delete_dependencies()
            db.session.delete(community)
            db.session.commit()

            flash(_('Community deleted'))
            return redirect('/communities')

        return render_template('community/community_delete.html', title=_('Delete community'), form=form,
                               community=community)
    else:
        abort(401)


@bp.route('/community/<int:community_id>/moderators', methods=['GET', 'POST'])
@login_required
def community_mod_list(community_id: int):
    if current_user.banned:
        return show_ban_message()
    community = Community.query.get_or_404(community_id)
    if community.is_owner() or current_user.is_admin() or community.is_moderator(current_user):

        moderators = User.query.filter(User.banned == False).join(CommunityMember, CommunityMember.user_id == User.id). \
            filter(CommunityMember.community_id == community_id,
                   or_(CommunityMember.is_moderator == True, CommunityMember.is_owner == True)).all()

        return render_template('community/community_mod_list.html',
                               title=_('Moderators for %(community)s', community=community.display_name()),
                               moderators=moderators, community=community, current="moderators")
    else:
        abort(401)


@bp.route('/community/<int:community_id>/moderators/add/<int:user_id>', methods=['GET', 'POST'])
@login_required
def community_add_moderator(community_id: int, user_id: int):
    if current_user.banned:
        return show_ban_message()
    try:
        add_mod_to_community(community_id, user_id, SRC_WEB)
    except Exception:
        abort(401)

    return redirect(url_for('community.community_mod_list', community_id=community_id))


@bp.route('/community/<int:community_id>/moderators/find', methods=['GET', 'POST'])
@login_required
def community_find_moderator(community_id: int):
    if current_user.banned:
        return show_ban_message()
    community = Community.query.get_or_404(community_id)
    if community.is_owner() or current_user.is_admin():
        form = AddModeratorForm()
        potential_moderators = None
        if form.validate_on_submit():
            potential_moderators = find_potential_moderators(form.user_name.data)

        return render_template('community/community_find_moderator.html', title=_('Add moderator to %(community)s',
                                                                                  community=community.display_name()),
                               community=community, form=form, potential_moderators=potential_moderators)
    else:
        abort(401)


@bp.route('/community/<int:community_id>/moderators/remove/<int:user_id>', methods=['POST'])
@login_required
def community_remove_moderator(community_id: int, user_id: int):
    if current_user.banned:
        return show_ban_message()

    try:
        remove_mod_from_community(community_id, user_id, SRC_WEB)
    except Exception:
        abort(401)

    return redirect(url_for('community.community_mod_list', community_id=community_id))


@bp.route('/community/<int:community_id>/block', methods=['GET', 'POST'])
@login_required
def community_block(community_id: int):
    community = Community.query.get_or_404(community_id)
    existing = CommunityBlock.query.filter_by(user_id=current_user.id, community_id=community_id).first()
    if not existing:
        db.session.add(CommunityBlock(user_id=current_user.id, community_id=community_id))
        db.session.commit()
        cache.delete_memoized(blocked_communities, current_user.id)
    flash(_('Posts in %(name)s will be hidden.', name=community.display_name()))
    return redirect(referrer())


@bp.route('/community/<int:community_id>/<int:user_id>/ban_user_community', methods=['GET', 'POST'])
@login_required
def community_ban_user(community_id: int, user_id: int):
    community = Community.query.get_or_404(community_id)
    user = User.query.get_or_404(user_id)
    existing = CommunityBan.query.filter_by(community_id=community.id, user_id=user.id).first()

    form = BanUserCommunityForm()
    if form.validate_on_submit():
        # Both CommunityBan and CommunityMember need to be updated. CommunityBan is under the control of moderators while
        # CommunityMember can be cleared by the user by leaving the group and rejoining. CommunityMember.is_banned stops
        # posts from the community from showing up in the banned person's home feed.
        if not existing:
            new_ban = CommunityBan(community_id=community_id, user_id=user.id, banned_by=current_user.id,
                                   reason=form.reason.data)
            if form.ban_until.data is not None and form.ban_until.data > utcnow().date():
                new_ban.ban_until = form.ban_until.data
            db.session.add(new_ban)
            db.session.commit()

        community_membership_record = CommunityMember.query.filter_by(community_id=community.id, user_id=user.id).first()
        if community_membership_record:
            community_membership_record.is_banned = True
            db.session.commit()

        flash(_('%(name)s has been banned.', name=user.display_name()))

        if form.delete_posts.data:
            posts = Post.query.filter(Post.user_id == user.id, Post.community_id == community.id).all()
            for post in posts:
                delete_post_from_community(post.id)
            if posts:
                flash(_('Posts by %(name)s have been deleted.', name=user.display_name()))
        if form.delete_post_replies.data:
            post_replies = PostReply.query.filter(PostReply.user_id == user.id, Post.community_id == community.id).all()
            for post_reply in post_replies:
                delete_post_reply_from_community(post_reply.id)
            if post_replies:
                flash(_('Comments by %(name)s have been deleted.', name=user.display_name()))

        # federate ban to post author instance
        task_selector('ban_from_community', user_id=user_id, mod_id=current_user.id, community_id=community.id,
                      expiry=form.ban_until.data, reason=form.reason.data)

        # Notify banned person
        if user.is_local():

            cache.delete_memoized(joined_communities, user.id)
            cache.delete_memoized(moderating_communities, user.id)
            targets_data = {'gen': '0', 'community_id': community.id}
            notify = Notification(title=shorten_string('You have been banned from ' + community.title),
                                  url='/notifications', user_id=user.id,
                                  author_id=1, notif_type=NOTIF_BAN,
                                  subtype='user_banned_from_community',
                                  targets=targets_data)
            db.session.add(notify)
            user.unread_notifications += 1
            db.session.commit()
        else:
            ...
            # todo: send chatmessage to remote user and federate it
        cache.delete_memoized(communities_banned_from, user.id)

        # Remove their notification subscription,  if any
        db.session.query(NotificationSubscription).filter(NotificationSubscription.entity_id == community.id,
                                                          NotificationSubscription.user_id == user.id,
                                                          NotificationSubscription.type == NOTIF_COMMUNITY).delete()

        add_to_modlog('ban_user', actor=current_user, target_user=user, community=community, link_text=user.display_name(), link=user.link())

        return redirect(community.local_url())
    else:
        return render_template('community/community_ban_user.html', title=_('Ban from community'), form=form,
                               community=community,
                               user=user,
                               inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                               )


@bp.route('/community/<int:community_id>/<int:user_id>/unban_user_community', methods=['GET', 'POST'])
@login_required
def community_unban_user(community_id: int, user_id: int):
    community = Community.query.get_or_404(community_id)
    user = User.query.get_or_404(user_id)
    existing_ban = CommunityBan.query.filter_by(community_id=community.id, user_id=user.id).first()
    if existing_ban:
        db.session.delete(existing_ban)
        db.session.commit()

    community_membership_record = CommunityMember.query.filter_by(community_id=community.id, user_id=user.id).first()
    if community_membership_record:
        community_membership_record.is_banned = False
        db.session.commit()

    flash(_('%(name)s has been unbanned.', name=user.display_name()))

    # federate ban to post author instance
    task_selector('unban_from_community', user_id=user_id, mod_id=current_user.id, community_id=community.id,
                  expiry=utcnow(), reason='Un-banned')

    # notify banned person
    if user.is_local():
        cache.delete_memoized(joined_communities, user.id)
        cache.delete_memoized(moderating_communities, user.id)
        targets_data = {'gen': '0', 'community_id': community.id}
        notify = Notification(title=shorten_string('You have been un-banned from ' + community.title),
                              url='/notifications', user_id=user.id,
                              author_id=1, notif_type=NOTIF_UNBAN,
                              subtype='user_unbanned_from_community',
                              targets=targets_data)
        db.session.add(notify)
        user.unread_notifications += 1
        db.session.commit()
    else:
        ...
        # todo: send chatmessage to remote user and federate it

    cache.delete_memoized(communities_banned_from, user.id)

    add_to_modlog('unban_user', actor=current_user, target_user=user, community=community, link_text=user.display_name(), link=user.link())

    return redirect(url_for('community.community_moderate_subscribers', actor=community.link()))


@bp.route('/<int:community_id>/notification', methods=['GET', 'POST'])
@login_required
def community_notification(community_id: int):
    try:
        return subscribe_community(community_id, None, SRC_WEB)
    except NoResultFound:
        abort(404)


@bp.route('/<actor>/move', methods=['GET', 'POST'])
@login_required
def community_move(actor):
    if current_user.banned:
        return show_ban_message()
    community = actor_to_community(actor)

    if community is not None and not community.is_local():
        form = MoveCommunityForm()
        if form.validate_on_submit():
            from flask import render_template as flask_render_template

            # Notify admin
            text_body = flask_render_template('email/move_community.txt', current_user=current_user,
                                              community=community,
                                              post_url=form.post_link.data,
                                              home_domain=current_app.config['SERVER_NAME'])
            html_body = flask_render_template('email/move_community.html', current_user=current_user,
                                              community=community,
                                              post_url=form.post_link.data,
                                              home_domain=current_app.config['SERVER_NAME'])
            send_email(f'Request to move {community.link()}', f'{current_app.config["MAIL_FROM"]}',
                       g.site.contact_email, text_body, html_body, current_user.email)

            targets_data = {'gen': '0', 'community_id': community.id, 'requestor_id': current_user.id}
            notify = Notification(title='Community move requested, check your email.',
                                  url=f'/admin/community/{community.id}/move/{current_user.id}', user_id=1,
                                  author_id=current_user.id, notif_type=NOTIF_MENTION,
                                  subtype='community_move_request',
                                  targets=targets_data)
            db.session.add(notify)
            db.session.execute(text('UPDATE "user" SET unread_notifications = unread_notifications + 1 WHERE id = 1'))
            db.session.commit()

            flash(_('Your request has been sent to the site admins.'))
        return render_template('community/community_move.html', community=community, form=form)
    else:
        abort(404)


@bp.route('/<actor>/moderate', methods=['GET'])
@login_required
def community_moderate(actor):
    if current_user.banned:
        return show_ban_message()
    community = actor_to_community(actor)

    if community is not None:
        if community.is_moderator() or current_user.is_admin():

            page = request.args.get('page', 1, type=int)
            local_remote = request.args.get('local_remote', '')

            reports = Report.query.filter_by(status=0, in_community_id=community.id)
            if local_remote == 'local':
                reports = reports.filter(Report.source_instance_id == 1)
            if local_remote == 'remote':
                reports = reports.filter(Report.source_instance_id != 1)
            reports = reports.filter(Report.status >= 0).order_by(desc(Report.created_at)).paginate(page=page,
                                                                                                    per_page=1000,
                                                                                                    error_out=False)

            next_url = url_for('community.community_moderate', page=reports.next_num) if reports.has_next else None
            prev_url = url_for('community.community_moderate',
                               page=reports.prev_num) if reports.has_prev and page != 1 else None

            return render_template('community/community_moderate.html',
                                   title=_('Moderation of %(community)s', community=community.display_name()),
                                   community=community, reports=reports, current='reports',
                                   next_url=next_url, prev_url=prev_url,
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None)
        else:
            abort(401)
    else:
        abort(404)


@bp.route('/<actor>/moderate/subscribers', methods=['GET', 'POST'])
@login_required
def community_moderate_subscribers(actor):
    community = actor_to_community(actor)
    ban_user_form = FindAndBanUserCommunityForm()

    if ban_user_form.submit.data and ban_user_form.validate():
        # find the user
        user_to_ban = find_actor_or_create(ban_user_form.user_name.data)

        if isinstance(user_to_ban, User):
            return redirect(url_for('community.community_ban_user', community_id=community.id, user_id=user_to_ban.id))
        else:
            flash(_(f'User: {ban_user_form.user_name.data} unable to be found'))
            return redirect(url_for('community.community_moderate_subscribers', actor=actor))
    elif community is not None:
        if community.is_moderator() or current_user.is_admin():

            page = request.args.get('page', 1, type=int)
            low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
            sort_by = request.args.get('sort_by', 'last_seen DESC')
            search = request.args.get('search', '')

            # Handle sort_by_btn redirects
            sort_by_btn = request.args.get('sort_by_btn', '')
            if sort_by_btn:
                return redirect(
                    url_for('community.community_moderate_subscribers', actor=actor, page=page, sort_by=sort_by_btn,
                            search=search))

            subscribers = db.session.query(User, CommunityMember.created_at).join(CommunityMember,
                                                                                  CommunityMember.user_id == User.id).filter(
                CommunityMember.community_id == community.id)
            subscribers = subscribers.filter(CommunityMember.is_banned == False)

            # Apply search filter
            if search:
                subscribers = subscribers.filter(User.user_name.ilike(f'%{search}%'))

            # Apply sorting
            if sort_by.startswith('joined'):
                if 'DESC' in sort_by:
                    subscribers = subscribers.order_by(desc(CommunityMember.created_at))
                else:
                    subscribers = subscribers.order_by(CommunityMember.created_at)
            elif sort_by.startswith('last_seen'):
                if 'DESC' in sort_by:
                    subscribers = subscribers.order_by(desc(User.last_seen))
                else:
                    subscribers = subscribers.order_by(User.last_seen)
            elif sort_by.startswith('local_remote'):
                if 'DESC' in sort_by:
                    subscribers = subscribers.order_by(desc(User.ap_id.is_(None)))
                else:
                    subscribers = subscribers.order_by(User.ap_id.is_(None))
            else:
                subscribers = subscribers.order_by(desc(User.last_seen))

            # Pagination
            subscribers = subscribers.paginate(page=page, per_page=100 if not low_bandwidth else 50, error_out=False)
            next_url = url_for('community.community_moderate_subscribers', actor=actor, page=subscribers.next_num,
                               sort_by=sort_by, search=search) if subscribers.has_next else None
            prev_url = url_for('community.community_moderate_subscribers', actor=actor, page=subscribers.prev_num,
                               sort_by=sort_by, search=search) if subscribers.has_prev and page != 1 else None

            banned_people = User.query.join(CommunityBan, CommunityBan.user_id == User.id).filter(
                CommunityBan.community_id == community.id).all()

            return render_template('community/community_moderate_subscribers.html',
                                   title=_('Moderation of %(community)s', community=community.display_name()),
                                   community=community, current='subscribers', subscribers=subscribers,
                                   banned_people=banned_people,
                                   ban_user_form=ban_user_form, sort_by=sort_by, search=search,
                                   next_url=next_url, prev_url=prev_url, low_bandwidth=low_bandwidth,
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None)
        else:
            abort(401)

    else:
        abort(404)


@bp.route('/<actor>/moderate/comments', methods=['GET'])
@login_required
def community_moderate_comments(actor):
    if current_user.banned:
        return show_ban_message()
    community = actor_to_community(actor)

    if community is not None:
        if community.is_moderator() or current_user.is_admin():
            replies_page = request.args.get('replies_page', 1, type=int)
            post_replies = PostReply.query.filter_by(community_id=community.id, deleted=False).order_by(
                desc(PostReply.posted_at)).paginate(page=replies_page, per_page=50, error_out=False)

            replies_next_url = url_for('community.community_moderate_comments', actor=community.link(),
                                       replies_page=post_replies.next_num) if post_replies.has_next else None
            replies_prev_url = url_for('community.community_moderate_comments', actor=community.link(),
                                       replies_page=post_replies.prev_num) if post_replies.has_prev and replies_page != 1 else None

            return render_template('community/community_moderate_comments.html', post_replies=post_replies,
                                   replies_next_url=replies_next_url, replies_prev_url=replies_prev_url,
                                   disable_voting=True, community=community, current='comments')


@bp.route('/community/<int:community_id>/<int:user_id>/kick_user_community', methods=['POST'])
@login_required
def community_kick_user(community_id: int, user_id: int):
    community = Community.query.get_or_404(community_id)
    user = User.query.get_or_404(user_id)

    if community is not None:
        if current_user.is_admin():

            db.session.query(CommunityMember).filter_by(user_id=user.id, community_id=community.id).delete()
            db.session.commit()

        else:
            abort(401)
    else:
        abort(404)

    return redirect(url_for('community.community_moderate_subscribers', actor=community.name))


@bp.route('/<actor>/moderate/wiki', methods=['GET'])
@login_required
def community_wiki_list(actor):
    community = actor_to_community(actor)

    if community is not None:
        if community.is_moderator() or current_user.is_admin():
            low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
            pages = CommunityWikiPage.query.filter(CommunityWikiPage.community_id == community.id).order_by(
                CommunityWikiPage.title).all()
            return render_template('community/community_wiki_list.html', title=_('Community Wiki'), community=community,
                                   pages=pages, low_bandwidth=low_bandwidth, current='wiki',
                                   )
        else:
            abort(401)
    else:
        abort(404)


@bp.route('/<actor>/moderate/wiki/add', methods=['GET', 'POST'])
@login_required
def community_wiki_add(actor):
    community = actor_to_community(actor)

    if community is not None:
        if community.is_moderator() or current_user.is_admin():
            low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
            form = EditCommunityWikiPageForm()
            if form.validate_on_submit():
                new_page = CommunityWikiPage(community_id=community.id, slug=form.slug.data, title=form.title.data,
                                             body=form.body.data, who_can_edit=form.who_can_edit.data)
                new_page.body_html = markdown_to_html(new_page.body)
                db.session.add(new_page)
                db.session.commit()

                initial_revision = CommunityWikiPageRevision(wiki_page_id=new_page.id, user_id=current_user.id,
                                                             community_id=community.id, title=form.title.data,
                                                             body=form.body.data, body_html=new_page.body_html)
                db.session.add(initial_revision)
                db.session.commit()

                flash(_('Saved'))
                return redirect(url_for('community.community_wiki_list', actor=community.link()))

            return render_template('community/community_wiki_edit.html', title=_('Add wiki page'), community=community,
                                   form=form, low_bandwidth=low_bandwidth, current='wiki',
                                   )
        else:
            abort(401)
    else:
        abort(404)


@bp.route('/<actor>/wiki/<slug>', methods=['GET', 'POST'])
def community_wiki_view(actor, slug):
    community = actor_to_community(actor)

    if community is not None:
        page: CommunityWikiPage = CommunityWikiPage.query.filter_by(slug=slug, community_id=community.id).first()
        if page is None:
            abort(404)
        else:
            # Breadcrumbs
            breadcrumbs = []
            breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
            breadcrumb.text = _('Home')
            breadcrumb.url = '/'
            breadcrumbs.append(breadcrumb)

            if community.topic_id:
                topics = []
                previous_topic = Topic.query.get(community.topic_id)
                topics.append(previous_topic)
                while previous_topic.parent_id:
                    topic = Topic.query.get(previous_topic.parent_id)
                    topics.append(topic)
                    previous_topic = topic
                topics = list(reversed(topics))

                breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
                breadcrumb.text = _('Topics')
                breadcrumb.url = '/topics'
                breadcrumbs.append(breadcrumb)

                existing_url = '/topic'
                for topic in topics:
                    breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
                    breadcrumb.text = topic.name
                    breadcrumb.url = f"{existing_url}/{topic.machine_name}"
                    breadcrumbs.append(breadcrumb)
                    existing_url = breadcrumb.url
            else:
                breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
                breadcrumb.text = _('Communities')
                breadcrumb.url = '/communities'
                breadcrumbs.append(breadcrumb)

            return render_template('community/community_wiki_page_view.html', title=page.title, page=page,
                                   community=community, breadcrumbs=breadcrumbs, is_moderator=community.is_moderator(),
                                   is_owner=community.is_owner(),
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None)


@bp.route('/<actor>/wiki/<slug>/<revision_id>', methods=['GET', 'POST'])
@login_required
def community_wiki_view_revision(actor, slug, revision_id):
    community = actor_to_community(actor)

    if community is not None:
        page: CommunityWikiPage = CommunityWikiPage.query.filter_by(slug=slug, community_id=community.id).first()
        revision: CommunityWikiPageRevision = CommunityWikiPageRevision.query.get_or_404(revision_id)
        if page is None or revision is None:
            abort(404)
        else:
            # Breadcrumbs
            breadcrumbs = []
            breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
            breadcrumb.text = _('Home')
            breadcrumb.url = '/'
            breadcrumbs.append(breadcrumb)

            if community.topic_id:
                topics = []
                previous_topic = Topic.query.get(community.topic_id)
                topics.append(previous_topic)
                while previous_topic.parent_id:
                    topic = Topic.query.get(previous_topic.parent_id)
                    topics.append(topic)
                    previous_topic = topic
                topics = list(reversed(topics))

                breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
                breadcrumb.text = _('Topics')
                breadcrumb.url = '/topics'
                breadcrumbs.append(breadcrumb)

                existing_url = '/topic'
                for topic in topics:
                    breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
                    breadcrumb.text = topic.name
                    breadcrumb.url = f"{existing_url}/{topic.machine_name}"
                    breadcrumbs.append(breadcrumb)
                    existing_url = breadcrumb.url
            else:
                breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
                breadcrumb.text = _('Communities')
                breadcrumb.url = '/communities'
                breadcrumbs.append(breadcrumb)

            return render_template('community/community_wiki_revision_view.html', title=page.title, page=page,
                                   community=community, breadcrumbs=breadcrumbs, is_moderator=community.is_moderator(),
                                   is_owner=community.is_owner(), revision=revision,
                                   )


@bp.route('/<actor>/wiki/<slug>/<revision_id>/revert', methods=['GET'])
@login_required
def community_wiki_revert_revision(actor, slug, revision_id):
    community = actor_to_community(actor)

    if community is not None:
        page: CommunityWikiPage = CommunityWikiPage.query.filter_by(slug=slug, community_id=community.id).first()
        revision: CommunityWikiPageRevision = CommunityWikiPageRevision.query.get_or_404(revision_id)
        if page is None or revision is None:
            abort(404)
        else:
            if page.can_edit(current_user, community):
                page.body = revision.body
                page.body_html = revision.body_html
                page.edited_at = utcnow()

                new_revision = CommunityWikiPageRevision(wiki_page_id=page.id, user_id=current_user.id,
                                                         community_id=community.id, title=revision.title,
                                                         body=revision.body, body_html=revision.body_html)
                db.session.add(new_revision)
                db.session.commit()

                flash(_('Reverted to old version of the page.'))
                return redirect(url_for('community.community_wiki_revisions', actor=community.link(), page_id=page.id))
            else:
                abort(401)


@bp.route('/<actor>/moderate/wiki/<int:page_id>/edit', methods=['GET', 'POST'])
@login_required
def community_wiki_edit(actor, page_id):
    community = actor_to_community(actor)

    if community is not None:
        page: CommunityWikiPage = CommunityWikiPage.query.get_or_404(page_id)
        if page.can_edit(current_user, community):
            low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

            form = EditCommunityWikiPageForm()
            if form.validate_on_submit():
                page.title = form.title.data
                page.slug = form.slug.data
                page.body = form.body.data
                page.body_html = markdown_to_html(page.body)
                page.who_can_edit = form.who_can_edit.data
                page.edited_at = utcnow()
                new_revision = CommunityWikiPageRevision(wiki_page_id=page.id, user_id=current_user.id,
                                                         community_id=community.id, title=form.title.data,
                                                         body=form.body.data, body_html=page.body_html)
                db.session.add(new_revision)
                db.session.commit()
                flash(_('Saved'))
                if request.args.get('return') == 'list':
                    return redirect(url_for('community.community_wiki_list', actor=community.link()))
                elif request.args.get('return') == 'page':
                    return redirect(url_for('community.community_wiki_view', actor=community.link(), slug=page.slug))
            else:
                form.title.data = page.title
                form.slug.data = page.slug
                form.body.data = page.body
                form.who_can_edit.data = page.who_can_edit

            return render_template('community/community_wiki_edit.html', title=_('Edit wiki page'), community=community,
                                   form=form, low_bandwidth=low_bandwidth,
                                   )
        else:
            abort(401)
    else:
        abort(404)


@bp.route('/<actor>/moderate/wiki/<int:page_id>/revisions', methods=['GET', 'POST'])
@login_required
def community_wiki_revisions(actor, page_id):
    community = actor_to_community(actor)

    if community is not None:
        page: CommunityWikiPage = CommunityWikiPage.query.get_or_404(page_id)
        if page.can_edit(current_user, community):
            low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

            revisions = CommunityWikiPageRevision.query.filter_by(wiki_page_id=page.id). \
                order_by(desc(CommunityWikiPageRevision.edited_at)).all()

            most_recent_revision = revisions[0].id

            return render_template('community/community_wiki_revisions.html',
                                   title=_('%(title)s revisions', title=page.title),
                                   community=community, page=page, revisions=revisions,
                                   most_recent_revision=most_recent_revision,
                                   low_bandwidth=low_bandwidth,
                                   )
        else:
            abort(401)
    else:
        abort(404)


@bp.route('/<actor>/moderate/wiki/<int:page_id>/delete', methods=['POST'])
@login_required
def community_wiki_delete(actor, page_id):
    community = actor_to_community(actor)

    if community is not None:
        page: CommunityWikiPage = CommunityWikiPage.query.get_or_404(page_id)
        if page.can_edit(current_user, community):
            db.session.delete(page)
            db.session.commit()
            flash(_('Page deleted'))
        return redirect(url_for('community.community_wiki_list', actor=community.link()))
    else:
        abort(404)


@bp.route('/<actor>/moderate/modlog', methods=['GET'])
@login_required
def community_modlog(actor):
    community = actor_to_community(actor)

    if community is not None:
        if community.is_moderator() or current_user.is_admin():

            page = request.args.get('page', 1, type=int)
            low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

            modlog_entries = ModLog.query.filter(ModLog.community_id == community.id).order_by(desc(ModLog.created_at))

            # Pagination
            modlog_entries = modlog_entries.paginate(page=page, per_page=100 if not low_bandwidth else 50,
                                                     error_out=False)
            next_url = url_for('community.community_modlog', actor=actor,
                               page=modlog_entries.next_num) if modlog_entries.has_next else None
            prev_url = url_for('community.community_modlog', actor=actor,
                               page=modlog_entries.prev_num) if modlog_entries.has_prev and page != 1 else None

            return render_template('community/community_modlog.html',
                                   title=_('Mod Log of %(community)s', community=community.display_name()),
                                   community=community, current='modlog', modlog_entries=modlog_entries,
                                   next_url=next_url, prev_url=prev_url, low_bandwidth=low_bandwidth,
                                   )

        else:
            abort(401)
    else:
        abort(404)


@bp.route('/community/<int:community_id>/moderate_report/<int:report_id>/escalate', methods=['GET', 'POST'])
@login_required
def community_moderate_report_escalate(community_id, report_id):
    community = Community.query.get_or_404(community_id)
    if community.is_moderator() or current_user.is_admin():
        report = Report.query.filter_by(in_community_id=community.id, id=report_id, status=REPORT_STATE_NEW).first()
        if report:
            form = EscalateReportForm()
            if form.validate_on_submit():
                targets_data = {'gen': '0', 'community_id': community.id, 'report_id': report_id}
                notify = Notification(title='Escalated report', url='/admin/reports', user_id=1,
                                      author_id=current_user.id, notif_type=NOTIF_REPORT_ESCALATION,
                                      subtype='report_escalation_from_community_mod',
                                      targets=targets_data)
                db.session.add(notify)
                report.description = form.reason.data
                report.status = REPORT_STATE_ESCALATED
                db.session.commit()
                flash(_('Admin has been notified about this report.'))
                # todo: remove unread notifications about this report
                # todo: append to mod log
                return redirect(url_for('community.community_moderate', actor=community.link()))
            else:
                form.reason.data = report.description
                return render_template('community/community_moderate_report_escalate.html', form=form)
    else:
        abort(401)


@bp.route('/community/<int:community_id>/moderate_report/<int:report_id>/resolve', methods=['GET', 'POST'])
@login_required
def community_moderate_report_resolve(community_id, report_id):
    community = Community.query.get_or_404(community_id)
    if community.is_moderator() or current_user.is_admin():
        report = Report.query.filter_by(in_community_id=community.id, id=report_id).first()
        if report:
            form = ResolveReportForm()
            if form.validate_on_submit():
                report.status = REPORT_STATE_RESOLVED

                # Reset the 'reports' counter on the comment, post or user
                if report.suspect_post_reply_id:
                    post_reply = PostReply.query.get(report.suspect_post_reply_id)
                    post_reply.reports = 0
                elif report.suspect_post_id:
                    post = Post.query.get(report.suspect_post_id)
                    post.reports = 0
                elif report.suspect_user_id:
                    user = User.query.get(report.suspect_user_id)
                    user.reports = 0
                db.session.commit()

                # todo: remove unread notifications about this report
                # todo: append to mod log
                if form.also_resolve_others.data:
                    if report.suspect_post_reply_id:
                        db.session.execute(text(
                            'UPDATE "report" SET status = :new_status WHERE suspect_post_reply_id = :suspect_post_reply_id'),
                            {'new_status': REPORT_STATE_RESOLVED,
                             'suspect_post_reply_id': report.suspect_post_reply_id})
                        # todo: remove unread notifications about these reports
                    elif report.suspect_post_id:
                        db.session.execute(
                            text('UPDATE "report" SET status = :new_status WHERE suspect_post_id = :suspect_post_id'),
                            {'new_status': REPORT_STATE_RESOLVED,
                             'suspect_post_id': report.suspect_post_id})
                        # todo: remove unread notifications about these reports
                    db.session.commit()
                flash(_('Report resolved.'))
                return redirect(url_for('community.community_moderate', actor=community.link()))
            else:
                return render_template('community/community_moderate_report_resolve.html', form=form)


@bp.route('/community/<int:community_id>/moderate_report/<int:report_id>/ignore', methods=['GET', 'POST'])
@login_required
def community_moderate_report_ignore(community_id, report_id):
    community = Community.query.get_or_404(community_id)
    if community.is_moderator() or current_user.is_admin():
        report = Report.query.filter_by(in_community_id=community.id, id=report_id).first()
        if report:
            # Set the 'reports' counter on the comment, post or user to -1 to ignore all future reports
            if report.suspect_post_reply_id:
                post_reply = PostReply.query.get(report.suspect_post_reply_id)
                post_reply.reports = -1
            elif report.suspect_post_id:
                post = Post.query.get(report.suspect_post_id)
                post.reports = -1
            elif report.suspect_user_id:
                user = User.query.get(report.suspect_user_id)
                user.reports = -1
            db.session.commit()

            # todo: append to mod log

            if report.suspect_post_reply_id:
                db.session.execute(text(
                    'UPDATE "report" SET status = :new_status WHERE suspect_post_reply_id = :suspect_post_reply_id'),
                    {'new_status': REPORT_STATE_DISCARDED,
                     'suspect_post_reply_id': report.suspect_post_reply_id})
                # todo: remove unread notifications about these reports
            elif report.suspect_post_id:
                db.session.execute(
                    text('UPDATE "report" SET status = :new_status WHERE suspect_post_id = :suspect_post_id'),
                    {'new_status': REPORT_STATE_DISCARDED,
                     'suspect_post_id': report.suspect_post_id})
                # todo: remove unread notifications about these reports
            db.session.commit()
            flash(_('Report ignored.'))
            return redirect(url_for('community.community_moderate', actor=community.link()))
        else:
            abort(404)


@bp.route('/<actor>/my_flair', methods=['GET', 'POST'])
@login_required
def community_my_flair(actor):
    community = actor_to_community(actor)

    if community is not None:
        form = SetMyFlairForm()
        existing_flair = UserFlair.query.filter(UserFlair.community_id == community.id,
                                                UserFlair.user_id == current_user.id).first()
        if form.validate_on_submit():
            if existing_flair:
                if form.my_flair.data.strip() == '':
                    db.session.delete(existing_flair)
                else:
                    existing_flair.flair = form.my_flair.data
            else:
                db.session.add(UserFlair(community_id=community.id, user_id=current_user.id, flair=form.my_flair.data))
            db.session.commit()
            flash(_('Saved'))
            return redirect(url_for('activitypub.community_profile', actor=community.link()))
        else:
            if existing_flair:
                form.my_flair.data = existing_flair.flair
            return render_template('generic_form.html', title=_('Set your flair in %(community_name)s',
                                                                community_name=community.display_name()),
                                   form=form)


@bp.route('/<actor>/moderate/flair', methods=['GET'])
@login_required
def community_flair(actor):
    community = actor_to_community(actor)

    if community is not None:
        if community.is_moderator() or current_user.is_admin():

            low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

            flairs = CommunityFlair.query.filter(CommunityFlair.community_id == community.id).order_by(CommunityFlair.flair)

            return render_template('community/community_flair.html', flairs=flairs,
                                   title=_('Flair in %(community)s', community=community.display_name()),
                                   community=community, current='flair', low_bandwidth=low_bandwidth,
                                   )
        else:
            abort(401)
    else:
        abort(404)


@bp.route('/community/<int:community_id>/flair/<int:flair_id>', methods=['GET', 'POST'])
@login_required
def community_flair_edit(community_id, flair_id):
    community = Community.query.get_or_404(community_id)

    if community.is_moderator() or current_user.is_admin():
        flair = CommunityFlair.query.get(flair_id) if flair_id else None
        form = EditCommunityFlairForm()
        if form.validate_on_submit():
            if flair is None:
                flair = CommunityFlair(community_id=community.id)
                db.session.add(flair)
                flash(_('Flair added.'))
            else:
                flash(_('Flair updated.'))
            flair.flair = form.flair.data
            flair.text_color = form.text_color.data
            flair.background_color = form.background_color.data
            flair.blur_images = form.blur_images.data
            db.session.commit()

            return redirect(url_for('community.community_flair', actor=community.link()))
        else:
            form.flair.data = flair.flair if flair else ''
            form.text_color.data = flair.text_color if flair else '#000000'
            form.background_color.data = flair.background_color if flair else '#deddda'
            form.blur_images.data = flair.blur_images if flair else False
            return render_template('generic_form.html', form=form, flair=flair,
                                   title=_('Edit %(flair_name)s in %(community_name)s', flair_name=flair.flair,
                                           community_name=community.display_name()) if flair else _(
                                       'Add flair in %(community_name)s', community_name=community.display_name()),
                                   community=community)
    else:
        abort(401)


@bp.route('/community/<int:community_id>/flair/<int:flair_id>/delete', methods=['POST'])
@login_required
def community_flair_delete(community_id, flair_id):
    community = Community.query.get_or_404(community_id)

    if community.is_moderator() or current_user.is_admin():
        db.session.execute(text('DELETE FROM "post_flair" WHERE flair_id = :flair_id'), {'flair_id': flair_id})
        db.session.query(CommunityFlair).filter(CommunityFlair.id == flair_id).delete()
        db.session.commit()
        flash(_('Flair deleted.'))
        return redirect(url_for('community.community_flair', actor=community.link()))
    else:
        abort(401)


@bp.route('/community/leave_all')
@login_required
def community_leave_all():
    all_communities = Community.query.filter_by(banned=False)
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

    for community in communities.all():
        subscription = community_membership(current_user, community)
        if subscription:
            if subscription != SUBSCRIPTION_OWNER:
                # Undo the Follow
                if not community.is_local():
                    if not community.instance.gone_forever:
                        follow_id = f"https://{current_app.config['SERVER_NAME']}/activities/follow/{gibberish(15)}"
                        if community.instance.domain == 'a.gup.pe':
                            join_request = CommunityJoinRequest.query.filter_by(user_id=current_user.id,
                                                                                community_id=community.id).first()
                            if join_request:
                                follow_id = f"https://{current_app.config['SERVER_NAME']}/activities/follow/{join_request.uuid}"
                        undo_id = f"https://{current_app.config['SERVER_NAME']}/activities/undo/" + gibberish(15)
                        follow = {
                            "actor": current_user.public_url(),
                            "to": [community.public_url()],
                            "object": community.public_url(),
                            "type": "Follow",
                            "id": follow_id
                        }
                        undo = {
                            'actor': current_user.public_url(),
                            'to': [community.public_url()],
                            'type': 'Undo',
                            'id': undo_id,
                            'object': follow
                        }
                        send_post_request(community.ap_inbox_url, undo, current_user.private_key,
                                          current_user.public_url() + '#main-key', timeout=10)

                db.session.query(CommunityMember).filter_by(user_id=current_user.id, community_id=community.id).delete()
                db.session.query(CommunityJoinRequest).filter_by(user_id=current_user.id,
                                                                 community_id=community.id).delete()
                cache.delete_memoized(community_membership, current_user, community)
                db.session.commit()

    return redirect(url_for('main.list_communities'))


@bp.route('/<actor>/invite', methods=['GET', 'POST'])
@limiter.limit("5 per 1 minutes", methods=['POST'])
@login_required
def community_invite(actor):
    if current_user.banned:
        return show_ban_message()
    form = InviteCommunityForm()

    community = actor_to_community(actor)

    if current_user.created_very_recently() and not current_user.is_admin():
        flash(_('Sorry your account is too new to do this.'), 'warning')
        return redirect(referrer())

    if community is not None:
        if form.validate_on_submit():
            chat_invites = 0
            email_invites = 0
            total_invites = 0
            sent_to = set()
            for line in form.to.data.split('\n'):
                line = line.strip()
                if line != '':
                    if line.startswith('http'):
                        chat_invites += invite_with_chat(community.id, line, SRC_WEB)
                    elif '@' in line:
                        if line.startswith('@') or instance_software(domain_from_email(line)):
                            chat_invites += invite_with_chat(community.id, line, SRC_WEB)
                        else:
                            if line not in sent_to:
                                email_invites += invite_with_email(community.id, line, SRC_WEB)
                                sent_to.add(line)
                        total_invites += 1

            flash(_('Invited %(total_invites)d people using %(chat_invites)d chat messages and %(email_invites)d emails.',
                  total_invites=total_invites, chat_invites=chat_invites, email_invites=email_invites))
            return redirect('/c/' + community.link())

        return render_template('community/invite.html', title=_('Invite to community'), form=form, community=community,
                               current_app=current_app,
                               )
    else:
        abort(404)


@bp.route('/lookup/<community>/<domain>')
def lookup(community, domain):
    if domain == current_app.config['SERVER_NAME']:
        return redirect('/c/' + community)

    community = community.lower()
    domain = domain.lower()

    exists = Community.query.filter_by(ap_id=f'{community}@{domain}').first()
    if exists:
        return redirect('/c/' + community + '@' + domain)
    else:
        address = '!' + community + '@' + domain
        if current_user.is_authenticated:
            new_community = None

            try:
                new_community = search_for_community(address)
            except Exception as e:
                if 'is blocked.' in str(e):
                    flash(_('Sorry, that instance is blocked, check https://gui.fediseer.com/ for reasons.'), 'warning')
            if new_community is None:
                if g.site.enable_nsfw:
                    flash(_('Community not found.'), 'warning')
                else:
                    flash(
                        _('Community not found. If you are searching for a nsfw community it is blocked by this instance.'),
                        'warning')
            else:
                if new_community.banned:
                    flash(_('That community is banned from %(site)s.', site=g.site.name), 'warning')

            return render_template('community/lookup_remote.html',
                                   title=_('Search result for remote community'), new_community=new_community,
                                   subscribed=community_membership(current_user, new_community) >= SUBSCRIPTION_MEMBER)
        else:
            # send them back where they came from
            flash(_('Searching for remote communities requires login'), 'error')
            referrer = request.headers.get('Referer', None)
            if referrer is not None:
                return redirect(referrer)
            else:
                return redirect('/')


@bp.route('/check_url_already_posted')
def check_url_already_posted():
    url = request.args.get('link_url')
    if url:
        url = remove_tracking_from_link(url.strip())
        communities = Community.query.filter_by(banned=False).join(Post).filter(Post.url == url, Post.deleted == False,
                                                                                Post.status > POST_STATUS_REVIEWING).all()
        return flask.render_template('community/check_url_posted.html', communities=communities,
                                     title=retrieve_title_of_url(url))
    else:
        abort(404)


@bp.route('/community_changed')
def community_changed():
    community_id = request.args.get('communities')
    if community_id:
        community = Community.query.get(community_id)
        return flask.render_template('community/community_changed.html', community=community)
    else:
        return ''


@bp.route('/get_sidebar/<int:community_id>')
def get_sidebar(community_id):
    community = Community.query.get(community_id)
    return flask.render_template('community/description.html', community=community, hide_community_actions=True)


def retrieve_title_of_url(url):
    try:
        response = httpx_client.get(url, timeout=10, follow_redirects=True)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')

            # Try og:title first
            og_title = soup.find('meta', property='og:title')
            if og_title and og_title.get('content'):
                return og_title.get('content').strip()

            # Fall back to HTML title
            title_tag = soup.find('title')
            if title_tag:
                return title_tag.get_text().strip()

            return ""
        else:
            return ""
    except Exception:
        return ""
