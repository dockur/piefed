from __future__ import annotations

import html
import math
import os
import uuid
import re
from collections import defaultdict
from datetime import datetime, timedelta
from time import time
from typing import List, Union
from urllib.parse import urlparse, parse_qs, urlencode
from zoneinfo import ZoneInfo

import arrow
import jwt
from flask import current_app, g, json
from flask_babel import _, lazy_gettext as _l
from flask_babel import force_locale, gettext
from flask_login import UserMixin, current_user
from flask_sqlalchemy.query import Query
from furl import furl
from slugify import slugify
from sqlalchemy import or_, text, desc, Index, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.dialects.postgresql import BIT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy_searchable import SearchQueryMixin
from sqlalchemy_utils.types import \
    TSVectorType  # https://sqlalchemy-searchable.readthedocs.io/en/latest/installation.html
from werkzeug.security import generate_password_hash, check_password_hash

from app import db, login, cache, celery, httpx_client, constants, app_bcrypt
from app.constants import SUBSCRIPTION_NONMEMBER, SUBSCRIPTION_MEMBER, SUBSCRIPTION_MODERATOR, SUBSCRIPTION_OWNER, \
    SUBSCRIPTION_BANNED, SUBSCRIPTION_PENDING, NOTIF_USER, NOTIF_COMMUNITY, NOTIF_TOPIC, NOTIF_POST, NOTIF_REPLY, \
    ROLE_ADMIN, ROLE_STAFF, NOTIF_FEED, NOTIF_DEFAULT, NOTIF_REPORT, NOTIF_MENTION, POST_STATUS_REVIEWING, \
    POST_STATUS_PUBLISHED, POST_TYPE_VIDEO, INVITE_MEMBERS_ONLY, INVITE_MODS_ONLY, INVITE_OWNER_ONLY


def utcnow(naive=True):
    if naive:
        return datetime.now(ZoneInfo('UTC')).replace(tzinfo=None)
    return datetime.now(ZoneInfo('UTC'))


class PostReplyValidationError(Exception):
    """Custom exception for PostReply validation errors"""
    pass


class FullTextSearchQuery(Query, SearchQueryMixin):
    pass


class BannedInstances(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True)
    reason = db.Column(db.String(256))
    initiator = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=utcnow)
    subscription_id = db.Column(db.Integer, db.ForeignKey('defederation_subscription.id'), index=True)  # is None when the ban was done by a local admin


class AllowedInstances(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class DefederationSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True)


class Instance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True, unique=True)
    inbox = db.Column(db.String(256))
    shared_inbox = db.Column(db.String(256))
    outbox = db.Column(db.String(256))
    software = db.Column(db.String(50))
    version = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime, default=utcnow)  # When an Activity was received from them
    last_successful_send = db.Column(db.DateTime)  # When we successfully sent them an Activity
    failures = db.Column(db.Integer, default=0)  # How many days that we have been unable to send (reset to 0 after every successful send)
    most_recent_attempt = db.Column(db.DateTime)  # When the most recent failure was
    dormant = db.Column(db.Boolean, default=False)  # True once this instance is considered offline and not worth sending to any more (5 days offline)
    start_trying_again = db.Column(db.DateTime)  # When to start trying again.
    gone_forever = db.Column(db.Boolean, default=False)  # True once this instance is considered offline forever - never start trying again (12 days offline)
    ip_address = db.Column(db.String(50))
    trusted = db.Column(db.Boolean, default=False, index=True)
    posting_warning = db.Column(db.String(512))
    nodeinfo_href = db.Column(db.String(100))
    admin_note = db.Column(db.Text)

    posts = db.relationship('Post', backref='instance', lazy='dynamic')
    post_replies = db.relationship('PostReply', backref='instance', lazy='dynamic')
    communities = db.relationship('Community', backref='instance', lazy='dynamic')

    def online(self):
        return not (self.dormant or self.gone_forever)

    def user_is_admin(self, user_id):
        role = InstanceRole.query.filter_by(instance_id=self.id, user_id=user_id).first()
        return role and role.role == 'admin'

    def votes_are_public(self):
        if self.trusted is True:  # only vote privately with untrusted instances
            return False
        return self.software.lower() == 'lemmy' or self.software.lower() == 'mbin' or self.software.lower() == 'kbin' or self.software.lower() == 'guppe groups'

    def post_count(self):
        return db.session.execute(text('SELECT COUNT(*) as c FROM "post" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def post_replies_count(self):
        return db.session.execute(text('SELECT COUNT(*) as c FROM "post_reply" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def known_communities_count(self):
        return db.session.execute(text('SELECT COUNT(*) as c FROM "community" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def known_users_count(self):
        return db.session.execute(text('SELECT COUNT(*) as c FROM "user" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def update_dormant_gone(self):
        if self.failures > 7 and self.dormant == True:
            self.gone_forever = True
        elif self.failures > 2 and self.dormant == False:
            self.dormant = True

    def can_poll(self):
        return self.software != 'lemmy'

    def can_event(self):
        return self.software != 'lemmy'

    def __repr__(self):
        return '<Instance {}>'.format(self.domain)

    @classmethod
    def unique_software_names(cls):
        return list(db.session.execute(text('SELECT DISTINCT software FROM instance ORDER BY software')).scalars())


class InstanceRole(db.Model):
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    role = db.Column(db.String(50), default='admin')

    user = db.relationship('User', lazy='joined')


# Instances that this user has blocked
class InstanceBlock(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow)


# Instances that have banned this user
class InstanceBan(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), primary_key=True)
    banned_until = db.Column(db.DateTime)


class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    reported = db.Column(db.Boolean, default=False)
    read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow)

    initiator = db.relationship('User', backref=db.backref('conversations_initiated', lazy='dynamic'),
                                foreign_keys=[user_id])
    messages = db.relationship('ChatMessage', backref=db.backref('conversation'), cascade='all,delete',
                               lazy='dynamic')

    def member_names(self, user_id):
        retval = []
        for member in self.members:
            if member.id != user_id:
                retval.append(member.display_name())
        return ', '.join(retval)

    def is_member(self, user):
        for member in self.members:
            if member.id == user.id:
                return True
        return False

    def instances(self):
        retval = []
        for member in self.members:
            if member.instance.id != 1 and member.instance not in retval:
                retval.append(member.instance)
        return retval
    
    def delete_if_abandoned(self):
        # Delete the conversation if all the participants are either remote or have left the conversation
        keep_convo = False
        for member in self.members:
            if member.is_local():
                joined = db.session.execute(text("SELECT joined FROM conversation_member WHERE user_id = :person_id AND conversation_id = :conversation_id"),
                                            {"person_id": member.id, "conversation_id": self.id}).first()
                
                # Returns None or a tuple, need to make it into a bool
                if joined and any(joined):
                    # There is still a local user joined to this conversation, just break and don't delete the convo
                    keep_convo = True
                    break
        
        if not keep_convo:
            # Delete the conversation
            Report.query.filter(Report.suspect_conversation_id == self.id).delete()
            db.session.delete(self)
            db.session.commit()

    def last_ap_id(self, sender_id):
        for message in self.messages.filter(ChatMessage.sender_id == sender_id).order_by(
                desc(ChatMessage.created_at)).limit(50):
            if message.ap_id:
                return message.ap_id
        return ''
        # most_recent_message = self.messages.order_by(desc(ChatMessage.created_at)).first()
        # if most_recent_message and most_recent_message.ap_id:
        #    return f"{current_app.config.server_name()}/private_message/{most_recent_message.id}"
        # else:
        #    return ''

    @staticmethod
    def find_existing_conversation(recipient, sender):
        sql = """SELECT 
                    c.id AS conversation_id, 
                    c.created_at AS conversation_created_at, 
                    c.updated_at AS conversation_updated_at, 
                    cm1.user_id AS user1_id, 
                    cm2.user_id AS user2_id 
                FROM 
                    public.conversation AS c 
                JOIN 
                    public.conversation_member AS cm1 ON c.id = cm1.conversation_id 
                JOIN 
                    public.conversation_member AS cm2 ON c.id = cm2.conversation_id 
                WHERE 
                    cm1.user_id = :user_id_1 AND 
                    cm2.user_id = :user_id_2 AND 
                    cm1.user_id <> cm2.user_id;"""
        ec = db.session.execute(text(sql), {'user_id_1': recipient.id, 'user_id_2': sender.id}).fetchone()
        return db.session.query(Conversation).get(ec[0]) if ec else None


conversation_member = db.Table('conversation_member',
                               db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
                               db.Column('conversation_id', db.Integer, db.ForeignKey('conversation.id')),
                               db.Column('joined', db.Boolean, default=True),
                               db.PrimaryKeyConstraint('user_id', 'conversation_id')
                               )


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversation.id'), index=True)
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    reported = db.Column(db.Boolean, default=False)
    read = db.Column(db.Boolean, default=False)
    encrypted = db.Column(db.String(15))
    created_at = db.Column(db.DateTime, default=utcnow)
    edited_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    deleted = db.Column(db.Boolean, default=False)

    ap_id = db.Column(db.String(255), index=True, unique=True)

    sender = db.relationship('User', foreign_keys=[sender_id])


class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), index=True)  # lowercase version of tag, e.g. solarstorm
    display_as = db.Column(db.String(256))  # Version of tag with uppercase letters, e.g. SolarStorm
    post_count = db.Column(db.Integer, default=0)
    banned = db.Column(db.Boolean, default=False, index=True)


class Licence(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))


class Language(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(5), index=True)
    name = db.Column(db.String(50))


class CommunityInvitation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(25), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    inviter_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=utcnow)


community_language = db.Table('community_language',
                              db.Column('community_id', db.Integer, db.ForeignKey('community.id')),
                              db.Column('language_id', db.Integer, db.ForeignKey('language.id')),
                              db.PrimaryKeyConstraint('community_id', 'language_id')
                              )

post_tag = db.Table('post_tag', db.Column('post_id', db.Integer, db.ForeignKey('post.id')),
                    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id')),
                    db.PrimaryKeyConstraint('post_id', 'tag_id')
                    )

post_flair = db.Table('post_flair', db.Column('post_id', db.Integer, db.ForeignKey('post.id')),
                      db.Column('flair_id', db.Integer, db.ForeignKey('community_flair.id')),
                      db.PrimaryKeyConstraint('post_id', 'flair_id')
                      )


post_file = db.Table('post_file', db.Column('post_id', db.Integer, db.ForeignKey('post.id')),
                      db.Column('file_id', db.Integer, db.ForeignKey('file.id')),
                      db.Column('weight', db.Integer),
                      db.PrimaryKeyConstraint('post_id', 'file_id')
                      )


class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    file_path = db.Column(db.String(255))
    file_name = db.Column(db.String(255))
    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    alt_text = db.Column(db.String(1500))
    source_url = db.Column(db.String(1024))
    thumbnail_path = db.Column(db.String(255))
    thumbnail_width = db.Column(db.Integer)
    thumbnail_height = db.Column(db.Integer)
    hash = db.Column(BIT(256), index=True)

    def view_url(self, resize=False):
        if self.source_url:
            if resize and '/pictrs/' in self.source_url and '?' not in self.source_url:
                return f'{self.source_url}?thumbnail=1024'
            else:
                return self.source_url
        elif self.file_path:
            if self.file_path.startswith('http'):
                return self.file_path
            file_path = self.file_path[4:] if self.file_path.startswith('app/') else self.file_path
            return f"{current_app.config['SERVER_URL']}/{file_path}"
        else:
            return ''

    def medium_url(self):
        if self.file_path is None:
            return self.thumbnail_url()
        if self.file_path.startswith('http'):
            return self.file_path
        file_path = self.file_path[4:] if self.file_path.startswith('app/') else self.file_path
        return f"{current_app.config['SERVER_URL']}/{file_path}"

    def thumbnail_url(self):
        if self.thumbnail_path is None:
            if self.source_url:
                return self.source_url
            else:
                return ''
        if self.thumbnail_path.startswith('http'):
            return self.thumbnail_path
        thumbnail_path = self.thumbnail_path[4:] if self.thumbnail_path.startswith('app/') else self.thumbnail_path
        return f"{current_app.config['SERVER_URL']}/{thumbnail_path}"   # image paths must include fqdn (not just starting with /) because apps need to make a request from outside

    def delete_from_disk(self, purge_cdn=True):
        purge_from_cache = []
        s3_files_to_delete = []
        if self.file_path:
            if self.file_path.startswith(f'https://{current_app.config["S3_PUBLIC_URL"]}') and _store_files_in_s3():
                s3_path = self.file_path.replace(f'https://{current_app.config["S3_PUBLIC_URL"]}/', '')
                s3_files_to_delete.append(s3_path)
                purge_from_cache.append(self.file_path)
            elif os.path.isfile(self.file_path):
                try:
                    os.unlink(self.file_path)
                except FileNotFoundError:
                    ...
                purge_from_cache.append(self.file_path.replace('app/', f"{current_app.config['SERVER_URL']}/"))

        if self.thumbnail_path:
            if self.thumbnail_path.startswith(
                    f'https://{current_app.config["S3_PUBLIC_URL"]}') and _store_files_in_s3():
                s3_path = self.thumbnail_path.replace(f'https://{current_app.config["S3_PUBLIC_URL"]}/', '')
                s3_files_to_delete.append(s3_path)
                purge_from_cache.append(self.thumbnail_path)
            elif os.path.isfile(self.thumbnail_path):
                try:
                    os.unlink(self.thumbnail_path)
                except FileNotFoundError:
                    ...
                purge_from_cache.append(
                    self.thumbnail_path.replace('app/', f"{current_app.config['SERVER_URL']}/"))
        if self.source_url:
            if self.source_url.startswith(f'https://{current_app.config["S3_PUBLIC_URL"]}') and _store_files_in_s3():
                s3_path = self.source_url.replace(f'https://{current_app.config["S3_PUBLIC_URL"]}/', '')
                s3_files_to_delete.append(s3_path)
                purge_from_cache.append(self.source_url)
            elif self.source_url.startswith('http') and current_app.config['SERVER_NAME'] in self.source_url:
                try:
                    os.unlink(self.source_url.replace(f"{current_app.config['SERVER_URL']}/", 'app/'))
                except FileNotFoundError:
                    ...
                purge_from_cache.append(self.source_url)

        if len(s3_files_to_delete) > 0:
            from app.shared.tasks.maintenance import delete_from_s3
            if current_app.debug:
                delete_from_s3(s3_files_to_delete)
            else:
                delete_from_s3.delay(s3_files_to_delete)

        if purge_cdn and purge_from_cache:
            flush_cdn_cache(purge_from_cache)

    def filesize(self):
        size = 0
        if self.file_path and os.path.exists(self.file_path):
            size += os.path.getsize(self.file_path)
        if self.thumbnail_path and os.path.exists(self.thumbnail_path):
            size += os.path.getsize(self.thumbnail_path)
        return size


def flush_cdn_cache(url: Union[str, List[str]]):
    zone_id = current_app.config['CLOUDFLARE_ZONE_ID']
    token = current_app.config['CLOUDFLARE_API_TOKEN']
    if zone_id and token:
        if current_app.debug:
            flush_cdn_cache_task(url)
        else:
            flush_cdn_cache_task.delay(url)


@celery.task
def flush_cdn_cache_task(to_purge: Union[str, List[str]]):
    with current_app.app_context():
        zone_id = current_app.config['CLOUDFLARE_ZONE_ID']
        token = current_app.config['CLOUDFLARE_API_TOKEN']
        if zone_id and token:
            headers = {
                'Authorization': f"Bearer {token}",
                'Content-Type': 'application/json'
            }
            # url can be a string or a list of strings
            body = ''
            if isinstance(to_purge, str) and to_purge == 'all':
                body = {
                    'purge_everything': True
                }
            else:
                if isinstance(to_purge, str):
                    body = {
                        'files': [to_purge]
                    }
                elif isinstance(to_purge, list):
                    body = {
                        'files': to_purge
                    }

            if body:
                httpx_client.request(
                    'POST',
                    f'https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache',
                    headers=headers,
                    json=body,
                    timeout=5,
                )


class Topic(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    machine_name = db.Column(db.String(50), index=True)
    name = db.Column(db.String(50))
    num_communities = db.Column(db.Integer, default=0)
    parent_id = db.Column(db.Integer)
    show_posts_in_children = db.Column(db.Boolean, default=False)
    communities = db.relationship('Community', lazy='dynamic', backref='topic', cascade="all, delete-orphan")

    def path(self):
        return_value = [self.machine_name]
        parent_id = self.parent_id
        while parent_id is not None:
            parent_topic = Topic.query.get(parent_id)
            if parent_topic is None:
                break
            return_value.append(parent_topic.machine_name)
            parent_id = parent_topic.parent_id
        return_value = list(reversed(return_value))
        return '/'.join(return_value)

    def notify_new_posts(self, user_id: int) -> bool:
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == self.id,
                                                                      NotificationSubscription.user_id == user_id,
                                                                      NotificationSubscription.type == NOTIF_TOPIC).first()
        return existing_notification is not None


class Community(db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    icon_id = db.Column(db.Integer, db.ForeignKey('file.id'))
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    name = db.Column(db.String(256), index=True)
    title = db.Column(db.String(256))
    description = db.Column(db.Text)  # markdown
    description_html = db.Column(db.Text)  # html equivalent of above markdown
    rules = db.Column(db.Text)  # this is unused but do not remove, it breaks everything
    content_warning = db.Column(db.Text)  # "Are you sure you want to view this community?"
    subscriptions_count = db.Column(db.Integer, default=0)  # Local subscribers
    total_subscriptions_count = db.Column(db.Integer, default=0)  # Local AND remote
    post_count = db.Column(db.Integer, default=0)
    post_reply_count = db.Column(db.Integer, default=0)
    nsfw = db.Column(db.Boolean, default=False)
    nsfl = db.Column(db.Boolean, default=False)
    ai_generated = db.Column(db.Boolean, default=False, index=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    low_quality = db.Column(db.Boolean, default=False)  # upvotes earned in low quality communities don't improve reputation
    created_at = db.Column(db.DateTime, default=utcnow)
    last_active = db.Column(db.DateTime, default=utcnow)
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    content_retention = db.Column(db.Integer, default=-1)
    topic_id = db.Column(db.Integer, db.ForeignKey('topic.id'), index=True)
    default_layout = db.Column(db.String(15))
    default_post_type = db.Column(db.String(15))
    posting_warning = db.Column(db.String(512))
    downvote_accept_mode = db.Column(db.Integer, default=0)  # -1 = None, 0 = All, 2 = Community members, 4 = This instance, 6 = Trusted instances
    rss_url = db.Column(db.String(2048))
    can_be_archived = db.Column(db.Boolean, default=True, index=True)
    always_translate = db.Column(db.Boolean)
    post_url_type = db.Column(db.String(15))
    question_answer = db.Column(db.Boolean, default=False)     # if this is a stackoverflow-style question and answer community
    first_federated_at = db.Column(db.DateTime, index=True, default=utcnow)

    ap_id = db.Column(db.String(255), index=True)
    ap_profile_id = db.Column(db.String(255), index=True, unique=True)
    ap_followers_url = db.Column(db.String(255))
    ap_preferred_username = db.Column(db.String(255))
    ap_discoverable = db.Column(db.Boolean, default=False)
    ap_public_url = db.Column(db.String(255))
    ap_fetched_at = db.Column(db.DateTime)
    ap_deleted_at = db.Column(db.DateTime)
    ap_inbox_url = db.Column(db.String(255))
    ap_outbox_url = db.Column(db.String(255))
    ap_featured_url = db.Column(db.String(255))
    ap_moderators_url = db.Column(db.String(255))
    ap_domain = db.Column(db.String(255))

    banned = db.Column(db.Boolean, default=False)
    restricted_to_mods = db.Column(db.Boolean, default=False)
    local_only = db.Column(db.Boolean, default=False)  # only users on this instance can post. no federation.
    private = db.Column(db.Boolean, default=False)     # only members can view. no federation.
    encrypted = db.Column(db.Boolean, default=False)
    invitations = db.Column(db.Integer, default=0)     # 0 = anyone can join, 1 = apply to join, 2 = must be invited by a member, 3 = must be invited by a mod, 4 = must be invited by owner
    new_mods_wanted = db.Column(db.Boolean, default=False)
    searchable = db.Column(db.Boolean, default=True)
    private_mods = db.Column(db.Boolean, default=False)

    active_daily = db.Column(db.Integer, default=0)
    active_weekly = db.Column(db.Integer, default=0)
    active_monthly = db.Column(db.Integer, default=0)
    active_6monthly = db.Column(db.Integer, default=0)

    # Which feeds posts from this community show up in
    show_popular = db.Column(db.Boolean, default=True)
    show_all = db.Column(db.Boolean, default=True)

    ignore_remote_language = db.Column(db.Boolean, default=False)
    ignore_remote_gen_ai = db.Column(db.Boolean, default=False)

    search_vector = db.Column(TSVectorType('name', 'title', 'description', 'rules', auto_index=False))

    posts = db.relationship('Post', lazy='dynamic', cascade="all, delete-orphan")
    replies = db.relationship('PostReply', lazy='dynamic', cascade="all, delete-orphan")
    wiki_pages = db.relationship('CommunityWikiPage', lazy='dynamic', backref='community', cascade="all, delete-orphan")
    icon = db.relationship('File', lazy='joined', foreign_keys=[icon_id], single_parent=True, backref='community',
                           cascade="all, delete-orphan")
    image = db.relationship('File', foreign_keys=[image_id], single_parent=True, cascade="all, delete-orphan")
    languages = db.relationship('Language', lazy='dynamic', secondary=community_language,
                                backref=db.backref('communities', lazy='dynamic'))
    flair = db.relationship('CommunityFlair', backref=db.backref('community'), cascade="all, delete-orphan")
    modlog = db.relationship('ModLog', lazy='dynamic', foreign_keys="ModLog.community_id", back_populates='community')

    __table_args__ = (
        db.Index(
            'idx_community_fts',
            'search_vector',
            postgresql_using='gin'
        ),
    )

    def language_ids(self):
        return [language.id for language in self.languages.all()]

    def language_names(self):
        return [language.name for language in self.languages.filter(Language.code != 'und').all()]

    def icon_image(self, size='default') -> str:
        if self.icon_id is not None:
            if size == 'default':
                if self.icon.file_path is not None:
                    if self.icon.file_path.startswith('app/'):
                        return self.icon.file_path.replace('app/', '/')
                    else:
                        return self.icon.file_path
                if self.icon.source_url is not None:
                    if self.icon.source_url.startswith('app/'):
                        return self.icon.source_url.replace('app/', '/')
                    else:
                        return self.icon.source_url
            elif size == 'tiny':
                if self.icon.thumbnail_path is not None:
                    if self.icon.thumbnail_path.startswith('app/'):
                        return self.icon.thumbnail_path.replace('app/', '/')
                    else:
                        return self.icon.thumbnail_path
                if self.icon.source_url is not None:
                    if self.icon.source_url.startswith('app/'):
                        return self.icon.source_url.replace('app/', '/')
                    else:
                        return self.icon.source_url
        return '/static/images/1px.gif'

    @cache.memoize(timeout=500)
    def header_image(self) -> str:
        if self.image_id is not None:
            if self.image.file_path is not None:
                if self.image.file_path.startswith('app/'):
                    return self.image.file_path.replace('app/', '/')
                else:
                    return self.image.file_path
            if self.image.source_url is not None:
                if self.image.source_url.startswith('app/'):
                    return self.image.source_url.replace('app/', '/')
                else:
                    return self.image.source_url
        return ''

    def display_name(self) -> str:
        if self.ap_id is None:
            return self.title
        else:
            return f"{self.title}@{self.ap_domain}"

    def link(self) -> str:
        if self.ap_id is None:
            return self.name
        else:
            return self.ap_id.lower()

    def lemmy_link(self) -> str:
        if self.ap_id is None:
            return f"!{self.name}@{current_app.config['SERVER_NAME']}"
        else:
            return f"!{self.ap_id.lower()}"

    @cache.memoize(timeout=300)
    def moderators(self):
        return CommunityMember.query.filter((CommunityMember.community_id == self.id) &
                                            (or_(
                                                CommunityMember.is_owner,
                                                CommunityMember.is_moderator
                                            ))
                                            ).filter(CommunityMember.is_banned == False).all()

    def is_member(self, user):
        if user.is_anonymous:
            return False
        if user is None:
            return CommunityMember.query.filter(CommunityMember.user_id == current_user.get_id(),
                                                CommunityMember.community_id == self.id,
                                                CommunityMember.is_banned == False).all()
        else:
            return CommunityMember.query.filter(CommunityMember.user_id == user.id,
                                                CommunityMember.community_id == self.id,
                                                CommunityMember.is_banned == False).all()

    def is_moderator(self, user=None):
        if user is None:
            return any(moderator.user_id == current_user.get_id() for moderator in self.moderators())
        else:
            return any(moderator.user_id == user.id for moderator in self.moderators())

    def is_owner(self, user=None):
        if user is None:
            return any(moderator.user_id == current_user.get_id() and moderator.is_owner for moderator in
                       self.moderators())
        else:
            return any(moderator.user_id == user.id and moderator.is_owner for moderator in self.moderators())

    def can_invite(self, user=None):
        if user is None and current_user.is_anonymous:
            return False
        else:
            u = current_user if user is None else user
            if self.invitations == INVITE_MEMBERS_ONLY and not self.is_member(u):
                return False
            elif self.invitations == INVITE_MODS_ONLY and not self.is_moderator(u):
                return False
            elif self.invitations == INVITE_OWNER_ONLY and not self.is_owner(u):
                return False
            return True

    def num_owners(self):
        result = 0
        for moderator in self.moderators():
            if moderator.is_owner:
                result += 1
        return result

    def is_instance_admin(self, user):
        if self.instance_id:
            instance_role = InstanceRole.query.filter(InstanceRole.instance_id == self.instance_id,
                                                      InstanceRole.user_id == user.id,
                                                      InstanceRole.role == 'admin').first()
            return instance_role is not None
        else:
            return False
    
    def is_admin_or_staff(self, user):
        return user.is_admin_or_staff()

    def user_is_banned(self, user):
        # use communities_banned_from() instead of this method, where possible. Redis caches the result of communities_banned_from()
        # we cannot use communities_banned_from() in models.py because it causes a circular import
        community_bans = CommunityBan.query.filter(CommunityBan.user_id == user.id).all()
        return self.id in [cb.community_id for cb in community_bans]

    def profile_id(self):
        retval = self.ap_profile_id if self.ap_profile_id else f"{current_app.config['SERVER_URL']}/c/{self.name}"
        return retval.lower()

    def public_url(self):
        result = self.ap_public_url if self.ap_public_url else f"{current_app.config['SERVER_URL']}/c/{self.name}"
        return result

    def is_local(self):
        return self.ap_id is None or self.profile_id().startswith(f"{current_app.config['SERVER_URL']}")

    def local_url(self):
        if self.is_local():
            return self.ap_profile_id
        else:
            return f"{current_app.config['SERVER_URL']}/c/{self.ap_id}"

    def humanize_subscribers(self, total=True, **kwargs):
        """Return an abbreviated, human readable number of followers (e.g. 1.2k instead of 1215)"""

        if "value" in kwargs:
            subscribers = kwargs.get("value")
        else:
            if total:
                subscribers = self.total_subscriptions_count if self.total_subscriptions_count else self.subscriptions_count
            else:
                subscribers = self.subscriptions_count
        
        if not subscribers:
            return "0"

        if subscribers < 1000:
            return str(subscribers)
        else:
            return str(int(subscribers / 100) / 10) + "k"
    
    def notify_new_posts(self, user_id: int) -> bool:
        existing_notification = db.session.query(NotificationSubscription).\
            filter(NotificationSubscription.entity_id == self.id,
                   NotificationSubscription.user_id == user_id,
                   NotificationSubscription.type == NOTIF_COMMUNITY).first()
        return existing_notification is not None

    # ids of all the users who want to be notified when there is a post in this community
    def notification_subscribers(self):
        return list(db.session.execute(
            text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :community_id AND type = :type '),
            {'community_id': self.id, 'type': NOTIF_COMMUNITY}).scalars())


    # instances that have users which are members of this community. (excluding the current instance)
    def following_instances(self, include_dormant=False, mod_hosts_only=False) -> List[Instance]:
        instances = db.session.query(Instance).join(User, User.instance_id == Instance.id).join(CommunityMember,
                                                                                                CommunityMember.user_id == User.id)
        instances = instances.filter(CommunityMember.community_id == self.id, CommunityMember.is_banned == False)
        if mod_hosts_only:
            instances = instances.filter(CommunityMember.is_moderator == True)
        if not include_dormant:
            instances = instances.filter(Instance.dormant == False)
        instances = instances.filter(Instance.id != 1, Instance.gone_forever == False)
        return instances.all()


    def has_followers_from_domain(self, domain: str) -> bool:
        instances = db.session.query(Instance).join(User, User.instance_id == Instance.id).join(CommunityMember,
                                                                                    CommunityMember.user_id == User.id)
        instances = instances.filter(CommunityMember.community_id == self.id, CommunityMember.is_banned == False)
        for instance in instances:
            if instance.domain == domain:
                return True
        return False

    def loop_videos(self) -> bool:
        return 'gifs' in self.name

    def scale_by(self) -> int:
        if self.subscriptions_count <= 1:
            return 3
        largest_community = _large_community_subscribers()
        if largest_community is None or largest_community == 0:
            return 0
        influence = self.subscriptions_count / int(largest_community)
        if influence < 0.05:
            return 4
        if influence < 0.25:
            return 3
        elif influence < 0.60:
            return 2
        elif influence < 1.0:
            return 1
        else:
            return 0

    def flair_for_ap(self, version=1):
        result = []
        
        if version == 1:
            for flair in self.flair:
                result.append({'type': 'lemmy:CommunityTag',
                            'id': f'{current_app.config["SERVER_URL"]}/c/{self.link()}/tag/{flair.id}',
                            'display_name': flair.flair,
                            'text_color': flair.text_color,
                            'background_color': flair.background_color,
                            'blur_images': flair.blur_images
                            })
        elif version == 2:
            for flair in self.flair:
                result.append({
                    "type": "CommunityPostTag",
                    "id": flair.get_ap_id(),
                    "preferredUsername": flair.flair,
                    "textColor": flair.text_color,
                    "backgroundColor": flair.background_color,
                    "blurImages": flair.blur_images,
                })
        
        return result

    def delete_dependencies(self):
        from app import redis_client
        for post in db.session.query(Post).filter_by(community_id=self.id):
            with redis_client.lock(f"lock:post:{post.id}", timeout=10, blocking_timeout=6):
                post.delete_dependencies()
                db.session.delete(post)
                db.session.commit()
        db.session.query(FeedItem).filter(FeedItem.community_id == self.id).delete()
        db.session.query(CommunityBan).filter(CommunityBan.community_id == self.id).delete()
        db.session.query(CommunityBlock).filter(CommunityBlock.community_id == self.id).delete()
        db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.community_id == self.id).delete()
        db.session.query(CommunityMember).filter(CommunityMember.community_id == self.id).delete()
        db.session.query(Report).filter(Report.suspect_community_id == self.id).delete()
        db.session.query(UserFlair).filter(UserFlair.community_id == self.id).delete()
        db.session.query(ModLog).filter(ModLog.community_id == self.id).update({ModLog.community_id: None})
        db.session.query(ActivityBatch).filter(ActivityBatch.community_id == self.id).delete()
        db.session.commit()


user_role = db.Table('user_role',
                     db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
                     db.Column('role_id', db.Integer, db.ForeignKey('role.id')),
                     db.PrimaryKeyConstraint('user_id', 'role_id')
                     )


user_file = db.Table('user_file',
                     db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
                     db.Column('file_id', db.Integer, db.ForeignKey('file.id')),
                     db.Column('size', db.Integer),
                     db.PrimaryKeyConstraint('user_id', 'file_id')
                     )

# table to hold users' read post ids
read_posts = db.Table('read_posts',
                      db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True, nullable=False),
                      db.Column('read_post_id', db.Integer, db.ForeignKey('post.id'), primary_key=True, nullable=False),
                      db.Column('interacted_at', db.DateTime, index=True, default=utcnow),
                      db.Index('ix_read_posts_user_post', 'user_id', 'read_post_id')
                      )


# table to hold users' hidden post ids
hidden_posts = db.Table('hidden_posts',
                      db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True, nullable=False),
                      db.Column('hidden_post_id', db.Integer, db.ForeignKey('post.id'), primary_key=True, nullable=False),
                      db.Column('interacted_at', db.DateTime, index=True, default=utcnow),
                      db.Index('ix_hidden_posts_user_post', 'user_id', 'hidden_post_id')
                      )


class Passkey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    passkey_id = db.Column(db.String(256))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    public_key = db.Column(db.LargeBinary)  # Store the raw binary public key
    created = db.Column(db.DateTime, default=utcnow)
    used = db.Column(db.DateTime, default=utcnow)
    device = db.Column(db.String(50))
    counter = db.Column(db.Integer, default=0)

    def __repr__(self):
        return f"<Passkey {self.id} {self.device}>"


class User(UserMixin, db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_name = db.Column(db.String(255), index=True)
    alt_user_name = db.Column(db.String(255), index=True)
    title = db.Column(db.String(256))
    email = db.Column(db.String(255), index=True)
    password_hash = db.Column(db.String(165))
    verified = db.Column(db.Boolean, default=False)
    verification_token = db.Column(db.String(16), index=True)
    banned = db.Column(db.Boolean, default=False, index=True)
    banned_until = db.Column(db.DateTime)  # null == permanent ban
    ban_posts = db.Column(db.Boolean, default=False)
    ban_comments = db.Column(db.Boolean, default=False)
    deleted = db.Column(db.Boolean, default=False)
    deleted_by = db.Column(db.Integer, index=True)
    about = db.Column(db.Text)  # markdown
    about_html = db.Column(db.Text)  # html
    keywords = db.Column(db.String(256))
    matrix_user_id = db.Column(db.String(256))
    hide_nsfw = db.Column(db.Integer, default=1)
    hide_nsfl = db.Column(db.Integer, default=1)
    hide_gen_ai = db.Column(db.Integer, default=2)      # 0 = show, 1 = hide, 2 = label, 3 = semi-transparent
    created = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime, default=utcnow, index=True)
    avatar_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    cover_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    newsletter = db.Column(db.Boolean, default=True)
    email_unread = db.Column(db.Boolean, default=True)  # True if they want to receive 'unread notifications' emails
    email_unread_sent = db.Column(db.Boolean)  # True after a 'unread notifications' email has been sent. None for remote users
    receive_message_mode = db.Column(db.String(20), default='Closed')  # possible values: Open, TrustedOnly, Closed
    bounces = db.Column(db.SmallInteger, default=0)
    timezone = db.Column(db.String(30))
    reputation = db.Column(db.Float, default=0.0)
    attitude = db.Column(db.Float, default=None)  # (upvotes cast - downvotes cast) / (upvotes + downvotes). A number between 1 and -1 is the ratio between up and down votes they cast
    post_count = db.Column(db.Integer, default=0)
    post_reply_count = db.Column(db.Integer, default=0)
    stripe_customer_id = db.Column(db.String(50))
    stripe_subscription_id = db.Column(db.String(50))
    searchable = db.Column(db.Boolean, default=True)        # whether their profile is visible in profile list
    indexable = db.Column(db.Boolean, default=True)         # whether posts appear in search results
    bot = db.Column(db.Boolean, default=False, index=True)
    bot_override = db.Column(db.Boolean, default=False, index=True)
    suppress_crossposts = db.Column(db.Boolean, default=False, index=True)
    vote_privately = db.Column(db.Boolean, default=False)
    ignore_bots = db.Column(db.Integer, default=0)
    unread_notifications = db.Column(db.Integer, default=0)
    ip_address = db.Column(db.String(50))
    ip_address_country = db.Column(db.String(50))
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    reports = db.Column(db.Integer, default=0)  # how many times this user has been reported.
    default_sort = db.Column(db.String(25), default='hot')
    default_comment_sort = db.Column(db.String(10), default='hot')
    default_filter = db.Column(db.String(25), default='subscribed')
    theme = db.Column(db.String(20), default='')
    font = db.Column(db.String(25), default='')
    community_keyword_filter = db.Column(db.String(150))
    referrer = db.Column(db.String(256))
    markdown_editor = db.Column(db.Boolean, default=True)
    interface_language = db.Column(db.String(10))  # a locale that the translation system understands e.g. 'en' or 'en-us'. If empty, use browser default
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'))  # the default choice in the language dropdown when composing posts & comments. NOT UI language
    read_language_ids = db.Column(MutableList.as_mutable(ARRAY(db.Integer)))
    reply_collapse_threshold = db.Column(db.Integer, default=-10)
    reply_hide_threshold = db.Column(db.Integer, default=-20)
    feed_auto_follow = db.Column(db.Boolean, default=True)  # does the user want to auto-follow feed communities
    feed_auto_leave = db.Column(db.Boolean, default=True)  # does the user want to auto-leave feed communities
    accept_private_messages = db.Column(db.Integer, default=3)  # None or 0 = do not accept, 1 = This instance, 2 = Trusted instances, 3 = All instances
    google_oauth_id = db.Column(db.String(64), unique=True, index=True)
    hide_low_quality = db.Column(db.Boolean, default=False)
    show_subscribed_communities = db.Column(db.Boolean, default=False)
    additional_css = db.Column(db.Text)
    mastodon_oauth_id = db.Column(db.String(64), unique=True, index=True)
    discord_oauth_id = db.Column(db.String(64), unique=True, index=True)
    password_updated_at = db.Column(db.DateTime, default=utcnow)
    code_style = db.Column(db.String(25), default='fruity')

    avatar = db.relationship('File', lazy='joined', foreign_keys=[avatar_id], single_parent=True, cascade="all, delete-orphan")
    cover = db.relationship('File', lazy='joined', foreign_keys=[cover_id], single_parent=True, cascade="all, delete-orphan")
    instance = db.relationship('Instance', lazy='joined', foreign_keys=[instance_id])
    conversations = db.relationship('Conversation', lazy='dynamic', secondary=conversation_member,
                                    backref=db.backref('members', lazy='joined'))
    user_notes = db.relationship('UserNote', lazy='dynamic', foreign_keys="UserNote.target_id")

    ap_id = db.Column(db.String(255), index=True)  # e.g. username@server
    ap_profile_id = db.Column(db.String(255), index=True, unique=True)  # e.g. https://server/u/username
    ap_public_url = db.Column(db.String(255))  # e.g. https://server/u/UserName
    ap_fetched_at = db.Column(db.DateTime)
    ap_followers_url = db.Column(db.String(255))
    ap_preferred_username = db.Column(db.String(255))
    ap_manually_approves_followers = db.Column(db.Boolean, default=False)
    ap_deleted_at = db.Column(db.DateTime)
    ap_inbox_url = db.Column(db.String(255))
    ap_domain = db.Column(db.String(255))

    search_vector = db.Column(TSVectorType('user_name', 'about', 'keywords', auto_index=False))
    activity = db.relationship('ActivityLog', backref='account', lazy='dynamic', cascade="all, delete-orphan")
    posts = db.relationship('Post', lazy='dynamic', cascade="all, delete-orphan")
    post_replies = db.relationship('PostReply', lazy='dynamic', cascade="all, delete-orphan")
    extra_fields = db.relationship('UserExtraField', lazy='dynamic', cascade="all, delete-orphan")
    roles = db.relationship('Role', secondary=user_role, lazy='dynamic')
    passkeys = db.relationship('Passkey', lazy='dynamic', cascade="all, delete-orphan")
    modlog_target = db.relationship('ModLog', lazy='dynamic', foreign_keys="ModLog.target_user_id", back_populates='target_user')
    modlog_actor = db.relationship('ModLog', lazy='dynamic', foreign_keys="ModLog.user_id", back_populates='author')

    hide_read_posts = db.Column(db.Boolean, default=False)
    # db relationship tracked by the "read_posts" table
    # this is the User side, so its referencing the Post side
    # read_by is the corresponding Post object variable
    read_post = db.relationship('Post', secondary=read_posts, back_populates='read_by', lazy='dynamic')
    hidden_post = db.relationship('Post', secondary=hidden_posts, back_populates='hidden_by', lazy='dynamic')

    def __repr__(self):
        return '<User {}_{}>'.format(self.user_name, self.id)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        try:
            result = check_password_hash(self.password_hash, password)
            return result
        except ValueError:
            # Caused when invalid hash method used, check bcrypt as a fallback
            result = app_bcrypt.check_password_hash(self.password_hash, password)

            # If pw validates, resave the hash using a more secure hashing algorithm
            if result:
                self.set_password(password)
                db.session.commit()

            return result
        except Exception:
            return False

    def get_id(self):
        if self.is_authenticated:
            return self.id
        else:
            return 0

    @classmethod
    def get_by_email(cls, email):
        return User.query.filter(User.email == email.strip()).first()

    def display_name(self):
        if self.deleted is False:
            if self.title:
                return self.title.strip()
            else:
                return self.user_name.strip()
        else:
            return '[deleted]'

    def avatar_thumbnail(self) -> str:
        if self.avatar_id is not None:
            if self.avatar.thumbnail_path is not None:
                if self.avatar.thumbnail_path.startswith('app/'):
                    return self.avatar.thumbnail_path.replace('app/', '/')
                else:
                    return self.avatar.thumbnail_path
            else:
                return self.avatar_image()
        return ''

    def avatar_image(self) -> str:
        if self.avatar_id is not None:
            if self.avatar.file_path is not None:
                if self.avatar.file_path.startswith('app/'):
                    return self.avatar.file_path.replace('app/', '/')
                else:
                    return self.avatar.file_path
            if self.avatar.source_url is not None:
                if self.avatar.source_url.startswith('app/'):
                    return self.avatar.source_url.replace('app/', '/')
                else:
                    return self.avatar.source_url
        return ''

    @cache.memoize(timeout=500)
    def cover_image(self) -> str:
        if self.cover_id is not None:
            if self.cover.thumbnail_path is not None:
                if self.cover.thumbnail_path.startswith('app/'):
                    return self.cover.thumbnail_path.replace('app/', '/')
                else:
                    return self.cover.thumbnail_path
            if self.cover.source_url is not None:
                if self.cover.source_url.startswith('app/'):
                    return self.cover.source_url.replace('app/', '/')
                else:
                    return self.cover.source_url
        return ''

    def filesize(self):
        size = 0
        if self.avatar_id:
            size += self.avatar.filesize()
        if self.cover_id:
            size += self.cover.filesize()
        return size

    def community_flair(self, community_id: int):
        user_flair = UserFlair.query.filter(UserFlair.community_id == community_id,
                                            UserFlair.user_id == self.id).first()
        return user_flair.flair.strip() if user_flair else ''

    def num_content(self):
        content = 0
        content += db.session.execute(text('SELECT COUNT(*) as c FROM "post" WHERE user_id = :user_id'),
                                      {'user_id': self.id}).scalar()
        content += db.session.execute(text('SELECT COUNT(*) as c FROM "post_reply" WHERE user_id = :user_id'),
                                      {'user_id': self.id}).scalar()
        return content

    def is_local(self):
        return self.ap_id is None or self.ap_profile_id.startswith(current_app.config['SERVER_URL'])

    def waiting_for_approval(self):
        application = UserRegistration.query.filter_by(user_id=self.id, status=0).first()
        return application is not None

    @cache.memoize(timeout=30)
    def is_admin(self):
        if self.id == 1:
            return True
        for role in self.roles:
            if role.name == 'Admin':
                return True
        return False

    @cache.memoize(timeout=30)
    def is_staff(self):
        for role in self.roles:
            if role.name == 'Staff':
                return True
        return False

    def is_admin_or_staff(self):
        return self.is_admin() or self.is_staff()

    def is_instance_admin(self):
        if self.instance_id:
            instance_role = InstanceRole.query.filter(InstanceRole.instance_id == self.instance_id,
                                                      InstanceRole.user_id == self.id,
                                                      InstanceRole.role == 'admin').first()
            return instance_role is not None
        else:
            return False

    def trustworthy(self):
        if self.is_admin():
            return True
        if self.created_recently() and self.reputation < 100:
            return False
        return True

    def cannot_vote(self):
        if self.is_local():
            return False
        return self.post_count == 0 and self.post_reply_count == 0 and len(
            self.user_name) == 8  # most vote manipulation bots have 8 character user names and never post any content

    def link(self) -> str:
        if self.is_local():
            return self.user_name
        else:
            return self.ap_id

    def lemmy_link(self) -> str:
        if self.ap_id is None:
            return f"{self.user_name}@{current_app.config['SERVER_NAME']}"
        else:
            return self.ap_id.lower()

    def followers_url(self):
        if self.ap_followers_url:
            return self.ap_followers_url
        else:
            return self.public_url() + '/followers'

    def instance_domain(self):
        if self.ap_domain:
            return self.ap_domain
        if self.is_local():
            return current_app.config['SERVER_NAME']
        else:
            return self.instance.domain

    def email_domain(self):
        email_parts = self.email.split('@')
        return email_parts[1]

    def get_reset_password_token(self, expires_in=600):
        return jwt.encode(
            {'reset_password': self.id, 'exp': time() + expires_in},
            current_app.config['SECRET_KEY'],
            algorithm='HS256')

    def another_account_using_email(self, email):
        another_account = User.query.filter(User.email == email, User.id != self.id).first()
        return another_account is not None

    def expires_soon(self):
        if self.expires is None:
            return False
        return self.expires < utcnow() + timedelta(weeks=1)

    def is_expired(self):
        if self.expires is None:
            return True
        return self.expires < utcnow()

    def expired_ages_ago(self):
        if self.expires is None:
            return True
        return self.expires < datetime(2019, 9, 1)

    def recalculate_attitude(self):
        # Use direct SQL queries to avoid potential ORM-related deadlocks
        # Count post upvotes and downvotes
        post_votes_result = db.session.execute(text("""
            SELECT 
                COUNT(CASE WHEN effect > 0 THEN 1 END) AS upvotes,
                COUNT(CASE WHEN effect < 0 THEN 1 END) AS downvotes
            FROM (
                SELECT effect
                FROM post_vote
                WHERE user_id = :user_id
                ORDER BY id DESC
                LIMIT 50
            ) AS recent_votes
        """), {"user_id": self.id}).fetchone()

        upvotes = post_votes_result[0] or 0
        downvotes = post_votes_result[1] or 0

        # Count comment upvotes and downvotes
        comment_votes_result = db.session.execute(text("""
            SELECT 
                COUNT(CASE WHEN effect > 0 THEN 1 END) AS upvotes,
                COUNT(CASE WHEN effect < 0 THEN 1 END) AS downvotes
            FROM (
                SELECT effect
                FROM post_reply_vote
                WHERE user_id = :user_id
                ORDER BY id DESC
                LIMIT 50
            ) AS recent_votes
        """), {"user_id": self.id}).fetchone()

        comment_upvotes = comment_votes_result[0] or 0
        comment_downvotes = comment_votes_result[1] or 0

        total_upvotes = upvotes + comment_upvotes
        total_downvotes = downvotes + comment_downvotes

        # Calculate the new attitude value
        if total_upvotes + total_downvotes > 9:  # Only calculate attitude if they've done 10 or more votes
            new_attitude = (total_upvotes - total_downvotes) / (total_upvotes + total_downvotes)
        else:
            new_attitude = None

        # Update attitude
        db.session.execute(text("""
            UPDATE "user"
            SET attitude = :attitude
            WHERE id = :user_id
        """), {"attitude": new_attitude, "user_id": self.id})
        # Note: Caller is responsible for committing

    def get_num_upvotes(self):
        post_votes = db.session.execute(
            text('SELECT COUNT(*) FROM "post_vote" WHERE user_id = :user_id AND effect > 0'),
            {'user_id': self.id}).scalar()
        post_reply_votes = db.session.execute(
            text('SELECT COUNT(*) FROM "post_reply_vote" WHERE user_id = :user_id AND effect > 0'),
            {'user_id': self.id}).scalar()
        return post_votes + post_reply_votes

    def get_num_downvotes(self):
        post_votes = db.session.execute(
            text('SELECT COUNT(*) FROM "post_vote" WHERE user_id = :user_id AND effect < 0'),
            {'user_id': self.id}).scalar()
        post_reply_votes = db.session.execute(
            text('SELECT COUNT(*) FROM "post_reply_vote" WHERE user_id = :user_id AND effect < 0'),
            {'user_id': self.id}).scalar()
        return post_votes + post_reply_votes

    def recalculate_post_stats(self, posts=True, replies=True):
        if posts:
            self.post_count = db.session.execute(
                text('SELECT COUNT(*) as c FROM "post" WHERE user_id = :user_id AND deleted = false'),
                {'user_id': self.id}).scalar()
        if replies:
            self.post_reply_count = db.session.execute(
                text('SELECT COUNT(*) as c FROM "post_reply" WHERE user_id = :user_id AND deleted = false'),
                {'user_id': self.id}).scalar()

    def subscribed(self, community_id: int) -> int:
        if community_id is None:
            return False
        subscription: CommunityMember = CommunityMember.query.filter_by(user_id=self.id,
                                                                        community_id=community_id).first()
        if subscription:
            if subscription.is_banned:
                return SUBSCRIPTION_BANNED
            elif subscription.is_owner:
                return SUBSCRIPTION_OWNER
            elif subscription.is_moderator:
                return SUBSCRIPTION_MODERATOR
            else:
                return SUBSCRIPTION_MEMBER
        else:
            join_request = CommunityJoinRequest.query.filter_by(user_id=self.id, community_id=community_id).first()
            if join_request:
                return SUBSCRIPTION_PENDING
            else:
                return SUBSCRIPTION_NONMEMBER

    def communities(self) -> List[Community]:
        return Community.query.filter(Community.banned == False). \
            join(CommunityMember).filter(CommunityMember.is_banned == False, CommunityMember.user_id == self.id).all()

    def profile_id(self):
        result = self.ap_profile_id if self.ap_profile_id else f"{current_app.config['SERVER_URL']}/u/{self.user_name.lower()}"
        return result

    def public_url(self):
        return self.ap_public_url if self.ap_public_url else f"{current_app.config['SERVER_URL']}/u/{self.user_name}"

    def created_recently(self):
        return self.created and self.created > utcnow() - timedelta(days=7)

    def created_very_recently(self):
        return self.created and self.created > utcnow() - timedelta(days=1)

    def has_blocked_instance(self, instance_id: int):
        instance_block = db.session.query(InstanceBlock).filter_by(user_id=self.id, instance_id=instance_id).first()
        return instance_block is not None

    def has_blocked_user(self, user_id: int):
        existing_block = db.session.query(UserBlock).filter_by(blocker_id=self.id, blocked_id=user_id).first()
        return existing_block is not None

    @staticmethod
    def verify_reset_password_token(token):
        try:
            id = jwt.decode(token, current_app.config['SECRET_KEY'],
                            algorithms=['HS256'])['reset_password']
        except:
            return
        return User.query.get(id)

    def delete_dependencies(self):
        if self.cover_id:
            file = db.session.query(File).get(self.cover_id)
            file.delete_from_disk()
            self.cover_id = None
            db.session.delete(file)
        if self.avatar_id:
            file = db.session.query(File).get(self.avatar_id)
            file.delete_from_disk()
            self.avatar_id = None
            db.session.delete(file)
        if self.waiting_for_approval():
            db.session.query(UserRegistration).filter(UserRegistration.user_id == self.id).delete()
        db.session.execute(text('DELETE FROM "user_role" WHERE user_id = :user_id'), {'user_id': self.id})
        db.session.execute(text('DELETE FROM "hidden_posts" WHERE user_id = :user_id'), {'user_id': self.id})
        db.session.execute(text('DELETE FROM "read_posts" WHERE user_id = :user_id'), {'user_id': self.id})
        db.session.query(NotificationSubscription).filter(NotificationSubscription.user_id == self.id).delete()
        db.session.query(Filter).filter(Filter.user_id == self.id).delete()
        db.session.query(UserFlair).filter(UserFlair.user_id == self.id).delete()
        db.session.query(UserFollower).filter(or_(UserFollower.local_user_id == self.id, UserFollower.remote_user_id == self.id)).delete()
        db.session.query(UserFollowRequest).filter(UserFollowRequest.user_id == self.id).delete()
        db.session.query(CommunityMember).filter(CommunityMember.user_id == self.id).delete()
        db.session.query(CommunityBlock).filter(CommunityBlock.user_id == self.id).delete()
        db.session.query(CommunityBan).filter(CommunityBan.user_id == self.id).delete()
        db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.user_id == self.id).delete()
        db.session.query(ChatMessage).filter(or_(ChatMessage.sender_id == self.id, ChatMessage.recipient_id == self.id)).delete()
        db.session.query(UserBlock).filter(or_(UserBlock.blocker_id == self.id, UserBlock.blocked_id == self.id)).delete()
        db.session.query(Notification).filter(Notification.user_id == self.id).delete()
        db.session.query(PollChoiceVote).filter(PollChoiceVote.user_id == self.id).delete()
        db.session.query(PostBookmark).filter(PostBookmark.user_id == self.id).delete()
        db.session.query(PostReplyBookmark).filter(PostReplyBookmark.user_id == self.id).delete()
        db.session.query(Reminder).filter(Reminder.user_id == self.id).delete()
        db.session.query(ModLog).filter(ModLog.user_id == self.id).update({ModLog.user_id: None})
        db.session.query(ModLog).filter(ModLog.target_user_id == self.id).update({ModLog.target_user_id: None})
        db.session.query(CommunityWikiPageRevision).filter(CommunityWikiPageRevision.user_id == self.id).update({CommunityWikiPageRevision.user_id: None})
        db.session.query(UserNote).filter(or_(UserNote.user_id == self.id, UserNote.target_id == self.id)).delete()

    def purge_content(self, soft=True, flush=True):
        files = File.query.join(Post).filter(Post.user_id == self.id).all()
        for file in files:
            file.delete_from_disk(purge_cdn=flush)
        db.session.commit()
        self.delete_dependencies()
        db.session.commit()
        posts = Post.query.filter_by(user_id=self.id).all()
        for post in posts:
            post.delete_dependencies()
            if soft:
                post.deleted = True
            else:
                db.session.delete(post)
            db.session.commit()

        post_replies = PostReply.query.filter_by(user_id=self.id).all()
        for reply in post_replies:
            reply.delete_dependencies()
            if soft:
                reply.deleted = True
            else:
                db.session.delete(reply)
            db.session.commit()

        files = File.query.join(user_file).filter(user_file.c.user_id == self.id).all()
        for file in files:
            file.delete_from_disk(purge_cdn=flush)
            db.session.execute(text('DELETE FROM "user_file" WHERE file_id = :file_id'), {'file_id': file.id})
            db.session.delete(file)
            db.session.commit()

    def mention_tag(self):
        if self.ap_domain is None:
            return '@' + self.user_name + '@' + current_app.config['SERVER_NAME']
        else:
            return '@' + self.user_name + '@' + self.ap_domain

    # True if user_id wants to be notified about posts by self
    def notify_new_posts(self, user_id):
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == self.id,
                                                                      NotificationSubscription.user_id == user_id,
                                                                      NotificationSubscription.type == NOTIF_USER).first()
        return existing_notification is not None

    # ids of all the users who want to be notified when self makes a post
    def notification_subscribers(self):
        return list(db.session.execute(
            text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :user_id AND type = :type '),
            {'user_id': self.id, 'type': NOTIF_USER}).scalars())

    def encode_jwt_token(self):
        payload = {'sub': str(self.id), 'iss': current_app.config['SERVER_NAME'], 'iat': int(time())}
        return jwt.encode(payload, current_app.config['SECRET_KEY'], algorithm='HS256')

    # mark a post as 'read' for this user
    def mark_post_as_read(self, post):
        # check if its already marked as read, if not, mark it as read
        if not self.has_read_post(post):
            self.read_post.append(post)

    # check if post has been read by this user
    # returns true if the post has been read, false if not
    def has_read_post(self, post):
        return self.read_post.filter(read_posts.c.read_post_id == post.id).count() > 0


    # mark a post as 'hidden' for this user
    def mark_post_as_hidden(self, post):
        # check if its already marked as hidden, if not, mark it as hidden
        if not self.has_hidden_post(post):
            self.hidden_post.append(post)

    # check if post has been hidden by this user
    # returns true if the post has been hidden, false if not
    def has_hidden_post(self, post):
        return self.hidden_post.filter(hidden_posts.c.hidden_post_id == post.id).count() > 0

    def get_note(self, by_user):
        user_note = self.user_notes.filter(UserNote.target_id == self.id, UserNote.user_id == by_user.id).first()
        if user_note:
            return user_note.body
        else:
            return ''


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    activity_type = db.Column(db.String(64))
    activity = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, index=True, default=utcnow)


class Post(db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), index=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    licence_id = db.Column(db.Integer, db.ForeignKey('licence.id'), index=True)
    status = db.Column(db.Integer, index=True, default=1)  # see POST_STATUS_* in constants.py
    slug = db.Column(db.String(255))
    title = db.Column(db.String(255))
    url = db.Column(db.String(2048))
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    type = db.Column(db.Integer, default=constants.POST_TYPE_ARTICLE)
    microblog = db.Column(db.Boolean, default=False)
    comments_enabled = db.Column(db.Boolean, default=True)
    deleted = db.Column(db.Boolean, default=False, index=True)
    deleted_by = db.Column(db.Integer, index=True)
    mea_culpa = db.Column(db.Boolean, default=False)
    has_embed = db.Column(db.Boolean, default=False)
    reply_count = db.Column(db.Integer, default=0, index=True)
    reply_count_cross_posted = db.Column(db.Integer, default=0)
    score = db.Column(db.Integer, default=0, index=True)  # used for 'top' ranking
    nsfw = db.Column(db.Boolean, default=False, index=True)
    nsfl = db.Column(db.Boolean, default=False, index=True)
    sticky = db.Column(db.Boolean, default=False, index=True)
    instance_sticky = db.Column(db.Boolean, default=False, index=True)
    ai_generated = db.Column(db.Boolean, default=False, index=True)
    notify_author = db.Column(db.Boolean, default=True)
    indexable = db.Column(db.Boolean, default=True, index=True)
    from_bot = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, index=True, default=utcnow)  # this is when the content arrived here
    posted_at = db.Column(db.DateTime, index=True, default=utcnow)  # this is when the original server created it
    last_active = db.Column(db.DateTime, index=True, default=utcnow)
    ip = db.Column(db.String(50))
    up_votes = db.Column(db.Integer, default=0)
    down_votes = db.Column(db.Integer, default=0)
    ranking = db.Column(db.Integer, default=0, index=True)  # used for 'hot' ranking
    ranking_scaled = db.Column(db.Integer, default=0, index=True)  # used for 'scaled' ranking
    edited_at = db.Column(db.DateTime)
    reports = db.Column(db.Integer, default=0)  # how many times this post has been reported. Set to -1 to ignore reports
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'), index=True)
    cross_posts = db.Column(MutableList.as_mutable(ARRAY(db.Integer)))
    scheduled_for = db.Column(db.DateTime, index=True)  # The first (or only) occurrence of this post
    repeat = db.Column(db.String(20), default='')  # 'daily', 'weekly', 'monthly'. Empty string = no repeat, just post once.
    stop_repeating = db.Column(db.DateTime, index=True)  # No more repeats after this datetime
    emoji_reactions = db.Column(db.JSON)            # a cache of the emoji reactions a post has received, to avoid joins
    tags = db.relationship('Tag', lazy='joined', secondary=post_tag, backref=db.backref('posts', lazy='dynamic'))
    timezone = db.Column(db.String(30))
    archived = db.Column(db.String(100))
    flair = db.relationship('CommunityFlair', lazy='joined', secondary=post_flair,
                            backref=db.backref('posts', lazy='dynamic'))

    ap_id = db.Column(db.String(255), index=True, unique=True)
    ap_create_id = db.Column(db.String(100))
    ap_announce_id = db.Column(db.String(100))
    ap_updated = db.Column(db.DateTime)  # When the remote instance edited the Post. Useful when local instance has been offline and a flurry of potentially out of order updates are coming in.

    search_vector = db.Column(TSVectorType('title', 'body', weights={"title": "A", "body": "B"}, auto_index=False))

    image = db.relationship(File, lazy='joined', foreign_keys=[image_id], cascade='all, delete')
    domain = db.relationship('Domain', lazy='joined', foreign_keys=[domain_id])
    author = db.relationship('User', lazy='joined', overlaps='posts', foreign_keys=[user_id])
    community = db.relationship('Community', lazy='joined', overlaps='posts', foreign_keys=[community_id])
    replies = db.relationship('PostReply', lazy='dynamic', backref='post', cascade='all, delete-orphan')
    language = db.relationship('Language', foreign_keys=[language_id], lazy='joined')
    licence = db.relationship('Licence', foreign_keys=[licence_id])
    modlog = db.relationship('ModLog', lazy='dynamic', foreign_keys="ModLog.post_id", back_populates='post')
    event = db.relationship('Event', uselist=False, backref='post', lazy='select', cascade='all, delete-orphan')
    gallery = db.relationship('File', secondary=post_file, lazy='dynamic')
    votes = db.relationship('PostVote', lazy='dynamic', backref='post', cascade='all, delete-orphan', passive_deletes=True)
    bookmarks = db.relationship('PostBookmark', backref='post', lazy='dynamic', cascade='all, delete-orphan')
    poll = db.relationship('Poll', uselist=False, backref='post', lazy='select', cascade='all, delete-orphan')

    # db relationship tracked by the "read_posts" table
    # this is the Post side, so its referencing the User side
    # read_post is the corresponding User object variable
    read_by = db.relationship('User', secondary=read_posts, back_populates='read_post', lazy='dynamic')
    hidden_by = db.relationship('User', secondary=hidden_posts, back_populates='hidden_post', lazy='dynamic')

    __table_args__ = (
        db.Index(
            'ix_post_user_id_not_deleted',
            'user_id',
            postgresql_where=db.text('deleted = false')
        ),
        db.Index(
            'ix_post_community_id_not_deleted',
            'community_id',
            postgresql_where=db.text('deleted = false')
        ),
        db.Index(
            'idx_post_fts',
            'search_vector',
            postgresql_using='gin'
        ),
        db.Index(
            'ix_post_community_posted_bot',
            'community_id', 'posted_at',
            postgresql_where=db.text('from_bot = false')
        ),
        db.Index(
            'ix_post_community_created_bot',
            'community_id', 'created_at',
            postgresql_where=db.text('from_bot = false')
        ),
    )

    def is_local(self):
        return self.ap_id is None or self.ap_id.startswith(current_app.config['SERVER_URL'])

    @classmethod
    def get_by_ap_id(cls, ap_id):
        return db.session.query(cls).filter_by(ap_id=ap_id).first()

    @classmethod
    def get_by_slug(cls, slug):
        return db.session.query(cls).filter_by(slug=slug).first()

    @classmethod
    def new(cls, user: User, community: Community, request_json: dict, announce_id=None):
        from app.activitypub.util import find_language_or_create, find_language, \
            find_hashtag_or_create, \
            find_licence_or_create, make_image_sizes, notify_about_post, find_flair_or_create
        from app.utils import allowlist_html, markdown_to_html, html_to_text, microblog_content_to_title, \
            blocked_phrases, get_setting, \
            is_image_url, is_video_url, domain_from_url, opengraph_parse, shorten_string, fixup_url, \
            is_video_hosting_site, communities_banned_from, recently_upvoted_posts, blocked_users

        microblog = False
        if 'name' not in request_json['object']:  # Microblog posts
            if 'content' in request_json['object'] and request_json['object']['content'] is not None:
                title = "[Microblog]"
                microblog = True
            else:
                return None
            if 'to' in request_json and len(request_json['to']) == 1:
                if request_json['to'][0].endswith('/followers'):  # Mastodon followers-only posts cannot be accepted because all posts are public in PieFed
                    return None
        else:
            title = request_json['object']['name'].strip()
        nsfl_in_title = '[NSFL]' in title.upper() or '(NSFL)' in title.upper() or '[COMBAT]' in title.upper()
        post = Post(user_id=user.id, community_id=community.id,
                    title=html.unescape(title),
                    comments_enabled=request_json['object']['commentsEnabled'] if 'commentsEnabled' in request_json['object'] else True,
                    sticky=request_json['object']['stickied'] if 'stickied' in request_json['object'] else False,
                    nsfw=request_json['object']['sensitive'] if 'sensitive' in request_json['object'] else False,
                    nsfl=request_json['object']['nsfl'] if 'nsfl' in request_json['object'] else nsfl_in_title,
                    ai_generated=request_json['object']['genAI'] if 'genAI' in request_json['object'] else False,
                    ap_id=request_json['object']['id'],
                    ap_create_id=request_json['id'],
                    ap_announce_id=announce_id,
                    up_votes=1,
                    from_bot=user.bot or user.bot_override,
                    score=1.0,
                    instance_id=user.instance_id,
                    indexable=user.indexable,
                    microblog=microblog,
                    posted_at=utcnow()
                    )
        if 'type' in request_json and request_json['type'] == 'Update':
            post.edited_at = utcnow()
        if community.nsfw:
            post.nsfw = True  # old Lemmy instances ( < 0.19.8 ) allow nsfw content in nsfw communities to be flagged as sfw which makes no sense
        if community.nsfl:
            post.nsfl = True
        if community.ai_generated:
            post.ai_generated = True
        if community.private:
            post.indexable = False
        if 'content' in request_json['object'] and request_json['object']['content'] is not None:
            # prefer Markdown in 'source' if provided
            if 'source' in request_json['object'] and isinstance(request_json['object']['source'], dict) and \
                    request_json['object']['source']['mediaType'] == 'text/markdown':
                post.body = request_json['object']['source']['content']
                post.body_html = markdown_to_html(post.body)
            elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/html':
                post.body_html = allowlist_html(request_json['object']['content'])
                post.body = html_to_text(post.body_html)
            elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/markdown':
                post.body = request_json['object']['content']
                post.body_html = markdown_to_html(post.body)
            else:
                if not (request_json['object']['content'].startswith('<p>') or request_json['object'][
                    'content'].startswith('<blockquote>')):
                    request_json['object']['content'] = '<p>' + request_json['object']['content'] + '</p>'
                post.body_html = allowlist_html(request_json['object']['content'])
                post.body = html_to_text(post.body_html)
            if microblog:
                autogenerated_title, link = microblog_content_to_title(post.body_html)
                if len(autogenerated_title) < 20:
                    title = '[Microblog] ' + autogenerated_title.strip()
                else:
                    title = autogenerated_title.strip()
                if '[NSFL]' in title.upper() or '(NSFL)' in title.upper() or '[COMBAT]' in title.upper():
                    post.nsfl = True
                if '[NSFW]' in title.upper() or '(NSFW)' in title.upper():
                    post.nsfw = True
                post.title = title
                if link != '':
                    post.url = link
        # Discard post if it contains certain phrases. Good for stopping spam floods.
        blocked_phrases_list = blocked_phrases()
        for blocked_phrase in blocked_phrases_list:
            if blocked_phrase in post.title:
                return None
        if post.body:
            for blocked_phrase in blocked_phrases_list:
                if blocked_phrase in post.body:
                    return None

        file_path = None
        alt_text = None
        if ('attachment' in request_json['object'] and
                isinstance(request_json['object']['attachment'], list) and
                len(request_json['object']['attachment']) > 0 and
                'type' in request_json['object']['attachment'][0]):
            for attachment in request_json['object']['attachment']:
                alt_text = None
                if attachment['type'] == 'Link':
                    if 'href' in attachment:
                        post.url = attachment['href']  # Lemmy < 0.19.4
                    elif 'url' in attachment:
                        post.url = attachment['url']  # NodeBB
                    if post.url:
                        break
                elif attachment['type'] == 'Document':
                    post.url = attachment['url']  # Mastodon
                    if 'name' in attachment:
                        alt_text = attachment['name']
                    if post.url:
                        break
                elif attachment['type'] == 'Audio':  # WordPress podcast
                    post.url = attachment['url']
                    if 'name' in attachment:
                        post.title = attachment['name']
                    if post.url:
                        break
            # Lastly, check for image posts. Mbin sends link posts with both image and link and we want to ignore the image in that case.
            if not post.url:
                for attachment in request_json['object']['attachment']:
                    if attachment['type'] == 'Image':
                        post.url = attachment['url']  # PixelFed, PieFed, Lemmy >= 0.19.4
                        alt_text = attachment.get("name")
                        file_path = attachment.get("file_path")

        if 'attachment' in request_json['object'] and isinstance(request_json['object']['attachment'],
                                                                 dict):  # a.gup.pe (Mastodon)
            alt_text = None
            post.url = request_json['object']['attachment']['url']

        if post.url:
            thumbnail_url, embed_url = fixup_url(post.url)
            post.url = embed_url
            if is_image_url(post.url):
                post.type = constants.POST_TYPE_IMAGE

                # PDQ hash of image
                image_hash = None
                if current_app.config['IMAGE_HASHING_ENDPOINT']:
                    from app.utils import retrieve_image_hash, hash_matches_blocked_image
                    image_hash = retrieve_image_hash(post.url)
                    if image_hash and hash_matches_blocked_image(image_hash):
                        return None

                image = File(source_url=post.url, hash=image_hash)
                if alt_text:
                    image.alt_text = alt_text
                if file_path:
                    image.file_path = file_path
                db.session.add(image)
                post.image = image
            elif is_video_url(post.url) or is_video_hosting_site(post.url):
                post.type = constants.POST_TYPE_VIDEO
            elif post.url.startswith('https://pixelfed.social') or post.url.startswith('pixelfed.uno'):
                post.type = constants.POST_TYPE_IMAGE
                opengraph = opengraph_parse(thumbnail_url)
                if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                    filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                    if not filename.startswith('/'):
                        file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                        post.image = file
                        db.session.add(file)
            elif post.url.startswith('https://loops.video'):
                post.type = constants.POST_TYPE_VIDEO
                opengraph = opengraph_parse(thumbnail_url)
                if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                    filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                    if not filename.startswith('/'):
                        filename = filename.replace('.jpg', '.720p.mp4')
                        file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                        post.image = file
                        db.session.add(file)
            else:
                post.type = constants.POST_TYPE_LINK
                # remove unnecessary "cross-posted from..." message that Lemmy inserts (only on link posts where we have a UI showing cross-posts)
                if post.body and ('cross-posted from: https://' in post.body or 'cross-posted from:  https://' in post.body):
                    lines = []
                    for line in post.body.split('\n'):
                        if not 'cross-posted from:  https://' in line.strip() and not 'cross-posted from: https://' in line.strip():
                            lines.append(line)
                    post.body = '\n'.join(lines)
                    post.body_html = markdown_to_html(post.body)
            if 'blogspot.com' in post.url:
                return None
            domain = domain_from_url(post.url)
            # notify about links to banned websites.
            already_notified = set()  # often admins and mods are the same people - avoid notifying them twice
            targets_data = {'gen': '0',
                            'post_id': post.id,
                            'orig_post_title': post.title,
                            'orig_post_body': post.body,
                            'orig_post_domain': post.domain,
                            }
            if domain.notify_mods:
                for community_member in post.community.moderators():
                    notify = Notification(title='Suspicious content', url=post.ap_id,
                                          user_id=community_member.user_id,
                                          author_id=user.id, notif_type=NOTIF_REPORT,
                                          subtype='post_from_suspicious_domain',
                                          targets=targets_data)
                    db.session.add(notify)
                    already_notified.add(community_member.user_id)
            if domain.notify_admins:
                targets_data = {'gen': '0', 'post_id': post.id}
                for admin in Site.admins():
                    if admin.id not in already_notified:
                        notify = Notification(title='Suspicious content',
                                              url=post.ap_id, user_id=admin.id,
                                              author_id=user.id, notif_type=NOTIF_REPORT,
                                              subtype='post_from_suspicious_domain',
                                              targets=targets_data)
                        db.session.add(notify)
            if domain.banned or domain.name.endswith('.pages.dev'):
                raise Exception(domain.name + ' is blocked by admin')
            else:
                domain.post_count += 1
                post.domain = domain

            if 'image' in request_json['object'] and post.image is None:
                image = File(source_url=request_json['object']['image']['url'])
                db.session.add(image)
                post.image = image
            if post.image is None:  # This is a link post but the source instance has not provided a thumbnail image
                # Let's see if we can do better than the source instance did!
                opengraph = opengraph_parse(thumbnail_url)
                if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                    filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                    if not filename.startswith('/'):
                        file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                        post.image = file
                        db.session.add(file)

        if post is not None:
            if request_json['object']['type'] == 'Video':
                post.type = constants.POST_TYPE_VIDEO
                post.url = request_json['object']['id']
                if 'icon' in request_json['object'] and isinstance(request_json['object']['icon'], list):
                    icon = File(source_url=request_json['object']['icon'][-1]['url'])
                    db.session.add(icon)
                    post.image = icon

            # Language. Lemmy uses 'language' while Mastodon has 'contentMap'
            if 'language' in request_json['object'] and isinstance(request_json['object']['language'], dict):
                language = find_language_or_create(request_json['object']['language']['identifier'],
                                                   request_json['object']['language']['name'])
                post.language = language
            elif 'contentMap' in request_json['object'] and isinstance(request_json['object']['contentMap'], dict):
                language = find_language(next(iter(request_json['object']['contentMap'])))
                post.language_id = language.id if language else None
            else:
                from app.utils import site_language_id
                post.language_id = site_language_id()
            if 'licence' in request_json['object'] and isinstance(request_json['object']['licence'], dict):
                licence = find_licence_or_create(request_json['object']['licence']['name'])
                post.licence = licence
            if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
                for json_tag in request_json['object']['tag']:
                    if json_tag and json_tag['type'] == 'Hashtag':
                        if json_tag['name'][1:].lower() != community.name.lower():  # Lemmy adds the community slug as a hashtag on every post in the community, which we want to ignore
                            hashtag = find_hashtag_or_create(json_tag['name'])
                            if hashtag:
                                post.tags.append(hashtag)
                    if json_tag and json_tag['type'] == 'lemmy:CommunityTag':
                        flair = find_flair_or_create(json_tag, post.community_id)
                        if flair:
                            post.flair.append(flair)
            if 'searchableBy' in request_json['object'] and request_json['object']['searchableBy'] != 'https://www.w3.org/ns/activitystreams#Public':
                post.indexable = False

            db.session.add(post)
            post.ranking = post.post_ranking(post.score, post.posted_at)
            post.ranking_scaled = int(post.ranking + community.scale_by())
            community.post_count += 1
            community.last_active = utcnow()
            db.session.execute(text('UPDATE "user" SET post_count = post_count + 1 WHERE id = :user_id'),
                               {'user_id': user.id})
            db.session.execute(text('UPDATE "site" SET last_active = NOW()'))
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return Post.query.filter_by(ap_id=request_json['object']['id']).one()

            # Mentions also need a post_id
            if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
                for json_tag in request_json['object']['tag']:
                    if 'type' in json_tag and json_tag['type'] == 'Mention':
                        profile_id = json_tag['href'] if 'href' in json_tag else None
                        if profile_id and isinstance(profile_id, str) and profile_id.startswith(current_app.config['SERVER_URL']):
                            profile_id = profile_id.lower()
                            recipient = User.query.filter_by(ap_profile_id=profile_id, ap_id=None).first()
                            if recipient:
                                blocked_senders = blocked_users(recipient.id)
                                if post.user_id not in blocked_senders:
                                    author = User.query.get(post.user_id).first()
                                    targets_data = {'gen': '0',
                                                    'post_id': post.id,
                                                    'post_body': post.body,
                                                    'post_title': post.title,
                                                    'author_user_name': author.ap_id if author.ap_id else author.user_name,
                                                    }
                                    # import here to avoid circular import errors
                                    from app.utils import get_recipient_language
                                    with force_locale(get_recipient_language(recipient.id)):
                                        notification = Notification(user_id=recipient.id, title=gettext(f"You have been mentioned in post {post.id}"),
                                                                    url=f"{current_app.config['SERVER_URL']}/post/{post.id}",
                                                                    author_id=post.user_id, notif_type=NOTIF_MENTION,
                                                                    subtype='post_mention',
                                                                    targets=targets_data)
                                        recipient.unread_notifications += 1
                                        db.session.add(notification)
                                        db.session.commit()

            # Polls need to be processed quite late because they need a post_id to refer to
            if request_json['object']['type'] == 'Question':
                post.type = constants.POST_TYPE_POLL
                mode = 'single'
                if 'anyOf' in request_json['object']:
                    mode = 'multiple'
                poll = Poll(post_id=post.id, end_poll=request_json['object']['endTime'], mode=mode, local_only=False)
                db.session.add(poll)
                i = 1
                for choice_ap in request_json['object']['oneOf' if mode == 'single' else 'anyOf']:
                    new_choice = PollChoice(post_id=post.id, choice_text=choice_ap['name'], sort_order=i)
                    db.session.add(new_choice)
                    i += 1
                db.session.commit()

            if request_json['object']['type'] == 'Event':
                post.type = constants.POST_TYPE_EVENT
                event = Event(post_id=post.id,
                              start=request_json['object']['startTime'],
                              end=request_json['object']['endTime'],
                              timezone=request_json['object']['timezone'],
                              max_attendees=request_json['object']['maximumAttendeeCapacity'],
                              participant_count=request_json['object']['participantCount'],
                              online_link=request_json['object']['onlineLink'],
                              join_mode=request_json['object']['joinMode'],
                              external_participation_url=request_json['object']['externalParticipationUrl'],
                              anonymous_participation=request_json['object']['anonymousParticipation'],
                              online=request_json['object']['isOnline'],
                              buy_tickets_link=request_json['object']['buyTicketsLink'],
                              event_fee_currency=request_json['object']['feeCurrency'],
                              event_fee_amount=request_json['object']['feeAmount'])
                db.session.add(event)
                post.url = ''   # Mobilizon puts the AP ID in request_json['object']['url'] and any attached website links in a request_json['object']['attachment'] list
                if ('attachment' in request_json['object'] and
                        isinstance(request_json['object']['attachment'], list) and
                        len(request_json['object']['attachment']) > 0):
                    for attachment_item in request_json['object']['attachment']:
                        if attachment_item['type'] == 'Link':
                            if 'href' in attachment_item:
                                post.url = attachment_item['href']
                                break
                if 'image' in request_json['object'] and post.image is None:
                    image = File(source_url=request_json['object']['image']['url'])
                    db.session.add(image)
                    post.image = image

                db.session.commit()

            if post.image_id and not post.type == constants.POST_TYPE_VIDEO and get_setting('cache_remote_images_locally', True):
                if post.type == constants.POST_TYPE_IMAGE:
                    make_image_sizes(post.image_id, 512, 1200, 'posts',
                                     community.low_quality)  # the 512 sized image is for masonry view
                else:
                    make_image_sizes(post.image_id, 170, 512, 'posts',
                                     community.low_quality)  # the 512 sized image is for masonry view and API responses

            # Update list of cross posts
            if post.url:
                post.calculate_cross_posts()

            if post.community_id not in communities_banned_from(user.id) and post.status == POST_STATUS_PUBLISHED:
                notify_about_post(post)

            # attach initial upvote to author
            vote = PostVote(user_id=user.id, post_id=post.id, author_id=user.id, effect=1)
            db.session.add(vote)
            if user.is_local():
                cache.delete_memoized(recently_upvoted_posts, user.id)

            post.generate_slug(community)
            db.session.commit()

            # check new accounts to see if their comments are AI generated
            if current_app.config['DETECT_AI_ENDPOINT'] and user.created_very_recently() and len(post.body) > 250:
                from app.utils import get_request, notify_admin
                is_ai = get_request(f"{current_app.config['DETECT_AI_ENDPOINT']}?url={post.ap_id}")
                if is_ai and is_ai.status_code == 200:
                    is_ai_result = is_ai.json()
                    if is_ai_result['confidence'] > 0.8:
                        # use redis to keep track of the posts this person has done in the last day and whether each is AI-generated
                        from app import redis_client

                        redis_key = f"ai_detection:user:{user.id}"
                        now = time()

                        # Store each detection as a JSON entry with timestamp
                        detection_data = {
                            'detection': is_ai_result['detection_result'],
                            'confidence': is_ai_result['confidence'],
                            'timestamp': utcnow().isoformat()
                        }

                        # Add with timestamp as score
                        redis_client.zadd(redis_key, {json.dumps(detection_data): now})

                        # Remove entries older than 24h
                        redis_client.zremrangebyscore(redis_key, 0, now - 86400)

                        # Get all recent detections
                        detections = redis_client.zrange(redis_key, 0, -1)
                        if len(detections) >= 3:
                            ai_count = sum(1 for d in detections if json.loads(d)['detection'] != 'human')
                            ai_percentage = ai_count / len(detections)

                            # if there are 3 or more posts and > 66% of them are ai generated
                            if ai_percentage > 0.66:
                                user.banned = True  # ban
                                db.session.commit()

                                # notify admin
                                targets_data = {'gen': '0',
                                                'suspect_user_id': user.id,
                                                'suspect_user_user_name': user.ap_id if user.ap_id else user.user_name,
                                                'source_instance_id': 1,
                                                'source_instance_domain': '',
                                                'reporter_id': 1,
                                                'reporter_user_name': 'automated'
                                                }
                                notify_admin('User auto-banned for AI-generated content', f'/u/{user.link()}', 1,
                                             NOTIF_REPORT, 'user_reported', targets_data)

        return post

    def calculate_cross_posts(self, delete_only=False, url_changed=False):
        if not self.url and not delete_only:
            return

        if self.cross_posts and (url_changed or delete_only):
            old_cross_posts = db.session.query(Post).filter(Post.id.in_(self.cross_posts),
                                                            Post.status == POST_STATUS_PUBLISHED).all()
            self.cross_posts.clear()
            for ocp in old_cross_posts:
                if ocp.cross_posts and self.id in ocp.cross_posts:
                    ocp.cross_posts.remove(self.id)

            db.session.commit()
        if delete_only:
            return

        if self.url.count('/') < 3 or (self.url.count('/') == 3 and self.url.endswith('/')):
            # reject if url is just a domain without a path
            return

        if self.community.ap_profile_id == 'https://lemmy.zip/c/dailygames':
            # daily posts to this community (e.g. to https://travle.earth/usa or https://www.nytimes.com/games/wordle/index.html) shouldn't be treated as cross-posts
            return

        limit = 9
        new_cross_posts = db.session.query(Post).filter(Post.id != self.id, Post.url == self.url, Post.deleted == False,
                                                        Post.status > POST_STATUS_REVIEWING).order_by(desc(Post.id)).limit(limit)

        # other posts: update their cross_posts field with this post.id if they have less than the limit
        for ncp in new_cross_posts:
            if ncp.cross_posts is None:
                ncp.cross_posts = [self.id]
            elif len(ncp.cross_posts) < limit:
                ncp.cross_posts.append(self.id)

        # this post: set the cross_posts field to the limited list of ids from the most recent other posts
        if new_cross_posts.count() > 0:
            self.cross_posts = [ncp.id for ncp in new_cross_posts]
        db.session.commit()

    def delete_dependencies(self):
        # Handle non-cascading deletes and special cleanup

        # ModLog entries should be preserved with NULL post_id
        db.session.query(ModLog).filter(ModLog.post_id == self.id).update({ModLog.post_id: None})

        # Reports should be deleted
        db.session.query(Report).filter(Report.suspect_post_id == self.id).delete()

        # Reminders should be deleted
        db.session.query(Reminder).filter(Reminder.reminder_destination == self.id, Reminder.reminder_type == 1).delete()

        # Delete event_user entries (association table, no cascade relationship)
        db.session.execute(text('DELETE FROM "event_user" WHERE post_id = :post_id'), {'post_id': self.id})

        db.session.execute(text('DELETE FROM "hidden_posts" WHERE hidden_post_id = :post_id'), {'post_id': self.id})
        db.session.execute(text('DELETE FROM "read_posts" WHERE read_post_id = :post_id'), {'post_id': self.id})

        # Handle file deletions from disk before cascade deletes the File records
        if self.image_id and self.image:
            self.image.delete_from_disk(purge_cdn=False)
        if self.type == POST_TYPE_VIDEO and _store_files_in_s3() and self.url.startswith(f'https://{current_app.config["S3_PUBLIC_URL"]}'):
            from app.shared.tasks.maintenance import delete_from_s3
            if current_app.debug:
                delete_from_s3([self.url])
            else:
                delete_from_s3.delay([self.url])

        for reply in self.replies:
            if reply.image_id and reply.image:
                reply.image.delete_from_disk(purge_cdn=False)
            # Update ModLog entries to remove references to deleted replies
            db.session.query(ModLog).filter(ModLog.reply_id == reply.id).update({ModLog.reply_id: None})
            # Delete reports for this reply
            db.session.query(Report).filter(Report.suspect_post_reply_id == reply.id).delete()

        if self.archived:
            if self.archived.startswith(f'https://{current_app.config["S3_PUBLIC_URL"]}') and _store_files_in_s3():
                from app.shared.tasks.maintenance import delete_from_s3
                s3_path = self.archived.replace(f'https://{current_app.config["S3_PUBLIC_URL"]}/', '')
                s3_files_to_delete = []
                s3_files_to_delete.append(s3_path)
                if current_app.debug:
                    delete_from_s3(s3_files_to_delete)
                else:
                    delete_from_s3.delay(s3_files_to_delete)
            elif os.path.isfile(self.archived):
                try:
                    os.unlink(self.archived)
                except FileNotFoundError:
                    ...

    def has_been_reported(self):
        return self.reports > 0 and current_user.is_authenticated and self.community.is_moderator()

    def youtube_embed(self, rel=True) -> str:
        if self.url:
            parsed_url = urlparse(self.url)
            query_params = parse_qs(parsed_url.query)

            # Handle playlists
            if 'list' in query_params:
                playlist_id = query_params['list'][0]
                return f'videoseries?list={playlist_id}'

            if 'v' in query_params:
                video_id = query_params.pop('v')[0]
                if rel:
                    query_params['rel'] = '0'
                new_query = urlencode(query_params, doseq=True)
                return f'{video_id}?{new_query}'

            if '/shorts/' in parsed_url.path:
                video_id = parsed_url.path.split('/shorts/')[1].split('/')[0]
                if 't' in query_params:
                    query_params['start'] = query_params.pop('t')[0]
                if rel:
                    query_params['rel'] = '0'
                new_query = urlencode(query_params, doseq=True)
                return f'{video_id}?{new_query}'

        return ''

    def youtube_video_id(self) -> str:
        if self.url:
            parsed_url = urlparse(self.url)
            query_params = parse_qs(parsed_url.query)

            if 'v' in query_params:
                return query_params['v'][0]
            if '/shorts/' in parsed_url.path:
                video_id = parsed_url.path.split('/shorts/')[1].split('/')[0]
                return f'{video_id}'

        return ''

    def url_domain(self):
        return 'https://' + furl(self.url).host + '/'

    def generate_ap_id(self, community: Community):
        if not community.post_url_type or community.post_url_type == 'friendly':
            # Make the ActivityPub ID of a post in the format of instance.tld/c/community@instance/p/post_id/post-title-as-slug
            # Use this for posts this instance is creating only - remote posts will already have an AP ID.
            if self.ap_id is None or self.ap_id == '' or len(self.ap_id) == 10:
                slug = slugify(self.title, max_length=100 - len(current_app.config["SERVER_NAME"]))
                if slug:
                    self.ap_id = f'{current_app.config["SERVER_URL"]}/c/{community.name}/p/{self.id}/{slug}'
                    self.slug = f'/c/{community.name}/p/{self.id}/{slug}'
                else:
                    # Post title can't be slugified, fall back to old url structure
                    self.ap_id = f'{current_app.config["SERVER_URL"]}/post/{self.id}'
                    self.slug = f'/post/{self.id}'
        else:
            # Make the ActivityPub ID of a post in the format of instance.tld/post/post_id
            if self.ap_id is None or self.ap_id == '' or len(self.ap_id) == 10:
                self.ap_id = f'{current_app.config["SERVER_URL"]}/post/{self.id}'
                self.slug = f'/post/{self.id}'

    def generate_slug(self, community: Community):
        if not community.post_url_type or community.post_url_type == 'friendly':
            # Make the slug of a post in the format of /c/community@instance/p/post_id/post-title-as-slug
            # This should only be used for incoming remote posts. Locally-made posts will have a slug from generate_ap_id()
            if self.slug is None or self.slug == '':
                slug = slugify(self.title, max_length=100 - len(current_app.config["SERVER_NAME"]))
                if slug:
                    self.slug = f'/c/{community.name}/p/{self.id}/{slug}'
                else:
                    self.slug = f'/post/{self.id}'
        else:
            # Make the slug use the old format of /post/post_id
            if self.slug is None or self.slug == '':
                self.slug = f'/post/{self.id}'

    def peertube_embed(self):
        if self.url:
            return self.url.replace('watch', 'embed')

    def profile_id(self):
        if self.ap_id:
            return self.ap_id
        else:
            return f"{current_app.config['SERVER_URL']}/post/{self.id}"

    def public_url(self):
        return self.profile_id()

    def blocked_by_content_filter(self, content_filters, user_id):
        if self.user_id == user_id:
            return False

        # tokenize title into words (lowercase)
        tokens = re.findall(r"\w+", self.title.lower())

        for name, keywords in (content_filters or {}).items():
            for keyword in keywords:
                if keyword.lower() in tokens:
                    return name
        return False

    def blurred(self, user):
        if user is None:
            return self.nsfw or self.nsfl or self.spoiler_flair()
        else:
            return (user.hide_nsfw == 2 and self.nsfw) or \
                (user.hide_nsfl == 2 and self.nsfl) or \
                (user.ignore_bots == 2 and self.from_bot) or \
                self.spoiler_flair()

    def posted_at_localized(self, sort, locale):
        # some locales do not have a definition for 'weeks' so are unable to display some dates in some languages. Fall back to english for those languages.
        try:
            return arrow.get(self.last_active if sort == 'active' else self.posted_at).humanize(locale=locale)
        except ValueError:
            return arrow.get(self.last_active if sort == 'active' else self.posted_at).humanize(locale='en')

    def notify_new_replies(self, user_id: int) -> bool:
        existing_notification = db.session.query(NotificationSubscription).\
            filter(NotificationSubscription.entity_id == self.id,
                   NotificationSubscription.user_id == user_id,
                   NotificationSubscription.type == NOTIF_POST).first()
        return existing_notification is not None

    def language_code(self):
        if self.language_id:
            return self.language.code
        else:
            return 'en'

    def language_name(self):
        if self.language_id:
            return self.language.name
        else:
            return 'English'

    def tags_for_activitypub(self):
        return_value = []
        for flair in self.flair:
            return_value.append({'type': 'lemmy:CommunityTag',
                                 'id': f'{current_app.config["SERVER_URL"]}/c/{self.community.link()}/tag/{flair.id}',
                                 'display_name': flair.flair,
                                 'text_color': flair.text_color,
                                 'background_color': flair.background_color,
                                 'blur_images': flair.blur_images})
        for tag in self.tags:
            return_value.append({'type': 'Hashtag',
                                 'href': f'{current_app.config["SERVER_URL"]}/tag/{tag.name}',
                                 'name': f'#{tag.name}'})

        # include emojis used in body text
        if self.body and ':' in self.body:
            from app.utils import guess_mime_type
            EMOJI_RE = re.compile(r':([a-z0-9_+-]{1,20}):', re.IGNORECASE)
            tokens = {
                f':{m.group(1).lower()}:'
                for m in EMOJI_RE.finditer(self.body)
            }

            if tokens:
                emojis = db.session.query(Emoji).filter(Emoji.token.in_(tokens)).order_by(Emoji.instance_id).all()
                for emoji in emojis:
                    return_value.append({
                        'type': 'Emoji',
                        'name': emoji.token,
                        'icon': {
                            'type': 'Image',
                            'mediaType': guess_mime_type(emoji.url),  # or store this in DB
                            'url': emoji.url,
                        },
                    })

        return return_value

    def spoiler_flair(self):
        for flair in self.flair:
            if flair.blur_images:
                return True

        return False

    def post_reply_count_recalculate(self):
        self.post_reply_count = db.session.execute(
            text('SELECT COUNT(*) as c FROM "post_reply" WHERE post_id = :post_id AND deleted is false'),
            {'post_id': self.id}).scalar()

    # All the following post/comment ranking math is explained at https://medium.com/hacking-and-gonzo/how-reddit-ranking-algorithms-work-ef111e33d0d9
    epoch = datetime(1970, 1, 1)

    def epoch_seconds(self, post_date):
        td = post_date - self.epoch
        return td.days * 86400 + td.seconds + (float(td.microseconds) / 1000000)

    # All the following post/comment ranking math is explained at https://medium.com/hacking-and-gonzo/how-reddit-ranking-algorithms-work-ef111e33d0d9
    def post_ranking(self, score, post_date: datetime):
        if post_date is None:
            post_date = datetime.utcnow()
        if score is None:
            score = 1
        order = math.log(max(abs(score), 1), 10)
        sign = 1 if score > 0 else -1 if score < 0 else 0
        seconds = self.epoch_seconds(post_date) - 1685766018
        return round(sign * order + seconds / 45000, 7)

    def vote(self, user: User, vote_direction: str, emoji: str | None):
        from app import redis_client
        if vote_direction == 'downvote':
            if self.author.has_blocked_user(user.id) or self.author.has_blocked_instance(user.instance_id):
                return None
        with redis_client.lock(f"lock:post:{self.id}", timeout=10, blocking_timeout=6):
            existing_vote = PostVote.query.filter_by(user_id=user.id, post_id=self.id).first()
            if existing_vote and vote_direction == 'reversal':  # api sends '1' for upvote, '-1' for downvote, and '0' for reversal
                if existing_vote.effect == 1:
                    vote_direction = 'upvote'
                elif existing_vote.effect == -1:
                    vote_direction = 'downvote'
            assert vote_direction == 'upvote' or vote_direction == 'downvote'
            undo = None
            if existing_vote:
                # If emoji is provided and vote direction matches existing vote, just update the emoji
                if emoji and ((existing_vote.effect > 0 and vote_direction == 'upvote') or
                             (existing_vote.effect < 0 and vote_direction == 'downvote')):
                    existing_vote.emoji = emoji
                    db.session.commit()
                    self.update_reaction_cache()
                    db.session.commit()
                    return None  # No undo, vote stays as-is with new emoji

                with redis_client.lock(f"lock:vote:{existing_vote.id}", timeout=10, blocking_timeout=6):
                    if not self.community.low_quality:
                        with redis_client.lock(f"lock:user:{self.user_id}", timeout=10, blocking_timeout=6):
                            db.session.execute(
                                text('UPDATE "user" SET reputation = reputation - :effect WHERE id = :user_id'),
                                {'effect': existing_vote.effect, 'user_id': self.user_id})
                            db.session.commit()
                    if existing_vote.effect > 0:  # previous vote was up
                        if vote_direction == 'upvote':  # new vote is also up, so remove it
                            db.session.delete(existing_vote)
                            db.session.commit()
                            self.up_votes -= 1
                            self.score -= existing_vote.effect  # score - (+1) = score-1
                            undo = 'Like'
                        else:  # new vote is down while previous vote was up, so reverse their previous vote
                            existing_vote.effect = -1
                            existing_vote.emoji = emoji
                            db.session.commit()
                            self.up_votes -= 1
                            self.down_votes += 1
                            self.score += existing_vote.effect * 2  # score + (-2) = score-2
                    else:  # previous vote was down
                        if vote_direction == 'downvote':  # new vote is also down, so remove it
                            db.session.delete(existing_vote)
                            db.session.commit()
                            self.down_votes -= 1
                            self.score -= existing_vote.effect  # score - (-1) = score+1
                            undo = 'Dislike'
                        else:  # new vote is up while previous vote was down, so reverse their previous vote
                            existing_vote.effect = 1
                            existing_vote.emoji = emoji
                            db.session.commit()
                            self.up_votes += 1
                            self.down_votes -= 1
                            self.score += existing_vote.effect * 2  # score + (+2) = score+2
                    db.session.commit()
            else:
                if vote_direction == 'upvote':
                    effect = 1.0
                    spicy_effect = effect
                    # Make 'hot' sort more spicy by amplifying the effect of early upvotes
                    if self.up_votes + self.down_votes <= 10:
                        spicy_effect = effect * current_app.config['SPICY_UNDER_10']
                    elif self.up_votes + self.down_votes <= 30:
                        spicy_effect = effect * current_app.config['SPICY_UNDER_30']
                    elif self.up_votes + self.down_votes <= 60:
                        spicy_effect = effect * current_app.config['SPICY_UNDER_60']
                    if user.cannot_vote():
                        effect = spicy_effect = 0
                    self.up_votes += 1
                    self.score += spicy_effect  # score + (+1) = score+1
                else:
                    effect = -1.0
                    spicy_effect = effect
                    self.down_votes += 1
                    # Make 'hot' sort more spicy by amplifying the effect of early downvotes
                    if self.up_votes + self.down_votes <= 30:
                        spicy_effect *= current_app.config['SPICY_UNDER_30']
                    elif self.up_votes + self.down_votes <= 60:
                        spicy_effect *= current_app.config['SPICY_UNDER_60']
                    if user.cannot_vote():
                        effect = spicy_effect = 0
                    self.score += spicy_effect  # score + (-1) = score-1
                vote = PostVote(user_id=user.id, post_id=self.id, author_id=self.author.id,
                                effect=effect, emoji=emoji)
                # upvotes do not increase reputation in low quality communities
                if self.community.low_quality and effect > 0:
                    effect = 0
                with redis_client.lock(f"lock:user:{self.user_id}", timeout=10, blocking_timeout=6):
                    db.session.execute(text('UPDATE "user" SET reputation = reputation + :effect WHERE id = :user_id'),
                                       {'effect': effect, 'user_id': self.user_id})
                    db.session.commit()
                db.session.add(vote)

            if emoji or emoji == '-1':
                db.session.commit()
                self.update_reaction_cache()

            # Calculate new ranking values
            self.ranking = self.post_ranking(self.score + self.reply_count, self.created_at)
            self.ranking_scaled = int(self.ranking + self.community.scale_by())

            db.session.commit()
            if user.is_local():
                from app.utils import recently_upvoted_posts, recently_downvoted_posts
                cache.delete_memoized(recently_upvoted_posts, user.id)
                cache.delete_memoized(recently_downvoted_posts, user.id)
        return undo

    def move_to(self, community: Community):
        self.community_id = community.id
        self.instance_id = community.instance_id
        self.flair.clear()
        db.session.execute(text('UPDATE post_reply SET community_id = :community_id, instance_id = :instance_id WHERE post_id = :post_id'),
                           {'community_id': community.id, 'instance_id': community.instance_id, 'post_id': self.id})

    def update_reaction_cache(self):
        count = func.count().label("count")
        # Use LEFT JOIN so unicode emojis (not in Emoji table) are included
        rows = db.session.query(PostVote.emoji, Emoji.url, count,
                                func.array_agg(User.user_name).label("authors")).\
            outerjoin(Emoji, PostVote.emoji == Emoji.token).\
            join(User, User.id == PostVote.user_id).\
            filter(PostVote.post_id == self.id, PostVote.emoji.isnot(None)).\
            group_by(PostVote.emoji, Emoji.url).\
            order_by(count.desc()).all()

        self.emoji_reactions = [
            {
                "url": url if url else '',  # None for unicode emoji, URL for custom emoji
                "token": emoji,  # The actual emoji value (unicode or :token:)
                "authors": authors,
                "count": count,
            }
            for emoji, url, count, authors in rows
        ]


class PostReply(db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), index=True)
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    parent_id = db.Column(db.Integer, index=True)
    root_id = db.Column(db.Integer, index=True)
    depth = db.Column(db.Integer, default=0)
    path = db.Column(MutableList.as_mutable(ARRAY(db.Integer)), index=True)
    child_count = db.Column(db.Integer, default=0)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    body_html_safe = db.Column(db.Boolean, default=False)
    score = db.Column(db.Integer, default=0, index=True)  # used for 'top' sorting
    indexable = db.Column(db.Boolean, default=True, index=True)
    nsfw = db.Column(db.Boolean, default=False, index=True)
    distinguished = db.Column(db.Boolean, default=False)
    notify_author = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, index=True, default=utcnow)
    posted_at = db.Column(db.DateTime, index=True, default=utcnow)
    deleted = db.Column(db.Boolean, default=False, index=True)
    deleted_by = db.Column(db.Integer, index=True)
    replies_enabled = db.Column(db.Boolean, default=True)
    sticky = db.Column(db.Boolean, default=False, index=True)
    ip = db.Column(db.String(50))
    from_bot = db.Column(db.Boolean, default=False, index=True)
    up_votes = db.Column(db.Integer, default=0)
    down_votes = db.Column(db.Integer, default=0)
    ranking = db.Column(db.Float, default=0.0, index=True)  # used for 'hot' sorting
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'), index=True)
    edited_at = db.Column(db.DateTime)
    reports = db.Column(db.Integer, default=0)  # how many times this post has been reported. Set to -1 to ignore reports
    answer = db.Column(db.Boolean, default=False)   # this comment was designated as the best answer to a question
    emoji_reactions = db.Column(db.JSON)            # a cache of the emoji reactions a post has received, to avoid joins

    ap_id = db.Column(db.String(255), index=True, unique=True)
    ap_create_id = db.Column(db.String(100))
    ap_announce_id = db.Column(db.String(100))
    ap_updated = db.Column(db.DateTime)  # When the remote instance edited the PostReply. Useful when local instance has been offline and a flurry of potentially out of order updates are coming in.

    search_vector = db.Column(TSVectorType('body', auto_index=False))

    author = db.relationship('User', lazy='joined', foreign_keys=[user_id], single_parent=True, overlaps="post_replies")
    community = db.relationship('Community', lazy='joined', overlaps='replies', foreign_keys=[community_id])
    language = db.relationship('Language', foreign_keys=[language_id], lazy='joined')
    image = db.relationship('File', foreign_keys=[image_id], cascade='all, delete')
    modlog = db.relationship('ModLog', lazy='dynamic', foreign_keys="ModLog.reply_id", back_populates='reply')
    votes = db.relationship('PostReplyVote', lazy='dynamic', backref='reply', cascade='all, delete-orphan', passive_deletes=True)
    bookmarks = db.relationship('PostReplyBookmark', backref='reply', cascade='all, delete-orphan')

    __table_args__ = (
        db.Index(
            'ix_post_reply_community_id_not_deleted',
            'community_id',
            postgresql_where=db.text('deleted = false')
        ),
        db.Index(
            'idx_post_reply_fts',
            'search_vector',
            postgresql_using='gin'
        ),
        db.Index(
            'idx_post_reply_path',
            'path',
            postgresql_using='gin'
        ),
        db.Index(
            'ix_post_reply_community_posted_bot',
            'community_id', 'posted_at',
            postgresql_where=db.text('from_bot = false')
        )
    )

    @classmethod
    def new(cls, user: User, post: Post, in_reply_to, body, body_html, notify_author, language_id, distinguished, answer,
            request_json: dict = None, announce_id=None, session=None):
        from app.utils import shorten_string, blocked_phrases, recently_upvoted_post_replies, reply_already_exists, \
            reply_is_just_link_to_gif_reaction, reply_is_stupid, wilson_confidence_lower_bound, get_setting
        from app.activitypub.util import notify_about_post_reply
        from app import redis_client

        if session is None:
            session = db.session

        if not post.comments_enabled:
            raise PostReplyValidationError(_('Comments are disabled on this post'))

        if user.ban_comments:
            raise PostReplyValidationError(_('Banned from commenting'))

        if in_reply_to is not None:
            parent_id = in_reply_to.id
            depth = in_reply_to.depth + 1
        else:
            parent_id = None
            depth = 0

        reply = PostReply(user_id=user.id, post_id=post.id, parent_id=parent_id,
                          depth=depth,
                          community_id=post.community.id, body=body,
                          body_html=body_html, body_html_safe=True,
                          from_bot=user.bot or user.bot_override, nsfw=post.nsfw,
                          notify_author=notify_author, instance_id=user.instance_id,
                          language_id=language_id,
                          distinguished=distinguished,
                          answer=answer,
                          indexable=user.indexable,
                          ap_id=request_json['object']['id'] if request_json else None,
                          ap_create_id=request_json['id'] if request_json else None,
                          ap_announce_id=announce_id)
        if request_json and request_json['type'] == 'Update':
            reply.edited_at = utcnow()
        if reply.body:
            for blocked_phrase in blocked_phrases():
                if blocked_phrase in reply.body:
                    raise PostReplyValidationError(_('Blocked phrase in comment'))
        if request_json and 'searchableBy' in request_json['object'] and request_json['object']['searchableBy'] != 'https://www.w3.org/ns/activitystreams#Public':
            reply.indexable = False
        if post.community.private:
            reply.indexable = False
        if in_reply_to is None or in_reply_to.parent_id is None:
            notification_target = post
        else:
            notification_target = PostReply.query.get(in_reply_to.parent_id)

        if notification_target.author.has_blocked_user(reply.user_id):
            raise PostReplyValidationError(_('Replier blocked'))

        if reply_already_exists(user_id=user.id, post_id=post.id, parent_id=reply.parent_id, body=reply.body):
            raise PostReplyValidationError(_('Duplicate reply'))

        site = Site.query.get(1)
        if site is None:
            site = Site()

        if reply_is_just_link_to_gif_reaction(reply.body) and site.enable_gif_reply_rep_decrease:
            user.reputation -= 1
            raise PostReplyValidationError(_('Gif comment ignored'))

        if reply_is_stupid(reply.body) and site.enable_this_comment_filter:
            raise PostReplyValidationError(_('Low quality reply'))

        try:
            session.add(reply)
            session.commit()
        except IntegrityError:
            session.rollback()
            return PostReply.query.filter_by(ap_id=request_json['object']['id']).one()

        if in_reply_to and in_reply_to.path:
            reply.path = in_reply_to.path[:]
            reply.path.append(reply.id)
            session.execute(text('update post_reply set child_count = child_count + 1 where id in :parents'),
                               {'parents': tuple(in_reply_to.path)})
        else:
            reply.path = [0, reply.id]
        reply.root_id = reply.path[1]

        # Notify subscribers
        notify_about_post_reply(in_reply_to, reply)

        # Subscribe to own comment
        if notify_author:
            new_notification = NotificationSubscription(name=shorten_string(_('Replies to my comment on %(post_title)s',
                                                                              post_title=post.title), 50),
                                                        user_id=user.id, entity_id=reply.id,
                                                        type=NOTIF_REPLY)
            session.add(new_notification)

        # upvote own reply
        reply.score = 1
        reply.up_votes = 1
        reply.ranking = wilson_confidence_lower_bound(1, 0)
        vote = PostReplyVote(user_id=user.id, post_reply_id=reply.id, author_id=user.id, effect=1)
        session.add(vote)
        if user.is_local():
            cache.delete_memoized(recently_upvoted_post_replies, user.id)

        reply.ap_id = reply.profile_id()

        with redis_client.lock(f"lock:post:{post.id}", timeout=10, blocking_timeout=6):
            if not user.bot:
                post.reply_count += 1
                post.community.post_reply_count += 1
                post.community.last_active = post.last_active = utcnow()
            session.execute(text('UPDATE "user" SET post_reply_count = post_reply_count + 1 WHERE id = :user_id'),
                               {'user_id': user.id})
            session.execute(text('UPDATE "site" SET last_active = NOW()'))
            session.commit()

            # update reply_count_cross_posted
            if post.cross_posts and len(post.cross_posts) > 0:
                ids = [post.id, *post.cross_posts]

                total = (session.query(db.func.sum(Post.reply_count)).filter(Post.id.in_(ids)).scalar()) or 0
                session.query(Post).filter(Post.id.in_(ids)).update({"reply_count_cross_posted": total}, synchronize_session=False)

                session.commit()
            else:
                post.reply_count_cross_posted = post.reply_count
                session.commit()

        # LLM Detection
        if reply.body and '—' in reply.body and user.created_very_recently() and get_setting('enable_report_em_dash_replies', True):
            # usage of em-dash is highly suspect.
            from app.utils import notify_admin
            # notify admin
            targets_data = {'gen': '0',
                            'suspect_user_id': user.id,
                            'suspect_user_user_name': user.ap_id if user.ap_id else user.user_name,
                            'source_instance_id': 1,
                            'source_instance_domain': '',
                            'reporter_id': 1,
                            'reporter_user_name': 'automated'
                            }
            notify_admin('Used em-dash in comment - likely AI', f'/u/{user.link()}', 1,
                         NOTIF_REPORT, 'user_reported', targets_data)
        elif current_app.config['DETECT_AI_ENDPOINT'] and user.created_very_recently() and len(reply.body) >= 250:
            # Use API to check new accounts to see if their comments are AI generated
            from app.utils import get_request, notify_admin
            is_ai = get_request(f"{current_app.config['DETECT_AI_ENDPOINT']}?url={reply.ap_id}")
            if is_ai and is_ai.status_code == 200:
                is_ai_result = is_ai.json()
                if is_ai_result['confidence'] > 0.8:
                    # use redis to keep track of the posts this person has done in the last day and whether each is AI-generated
                    from app import redis_client

                    redis_key = f"ai_detection:user:{user.id}"
                    now = time()

                    # Store each detection as a JSON entry with timestamp
                    detection_data = {
                        'reply_id': reply.id,
                        'detection': is_ai_result['detection_result'],
                        'confidence': is_ai_result['confidence'],
                        'timestamp': utcnow().isoformat()
                    }

                    # Add with timestamp as score
                    redis_client.zadd(redis_key, {json.dumps(detection_data): now})

                    # Remove entries older than 24h
                    redis_client.zremrangebyscore(redis_key, 0, now - 86400)

                    # Get all recent detections
                    detections = redis_client.zrange(redis_key, 0, -1)
                    if len(detections) >= 3:
                        ai_count = sum(1 for d in detections if json.loads(d)['detection'] != 'human')
                        ai_percentage = ai_count / len(detections)

                        # if there are 3 or more posts and > 66% of them are ai generated
                        if ai_percentage > 0.66:
                            user.banned = True  # ban
                            session.commit()

                            # notify admin
                            targets_data = {'gen': '0',
                                            'suspect_user_id': user.id,
                                            'suspect_user_user_name': user.ap_id if user.ap_id else user.user_name,
                                            'source_instance_id': 1,
                                            'source_instance_domain': '',
                                            'reporter_id': 1,
                                            'reporter_user_name': 'automated'
                                            }
                            notify_admin('User auto-banned for AI-generated content', f'/u/{user.link()}', 1,
                                         NOTIF_REPORT, 'user_reported', targets_data)

        return reply

    def language_code(self):
        if self.language_id:
            return self.language.code
        else:
            return 'en'

    def language_name(self):
        if self.language_id:
            return self.language.name
        else:
            return 'English'

    def is_local(self):
        return self.ap_id is None or self.ap_id.startswith(current_app.config['SERVER_URL'])

    @classmethod
    def get_by_ap_id(cls, ap_id):
        return db.session.query(cls).filter_by(ap_id=ap_id).first()

    def profile_id(self):
        if self.ap_id:
            return self.ap_id
        else:
            return f"{current_app.config['SERVER_URL']}/comment/{self.id}"

    def public_url(self):
        return self.profile_id()

    def posted_at_localized(self, locale):
        try:
            return arrow.get(self.posted_at).humanize(locale=locale)
        except ValueError:
            return arrow.get(self.posted_at).humanize(locale='en')

    # the ap_id of the parent object, whether it's another PostReply or a Post
    def in_reply_to(self):
        if self.parent_id is None:
            return self.post.ap_id
        else:
            parent = PostReply.query.get(self.parent_id)
            return parent.ap_id

    # the AP profile of the person who wrote the parent object, which could be another PostReply or a Post
    def to(self):
        if self.parent_id is None:
            return self.post.author.public_url()
        else:
            parent = PostReply.query.get(self.parent_id)
            return parent.author.public_url()

    def tags_for_activitypub(self):
        return_value = []
        # include emojis used in body text
        if self.body and ':' in self.body:
            from app.utils import guess_mime_type
            EMOJI_RE = re.compile(r':([a-z0-9_+-]{1,20}):', re.IGNORECASE)
            tokens = {
                f':{m.group(1).lower()}:'
                for m in EMOJI_RE.finditer(self.body)
            }

            if tokens:
                emojis = db.session.query(Emoji).filter(Emoji.token.in_(tokens)).order_by(Emoji.instance_id).all()
                for emoji in emojis:
                    return_value.append({
                        'type': 'Emoji',
                        'name': emoji.token,
                        'icon': {
                            'type': 'Image',
                            'mediaType': guess_mime_type(emoji.url),  # or store this in DB
                            'url': emoji.url,
                        },
                    })

        return return_value

    def delete_dependencies(self):
        """
        Handle non-cascading deletes and special cleanup.
        Note: PostReplyBookmark and PostReplyVote are now handled by cascade='all, delete-orphan'
        """

        # Reminders should be deleted (no relationship defined, small table)
        db.session.query(Reminder).filter(Reminder.reminder_destination == self.id, Reminder.reminder_type == 2).delete()

        # ModLog entries should be preserved with NULL reply_id
        db.session.query(ModLog).filter(ModLog.reply_id == self.id).update({ModLog.reply_id: None})

        # Reports should be deleted (small table, not worth adding cascade)
        db.session.query(Report).filter(Report.suspect_post_reply_id == self.id).delete()

        # Handle file deletion from disk before cascade deletes the File record
        if self.image_id and self.image:
            self.image.delete_from_disk(purge_cdn=False)

    def child_replies(self):
        return db.session(PostReply).filter_by(parent_id=self.id).all()

    def has_replies(self, include_deleted=False):
        if include_deleted:
            reply = db.session.query(PostReply).filter_by(parent_id=self.id).first()
        else:
            reply = db.session.query(PostReply).filter_by(parent_id=self.id).filter(PostReply.deleted == False).first()
        return reply is not None

    def has_been_reported(self):
        return self.reports > 0 and current_user.is_authenticated and self.community.is_moderator()

    def blocked_by_content_filter(self, content_filters):
        lowercase_body = self.body.lower()
        for name, keywords in content_filters.items() if content_filters else {}:
            for keyword in keywords:
                if keyword in lowercase_body:
                    return name
        return False

    def notify_new_replies(self, user_id: int) -> bool:
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == self.id,
                                                                      NotificationSubscription.user_id == user_id,
                                                                      NotificationSubscription.type == NOTIF_REPLY).first()
        return existing_notification is not None

    def vote(self, user: User, vote_direction: str, emoji: str):
        from app import redis_client
        from app.utils import wilson_confidence_lower_bound
        with redis_client.lock(f"lock:post_reply:{self.id}", timeout=10, blocking_timeout=6):
            existing_vote = db.session.query(PostReplyVote).filter_by(user_id=user.id, post_reply_id=self.id).first()
            if existing_vote and vote_direction == 'reversal':  # api sends '1' for upvote, '-1' for downvote, and '0' for reversal
                if existing_vote.effect == 1:
                    vote_direction = 'upvote'
                elif existing_vote.effect == -1:
                    vote_direction = 'downvote'
            assert vote_direction == 'upvote' or vote_direction == 'downvote'
            undo = None
            if existing_vote:
                # If emoji is provided and vote direction matches existing vote, just update the emoji
                if emoji and ((existing_vote.effect > 0 and vote_direction == 'upvote') or
                             (existing_vote.effect < 0 and vote_direction == 'downvote')):
                    existing_vote.emoji = emoji
                    db.session.commit()
                    self.update_reaction_cache()
                    db.session.commit()
                    return None  # No undo, vote stays as-is with new emoji

                with redis_client.lock(f"lock:user:{self.user_id}", timeout=10, blocking_timeout=6):
                    db.session.execute(text('UPDATE "user" SET reputation = reputation - :effect WHERE id = :user_id'),
                                       {'effect': existing_vote.effect, 'user_id': self.user_id})
                    db.session.commit()
                if existing_vote.effect > 0:  # previous vote was up
                    if vote_direction == 'upvote':  # new vote is also up, so remove it
                        db.session.delete(existing_vote)
                        db.session.commit()
                        self.up_votes -= 1
                        self.score -= 1
                        undo = 'Like'
                    else:  # new vote is down while previous vote was up, so reverse their previous vote
                        existing_vote.effect = -1
                        existing_vote.emoji = emoji
                        db.session.commit()
                        self.up_votes -= 1
                        self.down_votes += 1
                        self.score -= 2
                else:  # previous vote was down
                    if vote_direction == 'downvote':  # new vote is also down, so remove it
                        db.session.delete(existing_vote)
                        db.session.commit()
                        self.down_votes -= 1
                        self.score += 1
                        undo = 'Dislike'
                    else:  # new vote is up while previous vote was down, so reverse their previous vote
                        existing_vote.effect = 1
                        existing_vote.emoji = emoji
                        db.session.commit()
                        self.up_votes += 1
                        self.down_votes -= 1
                        self.score += 2
            else:
                if user.cannot_vote():
                    effect = 0
                else:
                    effect = 1
                if vote_direction == 'upvote':
                    self.up_votes += 1
                else:
                    effect = effect * -1
                    self.down_votes += 1
                self.score += effect
                vote = PostReplyVote(user_id=user.id, post_reply_id=self.id, author_id=self.author.id,
                                     effect=effect, emoji=emoji)
                with redis_client.lock(f"lock:user:{self.user_id}", timeout=10, blocking_timeout=6):
                    db.session.execute(text('UPDATE "user" SET reputation = reputation + :effect WHERE id = :user_id'),
                                       {'effect': effect, 'user_id': self.user_id})
                    db.session.commit()
                db.session.add(vote)
            if emoji or emoji == '-1':
                db.session.commit()
                self.update_reaction_cache()

            # Calculate the new ranking value
            self.ranking = wilson_confidence_lower_bound(self.up_votes, self.down_votes)
            db.session.commit()
            if user.is_local():
                from app.utils import recently_upvoted_post_replies, recently_downvoted_post_replies
                cache.delete_memoized(recently_upvoted_post_replies, user.id)
                cache.delete_memoized(recently_downvoted_post_replies, user.id)
        return undo

    def update_reaction_cache(self):
        count = func.count().label("count")
        # Use LEFT JOIN so unicode emojis (not in Emoji table) are included
        rows = db.session.query(PostReplyVote.emoji, Emoji.url, count,
                                func.array_agg(User.user_name).label("authors")).\
            outerjoin(Emoji, PostReplyVote.emoji == Emoji.token).\
            join(User, User.id == PostReplyVote.user_id).\
            filter(PostReplyVote.post_reply_id == self.id, PostReplyVote.emoji.isnot(None)).\
            group_by(PostReplyVote.emoji, Emoji.url).\
            order_by(count.desc()).all()

        self.emoji_reactions = [
            {
                "url": url if url else '',  # None for unicode emoji, URL for custom emoji
                "token": emoji,  # The actual emoji value (unicode or :token:)
                "authors": authors,
                "count": count,
            }
            for emoji, url, count, authors in rows
        ]


class ScheduledPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), index=True)
    licence_id = db.Column(db.Integer, db.ForeignKey('licence.id'), index=True)
    title = db.Column(db.String(255))
    url = db.Column(db.String(2048))
    body = db.Column(db.Text)
    microblog = db.Column(db.Boolean, default=False)
    nsfw = db.Column(db.Boolean, default=False, index=True)
    nsfl = db.Column(db.Boolean, default=False, index=True)
    sticky = db.Column(db.Boolean, default=False, index=True)
    indexable = db.Column(db.Boolean, default=True)
    from_bot = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, index=True, default=utcnow)
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'), index=True)
    scheduled_for = db.Column(db.DateTime, index=True)  # The first (or only) occurrence of this post
    repeat = db.Column(db.String(20), default='')  # 'daily', 'weekly', 'monthly'. Empty string = no repeat, just post once.

    @classmethod
    def new(cls, user, community: Community, request_json: dict):
        ...
        # use Post.new() for inspiration


class Domain(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), index=True)
    post_count = db.Column(db.Integer, default=0)
    banned = db.Column(db.Boolean, default=False, index=True)  # Domains can be banned site-wide (by admin) or DomainBlock'ed by users
    notify_mods = db.Column(db.Boolean, default=False, index=True)
    notify_admins = db.Column(db.Boolean, default=False, index=True)
    post_warning = db.Column(db.String(512))

    def blocked_by(self, user):
        block = DomainBlock.query.filter_by(domain_id=self.id, user_id=user.id).first()
        return block is not None

    def purge_content(self):
        files = File.query.join(Post).filter(Post.domain_id == self.id).all()
        for file in files:
            file.delete_from_disk(purge_cdn=False)
        posts = Post.query.filter_by(domain_id=self.id).all()
        for post in posts:
            post.delete_dependencies()
            db.session.delete(post)
        db.session.commit()


class DomainBlock(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class CommunityBlock(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class CommunityMember(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), primary_key=True)
    is_moderator = db.Column(db.Boolean, default=False)
    is_owner = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False, index=True)
    notify_new_posts = db.Column(db.Boolean, default=False)
    joined_via_feed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')

    __table_args__ = (
        db.Index('ix_community_member_community_banned', 'community_id', 'is_banned'),
    )


class CommunityWikiPage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    slug = db.Column(db.String(100), index=True)
    title = db.Column(db.String(255))
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    edited_at = db.Column(db.DateTime, default=utcnow)
    who_can_edit = db.Column(db.Integer, default=0)  # 0 = mods & admins, 1 = trusted, 2 = community members, 3 = anyone
    revisions = db.relationship('CommunityWikiPageRevision', backref=db.backref('page'), cascade='all,delete',
                                lazy='dynamic')

    def can_edit(self, user: User, community: Community):
        if user.is_anonymous:
            return False
        if self.who_can_edit == 0:
            if user.is_admin() or user.is_staff() or community.is_moderator(user):
                return True
        elif self.who_can_edit == 1:
            if user.is_admin() or user.is_staff() or community.is_moderator(user) or user.trustworthy():
                return True
        elif self.who_can_edit == 2:
            if user.is_admin() or user.is_staff() or community.is_moderator(
                    user) or user.trustworthy() or community.is_member(user):
                return True
        elif self.who_can_edit == 3:
            return True
        return False


class CommunityWikiPageRevision(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    wiki_page_id = db.Column(db.Integer, db.ForeignKey('community_wiki_page.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    title = db.Column(db.String(255))
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    edited_at = db.Column(db.DateTime, default=utcnow)

    author = db.relationship('User', lazy='joined', foreign_keys=[user_id])


class UserFollower(db.Model):
    local_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    remote_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    is_accepted = db.Column(db.Boolean, default=True)  # flip to ban remote user / reject follow
    is_inward = db.Column(db.Boolean, default=True)  # true = remote user is following a local one
    created_at = db.Column(db.DateTime, default=utcnow)


# people banned from communities
class CommunityBan(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)  # person who is banned, not the banner
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), primary_key=True)
    banned_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    reason = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=utcnow)
    ban_until = db.Column(db.DateTime)


class UserNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    target_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    body = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)


class UserExtraField(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    label = db.Column(db.String(1024))
    text = db.Column(db.String(1024))


class UserBlock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    blocker_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    blocked_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class Settings(db.Model):
    name = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(1024))


class Interest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))
    communities = db.Column(db.Text)


class CommunityJoinRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(UUID(as_uuid=True), index=True, default=uuid.uuid4)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    joined_via_feed = db.Column(db.Boolean, default=False)


class UserFollowRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    follow_id = db.Column(db.Integer, db.ForeignKey('user.id'))


class UserRegistration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    answer = db.Column(db.String(512))
    status = db.Column(db.Integer, default=0, index=True)  # 0 = unapproved, 1 = approved
    created_at = db.Column(db.DateTime, default=utcnow)
    approved_at = db.Column(db.DateTime)
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    warning = db.Column(db.String(100))
    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')

    def search_similar_names(self):
        return User.query.filter(or_(User.user_name == self.user.user_name, User.title == self.user.title),
                                 User.id != self.user.id).order_by(desc(User.banned)).order_by(User.reputation).limit(15)


class PostVote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id', ondelete='CASCADE'), index=True)
    effect = db.Column(db.Float, index=True)
    emoji = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=utcnow)

    __table_args__ = (
        Index('ix_post_vote_user_id_id_desc', 'user_id', desc('id')),
        db.Index(
            'ix_post_vote_post_created',
            'post_id', 'created_at'
        ),
        db.Index('ix_post_vote_created', 'created_at')
    )


class PostReplyVote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)  # who voted
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)  # the author of the reply voted on - who's reputation is affected
    post_reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id', ondelete='CASCADE'), index=True)
    effect = db.Column(db.Float)
    emoji = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=utcnow)

    __table_args__ = (
        Index('ix_post_reply_vote_user_id_id_desc', 'user_id', desc('id')),
        db.Index(
            'ix_post_reply_vote_reply_created',
            'post_reply_id', 'created_at'
        ),
        db.Index('ix_post_reply_vote_created', 'created_at')
    )


# save every activity to a log, to aid debugging
class ActivityPubLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    direction = db.Column(db.String(3))  # 'in' or 'out'
    activity_id = db.Column(db.String(256), index=True)
    activity_type = db.Column(db.String(50))  # e.g. 'Follow', 'Accept', 'Like', etc
    activity_json = db.Column(db.Text)  # the full json of the activity
    result = db.Column(db.String(10))  # 'success' or 'failure'
    exception_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)


class Filter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(50))
    filter_home = db.Column(db.Boolean, default=True)
    filter_posts = db.Column(db.Boolean, default=True)
    filter_replies = db.Column(db.Boolean, default=False)
    hide_type = db.Column(db.Integer, default=0)  # 0 = hide with warning, 1 = hide completely
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    expire_after = db.Column(db.Date)
    keywords = db.Column(db.String(500))

    def keywords_string(self):
        if self.keywords is None or self.keywords == '':
            return ''
        split_keywords = [kw.strip() for kw in self.keywords.split('\n')]
        return ', '.join(split_keywords)


class Role(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))
    weight = db.Column(db.Integer, default=0)
    permissions = db.relationship('RolePermission')


class RolePermission(db.Model):
    role_id = db.Column(db.Integer, db.ForeignKey('role.id'), primary_key=True)
    permission = db.Column(db.String, primary_key=True, index=True)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150))
    url = db.Column(db.String(512))
    read = db.Column(db.Boolean, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)  # who the notification should go to
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'))  # the person who caused the notification to happen
    created_at = db.Column(db.DateTime, default=utcnow)
    notif_type = db.Column(db.Integer, default=NOTIF_DEFAULT, index=True)  # see constants.py for possible values: NOTIF_*
    subtype = db.Column(db.String(50), index=True)
    targets = db.Column(db.JSON)


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reasons = db.Column(db.String(256))
    description = db.Column(db.String(256))
    status = db.Column(db.Integer, default=0)  # 0 = new, 1 = escalated to admin, 2 = being appealed, 3 = resolved, 4 = discarded
    type = db.Column(db.Integer, default=0)  # 0 = user, 1 = post, 2 = reply, 3 = community, 4 = conversation
    reporter_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    suspect_community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    suspect_user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    suspect_post_id = db.Column(db.Integer, db.ForeignKey('post.id'))
    suspect_post_reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id'))
    suspect_conversation_id = db.Column(db.Integer, db.ForeignKey('conversation.id'))
    in_community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    source_instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'))  # the instance of the reporter. mostly used to distinguish between local (instance 1) and remote reports
    created_at = db.Column(db.DateTime, default=utcnow)
    updated = db.Column(db.DateTime, default=utcnow)
    targets = db.Column(db.JSON)

    # textual representation of self.type
    def type_text(self):
        types = ('User', 'Post', 'Comment', 'Community', 'Conversation')
        if self.type is None:
            return ''
        else:
            return types[self.type]

    def is_local(self):
        return self.source_instance_id == 1


class NotificationSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256))  # to avoid needing to look up the thing subscribed to via entity_id
    type = db.Column(db.Integer, default=0, index=True)  # see constants.py for possible values: NOTIF_*
    entity_id = db.Column(db.Integer, index=True)  # ID of the user, post, community, etc being subscribed to
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)  # To whom this subscription belongs
    created_at = db.Column(db.DateTime, default=utcnow)  # Perhaps very old subscriptions can be automatically deleted


class Poll(db.Model):
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), primary_key=True)
    end_poll = db.Column(db.DateTime)
    mode = db.Column(db.String(10))  # 'single' or 'multiple' determines whether people can vote for one or multiple options
    local_only = db.Column(db.Boolean)
    latest_vote = db.Column(db.DateTime)

    choices = db.relationship('PollChoice', backref='poll', cascade='all, delete-orphan', foreign_keys='PollChoice.post_id', primaryjoin='Poll.post_id==PollChoice.post_id')

    def has_voted(self, user_id):
        existing_vote = PollChoiceVote.query.filter(PollChoiceVote.user_id == user_id,
                                                    PollChoiceVote.post_id == self.post_id).first()
        return existing_vote is not None

    def vote_for_choice(self, choice_id, user_id):
        existing_vote = PollChoiceVote.query.filter(PollChoiceVote.user_id == user_id,
                                                    PollChoiceVote.choice_id == choice_id).first()
        if not existing_vote:
            new_vote = PollChoiceVote(choice_id=choice_id, user_id=user_id, post_id=self.post_id)
            db.session.add(new_vote)
            choice = PollChoice.query.get(choice_id)
            choice.num_votes += 1
            self.latest_vote = utcnow()
            db.session.commit()
    
    def user_votes(self, user_id):
        existing_votes = PollChoiceVote.query.filter(PollChoiceVote.user_id == user_id,
                                                     PollChoiceVote.post_id == self.post_id).all()
        
        if not existing_votes:
            existing_votes = []
        
        return existing_votes

    def total_votes(self):
        return db.session.execute(text('SELECT SUM(num_votes) as s FROM "poll_choice" WHERE post_id = :post_id'),
                                  {'post_id': self.post_id}).scalar() or 0


class PollChoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    choice_text = db.Column(db.String(200))
    sort_order = db.Column(db.Integer)
    num_votes = db.Column(db.Integer, default=0)

    votes = db.relationship('PollChoiceVote', backref='choice', cascade='all, delete-orphan')

    def percentage(self, poll_total_votes):
        return math.floor(self.num_votes / poll_total_votes * 100)


class PollChoiceVote(db.Model):
    choice_id = db.Column(db.Integer, db.ForeignKey('poll_choice.id'), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class Event(db.Model):
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), primary_key=True)
    start = db.Column(db.DateTime, index=True)
    end = db.Column(db.DateTime)
    timezone = db.Column(db.String(30))
    max_attendees = db.Column(db.Integer, default=0)
    participant_count = db.Column(db.Integer, default=0)
    full = db.Column(db.Boolean, default=False)
    online_link = db.Column(db.String(1024))
    join_mode = db.Column(db.String(10), default='free')            # free, restricted, external, invite
    external_participation_url = db.Column(db.String(1024))         # join_made = external: the link to the place to RSVP, e.g. meetup.com
    anonymous_participation = db.Column(db.Boolean, default=False)
    online = db.Column(db.Boolean, default=False)
    buy_tickets_link = db.Column(db.String(1024))
    event_fee_currency = db.Column(db.String(4))
    event_fee_amount = db.Column(db.Float, default=0)
    location = db.Column(db.JSON)


event_user = db.Table('event_user', db.Column('post_id', db.Integer, db.ForeignKey('post.id')),
                      db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
                      db.Column('status', db.Integer),
                      db.Column('participation_message', db.String(200)),
                      db.PrimaryKeyConstraint('post_id', 'user_id'))


class PostBookmark(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class PostReplyBookmark(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id'), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class ModLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), nullable=True, index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=True, index=True)
    reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id'), nullable=True, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    type = db.Column(db.String(10))  # 'mod' or 'admin'
    action = db.Column(db.String(30))  # 'removing post', 'banning from community', etc
    reason = db.Column(db.String(512))
    link = db.Column(db.String(512))
    link_text = db.Column(db.String(512))
    public = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    author = db.relationship('User', lazy='joined', foreign_keys=[user_id], back_populates='modlog_actor')
    community = db.relationship('Community', lazy='joined', foreign_keys=[community_id], back_populates='modlog')
    target_user = db.relationship('User', lazy='joined', foreign_keys=[target_user_id], back_populates='modlog_target')
    post = db.relationship('Post', lazy='joined', foreign_keys=[post_id], back_populates='modlog')
    reply = db.relationship('PostReply', lazy='joined', foreign_keys=[reply_id], back_populates='modlog')

    action_map = {
        'add_mod': _l('Added moderator'),
        'remove_mod': _l('Removed moderator'),
        'featured_post': _l('Featured post'),
        'unfeatured_post': _l('Unfeatured post'),
        'delete_post': _l('Deleted post'),
        'restore_post': _l('Un-deleted post'),
        'delete_post_reply': _l('Deleted comment'),
        'restore_post_reply': _l('Un-deleted comment'),
        'delete_community': _l('Deleted community'),
        'delete_user': _l('Deleted account'),
        'undelete_user': _l('Restored account'),
        'ban_user': _l('Banned account'),
        'unban_user': _l('Un-banned account'),
        'lock_post': _l('Lock post'),
        'unlock_post': _l('Un-lock post'),
        'lock_post_reply': _l('Lock comment'),
        'unlock_post_reply': _l('Un-lock comment'),
        'move_post': _l('Move post')
    }

    def action_to_str(self):
        if self.action in self.action_map:
            return self.action_map[self.action]
        else:
            return self.action

    def get_correct_link(self):
        user_action_list = ["add_mod", "remove_mod", "delete_user", "undelete_user", "ban_user", "unban_user"]
        
        if self.action in user_action_list and not self.link.startswith("u/"):
            return "u/" + self.link
        else:
            return self.link

class IpBan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(50), index=True)
    notes = db.Column(db.String(150))
    created_at = db.Column(db.DateTime, default=utcnow)


class Site(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256))
    description = db.Column(db.String(256))
    icon_id = db.Column(db.Integer, db.ForeignKey('file.id'))
    sidebar = db.Column(db.Text, default='')
    sidebar_html = db.Column(db.Text, default='')
    legal_information = db.Column(db.Text, default='')
    legal_information_html = db.Column(db.Text, default='')
    tos_url = db.Column(db.String(256))
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    enable_downvotes = db.Column(db.Boolean, default=True)
    enable_gif_reply_rep_decrease = db.Column(db.Boolean, default=False)
    enable_chan_image_filter = db.Column(db.Boolean, default=False)
    enable_this_comment_filter = db.Column(db.Boolean, default=False)
    allow_local_image_posts = db.Column(db.Boolean, default=True)
    remote_image_cache_days = db.Column(db.Integer, default=30)
    enable_nsfw = db.Column(db.Boolean, default=False)
    enable_nsfl = db.Column(db.Boolean, default=False)
    community_creation_admin_only = db.Column(db.Boolean, default=False)
    reports_email_admins = db.Column(db.Boolean, default=True)
    registration_mode = db.Column(db.String(20), default='Closed')  # possible values: Open, RequireApplication, Closed
    application_question = db.Column(db.Text, default='')
    allow_or_block_list = db.Column(db.Integer, default=2)  # 1 = allow list, 2 = block list
    allowlist = db.Column(db.Text, default='')
    blocklist = db.Column(db.Text, default='')
    blocked_phrases = db.Column(db.Text, default='')  # discard incoming content with these phrases
    auto_decline_referrers = db.Column(db.Text, default='rdrama.net\nahrefs.com\nkiwifarms.sh')  # automatically decline registration requests if the referrer is one of these
    created_at = db.Column(db.DateTime, default=utcnow)
    updated = db.Column(db.DateTime, default=utcnow)
    last_active = db.Column(db.DateTime, default=utcnow)
    log_activitypub_json = db.Column(db.Boolean, default=False)
    default_theme = db.Column(db.String(20), default='')
    default_filter = db.Column(db.String(20), default='')
    contact_email = db.Column(db.String(255), default='')
    about = db.Column(db.Text, default='')
    about_html = db.Column(db.Text, default='')
    logo = db.Column(db.String(40), default='')
    logo_180 = db.Column(db.String(40), default='')
    logo_152 = db.Column(db.String(40), default='')
    logo_32 = db.Column(db.String(40), default='')
    logo_16 = db.Column(db.String(40), default='')
    show_inoculation_block = db.Column(db.Boolean, default=True)
    additional_css = db.Column(db.Text)
    additional_js = db.Column(db.Text)
    private_instance = db.Column(db.Boolean, default=False)
    language_id = db.Column(db.Integer)
    honeypot = db.Column(db.Boolean, default=True)
     

    @staticmethod
    def admins() -> List[User]:
        if hasattr(g, 'admin_ids'):
            return db.session.query(User).filter(User.id.in_(tuple(g.admin_ids))).all()
        else:
            return db.session.query(User).filter_by(deleted=False, banned=False).join(user_role).filter(
                                          or_(user_role.c.role_id == ROLE_ADMIN, User.id == 1)).order_by(User.id).all()

    @staticmethod
    def staff() -> List[User]:
        return db.session.query(User).filter_by(deleted=False, banned=False).join(user_role).filter(
                                      user_role.c.role_id == ROLE_STAFF).order_by(User.id).all()

    def active_now(self):
        return db.session.execute(text(
            "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '5 minutes' AND ap_id is null AND verified is true AND banned is false AND deleted is false")).scalar()

    def active_daily(self):
        from app.activitypub.util import active_day
        return active_day()

    def active_weekly(self):
        from app.activitypub.util import active_week
        return active_week()

    def active_monthly(self):
        from app.activitypub.util import active_month
        return active_month()

    def active_6monthly(self):
        from app.activitypub.util import active_half_year
        return active_half_year()

    def all_active_6monthly(self):
        return db.session.execute(text(
            "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '6 months' AND banned is false AND deleted is false")).scalar()

    def all_active_monthly(self):
        return db.session.execute(text(
            "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 month' AND banned is false AND deleted is false")).scalar()

    def all_active_weekly(self):
        return db.session.execute(text(
            "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 week' AND banned is false AND deleted is false")).scalar()

    def all_active_daily(self):
        return db.session.execute(text(
            "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 day' AND banned is false AND deleted is false")).scalar()


# class IngressQueue(db.Model):
#    id = db.Column(db.Integer, primary_key=True)
#    waiting_for = db.Column(db.String(255), index=True)         # The AP ID of the object we're waiting to be created before this Activity can be ingested
#    activity_pub_log_id = db.Column(db.Integer, db.ForeignKey('activity_pub_log.id')) # The original Activity that failed because some target object does not exist
#    ap_date_published = db.Column(db.DateTime, default=utcnow)  # The value of the datePublished field on the Activity
#    created_at = db.Column(db.DateTime, default=utcnow)
#    expires = db.Column(db.DateTime, default=utcnow)            # When to give up waiting and delete this row
#
#
@login.user_loader
def load_user(id):
    return db.session.query(User).get(int(id))


# --- Feeds Models ---

class FeedItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    feed_id = db.Column(db.Integer, db.ForeignKey('feed.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)


class FeedMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    feed_id = db.Column(db.Integer, db.ForeignKey('feed.id'), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    is_owner = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False, index=True)
    notify_new_communities = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)


class Feed(db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    title = db.Column(db.String(256))  # Human name
    name = db.Column(db.String(256), index=True, unique=True)  # url
    machine_name = db.Column(db.String(50), index=True)  # url also?!
    description = db.Column(db.Text)  # markdown
    description_html = db.Column(db.Text)  # html equivalent of above markdown
    nsfw = db.Column(db.Boolean, default=False)
    nsfl = db.Column(db.Boolean, default=False)
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    subscriptions_count = db.Column(db.Integer, default=0)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    instance = db.relationship('Instance', lazy='joined', foreign_keys=[instance_id])

    icon_id = db.Column(db.Integer, db.ForeignKey('file.id'))
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'))

    num_communities = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)
    public = db.Column(db.Boolean, default=False, index=True)
    last_edit = db.Column(db.DateTime, default=utcnow)
    parent_feed_id = db.Column(db.Integer, db.ForeignKey('feed.id'), index=True)
    is_instance_feed = db.Column(db.Boolean, default=False, index=True)

    ap_id = db.Column(db.String(255), index=True)
    ap_profile_id = db.Column(db.String(255), index=True, unique=True)
    ap_public_url = db.Column(db.String(255))
    ap_followers_url = db.Column(db.String(255))
    ap_following_url = db.Column(db.String(255))
    ap_domain = db.Column(db.String(255))
    ap_preferred_username = db.Column(db.String(255))
    ap_discoverable = db.Column(db.Boolean, default=False)
    ap_fetched_at = db.Column(db.DateTime)
    ap_deleted_at = db.Column(db.DateTime)
    ap_inbox_url = db.Column(db.String(255))
    ap_outbox_url = db.Column(db.String(255))
    ap_moderators_url = db.Column(db.String(255))

    banned = db.Column(db.Boolean, default=False)
    searchable = db.Column(db.Boolean, default=True)

    show_posts_in_children = db.Column(db.Boolean, default=False)
    member_communities = db.relationship('FeedItem', lazy='dynamic', cascade="all, delete-orphan")

    search_vector = db.Column(TSVectorType('name', 'description', auto_index=False))

    icon = db.relationship('File', lazy='joined', foreign_keys=[icon_id], single_parent=True, backref='feed', cascade="all, delete-orphan")
    image = db.relationship('File', lazy='joined', foreign_keys=[image_id], single_parent=True, cascade="all, delete-orphan")
    parent = db.relationship('Feed', remote_side=[id], backref=db.backref('children', lazy='dynamic'))

    def __repr__(self):
        return '<Feed {}_{}>'.format(self.name, self.id)

    @cache.memoize(timeout=500)
    def icon_image(self, size='default') -> str:
        if self.icon_id is not None:
            if size == 'default':
                if self.icon.file_path is not None:
                    if self.icon.file_path.startswith('app/'):
                        return self.icon.file_path.replace('app/', '/')
                    else:
                        return self.icon.file_path
                if self.icon.source_url is not None:
                    if self.icon.source_url.startswith('app/'):
                        return self.icon.source_url.replace('app/', '/')
                    else:
                        return self.icon.source_url
            elif size == 'tiny':
                if self.icon.thumbnail_path is not None:
                    if self.icon.thumbnail_path.startswith('app/'):
                        return self.icon.thumbnail_path.replace('app/', '/')
                    else:
                        return self.icon.thumbnail_path
                if self.icon.source_url is not None:
                    if self.icon.source_url.startswith('app/'):
                        return self.icon.source_url.replace('app/', '/')
                    else:
                        return self.icon.source_url
        return '/static/images/1px.gif'

    @cache.memoize(timeout=500)
    def header_image(self) -> str:
        if self.image_id is not None:
            if self.image.file_path is not None:
                if self.image.file_path.startswith('app/'):
                    return self.image.file_path.replace('app/', '/')
                else:
                    return self.image.file_path
            if self.image.source_url is not None:
                if self.image.source_url.startswith('app/'):
                    return self.image.source_url.replace('app/', '/')
                else:
                    return self.image.source_url
        return ''

    def display_name(self) -> str:
        if self.ap_id is None:
            return self.title
        else:
            return f"{self.title}@{self.ap_domain}"

    def link(self) -> str:
        if self.ap_id is None:
            return self.name
        else:
            return self.ap_id.lower()

    def lemmy_link(self) -> str:
        if self.ap_id is None:
            return f"~{self.name}@{current_app.config['SERVER_NAME']}"
        else:
            return f"~{self.ap_id.lower()}"

    def path(self):
        return_value = [self.machine_name]
        parent_id = self.parent_feed_id
        while parent_id is not None:
            parent_feed = Feed.query.get(parent_id)
            if parent_feed is None:
                break
            return_value.append(parent_feed.machine_name)
            parent_id = parent_feed.parent_feed_id
        return_value = list(reversed(return_value))
        return '/'.join(return_value)

    def creator(self):
        owner = User.query.get(self.user_id)
        return owner.ap_id if owner.ap_id else owner.user_name

    def parent_feed_name(self):
        parent_feed = Feed.query.get(self.parent_feed_id)
        return parent_feed.title if parent_feed else ""

    def subscribed(self, user_id: int) -> int:
        if user_id is None:
            return False
        subscription: FeedMember = FeedMember.query.filter_by(user_id=user_id, feed_id=self.id).first()
        if subscription:
            if subscription.is_owner:
                return SUBSCRIPTION_OWNER
            elif subscription.is_banned:
                return SUBSCRIPTION_BANNED
            return SUBSCRIPTION_MEMBER
        else:
            join_request = FeedJoinRequest.query.filter_by(user_id=user_id, feed_id=self.id).first()
            if join_request:
                return SUBSCRIPTION_PENDING
            else:
                return SUBSCRIPTION_NONMEMBER

    def profile_id(self):
        retval = self.ap_profile_id if self.ap_profile_id else f"{current_app.config['SERVER_URL']}/f/{self.name}"
        return retval.lower()

    def public_url(self):
        result = self.ap_public_url if self.ap_public_url else f"{current_app.config['SERVER_URL']}/f/{self.name}"
        return result

    def is_local(self):
        return self.ap_id is None or self.profile_id().startswith(current_app.config['SERVER_URL'])

    def local_url(self):
        if self.is_local():
            return self.ap_profile_id
        else:
            return f"{current_app.config['SERVER_URL']}/f/{self.ap_id}"

    def notify_new_posts(self, user_id: int) -> bool:
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == self.id,
                                                                      NotificationSubscription.user_id == user_id,
                                                                      NotificationSubscription.type == NOTIF_FEED).first()
        return existing_notification is not None

    # ids of all the users who want to be notified when there is an edit in this feed's communities
    def notification_subscribers(self):
        return list(db.session.execute(
            text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :feed_id AND type = :type '),
            {'feed_id': self.id, 'type': NOTIF_FEED}).scalars())

    # instances that have users which are members of this community. (excluding the current instance)
    def following_instances(self, include_dormant=False) -> List[Instance]:
        instances = Instance.query.join(User, User.instance_id == Instance.id).join(FeedMember,
                                                                                    FeedMember.user_id == User.id)
        instances = instances.filter(FeedMember.community_id == self.id, FeedMember.is_banned == False)
        if not include_dormant:
            instances = instances.filter(Instance.dormant == False)
        instances = instances.filter(Instance.id != 1, Instance.gone_forever == False)
        return instances.all()

    def has_followers_from_domain(self, domain: str) -> bool:
        instances = Instance.query.join(User, User.instance_id == Instance.id).join(FeedMember, FeedMember.user_id == User.id)
        instances = instances.filter(FeedMember.community_id == self.id, FeedMember.is_banned == False)
        for instance in instances:
            if instance.domain == domain:
                return True
        return False


class FeedJoinRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(UUID(as_uuid=True), index=True, default=uuid.uuid4)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    feed_id = db.Column(db.Integer, db.ForeignKey('feed.id'), index=True)


class CommunityFlair(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    flair = db.Column(db.String(50), index=True)
    text_color = db.Column(db.String(50))
    background_color = db.Column(db.String(50))
    blur_images = db.Column(db.Boolean, default=False)
    ap_id = db.Column(db.String(255), index=True, unique=True)

    def get_ap_id(self):
        if self.ap_id:
            return self.ap_id

        community = db.session.query(Community).get(self.community_id)

        self.ap_id = community.local_url() + f"/tag/{self.id}"
        db.session.commit()
        return self.ap_id


class UserFlair(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    flair = db.Column(db.String(50), index=True)


class SendQueue(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    destination_domain = db.Column(db.String(255), index=True)
    destination = db.Column(db.String(1024))
    actor = db.Column(db.String(255), index=True)
    private_key = db.Column(db.String(2000))
    payload = db.Column(db.Text)
    retries = db.Column(db.Integer, default=0)
    max_retries = db.Column(db.Integer, default=40)
    retry_reason = db.Column(db.String(255))
    created = db.Column(db.DateTime, default=utcnow)
    send_after = db.Column(db.DateTime, default=utcnow, index=True)


class ActivityBatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)    # where the activity will be sent to
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)  # which community the activity came from
    source_type = db.Column(db.Integer, index=True)       # the type of vote that caused this. PostVote, PostReplyVote
    source_id = db.Column(db.Integer, index=True)         # the ID of the vote that caused this. When undo-ing a vote, look it up by source_id and delete it from this table (before the batch is sent). If not found, federate the undo.
    payload = db.Column(db.JSON)
    created = db.Column(db.DateTime, default=utcnow)


class BlockedImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    file_name = db.Column(db.String(255), index=True)
    note = db.Column(db.String(255))
    hash = db.Column(BIT(256), index=True)


class CmsPage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(100), index=True)
    title = db.Column(db.String(255))
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    last_edited_by = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=utcnow)
    edited_at = db.Column(db.DateTime, default=utcnow)


class InstanceChooser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(100), index=True)
    language_id = db.Column(db.Integer, index=True)
    nsfw = db.Column(db.Boolean, default=False, index=True)
    newbie_friendly = db.Column(db.Boolean, default=True, index=True)
    hide = db.Column(db.Boolean, index=True, default=False)
    data = db.Column(db.JSON)


class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    remind_at = db.Column(db.DateTime)
    reminder_type = db.Column(db.Integer)
    reminder_destination = db.Column(db.Integer)


class Emoji(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(1024))
    token = db.Column(db.String(20), index=True)
    category = db.Column(db.String(20))
    aliases = db.Column(db.String(100), index=True)
    instance_id = db.Column(db.Integer, index=True)


def _large_community_subscribers() -> float:
    # average number of subscribers in the top 15% communities

    result = cache.get('large_community_subscribers')
    if result is None:
        sql = '''   SELECT AVG(subscriptions_count) AS avg_top_25
                    FROM (
                        SELECT subscriptions_count,
                               PERCENT_RANK() OVER (ORDER BY subscriptions_count DESC) AS percentile
                        FROM "community"
                        WHERE banned IS false and subscriptions_count > 0
                    ) AS ranked
                    WHERE percentile <= 0.15;'''
        result = db.session.execute(text(sql)).scalar()
        cache.set('large_community_subscribers', result, timeout=3600)
    return result


def _store_files_in_s3():
    return current_app.config['S3_ACCESS_KEY'] != '' and current_app.config['S3_ACCESS_SECRET'] != '' and \
        current_app.config['S3_ENDPOINT'] != ''
