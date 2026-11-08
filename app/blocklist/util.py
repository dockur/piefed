import re
from random import randint
from time import sleep
from typing import List, Set, Tuple

import httpx
from flask import current_app
from flask_babel import lazy_gettext as _l
from sqlalchemy import func

from app import db
from app.models import Blocklist, BannedInstances, BlocklistItem, User, Community, Feed
from app.utils import get_request


def apply_blocklist_url_rules(field) -> bool:
    """Validates and normalises the url field of a blocklist form. Adds errors to the field and returns False if invalid."""
    url = (field.data or '').strip()
    if url.lower().startswith('/b/'):
        url = url[3:]
    url = url.lower()
    field.data = url

    if url == '':
        field.errors.append(_l('Url is required.'))
        return False
    if '-' in url:
        field.errors.append(_l('- cannot be in Url. Use _ instead?'))
        return False
    if not re.match(r'^[a-z0-9_]+$', url):
        field.errors.append(_l('Blocklist urls can only contain letters, numbers, and underscores.'))
        return False

    if db.session.query(Blocklist).filter(Blocklist.machine_name == url, Blocklist.ap_id == None).first():
        field.errors.append(_l('A blocklist with this url already exists.'))
        return False
    if db.session.query(Community).filter(Community.name == url, Community.ap_id == None).first():
        field.errors.append(_l('A community with this url already exists.'))
        return False
    if db.session.query(Feed).filter(Feed.name == url, Feed.ap_id == None).first():
        field.errors.append(_l('A feed with this url already exists.'))
        return False
    user = db.session.query(User).filter(func.lower(User.user_name) == url, User.ap_id == None).first()
    if user is not None:
        if user.deleted:
            field.errors.append(_l('This name was used in the past and cannot be reused.'))
        else:
            field.errors.append(_l('This name is in use already.'))
        return False
    return True


def search_for_blocklist(address: str, allow_fetch: bool = True):
    """address is in the format %blocklistname@server.tld"""
    if address.startswith('%'):
        address = address[1:]
    if '@' not in address:
        return None
    name, server = address.lower().split('@', 1)

    banned = db.session.query(BannedInstances).filter_by(domain=server).first()
    if banned:
        reason = f" Reason: {banned.reason}" if banned.reason is not None else ''
        raise Exception(f"{server} is blocked.{reason}")  # todo: create custom exception class hierarchy

    if current_app.config['SERVER_NAME'].lower() == server:
        return db.session.query(Blocklist).filter_by(machine_name=name, ap_id=None).first()

    already_exists = db.session.query(Blocklist).filter_by(ap_id=f'{name}@{server}').first()
    if already_exists:
        return already_exists
    elif not allow_fetch:
        return None

    # Look up the profile address of the Blocklist using WebFinger. The % prefix tells the remote server that we are
    # looking for a blocklist, not a user/community/feed with the same name.
    resource = f"acct:%{name}@{server}"
    try:
        webfinger_data = get_request(f"https://{server}/.well-known/webfinger", params={'resource': resource})
    except httpx.HTTPError:
        sleep(randint(3, 10))
        try:
            webfinger_data = get_request(f"https://{server}/.well-known/webfinger", params={'resource': resource})
        except httpx.HTTPError:
            return None

    if webfinger_data.status_code == 200:
        from app.activitypub.util import actor_json_to_model
        webfinger_json = webfinger_data.json()
        webfinger_data.close()
        for links in webfinger_json.get('links', []):
            if links.get('rel') == 'self':  # this contains the URL of the activitypub profile
                type = links.get('type', 'application/activity+json')
                # retrieve the activitypub profile
                blocklist_data = get_request(links['href'], headers={'Accept': type})
                if blocklist_data.status_code == 200:
                    blocklist_json = blocklist_data.json()
                    blocklist_data.close()
                    if blocklist_json.get('type') == 'Blocklist':
                        return actor_json_to_model(blocklist_json, name, server)
    return None


def actor_to_blocklist(actor: str) -> Blocklist:
    actor = actor.strip().lower()
    if actor.startswith('%'):
        actor = actor[1:]
    if '@' in actor:
        name, server = actor.split('@', 1)
        if server == current_app.config['SERVER_NAME'].lower():
            actor = name
        else:
            return db.session.query(Blocklist).filter_by(ap_id=actor).first()
    return db.session.query(Blocklist).filter_by(machine_name=actor, ap_id=None).first()


def blocklist_members_for_edit(blocklist_id: int) -> str:
    """The contents of the 'members' textarea when editing or copying a blocklist."""
    return_value = []
    for user in db.session.query(User).join(BlocklistItem, BlocklistItem.user_id == User.id).\
            filter(BlocklistItem.blocklist_id == blocklist_id).all():
        if user.is_local():
            return_value.append(f'{user.user_name}@{current_app.config["SERVER_NAME"]}')
        else:
            return_value.append(user.ap_id)
    return "\n".join(sorted(return_value, key=str.lower))


def resolve_blocklist_member(line: str) -> User | None:
    """Turn one line of the 'members' textarea into a User. Accepts name, @name, name@server.tld, @name@server.tld
    or https://server.tld/u/name"""
    from app.activitypub.util import find_actor_or_create
    line = line.strip()
    if not line:
        return None
    if line.startswith('https://') or line.startswith('http://'):
        actor = find_actor_or_create(line)
        return actor if isinstance(actor, User) else None

    line = line.lstrip('@')
    if '@' in line:
        name, server = line.split('@', 1)
        if server.lower() != current_app.config['SERVER_NAME'].lower():
            actor = find_actor_or_create(f'{name}@{server}')
            return actor if isinstance(actor, User) else None
    else:
        name = line
    return db.session.query(User).filter(func.lower(User.user_name) == name.lower(), User.ap_id == None,
                                         User.deleted == False).first()


def form_members_to_ids(form_members: str) -> Tuple[Set[int], List[str]]:
    """Returns the ids of the accounts in the 'members' textarea, and the lines that could not be found"""
    result = set()
    unresolved = []
    for line in (form_members or '').strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        user = resolve_blocklist_member(line)
        if user:
            result.add(user.id)
        else:
            unresolved.append(line)
    return result, unresolved
