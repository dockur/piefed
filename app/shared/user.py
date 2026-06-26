from flask import flash, current_app
from flask_babel import _
from flask_login import current_user
from sqlalchemy import text

from app import db, cache
from app.constants import *
from app.models import UserBlock, NotificationSubscription, User, IpBan, UserFollower, Notification, BotChallenge, \
    Conversation
from app.shared.tasks import task_selector
from app.user.utils import purge_user_then_delete
from app.utils import authorise_api_user, blocked_users, render_template, add_to_modlog, gibberish


# only called from API for now, but can be called from web using [un]block_another_user(user.id, SRC_WEB)

# user_id: the local, logged-in user
# person_id: the person they want to block

def block_another_user(person_id, src, auth=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    if user_id == person_id:
        if src == SRC_API:
            raise Exception('cannot_block_self')
        else:
            flash(_('You cannot block yourself.'), 'error')
            return

    role = db.session.execute(text('SELECT role_id FROM "user_role" WHERE user_id = :person_id'),
                              {'person_id': person_id}).scalar()
    if role == ROLE_ADMIN or role == ROLE_STAFF:
        if src == SRC_API:
            raise Exception('cannot_block_admin_or_staff')
        else:
            flash(_('You cannot block admin or staff.'), 'error')
            return

    existing_block = db.session.query(UserBlock).filter_by(blocker_id=user_id, blocked_id=person_id).first()
    if not existing_block:
        block = UserBlock(blocker_id=user_id, blocked_id=person_id)
        db.session.add(block)
        db.session.execute(
            text('DELETE FROM "notification_subscription" WHERE entity_id = :current_user AND user_id = :user_id'),
            {'current_user': user_id, 'user_id': person_id})
        db.session.commit()

        cache.delete_memoized(blocked_users, user_id)

    # Nothing to fed? (Lemmy doesn't federate anything to the blocked person)

    if src == SRC_API:
        return user_id
    else:
        return  # let calling function handle confirmation flash message and redirect


def unblock_another_user(person_id, src, auth=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    if user_id == person_id:
        if src == SRC_API:
            raise Exception('cannot_unblock_self')
        else:
            flash(_('You cannot unblock yourself.'), 'error')
            return

    existing_block = db.session.query(UserBlock).filter_by(blocker_id=user_id, blocked_id=person_id).first()
    if existing_block:
        db.session.delete(existing_block)
        db.session.commit()

        cache.delete_memoized(blocked_users, user_id)

    # Nothing to fed? (Lemmy doesn't federate anything to the unblocked person)

    if src == SRC_API:
        return user_id
    else:
        return  # let calling function handle confirmation flash message and redirect


def subscribe_user(person_id: int, subscribe, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id
    person = db.session.query(User).filter_by(id=person_id, banned=False).one()

    if src == SRC_WEB:
        subscribe = False if person.notify_new_posts(user_id) else True

    existing_notification = NotificationSubscription.query.filter_by(entity_id=person_id, user_id=user_id,
                                                                     type=NOTIF_USER).first()
    if subscribe == False:
        if existing_notification:
            db.session.delete(existing_notification)
            db.session.commit()
        else:
            msg = 'A subscription for this user did not exist.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))

    else:
        if existing_notification:
            msg = 'A subscription for this user already existed.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))
        else:
            if person.id == user_id:
                msg = 'Target must be a another user.'
                if src == SRC_API:
                    raise Exception(msg)
                else:
                    flash(_(msg))
            if person.has_blocked_user(user_id):
                msg = 'This user has blocked you.'
                if src == SRC_API:
                    raise Exception(msg)
                else:
                    flash(_(msg))
            new_notification = NotificationSubscription(name=person.display_name(), user_id=user_id,
                                                        entity_id=person_id, type=NOTIF_USER)
            db.session.add(new_notification)
            db.session.commit()

    if src == SRC_API:
        return user_id
    else:
        return render_template('user/_notification_toggle.html', user=person)


def ban_user(input, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
        to_ban: User = db.session.query(User).get(input['person_id'])
        purge_content = input['purge_content']
        ban_ip_address = input['ban_ip_address']
        reason = input['reason']
        flush_cdn = False
    else:
        user = current_user
        to_ban = db.session.query(User).get(input.person_id)
        purge_content = input.purge.data
        ban_ip_address = input.ip_address.data
        reason = input.reason.data
        flush_cdn = input.flush.data
    to_ban.banned = True
    db.session.commit()

    # Purge content
    if purge_content:
        if SRC_WEB:
            if to_ban.is_instance_admin():
                flash(_('Purged user was a remote instance admin.'), 'warning')
            if to_ban.is_admin() or to_ban.is_staff():
                flash(_('Purged user with role permissions.'), 'warning')

        # federate deletion
        if to_ban.is_local():
            to_ban.deleted_by = user.id
            purge_user_then_delete(to_ban.id, flush=flush_cdn)
            if SRC_WEB:
                flash(_('%(actor)s has been banned, deleted and all their content deleted. This might take a few minutes.',
                        actor=to_ban.display_name()))
        else:
            to_ban.delete_dependencies()
            to_ban.purge_content(flush=flush_cdn)
            from app import redis_client
            with redis_client.lock(f"lock:user:{to_ban.id}", timeout=10, blocking_timeout=6):
                to_ban = User.query.get(to_ban.id)
                to_ban.deleted = True
                to_ban.deleted_by = user.id
                db.session.commit()
            if SRC_WEB:
                flash(_('%(actor)s has been banned, deleted and all their content deleted.', actor=to_ban.display_name()))

        add_to_modlog('delete_user', actor=user, target_user=to_ban, reason=reason,
                      link_text=to_ban.display_name(), link=to_ban.link())
    else:
        add_to_modlog('ban_user', actor=user, target_user=to_ban, reason=reason,
                      link_text=to_ban.display_name(), link=to_ban.link())

        if SRC_WEB:
            if to_ban.is_instance_admin():
                flash(_('Banned user was a remote instance admin.'), 'warning')
            if to_ban.is_admin() or to_ban.is_staff():
                flash(_('Banned user with role permissions.'), 'warning')
            else:
                flash(_('%(actor)s has been banned.', actor=to_ban.display_name()))

    # IP address ban
    if ban_ip_address and to_ban.ip_address:
        existing_ip_ban = IpBan.query.filter(IpBan.ip_address == to_ban.ip_address).first()
        if not existing_ip_ban:
            db.session.add(IpBan(ip_address=to_ban.ip_address, notes=reason))
            db.session.commit()

    task_selector('ban_from_site', user_id=to_ban.id, mod_id=user.id, expiry=None, reason=reason, remove_data=purge_content and to_ban.is_local())


def unban_user(input, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
        to_unban: User = db.session.query(User).get(input['person_id'])
    else:
        user = current_user
        to_unban: User = db.session.query(User).get(input['person_id'])

    to_unban.banned = False
    to_unban.deleted = False
    db.session.commit()

    task_selector('unban_from_site', user_id=to_unban.id, mod_id=user.id, expiry=None,
                  reason='')

    add_to_modlog('unban_user', actor=user, target_user=to_unban, link_text=to_unban.display_name(), link=to_unban.link())


def follow_user(follow_id: int, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    to_follow: User = db.session.query(User).get(follow_id)

    is_accepted = False
    if to_follow.is_local():
        if to_follow.ap_manually_approves_followers is True:
            is_accepted = False
        else:
            is_accepted = True
            user.num_following += 1
            to_follow.num_followers += 1

    db.session.add(UserFollower(local_user_id=user.id, remote_user_id=follow_id, is_accepted=is_accepted, is_inward=False))
    db.session.commit()

    if to_follow.is_local():
        if not is_accepted:
            targets_data = {'gen': '0',
                            'author_id': user.id,
                            'author_user_name': user.ap_id if user.ap_id else user.user_name}
            new_notification = Notification(title=_('Someone wants to follow you'), url=f"/user/{user.id}",
                                            user_id=to_follow.id, author_id=user.id,
                                            notif_type=NOTIF_FOLLOW,
                                            subtype='new_follower',
                                            targets=targets_data)
        else:
            targets_data = {'gen': '0',
                            'author_id': user.id,
                            'author_user_name': user.ap_id if user.ap_id else user.user_name}
            new_notification = Notification(title=_('You have a new follower'), url=f"/user/{user.id}",
                                            user_id=to_follow.id, author_id=user.id,
                                            notif_type=NOTIF_FOLLOW,
                                            subtype='new_follower',
                                            targets=targets_data)
        db.session.add(new_notification)
        to_follow.unread_notifications += 1
        db.session.commit()

    else:
        task_selector('follow_user', to_follow_id=follow_id, user_id=user.id)


def unfollow_user(follow_id: int, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    to_unfollow: User = db.session.query(User).get(follow_id)

    user.num_following -= 1
    to_unfollow.num_followers -= 1

    db.session.execute(text(
        'DELETE FROM "user_follower" WHERE local_user_id = :local_user_id AND remote_user_id = :remote_user_id'),
            {'local_user_id': user.id, 'remote_user_id': to_unfollow.id})
    db.session.commit()

    if not to_unfollow.is_local():
        task_selector('unfollow_user', to_follow_id=follow_id, user_id=user.id)
    else:
        db.session.execute(text(
            'DELETE FROM "user_follow_request" WHERE user_id = :user_id AND follow_id = :follow_id'),
                {'user_id': user.id, 'follow_id': to_unfollow.id})
        db.session.commit()


def bot_challenge_user(user_id: int, src, auth=None):
    from app.chat.util import send_message

    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    recipient = db.session.query(User).get(user_id)
    existing_challenge = BotChallenge.query.filter(BotChallenge.user_id == user_id).first()
    if existing_challenge:
        if existing_challenge.is_a_bot is False:
            raise Exception('This person has already responded to the challenge')
        uuid = existing_challenge.uuid
    else:
        uuid = gibberish(49)

    conversation = Conversation(user_id=user.id)
    conversation.members.append(recipient)
    conversation.members.append(current_user)
    db.session.add(conversation)
    db.session.commit()

    challenge_text = """Hi there,

We noticed some unusual behaviour coming from your account and are starting to wonder if it is a bot or a human.

If you are NOT using scripts, LLMs or other automation to create posts and comments, please visit this link:

"""
    challenge_text += f"{current_app.config['SERVER_URL']}/bot_challenge/{uuid}\n\n"
    challenge_text += f"If this account is run by a bot, in part or fully, do nothing and we will automatically flag it as a bot.\n\nThank you"
    send_message(challenge_text, conversation.id)

    if existing_challenge is None:
        db.session.add(BotChallenge(user_id=recipient.id, sent_by=user.id, uuid=uuid))
        db.session.commit()
