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
from app.models import User, Feed, FeedMember, BlocklistItem, Community, FeedJoinRequest, BlocklistMember, \
    BlocklistJoinRequest, Instance, File, NotificationSubscription, Blocklist, BlocklistMember, BlocklistItem, UserBlock
from app.shared.tasks import task_selector
from app.shared.community import leave_community
from app.shared.upload import process_upload
from app.utils import authorise_api_user, feed_membership, get_request, menu_subscribed_feeds, blocked_users, \
    community_membership, gibberish, get_task_session, instance_banned, menu_instance_feeds, \
    piefed_markdown_to_lemmy_markdown, markdown_to_html, is_image_url, render_template, blocklist_membership


def join_blocklist(actor, user_id, src=SRC_WEB):
    try:
        remote = False
        actor = actor.strip()
        user = User.query.get(user_id)
        if '@' in actor:
            blocklist = Blocklist.query.filter_by(ap_id=actor).first()
            remote = True
        else:
            blocklist = Blocklist.query.filter_by(name=actor, ap_id=None).first()

        if blocklist is not None:
            if blocklist_membership(user, blocklist) == SUBSCRIPTION_NONMEMBER:
                from app.blocklist.routes import do_block
                success = True

                # for local feeds, joining is instant
                member = BlocklistMember(user_id=user.id, blocklist_id=blocklist.id)
                db.session.add(member)
                blocklist.subscriptions_count += 1
                db.session.commit()

                # also subscribe the user to the BlocklistItem communities
                feed_items = BlocklistItem.query.filter_by(blocklist_id=blocklist.id).all()
                for fi in feed_items:
                    to_block = User.query.get(fi.user_id)
                    actor = to_block.ap_id if to_block.ap_id else to_block.name
                    if current_app.debug:
                        do_block(actor, user.id, joined_via_blocklist=True)
                    else:
                        do_block.delay(actor, user.id, joined_via_blocklist=True)

                # feed is remote
                if remote:
                    # send ActivityPub message to remote feed, asking to follow. Accept message will be sent to our shared inbox
                    join_request = FeedJoinRequest(user_id=user.id, blocklist_id=blocklist.id)
                    db.session.add(join_request)
                    db.session.commit()
                    if blocklist.instance.online():
                        follow = {
                            "actor": user.public_url(),
                            "to": [blocklist.public_url()],
                            "object": blocklist.public_url(),
                            "type": "Follow",
                            "id": f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
                        }
                        send_post_request(blocklist.ap_inbox_url, follow, user.private_key, user.public_url() + '#main-key',
                                          timeout=10)

                        # reach out and get the BlocklistItems from the remote /following collection
                        res = get_request(blocklist.ap_following_url)
                        following_collection = res.json()

                        # for each of those add the communities
                        # subscribe the user if they have feed_auto_follow turned on
                        for fci in following_collection['items']:
                            community_ap_id = fci
                            to_block = find_actor_or_create(community_ap_id, community_only=True)
                            if to_block and isinstance(to_block, Community):
                                actor = to_block.ap_id if to_block.ap_id else to_block.name
                                do_block(actor, user.id, joined_via_blocklist=True)
                                # also make a BlocklistItem in the local db
                                feed_item = BlocklistItem(blocklist_id=blocklist.id, user_id=to_block.id)
                                db.session.add(feed_item)
                                db.session.commit()

                if success is True and src == SRC_WEB:
                    flash(_('You subscribed to %(feed_title)s', feed_title=blocklist.title))
            else:
                msg_to_user = "Already subscribed, or subscription pending"
                if src == SRC_WEB:
                    flash(_(msg_to_user))

            cache.delete_memoized(feed_membership, user, blocklist)
            cache.delete_memoized(blocked_users, user.id)
        else:
            abort(404)
    except Exception:
        db.session.rollback()
        raise
    finally:
        db.session.remove()


def leave_blocklist(blocklist: int | Blocklist, src, auth=None, bulk_leave=False):
    if isinstance(blocklist, Blocklist):
        blocklist_id = blocklist.id
    elif isinstance(blocklist, int):
        blocklist_id = blocklist
        blocklist = db.session.query(Blocklist).get(blocklist_id)
    
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    fm = db.session.query(BlocklistMember).filter_by(user_id=user_id, blocklist_id=blocklist_id).one()

    if not fm.is_owner:
        task_selector('leave_blocklist', user_id=user_id, blocklist_id=blocklist_id)
        
        db.session.query(BlocklistMember).filter_by(user_id=user_id, blocklist_id=blocklist_id).delete()
        blocklist.subscriptions_count -= 1
        db.session.commit()

        if not bulk_leave:
            # Need to unsub from every community in the feed if the user has that option set
            # During bulk_leave, community memberships handled separately
            user = db.session.query(User).get(user_id)
            feed_items = db.session.query(BlocklistItem).filter_by(blocklist_id=blocklist_id).all()
            for feed_item in feed_items:
                # Send the community unsub requests to celery - it will handle all the db commits and cache busting
                do_unblock(user_id=feed_item.user_id, src=src, auth=auth, bulk_leave=bulk_leave)

        if src == SRC_WEB and not bulk_leave:
            flash(_('You have unsubscribed from the %(blocklist_name)s blocklist, '
                    'please allow a couple minutes for it to fully process', blocklist_name = blocklist.title))
    else:
        if src == SRC_API:
            raise Exception("You cannot leave your own blocklist")
        else:
            flash(_('You cannot leave your own blocklist'), 'warning')
            return


def make_blocklist(input, src, auth=None):
    if src == SRC_API:
        url = input['url']
        title = input['title']
        public = input['public']
        description = input['description']
        members = input['members']
        user = authorise_api_user(auth, return_type='model')
    else:
        url = input.url.data
        title = input.title.data
        description = piefed_markdown_to_lemmy_markdown(input.description.data)
        members = input.members.data
        user = current_user

    private_key, public_key = RsaKeys.generate_keypair()
    blocklist = Blocklist(user_id=user.id, title=title, name=url, machine_name=url,
                description=piefed_markdown_to_lemmy_markdown(description),
                description_html=markdown_to_html(description),
                private_key=private_key,
                public_key=public_key,
                ap_profile_id='https://' + current_app.config['SERVER_NAME'] + '/f/' + url.lower(),
                ap_public_url='https://' + current_app.config['SERVER_NAME'] + '/f/' + url,
                ap_followers_url='https://' + current_app.config['SERVER_NAME'] + '/f/' + url + '/followers',
                ap_following_url='https://' + current_app.config['SERVER_NAME'] + '/f/' + url + '/following',
                ap_outbox_url='https://' + current_app.config['SERVER_NAME'] + '/f/' + url + '/outbox',
                ap_domain=current_app.config['SERVER_NAME'],
                subscriptions_count=1, instance_id=1)

    db.session.add(blocklist)
    db.session.commit()

    membership = BlocklistMember(user_id=user.id, blocklist_id=blocklist.id, is_owner=True)
    db.session.add(membership)
    db.session.commit()

    new_members = form_communities_to_ids(members)  # the communities that are in the feed now

    for added_community in new_members:
        _blocklist_add_member(added_community, 0, blocklist.id, user.id)


def edit_blocklist(input, blocklist, src, auth=None, from_scratch=False):
    if src == SRC_API:
        url = input['url']
        title = input['title']
        public = input['public']
        description = input['description']
        members = input['members']
        user = authorise_api_user(auth, return_type='model')
    else:
        url = input.url.data
        title = input.title.data
        public = input.public.data
        description = piefed_markdown_to_lemmy_markdown(input.description.data)
        members = input.members.data
        user = current_user

    if url:
        url = slugify(url.strip().split('/')[0], separator='_').lower()
        url = slugify(url.strip(), separator='_').lower() + '/' + user.user_name.lower()
        blocklist.name = url
        blocklist.machine_name = url
    blocklist.title = title
    blocklist.description = piefed_markdown_to_lemmy_markdown(description)
    blocklist.description_html = markdown_to_html(description)

    if not from_scratch:
        if not (blocklist.user_id == user.id or user.is_admin()):
            raise Exception('incorrect_login')

    db.session.add(blocklist)
    db.session.commit()

    # Update BlocklistItems based on whatever has changed in the form.members field
    old_communities = set(existing_users(blocklist.id))  # the accounts that were in the blocklist before the edit was made
    new_communities = form_communities_to_ids(members)  # the members that are in the blocklist now
    added_communities = new_communities - old_communities
    removed_communities = old_communities - new_communities

    for added_community in added_communities:
        _blocklist_add_member(added_community, 0, blocklist.id, user.id)

    for removed_community in removed_communities:
        _blocklist_remove_member(removed_community, blocklist.id)
        

def delete_blocklist(blocklist_id: int, src, auth=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    blocklist = db.session.query(Blocklist).get(blocklist_id)

    # does the user own the feed
    if blocklist.user_id != user_id:
        abort(404)

    # announce the change to any potential subscribers
    # have to do it here before the blocklist members are cleared out
    if current_app.debug:
        announce_blocklist_delete_to_subscribers(user_id, blocklist.id)
    else:
        announce_blocklist_delete_to_subscribers.delay(user_id, blocklist.id)

    # strip out any feedmembers before deleting
    db.session.query(BlocklistMember).filter(BlocklistMember.blocklist_id == blocklist.id).delete()

    # delete the feed
    if blocklist.num_blocked > 0:
        db.session.query(BlocklistItem).filter(BlocklistItem.blocklist_id == blocklist.id).delete()
    db.session.delete(blocklist)
    db.session.commit()


def _blocklist_add_member(member_id: int, current_blocklist_id: int, blocklist_id: int, user_id: int):
    # if current_feed_id is not 0 then we are moving a blocked user from
    # one blocklist to another
    if current_blocklist_id != 0:
        current_feed_item = BlocklistItem.query.filter_by(blocklist_id=current_blocklist_id).filter_by(
            user_id=member_id).first()
        db.session.delete(current_feed_item)
        db.session.commit()

        # also update the num_communities for the old feed
        blocklist = Blocklist.query.get(current_blocklist_id)
        blocklist.num_blocked = blocklist.num_blocked - 1
        db.session.add(blocklist)
        db.session.commit()

        # announce the change to any potential subscribers
        user = User.query.get(member_id)
        if current_app.debug:
            announce_blocklist_add_remove_to_subscribers("Remove", blocklist.id, user.id)
        else:
            announce_blocklist_add_remove_to_subscribers.delay("Remove", blocklist.id, user.id)

    # make the new BlocklistItem and commit it
    blocklist_item = BlocklistItem(blocklist_id=blocklist_id, user_id=member_id)
    db.session.add(blocklist_item)
    db.session.commit()

    # also update the num_communities for the new feed
    blocklist = Blocklist.query.get(blocklist_id)
    blocklist.num_blocked = blocklist.num_blocked + 1
    db.session.add(blocklist)
    db.session.commit()

    # announce the change to any potential subscribers
    u = User.query.get(member_id)
    if current_app.debug:
        announce_blocklist_add_remove_to_subscribers("Add", blocklist.id, u.id)
    else:
        announce_blocklist_add_remove_to_subscribers.delay("Add", blocklist.id, u.id)

    # subscribe the user to the community if they are not already subscribed
    current_membership = BlocklistMember.query.filter_by(user_id=user_id, blocklist_id=blocklist_id).first()
    if current_membership is None:
        # import do_subscribe here, otherwise we get import errors from circular import problems
        from app.blocklist.routes import do_subscribe
        u = User.query.get(member_id)
        actor = u.ap_id if u.ap_id else u.name
        do_subscribe(actor, user_id, joined_via_blocklist=True)


def _blocklist_remove_member(user_id: int, current_blocklist_id: int):
    current_feed_item = db.session.query(BlocklistItem).filter_by(blocklist_id=current_blocklist_id).filter_by(user_id=user_id).first()
    db.session.delete(current_feed_item)
    db.session.commit()

    # also update the num_communities for the old feed
    blocklist = db.session.query(Blocklist).get(current_blocklist_id)
    blocklist.num_blocks = blocklist.num_blocks - 1
    db.session.add(blocklist)
    db.session.commit()

    user = db.session.query(User).get(user_id)
    community_members = db.session.query(UserBlock).filter_by(user_id=user.id).all()
    # make all local users un-block the previously blocked account - if the user blocked the other user
    # as a result of adding it to a blocklist
    for cm in community_members:
        user = db.session.query(User).get(cm.user_id)
        if user.is_local() and cm.blocked_via_blocklist is not None and cm.blocked_via_blocklist:
            subscription = blocklist_membership(user, blocklist)
            if subscription != SUBSCRIPTION_OWNER:
                proceed = True
                # Undo the Follow
                if not user.is_local():  # this is a remote community, so activitypub is needed
                    if not user.instance.gone_forever:
                        follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{gibberish(15)}"
                        if user.instance.domain == 'ovo.st':
                            join_request = db.session.query(BlocklistJoinRequest).filter_by(user_id=user.id,
                                                                                            user_id=user.id).first()
                            if join_request:
                                follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
                        undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15)
                        follow = {
                            "actor": user.public_url(),
                            "to": [user.public_url()],
                            "object": user.public_url(),
                            "type": "Follow",
                            "id": follow_id
                        }
                        undo = {
                            'actor': user.public_url(),
                            'to': [user.public_url()],
                            'type': 'Undo',
                            'id': undo_id,
                            'object': follow
                        }
                        send_post_request(user.ap_inbox_url, undo, user.private_key,
                                          user.public_url() + '#main-key', timeout=10)

                if proceed:
                    db.session.query(BlocklistMember).filter_by(user_id=user.id, user_id=user.id).delete()
                    db.session.query(BlocklistJoinRequest).filter_by(user_id=user.id, user_id=user.id).delete()
                    db.session.commit()

                cache.delete_memoized(blocklist_membership, user, user)
                cache.delete_memoized(blocked_users, user.id)

    # announce the change to any potential subscribers
    if blocklist.public:
        if current_app.debug:
            announce_blocklist_add_remove_to_subscribers("Remove", blocklist.id, user.id)
        else:
            announce_blocklist_add_remove_to_subscribers.delay("Remove", blocklist.id, user.id)


@celery.task
def announce_blocklist_add_remove_to_subscribers(action: str, feed_id: int, community_id: int):
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
    feed_members = FeedMember.query.filter_by(blocklist_id=feed.id).all()

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
def announce_blocklist_delete_to_subscribers(user_id, blocklist_id):
    # get the user
    user = User.query.get(user_id)
    # get the feed
    feed = Feed.query.get(blocklist_id)
    # create the delete json
    delete_json = {
        "@context": default_context(),
        "type": "Delete",
        "actor": user.ap_public_url,
        "id": f"{current_app.config['SERVER_URL']}/delete/{gibberish(15)}",
        "object": {
            "type": "Blocklist",
            "id": feed.ap_public_url
        }
    }

    # find the feed members
    feed_members = FeedMember.query.filter_by(blocklist_id=feed.id).all()

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

