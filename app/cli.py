# if commands in this file are not working (e.g. 'flask translate') make sure you set the FLASK_APP environment variable.
# e.g. export FLASK_APP=pyfedi.py
import imaplib
import re
from datetime import datetime, timedelta
from random import randint
from time import sleep

import flask
import httpx
from flask import json, current_app
from flask_babel import _
from sqlalchemy import inspect, or_, desc, text

from app import db
import click
import os

from app.activitypub.signature import RsaKeys
from app.activitypub.util import find_actor_or_create
from app.auth.util import random_token
from app.constants import NOTIF_COMMUNITY, NOTIF_POST, NOTIF_REPLY
from app.email import send_verification_email, send_email
from app.models import Settings, BannedInstances, Interest, Role, User, RolePermission, Domain, ActivityPubLog, \
    utcnow, Site, Instance, File, Notification, Post, CommunityMember, NotificationSubscription, PostReply, Language, \
    Tag, InstanceRole, Community
from app.post.routes import post_delete_post
from app.utils import file_get_contents, retrieve_block_list, blocked_domains, retrieve_peertube_block_list, \
    shorten_string, get_request, html_to_text, blocked_communities, ap_datetime, gibberish, get_request_instance, \
    instance_banned


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
    def keys():
        private_key, public_key = RsaKeys.generate_keypair()
        u: User = User.query.get(1)
        u.private_key = private_key
        u.public_key = public_key
        db.session.commit()
        print('Admin keys have been reset')

    @app.cli.command("init-db")
    @click.option("--force", is_flag=True, help="Force reinitialize the database.")
    def init_db(force):
        inspector = inspect(db.engine)
        tables_exist = 'user' in inspector.get_table_names()
        if not force and tables_exist:
            print("The database has already been initialized. Use --force to reinitialize.")
            return
        with app.app_context():
            db.drop_all()
            db.configure_mappers()
            db.create_all()
            private_key, public_key = RsaKeys.generate_keypair()
            db.session.add(Site(name="PieFed", description='Explore Anything, Discuss Everything.', public_key=public_key, private_key=private_key))
            db.session.add(Instance(domain=app.config['SERVER_NAME'], software='PieFed'))   # Instance 1 is always the local instance
            db.session.add(Settings(name='allow_nsfw', value=json.dumps(False)))
            db.session.add(Settings(name='allow_nsfl', value=json.dumps(False)))
            db.session.add(Settings(name='allow_dislike', value=json.dumps(True)))
            db.session.add(Settings(name='allow_local_image_posts', value=json.dumps(True)))
            db.session.add(Settings(name='allow_remote_image_posts', value=json.dumps(True)))
            db.session.add(Settings(name='federation', value=json.dumps(True)))
            db.session.add(Language(name='Undetermined', code='und'))
            db.session.add(Language(name='English', code='en'))
            banned_instances = ['anonib.al','lemmygrad.ml', 'gab.com', 'rqd2.net', 'exploding-heads.com', 'hexbear.net',
                                'threads.net', 'noauthority.social', 'pieville.net', 'links.hackliberty.org',
                                'poa.st', 'freespeechextremist.com', 'bae.st', 'nicecrew.digital', 'detroitriotcity.com',
                                'pawoo.net', 'shitposter.club', 'spinster.xyz', 'catgirl.life', 'gameliberty.club',
                                'yggdrasil.social', 'beefyboys.win', 'brighteon.social', 'cum.salon']
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

            # Initial roles
            # These roles will create rows in the 'role' table with IDs of 1,2,3,4. There are some constants (ROLE_*) in
            # constants.py that will need to be updated if the role IDs ever change.
            anon_role = Role(name='Anonymous user', weight=0)
            anon_role.permissions.append(RolePermission(permission='register'))
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
            db.session.add(admin_role)

            # Admin user
            if os.environ.get('MODE') == 'development':
                user_name = "pyfedi_admin"
                email = "pyfedi_admin@example.com"
                password = "pyfedi"
            else:
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

    @app.cli.command('daily-maintenance')
    def daily_maintenance():
        with app.app_context():
            # Remove old content from communities
            communities = Community.query.filter(Community.content_retention > 0).all()
            for community in communities:
                cut_off = utcnow() - timedelta(days=community.content_retention)
                old_posts = Post.query.filter_by(deleted=False, sticky=False, community_id=community.id).filter(Post.posted_at < cut_off).all()
                for post in old_posts:
                    post_delete_post(community, post, post.user_id, federate_all_communities=False)
                    community.post_count -= 1

            # Ensure accurate count of posts associated with each hashtag
            for tag in Tag.query.all():
                post_count = db.session.execute(text('SELECT COUNT(post_id) as c FROM "post_tag" WHERE tag_id = :tag_id'),
                                                { 'tag_id': tag.id}).scalar()
                tag.post_count = post_count
                db.session.commit()

            # Delete soft-deleted content after 7 days
            for post_reply in PostReply.query.filter(PostReply.deleted == True,
                                                     PostReply.posted_at < utcnow() - timedelta(days=7)).all():
                post_reply.delete_dependencies()
                if not post_reply.has_replies():
                    db.session.delete(post_reply)
            db.session.commit()

            for post in Post.query.filter(Post.deleted == True,
                                          Post.posted_at < utcnow() - timedelta(days=7)).all():
                post.delete_dependencies()
                db.session.delete(post)
            db.session.commit()

            # Delete voting data after 6 months
            db.session.execute(text('DELETE FROM "post_vote" WHERE created_at < :cutoff'), {'cutoff': utcnow() - timedelta(days=28 * 6)})
            db.session.execute(text('DELETE FROM "post_reply_vote" WHERE created_at < :cutoff'), {'cutoff': utcnow() - timedelta(days=28 * 6)})
            db.session.commit()

            # Check for dormant or dead instances
            try:
                # Check for dormant or dead instances
                instances = Instance.query.filter(Instance.gone_forever == False, Instance.id != 1).all()
                HEADERS = {'User-Agent': 'PieFed/1.0', 'Accept': 'application/activity+json'}

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
                                        break
                                    else:
                                        instance.failures += 1
                            elif nodeinfo.status_code >= 400:
                                current_app.logger.info(f"{instance.domain} has no well-known/nodeinfo response")
                        except Exception as e:
                            db.session.rollback()
                            current_app.logger.error(f"Error processing instance {instance.domain}: {e}")
                        finally:
                            nodeinfo.close()

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
                            elif node.status_code >= 400:
                                instance.nodeinfo_href = None
                        except Exception as e:
                            db.session.rollback()
                            current_app.logger.error(f"Error processing nodeinfo for {instance.domain}: {e}")
                        finally:
                            node.close()

                    # Handle admin roles
                    if instance.online() and (instance.software == 'lemmy' or instance.software == 'piefed'):
                        try:
                            response = get_request(f'https://{instance.domain}/api/v3/site')
                            if response and response.status_code == 200:
                                instance_data = response.json()
                                admin_profile_ids = []
                                for admin in instance_data['admins']:
                                    admin_profile_ids.append(admin['person']['actor_id'].lower())
                                    user = find_actor_or_create(admin['person']['actor_id'])
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
                        except Exception as e:
                            db.session.rollback()
                            current_app.logger.error(f"Error updating admins for {instance.domain}: {e}")
                        finally:
                            if response:
                                response.close()

                # Commit all changes at once
                db.session.commit()

            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Error in daily maintenance: {e}")

                                # remove any InstanceRoles that are no longer part of instance-data['admins']
                                #for instance_admin in InstanceRole.query.filter_by(instance_id=instance.id):
                                #    if instance_admin.user.profile_id() not in admin_profile_ids:
                                #        db.session.query(InstanceRole).filter(
                                #            InstanceRole.user_id == instance_admin.user.id,
                                #            InstanceRole.instance_id == instance.id,
                                #            InstanceRole.role == 'admin').delete()
                                #        db.session.commit()

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
                                                               notifications=notifications),
                               html_body=flask.render_template('email/unread_notifications.html', user=user,
                                                               notifications=notifications,
                                                               posts=posts,
                                                               domain=current_app.config['SERVER_NAME']))
                    user.email_unread_sent = True
                    db.session.commit()

    @app.cli.command("process_email_bounces")
    def process_email_bounces():
        with app.app_context():
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

    @app.cli.command("clean_up_old_activities")
    def clean_up_old_activities():
        with app.app_context():
            db.session.query(ActivityPubLog).filter(ActivityPubLog.created_at < utcnow() - timedelta(days=3)).delete()
            db.session.commit()

    @app.cli.command("migrate_community_notifs")
    def migrate_community_notifs():
        with app.app_context():
            member_infos = CommunityMember.query.filter(CommunityMember.notify_new_posts == True,
                                                       CommunityMember.is_banned == False).all()
            for member_info in member_infos:
                new_notification = NotificationSubscription(user_id=member_info.user_id, entity_id=member_info.community_id,
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
                new_notification = NotificationSubscription(name=shorten_string(_('Replies to my comment on %(post_title)s',
                                                                              post_title=reply.post.title)),
                                                            user_id=post.user_id, entity_id=reply.id,
                                                            type=NOTIF_REPLY)
                db.session.add(new_notification)
            db.session.commit()

            print('Done')


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
