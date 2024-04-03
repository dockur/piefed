from datetime import datetime, timedelta, date, timezone
from time import time
from typing import List, Union

import requests
from flask import current_app, escape, url_for, render_template_string
from flask_login import UserMixin, current_user
from sqlalchemy import or_, text
from werkzeug.security import generate_password_hash, check_password_hash
from flask_babel import _, lazy_gettext as _l
from sqlalchemy.orm import backref
from sqlalchemy_utils.types import TSVectorType # https://sqlalchemy-searchable.readthedocs.io/en/latest/installation.html
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.mutable import MutableList
from flask_sqlalchemy import BaseQuery
from sqlalchemy_searchable import SearchQueryMixin
from app import db, login, cache, celery
import jwt
import os

from app.constants import SUBSCRIPTION_NONMEMBER, SUBSCRIPTION_MEMBER, SUBSCRIPTION_MODERATOR, SUBSCRIPTION_OWNER, \
    SUBSCRIPTION_BANNED, SUBSCRIPTION_PENDING


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
    domain = db.Column(db.String(256), index=True)
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

    posts = db.relationship('Post', backref='instance', lazy='dynamic')
    post_replies = db.relationship('PostReply', backref='instance', lazy='dynamic')
    communities = db.relationship('Community', backref='instance', lazy='dynamic')

    def online(self):
        return not self.dormant and not self.gone_forever

    def user_is_admin(self, user_id):
        role = InstanceRole.query.filter_by(instance_id=self.id, user_id=user_id).first()
        return role and role.role == 'admin'


class InstanceRole(db.Model):
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    role = db.Column(db.String(50), default='admin')

    user = db.relationship('User', lazy='joined')


class InstanceBlock(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow)


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


class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    file_path = db.Column(db.String(255))
    file_name = db.Column(db.String(255))
    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    alt_text = db.Column(db.String(300))
    source_url = db.Column(db.String(1024))
    thumbnail_path = db.Column(db.String(255))
    thumbnail_width = db.Column(db.Integer)
    thumbnail_height = db.Column(db.Integer)

    def view_url(self):
        if self.source_url:
            return self.source_url
        elif self.file_path:
            file_path = self.file_path[4:] if self.file_path.startswith('app/') else self.file_path
            return f"https://{current_app.config['SERVER_NAME']}/{file_path}"
        else:
            return ''

    def medium_url(self):
        if self.file_path is None:
            return self.thumbnail_url()
        file_path = self.file_path[4:] if self.file_path.startswith('app/') else self.file_path
        return f"https://{current_app.config['SERVER_NAME']}/{file_path}"

    def thumbnail_url(self):
        if self.thumbnail_path is None:
            if self.source_url:
                return self.source_url
            else:
                return ''
        thumbnail_path = self.thumbnail_path[4:] if self.thumbnail_path.startswith('app/') else self.thumbnail_path
        return f"https://{current_app.config['SERVER_NAME']}/{thumbnail_path}"

    def delete_from_disk(self):
        purge_from_cache = []
        if self.file_path and os.path.isfile(self.file_path):
            os.unlink(self.file_path)
            purge_from_cache.append(self.file_path.replace('app/', f"https://{current_app.config['SERVER_NAME']}/"))
        if self.thumbnail_path and os.path.isfile(self.thumbnail_path):
            os.unlink(self.thumbnail_path)
            purge_from_cache.append(self.thumbnail_path.replace('app/', f"https://{current_app.config['SERVER_NAME']}/"))
        if self.source_url and self.source_url.startswith('http') and current_app.config['SERVER_NAME'] in self.source_url:
            # self.source_url is always a url rather than a file path, which makes deleting the file a bit fiddly
            os.unlink(self.source_url.replace(f"https://{current_app.config['SERVER_NAME']}/", 'app/'))
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
        response = requests.request(
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
    communities = db.relationship('Community', lazy='dynamic', backref='topic', cascade="all, delete-orphan")


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

    ap_id = db.Column(db.String(255), index=True)
    ap_profile_id = db.Column(db.String(255), index=True)
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
    show_home = db.Column(db.Boolean, default=False)        # For anonymous users. When logged in, the home feed shows posts from subscribed communities
    show_popular = db.Column(db.Boolean, default=True)
    show_all = db.Column(db.Boolean, default=True)

    search_vector = db.Column(TSVectorType('name', 'title', 'description', 'rules'))

    posts = db.relationship('Post', lazy='dynamic', cascade="all, delete-orphan")
    replies = db.relationship('PostReply', lazy='dynamic', cascade="all, delete-orphan")
    icon = db.relationship('File', foreign_keys=[icon_id], single_parent=True, backref='community', cascade="all, delete-orphan")
    image = db.relationship('File', foreign_keys=[image_id], single_parent=True, cascade="all, delete-orphan")

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
                                     )) & CommunityMember.is_banned == False
                                     ).all()

    def is_moderator(self, user=None):
        if user is None:
            return any(moderator.user_id == current_user.id for moderator in self.moderators())
        else:
            return any(moderator.user_id == user.id for moderator in self.moderators())

    def is_owner(self, user=None):
        if user is None:
            return any(moderator.user_id == current_user.id and moderator.is_owner for moderator in self.moderators())
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
        membership = CommunityMember.query.filter(CommunityMember.community_id == self.id, CommunityMember.user_id == user.id).first()
        if membership and membership.is_banned:
            return True
        banned = CommunityBan.query.filter(CommunityBan.community_id == self.id, CommunityBan.user_id == user.id).first()
        if banned:
            return True
        return False


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
        member_info = CommunityMember.query.filter(CommunityMember.community_id == self.id,
                                                   CommunityMember.is_banned == False,
                                                   CommunityMember.user_id == user_id).first()
        if not member_info:
            return False
        return member_info.notify_new_posts

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

    def delete_dependencies(self):
        for post in self.posts:
            post.delete_dependencies()
            db.session.delete(post)
        db.session.query(CommunityBan).filter(CommunityBan.community_id == self.id).delete()
        db.session.query(CommunityBlock).filter(CommunityBlock.community_id == self.id).delete()
        db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.community_id == self.id).delete()
        db.session.query(CommunityMember).filter(CommunityMember.community_id == self.id).delete()
        db.session.query(Report).filter(Report.suspect_community_id == self.id).delete()


user_role = db.Table('user_role',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
    db.Column('role_id', db.Integer, db.ForeignKey('role.id')),
    db.PrimaryKeyConstraint('user_id', 'role_id')
)


class User(UserMixin, db.Model):
    query_class = FullTextSearchQuery
    id = db.Column(db.Integer, primary_key=True)
    user_name = db.Column(db.String(255), index=True)
    title = db.Column(db.String(256))
    email = db.Column(db.String(255), index=True)
    password_hash = db.Column(db.String(128))
    verified = db.Column(db.Boolean, default=False)
    verification_token = db.Column(db.String(16), index=True)
    banned = db.Column(db.Boolean, default=False)
    deleted = db.Column(db.Boolean, default=False)
    about = db.Column(db.Text)      # markdown
    about_html = db.Column(db.Text) # html
    keywords = db.Column(db.String(256))
    matrix_user_id = db.Column(db.String(256))
    show_nsfw = db.Column(db.Boolean, default=False)
    show_nsfl = db.Column(db.Boolean, default=False)
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
    stripe_customer_id = db.Column(db.String(50))
    stripe_subscription_id = db.Column(db.String(50))
    searchable = db.Column(db.Boolean, default=True)
    indexable = db.Column(db.Boolean, default=False)
    bot = db.Column(db.Boolean, default=False)
    ignore_bots = db.Column(db.Boolean, default=False)
    unread_notifications = db.Column(db.Integer, default=0)
    ip_address = db.Column(db.String(50))
    instance_id = db.Column(db.Integer, db.ForeignKey('instance.id'), index=True)
    reports = db.Column(db.Integer, default=0)  # how many times this user has been reported.
    default_sort = db.Column(db.String(25), default='hot')
    theme = db.Column(db.String(20), default='')
    referrer = db.Column(db.String(256))
    markdown_editor = db.Column(db.Boolean, default=False)

    avatar = db.relationship('File', lazy='joined', foreign_keys=[avatar_id], single_parent=True, cascade="all, delete-orphan")
    cover = db.relationship('File', lazy='joined', foreign_keys=[cover_id], single_parent=True, cascade="all, delete-orphan")
    instance = db.relationship('Instance', lazy='joined', foreign_keys=[instance_id])
    conversations = db.relationship('Conversation', lazy='dynamic', secondary=conversation_member, backref=db.backref('members', lazy='joined'))

    ap_id = db.Column(db.String(255), index=True)           # e.g. username@server
    ap_profile_id = db.Column(db.String(255), index=True)   # e.g. https://server/u/username
    ap_public_url = db.Column(db.String(255))               # e.g. https://server/u/username
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
                return self.title
            else:
                return self.user_name
        else:
            return '[deleted]'

    @cache.memoize(timeout=10)
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

    @cache.memoize(timeout=10)
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

    def trustworthy(self):
        if self.is_admin():
            return True
        if self.created_recently() or self.reputation < 100:
            return False
        return True

    def link(self) -> str:
        if self.is_local():
            return self.user_name
        else:
            return self.ap_id

    def followers_url(self):
        if self.ap_followers_url:
            return self.ap_followers_url
        else:
            return self.profile_id() + '/followers'

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
        upvotes = db.session.execute(text('SELECT COUNT(id) as c FROM "post_vote" WHERE user_id = :user_id AND effect > 0'),
                                         {'user_id': self.id}).scalar()
        downvotes = db.session.execute(text('SELECT COUNT(id) as c FROM "post_vote" WHERE user_id = :user_id AND effect < 0'),
                                         {'user_id': self.id}).scalar()
        if upvotes is None:
            upvotes = 0
        if downvotes is None:
            downvotes = 0

        comment_upvotes = db.session.execute(text('SELECT COUNT(id) as c FROM "post_reply_vote" WHERE user_id = :user_id AND effect > 0'),
                                     {'user_id': self.id}).scalar()
        comment_downvotes = db.session.execute(text('SELECT COUNT(id) as c FROM "post_reply_vote" WHERE user_id = :user_id AND effect < 0'),
                                       {'user_id': self.id}).scalar()

        if comment_upvotes is None:
            comment_upvotes = 0
        if comment_downvotes is None:
            comment_downvotes = 0

        total_upvotes = upvotes + comment_upvotes
        total_downvotes = downvotes + comment_downvotes

        if total_downvotes == 0:    # guard against division by zero
            self.attitude = 1.0
        else:
            self.attitude = (total_upvotes - total_downvotes) / (total_upvotes + total_downvotes)

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
        result = self.ap_profile_id if self.ap_profile_id else f"https://{current_app.config['SERVER_NAME']}/u/{self.user_name}"
        return result

    def public_url(self):
        result = self.ap_public_url if self.ap_public_url else f"https://{current_app.config['SERVER_NAME']}/u/{self.user_name}"
        return result

    def created_recently(self):
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

    def flush_cache(self):
        cache.delete('/u/' + self.user_name + '_False')
        cache.delete('/u/' + self.user_name + '_True')

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

    def purge_content(self):
        files = File.query.join(Post).filter(Post.user_id == self.id).all()
        for file in files:
            file.delete_from_disk()
        self.delete_dependencies()
        posts = Post.query.filter_by(user_id=self.id).all()
        for post in posts:
            post.delete_dependencies()
            post.flush_cache()
            db.session.delete(post)
        post_replies = PostReply.query.filter_by(user_id=self.id).all()
        for reply in post_replies:
            reply.body = reply.body_html = ''
        db.session.commit()

    def mention_tag(self):
        if self.ap_domain is None:
            return '@' + self.user_name + '@' + current_app.config['SERVER_NAME']
        else:
            return '@' + self.user_name + '@' + self.ap_domain


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
    slug = db.Column(db.String(255))
    title = db.Column(db.String(255))
    url = db.Column(db.String(2048))
    body = db.Column(db.Text)
    body_html = db.Column(db.Text)
    type = db.Column(db.Integer)
    comments_enabled = db.Column(db.Boolean, default=True)
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
    language = db.Column(db.String(10))
    edited_at = db.Column(db.DateTime)
    reports = db.Column(db.Integer, default=0)                          # how many times this post has been reported. Set to -1 to ignore reports
    cross_posts = db.Column(MutableList.as_mutable(ARRAY(db.Integer)))

    ap_id = db.Column(db.String(255), index=True)
    ap_create_id = db.Column(db.String(100))
    ap_announce_id = db.Column(db.String(100))

    search_vector = db.Column(TSVectorType('title', 'body'))

    image = db.relationship(File, lazy='joined', foreign_keys=[image_id], cascade="all, delete")
    domain = db.relationship('Domain', lazy='joined', foreign_keys=[domain_id])
    author = db.relationship('User', lazy='joined', overlaps='posts', foreign_keys=[user_id])
    community = db.relationship('Community', lazy='joined', overlaps='posts', foreign_keys=[community_id])
    replies = db.relationship('PostReply', lazy='dynamic', backref='post')

    def is_local(self):
        return self.ap_id is None or self.ap_id.startswith('https://' + current_app.config['SERVER_NAME'])

    @classmethod
    def get_by_ap_id(cls, ap_id):
        return cls.query.filter_by(ap_id=ap_id).first()

    def delete_dependencies(self):
        db.session.query(Report).filter(Report.suspect_post_id == self.id).delete()
        db.session.execute(text('DELETE FROM post_reply_vote WHERE post_reply_id IN (SELECT id FROM post_reply WHERE post_id = :post_id)'),
                           {'post_id': self.id})
        db.session.execute(text('DELETE FROM post_reply WHERE post_id = :post_id'), {'post_id': self.id})
        db.session.execute(text('DELETE FROM post_vote WHERE post_id = :post_id'), {'post_id': self.id})
        if self.image_id:
            file = File.query.get(self.image_id)
            file.delete_from_disk()

    def youtube_embed(self):
        if self.url:
            vpos = self.url.find('v=')
            if vpos != -1:
                return self.url[vpos + 2:vpos + 13]

    def profile_id(self):
        if self.ap_id:
            return self.ap_id
        else:
            return f"https://{current_app.config['SERVER_NAME']}/post/{self.id}"

    def blocked_by_content_filter(self, content_filters):
        lowercase_title = self.title.lower()
        for name, keywords in content_filters.items() if content_filters else {}:
            for keyword in keywords:
                if keyword in lowercase_title:
                    return name
        return False

    def flush_cache(self):
        cache.delete(f'/post/{self.id}_False')
        cache.delete(f'/post/{self.id}_True')


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
    ip = db.Column(db.String(50))
    from_bot = db.Column(db.Boolean, default=False)
    up_votes = db.Column(db.Integer, default=0)
    down_votes = db.Column(db.Integer, default=0)
    ranking = db.Column(db.Float, default=0.0, index=True)  # used for 'hot' sorting
    language = db.Column(db.String(10))
    edited_at = db.Column(db.DateTime)
    reports = db.Column(db.Integer, default=0)  # how many times this post has been reported. Set to -1 to ignore reports

    ap_id = db.Column(db.String(255), index=True)
    ap_create_id = db.Column(db.String(100))
    ap_announce_id = db.Column(db.String(100))

    search_vector = db.Column(TSVectorType('body'))

    author = db.relationship('User', lazy='joined', foreign_keys=[user_id], single_parent=True, overlaps="post_replies")
    community = db.relationship('Community', lazy='joined', overlaps='replies', foreign_keys=[community_id])

    def is_local(self):
        return self.ap_id is None or self.ap_id.startswith('https://' + current_app.config['SERVER_NAME'])

    @classmethod
    def get_by_ap_id(cls, ap_id):
        return cls.query.filter_by(ap_id=ap_id).first()

    def profile_id(self):
        if self.ap_id:
            return self.ap_id
        else:
            return f"https://{current_app.config['SERVER_NAME']}/comment/{self.id}"

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
            return self.post.author.profile_id()
        else:
            parent = PostReply.query.get(self.parent_id)
            return parent.author.profile_id()

    def delete_dependencies(self):
        db.session.query(Report).filter(Report.suspect_post_reply_id == self.id).delete()
        db.session.execute(text('DELETE FROM post_reply_vote WHERE post_reply_id = :post_reply_id'),
                           {'post_reply_id': self.id})
        if self.image_id:
            file = File.query.get(self.image_id)
            file.delete_from_disk()

    def has_replies(self):
        reply = PostReply.query.filter_by(parent_id=self.id).first()
        return reply is not None

    def blocked_by_content_filter(self, content_filters):
        lowercase_body = self.body.lower()
        for name, keywords in content_filters.items() if content_filters else {}:
            for keyword in keywords:
                if keyword in lowercase_body:
                    return name
        return False


class Domain(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), index=True)
    post_count = db.Column(db.Integer, default=0)
    banned = db.Column(db.Boolean, default=False, index=True) # Domains can be banned site-wide (by admin) or DomainBlock'ed by users
    notify_mods = db.Column(db.Boolean, default=False, index=True)
    notify_admins = db.Column(db.Boolean, default=False, index=True)

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
            post.flush_cache()
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


# people banned from communities
class CommunityBan(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)             # person who is banned, not the banner
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), primary_key=True)
    banned_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    reason = db.Column(db.String(50))
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
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)
    effect = db.Column(db.Float, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    post = db.relationship('Post', foreign_keys=[post_id])


class PostReplyVote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)   # who voted
    author_id = db.Column(db.Integer, db.ForeignKey('user.id')) # the author of the reply voted on - who's reputation is affected
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

    @staticmethod
    def admins() -> List[User]:
        return User.query.filter_by(deleted=False, banned=False).join(user_role).filter(user_role.c.role_id == 4).all()


@login.user_loader
def load_user(id):
    return User.query.get(int(id))
