from datetime import timedelta

from flask import current_app
from sqlalchemy import text

from app import celery, cache
from app.activitypub.util import find_actor_or_create
from app.constants import NOTIF_UNBAN
from app.models import (
    Notification, SendQueue, CommunityBan, CommunityMember, User, Community,
    Post, PostReply, DefederationSubscription, Instance, ActivityPubLog,
    InstanceRole, utcnow
)
from app.post.routes import post_delete_post
from app.utils import (
    get_task_session, download_defeds, instance_banned, get_request_instance,
    get_request, shorten_string
)


@celery.task
def cleanup_old_notifications():
    """Remove notifications older than 90 days"""
    session = get_task_session()
    try:
        cutoff = utcnow() - timedelta(days=90)
        session.query(Notification).filter(Notification.created_at < cutoff).delete()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def cleanup_send_queue():
    """Remove SendQueue entries older than 7 days"""
    session = get_task_session()
    try:
        cutoff = utcnow() - timedelta(days=7)
        session.query(SendQueue).filter(SendQueue.created < cutoff).delete()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def process_expired_bans():
    """Handle expired community bans"""
    session = get_task_session()
    try:
        expired_bans = session.query(CommunityBan).filter(CommunityBan.ban_until < utcnow()).all()

        for expired_ban in expired_bans:
            community_membership_record = session.query(CommunityMember).filter_by(
                community_id=expired_ban.community_id,
                user_id=expired_ban.user_id
            ).first()

            if community_membership_record:
                community_membership_record.is_banned = False

            blocked = session.query(User).get(expired_ban.user_id)
            community = session.query(Community).get(expired_ban.community_id)

            if blocked and blocked.is_local():
                # Notify unbanned person
                targets_data = {'gen': '0', 'community_id': community.id}
                notify = Notification(
                    title=shorten_string('You have been unbanned from ' + community.display_name()),
                    url=f'/chat/ban_from_mod/{blocked.id}/{community.id}',
                    user_id=blocked.id,
                    author_id=1,
                    notif_type=NOTIF_UNBAN,
                    subtype='user_unbanned_from_community',
                    targets=targets_data
                )
                session.add(notify)
                blocked.unread_notifications += 1

                # Clear relevant caches
                from app.utils import communities_banned_from, joined_communities, moderating_communities
                cache.delete_memoized(communities_banned_from, blocked.id)
                cache.delete_memoized(joined_communities, blocked.id)
                cache.delete_memoized(moderating_communities, blocked.id)

            session.delete(expired_ban)
            session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def remove_old_community_content():
    """Remove old content from communities with content retention policies"""
    session = get_task_session()
    try:
        communities = session.query(Community).filter(Community.content_retention > 0).all()

        for community in communities:
            cut_off = utcnow() - timedelta(days=community.content_retention)
            old_posts = session.query(Post).filter_by(
                deleted=False,
                sticky=False,
                community_id=community.id
            ).filter(Post.posted_at < cut_off).all()

            for post in old_posts:
                post_delete_post(community, post, post.user_id, reason=None, federate_all_communities=False)
                community.post_count -= 1

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def update_hashtag_counts():
    """Ensure accurate count of posts associated with each hashtag"""
    session = get_task_session()
    try:
        session.execute(text('''
            UPDATE tag 
            SET post_count = (
                SELECT COUNT(post_tag.post_id)
                FROM post_tag 
                WHERE post_tag.tag_id = tag.id
            )
        '''))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def delete_old_soft_deleted_content():
    """Delete soft-deleted content after 7 days"""
    session = get_task_session()
    try:
        cutoff = utcnow() - timedelta(days=7)

        # Delete old post replies
        post_reply_ids = list(
            session.execute(
                text('SELECT id FROM post_reply WHERE deleted = true AND posted_at < :cutoff'),
                {'cutoff': cutoff}
            ).scalars()
        )

        for post_reply_id in post_reply_ids:
            post_reply = session.query(PostReply).get(post_reply_id)
            if post_reply:  # Check if still exists
                post_reply.delete_dependencies()
                if not post_reply.has_replies(include_deleted=True):
                    session.delete(post_reply)
                    session.commit()

        # Delete old posts
        post_ids = list(
            session.execute(
                text('SELECT id FROM post WHERE deleted = true AND posted_at < :cutoff'),
                {'cutoff': cutoff}
            ).scalars()
        )

        for post_id in post_ids:
            post = session.query(Post).get(post_id)
            if post:  # Check if still exists
                post.delete_dependencies()
                session.delete(post)
                session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def update_community_stats():
    """Ensure accurate community statistics"""
    session = get_task_session()
    try:
        communities = session.query(Community).filter(
            Community.banned == False,
            Community.last_active > utcnow() - timedelta(days=3)
        ).all()

        for community in communities:
            community.subscriptions_count = session.execute(text(
                'SELECT COUNT(user_id) as c FROM community_member WHERE community_id = :community_id AND is_banned = false'
            ), {'community_id': community.id}).scalar()

            community.post_count = session.execute(text(
                'SELECT COUNT(id) as c FROM post WHERE deleted is false and community_id = :community_id'
            ), {'community_id': community.id}).scalar()

            community.post_reply_count = session.execute(text(
                'SELECT COUNT(id) as c FROM post_reply WHERE deleted is false and community_id = :community_id'
            ), {'community_id': community.id}).scalar()

            session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def cleanup_old_voting_data():
    """Delete voting data after configured time"""
    session = get_task_session()
    try:
        local_months = current_app.config['KEEP_LOCAL_VOTE_DATA_TIME']
        remote_months = current_app.config['KEEP_REMOTE_VOTE_DATA_TIME']

        if local_months != -1:
            cutoff_local = utcnow() - timedelta(days=28 * local_months)

            # Delete local user votes
            session.execute(text('''
                DELETE FROM "post_vote" pv
                USING "user" u, "instance" i
                WHERE pv.user_id = u.id
                  AND u.instance_id = i.id
                  AND u.instance_id = :instance_id
                  AND pv.created_at < :cutoff
            '''), {'cutoff': cutoff_local, 'instance_id': 1})

            session.execute(text('''
                DELETE FROM "post_reply_vote" prv
                USING "user" u, "instance" i
                WHERE prv.user_id = u.id
                  AND u.instance_id = i.id
                  AND u.instance_id = :instance_id
                  AND prv.created_at < :cutoff
            '''), {'cutoff': cutoff_local, 'instance_id': 1})

        if remote_months != -1:
            cutoff_remote = utcnow() - timedelta(days=28 * remote_months)

            # Delete remote user votes
            session.execute(text('''
                DELETE FROM "post_vote" pv
                USING "user" u, "instance" i
                WHERE pv.user_id = u.id
                  AND u.instance_id = i.id
                  AND u.instance_id != :instance_id
                  AND pv.created_at < :cutoff
            '''), {'cutoff': cutoff_remote, 'instance_id': 1})

            session.execute(text('''
                DELETE FROM "post_reply_vote" prv
                USING "user" u, "instance" i
                WHERE prv.user_id = u.id
                  AND u.instance_id = i.id
                  AND u.instance_id != :instance_id
                  AND prv.created_at < :cutoff
            '''), {'cutoff': cutoff_remote, 'instance_id': 1})

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def unban_expired_users():
    """Unban users after ban expires"""
    session = get_task_session()
    try:
        session.execute(text(
            'UPDATE "user" SET banned = false WHERE banned is true AND banned_until < :cutoff AND banned_until is not null'
        ), {'cutoff': utcnow()})
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def sync_defederation_subscriptions():
    """Update and sync defederation subscriptions"""
    session = get_task_session()
    try:
        session.execute(text('DELETE FROM banned_instances WHERE subscription_id is not null'))
        session.commit()

        for defederation_sub in session.query(DefederationSubscription).all():
            download_defeds(defederation_sub.id, defederation_sub.domain)

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def check_instance_health():
    """Check for dormant or dead instances"""
    session = get_task_session()
    try:
        HEADERS = {'Accept': 'application/activity+json'}

        # Mark dormant instances as gone_forever after 5 days
        five_days_ago = utcnow() - timedelta(days=5)
        dormant_instances = session.query(Instance).filter(
            Instance.dormant == True,
            Instance.start_trying_again < five_days_ago
        ).all()

        for instance in dormant_instances:
            instance.gone_forever = True
        session.commit()

        # Re-check dormant instances that are not gone_forever
        dormant_to_recheck = session.query(Instance).filter(
            Instance.dormant == True,
            Instance.gone_forever == False,
            Instance.id != 1
        ).all()

        for instance in dormant_to_recheck:
            if instance_banned(instance.domain) or instance.domain == 'flipboard.com':
                continue

            try:
                # Try the nodeinfo endpoint first
                if instance.nodeinfo_href:
                    node = get_request_instance(instance.nodeinfo_href, headers=HEADERS, instance=instance)
                    if node.status_code == 200:
                        try:
                            node_json = node.json()
                            if 'software' in node_json:
                                instance.software = node_json['software']['name'].lower()[:50]
                                instance.version = node_json['software']['version'][:50]
                                instance.failures = 0
                                instance.dormant = False
                                current_app.logger.info(f"Dormant instance {instance.domain} is back online")
                        finally:
                            node.close()
                else:
                    # Try to discover nodeinfo
                    nodeinfo = get_request_instance(
                        f"https://{instance.domain}/.well-known/nodeinfo",
                        headers=HEADERS,
                        instance=instance
                    )
                    if nodeinfo.status_code == 200:
                        try:
                            nodeinfo_json = nodeinfo.json()
                            for links in nodeinfo_json['links']:
                                if isinstance(links, dict) and 'rel' in links and links['rel'] in [
                                    'http://nodeinfo.diaspora.software/ns/schema/2.0',
                                    'https://nodeinfo.diaspora.software/ns/schema/2.0',
                                    'http://nodeinfo.diaspora.software/ns/schema/2.1'
                                ]:
                                    instance.nodeinfo_href = links['href']
                                    instance.failures = 0
                                    instance.dormant = False
                                    current_app.logger.info(f"Dormant instance {instance.domain} is back online")
                                    break
                        finally:
                            nodeinfo.close()
            except Exception as e:
                session.rollback()
                instance.failures += 1
                current_app.logger.warning(f"Error rechecking dormant instance {instance.domain}: {e}")

        session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def monitor_healthy_instances():
    """Check healthy instances to see if still healthy"""
    session = get_task_session()
    try:
        HEADERS = {'Accept': 'application/activity+json'}

        instances = session.query(Instance).filter(
            Instance.gone_forever == False,
            Instance.dormant == False,
            Instance.id != 1
        ).all()

        for instance in instances:
            if instance_banned(instance.domain) or instance.domain == 'flipboard.com':
                continue

            nodeinfo_href = instance.nodeinfo_href
            if (instance.software == 'lemmy' and instance.version is not None and
                    instance.version >= '0.19.4' and instance.nodeinfo_href and
                    instance.nodeinfo_href.endswith('nodeinfo/2.0.json')):
                nodeinfo_href = None

            if not nodeinfo_href:
                try:
                    nodeinfo = get_request_instance(
                        f"https://{instance.domain}/.well-known/nodeinfo",
                        headers=HEADERS,
                        instance=instance
                    )

                    if nodeinfo.status_code == 200:
                        nodeinfo_json = nodeinfo.json()
                        for links in nodeinfo_json['links']:
                            if isinstance(links, dict) and 'rel' in links and links['rel'] in [
                                'http://nodeinfo.diaspora.software/ns/schema/2.0',
                                'https://nodeinfo.diaspora.software/ns/schema/2.0',
                                'http://nodeinfo.diaspora.software/ns/schema/2.1'
                            ]:
                                instance.nodeinfo_href = links['href']
                                instance.failures = 0
                                instance.dormant = False
                                instance.gone_forever = False
                                break
                            else:
                                instance.failures += 1
                    elif nodeinfo.status_code >= 300:
                        current_app.logger.info(f"{instance.domain} has no well-known/nodeinfo response")
                        instance.failures += 1
                except Exception:
                    session.rollback()
                    instance.failures += 1
                finally:
                    nodeinfo.close()
                session.commit()

            if instance.nodeinfo_href:
                try:
                    node = get_request_instance(instance.nodeinfo_href, headers=HEADERS, instance=instance)
                    if node.status_code == 200:
                        node_json = node.json()
                        if 'software' in node_json:
                            instance.software = node_json['software']['name'].lower()[:50]
                            instance.version = node_json['software']['version'][:50]
                            instance.failures = 0
                            instance.dormant = False
                            instance.gone_forever = False
                    elif node.status_code >= 300:
                        instance.nodeinfo_href = None
                        instance.failures += 1
                        instance.most_recent_attempt = utcnow()
                        if instance.failures > 5:
                            instance.dormant = True
                            instance.start_trying_again = utcnow() + timedelta(days=5)
                except Exception:
                    session.rollback()
                    instance.failures += 1
                    instance.most_recent_attempt = utcnow()
                    if instance.failures > 5:
                        instance.dormant = True
                        instance.start_trying_again = utcnow() + timedelta(days=5)
                    if instance.failures > 12:
                        instance.gone_forever = True
                finally:
                    node.close()

                session.commit()
            else:
                instance.failures += 1
                instance.most_recent_attempt = utcnow()
                if instance.failures > 5:
                    instance.dormant = True
                    instance.start_trying_again = utcnow() + timedelta(days=5)
                if instance.failures > 12:
                    instance.gone_forever = True
                session.commit()

            # Handle admin roles for Lemmy/PieFed instances
            if instance.online() and (instance.software == 'lemmy' or instance.software == 'piefed'):
                try:
                    response = get_request(f'https://{instance.domain}/api/v3/site')
                    if response and response.status_code == 200:
                        instance_data = response.json()
                        admin_profile_ids = []

                        for admin in instance_data['admins']:
                            profile_id = admin['person']['actor_id']
                            if profile_id.startswith('https://'):
                                admin_profile_ids.append(profile_id.lower())
                                user = find_actor_or_create(profile_id)
                                if user and not instance.user_is_admin(user.id):
                                    new_instance_role = InstanceRole(
                                        instance_id=instance.id,
                                        user_id=user.id,
                                        role='admin'
                                    )
                                    session.add(new_instance_role)

                        # Remove old admin roles
                        for instance_admin in session.query(InstanceRole).filter_by(instance_id=instance.id):
                            if instance_admin.user.profile_id() not in admin_profile_ids:
                                session.query(InstanceRole).filter(
                                    InstanceRole.user_id == instance_admin.user.id,
                                    InstanceRole.instance_id == instance.id,
                                    InstanceRole.role == 'admin'
                                ).delete()
                except Exception:
                    session.rollback()
                    instance.failures += 1
                finally:
                    if response:
                        response.close()
                session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def recalculate_user_attitudes():
    """Recalculate recent active user attitudes"""
    session = get_task_session()
    try:
        recent_users = session.query(User).filter(
            User.last_seen > utcnow() - timedelta(days=1)
        ).all()

        for user in recent_users:
            user.recalculate_attitude()

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def calculate_community_activity_stats():
    """Calculate active users for day/week/month/half year for local communities"""
    session = get_task_session()
    try:
        # Timing settings
        day = utcnow() - timedelta(hours=24)
        week = utcnow() - timedelta(days=7)
        month = utcnow() - timedelta(weeks=4)
        half_year = utcnow() - timedelta(weeks=26)

        # Get community IDs
        comm_ids = session.query(Community.id).filter(Community.banned == False).all()
        comm_ids = [id for (id,) in comm_ids]  # flatten list of tuples

        for community_id in comm_ids:
            for interval in [day, week, month, half_year]:
                count = session.execute(text('''
                    SELECT count(*) FROM
                    (
                        SELECT p.user_id FROM "post" p
                        WHERE p.posted_at > :time_interval 
                            AND p.from_bot = False
                            AND p.community_id = :community_id
                        UNION
                        SELECT pr.user_id FROM "post_reply" pr
                        WHERE pr.posted_at > :time_interval
                            AND pr.from_bot = False
                            AND pr.community_id = :community_id   
                        UNION
                        SELECT pv.user_id FROM "post_vote" pv
                        INNER JOIN "user" u ON pv.user_id = u.id
                        INNER JOIN "post" p ON pv.post_id = p.id
                        WHERE pv.created_at > :time_interval
                            AND u.bot = False
                            AND p.community_id = :community_id                            
                        UNION
                        SELECT prv.user_id FROM "post_reply_vote" prv
                        INNER JOIN "user" u ON prv.user_id = u.id
                        INNER JOIN "post_reply" pr ON prv.post_reply_id = pr.id
                        INNER JOIN "post" p ON pr.post_id = p.id
                        WHERE prv.created_at > :time_interval
                            AND u.bot = False
                            AND p.community_id = :community_id
                    ) AS activity
                '''), {'time_interval': interval, 'community_id': community_id}).scalar()

                # Update the community stats
                community = session.query(Community).get(community_id)
                if community:
                    if interval == day:
                        community.active_daily = count
                    elif interval == week:
                        community.active_weekly = count
                    elif interval == month:
                        community.active_monthly = count
                    elif interval == half_year:
                        community.active_6monthly = count
                    session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def cleanup_old_activitypub_logs():
    """Clean up old ActivityPub logs (older than 3 days)"""
    session = get_task_session()
    try:
        cutoff = utcnow() - timedelta(days=3)
        session.query(ActivityPubLog).filter(ActivityPubLog.created_at < cutoff).delete()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
