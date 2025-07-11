# if commands in this file are not working (e.g. 'flask translate') make sure you set the FLASK_APP environment variable.
# e.g. export FLASK_APP=pyfedi.py

# This file is part of PieFed, which is licensed under the GNU General Public License (GPL) version 3.0.
# You should have received a copy of the GPL along with this program. If not, see <http://www.gnu.org/licenses/>.

import imaplib
import os
import re
import uuid
from datetime import datetime, timedelta
from random import randint
from time import sleep
from zoneinfo import ZoneInfo

import click
import flask
import redis
from flask import json, current_app
from flask_babel import _
from sqlalchemy import or_, desc, text

from app import db, cache
from app.activitypub.signature import RsaKeys, send_post_request, default_context
from app.activitypub.util import find_actor_or_create, extract_domain_and_actor, notify_about_post
from app.auth.util import random_token
from app.constants import NOTIF_COMMUNITY, NOTIF_POST, NOTIF_REPLY, POST_STATUS_SCHEDULED, POST_STATUS_PUBLISHED, \
    NOTIF_UNBAN, POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_IMAGE
from app.email import send_email
from app.models import Settings, BannedInstances, Role, User, RolePermission, Domain, ActivityPubLog, \
    utcnow, Site, Instance, File, Notification, Post, CommunityMember, NotificationSubscription, PostReply, Language, \
    InstanceRole, Community, DefederationSubscription, SendQueue, CommunityBan, _store_files_in_s3, PostVote, Poll, \
    ActivityBatch
from app.post.routes import post_delete_post
from app.shared.tasks import task_selector
from app.utils import retrieve_block_list, blocked_domains, retrieve_peertube_block_list, \
    shorten_string, get_request, blocked_communities, gibberish, get_request_instance, \
    instance_banned, recently_upvoted_post_replies, recently_upvoted_posts, jaccard_similarity, download_defeds, \
    get_redis_connection, instance_online, instance_gone_forever, find_next_occurrence, \
    guess_mime_type, communities_banned_from, joined_communities, moderating_communities, ensure_directory_exists, \
    render_from_tpl, get_task_session, patch_db_session


def register(app):
    @app.cli.group()
    def translate():
        """Translation and localization commands."""
        pass

    @translate.command()
    @click.argument('lang')
    def init(lang):
        """Initialize a new language."""
        if os.system('pybabel extract -F babel.cfg -k _l -o messages.pot .'):
            raise RuntimeError('extract command failed')
        if os.system(
                'pybabel init -i messages.pot -d app/translations -l ' + lang):
            raise RuntimeError('init command failed')
        os.remove('messages.pot')

    @translate.command()
    def update():
        """Update all languages."""
        if os.system('pybabel extract -F babel.cfg -k _l -o messages.pot .'):
            raise RuntimeError('extract command failed')
        if os.system('pybabel update -i messages.pot -d app/translations'):
            raise RuntimeError('update command failed')
        os.remove('messages.pot')

    @translate.command()
    def compile():
        """Compile all languages."""
        if os.system('pybabel compile -d app/translations'):
            raise RuntimeError('compile command failed')

    @app.cli.command("keys")
    def keys():
        private_key, public_key = RsaKeys.generate_keypair()
        print(private_key)
        print(public_key)

    @app.cli.command("admin-keys")
    def admin_keys():
        private_key, public_key = RsaKeys.generate_keypair()
        u: User = User.query.get(1)
        u.private_key = private_key
        u.public_key = public_key
        db.session.commit()
        print('Admin keys have been reset')

    @app.cli.command("init-db")
    def init_db():
        with app.app_context():
            # Check if alembic_version table exists
            inspector = db.inspect(db.engine)
            if 'alembic_version' not in inspector.get_table_names():
                print("Error: alembic_version table not found. Please run 'flask db upgrade' first.")
                return

            db.drop_all()
            db.configure_mappers()
            db.create_all()
            private_key, public_key = RsaKeys.generate_keypair()
            db.session.add(
                Site(name="PieFed", description='Explore Anything, Discuss Everything.', public_key=public_key,
                     private_key=private_key, language_id=2))
            db.session.add(Instance(domain=app.config['SERVER_NAME'],
                                    software='PieFed'))  # Instance 1 is always the local instance
            db.session.add(Settings(name='allow_nsfw', value=json.dumps(False)))
            db.session.add(Settings(name='allow_nsfl', value=json.dumps(False)))
            db.session.add(Settings(name='allow_dislike', value=json.dumps(True)))
            db.session.add(Settings(name='allow_local_image_posts', value=json.dumps(True)))
            db.session.add(Settings(name='allow_remote_image_posts', value=json.dumps(True)))
            db.session.add(Settings(name='federation', value=json.dumps(True)))
            banned_instances = ['anonib.al', 'lemmygrad.ml', 'gab.com', 'rqd2.net', 'exploding-heads.com',
                                'hexbear.net',
                                'threads.net', 'noauthority.social', 'pieville.net', 'links.hackliberty.org',
                                'poa.st', 'freespeechextremist.com', 'bae.st', 'nicecrew.digital',
                                'detroitriotcity.com',
                                'pawoo.net', 'shitposter.club', 'spinster.xyz', 'catgirl.life', 'gameliberty.club',
                                'yggdrasil.social', 'beefyboys.win', 'brighteon.social', 'cum.salon', 'wizard.casa']
            for bi in banned_instances:
                db.session.add(BannedInstances(domain=bi))
                print("Added banned instance", bi)

            # Load initial domain block list
            block_list = retrieve_block_list()
            if block_list:
                for domain in block_list.split('\n'):
                    db.session.add(Domain(name=domain.strip(), banned=True))
                print("Added 'No-QAnon' blocklist, see https://github.com/rimu/no-qanon")

            # Load peertube domain block list
            block_list = retrieve_peertube_block_list()
            if block_list:
                for domain in block_list.split('\n'):
                    db.session.add(Domain(name=domain.strip(), banned=True))
                    db.session.add(BannedInstances(domain=domain.strip()))
                print("Added 'Peertube Isolation' blocklist, see https://peertube_isolation.frama.io/")

            # Initial languages
            db.session.add(Language(name='Undetermined', code='und'))
            db.session.add(Language(code='en', name='English'))
            db.session.add(Language(code='de', name='Deutsch'))
            db.session.add(Language(code='es', name='Español'))
            db.session.add(Language(code='fr', name='Français'))
            db.session.add(Language(code='hi', name='हिन्दी'))
            db.session.add(Language(code='ja', name='日本語'))
            db.session.add(Language(code='zh', name='中文'))

            # Initial roles
            # These roles will create rows in the 'role' table with IDs of 1,2,3,4. There are some constants (ROLE_*) in
            # constants.py that will need to be updated if the role IDs ever change.
            anon_role = Role(name='Anonymous user', weight=0)
            db.session.add(anon_role)

            auth_role = Role(name='Authenticated user', weight=1)
            db.session.add(auth_role)

            staff_role = Role(name='Staff', weight=2)
            staff_role.permissions.append(RolePermission(permission='approve registrations'))
            staff_role.permissions.append(RolePermission(permission='ban users'))
            staff_role.permissions.append(RolePermission(permission='administer all communities'))
            staff_role.permissions.append(RolePermission(permission='administer all users'))
            db.session.add(staff_role)

            admin_role = Role(name='Admin', weight=3)
            admin_role.permissions.append(RolePermission(permission='approve registrations'))
            admin_role.permissions.append(RolePermission(permission='change user roles'))
            admin_role.permissions.append(RolePermission(permission='ban users'))
            admin_role.permissions.append(RolePermission(permission='manage users'))
            admin_role.permissions.append(RolePermission(permission='change instance settings'))
            admin_role.permissions.append(RolePermission(permission='administer all communities'))
            admin_role.permissions.append(RolePermission(permission='administer all users'))
            admin_role.permissions.append(RolePermission(permission='edit cms pages'))
            db.session.add(admin_role)

            # Admin user
            print(
                'The admin user created here should be reserved for admin tasks and not used as a primary daily identity (unless this instance will only be for personal use).')
            user_name = input("Admin user name (ideally not 'admin'): ")
            email = input("Admin email address: ")
            password = input("Admin password: ")
            while '@' in user_name or ' ' in user_name:
                print('User name cannot be an email address or have spaces.')
                user_name = input("Admin user name (ideally not 'admin'): ")
            verification_token = random_token(16)
            private_key, public_key = RsaKeys.generate_keypair()
            admin_user = User(user_name=user_name, title=user_name,
                              email=email, verification_token=verification_token,
                              instance_id=1, email_unread_sent=False,
                              private_key=private_key, public_key=public_key,
                              alt_user_name=gibberish(randint(8, 20)))
            admin_user.set_password(password)
            admin_user.roles.append(admin_role)
            admin_user.verified = True
            admin_user.last_seen = utcnow()
            admin_user.ap_profile_id = f"https://{current_app.config['SERVER_NAME']}/u/{admin_user.user_name.lower()}"
            admin_user.ap_public_url = f"https://{current_app.config['SERVER_NAME']}/u/{admin_user.user_name}"
            admin_user.ap_inbox_url = f"https://{current_app.config['SERVER_NAME']}/u/{admin_user.user_name.lower()}/inbox"
            db.session.add(admin_user)

            db.session.commit()
            print("Initial setup is finished.")

    @app.cli.command('testing')
    def testing():
        with app.app_context():
            # calculate active users for day/week/month/half year
            # for local communities
            print("Calculating community stats...")

            # timing settings
            day = utcnow() - timedelta(hours=24)
            week = utcnow() - timedelta(days=7)
            month = utcnow() - timedelta(weeks=4)
            half_year = utcnow() - timedelta(weeks=26)  # 52 weeks/year divided by 2

            # get a list of the ids for communities
            comm_ids = db.session.query(Community.id).filter(Community.banned == False).all()
            comm_ids = [id for (id,) in comm_ids]  # flatten list of tuples

            for community_id in comm_ids:
                for interval in day, week, month, half_year:
                    count = db.session.execute(text('''
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

                    # update the community stats in the db
                    try:
                        if interval == day:
                            c = Community.query.get(community_id)
                            c.active_daily = count
                        elif interval == week:
                            c = Community.query.get(community_id)
                            c.active_weekly = count
                        elif interval == month:
                            c = Community.query.get(community_id)
                            c.active_monthly = count
                        elif interval == half_year:
                            c = Community.query.get(community_id)
                            c.active_6monthly = count
                        # commit to the db
                        db.session.commit()
                    except Exception as e:
                        print(f"Exception: {e}, db rollback initiated.")
                        db.session.rollback()
                        raise
                    finally:
                        db.session.remove()
            print("Stats update complete.")

    @app.cli.command('daily-maintenance-celery')
    def daily_maintenance_celery():
        """Schedule daily maintenance tasks via Celery background queue"""
        with app.app_context():
            from app.shared.tasks.maintenance import (
                cleanup_old_notifications, cleanup_send_queue, process_expired_bans,
                remove_old_community_content, update_hashtag_counts, delete_old_soft_deleted_content,
                update_community_stats, cleanup_old_voting_data, unban_expired_users,
                sync_defederation_subscriptions, check_instance_health, monitor_healthy_instances,
                recalculate_user_attitudes, calculate_community_activity_stats, cleanup_old_activitypub_logs
            )

            print(f'Scheduling daily maintenance tasks via Celery at {datetime.now()}')

            # Schedule all maintenance tasks (sync in debug mode, async in production)
            if current_app.debug:
                cleanup_old_notifications()
                cleanup_send_queue()
                process_expired_bans()
                remove_old_community_content()
                update_hashtag_counts()
                delete_old_soft_deleted_content()
                update_community_stats()
                cleanup_old_voting_data()
                unban_expired_users()
                sync_defederation_subscriptions()
                check_instance_health()
                monitor_healthy_instances()
                recalculate_user_attitudes()
                calculate_community_activity_stats()
                cleanup_old_activitypub_logs()
                print('All maintenance tasks completed synchronously (debug mode)')
            else:
                cleanup_old_notifications.delay()
                cleanup_send_queue.delay()
                process_expired_bans.delay()
                remove_old_community_content.delay()
                update_hashtag_counts.delay()
                delete_old_soft_deleted_content.delay()
                update_community_stats.delay()
                cleanup_old_voting_data.delay()
                unban_expired_users.delay()
                sync_defederation_subscriptions.delay()
                check_instance_health.delay()
                monitor_healthy_instances.delay()
                recalculate_user_attitudes.delay()
                calculate_community_activity_stats.delay()
                cleanup_old_activitypub_logs.delay()
                print('All maintenance tasks scheduled successfully (production mode)')

    @app.cli.command('daily-maintenance')
    def daily_maintenance():
        with app.app_context():
            # Remove notifications older than 90 days
            db.session.query(Notification).filter(Notification.created_at < utcnow() - timedelta(days=90)).delete()
            db.session.commit()

            # Remove SendQueue older than 7 days
            db.session.query(SendQueue).filter(SendQueue.created < utcnow() - timedelta(days=7)).delete()
            db.session.commit()

            # Expired bans
            community_bans = CommunityBan.query.filter(CommunityBan.ban_until < utcnow()).all()
            for expired_ban in community_bans:
                community_membership_record = CommunityMember.query.filter_by(community_id=expired_ban.community_id,
                                                                              user_id=expired_ban.user_id).first()
                if community_membership_record:
                    community_membership_record.is_banned = False
                blocked = User.query.get(expired_ban.user_id)
                community = Community.query.get(expired_ban.community_id)
                if blocked.is_local():
                    # Notify unbanned person
                    targets_data = {'gen': '0', 'community_id': community.id}
                    notify = Notification(
                        title=shorten_string('You have been unbanned from ' + community.display_name()),
                        url=f'/chat/ban_from_mod/{blocked.id}/{community.id}', user_id=blocked.id,
                        author_id=1, notif_type=NOTIF_UNBAN,
                        subtype='user_unbanned_from_community',
                        targets=targets_data)
                    db.session.add(notify)
                    blocked.unread_notifications += 1  # who pressed 'Re-submit this activity'.

                    cache.delete_memoized(communities_banned_from, blocked.id)
                    cache.delete_memoized(joined_communities, blocked.id)
                    cache.delete_memoized(moderating_communities, blocked.id)

                db.session.delete(expired_ban)
                db.session.commit()

            # Remove old content from communities
            print(f'Start removing old content from communities {datetime.now()}')
            communities = Community.query.filter(Community.content_retention > 0).all()
            for community in communities:
                cut_off = utcnow() - timedelta(days=community.content_retention)
                old_posts = Post.query.filter_by(deleted=False, sticky=False, community_id=community.id).\
                    filter(Post.posted_at < cut_off).all()
                for post in old_posts:
                    post_delete_post(community, post, post.user_id, reason=None, federate_all_communities=False)
                    community.post_count -= 1
            db.session.commit()

            # Ensure accurate count of posts associated with each hashtag
            print(f'Ensure accurate count of posts associated with each hashtag {datetime.now()}')
            db.session.execute(text('''
                UPDATE tag 
                SET post_count = (
                    SELECT COUNT(post_tag.post_id)
                    FROM post_tag 
                    WHERE post_tag.tag_id = tag.id
                )
            '''))
            db.session.commit()

            # Delete soft-deleted content after 7 days
            print(f'Delete soft-deleted content {datetime.now()}')
            # Get PostReply IDs using raw SQL to reduce memory usage
            post_reply_ids = list(
                db.session.execute(text('SELECT id FROM post_reply WHERE deleted = true AND posted_at < :cutoff'),
                                   {'cutoff': utcnow() - timedelta(days=7)}).scalars())
            for post_reply_id in post_reply_ids:
                post_reply = PostReply.query.get(post_reply_id)
                if post_reply:  # Check if still exists
                    post_reply.delete_dependencies()
                    if not post_reply.has_replies():
                        db.session.delete(post_reply)
                        db.session.commit()

            # Get Post IDs using raw SQL to reduce memory usage
            post_ids = list(db.session.execute(text('SELECT id FROM post WHERE deleted = true AND posted_at < :cutoff'),
                                               {'cutoff': utcnow() - timedelta(days=7)}).scalars())
            for post_id in post_ids:
                post = Post.query.get(post_id)
                if post:  # Check if still exists
                    post.delete_dependencies()
                    db.session.delete(post)
                    db.session.commit()

            # Ensure accurate community stats
            print(f'Ensure accurate community stats {datetime.now()}')
            for community in Community.query.filter(Community.banned == False,
                                                    Community.last_active > utcnow() - timedelta(days=3)).all():
                community.subscriptions_count = db.session.execute(text(
                    'SELECT COUNT(user_id) as c FROM community_member WHERE community_id = :community_id AND is_banned = false'),
                                                                   {'community_id': community.id}).scalar()
                community.post_count = db.session.execute(
                    text('SELECT COUNT(id) as c FROM post WHERE deleted is false and community_id = :community_id'),
                    {'community_id': community.id}).scalar()
                community.post_reply_count = db.session.execute(text(
                    'SELECT COUNT(id) as c FROM post_reply WHERE deleted is false and community_id = :community_id'),
                                                                {'community_id': community.id}).scalar()
                db.session.commit()

            # Delete voting data after configured time (default ~6 months)
            print(f'Delete old voting data {datetime.now()}')

            # Trim voting data
            local_months = current_app.config['KEEP_LOCAL_VOTE_DATA_TIME']
            remote_months = current_app.config['KEEP_REMOTE_VOTE_DATA_TIME']

            if local_months != -1:
                cutoff_local = utcnow() - timedelta(days=28 * local_months)

                # delete all the rows from post_vote where the user who did the vote is a local user
                db.session.execute(text('''
                    DELETE FROM "post_vote" pv
                    USING "user" u, "instance" i
                    WHERE pv.user_id = u.id
                      AND u.instance_id = i.id
                      AND u.instance_id = :instance_id
                      AND pv.created_at < :cutoff
                '''), {'cutoff': cutoff_local, 'instance_id': 1})

                # delete all the rows from post_reply_vote where the user who did the vote is a local user
                db.session.execute(text('''
                    DELETE FROM "post_reply_vote" prv
                    USING "user" u, "instance" i
                    WHERE prv.user_id = u.id
                      AND u.instance_id = i.id
                      AND u.instance_id = :instance_id
                      AND prv.created_at < :cutoff
                '''), {'cutoff': cutoff_local, 'instance_id': 1})

            if remote_months != -1:
                cutoff_remote = utcnow() - timedelta(days=28 * remote_months)
                # delete all the rows from post_vote where the user who did the vote is a remote user
                db.session.execute(text('''
                    DELETE FROM "post_vote" pv
                    USING "user" u, "instance" i
                    WHERE pv.user_id = u.id
                      AND u.instance_id = i.id
                      AND u.instance_id != :instance_id
                      AND pv.created_at < :cutoff
                '''), {'cutoff': cutoff_remote, 'instance_id': 1})

                # delete all the rows from post_reply_vote where the user who did the vote is a remote user
                db.session.execute(text('''
                    DELETE FROM "post_reply_vote" prv
                    USING "user" u, "instance" i
                    WHERE prv.user_id = u.id
                      AND u.instance_id = i.id
                      AND u.instance_id != :instance_id
                      AND prv.created_at < :cutoff
                '''), {'cutoff': cutoff_remote, 'instance_id': 1})

            db.session.commit()

            # Un-ban after ban expires
            print(f'Un-ban after ban expires {datetime.now()}')
            db.session.execute(text(
                'UPDATE "user" SET banned = false WHERE banned is true AND banned_until < :cutoff AND banned_until is not null'),
                               {'cutoff': utcnow()})
            db.session.commit()

            # update and sync defederation subscriptions
            print(f'update and sync defederation subscriptions {datetime.now()}')
            db.session.execute(text('DELETE FROM banned_instances WHERE subscription_id is not null'))
            db.session.commit()
            for defederation_sub in DefederationSubscription.query.all():
                download_defeds(defederation_sub.id, defederation_sub.domain)

            # Check for dormant or dead instances
            print(f'Check for dormant or dead instances {datetime.now()}')
            HEADERS = {'Accept': 'application/activity+json'}
            try:
                # Check for instances that have been dormant for 5+ days and mark them as gone_forever
                five_days_ago = utcnow() - timedelta(days=5)
                dormant_instances = Instance.query.filter(Instance.dormant == True,
                                                          Instance.start_trying_again < five_days_ago).all()

                for instance in dormant_instances:
                    instance.gone_forever = True
                db.session.commit()

                # Re-check dormant instances that are not gone_forever
                dormant_to_recheck = Instance.query.filter(Instance.dormant == True, Instance.gone_forever == False,
                                                           Instance.id != 1).all()

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
                            # If no nodeinfo_href, try to discover it
                            nodeinfo = get_request_instance(f"https://{instance.domain}/.well-known/nodeinfo",
                                                            headers=HEADERS, instance=instance)
                            if nodeinfo.status_code == 200:
                                try:
                                    nodeinfo_json = nodeinfo.json()
                                    for links in nodeinfo_json['links']:
                                        if isinstance(links, dict) and 'rel' in links and links['rel'] in [
                                            'http://nodeinfo.diaspora.software/ns/schema/2.0',
                                            'https://nodeinfo.diaspora.software/ns/schema/2.0',
                                            'http://nodeinfo.diaspora.software/ns/schema/2.1']:
                                            instance.nodeinfo_href = links['href']
                                            instance.failures = 0
                                            instance.dormant = False
                                            current_app.logger.info(f"Dormant instance {instance.domain} is back online")
                                            break
                                finally:
                                    nodeinfo.close()
                    except Exception as e:
                        db.session.rollback()
                        instance.failures += 1
                        current_app.logger.warning(f"Error rechecking dormant instance {instance.domain}: {e}")

                db.session.commit()

                # Check healthy instances to see if still healthy
                instances = Instance.query.filter(Instance.gone_forever == False, Instance.dormant == False,
                                                  Instance.id != 1).all()

                for instance in instances:
                    if instance_banned(instance.domain) or instance.domain == 'flipboard.com':
                        continue
                    nodeinfo_href = instance.nodeinfo_href
                    if instance.software == 'lemmy' and instance.version is not None and instance.version >= '0.19.4' and \
                            instance.nodeinfo_href and instance.nodeinfo_href.endswith('nodeinfo/2.0.json'):
                        nodeinfo_href = None

                    if not nodeinfo_href:
                        try:
                            nodeinfo = get_request_instance(f"https://{instance.domain}/.well-known/nodeinfo",
                                                            headers=HEADERS, instance=instance)

                            if nodeinfo.status_code == 200:
                                nodeinfo_json = nodeinfo.json()
                                for links in nodeinfo_json['links']:
                                    if isinstance(links, dict) and 'rel' in links and links['rel'] in [
                                        'http://nodeinfo.diaspora.software/ns/schema/2.0',
                                        'https://nodeinfo.diaspora.software/ns/schema/2.0',
                                        'http://nodeinfo.diaspora.software/ns/schema/2.1']:
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
                            db.session.rollback()
                            instance.failures += 1
                        finally:
                            nodeinfo.close()
                        db.session.commit()

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
                            db.session.rollback()
                            instance.failures += 1
                            instance.most_recent_attempt = utcnow()
                            if instance.failures > 5:
                                instance.dormant = True
                                instance.start_trying_again = utcnow() + timedelta(days=5)
                            if instance.failures > 12:
                                instance.gone_forever = True
                        finally:
                            node.close()

                        db.session.commit()
                    else:
                        instance.failures += 1
                        instance.most_recent_attempt = utcnow()
                        if instance.failures > 5:
                            instance.dormant = True
                            instance.start_trying_again = utcnow() + timedelta(days=5)
                        if instance.failures > 12:
                            instance.gone_forever = True
                        db.session.commit()

                    # Handle admin roles
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
                                            new_instance_role = InstanceRole(instance_id=instance.id, user_id=user.id,
                                                                             role='admin')
                                            db.session.add(new_instance_role)
                                # remove any InstanceRoles that are no longer part of instance-data['admins']
                                for instance_admin in InstanceRole.query.filter_by(instance_id=instance.id):
                                    if instance_admin.user.profile_id() not in admin_profile_ids:
                                        db.session.query(InstanceRole).filter(
                                            InstanceRole.user_id == instance_admin.user.id,
                                            InstanceRole.instance_id == instance.id,
                                            InstanceRole.role == 'admin').delete()
                        except Exception:
                            db.session.rollback()
                            instance.failures += 1
                        finally:
                            if response:
                                response.close()
                        db.session.commit()

            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Error in daily maintenance: {e}")

            # recalculate recent active user's attitude
            print(f'recalcuating attitudes {datetime.now()}')
            for user in User.query.filter(User.last_seen > utcnow() - timedelta(days=1)):
                user.recalculate_attitude()

            # calculate active users for day/week/month/half year
            # for local communities
            print("Calculating community stats...")

            # timing settings
            day = utcnow() - timedelta(hours=24)
            week = utcnow() - timedelta(days=7)
            month = utcnow() - timedelta(weeks=4)
            half_year = utcnow() - timedelta(weeks=26) # 52 weeks/year divided by 2

            # get a list of the ids for communities
            comm_ids = db.session.query(Community.id).filter(Community.banned == False).all()
            comm_ids = [id for (id,) in comm_ids]  # flatten list of tuples

            for community_id in comm_ids:
                for interval in day,week,month,half_year:
                    count = db.session.execute(text('''
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
                            '''), {'time_interval': interval,'community_id': community_id}).scalar()
                    
                    # update the community stats in the db
                    try:
                        if interval == day:
                            c = Community.query.get(community_id)
                            c.active_daily = count
                        elif interval == week:
                            c = Community.query.get(community_id)
                            c.active_weekly = count
                        elif interval == month:
                            c = Community.query.get(community_id)
                            c.active_monthly = count
                        elif interval == half_year:
                            c = Community.query.get(community_id)
                            c.active_6monthly = count
                        # commit to the db
                        db.session.commit()
                    except Exception as e:
                        print(f"Exception: {e}, db rollback initiated.")
                        db.session.rollback()
                        raise 
                    finally:
                        db.session.remove()
            print("Stats update complete.")

            print(f'Done {datetime.now()}')

    @app.cli.command('send-queue')
    def send_queue():
        with app.app_context():
            session = get_task_session()
            try:
                with patch_db_session(session):
                    from app import redis_client
                    try:  # avoid parallel runs of this task using Redis lock
                        with redis_client.lock("lock:send-queue", timeout=300, blocking_timeout=1):
                            # Check size of redis memory. Abort if > 200 MB used
                            try:
                                if redis_client and redis_client.memory_stats()['total.allocated'] > 200000000:
                                    print('Redis memory is quite full - stopping send queue to avoid making it worse.')
                                    return
                            except:  # retrieving memory stats fails on recent versions of redis. Once the redis package is fixed this problem should go away.
                                ...

                            to_be_deleted = []
                            # Send all waiting Activities that are due to be sent
                            for to_send in session.query(SendQueue).filter(SendQueue.send_after < utcnow()):
                                if instance_online(to_send.destination_domain):
                                    if to_send.retries <= to_send.max_retries:
                                        send_post_request(to_send.destination, json.loads(to_send.payload), to_send.private_key,
                                                          to_send.actor,
                                                          retries=to_send.retries + 1)
                                    to_be_deleted.append(to_send.id)
                                elif instance_gone_forever(to_send.destination_domain):
                                    to_be_deleted.append(to_send.id)
                            # Remove them once sent - send_post_request will have re-queued them if they failed
                            if len(to_be_deleted):
                                session.execute(text('DELETE FROM "send_queue" WHERE id IN :to_be_deleted'),
                                                   {'to_be_deleted': tuple(to_be_deleted)})
                                session.commit()

                            publish_scheduled_posts()

                            if current_app.config['FEP_AWESOME']:
                                send_batched_activities()

                    except redis.exceptions.LockError:
                        print('Send queue is still running - stopping this process to avoid duplication.')
                        return
                    except Exception as e:
                        print('Could not connect to redis or other error occurred')
                        raise e
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    @app.cli.command('publish-scheduled-posts')
    def publish_scheduled_posts_command():
        # for dev/debug purposes this is it's own separate cli command but once it's finished we'll want to remove the @app.cli.command decorator
        # so that instance admins don't need to set up another cron job
        publish_scheduled_posts()

    def publish_scheduled_posts():
            for post in Post.query.filter(Post.status == POST_STATUS_SCHEDULED,
                                          Post.deleted == False, Post.repeat != 'none', Post.scheduled_for != None):
                if post.timezone is None:
                    post.timezone = post.author.timezone
                if post.scheduled_for and post.timezone:
                    date_with_tz = post.scheduled_for.replace(tzinfo=ZoneInfo(post.timezone))
                    if date_with_tz.astimezone(ZoneInfo('UTC')) > utcnow(naive=False):
                        continue
                    if post.repeat and post.repeat != 'once':
                        next_occurrence = post.scheduled_for + find_next_occurrence(post)
                    else:
                        next_occurrence = None
                    # One shot scheduled post
                    if not next_occurrence:
                        post.status = POST_STATUS_PUBLISHED
                        post.scheduled_for = None
                        post.posted_at = utcnow()
                        post.edited_at = None
                        post.title = render_from_tpl(post.title)
                        if post.type == POST_TYPE_POLL:
                            poll = Poll.query.get(post.id)
                            time_difference = poll.end_poll - post.created_at
                            poll.end_poll += time_difference
                        db.session.commit()

                        # Federate post
                        task_selector('make_post', post_id=post.id)

                        # create Notification()s for all the people subscribed to this post.community, post.author, post.topic_id and feed
                        notify_about_post(post)

                    # Scheduled post with multiple occurences
                    else:
                        # Create a new instance and copy all fields
                        scheduled_post = Post()
                        for column in post.__table__.columns:
                            setattr(scheduled_post, column.name, getattr(post, column.name))
                        scheduled_post.id = None
                        scheduled_post.ap_id = None
                        scheduled_post.scheduled_for = None
                        scheduled_post.posted_at = utcnow()
                        scheduled_post.edited_at = None
                        scheduled_post.status = POST_STATUS_PUBLISHED
                        scheduled_post.title = render_from_tpl(scheduled_post.title)
                        db.session.add(scheduled_post)
                        db.session.commit()

                        scheduled_post.ap_id = f"https://{current_app.config['SERVER_NAME']}/post/{scheduled_post.id}"
                        # Update the scheduled_for with the next occurrence date
                        post.scheduled_for = next_occurrence

                        # Small hack to make image urls unique and avoid creating
                        # a crosspost when scheduling an image post
                        if post.type == POST_TYPE_IMAGE:
                            post.image.source_url += f"?uid={uuid.uuid4().hex}"

                        vote = PostVote(user_id=post.user_id, post_id=scheduled_post.id, author_id=scheduled_post.user_id,
                                        effect=1)
                        db.session.add(vote)
                        db.session.commit()

                        task_selector('make_post', post_id=scheduled_post.id)
                        notify_about_post(scheduled_post)

    @app.cli.command('send-batched-activities')
    def send_batched_activities_command():
        send_batched_activities()

    def send_batched_activities():
        instances_and_communities = db.session.execute(text("""SELECT DISTINCT instance_id, community_id 
                                                                FROM "activity_batch" 
                                                                ORDER BY instance_id, community_id""")).fetchall()
        current_instance = None
        for instances_and_community in instances_and_communities:
            if current_instance is None or current_instance.id != instances_and_community[0]:
                current_instance = Instance.query.get(instances_and_community[0])
            community = Community.query.get(instances_and_community[1])

            announce_id = f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}"
            actor = community.public_url()
            to = ["https://www.w3.org/ns/activitystreams#Public"]
            cc = [community.ap_followers_url]
            announce = {
                'id': announce_id,
                'type': 'Announce',
                'actor': actor,
                'object': [],
                '@context': default_context(),
                'to': to,
                'cc': cc
            }
            payloads = ActivityBatch.query.filter(ActivityBatch.instance_id == current_instance.id, ActivityBatch.community_id == community.id).order_by(ActivityBatch.created)
            delete_payloads = []
            for payload in payloads.all():
                announce['object'].append(payload.payload)
                delete_payloads.append(payload.id)
            send_post_request(current_instance.inbox, announce, community.private_key, community.public_url() + '#main-key')
            ActivityBatch.query.filter(ActivityBatch.id.in_(delete_payloads)).delete()
            db.session.commit()


    @app.cli.command('move-files-to-s3')
    def move_files_to_s3():
        with app.app_context():
            from app.utils import move_file_to_s3
            import boto3

            print('This will run for a long time, you should run it in a tmux session. Hit Ctrl+C now if not using tmux.')
            sleep(5.0)
            boto3_session = boto3.session.Session()
            s3 = boto3_session.client(
                service_name='s3',
                region_name=current_app.config['S3_REGION'],
                endpoint_url=current_app.config['S3_ENDPOINT'],
                aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
            )
            for community in Community.query.filter(Community.banned == False):
                did_something = False
                if community.icon_id:
                    did_something = True
                    move_file_to_s3(community.icon_id, s3)
                if community.image_id:
                    did_something = True
                    move_file_to_s3(community.image_id, s3)
                if did_something:
                    print(f'Moved image for community {community.link()}')

            for user in User.query.filter(User.deleted == False, User.banned == False,
                                          User.last_seen > utcnow() - timedelta(days=180)):
                did_something = False
                if user.avatar_id:
                    did_something = True
                    move_file_to_s3(user.avatar_id, s3)
                if user.cover_id:
                    did_something = True
                    move_file_to_s3(user.cover_id, s3)
                if did_something:
                    print(f'Moved image for user {user.link()}')
            s3.close()
            print('Done')

    @app.cli.command('move-post-images-to-s3')
    def move_post_images_to_s3():
        with app.app_context():
            from app.utils import move_file_to_s3
            import boto3
            processed = 0
            print('Beginning move of post images... this could take a long time. Use tmux.')
            local_post_image_ids = list(db.session.execute(text(
                'SELECT image_id FROM "post" WHERE deleted is false and image_id is not null and instance_id = 1 ORDER BY id DESC')).scalars())
            remote_post_image_ids = list(db.session.execute(text(
                'SELECT image_id FROM "post" WHERE deleted is false and image_id is not null and instance_id != 1 ORDER BY id DESC')).scalars())
            boto3_session = boto3.session.Session()
            s3 = boto3_session.client(
                service_name='s3',
                region_name=current_app.config['S3_REGION'],
                endpoint_url=current_app.config['S3_ENDPOINT'],
                aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
            )
            for post_image_id in local_post_image_ids:
                move_file_to_s3(post_image_id, s3)
                processed += 1
                if processed % 5:
                    print(processed)

            print('Finished moving local post images, doing remote ones now...')
            for post_image_id in remote_post_image_ids:
                move_file_to_s3(post_image_id, s3)
                processed += 1
                if processed % 5:
                    print(processed)
            s3.close()
            print('Done')

    @app.cli.command('move-more-post-images-to-s3')
    def move_more_post_images_to_s3():
        with app.app_context():
            import boto3
            server_name = current_app.config['SERVER_NAME']
            boto3_session = boto3.session.Session()
            s3 = boto3_session.client(
                service_name='s3',
                region_name=current_app.config['S3_REGION'],
                endpoint_url=current_app.config['S3_ENDPOINT'],
                aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
            )

            file_ids = list(db.session.execute(
                text(f'select id from "file" where source_url like \'https://{server_name}/static%\'')).scalars())
            for file_id in file_ids:
                file = File.query.get(file_id)
                content_type = guess_mime_type(file.source_url)
                new_path = file.source_url.replace('/static/media/', "/")
                s3_path = new_path.replace(f'https://{server_name}/', '')
                new_path = new_path.replace(server_name, current_app.config['S3_PUBLIC_URL'])
                local_file = file.source_url.replace(f'https://{server_name}/static/media/', 'app/static/media/')
                if os.path.isfile(local_file):
                    try:
                        s3.upload_file(local_file, current_app.config['S3_BUCKET'], s3_path,
                                       ExtraArgs={'ContentType': content_type})
                    except Exception as e:
                        print(f"Error uploading {local_file}: {e}")
                    os.unlink(local_file)
                    file.source_url = new_path
                    print(new_path)
                else:
                    print('Could not find ' + local_file)

                db.session.commit()
        print('Done')

    @app.cli.command("spaceusage")
    def spaceusage():
        with app.app_context():
            for user in User.query.all():
                filesize = user.filesize()
                num_content = user.num_content()
                if filesize > 0 and num_content > 0:
                    print(f'{user.id},"{user.ap_id}",{filesize},{num_content}')

    def list_files(directory):
        for root, dirs, files in os.walk(directory):
            for file in files:
                yield os.path.join(root, file)

    @app.cli.command("remove_orphan_files")
    def remove_orphan_files():
        """ Any user-uploaded file that does not have a corresponding entry in the File table should be deleted """
        with app.app_context():
            for file_path in list_files('app/static/media/users'):
                if 'thumbnail' in file_path:
                    f = File.query.filter(File.thumbnail_path == file_path).first()
                else:
                    f = File.query.filter(File.file_path == file_path).first()
                if f is None:
                    os.unlink(file_path)

    @app.cli.command("send_missed_notifs")
    def send_missed_notifs():
        with app.app_context():
            session = get_task_session()
            try:
                with patch_db_session(session):
                    site = Site.query.get(1)
                    users_to_notify = User.query.join(Notification, User.id == Notification.user_id).filter(
                        User.ap_id == None,
                        Notification.created_at > User.last_seen,
                        Notification.read == False,
                        User.email_unread_sent == False,  # they have not been emailed since last activity
                        User.email_unread == True  # they want to be emailed
                    ).all()

                    for user in users_to_notify:
                        notifications = Notification.query.filter(Notification.user_id == user.id, Notification.read == False,
                                                                  Notification.created_at > user.last_seen).all()
                        if notifications:
                            # Also get the top 20 posts since their last login
                            posts = Post.query.join(CommunityMember, Post.community_id == CommunityMember.community_id).filter(
                                CommunityMember.is_banned == False)
                            posts = posts.filter(CommunityMember.user_id == user.id)
                            if user.ignore_bots == 1:
                                posts = posts.filter(Post.from_bot == False)
                            if user.hide_nsfl == 1:
                                posts = posts.filter(Post.nsfl == False)
                            if user.hide_nsfw == 1:
                                posts = posts.filter(Post.nsfw == False)
                            domains_ids = blocked_domains(user.id)
                            if domains_ids:
                                posts = posts.filter(or_(Post.domain_id.not_in(domains_ids), Post.domain_id == None))
                            community_ids = blocked_communities(user.id)
                            if community_ids:
                                posts = posts.filter(Post.community_id.not_in(community_ids))
                            posts = posts.filter(Post.posted_at > user.last_seen).order_by(desc(Post.score))
                            posts = posts.limit(20).all()

                            # Send email!
                            send_email(_('[PieFed] You have unread notifications'),
                                       sender=f'{site.name} <{current_app.config["MAIL_FROM"]}>',
                                       recipients=[user.email],
                                       text_body=flask.render_template('email/unread_notifications.txt', user=user,
                                                                       notifications=notifications, site=site),
                                       html_body=flask.render_template('email/unread_notifications.html', user=user,
                                                                       notifications=notifications,
                                                                       posts=posts, site=site,
                                                                       domain=current_app.config['SERVER_NAME']))
                            user.email_unread_sent = True
                            db.session.commit()

            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    @app.cli.command("process_email_bounces")
    def process_email_bounces():
        with app.app_context():
            session = get_task_session()
            try:
                with patch_db_session(session):
                    import email

                    imap_host = current_app.config['BOUNCE_HOST']
                    imap_user = current_app.config['BOUNCE_USERNAME']
                    imap_pass = current_app.config['BOUNCE_PASSWORD']
                    something_deleted = False

                    if imap_host:

                        # connect to host using SSL
                        imap = imaplib.IMAP4_SSL(imap_host, port=993)

                        ## login to server
                        imap.login(imap_user, imap_pass)

                        imap.select('Inbox')

                        tmp, data = imap.search(None, 'ALL')
                        rgx = r'[\w\.-]+@[\w\.-]+'

                        emails = set()

                        for num in data[0].split():
                            tmp, data = imap.fetch(num, '(RFC822)')
                            email_message = email.message_from_bytes(data[0][1])
                            match = []
                            if not isinstance(email_message._payload, str):
                                if isinstance(email_message._payload[0]._payload, str):
                                    payload = email_message._payload[0]._payload.replace("\n", " ").replace("\r", " ")
                                    match = re.findall(rgx, payload)
                                elif isinstance(email_message._payload[0]._payload, list):
                                    if isinstance(email_message._payload[0]._payload[0]._payload, str):
                                        payload = email_message._payload[0]._payload[0]._payload.replace("\n", " ").replace("\r", " ")
                                        match = re.findall(rgx, payload)

                                for m in match:
                                    if current_app.config['SERVER_NAME'] not in m and current_app.config['SERVER_NAME'].upper() not in m:
                                        emails.add(m)
                                        print(str(num) + ' ' + m)

                            imap.store(num, '+FLAGS', '\\Deleted')
                            something_deleted = True

                        if something_deleted:
                            imap.expunge()
                            pass

                        imap.close()

                        # Keep track of how many times email to an account has bounced. After 2 bounces, disable email sending to them
                        for bounced_email in emails:
                            bounced_accounts = User.query.filter_by(email=bounced_email).all()
                            for account in bounced_accounts:
                                if account.bounces is None:
                                    account.bounces = 0
                                if account.bounces > 2:
                                    account.newsletter = False
                                    account.email_unread = False
                                else:
                                    account.bounces += 1
                            db.session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    @app.cli.command("clean_up_old_activities")
    def clean_up_old_activities():
        with app.app_context():
            session = get_task_session()
            try:
                with patch_db_session(session):
                    db.session.query(ActivityPubLog).filter(ActivityPubLog.created_at < utcnow() - timedelta(days=3)).delete()
                    db.session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    @app.cli.command("detect_vote_manipulation")
    def detect_vote_manipulation():
        with app.app_context():
            print('Getting user ids...')
            all_user_ids = [user.id for user in User.query.filter(User.last_seen > datetime.utcnow() - timedelta(days=7))]
            print('Checking...')
            for i, first_user_id in enumerate(all_user_ids):
                current_user_upvoted_posts = ['post/' + str(id) for id in recently_upvoted_posts(first_user_id)]
                current_user_upvoted_replies = ['reply/' + str(id) for id in recently_upvoted_post_replies(first_user_id)]

                current_user_upvotes = set(current_user_upvoted_posts + current_user_upvoted_replies)
                if len(current_user_upvotes) > 12:
                    print(i)
                    for j in range(i + 1, len(all_user_ids)):
                        other_user_id = all_user_ids[j]
                        if jaccard_similarity(current_user_upvotes, other_user_id) >= 95:
                            first_user = User.query.get(first_user_id)
                            other_user = User.query.get(other_user_id)
                            print(f'{first_user.link()} votes the same as {other_user.link()}')

    @app.cli.command("migrate_community_notifs")
    def migrate_community_notifs():
        with app.app_context():
            member_infos = CommunityMember.query.filter(CommunityMember.notify_new_posts == True,
                                                        CommunityMember.is_banned == False).all()
            for member_info in member_infos:
                new_notification = NotificationSubscription(user_id=member_info.user_id,
                                                            entity_id=member_info.community_id,
                                                            type=NOTIF_COMMUNITY)
                db.session.add(new_notification)
            db.session.commit()
            print('Done')

    @app.cli.command("migrate_post_notifs")
    def migrate_post_notifs():
        with app.app_context():
            posts = Post.query.filter(Post.notify_author == True).all()
            for post in posts:
                new_notification = NotificationSubscription(name=shorten_string(_('Replies to my post %(post_title)s',
                                                                                  post_title=post.title)),
                                                            user_id=post.user_id, entity_id=post.id,
                                                            type=NOTIF_POST)
                db.session.add(new_notification)
            db.session.commit()

            post_replies = PostReply.query.filter(PostReply.notify_author == True).all()
            for reply in post_replies:
                new_notification = NotificationSubscription(
                    name=shorten_string(_('Replies to my comment on %(post_title)s',
                                          post_title=reply.post.title)),
                    user_id=post.user_id, entity_id=reply.id,
                    type=NOTIF_REPLY)
                db.session.add(new_notification)
            db.session.commit()

            print('Done')

    @app.cli.command("populate_community_search")
    def populate_community_search():
        with app.app_context():
            # pull down the community.full.json
            print('Pulling in the lemmyverse data ...')
            resp = get_request('https://data.lemmyverse.net/data/community.full.json')
            community_json = resp.json()
            resp.close()

            # get the banned urls list
            print('Getting the banned domains list ...')
            banned_urls = list(db.session.execute(text('SELECT domain FROM "banned_instances"')).scalars())

            # iterate over the entries and get out the url and the nsfw status in a new list
            print('Generating the communities lists ...')
            all_communities = []
            all_sfw_communities = []
            for c in community_json:
                # skip if the domain is banned
                if c['baseurl'] in banned_urls:
                    continue

                # sort out any that have less than 50 posts
                elif c['counts']['posts'] < 50:
                    continue

                # sort out any that do not have greater than 10 active users over the past week
                elif c['counts']['users_active_week'] < 10:
                    continue

                # sort out the 'seven things you can't say on tv' names (cursewords), plus some
                # "low effort" communities
                seven_things_plus = [
                    'shit', 'piss', 'fuck',
                    'cunt', 'cocksucker', 'motherfucker', 'tits',
                    'piracy', '196', 'greentext', 'usauthoritarianism',
                    'enoughmuskspam', 'political_weirdos', '4chan'
                ]
                if any(badword in c['name'].lower() for badword in seven_things_plus):
                    continue

                # convert the url to server, community
                server, community = extract_domain_and_actor(c['url'])

                # if the community is nsfw append to the all_communites only, if sfw, append to both
                if c['nsfw']:
                    all_communities.append(f'{community}@{server}')
                else:
                    all_communities.append(f'{community}@{server}')
                    all_sfw_communities.append(f'{community}@{server}')

            # add those lists to dicts
            all_communities_json = {}
            all_sfw_communities_json = {}
            all_communities_json['all_communities'] = all_communities
            all_sfw_communities_json['all_sfw_communities'] = all_sfw_communities

            # write those files to disk as json
            print('Saving the communities lists to app/static/tmp/ ...')
            ensure_directory_exists('app/static/tmp')
            with open('app/static/tmp/all_communities.json', 'w') as acj:
                json.dump(all_communities_json, acj)

            with open('app/static/tmp/all_sfw_communities.json', 'w') as asfwcj:
                json.dump(all_sfw_communities_json, asfwcj)

            print('Getting disposable email domain list...')
            resp = get_request(
                'https://raw.githubusercontent.com/disposable/disposable-email-domains/master/domains.txt')
            if resp.status_code == 200:
                with open('app/static/tmp/disposable_domains.txt', 'w') as f:
                    f.write(resp.content.decode('utf-8'))
                resp.close()

            print('Done!')

    @app.cli.command("remove-unnecessary-images")
    def remove_unnecessary_images():
        # link posts only need a thumbnail but for a long time we have been generating both a thumbnail and a medium-sized image
        # AS OF JUN 2025 link posts DO need a medium-sized version, as some mobile apps need larger images. DO NOT RUN THIS.
        with app.app_context():
            import boto3
            sql = '''select file_path from "file" as f 
                    inner join "post" as p on p.image_id  = f.id 
                    where p.type = :type and f.file_path  is not null'''
            files = list(db.session.execute(text(sql), {'type': POST_TYPE_LINK}).scalars())
            s3_files_to_delete = []
            print('Gathering file list...')
            for file in files:
                if file:
                    if file.startswith(f'https://{current_app.config["S3_PUBLIC_URL"]}') and _store_files_in_s3():
                        s3_path = file.replace(f'https://{current_app.config["S3_PUBLIC_URL"]}/', '')
                        s3_files_to_delete.append(s3_path)
                    elif os.path.isfile(file):
                        try:
                            os.unlink(file)
                        except FileNotFoundError:
                            ...
            print(f'Sending list to S3 ({len(s3_files_to_delete)} files to delete)...')
            if len(s3_files_to_delete) > 0:
                boto3_session = boto3.session.Session()
                s3 = boto3_session.client(
                    service_name='s3',
                    region_name=current_app.config['S3_REGION'],
                    endpoint_url=current_app.config['S3_ENDPOINT'],
                    aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                    aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
                )

                # S3 can only process 1000 files per delete operation, so we need to batch
                batch_size = 1000
                total_deleted = 0

                for i in range(0, len(s3_files_to_delete), batch_size):
                    batch = s3_files_to_delete[i:i + batch_size]
                    delete_payload = {
                        'Objects': [{'Key': key} for key in batch],
                        'Quiet': True  # If True, successful deletions are not returned
                    }
                    s3.delete_objects(Bucket=current_app.config['S3_BUCKET'], Delete=delete_payload)
                    total_deleted += len(batch)
                    print(f'Deleted batch {i // batch_size + 1}, progress: {total_deleted}/{len(s3_files_to_delete)} files')

                s3.close()
            print(f'Done, {len(s3_files_to_delete)} files deleted.')

    @app.cli.command("populate_post_reply_for_api")
    def populate_post_reply_for_api():
        with app.app_context():
            sql = '''WITH RECURSIVE reply_path AS (
                        -- Base case: each reply starts with its own ID
                        SELECT
                            id,
                            parent_id,
                            ARRAY[0, id] AS path
                        FROM post_reply
                        WHERE parent_id IS NULL  -- Top-level replies
                    
                        UNION ALL
                    
                        -- Recursive case: build the path from parent replies
                        SELECT
                            pr.id,
                            pr.parent_id,
                            rp.path || pr.id  -- Append current reply ID to the parent's path
                        FROM post_reply pr
                        JOIN reply_path rp ON pr.parent_id = rp.id
                    )
                    UPDATE post_reply
                    SET path = reply_path.path
                    FROM reply_path
                    WHERE post_reply.id = reply_path.id;
                    '''
            db.session.execute(text(sql))
            db.session.commit()

            db.session.execute(text('update post_reply set child_count = 0'))

            sql = '''WITH unnested_paths AS (
                        SELECT
                            UNNEST(path[2:array_length(path, 1) - 1]) AS parent_id  -- Exclude the last element (self)
                        FROM post_reply
                        WHERE array_length(path, 1) > 2  -- Ensure the path contains at least one parent
                    ),
                    child_counts AS (
                        SELECT
                            parent_id,
                            COUNT(*) AS child_count
                        FROM unnested_paths
                        GROUP BY parent_id
                    )
                    UPDATE post_reply
                    SET child_count = COALESCE(child_counts.child_count, 0)
                    FROM child_counts
                    WHERE post_reply.id = child_counts.parent_id;'''
            db.session.execute(text(sql))
            db.session.commit()

            sql = 'UPDATE post_reply SET root_id = path[1] WHERE path IS NOT NULL;'
            db.session.execute(text(sql))
            db.session.commit()

            print('Done!')

    @app.cli.command("config_check")
    def config_check():
        """Perform basic sanity checks on site configuration."""
        with app.app_context():
            session = get_task_session()
            try:
                with patch_db_session(session):
                    print("PieFed Configuration Check")
                    print("=" * 40)
                    errors = []
                    warnings = []

                    # Check required environment variables and their formats
                    print("\n1. Checking required environment variables...")

                    # Check SERVER_NAME
                    server_name = current_app.config.get('SERVER_NAME')
                    if not server_name or server_name == 'localhost':
                        errors.append("   ❌ SERVER_NAME is not set or using default value")
                    else:
                        # Check for common format issues
                        format_issues = []
                        if server_name.startswith('http://') or server_name.startswith('https://'):
                            format_issues.append("should not include protocol (http:// or https://)")
                        if server_name.endswith('/'):
                            format_issues.append("should not end with trailing slash")
                        if ' ' in server_name:
                            format_issues.append("should not contain spaces")
                        if server_name.startswith("'") and server_name.endswith("'"):
                            format_issues.append("should not be wrapped in quotes")

                        if format_issues:
                            errors.append(f"   ❌ SERVER_NAME format issues: {', '.join(format_issues)}")
                        else:
                            print(f"   ✅ SERVER_NAME is configured: {server_name}")

                    # Check SECRET_KEY
                    secret_key = current_app.config.get('SECRET_KEY')
                    if not secret_key or secret_key == 'you-will-never-guesss' or secret_key == 'change this to random characters':
                        errors.append("   ❌ SECRET_KEY is not set or using default value")
                    elif len(secret_key) < 32:
                        warnings.append("   ⚠️  SECRET_KEY should be at least 32 characters long")
                        print("   ✅ SECRET_KEY is configured (but short)")
                    else:
                        print("   ✅ SECRET_KEY is configured")

                    # Check DATABASE_URL
                    database_url = current_app.config.get('SQLALCHEMY_DATABASE_URI')
                    if not database_url:
                        errors.append("   ❌ DATABASE_URL is not set")
                    elif database_url.startswith('sqlite://'):
                        warnings.append("   ⚠️  Using SQLite database - consider PostgreSQL for production")
                        print("   ✅ DATABASE_URL is configured (SQLite)")
                    elif database_url.startswith('postgresql'):
                        print("   ✅ DATABASE_URL is configured (PostgreSQL)")
                    else:
                        print("   ✅ DATABASE_URL is configured")

                    # Check numeric environment variables
                    print("\n   Checking numeric environment variables...")
                    numeric_vars = {
                        'MAIL_PORT': current_app.config.get('MAIL_PORT'),
                        'DB_POOL_SIZE': current_app.config.get('DB_POOL_SIZE'),
                        'DB_MAX_OVERFLOW': current_app.config.get('DB_MAX_OVERFLOW'),
                    }

                    for var, value in numeric_vars.items():
                        if value is not None:
                            try:
                                int_val = int(value)
                                if var == 'MAIL_PORT' and (int_val < 1 or int_val > 65535):
                                    warnings.append(f"   ⚠️  {var} should be between 1 and 65535")
                                else:
                                    print(f"   ✅ {var} is valid: {int_val}")
                            except (ValueError, TypeError):
                                errors.append(f"   ❌ {var} should be a number, got: {value}")

                    # Check boolean environment variables
                    print("\n   Checking boolean environment variables...")
                    boolean_vars = ['MAIL_USE_TLS', 'SESSION_COOKIE_SECURE', 'SESSION_COOKIE_HTTPONLY']
                    for var in boolean_vars:
                        env_value = os.environ.get(var)
                        if env_value is not None:
                            if env_value.lower() in ['true', 'false', '1', '0', 'yes', 'no']:
                                print(f"   ✅ {var} is valid: {env_value}")
                            else:
                                warnings.append(f"   ⚠️  {var} should be true/false or 1/0, got: {env_value}")

                    # Check URL format variables
                    print("\n   Checking URL format variables...")
                    url_vars = {
                        'CACHE_REDIS_URL': current_app.config.get('CACHE_REDIS_URL'),
                        'CELERY_BROKER_URL': current_app.config.get('CELERY_BROKER_URL'),
                        'SENTRY_DSN': current_app.config.get('SENTRY_DSN'),
                        'S3_ENDPOINT': current_app.config.get('S3_ENDPOINT'),
                        'S3_PUBLIC_URL': current_app.config.get('S3_PUBLIC_URL'),
                    }

                    for var, value in url_vars.items():
                        if value:
                            format_issues = []
                            if var in ['CACHE_REDIS_URL', 'CELERY_BROKER_URL'] and not (value.startswith('redis://') or value.startswith('unix://')):
                                format_issues.append("should start with redis:// or unix://")
                            if var == 'SENTRY_DSN' and not (value.startswith('https://') or value.startswith('http://')):
                                format_issues.append("should start with https:// or http://")
                            if var in ['S3_ENDPOINT'] and not (value.startswith('https://') or value.startswith('http://')):
                                format_issues.append("should start with https:// or http://")
                            if var in ['S3_PUBLIC_URL'] and value.startswith('https://'):
                                format_issues.append("should start with https://")
                            if value.endswith('/') and var in ['S3_ENDPOINT', 'S3_PUBLIC_URL']:
                                format_issues.append("should not end with trailing slash")

                            if format_issues:
                                warnings.append(f"   ⚠️  {var} format issues: {', '.join(format_issues)}")
                            else:
                                print(f"   ✅ {var} format is valid")

                    # Check database connection
                    print("\n2. Checking database connection...")
                    try:
                        db.session.execute(text('SELECT 1'))
                        print("   ✅ Database connection successful")
                    except Exception as e:
                        errors.append(f"   ❌ Database connection failed: {e}")

                    # Check Redis connection
                    print("\n3. Checking Redis connection...")
                    try:
                        redis = get_redis_connection()
                        if redis:
                            redis.ping()
                            print("   ✅ Redis connection successful")
                        else:
                            warnings.append("   ⚠️  Redis connection could not be established")
                    except Exception as e:
                        warnings.append(f"   ⚠️  Redis connection failed: {e}")

                    # Check write access to critical directories
                    print("\n4. Checking directory write permissions...")
                    critical_dirs = [
                        'app/static/tmp',
                        'app/static/media',
                        'logs'
                    ]

                    for dir_path in critical_dirs:
                        try:
                            if not os.path.exists(dir_path):
                                os.makedirs(dir_path, exist_ok=True)

                            # Test write access
                            test_file = os.path.join(dir_path, 'config_check_test.txt')
                            with open(test_file, 'w') as f:
                                f.write('test')
                            os.remove(test_file)
                            print(f"   ✅ {dir_path} is writable")
                        except Exception as e:
                            errors.append(f"   ❌ {dir_path} is not writable: {e}")

                    # Check mail configuration
                    print("\n5. Checking mail configuration...")
                    mail_server = current_app.config.get('MAIL_SERVER')
                    if mail_server:
                        print(f"   ✅ Mail server configured: {mail_server}. Visit /test_email in your browser to test.")
                        mail_from = current_app.config.get('MAIL_FROM')
                        if mail_from:
                            print(f"   ✅ Mail from address: {mail_from}")
                        else:
                            warnings.append("   ⚠️  MAIL_FROM not configured")
                        errors_to = current_app.config.get('ERRORS_TO')
                        if errors_to:
                            print(f"   ✅ Error messages sent to: {errors_to}")
                    else:
                        warnings.append("   ⚠️  Mail server not configured - email functionality will be disabled")

                    # Check cache configuration
                    print("\n6. Checking cache configuration...")
                    cache_type = current_app.config.get('CACHE_TYPE')
                    print(f"   ✅ Cache type: {cache_type}")

                    if cache_type == 'FileSystemCache':
                        cache_dir = current_app.config.get('CACHE_DIR')
                        if cache_dir:
                            try:
                                os.makedirs(cache_dir, exist_ok=True)
                                print(f"   ✅ Cache directory: {cache_dir}")
                            except Exception as e:
                                errors.append(f"   ❌ Cache directory not accessible: {e}")

                    # Check S3 configuration (if used)
                    print("\n7. Checking S3 configuration...")
                    s3_bucket = current_app.config.get('S3_BUCKET')
                    if s3_bucket:
                        s3_required = ['S3_REGION', 'S3_ENDPOINT', 'S3_ACCESS_KEY', 'S3_ACCESS_SECRET', 'S3_PUBLIC_URL']
                        s3_missing = []
                        for var in s3_required:
                            if not current_app.config.get(var):
                                s3_missing.append(var)

                        if s3_missing:
                            warnings.append(f"   ⚠️  S3 partially configured, missing: {', '.join(s3_missing)}")
                        else:
                            print("   ✅ S3 configuration appears complete")
                    else:
                        print("   ℹ️  S3 not configured (local file storage will be used)")

                    # Check ActivityPub configuration
                    print("\n8. Checking ActivityPub configuration...")
                    server_name = current_app.config.get('SERVER_NAME')
                    if server_name and server_name != 'localhost' and '127.0.0.1' not in server_name:
                        print(f"   ✅ Server name configured for federation: {server_name}")

                        # Check if we have a site with keys
                        site = Site.query.first()
                        if site and site.private_key and site.public_key:
                            print("   ✅ Site ActivityPub keys are configured")
                        else:
                            warnings.append("   ⚠️  Site ActivityPub keys not found - run 'flask init-db' if this is a new installation")
                    else:
                        warnings.append("   ⚠️  SERVER_NAME not properly configured for federation")

                    admin = User.query.get(1)
                    if admin and admin.private_key and admin.public_key:
                        print("   ✅ Admin user configured")
                    else:
                        warnings.append("   ⚠️  Admin user not found - run 'flask init-db' if this is a new installation")

                    # Check migration system
                    print("\n9. Checking database migration system...")
                    try:
                        inspector = db.inspect(db.engine)
                        if 'alembic_version' in inspector.get_table_names():
                            print("   ✅ Database migration system is initialized")
                        else:
                            errors.append("   ❌ alembic_version table not found")
                    except Exception as e:
                        errors.append(f"   ❌ Error checking database migration system: {e}")

                    # Summary
                    print("\n" + "=" * 40)
                    print("CONFIGURATION CHECK SUMMARY")
                    print("=" * 40)

                    if not errors and not warnings:
                        print("✅ All checks passed! Your PieFed configuration looks good.")
                    else:
                        if errors:
                            print(f"❌ {len(errors)} error(s) found:")
                            for error in errors:
                                print(error)

                        if warnings:
                            print(f"\n⚠️  {len(warnings)} warning(s) found:")
                            for warning in warnings:
                                print(warning)

                        if errors:
                            print("\n❌ Configuration has critical issues that need to be resolved.")
                            exit(1)
                        else:
                            print("\n⚠️  Configuration has warnings but should work.")

                    print("\nFor more information, see the installation documentation.")
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()


def parse_communities(interests_source, segment):
    lines = interests_source.split("\n")
    include_in_output = False
    output = []

    for line in lines:
        line = line.strip()
        if line == segment:
            include_in_output = True
            continue
        elif line == '':
            include_in_output = False
        if include_in_output:
            output.append(line)

    return "\n".join(output)
