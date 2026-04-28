from datetime import timedelta
from typing import List

from flask import g
from flask_login import current_user
from sqlalchemy import desc, text

from app import cache, db
from app.constants import POST_STATUS_REVIEWING, SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, \
    SUBSCRIPTION_MODERATOR
from app.models import Community, utcnow, BannedInstances, Instance
from app.utils import blocked_communities, communities_banned_from, blocked_or_banned_instances, get_setting, \
    joined_or_modding_communities, pending_communities


@cache.memoize(timeout=10)
def sidebar_active_communities(user_id: int) -> List:
    if user_id == 0:
        return Community.query.filter_by(banned=False, nsfw=False, nsfl=False, private=False, show_all=True).order_by(desc(Community.last_active)).limit(5).all()
    q = Community.query.filter_by(banned=False, nsfw=False, nsfl=False, private=False, show_all=True)
    banned = communities_banned_from(user_id)
    if banned:
        q = q.filter(Community.id.not_in(banned))
    blocked = blocked_communities(user_id)
    if blocked:
        q = q.filter(Community.id.not_in(blocked))
    blocked_instances = blocked_or_banned_instances(user_id)
    if blocked_instances:
        q = q.filter(Community.instance_id.not_in(blocked_instances))
    return q.order_by(desc(Community.last_active)).limit(5).all()


@cache.memoize(timeout=60)
def sidebar_new_communities(user_id: int) -> List:
    cutoff = utcnow() - timedelta(days=30)
    if user_id == 0:
        return Community.query.filter_by(banned=False, nsfw=False, nsfl=False, private=False, show_all=True).filter(Community.created_at > cutoff).order_by(desc(Community.first_federated_at)).order_by(desc(Community.created_at)).limit(5).all()
    q = Community.query.filter_by(banned=False, nsfw=False, nsfl=False, private=False, show_all=True).filter(Community.created_at > cutoff)
    banned = communities_banned_from(user_id)
    if banned:
        q = q.filter(Community.id.not_in(banned))
    blocked = blocked_communities(user_id)
    if blocked:
        q = q.filter(Community.id.not_in(blocked))
    blocked_instances = blocked_or_banned_instances(user_id)
    if blocked_instances:
        q = q.filter(Community.instance_id.not_in(blocked_instances))
    return q.order_by(desc(Community.first_federated_at)).order_by(desc(Community.created_at)).limit(5).all()


@cache.memoize(timeout=300)
def sidebar_new_instances() -> List:
    return db.session.query(Instance).\
        outerjoin(BannedInstances, BannedInstances.domain == Instance.domain).\
        filter(Instance.gone_forever == False, Instance.dormant == False,
               BannedInstances.id == None, Instance.software == 'piefed').\
        order_by(desc(Instance.created_at)).limit(5).all()


@cache.memoize(timeout=60)
def sidebar_upcoming_events() -> List:
    return db.session.execute(text("""SELECT e.start, p.title, p.id FROM "event" e
                                     INNER JOIN post p on e.post_id = p.id
                                     WHERE e.start > now() AND p.deleted is false
                                     AND p.status > :reviewing
                                     ORDER BY e.start LIMIT 5"""),
                              {'reviewing': POST_STATUS_REVIEWING}).all()


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
