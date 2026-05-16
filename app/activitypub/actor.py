from __future__ import annotations

import time
from datetime import timedelta
from random import randint

import httpx
from flask import current_app
from sqlalchemy import or_, func

from app import cache, db
from app.activitypub.util import get_request, signed_get_request, actor_json_to_model, refresh_user_profile, \
    refresh_community_profile, refresh_feed_profile, extract_domain_and_actor, normalise_actor_string
from app.models import User, Community, Feed, Site
from app.utils import utcnow, get_setting, actor_contains_blocked_words, actor_profile_contains_blocked_words, \
    instance_banned, low_value_reposters, instance_allowed


def find_local_community(actor_url: str) -> Community:
    """Find a local community by URL."""
    return db.session.query(Community).filter(Community.ap_profile_id == actor_url).first()


def find_local_feed(actor_url: str) -> Feed:
    """Find a local feed by URL."""
    return db.session.query(Feed).filter(Feed.ap_profile_id == actor_url).first()


def find_local_user(actor_url: str, allow_banned: bool = False) -> User:
    """Find a local user by URL or alt name."""
    alt_user_name = actor_url.rsplit('/', 1)[-1]
    user = db.session.query(User).filter(or_(User.ap_profile_id == actor_url, User.alt_user_name == alt_user_name),
                                         User.ap_id == None)
    if not allow_banned:
        user = user.filter_by(banned=False)
    return user.first()


def validate_remote_actor(actor_url, actor=None, allow_banned=False):
    if actor_url == 'https://www.w3.org/ns/activitystreams#Public':
        return False
    """Validate if a remote actor is allowed."""
    server, _ = extract_domain_and_actor(actor_url)

    # Check if instance is allowed/banned
    if get_setting('use_allowlist', False):
        if not instance_allowed(server):
            return False
    else:
        if instance_banned(server):
            return False

    # Check for blocked words
    if actor_contains_blocked_words(actor_url):
        return False

    # If we have the actor object, check more conditions
    if actor:
        if actor.banned and not allow_banned:
            return False
        if isinstance(actor, User):
            if actor.deleted or actor_profile_contains_blocked_words(actor):
                return False

    return True


def find_remote_actor(actor_url):
    """Find a remote actor in the database."""
    # Check URL patterns to optimize database queries
    if '/u/' in actor_url:
        # URL contains /u/ - likely a user
        actor = db.session.query(User).filter(User.ap_profile_id == actor_url).first()
        if actor:
            return actor
    elif (('/c/' in actor_url and '/p/' not in actor_url) or
          ('/m/' in actor_url and '/t/' not in actor_url)):
        # URL contains /c/ - likely a community
        actor = db.session.query(Community).filter(Community.ap_profile_id == actor_url).first()
        if actor and actor.banned:
            # Try to find a non-banned copy of the community
            unbanned_actor = db.session.query(Community).filter(Community.ap_profile_id == actor_url,
                                                                Community.banned == False).first()
            if unbanned_actor is None:
                return None
            actor = unbanned_actor
        if actor:
            return actor
    elif '/f/' in actor_url:
        # URL contains /f/ - likely a feed
        actor = db.session.query(Feed).filter(Feed.ap_profile_id == actor_url).first()
        if actor:
            return actor
    
    # Fallback to trying everything
    actor = db.session.query(User).filter(User.ap_profile_id == actor_url).first()

    # Look for a remote community if not found as user
    if actor is None:
        actor = db.session.query(Community).filter(Community.ap_profile_id == actor_url).first()
        if actor and actor.banned:
            # Try to find a non-banned copy of the community
            unbanned_actor = db.session.query(Community).filter(Community.ap_profile_id == actor_url,
                                                                Community.banned == False).first()
            if unbanned_actor is None:
                return None
            actor = unbanned_actor

    # Look for a remote feed if not found as user or community
    if actor is None:
        actor = db.session.query(Feed).filter(Feed.ap_profile_id == actor_url).first()

    return actor


def schedule_actor_refresh(actor, override=False):
    """Schedule an async refresh of actor data if needed."""
    if not actor.is_local() and (
        actor.ap_fetched_at is None or actor.ap_fetched_at < utcnow() - timedelta(days=1) or override):
        refresh_in_progress = cache.get(f'refreshing_{actor.id}')
        if not refresh_in_progress:
            cache.set(f'refreshing_{actor.id}', True, timeout=300)

            if isinstance(actor, User):
                refresh_user_profile(actor.id)
            elif isinstance(actor, Community):
                refresh_community_profile(actor.id)
            elif isinstance(actor, Feed):
                refresh_feed_profile(actor.id)


def fetch_remote_actor_data(url: str, retry_count=1):
    """Fetch actor data with retry logic."""
    for attempt in range(retry_count + 1):
        response = None
        try:
            response = get_request(url, headers={'Accept': 'application/activity+json'})
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    return None

            elif response.status_code == 401:
                signed_response = None
                try:
                    site = db.session.query(Site).get(1)
                    signed_response = signed_get_request(url, site.private_key, f"{current_app.config['SERVER_URL']}/actor#main-key")
                    try:
                        return signed_response.json()
                    except ValueError:
                        return None
                except Exception:
                    return None
                finally:
                    if signed_response is not None:
                        signed_response.close()

            elif response.status_code in (429, 502, 503, 504):
                # Retryable server errors
                if attempt < retry_count:
                    time.sleep(randint(3, 10))
                    continue
                else:
                    return None

            # Any other status code → give up
            return None

        # These exceptions usually mean "server is overloaded", so retry
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError):
            if attempt < retry_count:
                time.sleep(randint(3, 10))
                continue
            else:
                return None

        except httpx.HTTPError:
            # Non-retryable network issues (DNS fail, unreachable, SSL error, etc.)
            return None

        finally:
            if response is not None:
                response.close()

    return None



def fetch_actor_from_webfinger(address: str, server: str):
    """Fetch actor data using webfinger protocol."""
    try:
        webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                     params={'resource': f"acct:{address}@{server}"})
    except httpx.HTTPError:
        time.sleep(randint(3, 10))
        try:
            webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                         params={'resource': f"acct:{address}@{server}"})
        except Exception:
            return None

    content_type = webfinger_data.headers.get('Content-Type', '').lower()

    if webfinger_data.status_code == 200 and ('application/jrd+json' in content_type or 'application/json' in content_type):
        webfinger_json = webfinger_data.json()
        webfinger_data.close()

        for link in webfinger_json.get('links', []):
            if link.get('rel') == 'self':
                type_header = link.get('type', 'application/activity+json')
                try:
                    actor_data = get_request(link['href'], headers={'Accept': type_header})
                except httpx.HTTPError:
                    time.sleep(randint(3, 10))
                    try:
                        actor_data = get_request(link['href'], headers={'Accept': type_header})
                    except Exception:
                        return None

                if actor_data.status_code == 200:
                    try:
                        actor_json = actor_data.json()
                        actor_data.close()
                        return actor_json
                    except Exception:
                        actor_data.close()

    return None


def create_actor_from_remote(actor_address: str, community_only=False,
                             feed_only=False) -> User | Community | Feed | None:
    """Create a new actor from remote data."""
    if actor_address.startswith('https://') or actor_address.startswith('http://'):
        server, address = extract_domain_and_actor(actor_address)
        actor_json = fetch_remote_actor_data(actor_address)
    else:
        # Try webfinger
        address, server = normalise_actor_string(actor_address)
        if not address:
            return None
        actor_json = fetch_actor_from_webfinger(address, server)

    if actor_json:
        actor_model = actor_json_to_model(actor_json, address, server)

        if community_only and not isinstance(actor_model, Community):
            return None
        if feed_only and not isinstance(actor_model, Feed):
            return None

        if isinstance(actor_model, User) and actor_model.bot:
            cache.delete_memoized(low_value_reposters)

        return actor_model

    return None


def find_actor_by_url(actor_url, community_only=False, feed_only=False, allow_banned=False):
    """Find an actor by URL without creating it."""
    """Warning: this function returns None if not found, False if found and banned/deleted"""
    actor_url = actor_url.strip().lower()
    server_name = current_app.config['SERVER_NAME']

    # Check for local actors first
    if f"{server_name}/c/" in actor_url and "/p/" not in actor_url:
        actor = find_local_community(actor_url)
        if actor and community_only:
            return actor
        elif actor and not community_only:
            return actor
        return None

    if f"{server_name}/f/" in actor_url:
        actor = find_local_feed(actor_url)
        if actor and feed_only:
            return actor
        elif actor and not feed_only:
            return actor
        return None

    if f"{server_name}/u/" in actor_url:
        actor = find_local_user(actor_url, allow_banned=allow_banned)
        if actor and not community_only and not feed_only:
            return actor
        return None

    # For remote actors
    if actor_url.startswith('https://') or actor_url.startswith('http://'):
        actor = find_remote_actor(actor_url)

        if actor:
            if not validate_remote_actor(actor_url, actor):
                return False  # banned actor found
            if community_only and not isinstance(actor, Community):
                return None
            if feed_only and not isinstance(actor, Feed):
                return None

            return actor

    if '@' in actor_url:
        user: User = User.query.filter_by(ap_id=actor_url.lower()).first()
        return user
    else:
        user: User = User.query.filter(or_(func.lower(User.user_name) == actor_url.lower())).filter_by(ap_id=None).first()
        if user is None:
            user = User.query.filter_by(ap_profile_id=f'{current_app.config["SERVER_URL"]}/u/{actor_url.lower()}',
                                        ap_id=None).first()
        return user

    return None
