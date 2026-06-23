from app import cache, celery, db
from app.constants import *
from app.activitypub.signature import default_context, post_request, send_post_request
from app.models import Community, CommunityBan, CommunityJoinRequest, User, Feed, FeedJoinRequest, UserFollowRequest
from app.utils import community_membership, gibberish, joined_communities, instance_banned, get_task_session, \
    feed_membership, menu_subscribed_feeds, patch_db_session

from flask import current_app, flash
from markupsafe import Markup
from flask_babel import _



""" JSON format
{
  'id':
  'type':
  'actor':
  'object':
  '@context':   (outer object only)
  'to': []
}
"""

"""
async:
    delete memoized
    add or delete community_join_request
    used for admin preload in production (return values are ignored)
    used for API
sync:
    add or delete community_member
    used for debug mode
    used for web users to provide feedback
"""


@celery.task
def join_community(send_async, user_id, community_id, src):
    session = get_task_session()
    try:
        with patch_db_session(session):
            user = session.query(User).get(user_id)
            community = session.query(Community).filter_by(id=community_id).one()

            pre_load_message = {}
            banned = session.query(CommunityBan).filter_by(user_id=user_id, community_id=community_id).first()
            if banned:
                if not send_async:
                    if src == SRC_WEB:
                        flash(_('You cannot join this community'))
                        return
                    elif src == SRC_PLD:
                        pre_load_message['user_banned'] = True
                        return pre_load_message
                    elif src == SRC_API:
                        raise Exception('banned_from_community')
                return

            if (not community.is_local() and
                (user.has_blocked_instance(community.instance.id) or
                 instance_banned(community.instance.domain))):
                if not send_async:
                    if src == SRC_WEB:
                        flash(_('Community is on banned or blocked instance'))
                        return
                    elif src == SRC_PLD:
                        pre_load_message['community_on_banned_or_blocked_instance'] = True
                        return pre_load_message
                    elif src == SRC_API:
                        raise Exception('community_on_banned_or_blocked_instance')
                return

            if not community.is_local() and community.instance.online():
                join_request = CommunityJoinRequest(user_id=user_id, community_id=community_id)
                session.add(join_request)
                session.commit()

                follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
                follow = {
                  'id': follow_id,
                  'type': 'Follow',
                  'actor': user.public_url(),
                  'object': community.public_url(),
                  '@context': default_context(),
                  'to': [community.public_url()],
                }
                send_post_request(community.ap_inbox_url, follow, user.private_key,
                                  user.public_url() + '#main-key', timeout=10)

            # for communities on local or offline instances, joining is instant
            cache.delete_memoized(community_membership, user, community)
            cache.delete_memoized(joined_communities, user.id)

            if src == SRC_WEB:
                flash(Markup(_('You joined %(community_name)s',
                               community_name=f'<a href="/c/{community.link()}">{community.display_name()}</a>')))
                return
            elif src == SRC_PLD:
                pre_load_message['status'] = 'joined'
                return pre_load_message

            return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def leave_community(send_async, user_id, community_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            user = session.query(User).get(user_id)
            community = session.query(Community).filter_by(id=community_id).one()

            cache.delete_memoized(community_membership, user, community)
            cache.delete_memoized(joined_communities, user.id)

            if community.is_local():
                return

            join_request = session.query(CommunityJoinRequest).filter_by(user_id=user_id, community_id=community_id).first()
            session.delete(join_request)
            session.commit()

            if (not community.instance.online() or
               user.has_blocked_instance(community.instance.id) or
               instance_banned(community.instance.domain)):
                return

            follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
            follow = {
              'id': follow_id,
              'type': 'Follow',
              'actor': user.public_url(),
              'object': community.public_url(),
              'to': [community.public_url()]
            }
            undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/{gibberish(15)}"
            undo = {
              'id': undo_id,
              'type': 'Undo',
              'actor': user.public_url(),
              'object': follow,
              '@context': default_context(),
              'to': [community.public_url()]
            }

            send_post_request(community.ap_inbox_url, undo, user.private_key, user.public_url() + '#main-key', timeout=10)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def leave_feed(send_async, user_id, feed_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            user = session.query(User).get(user_id)
            feed = session.query(Feed).filter_by(id=feed_id).one()

            cache.delete_memoized(feed_membership, user, feed)
            cache.delete_memoized(menu_subscribed_feeds, user_id)
            cache.delete_memoized(joined_communities, user_id)

            if feed.is_local():
                return

            join_request = session.query(FeedJoinRequest).filter_by(user_id=user_id, feed_id=feed_id).first()
            if join_request:
                uuid = join_request.uuid
            session.query(FeedJoinRequest).filter_by(user_id=user_id, feed_id=feed_id).delete()
            session.commit()

            if (not feed.instance.online()
                or user.has_blocked_instance(feed.instance.id)
                or instance_banned(feed.instance.domain)):
                return

            # This code is based on feed.feed_unsubscribe and leave_community above
            if not feed.instance.gone_forever:
                follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{uuid}"
                undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15)
                follow = {
                    "actor": user.public_url(),
                    "to": [feed.public_url()],
                    "object": feed.public_url(),
                    "type": "Follow",
                    "id": follow_id
                }
                undo = {
                    'actor': user.public_url(),
                    'to': [feed.public_url()],
                    'type': 'Undo',
                    'id': undo_id,
                    'object': follow
                }
                send_post_request(feed.ap_inbox_url, undo, user.private_key,
                                    user.public_url() + '#main-key', timeout=10)

    except Exception:
        session.rollback()
    finally:
        session.close()


@celery.task
def follow_user(to_follow_id, user_id, send_async=True):
    session = get_task_session()
    try:
        to_follow: User = session.query(User).get(to_follow_id)
        user: User = session.query(User).get(user_id)
        if not to_follow.is_local() and to_follow.instance.online():
            join_request = UserFollowRequest(user_id=user_id, follow_id=to_follow_id)
            session.add(join_request)
            session.commit()

            to_follow_ap_id = f"{current_app.config['SERVER_URL']}/activities/follow_user/{join_request.uuid}"
            follow = {
                'id': to_follow_ap_id,
                'type': 'Follow',
                'actor': user.public_url(),
                'object': to_follow.public_url(),
                '@context': default_context(),
                'to': [to_follow.public_url()],
            }
            send_post_request(to_follow.ap_inbox_url, follow, user.private_key, user.public_url() + '#main-key')
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def unfollow_user(to_follow_id, user_id, send_async=True):
    session = get_task_session()
    try:
        to_follow: User = session.query(User).get(to_follow_id)
        user: User = session.query(User).get(user_id)
        if not to_follow.is_local() and to_follow.instance.online():
            join_request = session.query(UserFollowRequest).filter(UserFollowRequest.user_id == int(user_id),
                                                                   UserFollowRequest.follow_id == int(to_follow_id)).first()
            if join_request:
                session.delete(join_request)
                session.commit()
                to_follow_ap_id = f"{current_app.config['SERVER_URL']}/activities/follow_user/{join_request.uuid}"
            else:
                to_follow_ap_id = f"{current_app.config['SERVER_URL']}/activities/follow_user/{gibberish(15)}"
            undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15)
            follow = {
                'id': to_follow_ap_id,
                'type': 'Follow',
                'actor': user.public_url(),
                'object': to_follow.public_url(),
                '@context': default_context(),
                'to': [to_follow.public_url()],
            }
            undo = {
                'actor': user.public_url(),
                'to': [to_follow.public_url()],
                'type': 'Undo',
                'id': undo_id,
                'object': follow
            }
            send_post_request(to_follow.ap_inbox_url, undo, user.private_key, user.public_url() + '#main-key')
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
