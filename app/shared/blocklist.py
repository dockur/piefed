from __future__ import annotations

from typing import List

from flask import flash, current_app
from flask_babel import _
from flask_login import current_user
from sqlalchemy import text

from app import db, cache, celery
from app.activitypub.signature import send_post_request, default_context, RsaKeys
from app.constants import *
from app.models import User, Blocklist, BlocklistMember, BlocklistItem, BlocklistJoinRequest, utcnow
from app.utils import authorise_api_user, get_request, blocked_users, gibberish, get_task_session, instance_banned, \
    piefed_markdown_to_lemmy_markdown, markdown_to_html, blocklist_membership, patch_db_session


# How blocklists work
# -------------------
# Blocklist         - a list of accounts, owned by one user. Federated as an actor of type 'Blocklist' at /b/<machine_name>
# BlocklistItem     - one account that is on the list. The /b/<machine_name>/following collection lists these.
# BlocklistMember   - someone subscribed to the list. The owner is always a member (is_owner=True).
# UserBlock         - for every *local* member, each account on the list gets a UserBlock with blocked_via_blocklist=True.
#                     Blocks the user made by hand have blocked_via_blocklist=False and are never removed by blocklist code.
#
# Remote instances subscribe to a list by sending a Follow from one of their users to the Blocklist actor. Changes to the
# list are sent to those instances as Announce(Add) / Announce(Remove) from the Blocklist actor, and each instance applies
# the change to its own local subscribers. Blocks themselves are never federated (same as normal user blocks).


# ----- Applying / removing UserBlocks -----

def _block_params(**kwargs):
    params = {'admin': ROLE_ADMIN, 'staff': ROLE_STAFF, 'notif_user': NOTIF_USER, 'now': utcnow()}
    params.update(kwargs)
    return params


def apply_blocklist_to_user(blocklist_id: int, user_id: int):
    """Block every account in the blocklist, on behalf of a local user. Safe to call repeatedly."""
    params = _block_params(blocklist_id=blocklist_id, user_id=user_id)
    db.session.execute(text('''
        INSERT INTO "user_block" (blocker_id, blocked_id, created_at, blocked_via_blocklist)
        SELECT DISTINCT :user_id, bi.user_id, :now, true
        FROM "blocklist_item" bi
        WHERE bi.blocklist_id = :blocklist_id AND bi.user_id != :user_id
          AND NOT EXISTS (SELECT 1 FROM "user_block" ub WHERE ub.blocker_id = :user_id AND ub.blocked_id = bi.user_id)
          AND NOT EXISTS (SELECT 1 FROM "user_role" ur WHERE ur.user_id = bi.user_id AND ur.role_id IN (:admin, :staff))
    '''), params)
    db.session.execute(text('''
        DELETE FROM "notification_subscription"
        WHERE type = :notif_user AND entity_id = :user_id
          AND user_id IN (SELECT user_id FROM "blocklist_item" WHERE blocklist_id = :blocklist_id)
    '''), params)
    db.session.commit()
    cache.delete_memoized(blocked_users, user_id)


def unapply_blocklist_from_user(blocklist_id: int, user_id: int):
    """Remove the blocks that a local user got from this blocklist, unless another blocklist they are subscribed to
    also blocks the same account. Blocks made by hand are left alone."""
    db.session.execute(text('''
        DELETE FROM "user_block" ub
        WHERE ub.blocker_id = :user_id AND ub.blocked_via_blocklist = true
          AND ub.blocked_id IN (SELECT user_id FROM "blocklist_item" WHERE blocklist_id = :blocklist_id)
          AND NOT EXISTS (SELECT 1 FROM "blocklist_item" bi
                          JOIN "blocklist_member" bm ON bm.blocklist_id = bi.blocklist_id
                          WHERE bm.user_id = :user_id AND bi.blocklist_id != :blocklist_id AND bi.user_id = ub.blocked_id)
    '''), {'blocklist_id': blocklist_id, 'user_id': user_id})
    db.session.commit()
    cache.delete_memoized(blocked_users, user_id)


def _local_subscriber_ids(blocklist_id: int) -> List[int]:
    return list(db.session.execute(text('''
        SELECT bm.user_id FROM "blocklist_member" bm JOIN "user" u ON u.id = bm.user_id
        WHERE bm.blocklist_id = :blocklist_id AND u.ap_id is null
    '''), {'blocklist_id': blocklist_id}).scalars())


def _apply_item_to_subscribers(blocklist_id: int, blocked_id: int):
    """A new account was added to the blocklist - block it on behalf of every local subscriber."""
    role = db.session.execute(text('SELECT role_id FROM "user_role" WHERE user_id = :user_id AND role_id IN (:admin, :staff)'),
                              _block_params(user_id=blocked_id)).scalar()
    if role:
        return  # admin and staff cannot be blocked
    params = _block_params(blocklist_id=blocklist_id, blocked_id=blocked_id)
    db.session.execute(text('''
        INSERT INTO "user_block" (blocker_id, blocked_id, created_at, blocked_via_blocklist)
        SELECT DISTINCT bm.user_id, :blocked_id, :now, true
        FROM "blocklist_member" bm JOIN "user" u ON u.id = bm.user_id
        WHERE bm.blocklist_id = :blocklist_id AND u.ap_id is null AND bm.user_id != :blocked_id
          AND NOT EXISTS (SELECT 1 FROM "user_block" ub WHERE ub.blocker_id = bm.user_id AND ub.blocked_id = :blocked_id)
    '''), params)
    db.session.execute(text('''
        DELETE FROM "notification_subscription"
        WHERE type = :notif_user AND user_id = :blocked_id
          AND entity_id IN (SELECT user_id FROM "blocklist_member" WHERE blocklist_id = :blocklist_id)
    '''), params)
    db.session.commit()
    for subscriber_id in _local_subscriber_ids(blocklist_id):
        cache.delete_memoized(blocked_users, subscriber_id)


def _remove_item_from_subscribers(blocklist_id: int, blocked_id: int):
    """An account was removed from the blocklist - unblock it for every local subscriber, unless they also block it
    by hand or through another blocklist."""
    db.session.execute(text('''
        DELETE FROM "user_block" ub USING "blocklist_member" bm
        WHERE bm.blocklist_id = :blocklist_id AND ub.blocker_id = bm.user_id AND ub.blocked_id = :blocked_id
          AND ub.blocked_via_blocklist = true
          AND NOT EXISTS (SELECT 1 FROM "blocklist_item" bi
                          JOIN "blocklist_member" bm2 ON bm2.blocklist_id = bi.blocklist_id
                          WHERE bm2.user_id = bm.user_id AND bi.blocklist_id != :blocklist_id AND bi.user_id = :blocked_id)
    '''), {'blocklist_id': blocklist_id, 'blocked_id': blocked_id})
    db.session.commit()
    for subscriber_id in _local_subscriber_ids(blocklist_id):
        cache.delete_memoized(blocked_users, subscriber_id)


# ----- Changing the contents of a blocklist -----

def add_to_blocklist(blocklist: Blocklist, blocked_user_id: int, federate=True) -> bool:
    if blocked_user_id == blocklist.user_id:
        return False
    existing = db.session.query(BlocklistItem).filter_by(blocklist_id=blocklist.id, user_id=blocked_user_id).first()
    if existing:
        return False
    db.session.add(BlocklistItem(blocklist_id=blocklist.id, user_id=blocked_user_id))
    blocklist.num_blocked = (blocklist.num_blocked or 0) + 1
    blocklist.last_edit = utcnow()
    db.session.commit()

    _apply_item_to_subscribers(blocklist.id, blocked_user_id)

    if federate and blocklist.is_local():
        announce_blocklist_add_remove(blocklist, 'Add', blocked_user_id)
    return True


def remove_from_blocklist(blocklist: Blocklist, blocked_user_id: int, federate=True) -> bool:
    existing = db.session.query(BlocklistItem).filter_by(blocklist_id=blocklist.id, user_id=blocked_user_id).first()
    if not existing:
        return False

    _remove_item_from_subscribers(blocklist.id, blocked_user_id)

    db.session.query(BlocklistItem).filter_by(blocklist_id=blocklist.id, user_id=blocked_user_id).delete()
    blocklist.num_blocked = max((blocklist.num_blocked or 1) - 1, 0)
    blocklist.last_edit = utcnow()
    db.session.commit()

    if federate and blocklist.is_local():
        announce_blocklist_add_remove(blocklist, 'Remove', blocked_user_id)
    return True


def sync_remote_blocklist_items(blocklist: Blocklist):
    """Fetch the /following collection of a remote blocklist and make our copy match it."""
    from app.activitypub.util import find_actor_or_create
    if blocklist.is_local() or not blocklist.ap_following_url:
        return
    try:
        res = get_request(blocklist.ap_following_url, headers={'Accept': 'application/activity+json'})
        if res.status_code != 200:
            return
        collection = res.json()
        res.close()
    except Exception:
        return

    items = collection.get('items') or collection.get('orderedItems') or []
    remote_ids = set()
    for actor_url in items:
        if isinstance(actor_url, dict):
            actor_url = actor_url.get('id')
        if not actor_url:
            continue
        blocked = find_actor_or_create(actor_url)
        if blocked and isinstance(blocked, User):
            remote_ids.add(blocked.id)

    existing_ids = set(existing_blocklist_item_ids(blocklist.id))
    for added_id in remote_ids - existing_ids:
        add_to_blocklist(blocklist, added_id, federate=False)
    for removed_id in existing_ids - remote_ids:
        remove_from_blocklist(blocklist, removed_id, federate=False)

    blocklist.ap_fetched_at = utcnow()
    db.session.commit()


@celery.task
def sync_remote_blocklist_task(blocklist_id: int, then_apply_to_user_id: int | None = None):
    session = get_task_session()
    try:
        with patch_db_session(session):
            blocklist = session.query(Blocklist).get(blocklist_id)
            if blocklist:
                sync_remote_blocklist_items(blocklist)
                if then_apply_to_user_id:
                    apply_blocklist_to_user(blocklist.id, then_apply_to_user_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def sync_remote_blocklist(blocklist_id: int, then_apply_to_user_id: int | None = None):
    if current_app.debug:
        sync_remote_blocklist_task(blocklist_id, then_apply_to_user_id)
    else:
        sync_remote_blocklist_task.delay(blocklist_id, then_apply_to_user_id)


# ----- Subscribing / unsubscribing -----

def join_blocklist(blocklist: Blocklist, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id
    user = db.session.query(User).get(user_id)

    if blocklist.subscribed(user.id) != SUBSCRIPTION_NONMEMBER:
        if src == SRC_API:
            raise Exception('already_subscribed')
        flash(_('Already subscribed, or subscription pending'))
        return

    # Membership is created straight away (also for remote blocklists) so that the blocks take effect immediately.
    # For remote blocklists a Follow is sent too, so the remote instance will tell us about future changes to the list.
    db.session.add(BlocklistMember(user_id=user.id, blocklist_id=blocklist.id))
    blocklist.subscriptions_count = (blocklist.subscriptions_count or 0) + 1
    db.session.commit()

    apply_blocklist_to_user(blocklist.id, user.id)

    if not blocklist.is_local():
        join_request = BlocklistJoinRequest(user_id=user.id, blocklist_id=blocklist.id)
        db.session.add(join_request)
        db.session.commit()
        if blocklist.instance and blocklist.instance.online():
            follow = {
                "actor": user.public_url(),
                "to": [blocklist.public_url()],
                "object": blocklist.public_url(),
                "type": "Follow",
                "id": f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
            }
            send_post_request(blocklist.ap_inbox_url, follow, user.private_key, user.public_url() + '#main-key',
                              timeout=10)
        # our copy of the list might be out of date, refresh it (in the background, it could be large)
        sync_remote_blocklist(blocklist.id, then_apply_to_user_id=user.id)

    cache.delete_memoized(blocklist_membership, user, blocklist)

    if src == SRC_WEB:
        flash(_('You subscribed to %(blocklist_title)s', blocklist_title=blocklist.title))


def leave_blocklist(blocklist: Blocklist, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id
    user = db.session.query(User).get(user_id)

    membership = db.session.query(BlocklistMember).filter_by(user_id=user.id, blocklist_id=blocklist.id).first()
    join_request = db.session.query(BlocklistJoinRequest).filter_by(user_id=user.id, blocklist_id=blocklist.id).first()
    if membership is None and join_request is None:
        if src == SRC_API:
            raise Exception('not_subscribed')
        return
    if membership and membership.is_owner:
        if src == SRC_API:
            raise Exception('cannot_leave_own_blocklist')
        flash(_('You cannot unsubscribe from your own blocklist. Delete it instead.'), 'warning')
        return

    # Undo the Follow
    if not blocklist.is_local() and blocklist.instance and not blocklist.instance.gone_forever:
        follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}" if join_request \
            else f"{current_app.config['SERVER_URL']}/activities/follow/{gibberish(15)}"
        follow = {
            "actor": user.public_url(),
            "to": [blocklist.public_url()],
            "object": blocklist.public_url(),
            "type": "Follow",
            "id": follow_id
        }
        undo = {
            'actor': user.public_url(),
            'to': [blocklist.public_url()],
            'type': 'Undo',
            'id': f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15),
            'object': follow
        }
        send_post_request(blocklist.ap_inbox_url, undo, user.private_key, user.public_url() + '#main-key', timeout=10)

    db.session.query(BlocklistMember).filter_by(user_id=user.id, blocklist_id=blocklist.id).delete()
    db.session.query(BlocklistJoinRequest).filter_by(user_id=user.id, blocklist_id=blocklist.id).delete()
    if membership:
        blocklist.subscriptions_count = max((blocklist.subscriptions_count or 1) - 1, 0)
    db.session.commit()

    unapply_blocklist_from_user(blocklist.id, user.id)
    cache.delete_memoized(blocklist_membership, user, blocklist)

    if src == SRC_WEB:
        flash(_('You have unsubscribed from %(blocklist_title)s', blocklist_title=blocklist.title))


# ----- Creating / editing / deleting -----

def make_blocklist(input, src, auth=None) -> tuple[Blocklist, List[str]]:
    """Returns the new blocklist and a list of the lines in 'members' that could not be resolved to an account"""
    if src == SRC_API:
        machine_name = input['url']
        title = input['title']
        description = input.get('description') or ''
        members = input.get('members') or ''
        user = authorise_api_user(auth, return_type='model')
    else:
        machine_name = input.url.data
        title = input.title.data
        description = input.description.data or ''
        members = input.members.data or ''
        user = current_user

    machine_name = machine_name.strip().lower()
    server_url = current_app.config['SERVER_URL']
    private_key, public_key = RsaKeys.generate_keypair()
    blocklist = Blocklist(user_id=user.id, title=title, machine_name=machine_name,
                          description=piefed_markdown_to_lemmy_markdown(description),
                          description_html=markdown_to_html(description),
                          private_key=private_key, public_key=public_key,
                          ap_profile_id=f'{server_url}/b/{machine_name}',
                          ap_public_url=f'{server_url}/b/{machine_name}',
                          ap_followers_url=f'{server_url}/b/{machine_name}/followers',
                          ap_following_url=f'{server_url}/b/{machine_name}/following',
                          ap_outbox_url=f'{server_url}/b/{machine_name}/outbox',
                          ap_inbox_url=f'{server_url}/b/{machine_name}/inbox',
                          ap_moderators_url=f'{server_url}/b/{machine_name}/moderators',
                          ap_domain=current_app.config['SERVER_NAME'],
                          subscriptions_count=1, num_blocked=0, instance_id=1)
    db.session.add(blocklist)
    db.session.commit()

    db.session.add(BlocklistMember(user_id=user.id, blocklist_id=blocklist.id, is_owner=True))
    db.session.commit()

    from app.blocklist.util import form_members_to_ids
    new_member_ids, unresolved = form_members_to_ids(members)
    for member_id in new_member_ids:
        add_to_blocklist(blocklist, member_id, federate=False)  # nobody remote is subscribed yet

    cache.delete_memoized(blocklist_membership, user, blocklist)
    return blocklist, unresolved


def edit_blocklist(input, blocklist: Blocklist, src, auth=None) -> List[str]:
    """Returns a list of the lines in 'members' that could not be resolved to an account"""
    if src == SRC_API:
        title = input['title']
        description = input.get('description') or ''
        members = input.get('members')
        user = authorise_api_user(auth, return_type='model')
    else:
        title = input.title.data
        description = input.description.data or ''
        members = input.members.data
        user = current_user

    if not (blocklist.user_id == user.id or user.is_admin()):
        raise Exception('incorrect_login')

    blocklist.title = title
    blocklist.description = piefed_markdown_to_lemmy_markdown(description)
    blocklist.description_html = markdown_to_html(description)
    blocklist.last_edit = utcnow()
    db.session.commit()

    unresolved = []
    if members is not None:
        from app.blocklist.util import form_members_to_ids
        old_ids = set(existing_blocklist_item_ids(blocklist.id))
        new_ids, unresolved = form_members_to_ids(members)
        for added_id in new_ids - old_ids:
            add_to_blocklist(blocklist, added_id)
        for removed_id in old_ids - new_ids:
            remove_from_blocklist(blocklist, removed_id)
    return unresolved


def delete_blocklist(blocklist: Blocklist, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    if not (blocklist.user_id == user.id or user.is_admin()):
        raise Exception('incorrect_login')

    # tell remote subscribers. Done before BlocklistMembers are deleted as we need them to know where to send it.
    if blocklist.is_local():
        owner = db.session.query(User).get(blocklist.user_id)
        delete_json = {
            "@context": default_context(),
            "type": "Delete",
            "actor": owner.public_url(),
            "id": f"{current_app.config['SERVER_URL']}/activities/delete/{gibberish(15)}",
            "object": {
                "type": "Blocklist",
                "id": blocklist.public_url()
            }
        }
        for inbox in _remote_subscriber_inboxes(blocklist):
            send_post_request(inbox, delete_json, owner.private_key, owner.public_url() + '#main-key', timeout=10)

    remove_blocklist_locally(blocklist)


def remove_blocklist_locally(blocklist: Blocklist):
    """Delete a blocklist from the db, after taking away the blocks it gave to local subscribers.
    Used when a local user deletes their blocklist, or a remote instance tells us one of its blocklists was deleted."""
    for subscriber_id in _local_subscriber_ids(blocklist.id):
        unapply_blocklist_from_user(blocklist.id, subscriber_id)
        cache.delete_memoized(blocklist_membership, db.session.query(User).get(subscriber_id), blocklist)
    db.session.query(BlocklistMember).filter(BlocklistMember.blocklist_id == blocklist.id).delete()
    db.session.query(BlocklistJoinRequest).filter(BlocklistJoinRequest.blocklist_id == blocklist.id).delete()
    db.session.query(BlocklistItem).filter(BlocklistItem.blocklist_id == blocklist.id).delete()
    db.session.delete(blocklist)
    db.session.commit()


# ----- Federation -----

def _remote_subscriber_inboxes(blocklist: Blocklist) -> List[str]:
    inboxes = []
    for instance in blocklist.following_instances():
        if instance.inbox and instance.online() and not instance_banned(instance.domain):
            inboxes.append(instance.inbox)
    return inboxes


def announce_blocklist_add_remove(blocklist: Blocklist, action: str, blocked_user_id: int):
    """Tell instances that have users subscribed to this (local) blocklist that an account was added or removed."""
    inboxes = _remote_subscriber_inboxes(blocklist)
    if not inboxes:
        return
    blocked_user = db.session.query(User).get(blocked_user_id)
    if blocked_user is None:
        return
    server_url = current_app.config['SERVER_URL']
    activity_json = {
        "@context": default_context(),
        "type": "Announce",
        "actor": blocklist.public_url(),
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [blocklist.ap_followers_url],
        "id": f"{server_url}/activities/announce/{gibberish(15)}",
        "object": {
            "type": action,
            "actor": blocklist.public_url(),
            "id": f"{server_url}/activities/{action.lower()}/{gibberish(15)}",
            "object": {
                "type": "Person",
                "id": blocked_user.public_url()
            },
            "target": {
                "type": "Collection",
                "id": blocklist.ap_following_url
            }
        }
    }
    for inbox in inboxes:
        send_post_request(inbox, activity_json, blocklist.private_key, blocklist.public_url() + '#main-key', timeout=10)


def existing_blocklist_item_ids(blocklist_id: int) -> List[int]:
    return list(db.session.execute(text('SELECT user_id FROM "blocklist_item" WHERE blocklist_id = :blocklist_id'),
                                   {'blocklist_id': blocklist_id}).scalars())
