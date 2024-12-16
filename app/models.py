import html
from datetime import datetime, timedelta, date, timezone
from time import time
from typing import List, Union, Type
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import arrow
from flask import current_app, escape, url_for, render_template_string
from flask_login import UserMixin, current_user
from sqlalchemy import or_, text, desc
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from flask_babel import _, lazy_gettext as _l
from sqlalchemy.orm import backref
from sqlalchemy_utils.types import TSVectorType # https://sqlalchemy-searchable.readthedocs.io/en/latest/installation.html
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.mutable import MutableList
from flask_sqlalchemy import BaseQuery
from sqlalchemy_searchable import SearchQueryMixin
from app import db, login, cache, celery, httpx_client, constants
import jwt
import os
import math

from app.constants import SUBSCRIPTION_NONMEMBER, SUBSCRIPTION_MEMBER, SUBSCRIPTION_MODERATOR, SUBSCRIPTION_OWNER, \
    SUBSCRIPTION_BANNED, SUBSCRIPTION_PENDING, NOTIF_USER, NOTIF_COMMUNITY, NOTIF_TOPIC, NOTIF_POST, NOTIF_REPLY, \
    ROLE_ADMIN, ROLE_STAFF


# datetime.utcnow() is depreciated in Python 3.12 so it will need to be swapped out eventually
def utcnow():
    return datetime.utcnow()


class FullTextSearchQuery(BaseQuery, SearchQueryMixin):
    pass


class BannedInstances(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True)
    reason = db.Column(db.String(256))
    initiator = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=utcnow)


class AllowedInstances(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class Instance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(256), index=True, unique=True)
    inbox = db.Column(db.String(256))
    shared_inbox = db.Column(db.String(256))
    outbox = db.Column(db.String(256))
    vote_weight = db.Column(db.Float, default=1.0)
    software = db.Column(db.String(50))
    version = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime, default=utcnow)      # When an Activity was received from them
    last_successful_send = db.Column(db.DateTime)           # When we successfully sent them an Activity
    failures = db.Column(db.Integer, default=0)             # How many times we failed to send (reset to 0 after every successful send)
    most_recent_attempt = db.Column(db.DateTime)            # When the most recent failure was
    dormant = db.Column(db.Boolean, default=False)          # True once this instance is considered offline and not worth sending to any more
    start_trying_again = db.Column(db.DateTime)             # When to start trying again. Should grow exponentially with each failure.
    gone_forever = db.Column(db.Boolean, default=False)     # True once this instance is considered offline forever - never start trying again
    ip_address = db.Column(db.String(50))
    trusted = db.Column(db.Boolean, default=False)
    posting_warning = db.Column(db.String(512))
    nodeinfo_href = db.Column(db.String(100))

    posts = db.relationship('Post', backref='instance', lazy='dynamic')
    post_replies = db.relationship('PostReply', backref='instance', lazy='dynamic')
    communities = db.relationship('Community', backref='instance', lazy='dynamic')

    def online(self):
        return not (self.dormant or self.gone_forever)

    def user_is_admin(self, user_id):
        role = InstanceRole.query.filter_by(instance_id=self.id, user_id=user_id).first()
        return role and role.role == 'admin'

    def votes_are_public(self):
        if self.trusted is True:    # only vote privately with untrusted instances
            return False
        return self.software.lower() == 'lemmy' or self.software.lower() == 'mbin' or self.software.lower() == 'kbin' or self.software.lower() == 'guppe groups'

    def post_count(self):
        return db.session.execute(text('SELECT COUNT(id) as c FROM "post" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def post_replies_count(self):
        return db.session.execute(text('SELECT COUNT(id) as c FROM "post_reply" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def known_communities_count(self):
        return db.session.execute(text('SELECT COUNT(id) as c FROM "community" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def known_users_count(self):
        return db.session.execute(text('SELECT COUNT(id) as c FROM "user" WHERE instance_id = :instance_id'),
                                  {'instance_id': self.id}).scalar()

    def update_dormant_gone(self):
        if self.failures > 7 and self.dormant == True:
            self.gone_forever = True
        elif self.failures > 2 and self.dormant == False:
            self.dormant = True

    @classmethod
    def weight(cls, domain: str):
        if domain:
            instance = Instance.query.filter_by(domain=domain).first()
            if instance:
                return instance.vote_weight
        return 1.0

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
        return Conversation.query.get(ec[0]) if ec else None


conversation_member = db.Table('conversation_member',
                               db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
                               db.Column('conversation_id', db.Integer, db.ForeignKey('conversation.id')),
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

    sender = db.relationship('User', foreign_keys=[sender_id])


class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), index=True)    # lowercase version of tag, e.g. solarstorm
    display_as = db.Column(db.String(256))          # Version of tag with uppercase letters, e.g. SolarStorm
    post_count = db.Column(db.Integer, default=0)
    banned = db.Column(db.Boolean, default=False, index=True)


class Licence(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))


class Language(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(5), index=True)
    name = db.Column(db.String(50))


community_language = db.Table('community_language', db.Column('community_id', db.Integer, db.ForeignKey('community.id')),
                                          db.Column('language_id', db.Integer, db.ForeignKey('language.id')),
                                          db.PrimaryKeyConstraint('community_id', 'language_id')
                        )

post_tag = db.Table('post_tag', db.Column('post_id', db.Integer, db.ForeignKey('post.id')),
                                          db.Column('tag_id', db.Integer, db.ForeignKey('tag.id')),
                                          db.PrimaryKeyConstraint('post_id', 'tag_id')
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

    def view_url(self, resize=False):
        if self.source_url:
            if resize and '/pictrs/' in self.source_url and '?' not in self.source_url:
                return f'{self.source_url}?thumbnail=1024'
            else:
                return self.source_url
        elif self.file_path:
            file_path = self.file_path[4:] if self.file_path.startswith('app/') else self.file_path
            scheme = 'http' if current_app.config['SERVER_NAME'] == '127.0.0.1:5000' else 'https'
            return f"{scheme}://{current_app.config['SERVER_NAME']}/{file_path}"
        else:
            return ''

    def medium_url(self):
        if self.file_path is None:
            return self.thumbnail_url()
        file_path = self.file_path[4:] if self.file_path.startswith('app/') else self.file_path
        scheme = 'http' if current_app.config['SERVER_NAME'] == '127.0.0.1:5000' else 'https'
        return f"{scheme}://{current_app.config['SERVER_NAME']}/{file_path}"

    def thumbnail_url(self):
        if self.thumbnail_path is None:
            if self.source_url:
                return self.source_url
            else:
                return ''
        thumbnail_path = self.thumbnail_path[4:] if self.thumbnail_path.startswith('app/') else self.thumbnail_path
        scheme = 'http' if current_app.config['SERVER_NAME'] == '127.0.0.1:5000' else 'https'
        return f"{scheme}://{current_app.config['SERVER_NAME']}/{thumbnail_path}"

    def delete_from_disk(self):
        purge_from_cache = []
        if self.file_path and os.path.isfile(self.file_path):
            try:
                os.unlink(self.file_path)
            except FileNotFoundError as e:
                ...
            purge_from_cache.append(self.file_path.replace('app/', f"https://{current_app.config['SERVER_NAME']}/"))
        if self.thumbnail_path and os.path.isfile(self.thumbnail_path):
            try:
                os.unlink(self.thumbnail_path)
            except FileNotFoundError as e:
                ...
            purge_from_cache.append(self.thumbnail_path.replace('app/', f"https://{current_app.config['SERVER_NAME']}/"))
        if self.source_url and self.source_url.startswith('http') and current_app.config['SERVER_NAME'] in self.source_url:
            # self.source_url is always a url rather than a file path, which makes deleting the file a bit fiddly
            try:
                os.unlink(self.source_url.replace(f"https://{current_app.config['SERVER_NAME']}/", 'app/'))
            except FileNotFoundError as e:
                ...
            purge_from_cache.append(self.source_url) # otoh it makes purging the cdn cache super easy.

        if purge_from_cache:
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
    zone_id = current_app.config['CLOUDFLARE_ZONE_ID']
    token = current_app.config['CLOUDFLARE_API_TOKEN']
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
        response = httpx_client.request(
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
    description = db.Column(db.Text)        # markdown
    description_html = db.Column(db.Text)   # html equivalent of above markdown
    rules = db.Column(db.Text)
    rules_html = db.Column(db.Text)
    content_warning = db.Column(db.Text)        # "Are you sure you want to view this community?"
    subscriptions_count = db.Column(db.Integer, default=0)
    post_count = db.Column(db.Integer, default=0)
    post_reply_count = db.Column(db.Integer, default=0)
    nsfw = db.Column(db.Boolean, default=False)
    nsfl = db.Column(db.Boolean, default=False)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    low_quality = db.Column(db.Boolean, default=False)      # upvotes earned in low quality communities don't improve reputation
    created_at = db.Column(db.DateTime, default=utcnow)
    last_active = db.Column(db.DateTime, default=utcnow)
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    content_retention = db.Column(db.Integer, default=-1)
    topic_id = db.Column(db.Integer, db.ForeignKey('topic.id'), index=True)
    default_layout = db.Column(db.String(15))
    posting_warning = db.Column(db.String(512))

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
    local_only = db.Column(db.Boolean, default=False)       # only users on this instance can post
    new_mods_wanted = db.Column(db.Boolean, default=False)
    searchable = db.Column(db.Boolean, default=True)
    private_mods = db.Column(db.Boolean, default=False)

    # Which feeds posts from this community show up in
    show_popular = db.Column(db.Boolean, default=True)
    show_all = db.Column(db.Boolean, default=True)

    ignore_remote_language = db.Column(db.Boolean, default=False)

    search_vector = db.Column(TSVectorType('name', 'title', 'description', 'rules'))

    posts = db.relationship('Post', lazy='dynamic', cascade="all, delete-orphan")
    replies = db.relationship('PostReply', lazy='dynamic', cascade="all, delete-orphan")
    wiki_pages = db.relationship('CommunityWikiPage', lazy='dynamic', backref='community', cascade="all, delete-orphan")
    icon = db.relationship('File', foreign_keys=[icon_id], single_parent=True, backref='community', cascade="all, delete-orphan")
    image = db.relationship('File', foreign_keys=[image_id], single_parent=True, cascade="all, delete-orphan")
    languages = db.relationship('Language', lazy='dynamic', secondary=community_language, backref=db.backref('communities', lazy='dynamic'))

    def language_ids(self):
        return [language.id for language in self.languages.all()]

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

    @cache.memoize(timeout=3)
    def moderators(self):
        return CommunityMember.query.filter((CommunityMember.community_id == self.id) &
                                     (or_(
                                         CommunityMember.is_owner,
                                         CommunityMember.is_moderator
                                     ))
                                     ).filter(CommunityMember.is_banned == False).all()

    def is_member(self, user):
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
            return any(moderator.user_id == current_user.get_id() and moderator.is_owner for moderator in self.moderators())
        else:
            return any(moderator.user_id == user.id and moderator.is_owner for moderator in self.moderators())

    def is_instance_admin(self, user):
        if self.instance_id:
            instance_role = InstanceRole.query.filter(InstanceRole.instance_id == self.instance_id,
                                                      InstanceRole.user_id == user.id,
                                                      InstanceRole.role == 'admin').first()
            return instance_role is not None
        else:
            return False

    def user_is_banned(self, user):
        # use communities_banned_from() instead of this method, where possible. Redis caches the result of communities_banned_from()
        # we cannot use communities_banned_from() in models.py because it causes a circular import
        community_bans = CommunityBan.query.filter(CommunityBan.user_id == user.id).all()
        return self.id in [cb.community_id for cb in community_bans]

    def profile_id(self):
        retval = self.ap_profile_id if self.ap_profile_id else f"https://{current_app.config['SERVER_NAME']}/c/{self.name}"
        return retval.lower()

    def public_url(self):
        result = self.ap_public_url if self.ap_public_url else f"https://{current_app.config['SERVER_NAME']}/c/{self.name}"
        return result

    def is_local(self):
        return self.ap_id is None or self.profile_id().startswith('https://' + current_app.config['SERVER_NAME'])

    def local_url(self):
        if self.is_local():
            return self.ap_profile_id
        else:
            return f"https://{current_app.config['SERVER_NAME']}/c/{self.ap_id}"

    def notify_new_posts(self, user_id: int) -> bool:
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == self.id,
                                                                      NotificationSubscription.user_id == user_id,
                                                                      NotificationSubscription.type == NOTIF_COMMUNITY).first()
        return existing_notification is not None

    # ids of all the users who want to be notified when there is a post in this community
    def notification_subscribers(self):
        return list(db.session.execute(text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :community_id AND type = :type '),
                                  {'community_id': self.id, 'type': NOTIF_COMMUNITY}).scalars())

    # instances that have users which are members of this community. (excluding the current instance)
    def following_instances(self, include_dormant=False) -> List[Instance]:
        instances = Instance.query.join(User, User.instance_id == Instance.id).join(CommunityMember, CommunityMember.user_id == User.id)
        instances = instances.filter(CommunityMember.community_id == self.id, CommunityMember.is_banned == False)
        if not include_dormant:
            instances = instances.filter(Instance.dormant == False)
        instances = instances.filter(Instance.id != 1, Instance.gone_forever == False)
        return instances.all()

    def has_followers_from_domain(self, domain: str) -> bool:
        instances = Instance.query.join(User, User.instance_id == Instance.id).join(CommunityMember, CommunityMember.user_id == User.id)
        instances = instances.filter(CommunityMember.community_id == self.id, CommunityMember.is_banned == False)
        for instance in instances:
            if instance.domain == domain:
                return True
        return False

    def loop_videos(self) -> bool:
        return 'gifs' in self.name

    def delete_dependencies(self):
        for post in self.posts:
            post.delete_dependencies()
            db.session.delete(post)
        db.session.query(CommunityBan).filter(CommunityBan.community_id == self.id).delete()
        db.session.query(CommunityBlock).filter(CommunityBlock.community_id == self.id).delete()
        db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.community_id == self.id).delete()
        db.session.query(CommunityMember).filter(CommunityMember.community_id == self.id).delete()
        db.session.query(Report).filter(Report.suspect_community_id == self.id).delete()
        db.session.query(ModLog).filter(ModLog.community_id == self.id).delete()


user_role = db.Table('user_role',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
    db.Column('role_id', db.Integer, db.ForeignKey('role.id')),
    db.PrimaryKeyConstraint('user_id', 'role_id')
)

# table to hold users' 'read' post ids
read_posts = db.Table('read_posts',
                        db.Column('user_id', db.Integer, db.ForeignKey('user.id'), index=True),
                        db.Column('read_post_id', db.Integer, db.ForeignKey('post.id'), index=True),
                        db.Column('interacted_at', db.DateTime, index=True, default=utcnow)    # this is when the content is interacted with
                      )


class User(UserMixin, db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_name = db.Column(db.String(255), index=True)
    alt_user_name = db.Column(db.String(255), index=True)
    title = db.Column(db.String(256))
    email = db.Column(db.String(255), index=True)
    password_hash = db.Column(db.String(128))
    verified = db.Column(db.Boolean, default=False)
    verification_token = db.Column(db.String(16), index=True)
    banned = db.Column(db.Boolean, default=False, index=True)
    banned_until = db.Column(db.DateTime)   # null == permanent ban
    ban_posts = db.Column(db.Boolean, default=False)
    ban_comments = db.Column(db.Boolean, default=False)
    deleted = db.Column(db.Boolean, default=False)
    deleted_by = db.Column(db.Integer, index=True)
    about = db.Column(db.Text)      # markdown
    about_html = db.Column(db.Text) # html
    keywords = db.Column(db.String(256))
    matrix_user_id = db.Column(db.String(256))
    hide_nsfw = db.Column(db.Integer, default=1)
    hide_nsfl = db.Column(db.Integer, default=1)
    created = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime, default=utcnow, index=True)
    avatar_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    cover_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    newsletter = db.Column(db.Boolean, default=True)
    email_unread = db.Column(db.Boolean, default=True)          # True if they want to receive 'unread notifications' emails
    email_unread_sent = db.Column(db.Boolean)                   # True after a 'unread notifications' email has been sent. None for remote users
    receive_message_mode = db.Column(db.String(20), default='Closed')  # possible values: Open, TrustedOnly, Closed
    bounces = db.Column(db.SmallInteger, default=0)
    timezone = db.Column(db.String(20))
    reputation = db.Column(db.Float, default=0.0)
    attitude = db.Column(db.Float, default=1.0)  # (upvotes cast - downvotes cast) / (upvotes + downvotes). A number between 1 and -1 is the ratio between up and down votes they cast
    post_count = db.Column(db.Integer, default=0)
    post_reply_count = db.Column(db.Integer, default=0)
    stripe_customer_id = db.Column(db.String(50))
    stripe_subscription_id = db.Column(db.String(50))
    searchable = db.Column(db.Boolean, default=True)
    indexable = db.Column(db.Boolean, default=False)
    bot = db.Column(db.Boolean, default=False)
    ignore_bots = db.Column(db.Integer, default=0)
    unread_notifications = db.Column(db.Integer, default=0)
    ip_address = db.Column(db.String(50))
    ip_address_country = db.Column(db.String(50))
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    reports = db.Column(db.Integer, default=0)  # how many times this user has been reported.
    default_sort = db.Column(db.String(25), default='hot')
    default_filter = db.Column(db.String(25), default='subscribed')
    theme = db.Column(db.String(20), default='')
    referrer = db.Column(db.String(256))
    markdown_editor = db.Column(db.Boolean, default=False)
    interface_language = db.Column(db.String(10))           # a locale that the translation system understands e.g. 'en' or 'en-us'. If empty, use browser default
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'))   # the default choice in the language dropdown when composing posts & comments
    reply_collapse_threshold = db.Column(db.Integer, default=-10)
    reply_hide_threshold = db.Column(db.Integer, default=-20)

    avatar = db.relationship('File', lazy='joined', foreign_keys=[avatar_id], single_parent=True, cascade="all, delete-orphan")
    cover = db.relationship('File', lazy='joined', foreign_keys=[cover_id], single_parent=True, cascade="all, delete-orphan")
    instance = db.relationship('Instance', lazy='joined', foreign_keys=[instance_id])
    conversations = db.relationship('Conversation', lazy='dynamic', secondary=conversation_member, backref=db.backref('members', lazy='joined'))

    ap_id = db.Column(db.String(255), index=True)           # e.g. username@server
    ap_profile_id = db.Column(db.String(255), index=True, unique=True)   # e.g. https://server/u/username
    ap_public_url = db.Column(db.String(255))               # e.g. https://server/u/UserName
    ap_fetched_at = db.Column(db.DateTime)
    ap_followers_url = db.Column(db.String(255))
    ap_preferred_username = db.Column(db.String(255))
    ap_manually_approves_followers = db.Column(db.Boolean, default=False)
    ap_deleted_at = db.Column(db.DateTime)
    ap_inbox_url = db.Column(db.String(255))
    ap_domain = db.Column(db.String(255))

    search_vector = db.Column(TSVectorType('user_name', 'about', 'keywords'))
    activity = db.relationship('ActivityLog', backref='account', lazy='dynamic', cascade="all, delete-orphan")
    posts = db.relationship('Post', lazy='dynamic', cascade="all, delete-orphan")
    post_replies = db.relationship('PostReply', lazy='dynamic', cascade="all, delete-orphan")

    roles = db.relationship('Role', secondary=user_role, lazy='dynamic', cascade="all, delete")

    hide_read_posts = db.Column(db.Boolean, default=False)
    # db relationship tracked by the "read_posts" table
    # this is the User side, so its referencing the Post side
    # read_by is the corresponding Post object variable
    read_post = db.relationship('Post', secondary=read_posts, back_populates='read_by', lazy='dynamic')

    def __repr__(self):
        return '<User {}_{}>'.format(self.user_name, self.id)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        try:
            result = check_password_hash(self.password_hash, password)
            return result
        except Exception:
            return False

    def get_id(self):
        if self.is_authenticated:
            return self.id
        else:
            return 0

    def display_name(self):
        if self.deleted is False:
            if self.title:
                return self.title.strip()
            else:
                return self.user_name.strip()
        else:
            return '[deleted]'

    @cache.memoize(timeout=500)
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

    @cache.memoize(timeout=500)
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

    def vote_privately(self):
        return self.alt_user_name is not None and self.alt_user_name != ''

    def num_content(self):
        content = 0
        content += db.session.execute(text('SELECT COUNT(id) as c FROM "post" WHERE user_id = :user_id'), {'user_id': self.id}).scalar()
        content += db.session.execute(text('SELECT COUNT(id) as c FROM "post_reply" WHERE user_id = :user_id'), {'user_id': self.id}).scalar()
        return content

    def is_local(self):
        return self.ap_id is None or self.ap_profile_id.startswith('https://' + current_app.config['SERVER_NAME'])

    def waiting_for_approval(self):
        application = UserRegistration.query.filter_by(user_id=self.id, status=0).first()
        return application is not None

    @cache.memoize(timeout=30)
    def is_admin(self):
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
        if self.created_recently() or self.reputation < 100:
            return False
        return True

    def cannot_vote(self):
        if self.is_local():
            return False
        return self.post_count == 0 and self.post_reply_count == 0 and len(self.user_name) == 8  # most vote manipulation bots have 8 character user names and never post any content

    def link(self) -> str:
        if self.is_local():
            return self.user_name
        else:
            return self.ap_id

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
        upvotes = downvotes = 0
        with db.session.no_autoflush:  # Avoid StaleDataError exception
            last_50_votes = PostVote.query.filter(PostVote.user_id == self.id).order_by(-PostVote.id).limit(50)
            for vote in last_50_votes:
                if vote.effect > 0:
                    upvotes += 1
                if vote.effect < 0:
                    downvotes += 1

            comment_upvotes = comment_downvotes = 0
            last_50_votes = PostReplyVote.query.filter(PostReplyVote.user_id == self.id).order_by(-PostReplyVote.id).limit(50)
            for vote in last_50_votes:
                if vote.effect > 0:
                    comment_upvotes += 1
                if vote.effect < 0:
                    comment_downvotes += 1

        total_upvotes = upvotes + comment_upvotes
        total_downvotes = downvotes + comment_downvotes

        if total_downvotes == 0:    # guard against division by zero
            self.attitude = 1.0
        else:
            if total_upvotes + total_downvotes > 2:  # Only calculate attitude if they've done 3 or more votes as anything less than this could be an outlier and not representative of their overall attitude
                self.attitude = (total_upvotes - total_downvotes) / (total_upvotes + total_downvotes)
            else:
                self.attitude = 1.0

    def recalculate_post_stats(self, posts=True, replies=True):
        if posts:
            self.post_count = db.session.execute(text('SELECT COUNT(id) as c FROM "post" WHERE user_id = :user_id AND deleted = false'),
                                                 {'user_id': self.id}).scalar()
        if replies:
            self.post_reply_count = db.session.execute(text('SELECT COUNT(id) as c FROM "post_reply" WHERE user_id = :user_id AND deleted = false'),
                                                       {'user_id': self.id}).scalar()

    def subscribed(self, community_id: int) -> int:
        if community_id is None:
            return False
        subscription:CommunityMember = CommunityMember.query.filter_by(user_id=self.id, community_id=community_id).first()
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
        return Community.query.filter(Community.banned == False).\
            join(CommunityMember).filter(CommunityMember.is_banned == False, CommunityMember.user_id == self.id).all()

    def profile_id(self):
        result = self.ap_profile_id if self.ap_profile_id else f"https://{current_app.config['SERVER_NAME']}/u/{self.user_name.lower()}"
        return result

    def public_url(self, main_user_name=True):
        if main_user_name:
            result = self.ap_public_url if self.ap_public_url else f"https://{current_app.config['SERVER_NAME']}/u/{self.user_name}"
        else:
            result = f"https://{current_app.config['SERVER_NAME']}/u/{self.alt_user_name}"
        return result

    def created_recently(self):
        if self.is_admin():
            return False
        return self.created and self.created > utcnow() - timedelta(days=7)

    def has_blocked_instance(self, instance_id: int):
        instance_block = InstanceBlock.query.filter_by(user_id=self.id, instance_id=instance_id).first()
        return instance_block is not None

    def has_blocked_user(self, user_id: int):
        existing_block = UserBlock.query.filter_by(blocker_id=self.id, blocked_id=user_id).first()
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
            file = File.query.get(self.cover_id)
            file.delete_from_disk()
            self.cover_id = None
            db.session.delete(file)
        if self.avatar_id:
            file = File.query.get(self.avatar_id)
            file.delete_from_disk()
            self.avatar_id = None
            db.session.delete(file)
        if self.waiting_for_approval():
            db.session.query(UserRegistration).filter(UserRegistration.user_id == self.id).delete()
        db.session.query(NotificationSubscription).filter(NotificationSubscription.user_id == self.id).delete()
        db.session.query(Notification).filter(Notification.user_id == self.id).delete()
        db.session.query(PollChoiceVote).filter(PollChoiceVote.user_id == self.id).delete()
        db.session.query(PostBookmark).filter(PostBookmark.user_id == self.id).delete()
        db.session.query(PostReplyBookmark).filter(PostReplyBookmark.user_id == self.id).delete()
        db.session.query(ModLog).filter(ModLog.user_id == self.id).delete()

    def purge_content(self, soft=True):
        files = File.query.join(Post).filter(Post.user_id == self.id).all()
        for file in files:
            file.delete_from_disk()
        self.delete_dependencies()
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
        return list(db.session.execute(text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :user_id AND type = :type '),
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
    reply_count = db.Column(db.Integer, default=0)
    score = db.Column(db.Integer, default=0, index=True)                # used for 'top' ranking
    nsfw = db.Column(db.Boolean, default=False, index=True)
    nsfl = db.Column(db.Boolean, default=False, index=True)
    sticky = db.Column(db.Boolean, default=False)
    notify_author = db.Column(db.Boolean, default=True)
    indexable = db.Column(db.Boolean, default=True)
    from_bot = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, index=True, default=utcnow)    # this is when the content arrived here
    posted_at = db.Column(db.DateTime, index=True, default=utcnow)     # this is when the original server created it
    last_active = db.Column(db.DateTime, index=True, default=utcnow)
    ip = db.Column(db.String(50))
    up_votes = db.Column(db.Integer, default=0)
    down_votes = db.Column(db.Integer, default=0)
    ranking = db.Column(db.Integer, default=0, index=True)                          # used for 'hot' ranking
    edited_at = db.Column(db.DateTime)
    reports = db.Column(db.Integer, default=0)                          # how many times this post has been reported. Set to -1 to ignore reports
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'), index=True)
    cross_posts = db.Column(MutableList.as_mutable(ARRAY(db.Integer)))
    tags = db.relationship('Tag', lazy='dynamic', secondary=post_tag, backref=db.backref('posts', lazy='dynamic'))

    ap_id = db.Column(db.String(255), index=True, unique=True)
    ap_create_id = db.Column(db.String(100))
    ap_announce_id = db.Column(db.String(100))

    search_vector = db.Column(TSVectorType('title', 'body'))

    image = db.relationship(File, lazy='joined', foreign_keys=[image_id], cascade="all, delete")
    domain = db.relationship('Domain', lazy='joined', foreign_keys=[domain_id])
    author = db.relationship('User', lazy='joined', overlaps='posts', foreign_keys=[user_id])
    community = db.relationship('Community', lazy='joined', overlaps='posts', foreign_keys=[community_id])
    replies = db.relationship('PostReply', lazy='dynamic', backref='post')
    language = db.relationship('Language', foreign_keys=[language_id])
    licence = db.relationship('Licence', foreign_keys=[licence_id])

    # db relationship tracked by the "read_posts" table
    # this is the Post side, so its referencing the User side
    # read_post is the corresponding User object variable
    read_by = db.relationship('User', secondary=read_posts, back_populates='read_post', lazy='dynamic')

    def is_local(self):
        return self.ap_id is None or self.ap_id.startswith('https://' + current_app.config['SERVER_NAME'])

    @classmethod
    def get_by_ap_id(cls, ap_id):
        return cls.query.filter_by(ap_id=ap_id.lower()).first()

    @classmethod
    def new(cls, user: User, community: Community, request_json: dict, announce_id=None):
        from app.activitypub.util import instance_weight, find_language_or_create, find_language, find_hashtag_or_create, \
            find_licence_or_create, make_image_sizes, notify_about_post
        from app.utils import allowlist_html, markdown_to_html, html_to_text, microblog_content_to_title, blocked_phrases, \
            is_image_url, is_video_url, domain_from_url, opengraph_parse, shorten_string, remove_tracking_from_link, \
            is_video_hosting_site, communities_banned_from

        microblog = False
        if 'name' not in request_json['object']:  # Microblog posts
            if 'content' in request_json['object'] and request_json['object']['content'] is not None:
                title = "[Microblog]"
                microblog = True
            else:
                return None
        else:
            title = request_json['object']['name'].strip()
        nsfl_in_title = '[NSFL]' in title.upper() or '(NSFL)' in title.upper()
        post = Post(user_id=user.id, community_id=community.id,
                    title=html.unescape(title),
                    comments_enabled=request_json['object']['commentsEnabled'] if 'commentsEnabled' in request_json['object'] else True,
                    sticky=request_json['object']['stickied'] if 'stickied' in request_json['object'] else False,
                    nsfw=request_json['object']['sensitive'] if 'sensitive' in request_json['object'] else False,
                    nsfl=request_json['object']['nsfl'] if 'nsfl' in request_json['object'] else nsfl_in_title,
                    ap_id=request_json['object']['id'].lower(),
                    ap_create_id=request_json['id'],
                    ap_announce_id=announce_id,
                    up_votes=1,
                    from_bot=user.bot,
                    score=instance_weight(user.ap_domain),
                    instance_id=user.instance_id,
                    indexable=user.indexable,
                    microblog=microblog,
                    posted_at=utcnow()
                    )

        if 'content' in request_json['object'] and request_json['object']['content'] is not None:
            if 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/html':
                post.body_html = allowlist_html(request_json['object']['content'])
                if 'source' in request_json['object'] and isinstance(request_json['object']['source'], dict) and \
                        request_json['object']['source']['mediaType'] == 'text/markdown':
                    post.body = request_json['object']['source']['content']
                    post.body_html = markdown_to_html(post.body)  # prefer Markdown if provided, overwrite version obtained from HTML
                else:
                    post.body = html_to_text(post.body_html)
            elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/markdown':
                post.body = request_json['object']['content']
                post.body_html = markdown_to_html(post.body)
            else:
                if not (request_json['object']['content'].startswith('<p>') or request_json['object']['content'].startswith('<blockquote>')):
                    request_json['object']['content'] = '<p>' + request_json['object']['content'] + '</p>'
                post.body_html = allowlist_html(request_json['object']['content'])
                post.body = html_to_text(post.body_html)
            if microblog:
                autogenerated_title = microblog_content_to_title(post.body_html)
                if len(autogenerated_title) < 20:
                    title = '[Microblog] ' + autogenerated_title.strip()
                else:
                    title = autogenerated_title.strip()
                if '[NSFL]' in title.upper() or '(NSFL)' in title.upper():
                    post.nsfl = True
                if '[NSFW]' in title.upper() or '(NSFW)' in title.upper():
                    post.nsfw = True
                post.title = title
        # Discard post if it contains certain phrases. Good for stopping spam floods.
        blocked_phrases_list = blocked_phrases()
        for blocked_phrase in blocked_phrases_list:
            if blocked_phrase in post.title:
                return None
        if post.body:
            for blocked_phrase in blocked_phrases_list:
                if blocked_phrase in post.body:
                    return None

        if ('attachment' in request_json['object'] and
            isinstance(request_json['object']['attachment'], list) and
            len(request_json['object']['attachment']) > 0 and
            'type' in request_json['object']['attachment'][0]):
            alt_text = None
            if request_json['object']['attachment'][0]['type'] == 'Link':
                post.url = request_json['object']['attachment'][0]['href']  # Lemmy < 0.19.4
            if request_json['object']['attachment'][0]['type'] == 'Document':
                post.url = request_json['object']['attachment'][0]['url']  # Mastodon
                if 'name' in request_json['object']['attachment'][0]:
                    alt_text = request_json['object']['attachment'][0]['name']
            if request_json['object']['attachment'][0]['type'] == 'Image':
                post.url = request_json['object']['attachment'][0]['url']  # PixelFed, PieFed, Lemmy >= 0.19.4
                if 'name' in request_json['object']['attachment'][0]:
                    alt_text = request_json['object']['attachment'][0]['name']

        if 'attachment' in request_json['object'] and isinstance(request_json['object']['attachment'], dict):  # a.gup.pe (Mastodon)
            alt_text = None
            post.url = request_json['object']['attachment']['url']

        if post.url:
            if is_image_url(post.url):
                post.type = constants.POST_TYPE_IMAGE
                image = File(source_url=post.url)
                if alt_text:
                    image.alt_text = alt_text
                db.session.add(image)
                post.image = image
            elif is_video_url(post.url):  # youtube is detected later
                post.type = constants.POST_TYPE_VIDEO
                # custom thumbnails will be added below in the "if 'image' in request_json['object'] and post.image is None:" section
            else:
                post.type = constants.POST_TYPE_LINK
            domain = domain_from_url(post.url)
            # notify about links to banned websites.
            already_notified = set()  # often admins and mods are the same people - avoid notifying them twice
            if domain.notify_mods:
                for community_member in post.community.moderators():
                    notify = Notification(title='Suspicious content', url=post.ap_id,
                                          user_id=community_member.user_id,
                                          author_id=user.id)
                    db.session.add(notify)
                    already_notified.add(community_member.user_id)
            if domain.notify_admins:
                for admin in Site.admins():
                    if admin.id not in already_notified:
                        notify = Notification(title='Suspicious content',
                                              url=post.ap_id, user_id=admin.id,
                                              author_id=user.id)
                        db.session.add(notify)
            if domain.banned or domain.name.endswith('.pages.dev'):
                raise Exception(domain.name + ' is blocked by admin')
            else:
                domain.post_count += 1
                post.domain = domain

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
            if 'image' in request_json['object'] and post.image is None:
                image = File(source_url=request_json['object']['image']['url'])
                db.session.add(image)
                post.image = image
            if post.image is None and post.type == constants.POST_TYPE_LINK:  # This is a link post but the source instance has not provided a thumbnail image
                # Let's see if we can do better than the source instance did!
                tn_url = post.url
                if tn_url[:32] == 'https://www.youtube.com/watch?v=':
                    tn_url = 'https://youtu.be/' + tn_url[
                                                   32:43]  # better chance of thumbnail from youtu.be than youtube.com
                opengraph = opengraph_parse(tn_url)
                if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                    filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                    if not filename.startswith('/'):
                        file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                        post.image = file
                        db.session.add(file)

            if 'searchableBy' in request_json['object'] and request_json['object']['searchableBy'] != 'https://www.w3.org/ns/activitystreams#Public':
                post.indexable = False

            if post.url:
                post.url = remove_tracking_from_link(post.url)  # moved here as changes youtu.be to youtube.com
                if is_video_hosting_site(post.url):
                    post.type = constants.POST_TYPE_VIDEO
            db.session.add(post)
            post.ranking = post.post_ranking(post.score, post.posted_at)
            community.post_count += 1
            community.last_active = utcnow()
            user.post_count += 1
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return Post.query.filter_by(ap_id=request_json['object']['id'].lower()).one()

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

            if post.image_id and not post.type == constants.POST_TYPE_VIDEO:
                make_image_sizes(post.image_id, 170, 512, 'posts',
                                 community.low_quality)  # the 512 sized image is for masonry view

            # Update list of cross posts
            if post.url:
                other_posts = Post.query.filter(Post.id != post.id, Post.url == post.url, Post.deleted == False,
                                                Post.posted_at > post.posted_at - timedelta(days=6)).all()
                for op in other_posts:
                    if op.cross_posts is None:
                        op.cross_posts = [post.id]
                    else:
                        op.cross_posts.append(post.id)
                    if post.cross_posts is None:
                        post.cross_posts = [op.id]
                    else:
                        post.cross_posts.append(op.id)
                db.session.commit()

            if post.community_id not in communities_banned_from(user.id):
                notify_about_post(post)

            if user.reputation > 100:
                post.up_votes += 1
                post.score += 1
                post.ranking = post.post_ranking(post.score, post.posted_at)
                db.session.commit()

        return post

    # All the following post/comment ranking math is explained at https://medium.com/hacking-and-gonzo/how-reddit-ranking-algorithms-work-ef111e33d0d9
    epoch = datetime(1970, 1, 1)

    @classmethod
    def epoch_seconds(self, date):
        td = date - self.epoch
        return td.days * 86400 + td.seconds + (float(td.microseconds) / 1000000)

    def delete_dependencies(self):
        db.session.query(PostBookmark).filter(PostBookmark.post_id == self.id).delete()
        db.session.query(PollChoiceVote).filter(PollChoiceVote.post_id == self.id).delete()
        db.session.query(PollChoice).filter(PollChoice.post_id == self.id).delete()
        db.session.query(Poll).filter(Poll.post_id == self.id).delete()
        db.session.query(Report).filter(Report.suspect_post_id == self.id).delete()
        db.session.execute(text('DELETE FROM "post_vote" WHERE post_id = :post_id'), {'post_id': self.id})

        reply_ids = db.session.execute(text('SELECT id FROM "post_reply" WHERE post_id = :post_id'), {'post_id': self.id}).scalars()
        reply_ids = tuple(reply_ids)
        if reply_ids:
            db.session.execute(text('DELETE FROM "post_reply_vote" WHERE post_reply_id IN :reply_ids'), {'reply_ids': reply_ids})
            db.session.execute(text('DELETE FROM "post_reply_bookmark" WHERE post_reply_id IN :reply_ids'), {'reply_ids': reply_ids})
            db.session.execute(text('DELETE FROM "report" WHERE suspect_post_reply_id IN :reply_ids'), {'reply_ids': reply_ids})
            db.session.execute(text('DELETE FROM "post_reply" WHERE post_id = :post_id'), {'post_id': self.id})

            self.community.post_reply_count = db.session.execute(text('SELECT COUNT(id) as c FROM "post_reply" WHERE community_id = :community_id AND deleted = false'),
                                                                {'community_id': self.community_id}).scalar()

        if self.image_id:
            file = File.query.get(self.image_id)
            file.delete_from_disk()

    def youtube_embed(self, rel=True) -> str:
        if self.url:
            parsed_url = urlparse(self.url)
            query_params = parse_qs(parsed_url.query)

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

    def peertube_embed(self):
        if self.url:
            return self.url.replace('watch', 'embed')

    def profile_id(self):
        if self.ap_id:
            return self.ap_id
        else:
            return f"https://{current_app.config['SERVER_NAME']}/post/{self.id}"

    def public_url(self):
        return self.profile_id()

    def blocked_by_content_filter(self, content_filters):
        lowercase_title = self.title.lower()
        for name, keywords in content_filters.items() if content_filters else {}:
            for keyword in keywords:
                if keyword in lowercase_title:
                    return name
        return False

    def posted_at_localized(self, sort, locale):
        # some locales do not have a definition for 'weeks' so are unable to display some dates in some languages. Fall back to english for those languages.
        try:
            return arrow.get(self.last_active if sort == 'active' else self.posted_at).humanize(locale=locale)
        except ValueError as v:
            return arrow.get(self.last_active if sort == 'active' else self.posted_at).humanize(locale='en')

    def notify_new_replies(self, user_id: int) -> bool:
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == self.id,
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
        for tag in self.tags:
            return_value.append({'type': 'Hashtag',
                                 'href': f'https://{current_app.config["SERVER_NAME"]}/tag/{tag.name}',
                                 'name': f'#{tag.name}'})
        return return_value

    def post_reply_count_recalculate(self):
        self.post_reply_count = db.session.execute(text('SELECT COUNT(id) as c FROM "post_reply" WHERE post_id = :post_id AND deleted is false'),
            {'post_id': self.id}).scalar()

    # All the following post/comment ranking math is explained at https://medium.com/hacking-and-gonzo/how-reddit-ranking-algorithms-work-ef111e33d0d9
    epoch = datetime(1970, 1, 1)

    def epoch_seconds(self, date):
        td = date - self.epoch
        return td.days * 86400 + td.seconds + (float(td.microseconds) / 1000000)

    # All the following post/comment ranking math is explained at https://medium.com/hacking-and-gonzo/how-reddit-ranking-algorithms-work-ef111e33d0d9
    def post_ranking(self, score, date: datetime):
        if date is None:
            date = datetime.utcnow()
        if score is None:
            score = 1
        order = math.log(max(abs(score), 1), 10)
        sign = 1 if score > 0 else -1 if score < 0 else 0
        seconds = self.epoch_seconds(date) - 1685766018
        return round(sign * order + seconds / 45000, 7)

    def vote(self, user: User, vote_direction: str):
        existing_vote = PostVote.query.filter_by(user_id=user.id, post_id=self.id).first()
        if existing_vote and vote_direction == 'reversal':                            # api sends '1' for upvote, '-1' for downvote, and '0' for reversal
            if existing_vote.effect == 1:
                vote_direction = 'upvote'
            elif existing_vote.effect == -1:
                 vote_direction = 'downvote'
        assert vote_direction == 'upvote' or vote_direction == 'downvote'
        undo = None
        if existing_vote:
            if not self.community.low_quality:
                self.author.reputation -= existing_vote.effect
            if existing_vote.effect > 0:  # previous vote was up
                if vote_direction == 'upvote':  # new vote is also up, so remove it
                    db.session.delete(existing_vote)
                    self.up_votes -= 1
                    self.score -= existing_vote.effect              # score - (+1) = score-1
                    undo = 'Like'
                else:  # new vote is down while previous vote was up, so reverse their previous vote
                    existing_vote.effect = -1
                    self.up_votes -= 1
                    self.down_votes += 1
                    self.score += existing_vote.effect * 2          # score + (-2) = score-2
            else:  # previous vote was down
                if vote_direction == 'downvote':  # new vote is also down, so remove it
                    db.session.delete(existing_vote)
                    self.down_votes -= 1
                    self.score -= existing_vote.effect              # score - (-1) = score+1
                    undo = 'Dislike'
                else:  # new vote is up while previous vote was down, so reverse their previous vote
                    existing_vote.effect = 1
                    self.up_votes += 1
                    self.down_votes -= 1
                    self.score += existing_vote.effect * 2          # score + (+2) = score+2
            db.session.commit()
        else:
            if vote_direction == 'upvote':
                effect = Instance.weight(user.ap_domain)
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
                self.score += spicy_effect                      # score + (+1) = score+1
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
                self.score += spicy_effect                      # score + (-1) = score-1
            vote = PostVote(user_id=user.id, post_id=self.id, author_id=self.author.id,
                            effect=effect)
            # upvotes do not increase reputation in low quality communities
            if self.community.low_quality and effect > 0:
                effect = 0
            self.author.reputation += effect
            db.session.add(vote)

        user.last_seen = utcnow()
        db.session.commit()
        if not user.banned:
            self.ranking = self.post_ranking(self.score, self.created_at)
            user.recalculate_attitude()
            db.session.commit()
        return undo


class PostReply(db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), index=True)
    image_id = db.Column(db.Integer, db.ForeignKey('file.id'), index=True)
    parent_id = db.Column(db.Integer, index=True)
    root_id = db.Column(db.Integer)
    depth = db.Column(db.Integer, default=0)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    body_html_safe = db.Column(db.Boolean, default=False)
    score = db.Column(db.Integer, default=0, index=True)    # used for 'top' sorting
    nsfw = db.Column(db.Boolean, default=False)
    nsfl = db.Column(db.Boolean, default=False)
    notify_author = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, index=True, default=utcnow)
    posted_at = db.Column(db.DateTime, index=True, default=utcnow)
    deleted = db.Column(db.Boolean, default=False, index=True)
    deleted_by = db.Column(db.Integer, index=True)
    ip = db.Column(db.String(50))
    from_bot = db.Column(db.Boolean, default=False)
    up_votes = db.Column(db.Integer, default=0)
    down_votes = db.Column(db.Integer, default=0)
    ranking = db.Column(db.Float, default=0.0, index=True)  # used for 'hot' sorting
    language_id = db.Column(db.Integer, db.ForeignKey('language.id'), index=True)
    edited_at = db.Column(db.DateTime)
    reports = db.Column(db.Integer, default=0)  # how many times this post has been reported. Set to -1 to ignore reports

    ap_id = db.Column(db.String(255), index=True, unique=True)
    ap_create_id = db.Column(db.String(100))
    ap_announce_id = db.Column(db.String(100))

    search_vector = db.Column(TSVectorType('body'))

    author = db.relationship('User', lazy='joined', foreign_keys=[user_id], single_parent=True, overlaps="post_replies")
    community = db.relationship('Community', lazy='joined', overlaps='replies', foreign_keys=[community_id])
    language = db.relationship('Language', foreign_keys=[language_id])

    @classmethod
    def new(cls, user: User, post: Post, in_reply_to, body, body_html, notify_author, language_id, request_json: dict = None, announce_id=None):

        from app.utils import shorten_string, blocked_phrases, recently_upvoted_post_replies, reply_already_exists, reply_is_just_link_to_gif_reaction, reply_is_stupid
        from app.activitypub.util import notify_about_post_reply

        if not post.comments_enabled:
            raise Exception('Comments are disabled on this post')

        if user.ban_comments:
            raise Exception('Banned from commenting')

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
                          from_bot=user.bot, nsfw=post.nsfw, nsfl=post.nsfl,
                          notify_author=notify_author, instance_id=user.instance_id,
                          language_id=language_id,
                          ap_id=request_json['object']['id'].lower() if request_json else None,
                          ap_create_id=request_json['id'] if request_json else None,
                          ap_announce_id=announce_id)
        if reply.body:
            for blocked_phrase in blocked_phrases():
                if blocked_phrase in reply.body:
                    raise Exception('Blocked phrase in comment')
        if in_reply_to is None or in_reply_to.parent_id is None:
            notification_target = post
        else:
            notification_target = PostReply.query.get(in_reply_to.parent_id)

        if notification_target.author.has_blocked_user(reply.user_id):
            raise Exception('Replier blocked')

        if reply_already_exists(user_id=user.id, post_id=post.id, parent_id=reply.parent_id, body=reply.body):
            raise Exception('Duplicate reply')

        if reply_is_just_link_to_gif_reaction(reply.body):
            user.reputation -= 1
            raise Exception('Gif comment ignored')

        if reply_is_stupid(reply.body):
            raise Exception('Low quality reply')

        try:
            db.session.add(reply)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return PostReply.query.filter_by(ap_id=request_json['object']['id'].lower()).one()

        # Notify subscribers
        notify_about_post_reply(in_reply_to, reply)

        # Subscribe to own comment
        if notify_author:
            new_notification = NotificationSubscription(name=shorten_string(_('Replies to my comment on %(post_title)s',
                                                                              post_title=post.title), 50),
                                                        user_id=user.id, entity_id=reply.id,
                                                        type=NOTIF_REPLY)
            db.session.add(new_notification)

        # upvote own reply
        reply.score = 1
        reply.up_votes = 1
        reply.ranking = PostReply.confidence(1, 0)
        vote = PostReplyVote(user_id=user.id, post_reply_id=reply.id, author_id=user.id, effect=1)
        db.session.add(vote)
        if user.is_local():
            cache.delete_memoized(recently_upvoted_post_replies, user.id)

        reply.ap_id = reply.profile_id()
        if user.reputation > 100:
            reply.up_votes += 1
            reply.score += 1
            reply.ranking += 1
        elif user.reputation < -100:
            reply.score -= 1
            reply.ranking -= 1
        if not user.bot:
            post.reply_count += 1
            post.community.post_reply_count += 1
            post.community.last_active = post.last_active = utcnow()
        user.post_reply_count += 1
        db.session.commit()

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
        return self.ap_id is None or self.ap_id.startswith('https://' + current_app.config['SERVER_NAME'])

    @classmethod
    def get_by_ap_id(cls, ap_id):
        return cls.query.filter_by(ap_id=ap_id.lower()).first()

    def profile_id(self):
        if self.ap_id:
            return self.ap_id
        else:
            return f"https://{current_app.config['SERVER_NAME']}/comment/{self.id}"

    def public_url(self):
        return self.profile_id()

    def posted_at_localized(self, locale):
        try:
            return arrow.get(self.posted_at).humanize(locale=locale)
        except ValueError as v:
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

    def delete_dependencies(self):
        """
        The first loop doesn't seem to ever be invoked with the current behaviour.
        For replies with their own replies: functions which deal with removal don't set reply.deleted and don't call this, and
        because reply.deleted isn't set, the cli task 7 days later doesn't call this either.

        The plan is to set reply.deleted whether there's child replies or not (as happens with the API call), so I've commented
        it out so the current behaviour isn't changed.

        for child_reply in self.child_replies():
            child_reply.delete_dependencies()
            db.session.delete(child_reply)
        """

        db.session.query(PostReplyBookmark).filter(PostReplyBookmark.post_reply_id == self.id).delete()
        db.session.query(Report).filter(Report.suspect_post_reply_id == self.id).delete()
        db.session.execute(text('DELETE FROM post_reply_vote WHERE post_reply_id = :post_reply_id'),
                           {'post_reply_id': self.id})
        if self.image_id:
            file = File.query.get(self.image_id)
            file.delete_from_disk()

    def child_replies(self):
        return PostReply.query.filter_by(parent_id=self.id).all()

    def has_replies(self):
        reply = PostReply.query.filter_by(parent_id=self.id).filter(PostReply.deleted == False).first()
        return reply is not None

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

    # used for ranking comments
    @classmethod
    def _confidence(cls, ups, downs):
        n = ups + downs

        if n == 0:
            return 0.0

        z = 1.281551565545
        p = float(ups) / n

        left = p + 1 / (2 * n) * z * z
        right = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        under = 1 + 1 / n * z * z

        return (left - right) / under

    @classmethod
    def confidence(cls, ups, downs) -> float:
        if ups is None or ups < 0:
            ups = 0
        if downs is None or downs < 0:
            downs = 0
        if ups + downs == 0:
            return 0.0
        else:
            return cls._confidence(ups, downs)

    def vote(self, user: User, vote_direction: str):
        existing_vote = PostReplyVote.query.filter_by(user_id=user.id, post_reply_id=self.id).first()
        if existing_vote and vote_direction == 'reversal':                            # api sends '1' for upvote, '-1' for downvote, and '0' for reversal
            if existing_vote.effect == 1:
                vote_direction = 'upvote'
            elif existing_vote.effect == -1:
                 vote_direction = 'downvote'
        assert vote_direction == 'upvote' or vote_direction == 'downvote'
        undo = None
        if existing_vote:
            if existing_vote.effect > 0:  # previous vote was up
                if vote_direction == 'upvote':  # new vote is also up, so remove it
                    db.session.delete(existing_vote)
                    self.up_votes -= 1
                    self.score -= 1
                    undo = 'Like'
                else:  # new vote is down while previous vote was up, so reverse their previous vote
                    existing_vote.effect = -1
                    self.up_votes -= 1
                    self.down_votes += 1
                    self.score -= 2
            else:  # previous vote was down
                if vote_direction == 'downvote':  # new vote is also down, so remove it
                    db.session.delete(existing_vote)
                    self.down_votes -= 1
                    self.score += 1
                    undo = 'Dislike'
                else:  # new vote is up while previous vote was down, so reverse their previous vote
                    existing_vote.effect = 1
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
                                 effect=effect)
            self.author.reputation += effect
            db.session.add(vote)
        db.session.commit()
        user.last_seen = utcnow()
        self.ranking = PostReply.confidence(self.up_votes, self.down_votes)
        user.recalculate_attitude()
        db.session.commit()
        return undo


class Domain(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), index=True)
    post_count = db.Column(db.Integer, default=0)
    banned = db.Column(db.Boolean, default=False, index=True) # Domains can be banned site-wide (by admin) or DomainBlock'ed by users
    notify_mods = db.Column(db.Boolean, default=False, index=True)
    notify_admins = db.Column(db.Boolean, default=False, index=True)
    post_warning = db.Column(db.String(512))

    def blocked_by(self, user):
        block = DomainBlock.query.filter_by(domain_id=self.id, user_id=user.id).first()
        return block is not None

    def purge_content(self):
        files = File.query.join(Post).filter(Post.domain_id == self.id).all()
        for file in files:
            file.delete_from_disk()
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
    created_at = db.Column(db.DateTime, default=utcnow)


class CommunityWikiPage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    slug = db.Column(db.String(100), index=True)
    title = db.Column(db.String(255))
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    edited_at = db.Column(db.DateTime, default=utcnow)
    who_can_edit = db.Column(db.Integer, default=0)     # 0 = mods & admins, 1 = trusted, 2 = community members, 3 = anyone
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
            if user.is_admin() or user.is_staff() or community.is_moderator(user) or user.trustworthy() or community.is_member(user):
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
    is_accepted = db.Column(db.Boolean, default=True)       # flip to ban remote user / reject follow
    is_inward = db.Column(db.Boolean, default=True)         # true = remote user is following a local one
    created_at = db.Column(db.DateTime, default=utcnow)


# people banned from communities
class CommunityBan(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)             # person who is banned, not the banner
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), primary_key=True)
    banned_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    banned_until = db.Column(db.DateTime)
    reason = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=utcnow)
    ban_until = db.Column(db.DateTime)


class UserNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    target_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    body = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)


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
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)


class UserFollowRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    follow_id = db.Column(db.Integer, db.ForeignKey('user.id'))


class UserRegistration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    answer = db.Column(db.String(512))
    status = db.Column(db.Integer, default=0, index=True)                        # 0 = unapproved, 1 = approved
    created_at = db.Column(db.DateTime, default=utcnow)
    approved_at = db.Column(db.DateTime)
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')


class PostVote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    effect = db.Column(db.Float, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    post = db.relationship('Post', foreign_keys=[post_id])


class PostReplyVote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)   # who voted
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True) # the author of the reply voted on - who's reputation is affected
    post_reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id'), index=True)
    effect = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=utcnow)


# save every activity to a log, to aid debugging
class ActivityPubLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    direction = db.Column(db.String(3))         # 'in' or 'out'
    activity_id = db.Column(db.String(256), index=True)
    activity_type = db.Column(db.String(50))    # e.g. 'Follow', 'Accept', 'Like', etc
    activity_json = db.Column(db.Text)          # the full json of the activity
    result = db.Column(db.String(10))           # 'success' or 'failure'
    exception_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)


class Filter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(50))
    filter_home = db.Column(db.Boolean, default=True)
    filter_posts = db.Column(db.Boolean, default=True)
    filter_replies = db.Column(db.Boolean, default=False)
    hide_type = db.Column(db.Integer, default=0)    # 0 = hide with warning, 1 = hide completely
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
    title = db.Column(db.String(50))
    url = db.Column(db.String(512))
    read = db.Column(db.Boolean, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)       # who the notification should go to
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'))     # the person who caused the notification to happen
    created_at = db.Column(db.DateTime, default=utcnow)


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reasons = db.Column(db.String(256))
    description = db.Column(db.String(256))
    status = db.Column(db.Integer, default=0)   # 0 = new, 1 = escalated to admin, 2 = being appealed, 3 = resolved, 4 = discarded
    type = db.Column(db.Integer, default=0)     # 0 = user, 1 = post, 2 = reply, 3 = community, 4 = conversation
    reporter_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    suspect_community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    suspect_user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    suspect_post_id = db.Column(db.Integer, db.ForeignKey('post.id'))
    suspect_post_reply_id = db.Column(db.Integer, db.ForeignKey('post_reply.id'))
    suspect_conversation_id = db.Column(db.Integer, db.ForeignKey('conversation.id'))
    in_community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    source_instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'))   # the instance of the reporter. mostly used to distinguish between local (instance 1) and remote reports
    created_at = db.Column(db.DateTime, default=utcnow)
    updated = db.Column(db.DateTime, default=utcnow)

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
    name = db.Column(db.String(256))                        # to avoid needing to look up the thing subscribed to via entity_id
    type = db.Column(db.Integer, default=0, index=True)     # see constants.py for possible values: NOTIF_*
    entity_id = db.Column(db.Integer, index=True)           # ID of the user, post, community, etc being subscribed to
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)  # To whom this subscription belongs
    created_at = db.Column(db.DateTime, default=utcnow)     # Perhaps very old subscriptions can be automatically deleted


class Poll(db.Model):
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), primary_key=True)
    end_poll = db.Column(db.DateTime)
    mode = db.Column(db.String(10))     # 'single' or 'multiple' determines whether people can vote for one or multiple options
    local_only = db.Column(db.Boolean)
    latest_vote = db.Column(db.DateTime)

    def has_voted(self, user_id):
        existing_vote = PollChoiceVote.query.filter(PollChoiceVote.user_id == user_id, PollChoiceVote.post_id == self.post_id).first()
        return existing_vote is not None

    def vote_for_choice(self, choice_id, user_id):
        existing_vote = PollChoiceVote.query.filter(PollChoiceVote.user_id == user_id,
                                                    PollChoiceVote.choice_id == choice_id).first()
        if not existing_vote:
            new_vote = PollChoiceVote(choice_id=choice_id, user_id=user_id, post_id=self.post_id)
            db.session.add(new_vote)
            choice = PollChoice.query.get(choice_id)
            choice.num_votes += 1
            self.latest_vote = datetime.utcnow()
            db.session.commit()

    def total_votes(self):
        return db.session.execute(text('SELECT SUM(num_votes) as s FROM "poll_choice" WHERE post_id = :post_id'),
                           {'post_id': self.post_id}).scalar()


class PollChoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    choice_text = db.Column(db.String(200))
    sort_order = db.Column(db.Integer)
    num_votes = db.Column(db.Integer, default=0)

    def percentage(self, poll_total_votes):
        return math.ceil(self.num_votes / poll_total_votes * 100)


class PollChoiceVote(db.Model):
    choice_id = db.Column(db.Integer, db.ForeignKey('poll_choice.id'), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


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
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), index=True)
    type = db.Column(db.String(10))             # 'mod' or 'admin'
    action = db.Column(db.String(30))           # 'removing post', 'banning from community', etc
    reason = db.Column(db.String(512))
    link = db.Column(db.String(512))
    link_text = db.Column(db.String(512))
    public = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    community = db.relationship('Community', lazy='joined', foreign_keys=[community_id])
    author = db.relationship('User', lazy='joined', foreign_keys=[user_id])

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
    }

    def action_to_str(self):
        if self.action in self.action_map:
            return self.action_map[self.action]
        else:
            return self.action


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
    legal_information = db.Column(db.Text, default='')
    public_key = db.Column(db.Text)
    private_key = db.Column(db.Text)
    enable_downvotes = db.Column(db.Boolean, default=True)
    allow_local_image_posts = db.Column(db.Boolean, default=True)
    remote_image_cache_days = db.Column(db.Integer, default=30)
    enable_nsfw = db.Column(db.Boolean, default=False)
    enable_nsfl = db.Column(db.Boolean, default=False)
    community_creation_admin_only = db.Column(db.Boolean, default=False)
    reports_email_admins = db.Column(db.Boolean, default=True)
    registration_mode = db.Column(db.String(20), default='Closed')      # possible values: Open, RequireApplication, Closed
    application_question = db.Column(db.Text, default='')
    allow_or_block_list = db.Column(db.Integer, default=2)  # 1 = allow list, 2 = block list
    allowlist = db.Column(db.Text, default='')
    blocklist = db.Column(db.Text, default='')
    blocked_phrases = db.Column(db.Text, default='')                     # discard incoming content with these phrases
    auto_decline_referrers = db.Column(db.Text, default='rdrama.net\nahrefs.com')   # automatically decline registration requests if the referrer is one of these
    created_at = db.Column(db.DateTime, default=utcnow)
    updated = db.Column(db.DateTime, default=utcnow)
    last_active = db.Column(db.DateTime, default=utcnow)
    log_activitypub_json = db.Column(db.Boolean, default=False)
    default_theme = db.Column(db.String(20), default='')
    contact_email = db.Column(db.String(255), default='')
    about = db.Column(db.Text, default='')
    logo = db.Column(db.String(40), default='')
    logo_152 = db.Column(db.String(40), default='')
    logo_32 = db.Column(db.String(40), default='')
    logo_16 = db.Column(db.String(40), default='')
    show_inoculation_block = db.Column(db.Boolean, default=True)

    @staticmethod
    def admins() -> List[User]:
        return User.query.filter_by(deleted=False, banned=False).join(user_role).filter(user_role.c.role_id == ROLE_ADMIN).order_by(User.id).all()

    @staticmethod
    def staff() -> List[User]:
        return User.query.filter_by(deleted=False, banned=False).join(user_role).filter(user_role.c.role_id == ROLE_STAFF).order_by(User.id).all()


#class IngressQueue(db.Model):
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
    return User.query.get(int(id))
