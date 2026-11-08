import re
from random import randint
from time import sleep

import httpx
from flask import current_app
from flask_login import current_user
from flask_babel import _, lazy_gettext as _l
from sqlalchemy import func

from app.activitypub.util import actor_json_to_model
from app.models import Blocklist, BannedInstances, BlocklistItem, User
from app.utils import get_request


def apply_blocklist_url_rules(self):
    if '-' in self.url.data.strip():
        self.url.errors.append(_l('- cannot be in Url. Use _ instead?'))
        return False

    if not self.public.data and not '/' in self.url.data.strip():
        self.url.data = self.url.data.strip().lower() + '/' + current_user.user_name.lower()
    elif self.public.data and '/' in self.url.data.strip():
        self.url.data = self.url.data.strip().split('/', 1)[0]
    else:
        self.url.data = self.url.data.strip().lower()

    # Allow alphanumeric characters and underscores (a-z, A-Z, 0-9, _)
    if self.public.data:
        regex = r'^[a-zA-Z0-9_]+$'
    else:
        regex = r'^[a-zA-Z0-9_]+(?:/' + current_user.user_name.lower() + ')?$'
    if not re.match(regex, self.url.data):
        self.url.errors.append(_l('Blocklist urls can only contain letters, numbers, and underscores.'))
        return False

    try:
        self.feed_id
    except AttributeError:
        feed = Blocklist.query.filter(Blocklist.name == self.url.data).first()
    else:
        feed = Blocklist.query.filter(Blocklist.name == self.url.data).filter(Blocklist.id != self.feed_id).first()
    if feed is not None:
        self.url.errors.append(_l('A blocklist with this url already exists.'))
        return False
    return True


def search_for_blocklist(address: str, allow_fetch: bool = True):
    if address.startswith('%'):
        name, server = address[1:].split('@')

        banned = BannedInstances.query.filter_by(domain=server).first()
        if banned:
            reason = f" Reason: {banned.reason}" if banned.reason is not None else ''
            raise Exception(f"{server} is blocked.{reason}")  # todo: create custom exception class hierarchy

        if current_app.config['SERVER_NAME'] == server:
            already_exists = Blocklist.query.filter_by(name=name, ap_id=None).first()
            return already_exists

        already_exists = Blocklist.query.filter_by(ap_id=address[1:]).first()
        if already_exists:
            return already_exists
        elif not allow_fetch:
            return None

        # Look up the profile address of the Feed using WebFinger
        try:
            webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                         params={'resource': f"acct:{address}"})  # include the ~ on the start to indicate we're searching for a feed
        except httpx.HTTPError:
            sleep(randint(3, 10))
            try:
                webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                             params={'resource': f"acct:{address}"})
            except httpx.HTTPError:
                return None

        if webfinger_data.status_code == 200:
            webfinger_json = webfinger_data.json()
            for links in webfinger_json['links']:
                if 'rel' in links and links['rel'] == 'self':  # this contains the URL of the activitypub profile
                    type = links['type'] if 'type' in links else 'application/activity+json'
                    # retrieve the activitypub profile
                    feed_data = get_request(links['href'], headers={'Accept': type})
                    if feed_data.status_code == 200:
                        feed_json = feed_data.json()
                        feed_data.close()
                        if feed_json['type'] == 'Blocklist':
                            feed = actor_json_to_model(feed_json, name, server)
                            if feed:
                                initialise_new_members(feed)
                                return feed
        return None


def blocklist_members_for_edit(blocklist_id: int) -> str:
    return_value = []
    for user in User.query.filter(User.banned == False).join(BlocklistItem, BlocklistItem.user_id == User.id).\
            filter(BlocklistItem.feed_id == blocklist_id).all():
        ap_id = user.link()
        if '@' not in ap_id:
            ap_id = f'{ap_id}@{current_app.config["SERVER_NAME"]}'
        return_value.append(ap_id)
    return "\n".join(sorted(return_value))
