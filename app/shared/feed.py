from __future__ import annotations

from typing import List

from flask import flash, current_app, abort, g
from flask_babel import _
from flask_login import current_user
from slugify import slugify
from sqlalchemy import text

from app import db, cache, celery
from app.activitypub.signature import send_post_request, default_context, RsaKeys
from app.activitypub.util import find_actor_or_create, make_image_sizes
from app.constants import *
from app.models import User, Feed, FeedMember, FeedItem, Community, FeedJoinRequest, CommunityMember, \
    CommunityJoinRequest, Instance, File
from app.shared.tasks import task_selector
from app.shared.community import leave_community
from app.shared.upload import process_upload
from app.utils import authorise_api_user, feed_membership, get_request, menu_subscribed_feeds, joined_communities, \
    community_membership, gibberish, get_task_session, instance_banned, menu_instance_feeds, \
    piefed_markdown_to_lemmy_markdown, markdown_to_html, is_image_url


def join_feed(actor, user_id, src=SRC_WEB):
    try:
        remote = False
        actor = actor.strip()
        user = User.query.get(user_id)
        if '@' in actor:
            feed = Feed.query.filter_by(ap_id=actor).first()
            remote = True
        else:
            feed = Feed.query.filter_by(name=actor, ap_id=None).first()

        if feed is not None:
            if feed_membership(user, feed) == SUBSCRIPTION_NONMEMBER:
                from app.community.routes import do_subscribe
                success = True

                # for local feeds, joining is instant
                member = FeedMember(user_id=user.id, feed_id=feed.id)
                db.session.add(member)
                feed.subscriptions_count += 1
                db.session.commit()

                # also subscribe the user to the feeditem communities
                # if they have feed_auto_follow turned on
                if user.feed_auto_follow:
                    feed_items = FeedItem.query.filter_by(feed_id=feed.id).all()
                    for fi in feed_items:
                        community = Community.query.get(fi.community_id)
                        actor = community.ap_id if community.ap_id else community.name
                        if current_app.debug:
                            do_subscribe(actor, user.id, joined_via_feed=True)
                        else:
                            do_subscribe.delay(actor, user.id, joined_via_feed=True)

                # feed is remote
                if remote:
                    # send ActivityPub message to remote feed, asking to follow. Accept message will be sent to our shared inbox
                    join_request = FeedJoinRequest(user_id=user.id, feed_id=feed.id)
                    db.session.add(join_request)
                    db.session.commit()
                    if feed.instance.online():
                        follow = {
                            "actor": user.public_url(),
                            "to": [feed.public_url()],
                            "object": feed.public_url(),
                            "type": "Follow",
                            "id": f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
                        }
                        send_post_request(feed.ap_inbox_url, follow, user.private_key, user.public_url() + '#main-key',
                                          timeout=10)

                        # reach out and get the feeditems from the remote /following collection
                        res = get_request(feed.ap_following_url)
                        following_collection = res.json()

                        # for each of those add the communities
                        # subscribe the user if they have feed_auto_follow turned on
                        for fci in following_collection['items']:
                            community_ap_id = fci
                            community = find_actor_or_create(community_ap_id, community_only=True)
                            if community and isinstance(community, Community):
                                actor = community.ap_id if community.ap_id else community.name
                                if user.feed_auto_follow:
                                    do_subscribe(actor, user.id, joined_via_feed=True)
                                # also make a feeditem in the local db
                                feed_item = FeedItem(feed_id=feed.id, community_id=community.id)
                                db.session.add(feed_item)
                                db.session.commit()

                if success is True and src == SRC_WEB:
                    flash(_('You subscribed to %(feed_title)s', feed_title=feed.title))
            else:
                msg_to_user = "Already subscribed, or subscription pending"
                if src == SRC_WEB:
                    flash(_(msg_to_user))

            cache.delete_memoized(feed_membership, user, feed)
            cache.delete_memoized(menu_subscribed_feeds, user.id)
            cache.delete_memoized(joined_communities, user.id)
        else:
            abort(404)
    except Exception:
        db.session.rollback()
        raise
    finally:
        db.session.remove()


def leave_feed(feed: int | Feed, src, auth=None, bulk_leave=False):
    if isinstance(feed, Feed):
        feed_id = feed.id
    elif isinstance(feed, int):
        feed_id = feed
        feed = db.session.query(Feed).get(feed_id)
    
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    fm = db.session.query(FeedMember).filter_by(user_id=user_id, feed_id=feed_id).one()

    if not fm.is_owner:
        task_selector('leave_feed', user_id=user_id, feed_id=feed_id)
        
        db.session.query(FeedMember).filter_by(user_id=user_id, feed_id=feed_id).delete()
        feed.subscriptions_count -= 1
        db.session.commit()

        if not bulk_leave:
            # Need to unsub from every community in the feed if the user has that option set
            # During bulk_leave, community memberships handled separately
            user = db.session.query(User).get(user_id)
            if user.feed_auto_leave:
                feed_items = db.session.query(FeedItem).filter_by(feed_id=feed_id).all()
                for feed_item in feed_items:
                    # Send the community unsub requests to celery - it will handle all the db commits and cache busting
                    leave_community(community_id=feed_item.community_id, src=src, auth=auth, bulk_leave=bulk_leave)

        if src == SRC_WEB and not bulk_leave:
            flash(_('You have unsubscribed from the %(feed_name)s feed, '
                    'please allow a couple minutes for it to fully process', feed_name = feed.title))
    else:
        if src == SRC_API:
            raise Exception("You cannot leave your own feed")
        else:
            flash(_('You cannot leave your own feed'), 'warning')
            return


def make_feed(input, src, auth=None, uploaded_icon_file=None, uploaded_banner_file=None):
    if src == SRC_API:
        url = input['url']
        title = input['title']
        public = input['public']
        description = input['description']
        icon_url = input['icon_url']
        banner_url = input['banner_url']
        nsfw = input['nsfw']
        nsfl = input['nsfl']
        communities = input['communities']
        is_instance_feed = input['is_instance_feed']
        show_child_posts = input['show_child_posts']
        parent_feed_id = input['parent_feed_id']
        user = authorise_api_user(auth, return_type='model')
    else:
        url = input.url.data
        title = input.title.data
        public = input.public.data
        description = piefed_markdown_to_lemmy_markdown(input.description.data)
        icon_url = process_upload(uploaded_icon_file, destination='feeds') if uploaded_icon_file else None
        banner_url = process_upload(uploaded_banner_file, destination='feeds') if uploaded_banner_file else None
        nsfw = input.nsfw.data
        nsfl = input.nsfl.data
        communities = input.communities.data
        is_instance_feed = input.is_instance_feed.data
        show_child_posts = input.show_child_posts.data
        parent_feed_id = input.parent_feed_id.data
        user = current_user

    private_key, public_key = RsaKeys.generate_keypair()
    feed = Feed(user_id=user.id, title=title, name=url, machine_name=url,
                description=piefed_markdown_to_lemmy_markdown(description),
                description_html=markdown_to_html(description),
                show_posts_in_children=show_child_posts,
                nsfw=nsfw, nsfl=nsfl,
                private_key=private_key,
                public_key=public_key,
                public=public, is_instance_feed=is_instance_feed,
                ap_profile_id='https://' + current_app.config['SERVER_NAME'] + '/f/' + url.lower(),
                ap_public_url='https://' + current_app.config['SERVER_NAME'] + '/f/' + url,
                ap_followers_url='https://' + current_app.config['SERVER_NAME'] + '/f/' + url + '/followers',
                ap_following_url='https://' + current_app.config['SERVER_NAME'] + '/f/' + url + '/following',
                ap_outbox_url='https://' + current_app.config['SERVER_NAME'] + '/f/' + url + '/outbox',
                ap_domain=current_app.config['SERVER_NAME'],
                subscriptions_count=1, instance_id=1)
    if parent_feed_id:
        feed.parent_feed_id = parent_feed_id
    else:
        feed.parent_feed_id = None

    if icon_url and is_image_url(icon_url):
        file = File(source_url=icon_url)
        db.session.add(file)
        db.session.commit()
        feed.icon_id = file.id
        make_image_sizes(feed.icon_id, 40, 250, 'feeds', False)
    if banner_url and is_image_url(banner_url):
        file = File(source_url=banner_url)
        db.session.add(file)
        db.session.commit()
        feed.image_id = file.id
        make_image_sizes(feed.image_id, 878, 1600, 'feeds', False)

    db.session.add(feed)
    db.session.commit()

    membership = FeedMember(user_id=user.id, feed_id=feed.id, is_owner=True)
    db.session.add(membership)
    db.session.commit()

    new_communities = form_communities_to_ids(communities)  # the communities that are in the feed now

    for added_community in new_communities:
        _feed_add_community(added_community, 0, feed.id, user.id)


def edit_feed(input, feed, src, auth=None, uploaded_icon_file=None, uploaded_banner_file=None, from_scratch=False):
    if src == SRC_API:
        url = input['url']
        title = input['title']
        public = input['public']
        description = input['description']
        icon_url = input['icon_url']
        banner_url = input['banner_url']
        nsfw = input['nsfw']
        nsfl = input['nsfl']
        communities = input['communities']
        is_instance_feed = input['is_instance_feed']
        show_child_posts = input['show_child_posts']
        parent_feed_id = input['parent_feed_id']
        user = authorise_api_user(auth, return_type='model')
    else:
        url = input.url.data
        title = input.title.data
        public = input.public.data
        description = piefed_markdown_to_lemmy_markdown(input.description.data)
        icon_url = process_upload(uploaded_icon_file, destination='feeds') if uploaded_icon_file else None
        banner_url = process_upload(uploaded_banner_file, destination='feeds') if uploaded_banner_file else None
        nsfw = input.nsfw.data
        nsfl = input.nsfl.data
        communities = input.communities.data
        is_instance_feed = input.is_instance_feed.data
        show_child_posts = input.show_child_posts.data
        parent_feed_id = input.parent_feed_id.data
        user = current_user

    icon_url_changed = banner_url_changed = False

    if url:
        url = slugify(url.strip().split('/')[0], separator='_').lower()
        if not public:
            url = slugify(url.strip(), separator='_').lower() + '/' + user.user_name.lower()
        feed.name = url
        feed.machine_name = url
    feed.title = title
    feed.description = piefed_markdown_to_lemmy_markdown(description)
    feed.description_html = markdown_to_html(description)
    feed.show_posts_in_children = show_child_posts
    if parent_feed_id:
        feed.parent_feed_id = parent_feed_id
    else:
        feed.parent_feed_id = None

    old_icon_id = 0
    old_banner_id = 0

    if not from_scratch:
        if not (feed.user_id == user.id or user.is_admin()):
            raise Exception('incorrect_login')

        # Store old file IDs before processing new URLs
        old_icon_id = feed.icon_id
        old_banner_id = feed.image_id
        icon_url_changed = banner_url_changed = False

        if feed.icon_id and icon_url != feed.icon.source_url:
            if icon_url != feed.icon.medium_url():
                icon_url_changed = True
        if not feed.icon_id:
            icon_url_changed = True
        if feed.image_id and banner_url != feed.image.source_url:
            if banner_url != feed.image.medium_url():
                banner_url_changed = True
        if not feed.image_id:
            cache.delete_memoized(Feed.header_image, feed)
            banner_url_changed = True

    if icon_url and (from_scratch or icon_url_changed) and is_image_url(icon_url):
        file = File(source_url=icon_url)
        db.session.add(file)
        db.session.commit()
        feed.icon_id = file.id
        make_image_sizes(feed.icon_id, 40, 250, 'feeds', False)
        # Only delete old icon after new one is successfully saved
        if not from_scratch and old_icon_id and old_icon_id != feed.icon_id:
            remove_file = File.query.get(old_icon_id)
            if remove_file:
                remove_file.delete_from_disk()
                db.session.delete(remove_file)
    if banner_url and (from_scratch or banner_url_changed) and is_image_url(banner_url):
        file = File(source_url=banner_url)
        db.session.add(file)
        db.session.commit()
        feed.image_id = file.id
        make_image_sizes(feed.image_id, 878, 1600, 'feeds', False)
        # Only delete old banner after new one is successfully saved
        if not from_scratch and old_banner_id and old_banner_id != feed.image_id:
            remove_file = File.query.get(old_banner_id)
            if remove_file:
                remove_file.delete_from_disk()
                db.session.delete(remove_file)
                cache.delete_memoized(Feed.header_image, feed)

    if g.site.enable_nsfw:
        feed.nsfw = nsfw
    if g.site.enable_nsfl:
        feed.nsfl = nsfl
    # unsubscribe every feed member except owner when moving from public to private
    if feed.public and not public:
        db.session.query(FeedMember).filter(FeedMember.is_owner == False).delete()
        db.session.query(FeedJoinRequest).filter_by(user_id=user.id, feed_id=feed.id).delete()
        feed.subscriptions_count = db.session.query(FeedMember).filter(FeedMember.is_owner == False).count()
    feed.public = public
    if user.is_admin():
        feed.is_instance_feed = is_instance_feed
        cache.delete_memoized(menu_instance_feeds)
    db.session.add(feed)
    db.session.commit()

    # Update FeedItems based on whatever has changed in the form.communities field
    old_communities = set(
        existing_communities(feed.id))  # the communities that were in the feed before the edit was made
    new_communities = form_communities_to_ids(communities)  # the communities that are in the feed now
    added_communities = new_communities - old_communities
    removed_communities = old_communities - new_communities

    for added_community in added_communities:
        _feed_add_community(added_community, 0, feed.id, user.id)

    for removed_community in removed_communities:
        _feed_remove_community(removed_community, feed.id)
        

def delete_feed(feed_id: int, src, auth=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    feed = db.session.query(Feed).get(feed_id)

    # does the user own the feed
    if feed.user_id != user_id:
        abort(404)

    # announce the change to any potential subscribers
    # have to do it here before the feed members are cleared out
    if feed.public:
        if current_app.debug:
            announce_feed_delete_to_subscribers(user_id, feed.id)
        else:
            announce_feed_delete_to_subscribers.delay(user_id, feed.id)

    # strip out any feedmembers before deleting
    db.session.query(FeedMember).filter(FeedMember.feed_id == feed.id).delete()

    # delete the feed
    if feed.num_communities > 0:
        db.session.query(FeedItem).filter(FeedItem.feed_id == feed.id).delete()
    db.session.delete(feed)
    db.session.commit()


def _feed_add_community(community_id: int, current_feed_id: int, feed_id: int, user_id: int):
    # if current_feed_id is not 0 then we are moving a community from
    # one feed to another
    if current_feed_id != 0:
        current_feed_item = FeedItem.query.filter_by(feed_id=current_feed_id).filter_by(
            community_id=community_id).first()
        db.session.delete(current_feed_item)
        db.session.commit()

        # also update the num_communities for the old feed
        current_feed = Feed.query.get(current_feed_id)
        current_feed.num_communities = current_feed.num_communities - 1
        db.session.add(current_feed)
        db.session.commit()

        # announce the change to any potential subscribers
        if current_feed.public:
            community = Community.query.get(community_id)
            if current_app.debug:
                announce_feed_add_remove_to_subscribers("Remove", current_feed.id, community.id)
            else:
                announce_feed_add_remove_to_subscribers.delay("Remove", current_feed.id, community.id)

    # make the new feeditem and commit it
    feed_item = FeedItem(feed_id=feed_id, community_id=community_id)
    db.session.add(feed_item)
    db.session.commit()

    # also update the num_communities for the new feed
    feed = Feed.query.get(feed_id)
    feed.num_communities = feed.num_communities + 1
    db.session.add(feed)
    db.session.commit()

    # announce the change to any potential subscribers
    if feed.public:
        community = Community.query.get(community_id)
        if current_app.debug:
            announce_feed_add_remove_to_subscribers("Add", feed.id, community.id)
        else:
            announce_feed_add_remove_to_subscribers.delay("Add", feed.id, community.id)

    # subscribe the user to the community if they are not already subscribed
    current_membership = CommunityMember.query.filter_by(user_id=user_id, community_id=community_id).first()
    if current_membership is None and current_user.feed_auto_follow:
        # import do_subscribe here, otherwise we get import errors from circular import problems
        from app.community.routes import do_subscribe
        community = Community.query.get(community_id)
        actor = community.ap_id if community.ap_id else community.name
        do_subscribe(actor, user_id, joined_via_feed=True)


def _feed_remove_community(community_id: int, current_feed_id: int):
    current_feed_item = db.session.query(FeedItem).filter_by(feed_id=current_feed_id).filter_by(community_id=community_id).first()
    db.session.delete(current_feed_item)
    db.session.commit()

    # also update the num_communities for the old feed
    current_feed = db.session.query(Feed).get(current_feed_id)
    current_feed.num_communities = current_feed.num_communities - 1
    db.session.add(current_feed)
    db.session.commit()

    community = db.session.query(Community).get(community_id)
    community_members = db.session.query(CommunityMember).filter_by(community_id=community.id).all()
    # make all local users un-follow the community - if user.feed_auto_leave, and the user joined the community
    # as a result of adding it to a feed
    for cm in community_members:
        user = db.session.query(User).get(cm.user_id)
        if user.is_local() and user.feed_auto_leave and cm.joined_via_feed is not None and cm.joined_via_feed:
            subscription = community_membership(user, community)
            if subscription != SUBSCRIPTION_OWNER:
                proceed = True
                # Undo the Follow
                if not community.is_local():  # this is a remote community, so activitypub is needed
                    if not community.instance.gone_forever:
                        follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{gibberish(15)}"
                        if community.instance.domain == 'ovo.st':
                            join_request = db.session.query(CommunityJoinRequest).filter_by(user_id=user.id,
                                                                                            community_id=community.id).first()
                            if join_request:
                                follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
                        undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15)
                        follow = {
                            "actor": user.public_url(),
                            "to": [community.public_url()],
                            "object": community.public_url(),
                            "type": "Follow",
                            "id": follow_id
                        }
                        undo = {
                            'actor': user.public_url(),
                            'to': [community.public_url()],
                            'type': 'Undo',
                            'id': undo_id,
                            'object': follow
                        }
                        send_post_request(community.ap_inbox_url, undo, user.private_key,
                                          user.public_url() + '#main-key', timeout=10)

                if proceed:
                    db.session.query(CommunityMember).filter_by(user_id=user.id, community_id=community.id).delete()
                    db.session.query(CommunityJoinRequest).filter_by(user_id=user.id, community_id=community.id).delete()
                    # Only decrement subscriptions_count for users with ap_id == None
                    if user.ap_id is None:
                        community.subscriptions_count -= 1
                    # Only decrement total_subscriptions_count for local communities
                    if community.is_local():
                        community.total_subscriptions_count -= 1
                    db.session.commit()

                cache.delete_memoized(community_membership, user, community)
                cache.delete_memoized(joined_communities, user.id)

    # announce the change to any potential subscribers
    if current_feed.public:
        if current_app.debug:
            announce_feed_add_remove_to_subscribers("Remove", current_feed.id, community.id)
        else:
            announce_feed_add_remove_to_subscribers.delay("Remove", current_feed.id, community.id)


@celery.task
def announce_feed_add_remove_to_subscribers(action: str, feed_id: int, community_id: int):
    # find the feed
    feed = Feed.query.get(feed_id)
    # find the community
    community = Community.query.get(community_id)
    # build the Announce json
    activity_json = {
        "@context": default_context(),
        "type": "Announce",
        "actor": feed.ap_public_url,
        "id": f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}",
    }

    # build the object json
    object_json = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "type": action,
        "actor": feed.ap_public_url,
        "id": f"{current_app.config['SERVER_URL']}/activities/feedadd/{gibberish(15)}",
        "object": {
            "type": "Group",
            "id": community.ap_public_url
        },
        "target": {
            "type": "Collection",
            "id": feed.ap_following_url
        }
    }

    # embed the object json in the Announce json
    activity_json['object'] = object_json

    # look up the feedmembers
    feed_members = FeedMember.query.filter_by(feed_id=feed.id).all()

    # for each member
    #  - if its the owner, skip
    #  - if its a local server user, skip
    #  - if its a remote user
    # setup a db session for this task
    session = get_task_session()
    try:
        for fm in feed_members:
            fm_user = User.query.get(fm.user_id)
            if fm_user.id == feed.user_id:
                continue
            if fm_user.is_local():
                # user is local so lets auto-subscribe them to the community
                from app.community.routes import do_subscribe
                actor = community.ap_id if community.ap_id else community.name
                do_subscribe(actor, fm_user.id, joined_via_feed=True)
                continue

            # if we get here the feedmember is a remote user
            instance: Instance = session.query(Instance).get(fm_user.instance.id)
            if instance.inbox and instance.online() and not instance_banned(instance.domain):
                send_post_request(instance.inbox, activity_json, feed.private_key, feed.ap_profile_id + '#main-key', timeout=10)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def announce_feed_delete_to_subscribers(user_id, feed_id):
    # get the user
    user = User.query.get(user_id)
    # get the feed
    feed = Feed.query.get(feed_id)
    # create the delete json
    delete_json = {
        "@context": default_context(),
        "type": "Delete",
        "actor": user.ap_public_url,
        "id": f"{current_app.config['SERVER_URL']}/delete/{gibberish(15)}",
        "object": {
            "type": "Feed",
            "id": feed.ap_public_url
        }
    }

    # find the feed members
    feed_members = FeedMember.query.filter_by(feed_id=feed.id).all()

    # for each member
    #  - if its the owner, skip
    #  - if its a local server user, skip
    #  - if its a remote user
    session = get_task_session()
    try:
        for fm in feed_members:
            fm_user = session.query(User).get(fm.user_id)
            if fm_user.id == feed.user_id:
                continue
            if fm_user.is_local():
                continue
            # if we get here the feedmember is a remote user
            instance: Instance = session.query(Instance).get(fm_user.instance.id)
            if instance.inbox and instance.online() and not instance_banned(instance.domain):
                send_post_request(instance.inbox, delete_json, user.private_key, user.ap_profile_id + '#main-key', timeout=10)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def existing_communities(feed_id: int) -> List:
    return db.session.execute(text('SELECT community_id FROM feed_item WHERE feed_id = :feed_id'),
                              {'feed_id': feed_id}).scalars()

def form_communities_to_ids(form_communities: str) -> set:
    from app.community.util import search_for_community
    result = set()
    parts = form_communities.strip().split('\n')
    for community_ap_id in parts:
        if not community_ap_id.startswith('!'):
            community_ap_id = '!' + community_ap_id
        if not '@' in community_ap_id:
            community_ap_id = community_ap_id + '@' + current_app.config['SERVER_NAME']
        community = search_for_community(community_ap_id.strip())
        if community:
            result.add(community.id)
    return result
