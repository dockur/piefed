import random
import time

from flask import current_app
from flask_login import current_user
from sqlalchemy import text, desc, or_

from app import celery, db
from app.activitypub.signature import signed_get_request, send_post_request
from app.activitypub.util import actor_json_to_model
from app.constants import POST_STATUS_REVIEWING
from app.models import User, CommunityMember, Community, Site, BannedInstances, Post, \
    PostVote, ArchivedPostReply, PostReply, UserNote
from app.shared.tasks import task_selector
from app.utils import gibberish, get_request, get_task_session, patch_db_session, \
    intlist_to_strlist, community_membership_private

import httpx


def purge_user_then_delete(user_id, flush=True):
    if current_app.debug:
        purge_user_then_delete_task(user_id, flush)
    else:
        purge_user_then_delete_task.delay(user_id, flush)


@celery.task
def purge_user_then_delete_task(user_id, flush):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                user = session.query(User).get(user_id)
                if user:
                    # posts
                    for post in user.posts:
                        task_selector('delete_post', user_id=user.id, post_id=post.id)
                    for reply in user.post_replies:
                        task_selector('delete_reply', user_id=user.id, reply_id=reply.id)

                    # unsubscribe
                    communities = session.query(CommunityMember).filter_by(user_id=user_id).all()
                    for membership in communities:
                        community = session.query(Community).get(membership.community_id)
                        unsubscribe_from_community(community, user)

                    user.delete_dependencies()
                    user.purge_content(flush)
                    from app import redis_client
                    with redis_client.lock(f"lock:user:{user.id}", timeout=10, blocking_timeout=6):
                        user = session.query(User).get(user_id)
                        user.deleted = True
                        session.commit()

        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def unsubscribe_from_community(community, user):
    if community.instance.gone_forever or community.instance.dormant:
        return

    undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15)
    follow = {
        "actor": user.public_url(),
        "to": [community.public_url()],
        "object": community.public_url(),
        "type": "Follow",
        "id": f"{current_app.config['SERVER_URL']}/activities/follow/{gibberish(15)}"
    }
    undo = {
        'actor': user.public_url(),
        'to': [community.public_url()],
        'type': 'Undo',
        'id': undo_id,
        'object': follow
    }
    send_post_request(community.ap_inbox_url, undo, user.private_key, user.public_url() + '#main-key')


def search_for_user(address: str, allow_fetch: bool = True):
    if address.startswith('@'):
        address = address[1:]
    if '@' in address:
        name, server = address.lower().split('@')
    else:
        name = address
        server = ''

    if server:
        banned = BannedInstances.query.filter_by(domain=server).first()
        if banned:
            reason = f" Reason: {banned.reason}" if banned.reason is not None else ''
            raise Exception(f"{server} is blocked.{reason}")
        already_exists = User.query.filter_by(ap_id=address).first()
    else:
        already_exists = User.query.filter_by(user_name=name, ap_id=None).first()
    
    if already_exists:
        return already_exists
    elif not allow_fetch:
        return None

    if not server:
        return None

    # Look up the profile address of the user using WebFinger
    # todo: try, except block around every get_request
    webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                 params={'resource': f"acct:{address}"})
    if webfinger_data.status_code == 200:
        try:
            webfinger_json = webfinger_data.json()
            webfinger_data.close()
        except:
            webfinger_data.close()
            return None
        for links in webfinger_json['links']:
            if 'rel' in links and links['rel'] == 'self':  # this contains the URL of the activitypub profile
                type = links['type'] if 'type' in links else 'application/activity+json'
                # retrieve the activitypub profile
                for attempt in [1,2]:
                    try:
                        object_request = get_request(links['href'], headers={'Accept': type})
                    except httpx.HTTPError:
                        if attempt == 1:
                            time.sleep(3 + random.randrange(3))
                        else:
                            return None
                if object_request.status_code == 401:
                    site = Site.query.get(1)
                    for attempt in [1,2]:
                        try:
                            object_request = signed_get_request(links['href'], site.private_key, f"{current_app.config['SERVER_URL']}/actor#main-key")
                        except httpx.HTTPError:
                            if attempt == 1:
                                time.sleep(3)
                            else:
                                return None
                if object_request.status_code == 200:
                    try:
                        object = object_request.json()
                        object_request.close()
                    except:
                        object_request.close()
                        return None
                else:
                    return None

                if object['type'] == 'Person' or object['type'] == 'Service':
                    user = actor_json_to_model(object, name, server)
                    return user

    return None


def _get_user_posts(user, post_page):
    """Get posts for a user based on current user's permissions."""
    base_query = Post.query.filter_by(user_id=user.id).filter(Post.community_id.not_in(community_membership_private(user.id)))

    if current_user.is_authenticated and (current_user.is_admin() or current_user.is_staff()):
        # Admins see everything
        return base_query.order_by(desc(Post.posted_at)).paginate(page=post_page, per_page=20, error_out=False)
    elif current_user.is_authenticated and current_user.id == user.id:
        # Users see their own posts including soft-deleted ones they deleted
        return base_query.filter(
            or_(Post.deleted == False, Post.status > POST_STATUS_REVIEWING, Post.deleted_by == user.id)
        ).order_by(desc(Post.posted_at)).paginate(page=post_page, per_page=20, error_out=False)
    else:
        # Everyone else sees only public, non-deleted posts
        return base_query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING, Post.private == False).order_by(
            desc(Post.posted_at)).paginate(page=post_page, per_page=20, error_out=False)


def _get_user_post_replies(user, replies_page):
    """Get post replies for a user based on current user's permissions."""
    base_query = PostReply.query.filter_by(user_id=user.id).filter(PostReply.community_id.not_in(community_membership_private(user.id)))

    if current_user.is_authenticated and (current_user.is_admin() or current_user.is_staff()):
        # Admins see everything
        return base_query.order_by(desc(PostReply.posted_at)).paginate(page=replies_page, per_page=20, error_out=False)
    elif current_user.is_authenticated and current_user.id == user.id:
        # Users see their own replies including soft-deleted ones they deleted
        return base_query.filter(or_(PostReply.deleted == False, PostReply.deleted_by == user.id)).order_by(
            desc(PostReply.posted_at)).paginate(page=replies_page, per_page=20, error_out=False)
    else:
        # Everyone else sees only non-deleted replies
        return base_query.filter(PostReply.deleted == False, PostReply.private == False).order_by(
            desc(PostReply.posted_at)).paginate(page=replies_page, per_page=20, error_out=False)


def _get_user_posts_and_replies(user, page):
    """Get list of posts and replies in reverse chronological order based on current user's permissions"""
    returned_list = []
    user_id = user.id
    per_page = 20
    offset_val = (page - 1) * per_page
    next_page = False

    private = ','.join(intlist_to_strlist(community_membership_private(user_id)))
    if current_user.is_authenticated and (current_user.is_admin() or current_user.is_staff()):
        # Admins see everything
        post_select = f"SELECT id, posted_at, 'post' AS type FROM post WHERE user_id = {user_id}"
        reply_select = f"SELECT id, posted_at, 'reply' AS type FROM post_reply WHERE user_id = {user_id}"
        if private:
            post_select += f" AND community_id NOT IN ({private})"
            reply_select += f" AND community_id NOT IN ({private})"
    elif current_user.is_authenticated and current_user.id == user_id:
        # Users see their own posts/replies including soft-deleted ones they deleted
        post_select = f"SELECT id, posted_at, 'post' AS type FROM post WHERE user_id = {user_id} AND (deleted = 'False' OR deleted_by = {user_id})"
        reply_select = f"SELECT id, posted_at, 'reply' AS type FROM post_reply WHERE user_id={user_id} AND (deleted = 'False' OR deleted_by = {user_id})"
        if private:
            post_select += f" AND community_id NOT IN ({private})"
            reply_select += f" AND community_id NOT IN ({private})"
    else:
        # Everyone else sees only non-deleted posts/replies
        post_select = f"SELECT id, posted_at, 'post' AS type FROM post WHERE user_id = {user_id} AND deleted = 'False' and status > {POST_STATUS_REVIEWING}"
        reply_select = f"SELECT id, posted_at, 'reply' AS type FROM post_reply WHERE user_id={user_id} AND deleted = 'False'"
        if private:
            post_select += f" AND community_id NOT IN ({private})"
            reply_select += f" AND community_id NOT IN ({private})"

    full_query = post_select + " UNION " + reply_select + f" ORDER BY posted_at DESC LIMIT {per_page + 1} OFFSET {offset_val};"
    query_result = db.session.execute(text(full_query))

    for row in query_result:
        if row.type == "post":
            returned_list.append(Post.query.get(row.id))
        elif row.type == "reply":
            returned_list.append(PostReply.query.get(row.id))

    if len(returned_list) > per_page:
        next_page = True
        returned_list = returned_list[:-1]

    return (returned_list, next_page)


def _get_user_archived_replies(user):
    return ArchivedPostReply.query.filter(ArchivedPostReply.user_id == user.id).\
        order_by(desc(ArchivedPostReply.created_at)).all()


def _get_user_moderates(user):
    """Get communities moderated by user."""

    moderates = Community.query.filter_by(banned=False).join(CommunityMember).filter(
        CommunityMember.user_id == user.id). \
        filter(or_(CommunityMember.is_moderator, CommunityMember.is_owner)). \
        order_by(Community.name)

    # Hide private mod communities unless user is admin or viewing their own profile
    if current_user.is_anonymous or (user.id != current_user.id and not current_user.is_admin()):
        moderates = moderates.filter(Community.private_mods == False)

    return moderates.all()


def _get_user_same_ip(user):
    """Get users that have the same IP address as this user"""

    if current_user.is_anonymous or user.ip_address is None or user.ip_address == '':
        return []

    return User.query.filter_by(ip_address=user.ip_address).filter(User.ap_id == None, User.id != user.id).all()


def _get_user_upvoted_posts(user):
    """Get posts upvoted by user (only for user themselves or admins)."""
    if current_user.is_authenticated and (user.id == current_user.get_id() or current_user.is_admin()):
        return Post.query.join(PostVote, PostVote.post_id == Post.id).filter(PostVote.effect > 0, PostVote.user_id == user.id). \
            order_by(desc(PostVote.id)).limit(50).all()
    return []


def _get_user_subscribed_communities(user):
    """Get communities subscribed to by user."""
    if current_user.is_authenticated and (user.id == current_user.get_id()
                                          or current_user.is_staff() or current_user.is_admin()
                                          or user.show_subscribed_communities):
        return Community.query.filter_by(banned=False).join(CommunityMember).filter(CommunityMember.user_id == user.id).order_by(Community.name).all()
    return []


def insert_or_update_user_note(text, user):
    usernote = UserNote.query.filter(UserNote.target_id == user.id, UserNote.user_id == current_user.id).first()
    if usernote:
        usernote.body = text
    else:
        usernote = UserNote(target_id=user.id, user_id=current_user.id, body=text)
        db.session.add(usernote)
    db.session.commit()
