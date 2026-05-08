# ----- imports -----
from collections import namedtuple
from datetime import timezone
from random import randint
from typing import List

from feedgen.feed import FeedGenerator
from flask import g, current_app, request, redirect, url_for, flash, abort, make_response
from markupsafe import Markup
from flask_babel import _
from flask_login import current_user
from slugify import slugify
from sqlalchemy import desc, or_

from app import db, cache, celery
from app.activitypub.signature import RsaKeys, default_context, send_post_request
from app.activitypub.util import find_actor_or_create, extract_domain_and_actor
from app.community.util import save_icon_file, save_banner_file, hashtags_used_in_communities
from app.constants import SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR, POST_TYPE_IMAGE, \
    POST_TYPE_LINK, POST_TYPE_VIDEO, NOTIF_FEED, SUBSCRIPTION_MEMBER, SUBSCRIPTION_NONMEMBER, SRC_WEB
from app.feed import bp
from app.feed.forms import AddCopyFeedForm, EditFeedForm, SearchRemoteFeed
from app.feed.util import feeds_for_form, search_for_feed, actor_to_feed, feed_communities_for_edit
from app.inoculation import inoculation
from app.models import Feed, FeedMember, FeedItem, Community, NotificationSubscription, \
    CommunityMember, User, FeedJoinRequest, Instance, Topic, CommunityJoinRequest
from app.shared.feed import join_feed, _feed_add_community, announce_feed_delete_to_subscribers, edit_feed, \
    form_communities_to_ids, make_feed, delete_feed
from app.utils import show_ban_message, piefed_markdown_to_lemmy_markdown, markdown_to_html, render_template, \
    user_filters_posts, joined_communities, menu_instance_feeds, validation_required, feed_membership, \
    gibberish, get_task_session, instance_banned, menu_subscribed_feeds, referrer, community_membership, \
    paginate_post_ids, get_deduped_post_ids, get_request, post_ids_to_models, recently_upvoted_posts, \
    recently_downvoted_posts, joined_or_modding_communities, login_required_if_private_instance, \
    communities_banned_from, reported_posts, user_notes, login_required, moderating_communities_ids, approval_required, \
    blocked_or_banned_instances, blocked_communities, block_honey_pot, user_pronouns, mimetype_from_url, \
    community_membership_private


@bp.route('/feed/new', methods=['GET', 'POST'])
@login_required
def feed_new():
    if current_user.banned:
        return show_ban_message()

    form = AddCopyFeedForm()
    if g.site.enable_nsfw is False:
        form.nsfw.render_kw = {'disabled': True}
    if g.site.enable_nsfl is False:
        form.nsfl.render_kw = {'disabled': True}
    if not current_user.is_admin():
        form.is_instance_feed.render_kw = {'disabled': True}
    form.parent_feed_id.choices = feeds_for_form(0, current_user.id)

    if form.validate_on_submit():
        if form.url.data.strip().lower().startswith('/f/'):
            form.url.data = form.url.data[3:]
        form.url.data = slugify(form.url.data.strip().split('/')[0], separator='_').lower()
        if not form.public.data:
            form.url.data = slugify(form.url.data.strip(), separator='_').lower() + '/' + current_user.user_name.lower()

        make_feed(form, SRC_WEB, None, form.icon_file.data, form.banner_file.data)

        flash(_('Your new Feed has been created.'))
        return redirect(url_for('user.user_myfeeds', actor=current_user.link()))

    # Create Feed from a topic
    if request.args.get('topic_id'):
        topic = Topic.query.get(request.args.get('topic_id'))
        community_apids = []
        for community in topic.communities:
            community_apids.append(community.lemmy_link().replace('!', ''))
        form.title.data = topic.name
        form.url.data = topic.machine_name
        form.communities.data = '\n'.join(community_apids)

    return render_template('feed/feed_new.html', title=_('Create a Feed'), form=form,
                           current_app=current_app, )


@bp.route('/feed/add_remote', methods=['GET', 'POST'])
@login_required
def feed_add_remote():
    if current_user.banned:
        return show_ban_message()
    form = SearchRemoteFeed()
    new_feed = None
    if form.validate_on_submit():
        address = form.address.data.strip().lower()

        if address.startswith('~') and '@' in address:
            try:
                new_feed = search_for_feed(address)
            except Exception as e:
                if 'is blocked.' in str(e):
                    flash(_('Sorry, that instance is blocked, check https://gui.fediseer.com/ for reasons.'), 'warning')
        elif address.startswith('@') and '@' in address[1:]:
            # todo: the user is searching for a person instead
            ...
        elif '@' in address:
            new_feed = search_for_feed('~' + address)
        elif address.startswith('https://') or address.startswith('http://'):
            server, feed = extract_domain_and_actor(address)
            new_feed = search_for_feed('~' + feed + '@' + server)
        else:
            message = Markup(
                _('Accepted address formats: ~feedname@server.name or https://server.name/f/feedname.'))
            flash(message, 'error')
        if new_feed is None:
            if g.site.enable_nsfw:
                flash(_('Feed not found.'), 'warning')
            else:
                flash(_('Feed not found. If you are searching for a nsfw feed it is blocked by this instance.'),
                      'warning')
        else:
            cache.delete_memoized(feed_membership, current_user, new_feed)

    return render_template('feed/add_remote.html',
                           title=_('Add remote feed'), form=form, new_feed=new_feed,
                           subscribed=feed_membership(current_user, new_feed) >= SUBSCRIPTION_MEMBER,
                           )


@bp.route('/feed/<int:feed_id>/edit', methods=['GET', 'POST'])
@login_required
def feed_edit(feed_id: int):
    url_changed = False
    old_url = None
    if current_user.banned:
        return show_ban_message()
    # load the feed
    feed_to_edit: Feed = Feed.query.get_or_404(feed_id)
    # make sure the user owns this feed
    if feed_to_edit.user_id != current_user.id:
        abort(404)
    edit_feed_form = EditFeedForm()
    edit_feed_form.parent_feed_id.choices = feeds_for_form(feed_id, current_user.id)
    edit_feed_form.feed_id = feed_id

    if not current_user.is_admin():
        edit_feed_form.is_instance_feed.render_kw = {'disabled': True}

    if feed_to_edit.subscriptions_count > 1:
        edit_feed_form.url.render_kw = {'disabled': True}

    if edit_feed_form.validate_on_submit():
        if edit_feed_form.url.data:
            edit_feed_form.url.data = slugify(edit_feed_form.url.data.strip().split('/')[0], separator='_').lower()
            if not edit_feed_form.public.data:
                edit_feed_form.url.data = slugify(edit_feed_form.url.data.strip(),
                                                  separator='_').lower() + '/' + current_user.user_name.lower()
            old_url = feed_to_edit.name
            url_changed = feed_to_edit.name != edit_feed_form.url.data
            feed_to_edit.name = edit_feed_form.url.data
            feed_to_edit.machine_name = edit_feed_form.url.data

        edit_feed(edit_feed_form, feed_to_edit, SRC_WEB, None, edit_feed_form.icon_file.data, edit_feed_form.banner_file.data, from_scratch=False)

        flash(_('Settings saved.'))
        if url_changed and old_url is not None:
            if referrer().endswith(old_url):
                return redirect('/f/' + feed_to_edit.name)
            else:
                return redirect(referrer())
        else:
            return redirect(referrer())

    # add the current data to the form
    edit_feed_form.title.data = feed_to_edit.title
    edit_feed_form.url.data = feed_to_edit.name
    edit_feed_form.description.data = feed_to_edit.description
    edit_feed_form.communities.data = feed_communities_for_edit(feed_to_edit.id)
    edit_feed_form.show_child_posts.data = feed_to_edit.show_posts_in_children
    edit_feed_form.parent_feed_id.data = feed_to_edit.parent_feed_id
    if g.site.enable_nsfw is False:
        edit_feed_form.nsfw.render_kw = {'disabled': True}
    else:
        edit_feed_form.nsfw.data = feed_to_edit.nsfw
    if g.site.enable_nsfl is False:
        edit_feed_form.nsfl.render_kw = {'disabled': True}
    else:
        edit_feed_form.nsfw.data = feed_to_edit.nsfw
    edit_feed_form.public.data = feed_to_edit.public
    edit_feed_form.is_instance_feed.data = feed_to_edit.is_instance_feed

    return render_template('feed/feed_edit.html', form=edit_feed_form, )


@bp.route('/feed/<int:feed_id>/delete', methods=['POST'])
@login_required
def feed_delete(feed_id: int):

    feed = Feed.query.get_or_404(feed_id)

    delete_feed(feed_id, SRC_WEB)

    flash(_('Feed deleted'))

    # clear instance feeds for dropdown menu cache
    if feed.is_instance_feed:
        cache.delete_memoized(menu_instance_feeds)

    # send the user back to the page they came from or main
    # Get the referrer from the request headers
    referrer = request.referrer

    # If the referrer exists and is not the same as the current request URL, redirect to the referrer
    if referrer and referrer != request.url:
        return redirect(referrer)

    # If referrer is not available or is the same as the current request URL, redirect to the default URL
    return redirect(url_for('main.index'))


@bp.route('/feed/<int:feed_id>/copy', methods=['GET', 'POST'])
@login_required
def feed_copy(feed_id: int):
    if current_user.banned:
        return show_ban_message()
    # load the feed
    feed_to_copy = Feed.query.get_or_404(feed_id)
    copy_feed_form = AddCopyFeedForm()
    copy_feed_form.parent_feed_id.choices = feeds_for_form(0, current_user.id)

    if not current_user.is_admin():
        copy_feed_form.is_instance_feed.render_kw = {'disabled': True}

    if copy_feed_form.validate_on_submit():
        if copy_feed_form.url.data.strip().lower().startswith('/f/'):
            copy_feed_form.url.data = copy_feed_form.url.data[3:]
        if copy_feed_form.public.data:
            copy_feed_form.url.data = slugify(copy_feed_form.url.data, separator='_').lower()
        else:
            copy_feed_form.url.data = slugify(copy_feed_form.url.data.strip(),
                                              separator='_').lower() + '/' + current_user.user_name.lower()
        private_key, public_key = RsaKeys.generate_keypair()
        feed = Feed(user_id=current_user.id, title=copy_feed_form.title.data, name=copy_feed_form.url.data,
                    machine_name=copy_feed_form.url.data,
                    description=piefed_markdown_to_lemmy_markdown(copy_feed_form.description.data),
                    description_html=markdown_to_html(copy_feed_form.description.data),
                    show_posts_in_children=copy_feed_form.show_child_posts.data,
                    nsfw=copy_feed_form.nsfw.data, nsfl=copy_feed_form.nsfl.data,
                    private_key=private_key,
                    public_key=public_key,
                    public=copy_feed_form.public.data, is_instance_feed=copy_feed_form.is_instance_feed.data,
                    ap_profile_id='https://' + current_app.config[
                        'SERVER_NAME'] + '/f/' + copy_feed_form.url.data.lower(),
                    ap_public_url='https://' + current_app.config['SERVER_NAME'] + '/f/' + copy_feed_form.url.data,
                    ap_followers_url='https://' + current_app.config[
                        'SERVER_NAME'] + '/f/' + copy_feed_form.url.data + '/followers',
                    ap_following_url='https://' + current_app.config[
                        'SERVER_NAME'] + '/f/' + copy_feed_form.url.data + '/following',
                    ap_domain=current_app.config['SERVER_NAME'],
                    subscriptions_count=1, instance_id=1)
        if copy_feed_form.parent_feed_id.data:
            feed.parent_feed_id = copy_feed_form.parent_feed_id.data
        else:
            feed.parent_feed_id = None
        icon_file = request.files['icon_file']
        if icon_file and icon_file.filename != '':
            file = save_icon_file(icon_file, directory='feeds')
            if file:
                feed.icon = file
        banner_file = request.files['banner_file']
        if banner_file and banner_file.filename != '':
            file = save_banner_file(banner_file, directory='feeds')
            if file:
                feed.image = file
        db.session.add(feed)
        db.session.commit()

        # get the FeedItems from the feed being copied and 
        # make sure they all come over to the new Feed
        old_feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == feed_to_copy.id).all()
        for item in old_feed_items:
            fi = FeedItem(feed_id=feed.id, community_id=item.community_id)
            db.session.add(fi)
            db.session.commit()

        # also subscribe the user to any community they are not already subscribed to
        member_of_ids = []
        member_of = CommunityMember.query.filter_by(user_id=current_user.id).all()
        for cm in member_of:
            member_of_ids.append(cm.community_id)
        for item in old_feed_items:
            if item.community_id not in member_of_ids and current_user.feed_auto_follow:
                from app.community.routes import do_subscribe
                community = Community.query.get(item.community_id)
                actor = community.ap_id if community.ap_id else community.name
                do_subscribe(actor, current_user.id, joined_via_feed=True)

        feed.num_communities = len(old_feed_items)
        db.session.add(feed)
        db.session.commit()

        membership = FeedMember(user_id=current_user.id, feed_id=feed.id, is_owner=True)
        db.session.add(membership)
        db.session.commit()

        flash(_('Your new Feed has been created.'))
        return redirect(url_for('main.index'))

        # add the current data to the form
    copy_feed_form.title.data = feed_to_copy.title
    copy_feed_form.url.data = feed_to_copy.name
    copy_feed_form.description.data = feed_to_copy.description
    copy_feed_form.communities.data = feed_communities_for_edit(feed_to_copy.id)
    copy_feed_form.show_child_posts.data = feed_to_copy.show_posts_in_children
    if g.site.enable_nsfw is False:
        copy_feed_form.nsfw.render_kw = {'disabled': True}
    else:
        copy_feed_form.nsfw.data = feed_to_copy.nsfw
    if g.site.enable_nsfl is False:
        copy_feed_form.nsfl.render_kw = {'disabled': True}
    else:
        copy_feed_form.nsfw.data = feed_to_copy.nsfw
    copy_feed_form.public.data = feed_to_copy.public
    copy_feed_form.is_instance_feed.data = feed_to_copy.is_instance_feed

    return render_template('feed/feed_copy.html', form=copy_feed_form)


@bp.route('/feed/<int:feed_id>/notification', methods=['GET', 'POST'])
@login_required
def feed_notification(feed_id: int):
    # Toggle whether the current user is subscribed to notifications about this feed's posts or not
    feed = Feed.query.get_or_404(feed_id)
    existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == feed.id,
                                                                  NotificationSubscription.user_id == current_user.id,
                                                                  NotificationSubscription.type == NOTIF_FEED).first()
    if existing_notification:
        db.session.delete(existing_notification)
        db.session.commit()
    else:  # no subscription yet, so make one
        new_notification = NotificationSubscription(name=feed.name, user_id=current_user.id, entity_id=feed.id,
                                                    type=NOTIF_FEED)
        db.session.add(new_notification)
        db.session.commit()

    return render_template('feed/_notification_toggle.html', feed=feed)


@bp.route('/feed/add_community', methods=['GET'])
@login_required
def feed_add_community():
    # this expects a user_id, a new_feed_id, a current_feed_id,
    # and a community_id
    # it will get those and then add a community to 
    # a feed using the FeedItem model
    user_id = int(request.args.get('user_id'))
    feed_id = int(request.args.get('new_feed_id'))
    current_feed_id = int(request.args.get('current_feed_id'))
    community_id = int(request.args.get('community_id'))

    # make sure the user owns this feed
    if Feed.query.get(feed_id).user_id != user_id:
        abort(404)

    _feed_add_community(community_id, current_feed_id, feed_id, user_id)

    # send the user back to the page they came from or main
    # Get the referrer from the request headers
    referrer = request.referrer

    # If the referrer exists and is not the same as the current request URL, redirect to the referrer
    if referrer and referrer != request.url:
        return redirect(referrer)

    # If referrer is not available or is the same as the current request URL, redirect to the default URL
    return redirect(url_for('main.index'))


@bp.route('/feed/list', methods=['GET'])
@login_required
def feed_list():
    # this takes a user id, community id, and current_feed id, 
    # and returns a set of html entries of the users feeds 

    # get the user id
    user_id = int(request.args.get('user_id'))
    # get the community id
    community_id = int(request.args.get('community_id'))
    # get the current_feed
    current_feed_id = int(request.args.get('current_feed_id'))
    # get the user's feeds
    user_feeds = Feed.query.filter_by(user_id=user_id).all()

    # setup html base to send back
    options_html = ""

    # add the none option if already in a feed
    if current_feed_id != 0:
        options_html = options_html + f'<li><a class="dropdown-item" href="/feed/remove_community?user_id={user_id}&new_feed_id=0&current_feed_id={current_feed_id}&community_id={community_id}">None</li>'

    # for loop to add the rest of the options to the html
    for feed in user_feeds:
        # skip the current_feed if it has one
        if feed.id == current_feed_id:
            continue
        options_html = options_html + f'<li><a class="dropdown-item" href="/feed/add_community?user_id={user_id}&new_feed_id={feed.id}&current_feed_id={current_feed_id}&community_id={community_id}">{feed.title}</li>'

    return options_html


# @bp.route('/f/<actor>', methods=['GET']) - defined in activitypub/routes.py, which calls this function for user requests. A bit weird.
@login_required_if_private_instance
def show_feed(feed):
    block_honey_pot()
    # if the feed is private abort, unless the logged in user is the owner of the feed
    if not feed.public:
        if current_user.is_authenticated and current_user.id == feed.user_id:
            ...
        elif current_user.is_authenticated and feed.subscribed(current_user.id):
            ...
        else:
            flash(_('Could not find that feed or it is not public. Try one of these instead...'))
            return redirect(url_for('main.list_feeds'))
    
    if current_user.is_anonymous:
        if current_app.config['CONTENT_WARNING']:
            if feed.nsfl:
                flash(_("This feed is only visible to logged in users."))
                next_url = "/f/" + (feed.ap_id if feed.ap_id else feed.machine_name)
                return redirect(url_for("auth.login", next=next_url))
        else:
            if feed.nsfw or feed.nsfl:
                flash(_("This feed is only visible to logged in users."))
                next_url = "/f/" + (feed.ap_id if feed.ap_id else feed.machine_name)
                return redirect(url_for("auth.login", next=next_url))

    page = request.args.get('page', 0, type=int)
    sort = request.args.get('sort', '' if current_user.is_anonymous else current_user.default_sort)
    if sort == 'scaled':
        sort = ''
    result_id = request.args.get('result_id', gibberish(15)) if current_user.is_authenticated else None
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    tag = request.args.get('tag', '')
    page_length = 20 if low_bandwidth else current_app.config['PAGE_LENGTH']
    if current_user.is_authenticated and current_user.page_length and current_user.page_length < page_length:
        page_length = current_user.page_length
    post_layout = request.args.get('layout', 'list' if not low_bandwidth else None)
    if post_layout == 'masonry':
        page_length = 200
    elif post_layout == 'masonry_wide':
        page_length = 300

    breadcrumbs = []
    existing_url = '/f'

    parent_id = feed.parent_feed_id
    parents = []
    while parent_id:
        parent_feed = Feed.query.get(parent_id)
        parents.append(parent_feed)
        parent_id = parent_feed.parent_feed_id

    for parent_feed in reversed(parents):
        breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
        breadcrumb.text = parent_feed.title
        breadcrumb.url = f'{existing_url}/{parent_feed.link()}'
        breadcrumbs.append(breadcrumb)

    breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
    breadcrumb.text = feed.title
    breadcrumb.url = ""
    breadcrumbs.append(breadcrumb)

    current_feed = feed

    if current_feed:
        # get the feed_ids
        if current_feed.show_posts_in_children:  # include posts from child feeds
            feed_ids = get_all_child_feed_ids(current_feed)
        else:
            feed_ids = [current_feed.id]

        # for each feed get the community ids (FeedItem) in the feed
        # used for the posts searching
        feed_community_ids = []
        for fid in feed_ids:
            feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == fid).\
                join(Community, Community.id == FeedItem.community_id).\
                filter(or_(Community.private == False, Community.id.in_(community_membership_private(current_user.get_id())))).all()
            for item in feed_items:
                feed_community_ids.append(item.community_id)

        post_ids = get_deduped_post_ids(result_id, feed_community_ids, sort, tag)
        has_next_page = len(post_ids) > page + 1 * page_length
        post_ids = paginate_post_ids(post_ids, page, page_length=page_length)
        posts = post_ids_to_models(post_ids, sort)

        feed_communities = Community.query.filter(
            Community.id.in_(feed_community_ids), Community.banned == False, Community.total_subscriptions_count > 0).\
            filter(Community.instance_id.not_in(blocked_or_banned_instances(current_user.get_id()))).\
            filter(Community.id.not_in(blocked_communities(current_user.get_id()))). \
            filter(or_(Community.private == False, Community.id.in_(community_membership_private(current_user.get_id())))). \
            order_by(desc(Community.total_subscriptions_count))

        next_url = url_for('activitypub.feed_profile', actor=feed.ap_id if feed.ap_id is not None else feed.name,
                           page=page + 1, sort=sort, layout=post_layout, result_id=result_id) if has_next_page else None
        prev_url = url_for('activitypub.feed_profile', actor=feed.ap_id if feed.ap_id is not None else feed.name,
                           page=page - 1, sort=sort, layout=post_layout,
                           result_id=result_id if page > 1 else None) if page > 0 else None

        sub_feeds = Feed.query.filter_by(parent_feed_id=current_feed.id).order_by(Feed.name).all()

        owner = User.query.get(feed.user_id)

        # Voting history
        if current_user.is_authenticated:
            recently_upvoted = recently_upvoted_posts(current_user.id)
            recently_downvoted = recently_downvoted_posts(current_user.id)
            communities_banned_from_list = communities_banned_from(current_user.id)
            content_filters = user_filters_posts(current_user.id)
        else:
            recently_upvoted = []
            recently_downvoted = []
            communities_banned_from_list = []
            content_filters = {}

        resp = make_response(render_template('feed/show_feed.html', title=_(current_feed.name), posts=posts, feed=current_feed,
                                             sort=sort, owner=owner,
                                             page=page, post_layout=post_layout, next_url=next_url, prev_url=prev_url,
                                             feed_communities=feed_communities, content_filters=user_filters_posts(current_user.id) if current_user.is_authenticated else {},
                                             tags=hashtags_used_in_communities(feed_community_ids, content_filters),
                                             sub_feeds=sub_feeds, feed_path=feed.path(), breadcrumbs=breadcrumbs,
                                             rss_feed=f"{current_app.config['SERVER_URL']}/f/{feed.path()}.rss",
                                             rss_feed_name=f"{current_feed.name} on {g.site.name}",
                                             communities_banned_from_list=communities_banned_from_list,
                                             show_post_community=True,
                                             joined_communities=joined_or_modding_communities(current_user.get_id()),
                                             moderated_community_ids=moderating_communities_ids(current_user.get_id()),
                                             recently_upvoted=recently_upvoted, recently_downvoted=recently_downvoted,
                                             reported_posts=reported_posts(current_user.get_id(), current_user.get_id() in g.admin_ids),
                                             user_notes=user_notes(current_user.get_id()),
                                             user_pronouns=user_pronouns(),
                                             inoculation=inoculation[
                                   randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                                             POST_TYPE_LINK=POST_TYPE_LINK, POST_TYPE_IMAGE=POST_TYPE_IMAGE,
                                             POST_TYPE_VIDEO=POST_TYPE_VIDEO,
                                             SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                                             ))
        if current_user.is_anonymous:
            resp.headers.set('Cache-Control', 'public, max-age=30')
        else:
            resp.headers.set('Cache-Control', 'private, max-age=15, must-revalidate')

        return resp
    else:
        abort(404)


def get_all_child_feed_ids(feed: Feed) -> List[int]:
    # recurse down the feed tree, gathering all the feed IDs found
    feed_ids = [feed.id]
    for child_feed in Feed.query.filter(Feed.parent_feed_id == feed.id):
        feed_ids.extend(get_all_child_feed_ids(child_feed))
    return feed_ids


@bp.route('/f/<feed_name>/submit', methods=['GET', 'POST'])
@login_required
@validation_required
@approval_required
def feed_create_post(feed_name):
    feed = Feed.query.filter(Feed.machine_name == feed_name.strip().lower()).first()
    if not feed:
        abort(404)

    feed_community_ids = []
    feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == feed.id).all()
    for item in feed_items:
        feed_community_ids.append(item.community_id)

    communities = Community.query.filter(Community.id.in_(feed_community_ids)).filter_by(banned=False).\
        order_by(Community.title).all()
    sub_feed_community_ids = []
    child_feeds = [feed.id for feed in Feed.query.filter(Feed.parent_feed_id == feed.id).all()]
    for cf_id in child_feeds:
        feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == cf_id).all()
        for item in feed_items:
            sub_feed_community_ids.append(item.community_id)

    sub_communities = Community.query.filter_by(banned=False).filter(Community.id.in_(sub_feed_community_ids)).\
        order_by(Community.title).all()
    if request.form.get('community_id', '') != '':
        community = Community.query.get_or_404(int(request.form.get('community_id')))
        return redirect(url_for('community.join_then_add', actor=community.link()))
    return render_template('feed/feed_create_post.html', communities=communities, sub_communities=sub_communities,
                           feed=feed,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR)


@bp.route('/feed/<actor>/subscribe', methods=['GET'])
@login_required
@validation_required
@approval_required
def subscribe(actor):
    join_feed(actor, current_user.id)
    referrer = request.headers.get('Referer', None)
    if referrer is not None:
        return redirect(referrer)
    else:
        return redirect('/f/' + actor)


@bp.route('/feed/<actor>/unsubscribe', methods=['GET'])
@login_required
def feed_unsubscribe(actor):
    feed = actor_to_feed(actor)

    if feed is not None:
        subscription = feed_membership(current_user, feed)
        if subscription:
            if subscription != SUBSCRIPTION_OWNER:
                proceed = True
                # Undo the Follow
                if '@' in actor:  # this is a remote feed, so activitypub is needed
                    if not feed.instance.gone_forever:
                        follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{gibberish(15)}"
                        if feed.instance.domain == 'ovo.st':
                            join_request = FeedJoinRequest.query.filter_by(user_id=current_user.id,
                                                                           feed_id=feed.id).first()
                            if join_request:
                                follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
                        undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15)
                        follow = {
                            "actor": current_user.public_url(),
                            "to": [feed.public_url()],
                            "object": feed.public_url(),
                            "type": "Follow",
                            "id": follow_id
                        }
                        undo = {
                            'actor': current_user.public_url(),
                            'to': [feed.public_url()],
                            'type': 'Undo',
                            'id': undo_id,
                            'object': follow
                        }
                        send_post_request(feed.ap_inbox_url, undo, current_user.private_key,
                                          current_user.public_url() + '#main-key', timeout=10)

                if proceed:
                    db.session.query(FeedMember).filter_by(user_id=current_user.id, feed_id=feed.id).delete()
                    db.session.query(FeedJoinRequest).filter_by(user_id=current_user.id, feed_id=feed.id).delete()
                    feed.subscriptions_count -= 1
                    db.session.commit()

                    # Remove the account from each community in the feed
                    if current_user.feed_auto_leave:
                        for feed_item in FeedItem.query.filter_by(feed_id=feed.id).all():
                            membership = CommunityMember.query.filter_by(user_id=current_user.id,
                                                                         community_id=feed_item.community_id).first()
                            if membership and membership.joined_via_feed:
                                db.session.query(CommunityMember).filter_by(user_id=current_user.id,
                                                                            community_id=feed_item.community_id).delete()
                            db.session.query(CommunityJoinRequest).filter_by(user_id=current_user.id,
                                                                             community_id=feed_item.community_id).delete()
                            cache.delete_memoized(community_membership, current_user,
                                                  Community.query.get(feed_item.community_id))
                        db.session.commit()

                    flash(_('You have left %(feed_title)s', feed_title=feed.title))
                cache.delete_memoized(feed_membership, current_user, feed)
                cache.delete_memoized(menu_subscribed_feeds, current_user.id)
                cache.delete_memoized(joined_communities, current_user.id)
            else:
                # todo: community deletion
                flash(_('You need to make someone else the owner before unsubscribing.'), 'warning')

        # send them back where they came from
        referrer = request.headers.get('Referer', None)
        if referrer is not None:
            return redirect(referrer)
        else:
            return redirect('/f/' + actor)
    else:
        abort(404)


@bp.route('/feed/lookup/<feedname>/<domain>')
def lookup(feedname, domain):
    if domain == current_app.config['SERVER_NAME']:
        return redirect('/f/' + feedname)

    feedname = feedname.lower()
    domain = domain.lower()

    exists = Feed.query.filter_by(ap_id=f'{feedname}@{domain}').first()
    if exists:
        return redirect('/f/' + feedname + '@' + domain)
    else:
        address = '~' + feedname + '@' + domain
        if current_user.is_authenticated:
            new_feed = None

            try:
                new_feed = search_for_feed(address)
            except Exception as e:
                if 'is blocked.' in str(e):
                    flash(_('Sorry, that instance is blocked, check https://gui.fediseer.com/ for reasons.'), 'warning')
            if new_feed is None:
                if g.site.enable_nsfw:
                    flash(_('Feed not found.'), 'warning')
                else:
                    flash(_('Feed not found. If you are searching for a nsfw feed it is blocked by this instance.'),
                          'warning')
            else:
                if new_feed.banned:
                    flash(_('That feed is banned from %(site)s.', site=g.site.name), 'warning')

            return render_template('feed/lookup_remote.html',
                                   title=_('Search result for remote feed'), new_feed=new_feed,
                                   subscribed=feed_membership(current_user, new_feed) >= SUBSCRIPTION_MEMBER)
        else:
            # send them back where they came from
            flash(_('Searching for remote feeds requires login'), 'error')
            referrer = request.headers.get('Referer', None)
            if referrer is not None:
                return redirect(referrer)
            else:
                return redirect('/')


@bp.route('/f/<path:feed_path>.rss', methods=['GET'])
def show_feed_rss(feed_path):
    feed_url_parts = feed_path.split('/')
    last_feed_machine_name = feed_url_parts[-1]
    feed = Feed.query.filter(Feed.machine_name == last_feed_machine_name.strip().lower()).first()

    if feed:
        # Get the feed_ids
        if feed.show_posts_in_children:  # include posts from child feeds
            feed_ids = get_all_child_feed_ids(feed)
        else:
            feed_ids = [feed.id]

        # For each feed get the community ids (FeedItem) in the feed
        feed_community_ids = []
        for fid in feed_ids:
            feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == fid).all()
            for item in feed_items:
                feed_community_ids.append(item.community_id)

        post_ids = get_deduped_post_ids('', feed_community_ids, 'new')
        post_ids = paginate_post_ids(post_ids, 0, page_length=100)
        posts = post_ids_to_models(post_ids, 'new')

        fg = FeedGenerator()
        fg.id(f"{current_app.config['SERVER_URL']}/f/{last_feed_machine_name}")
        fg.title(f'{feed.title} on {g.site.name}')
        fg.link(href=f"{current_app.config['SERVER_URL']}/f/{last_feed_machine_name}", rel='alternate')
        fg.logo(f"{current_app.config['SERVER_URL']}/static/images/apple-touch-icon.png")
        fg.subtitle(' ')
        fg.link(href=f"{current_app.config['SERVER_URL']}/f/{last_feed_machine_name}.rss", rel='self')
        fg.language('en')

        for post in posts:
            fe = fg.add_entry()
            fe.title(post.title)
            if post.slug:
                fe.link(href=f"{current_app.config['SERVER_URL']}{post.slug}")
            else:
                fe.link(href=f"{current_app.config['SERVER_URL']}/post/{post.id}")
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
        response.headers.add_header('ETag', f"{feed.id}_{hash(g.site.last_active)}")
        response.headers.add_header('Cache-Control', 'no-cache, max-age=600, must-revalidate')
        return response
    else:
        abort(404)
