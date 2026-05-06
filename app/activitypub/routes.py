import werkzeug.exceptions
from flask import request, current_app, abort, jsonify, json, g, url_for, redirect, make_response, flash
from flask_babel import _
from flask_login import current_user
from furl import furl
from psycopg2 import IntegrityError
from sqlalchemy import desc, or_, text, func

from app import db, cache, celery, limiter
from app.activitypub import bp
from app.activitypub.signature import HttpSignature, VerificationError, VerificationFormatError, default_context, LDSignature, \
    send_post_request
from app.activitypub.util import users_total, active_half_year, active_month, local_posts, local_comments, \
    post_to_activity, find_actor_or_create_cached, find_liked_object, \
    lemmy_site_data, is_activitypub_request, delete_post_or_comment, community_members, \
    create_post, create_post_reply, update_post_reply_from_activity, \
    update_post_from_activity, undo_vote, post_to_page, find_reported_object, \
    process_report, ensure_domains_match, resolve_remote_post, refresh_community_profile, \
    comment_model_to_json, restore_post_or_comment, ban_user, unban_user, \
    log_incoming_ap, find_community, site_ban_remove_data, community_ban_remove_data, verify_object_from_source, \
    post_replies_for_ap, is_vote, find_instance_id, resolve_remote_post_from_search, proactively_delete_content, \
    process_quote_boost, object_has_missing_fields
from app.community.routes import show_community
from app.community.util import send_to_remote_instance, send_to_remote_instance_fast
from app.constants import *
from app.feed.routes import show_feed
from app.models import User, Community, CommunityJoinRequest, CommunityMember, CommunityBan, ActivityPubLog, Post, \
    PostReply, Instance, AllowedInstances, BannedInstances, utcnow, Site, Notification, \
    ChatMessage, Conversation, UserFollower, UserBlock, Poll, PollChoice, Feed, FeedItem, FeedMember, FeedJoinRequest, \
    IpBan, ActivityBatch, InstanceBan
from app.post.routes import continue_discussion, show_post
from app.shared.tasks import task_selector
from app.user.routes import show_profile
from app.utils import gibberish, get_setting, community_membership, ap_datetime, ip_address, can_downvote, \
    can_upvote, can_create_post, awaken_dormant_instance, shorten_string, can_create_post_reply, sha256_digest, \
    community_moderators, html_to_text, add_to_modlog, instance_banned, get_redis_connection, \
    feed_membership, get_task_session, patch_db_session, \
    blocked_phrases, orjson_response, moderating_communities, joined_communities, moderating_communities_ids, \
    moderating_communities_ids_all_users, publish_sse_event, blocked_users, block_honey_pot


@bp.route('/testredis')
def testredis_get():
    redis_client = get_redis_connection()
    redis_client.set("cowbell", "1", ex=600)
    x = redis_client.get('cowbell')
    if x is not None:
        return "Redis: OK"
    else:
        return "Redis: FAIL"


@bp.route('/.well-known/webfinger')
@cache.cached(timeout=60, query_string=True)
def webfinger():
    if request.args.get('resource'):
        feed = False
        query = request.args.get('resource', '')  # acct:alice@tada.club
        if 'acct:' in query:
            actor = query.split(':')[1].split('@')[0]  # alice
            if actor.startswith('~'):
                feed = True
                actor = actor[1:]
        elif 'https:' in query or 'http:' in query:
            actor = query.split('/')[-1]
        else:
            return 'Webfinger regex failed to match'

        # special case: instance actor
        if actor == current_app.config['SERVER_NAME']:
            webfinger_data = {
                "subject": f"acct:{actor}@{current_app.config['SERVER_NAME']}",
                "aliases": [f"{current_app.config['SERVER_URL']}/actor"],
                "links": [
                    {
                        "rel": "http://webfinger.net/rel/profile-page",
                        "type": "text/html",
                        "href": f"{current_app.config['SERVER_URL']}/about"
                    },
                    {
                        "rel": "self",
                        "type": "application/activity+json",
                        "href": f"{current_app.config['SERVER_URL']}/actor",
                    }
                ]
            }
            resp = jsonify(webfinger_data)
            resp.content_type = 'application/jrd+json'
            resp.headers.set('Cache-Control', 'public, max-age=15')
            resp.headers.add_header('Access-Control-Allow-Origin', '*')
            return resp

        object = None
        if not feed:
            # look for the User first, then the Community, then the Feed that matches
            type = 'Person'
            object = User.query.filter(
                or_(func.lower(User.user_name) == actor.strip().lower(), func.lower(User.alt_user_name) == actor.strip().lower())).filter_by(deleted=False,
                                                                                                     banned=False,
                                                                                                     ap_id=None).first()
            if object is None:
                profile_id = f"{current_app.config['SERVER_URL']}/c/{actor.strip().lower()}"
                object = Community.query.filter_by(ap_profile_id=profile_id, ap_id=None, local_only=False).first()
                type = 'Group'
                if object is None:
                    object = Feed.query.filter_by(name=actor.strip(), ap_id=None).first()
                    type = 'Feed'
        else:
            object = Feed.query.filter_by(name=actor.strip(), ap_id=None).first()
            type = 'Feed'

        if object is None:
            return ''

        webfinger_data = {
            "subject": f"acct:{actor.strip()}@{current_app.config['SERVER_NAME']}",
            "aliases": [object.public_url()],
            "links": [
                {
                    "rel": "http://webfinger.net/rel/profile-page",
                    "type": "text/html",
                    "href": object.public_url()
                },
                {
                    "rel": "self",
                    "type": "application/activity+json",
                    "href": object.public_url(),
                    "properties": {
                        "https://www.w3.org/ns/activitystreams#type": type
                    }
                }
            ]
        }
        if isinstance(object, User):
            webfinger_data['links'].append({
              "rel": "https://w3id.org/fep/3b86/Create",
              "template": f"{current_app.config['SERVER_URL']}/share?url=" + '{object}'
            })
        elif isinstance(object, Community):
            webfinger_data['links'].append({
                "rel": "https://w3id.org/fep/3b86/Follow",
                "template": "{object}/subscribe"
            })
        resp = jsonify(webfinger_data)
        resp.headers.add_header('Access-Control-Allow-Origin', '*')
        resp.headers.set('Cache-Control', 'public, max-age=15')
        resp.content_type = 'application/jrd+json'
        return resp
    else:
        abort(404)


@bp.route('/.well-known/nodeinfo')
@cache.cached(timeout=600)
def nodeinfo():
    nodeinfo_data = {"links": [{"rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                                "href": f"{current_app.config['SERVER_URL']}/nodeinfo/2.0"},
                               {"rel": "http://nodeinfo.diaspora.software/ns/schema/2.1",
                                "href": f"{current_app.config['SERVER_URL']}/nodeinfo/2.1"},
                               ]}
    return jsonify(nodeinfo_data)


@bp.route('/.well-known/host-meta')
@cache.cached(timeout=600)
def host_meta():
    resp = make_response(
        '<?xml version="1.0" encoding="UTF-8"?>\n<XRD xmlns="http://docs.oasis-open.org/ns/xri/xrd-1.0">\n<Link rel="lrdd" template="https://' +
        current_app.config["SERVER_NAME"] + '/.well-known/webfinger?resource={uri}"/>\n</XRD>')
    resp.content_type = 'application/xrd+xml; charset=utf-8'
    return resp


@bp.route('/nodeinfo/2.0')
@bp.route('/nodeinfo/2.0.json')
@cache.cached(timeout=600)
def nodeinfo2():
    nodeinfo_data = {
        "version": "2.0",
        "software": {
            "name": "piefed",
            "version": current_app.config["VERSION"]
        },
        "protocols": [
            "activitypub"
        ],
        "usage": {
            "users": {
                "total": users_total(),
                "activeHalfyear": active_half_year(),
                "activeMonth": active_month()
            },
            "localPosts": local_posts(),
            "localComments": local_comments()
        },
        "openRegistrations": g.site.registration_mode != 'Closed'
    }
    return jsonify(nodeinfo_data)


@bp.route('/nodeinfo/2.1')
@bp.route('/nodeinfo/2.1.json')
@cache.cached(timeout=600)
def nodeinfo21():
    nodeinfo_data = {
        "version": "2.1",
        "software": {
            "name": "piefed",
            "version": current_app.config["VERSION"],
            "repository": "https://codeberg.org/rimu/pyfedi",
            "homepage": "https://join.piefed.social"
        },
        "protocols": [
            "activitypub"
        ],
        "usage": {
            "users": {
                "total": users_total(),
                "activeHalfyear": active_half_year(),
                "activeMonth": active_month()
            },
            "localPosts": local_posts(),
            "localComments": local_comments()
        },
        "openRegistrations": g.site.registration_mode != 'Closed',
        "services": {
            "inbound": [],
            "outbound": []
        },
        "metadata": {
            "capabilities": [
                "fep-1b12",
                "fep-1a11",
                "fep-7888",
                "fep-c0e0",
                "fep-5feb"
            ]
        }
    }
    return jsonify(nodeinfo_data)


@bp.route('/api/v1/instance')
@cache.cached(timeout=600)
def api_v1_instance():
    retval = {
        'title': g.site.name,
        'uri': current_app.config['SERVER_NAME'],
        'stats': {
            "user_count": users_total(),
            "status_count": local_posts() + local_comments(),
            "domain_count": 1
        },
        'registrations': g.site.registration_mode != 'Closed',
        'approval_required': g.site.registration_mode == 'RequireApplication'
    }
    return jsonify(retval)


@bp.route('/api/v1/instance/domain_blocks')
@cache.cached(timeout=600)
def domain_blocks():
    use_allowlist = get_setting('use_allowlist', False)
    if use_allowlist:
        return jsonify([])
    else:
        retval = []
        for domain in BannedInstances.query.all():
            retval.append({
                'domain': domain.domain,
                'digest': sha256_digest(domain.domain),
                'severity': 'suspend',
                'comment': domain.reason if domain.reason else ''
            })
    return jsonify(retval)


@bp.route('/api/is_ip_banned', methods=['POST'])
@limiter.limit("60 per 1 minutes", methods=['POST'])
def api_is_ip_banned():
    result = []
    counter = 0
    for ip in request.form.get('ip_addresses').split(','):
        banned_ip = IpBan.query.filter(IpBan.ip_address == ip).first()
        result.append(banned_ip is not None)
        counter += 1
        if counter >= 10:
            break
    return jsonify(result)


@bp.route('/api/is_email_banned', methods=['POST'])
@limiter.limit("60 per 1 minutes", methods=['POST'])
def api_is_email_banned():
    result = []
    counter = 0
    for email in request.form.get('emails').split(','):
        user_id = db.session.query(User.id).filter(User.banned == True, User.email == email.strip(),
                                                   User.ap_id == None).scalar()
        result.append(user_id is not None)
        counter += 1
        if counter >= 10:
            break

    return jsonify(result)


@bp.route('/api/v3/site')
@cache.cached(timeout=600)
def lemmy_site():
    return orjson_response(lemmy_site_data())


@bp.route('/api/v3/federated_instances')
@cache.cached(timeout=600)
def lemmy_federated_instances():
    instances = Instance.query.filter(Instance.id != 1, Instance.gone_forever == False).all()
    linked = []
    allowed = []
    blocked = []
    for instance in AllowedInstances.query.all():
        allowed.append({"id": instance.id, "domain": instance.domain, "published": utcnow(), "updated": utcnow()})
    for instance in BannedInstances.query.all():
        blocked.append({"id": instance.id, "domain": instance.domain, "published": utcnow(), "updated": utcnow()})
    for instance in instances:
        instance_data = {"id": instance.id, "domain": instance.domain, "published": instance.created_at.isoformat(),
                         "updated": instance.updated_at.isoformat()}
        if instance.software:
            instance_data['software'] = instance.software
        if instance.version:
            instance_data['version'] = instance.version
        if not any(blocked_instance.get('domain') == instance.domain for blocked_instance in blocked):
            linked.append(instance_data)
    return orjson_response({
        "federated_instances": {
            "linked": linked,
            "allowed": allowed,
            "blocked": blocked
        }
    })


@bp.route('/u/<actor>', methods=['GET', 'HEAD'])
def user_profile(actor):
    """ Requests to this endpoint can be for a JSON representation of the user, or a HTML rendering of their profile.
    The two types of requests are differentiated by the header """
    actor = actor.strip()

    # admins can view deleted accounts
    if current_user.is_authenticated and current_user.is_admin():
        if '@' in actor:
            user: User = User.query.filter_by(ap_id=actor.lower()).first()
        else:
            user: User = User.query.filter(or_(func.lower(User.user_name) == actor.lower())).filter_by(ap_id=None).first()
            if user is None:
                user = User.query.filter_by(ap_profile_id=f'{current_app.config["SERVER_URL"]}/u/{actor.lower()}', ap_id=None).first()
    else:
        if '@' in actor:
            user: User = User.query.filter_by(ap_id=actor.lower()).first()
        else:
            user: User = User.query.filter(or_(func.lower(User.user_name) == actor.lower())).filter_by(ap_id=None).first()
            if user is None:
                user = User.query.filter_by(ap_profile_id=f'{current_app.config["SERVER_URL"]}/u/{actor.lower()}', ap_id=None).first()

    if user is not None:
        if request.method == 'HEAD':
            if is_activitypub_request():
                resp = jsonify('')
                resp.content_type = 'application/activity+json'
                return resp
            else:
                return ''
        if is_activitypub_request():
            actor_data = {"@context": default_context(),
                          "type": "Person" if not user.bot else "Service",
                          "id": user.public_url(),
                          "preferredUsername": actor,
                          "name": user.title if user.title else user.user_name,
                          "inbox": f"{user.public_url()}/inbox",
                          "outbox": f"{user.public_url()}/outbox",
                          "discoverable": user.searchable,
                          "indexable": user.indexable,
                          "acceptPrivateMessages": user.accept_private_messages,
                          "manuallyApprovesFollowers": False if not user.ap_manually_approves_followers else user.ap_manually_approves_followers,
                          "publicKey": {
                              "id": f"{user.public_url()}#main-key",
                              "owner": user.public_url(),
                              "publicKeyPem": user.public_key
                          },
                          "endpoints": {
                              "sharedInbox": f"{current_app.config['SERVER_URL']}/inbox"
                          },
                          "published": ap_datetime(user.created),
                          }

            if user.avatar_id is not None:
                avatar_image = user.avatar_image()
                if avatar_image.startswith('http'):
                    actor_data["icon"] = {
                        "type": "Image",
                        "url": user.avatar_image()
                    }
                else:
                    actor_data["icon"] = {
                        "type": "Image",
                        "url": f"{current_app.config['SERVER_URL']}{user.avatar_image()}"
                    }
            if user.cover_id is not None:
                cover_image = user.cover_image()
                if cover_image.startswith('http'):
                    actor_data["image"] = {
                        "type": "Image",
                        "url": user.cover_image()
                    }
                else:
                    actor_data["image"] = {
                        "type": "Image",
                        "url": f"{current_app.config['SERVER_URL']}{user.cover_image()}"
                    }
            if user.about_html:
                actor_data['summary'] = user.about_html
                actor_data['source'] = {'content': user.about, 'mediaType': 'text/markdown'}
            if user.matrix_user_id:
                actor_data['matrixUserId'] = user.matrix_user_id
            if user.extra_fields.count() > 0:
                actor_data['attachment'] = []
                for field in user.extra_fields:
                    actor_data['attachment'].append({'type': 'PropertyValue',
                                                     'name': field.label,
                                                     'value': field.text})
            resp = jsonify(actor_data)
            resp.content_type = 'application/activity+json'
            resp.headers.set('Cache-Control', 'public, max-age=15')
            resp.headers.set('Vary', 'Accept')
            resp.headers.set('Link',
                             f'<https://{current_app.config["SERVER_NAME"]}/u/{actor}>; rel="alternate"; type="text/html"')
            return resp
        else:
            return show_profile(user)
    else:
        abort(404)


@bp.route('/u/<actor>/outbox', methods=['GET'])
def user_outbox(actor):
    outbox = {
        "@context": default_context(),
        'type': 'OrderedCollection',
        'id': f"{current_app.config['SERVER_URL']}/u/{actor}/outbox",
        'orderedItems': [],
        'totalItems': 0
    }
    resp = jsonify(outbox)
    resp.content_type = 'application/activity+json'
    resp.headers.set('Cache-Control', 'public, max-age=10')
    resp.headers.set('Vary', 'Accept')
    return resp


@bp.route('/c/<actor>', methods=['GET'])
def community_profile(actor):
    """ Requests to this endpoint can be for a JSON representation of the community, or a HTML rendering of it.
        The two types of requests are differentiated by the header """
    actor = actor.strip()
    if '@' in actor:
        # don't provide activitypub info for remote communities
        if 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get('Accept', ''):
            abort(400)
        community: Community = Community.query.filter_by(ap_id=actor.lower(), banned=False).first()
    else:
        profile_id = f"https://{current_app.config['SERVER_NAME']}/c/{actor.lower()}"
        community: Community = Community.query.filter_by(ap_profile_id=profile_id, ap_id=None).first()
    if community is not None:
        if is_activitypub_request():
            if community.local_only or community.private:
                abort(403)
            server = current_app.config['SERVER_NAME']
            actor_data = {"@context": default_context(),
                          "type": "Group",
                          "id": f"https://{server}/c/{actor}",
                          "name": community.title,
                          "postingWarning": community.posting_warning,
                          "sensitive": True if community.nsfw or community.nsfl else False,
                          "preferredUsername": actor,
                          "inbox": f"https://{server}/c/{actor}/inbox",
                          "outbox": f"https://{server}/c/{actor}/outbox",
                          "followers": f"https://{server}/c/{actor}/followers",
                          "moderators": f"https://{server}/c/{actor}/moderators",
                          "featured": f"https://{server}/c/{actor}/featured",
                          "attributedTo": f"https://{server}/c/{actor}/moderators",
                          "postingRestrictedToMods": community.restricted_to_mods or community.local_only,
                          "questionAnswer": community.question_answer,
                          "defaultPostType": community.default_post_type,
                          "newModsWanted": community.new_mods_wanted,
                          "privateMods": community.private_mods,
                          "genAI": community.ai_generated,
                          "url": f"https://{server}/c/{actor}",
                          "publicKey": {
                              "id": f"https://{server}/c/{actor}#main-key",
                              "owner": f"https://{server}/c/{actor}",
                              "publicKeyPem": community.public_key
                          },
                          "endpoints": {
                              "sharedInbox": f"{current_app.config['SERVER_URL']}/inbox"
                          },
                          "published": ap_datetime(community.created_at),
                          "updated": ap_datetime(community.last_active),
                          "lemmy:tagsForPosts": community.flair_for_ap(version=1),
                          "tag": community.flair_for_ap(version=2),
                          "postUrlType": community.post_url_type if community.post_url_type else "friendly",
                          }
            if community.description_html:
                actor_data["summary"] = community.description_html
                actor_data['source'] = {'content': community.description, 'mediaType': 'text/markdown'}
            if community.theme:
                actor_data["theme"] = community.theme
            if community.icon_id is not None:
                icon_image = community.icon_image()
                if icon_image.startswith('http'):
                    actor_data["icon"] = {
                        "type": "Image",
                        "url": community.icon_image()
                    }
                else:
                    actor_data["icon"] = {
                        "type": "Image",
                        "url": f"{current_app.config['SERVER_URL']}{community.icon_image()}"
                    }
            if community.image_id is not None:
                header_image = community.header_image()
                if header_image.startswith('http'):
                    actor_data["image"] = {
                        "type": "Image",
                        "url": community.header_image()
                    }
                else:
                    actor_data["image"] = {
                        "type": "Image",
                        "url": f"{current_app.config['SERVER_URL']}{community.header_image()}"
                    }
            actor_data['language'] = []
            for language in community.languages:
                actor_data['language'].append({'identifier': language.code, 'name': language.name})
            resp = jsonify(actor_data)
            resp.content_type = 'application/activity+json'
            resp.headers.set('Cache-Control', 'public, max-age=30')
            resp.headers.set('Vary', 'Accept')
            resp.headers.set('Link',
                             f'<https://{current_app.config["SERVER_NAME"]}/c/{actor}>; rel="alternate"; type="text/html"')
            return resp
        else:  # browser request - return html
            return show_community(community)
    else:
        if is_activitypub_request():
            abort(404)
        elif current_user.is_authenticated and "@" in actor:
            flash(_("Community not found on this instance"))
            part = actor.split('@')
            return redirect(url_for("community.lookup", community=part[0], domain=part[1]))
        elif current_user.is_authenticated:
            flash(_("Community not found on this instance"))
            return redirect(url_for("community.add_local"))
        else:
            abort(404)


@bp.route('/c/<actor>/subscribe', methods=['GET'])
def community_profile_subscribe(actor):
    # For use by FEP 3b86, activity intents. See webfinger()
    return redirect(url_for('community.subscribe', actor=actor))


@bp.route('/inbox', methods=['POST'])
def shared_inbox():
    from app import redis_client
    try:
        request_json = request.get_json(force=True)
    except werkzeug.exceptions.BadRequest as e:
        log_incoming_ap('', APLOG_NOTYPE, APLOG_FAILURE, None, 'Unable to parse json body: ' + e.description +  str(request.user_agent))
        return '', 400
    except BlockingIOError:
        log_incoming_ap('', APLOG_NOTYPE, APLOG_FAILURE, None, 'Client disconnected while sending JSON body ' + str(request.user_agent))
        return '', 400

    if request_json is None:
        log_incoming_ap('', APLOG_NOTYPE, APLOG_FAILURE, None, 'Empty JSON body ' + str(request.user_agent))
        return "", 400

    pause_federation = redis_client.get('pause_federation')
    if pause_federation == '1': # temporary pause as this instance is overloaded
        return '', 429
    elif pause_federation == '666':
        return '', 410 # this instance has been permanently closed down, everyone should stop sending to it.

    g.site = Site.query.get(1)  # g.site is not initialized by @app.before_request when request.path == '/inbox'
    store_ap_json = g.site.log_activitypub_json or False
    saved_json = request_json if store_ap_json else None

    if not 'id' in request_json or not 'type' in request_json or not 'actor' in request_json or not 'object' in request_json:
        log_incoming_ap('', APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Missing minimum expected fields in JSON')
        return '', 200

    id = request_json['id']
    if request_json['type'] == 'Announce' and isinstance(request_json['object'], dict):
        object = request_json['object']
        if object_has_missing_fields(object):
            if 'type' in object and (object['type'] == 'Page' or object['type'] == 'Note'):
                log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Intended for Mastodon')
            else:
                log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json,
                                'Missing minimum expected fields in JSON Announce object')
            return '', 200

        if 'actor' in object and isinstance(object['actor'], str) and object['actor'].startswith(
                'https://' + current_app.config['SERVER_NAME']):
            log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, saved_json,
                            'Activity about local content which is already present')
            return '', 200

        id = object['id']


    if redis_client.exists(id):  # Something is sending same activity multiple times
        log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, saved_json, 'Already aware of this activity')
        return '', 200
    redis_client.set(id, 1, ex=90)  # Save the activity ID into redis, to avoid duplicate activities

    # Ignore unutilised PeerTube activity
    if isinstance(request_json['actor'], str) and request_json['actor'].endswith('accounts/peertube'):
        log_incoming_ap(id, APLOG_PT_VIEW, APLOG_IGNORED, saved_json, 'PeerTube View or CacheFile activity')
        return ''

    try:
        HttpSignature.precheck(request)
    except VerificationFormatError as e:
        log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Precheck failed: ' + str(e))

    # Ignore account deletion requests from users that do not already exist here
    account_deletion = False
    if request_json['type'] == 'Delete' and 'object' in request_json and isinstance(request_json['object'], str) and \
            request_json['actor'] == request_json['object']:
        account_deletion = True
        actor = db.session.query(User).filter_by(ap_profile_id=request_json['actor'].lower()).first()
        if not actor:
            log_incoming_ap(id, APLOG_DELETE, APLOG_IGNORED, saved_json, 'Does not exist here')
            return '', 200
    else:
        actor = find_actor_or_create_cached(request_json['actor'])

    if not actor:
        actor_name = request_json['actor']
        log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, f'Actor could not be found 1 - : {actor_name}, actor object: {actor}')
        return '', 200

    #if actor.is_local():  # should be impossible (can be Announced back, but not sent without access to privkey)
    #    log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'ActivityPub activity from a local actor')
    #    return '', 200

    bounced = False
    try:
        HttpSignature.verify_request(request, actor.public_key, skip_date=True)
    except VerificationError as e:
        bounced = True
        # HTTP sig will fail if a.gup.pe or PeerTube have bounced a request, so check LD sig instead
        if 'signature' in request_json:
            try:
                LDSignature.verify_signature(request_json, actor.public_key)
            except VerificationError as e:
                log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Could not verify LD signature: ' + str(e))
                return '', 400
        elif (
                actor.ap_profile_id == 'https://fediseer.com/api/v1/user/fediseer' and  # accept unsigned chat message from fediseer for API key
                request_json['type'] == 'Create' and isinstance(request_json['object'], dict) and
                'type' in request_json['object'] and request_json['object']['type'] == 'ChatMessage'):
            ...
        # no HTTP sig, and no LD sig, so reduce the inner object to just its remote ID, and then fetch it and check it in process_inbox_request()
        elif ((request_json['type'] == 'Create' or request_json['type'] == 'Update') and
              isinstance(request_json['object'], dict) and 'id' in request_json['object'] and isinstance(
                    request_json['object']['id'], str)):
            request_json['object'] = request_json['object']['id']
        else:
            log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Could not verify HTTP signature: ' + str(e))
            return '', 400

    if actor.instance_id:
        actor.instance.last_seen = utcnow()
        actor.instance.dormant = False
        actor.instance.gone_forever = False
        actor.instance.failures = 0
        actor.instance.ip_address = ip_address() if not bounced else ''
    db.session.commit()

    # When a user is deleted, the only way to be fairly sure they get deleted everywhere is to tell the whole fediverse.
    # Earlier check means this is only for users that already exist, processing it here means that http signature will have been verified
    if account_deletion == True:
        if current_app.debug:
            process_delete_request(request_json, store_ap_json)
        else:
            process_delete_request.delay(request_json, store_ap_json)
        return ''

    if current_app.debug:
        process_inbox_request(request_json, store_ap_json)
    else:
        process_inbox_request.delay(request_json, store_ap_json)

    return ''


@bp.route('/site_inbox', methods=['POST'])
def site_inbox():
    return shared_inbox()


@bp.route('/u/<actor>/inbox', methods=['POST'])
def user_inbox(actor):
    return shared_inbox()


@bp.route('/c/<actor>/inbox', methods=['POST'])
def community_inbox(actor):
    return shared_inbox()


def replay_inbox_request(request_json):
    if not 'id' in request_json or not 'type' in request_json or not 'actor' in request_json or not 'object' in request_json:
        log_incoming_ap('', APLOG_NOTYPE, APLOG_FAILURE, request_json, 'REPLAY: Missing minimum expected fields in JSON')
        return

    id = request_json['id']
    if request_json['type'] == 'Announce' and isinstance(request_json['object'], dict):
        object = request_json['object']
        if object_has_missing_fields(object):
            if 'type' in object and (object['type'] == 'Page' or object['type'] == 'Note'):
                log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, request_json, 'REPLAY: Intended for Mastodon')
            else:
                log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, request_json, 'REPLAY: Missing minimum expected fields in JSON Announce object')
            return

        if 'actor' in object and isinstance(object['actor'], str) and object['actor'].startswith(
                'https://' + current_app.config['SERVER_NAME']):
            log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, request_json, 'REPLAY: Activity about local content which is already present')
            return

    # Ignore unutilised PeerTube activity
    if 'actor' in request_json and isinstance(request_json['actor'], str) and request_json['actor'].endswith('accounts/peertube'):
        log_incoming_ap(id, APLOG_PT_VIEW, APLOG_IGNORED, request_json, 'REPLAY: PeerTube View or CacheFile activity')
        return

    # Ignore account deletion requests from users that do not already exist here
    account_deletion = False
    if (request_json['type'] == 'Delete' and
            'object' in request_json and isinstance(request_json['object'], str) and
            request_json['actor'] == request_json['object']):
        account_deletion = True
        actor = db.session.query(User).filter_by(ap_profile_id=request_json['actor'].lower()).first()
        if not actor:
            log_incoming_ap(id, APLOG_DELETE, APLOG_IGNORED, request_json, 'REPLAY: Does not exist here')
            return
    else:
        actor = find_actor_or_create_cached(request_json['actor'])

    if not actor:
        actor_name = request_json['actor']
        log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, request_json, f'REPLAY: Actor could not be found 1: {actor_name}')
        return

    if actor.is_local():  # should be impossible (can be Announced back, but not sent back without access to privkey)
        log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, request_json, 'REPLAY: ActivityPub activity from a local actor')
        return

    # When a user is deleted, the only way to be fairly sure they get deleted everywhere is to tell the whole fediverse.
    if account_deletion == True:
        process_delete_request(request_json, True)
        return

    process_inbox_request(request_json, True)

    return


@celery.task
def process_inbox_request(request_json, store_ap_json):
    with current_app.app_context():
        session = get_task_session()
        try:
            # patch_db_session makes all db.session.whatever() use the session created with get_task_session, to guarantee proper connection clean-up at the end of the task.
            # although process_inbox_request uses session instead of db.session, many of the functions it calls, like find_actor_or_create_cached, do not which makes this necessary.
            with patch_db_session(session):
                from app import redis_client
                # For an Announce, Accept, or Reject, we have the community/feed, and need to find the user
                # For everything else, we have the user, and need to find the community/feed
                # Benefits of always using request_json['actor']:
                #   It's the actor who signed the request, and whose signature has been verified
                #   Because of the earlier check, we know that they already exist, and so don't need to check again
                #   Using actors from inner objects has a vulnerability to spoofing attacks (e.g. if 'attributedTo' doesn't match the 'Create' actor)
                saved_json = request_json if store_ap_json else None
                id = request_json['id']
                actor_id = request_json['actor']
                if isinstance(actor_id, dict):  # Discourse does this
                    actor_id = actor_id['id']
                feed = community = None
                if request_json['type'] == 'Announce' or request_json['type'] == 'Accept' or request_json['type'] == 'Reject':
                    community = find_actor_or_create_cached(actor_id, community_only=True, create_if_not_found=False)
                    if not community:
                        feed = find_actor_or_create_cached(actor_id, feed_only=True, create_if_not_found=False)
                    if not community and not feed:
                        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Actor was not a feed or a community')
                        return
                else:
                    actor = find_actor_or_create_cached(actor_id)
                    if actor and isinstance(actor, User):
                        user = actor
                    elif actor and isinstance(actor, Community):  # Process a few activities from NodeBB and a.gup.pe
                        if request_json['type'] == 'Add' or request_json['type'] == 'Remove':
                            log_incoming_ap(id, APLOG_ADD, APLOG_IGNORED, saved_json, 'NodeBB Topic Management')
                            return
                        elif request_json['type'] == 'Update' and 'type' in request_json['object']:
                            if request_json['object']['type'] == 'Group':
                                community = actor  # process it same as Update/Group from Lemmy
                            elif request_json['object']['type'] == 'OrderedCollection':
                                log_incoming_ap(id, APLOG_ADD, APLOG_IGNORED, saved_json, 'Follower count update from a.gup.pe')
                                return
                            else:
                                log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Unexpected Update activity from Group')
                                return
                        else:
                            log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Unexpected activity from Group')
                            return
                    else:
                        log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Actor was not a user or a community')
                        return

                # Announce: take care of inner objects that are just a URL (PeerTube, a.gup.pe), or find the user if the inner object is a dict
                if request_json['type'] == 'Announce':
                    if isinstance(request_json['object'], str):
                        if request_json['object'].startswith('https://' + current_app.config['SERVER_NAME']):
                            log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, saved_json, 'Activity about local content which is already present')
                            return
                        post = resolve_remote_post(request_json['object'], community, id, store_ap_json)
                        if post:
                            log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_SUCCESS, request_json)
                        else:
                            log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, request_json, 'Could not resolve post')
                        return
                    elif isinstance(request_json['object'], list):  # PieFed can Announce an unlimited amount of objects at once, as long as they are all from the same community.
                        for obj in request_json['object']:
                            # Convert each object into the list into an identical Announce activity containing just that object
                            fake_activity = request_json.copy()
                            fake_activity['object'] = obj
                            process_inbox_request(fake_activity, store_ap_json)  # Process the Announce (with single object) as normal
                        return
                    elif 'type' in request_json['object'] and request_json['object']['type'] == 'OrderedCollection':
                        for obj in request_json['object']['orderedItems']:
                            fake_activity = request_json.copy()
                            fake_activity['object'] = obj
                            process_inbox_request(fake_activity, store_ap_json)  # Process the Announce (with single object) as normal
                        return
                    if not feed:
                        user = find_actor_or_create_cached(request_json['object']['actor'])
                        if user and isinstance(user, User):
                            if user.banned:
                                log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, f'{user.ap_id} is banned')
                                return
                        else:
                            log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Blocked or unfound user for Announce object actor ' + str(request_json['object']['actor']))
                            return
                    else:
                        user = None

                    # Now that we have the community and the user from an Announce, we can save repeating code by removing it
                    # core_activity is checked for its Type, but the original request_json is sometimes passed to any other functions
                    announced = True
                    core_activity = request_json['object']
                else:
                    announced = False
                    core_activity = request_json

                # Follow: remote user wants to join/follow one of our users, communities, or feeds
                if core_activity['type'] == 'Follow':
                    target_ap_id = core_activity['object']
                    follow_id = core_activity['id']
                    target = find_actor_or_create_cached(target_ap_id)
                    if not target:
                        log_incoming_ap(id, APLOG_FOLLOW, APLOG_FAILURE, saved_json, 'Could not find target of Follow')
                        return
                    if isinstance(target, Community):
                        community = target
                        reject_follow = False
                        if community.local_only:
                            log_incoming_ap(id, APLOG_FOLLOW, APLOG_FAILURE, saved_json, 'Local only cannot be followed by remote users')
                            reject_follow = True
                        else:
                            # check if user is banned from this community
                            user_banned = session.query(CommunityBan).filter_by(user_id=user.id, community_id=community.id).first()
                            if user_banned:
                                log_incoming_ap(id, APLOG_FOLLOW, APLOG_FAILURE, saved_json, 'Remote user has been banned')
                                reject_follow = True
                        if reject_follow:
                            # send reject message to deny the follow
                            reject = {"@context": default_context(), "actor": community.public_url(),
                                      "to": [user.public_url()],
                                      "object": {"actor": user.public_url(), "to": None, "object": community.public_url(),
                                                 "type": "Follow", "id": follow_id},
                                      "type": "Reject",
                                      "id": f"{current_app.config['SERVER_URL']}/activities/reject/" + gibberish(32)}
                            send_post_request(user.ap_inbox_url, reject, community.private_key, f"{community.public_url()}#main-key")
                        else:
                            existing_member = session.query(CommunityMember).filter_by(user_id=user.id, community_id=community.id).first()
                            if not existing_member:
                                member = CommunityMember(user_id=user.id, community_id=community.id)
                                session.add(member)
                                community.subscriptions_count += 1
                                community.last_active = utcnow()
                                user.last_seen = utcnow()
                                session.commit()
                                cache.delete_memoized(community_membership, user, community)
                                # send accept message to acknowledge the follow
                                accept = {"@context": default_context(), "actor": community.public_url(),
                                          "to": [user.public_url()],
                                          "object": {"actor": user.public_url(), "to": None,
                                                     "object": community.public_url(), "type": "Follow", "id": follow_id},
                                          "type": "Accept",
                                          "id": f"{current_app.config['SERVER_URL']}/activities/accept/" + gibberish(32)}
                                send_post_request(user.ap_inbox_url, accept, community.private_key, f"{community.public_url()}#main-key")
                                log_incoming_ap(id, APLOG_FOLLOW, APLOG_SUCCESS, saved_json)
                        return
                    elif isinstance(target, Feed):
                        feed = target
                        reject_follow = False
                        # if the feed is not public send a reject
                        # it should not get here as we wont have a subscribe option on non-public feeds,
                        # but just for cya its here.
                        if not feed.public:
                            reject_follow = True

                        if reject_follow:
                            # send reject message to deny the follow
                            reject = {"@context": default_context(), "actor": feed.public_url(), "to": [user.public_url()],
                                      "object": {"actor": user.public_url(), "to": None, "object": feed.public_url(),
                                                 "type": "Follow", "id": follow_id},
                                      "type": "Reject",
                                      "id": f"{current_app.config['SERVER_URL']}/activities/reject/" + gibberish(32)}
                            send_post_request(user.ap_inbox_url, reject, feed.private_key, f"{feed.public_url()}#main-key")
                        else:
                            if feed_membership(user, feed) != SUBSCRIPTION_MEMBER:
                                member = FeedMember(user_id=user.id, feed_id=feed.id)
                                session.add(member)
                                feed.subscriptions_count += 1
                                session.commit()
                                cache.delete_memoized(feed_membership, user, feed)
                                # send accept message to acknowledge the follow
                                accept = {"@context": default_context(), "actor": feed.public_url(),
                                          "to": [user.public_url()],
                                          "object": {"actor": user.public_url(), "to": None, "object": feed.public_url(),
                                                     "type": "Follow", "id": follow_id},
                                          "type": "Accept",
                                          "id": f"{current_app.config['SERVER_URL']}/activities/accept/" + gibberish(32)}
                                send_post_request(user.ap_inbox_url, accept, feed.private_key,
                                                  f"{feed.public_url()}#main-key")
                                log_incoming_ap(id, APLOG_FOLLOW, APLOG_SUCCESS, saved_json)
                        return
                    elif isinstance(target, User):
                        local_user = target
                        remote_user = user
                        if not local_user.is_local():
                            log_incoming_ap(id, APLOG_FOLLOW, APLOG_FAILURE, saved_json,
                                            'Follow request for remote user received')
                            return
                        existing_follower = session.query(UserFollower).filter_by(local_user_id=local_user.id,
                                                                         remote_user_id=remote_user.id).first()
                        if not existing_follower:
                            auto_accept = not local_user.ap_manually_approves_followers
                            new_follower = UserFollower(local_user_id=local_user.id, remote_user_id=remote_user.id,
                                                        is_accepted=auto_accept)
                            if not local_user.ap_followers_url:
                                local_user.ap_followers_url = local_user.public_url() + '/followers'
                            session.add(new_follower)
                            session.commit()
                            accept = {"@context": default_context(), "actor": local_user.public_url(),
                                      "to": [remote_user.public_url()],
                                      "object": {"actor": remote_user.public_url(), "to": None,
                                                 "object": local_user.public_url(), "type": "Follow", "id": follow_id},
                                      "type": "Accept",
                                      "id": f"{current_app.config['SERVER_URL']}/activities/accept/" + gibberish(32)}
                            send_post_request(remote_user.ap_inbox_url, accept, local_user.private_key,
                                              f"{local_user.public_url()}#main-key")
                            log_incoming_ap(id, APLOG_FOLLOW, APLOG_SUCCESS, saved_json)
                    return

                # Accept: remote server is accepting our previous follow request
                if core_activity['type'] == 'Accept':
                    user = None
                    if isinstance(core_activity['object'], str):  # a.gup.pe accepts using a string with the ID of the follow request
                        join_request_parts = core_activity['object'].split('/')
                        try:
                            join_request = session.query(CommunityJoinRequest).filter_by(uuid=join_request_parts[-1]).first()
                        except Exception:  # old style join requests were just a number
                            session.rollback()
                            join_request = session.query(CommunityJoinRequest).get(join_request_parts[-1])
                        if join_request:
                            user = session.query(User).get(join_request.user_id)
                    elif core_activity['object']['type'] == 'Follow':
                        user = find_actor_or_create_cached(core_activity['object']['actor'])
                        if user and user.banned:
                            log_incoming_ap(id, APLOG_ACCEPT, APLOG_FAILURE, saved_json, f'{user.ap_id} is banned')
                            return
                    if not user:
                        log_incoming_ap(id, APLOG_ACCEPT, APLOG_FAILURE, saved_json, 'Could not find recipient of Accept')
                        return

                    if community:
                        join_request = session.query(CommunityJoinRequest).filter_by(user_id=user.id, community_id=community.id).first()
                        if join_request:
                            try:
                                existing_membership = session.query(CommunityMember).filter_by(user_id=join_request.user_id,
                                                                                      community_id=join_request.community_id).first()
                                if not existing_membership:
                                    # check if the join request was a result of a feed join
                                    joined_via_feed = join_request.joined_via_feed
                                    member = CommunityMember(user_id=join_request.user_id,
                                                             community_id=join_request.community_id,
                                                             joined_via_feed=joined_via_feed)
                                    session.add(member)
                                    if User.query.get(join_request.user_id).bot is False:
                                        community.subscriptions_count += 1
                                    community.last_active = utcnow()
                                    session.commit()
                                    cache.delete_memoized(community_membership, user, community)
                                log_incoming_ap(id, APLOG_ACCEPT, APLOG_SUCCESS, saved_json)
                            except IntegrityError:
                                session.rollback()
                                # Membership already exists, just log success and continue
                                log_incoming_ap(id, APLOG_ACCEPT, APLOG_SUCCESS, saved_json, "Membership already exists")
                    elif feed:
                        join_request = session.query(FeedJoinRequest).filter_by(user_id=user.id, feed_id=feed.id).first()
                        if join_request:
                            existing_membership = session.query(FeedMember).filter_by(user_id=join_request.user_id,
                                                                             feed_id=join_request.feed_id).first()
                            if not existing_membership:
                                member = FeedMember(user_id=join_request.user_id, feed_id=join_request.feed_id)
                                session.add(member)
                                feed.subscriptions_count += 1
                                session.commit()
                                cache.delete_memoized(feed_membership, user, feed)
                            log_incoming_ap(id, APLOG_ACCEPT, APLOG_SUCCESS, saved_json)
                    return

                # Reject: remote server is rejecting our previous follow request
                if core_activity['type'] == 'Reject':
                    if core_activity['object']['type'] == 'Follow':
                        user = find_actor_or_create_cached(core_activity['object']['actor'])
                        if not user:
                            log_incoming_ap(id, APLOG_ACCEPT, APLOG_FAILURE, saved_json, 'Could not find recipient of Reject')
                            return

                        if community:
                            join_request = session.query(CommunityJoinRequest).filter_by(user_id=user.id,
                                                                                community_id=community.id).first()
                            if join_request:
                                session.delete(join_request)
                            existing_membership = session.query(CommunityMember).filter_by(user_id=user.id,
                                                                                  community_id=community.id).first()
                            if existing_membership:
                                session.delete(existing_membership)
                                cache.delete_memoized(community_membership, user, community)
                            session.commit()
                            log_incoming_ap(id, APLOG_ACCEPT, APLOG_SUCCESS, saved_json)
                        elif feed:
                            join_request = session.query(FeedJoinRequest).filter_by(user_id=user.id, feed_id=feed.id).first()
                            if join_request:
                                session.delete(join_request)
                            existing_membership = session.query(FeedMember).filter_by(user_id=user.id, feed_id=feed.id).first()
                            if existing_membership:
                                session.delete(existing_membership)
                                cache.delete_memoized(feed_membership, user, feed)
                            session.commit()
                            log_incoming_ap(id, APLOG_ACCEPT, APLOG_SUCCESS, saved_json)
                    return

                # Create is new content. Update is often an edit, but Updates from Lemmy can also be new content
                if core_activity['type'] == 'Create' or core_activity['type'] == 'Update':
                    if isinstance(core_activity['object'], str):
                        core_activity = verify_object_from_source(core_activity)  # change core_activity['object'] from str to dict, then process normally
                        if not core_activity:
                            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Could not verify unsigned request from source')
                            return

                    if core_activity['object']['type'] == 'ChatMessage':
                        process_chat(user, store_ap_json, core_activity, session)
                        return
                    else:
                        if (core_activity['object']['type'] == 'Note' and 'name' in core_activity['object'] and  # Poll Votes
                                'inReplyTo' in core_activity['object'] and 'attributedTo' in core_activity['object'] and
                                not 'published' in core_activity['object']):
                            post_being_replied_to = Post.get_by_ap_id(core_activity['object']['inReplyTo'])
                            if post_being_replied_to:
                                poll_data = session.query(Poll).get(post_being_replied_to.id)
                                choice = session.query(PollChoice).filter_by(post_id=post_being_replied_to.id,
                                                                    choice_text=core_activity['object']['name']).first()
                                if poll_data and choice:
                                    poll_data.vote_for_choice(choice.id, user.id)
                                    log_incoming_ap(id, APLOG_CREATE, APLOG_SUCCESS, saved_json)
                                    if post_being_replied_to.author.is_local():
                                        post_being_replied_to.edited_at = utcnow()
                                        session.commit()
                                        task_selector('edit_post', post_id=post_being_replied_to.id)
                            return
                        if not announced and not community:
                            community = find_community(request_json)
                            if not community:
                                was_chat_message = process_chat(user, store_ap_json, core_activity, session)
                                if not was_chat_message:
                                    log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Blocked or unfound community')
                                return
                            if not ensure_domains_match(core_activity['object']):
                                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Domains do not match')
                                return
                            if community.local_only:
                                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Remote Create in local_only community')
                                return

                        object_type = core_activity['object']['type']
                        new_content_types = ['Page', 'Article', 'Link', 'Note', 'Question', 'Event']
                        if object_type in new_content_types:  # create or update a post
                            process_new_content(user, community, store_ap_json, request_json, announced)
                            return
                        elif object_type == 'Video':  # PeerTube: editing a video (mostly used to update post score)
                            post = Post.get_by_ap_id(core_activity['object']['id'])
                            if post:
                                if user.id == post.user_id:
                                    update_post_from_activity(post, request_json)
                                    log_incoming_ap(id, APLOG_UPDATE, APLOG_SUCCESS, saved_json)
                                    return
                                else:
                                    log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json, 'Edit attempt denied')
                                    return
                            else:
                                log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json, 'PeerTube post not found')
                                return
                        elif object_type == 'Group' and core_activity['type'] == 'Update':  # update community/category info
                            if community.is_local() and not community.is_moderator(user):
                                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Comm edit by non-moderator')
                            else:
                                refresh_community_profile(community.id, core_activity['object'])
                                log_incoming_ap(id, APLOG_UPDATE, APLOG_SUCCESS, saved_json)
                                if community.is_local():
                                    announce_activity_to_followers(community, user, request_json)
                        else:
                            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Unacceptable type (create): ' + object_type)
                    return

                if core_activity['type'] == 'Delete':
                    # check if its a feed being deleted
                    if isinstance(core_activity['object'], dict) and core_activity['object']['type'] == 'Feed':
                        # find the user in the traffic
                        user = find_actor_or_create_cached(actor_id)
                        # find the feed
                        feed = session.query(Feed).filter_by(ap_public_url=core_activity['object']['id']).first()

                        # make sure the user sending the delete owns the feed
                        if not user.id == feed.user_id:
                            log_incoming_ap(id, APLOG_DELETE, APLOG_FAILURE, saved_json, 'Delete rejected, request came from non-owner.')
                            return

                        # if found, remove all the feeditems and feedmembers
                        if feed:
                            # find the feeditems and remove them
                            feed_items = session.query(FeedItem).filter_by(feed_id=feed.id).all()
                            for fi in feed_items:
                                session.delete(fi)
                                session.commit()
                            # find the feedmembers and remove them
                            feed_members = session.query(FeedMember).filter_by(feed_id=feed.id).all()
                            for fm in feed_members:
                                session.delete(fm)
                                session.commit()
                            # find any feedjoinrequests and remove them
                            feed_join_requests = session.query(FeedJoinRequest).filter_by(feed_id=feed.id).all()
                            for fjr in feed_join_requests:
                                session.delete(fjr)
                                session.commit()
                            # finally remove the feed itself
                            session.delete(feed)
                            session.commit()
                            log_incoming_ap(id, APLOG_DELETE, APLOG_SUCCESS, saved_json, f"Delete: Feed {core_activity['object']['id']} deleted")
                            return
                        else:
                            log_incoming_ap(id, APLOG_DELETE, APLOG_FAILURE, saved_json, f"Delete: cannot find {core_activity['object']['id']}")
                            return
                    elif isinstance(core_activity['object'], str):
                        ap_id = core_activity['object']  # lemmy
                    else:
                        ap_id = core_activity['object']['id']  # kbin
                    to_delete = find_liked_object(ap_id)  # Just for Posts and Replies (User deletes go through process_delete_request())

                    if to_delete:  # Deleting content. User self-deletes are handled in process_delete_request()
                        if to_delete.deleted:
                            log_incoming_ap(id, APLOG_DELETE, APLOG_IGNORED, saved_json,
                                            'Activity about local content which is already deleted')
                        else:
                            reason = core_activity['summary'] if 'summary' in core_activity else ''
                            delete_post_or_comment(user, to_delete, store_ap_json, request_json, reason)
                            if not announced:
                                announce_activity_to_followers(to_delete.community, user, request_json)
                    else:
                        # no content found. check if it was a PM
                        updated_message = session.query(ChatMessage).filter_by(ap_id=ap_id, sender_id=user.id).first()
                        if updated_message:
                            updated_message.read = True
                            updated_message.deleted = True
                            session.commit()
                            log_incoming_ap(id, APLOG_DELETE, APLOG_SUCCESS, saved_json,
                                            f"Delete: PM {ap_id} deleted")
                    return

                if core_activity['type'] == 'Like' or core_activity['type'] == 'EmojiReact':  # Upvote
                    process_upvote(user, store_ap_json, request_json, announced)
                    return

                if core_activity['type'] == 'Dislike':  # Downvote
                    process_downvote(user, store_ap_json, request_json, announced)
                    return

                if core_activity['type'] == 'PollVote': # Vote in a poll
                    process_poll_vote(user, store_ap_json, request_json, announced)
                    return

                if core_activity['type'] == 'ChooseAnswer': # Set the answer to a question
                    process_question_answer(user, store_ap_json, request_json, announced)
                    return

                if core_activity['type'] == 'Flag':  # Reported content
                    reported = find_reported_object(core_activity['object'])
                    if reported:
                        process_report(user, reported, core_activity, session)
                        log_incoming_ap(id, APLOG_REPORT, APLOG_SUCCESS, saved_json)
                        announce_activity_to_followers(reported.community, user, request_json,
                                                       is_flag=True, admin_instance_id=reported.author.instance_id)
                    else:
                        log_incoming_ap(id, APLOG_REPORT, APLOG_IGNORED, saved_json,
                                        'Report ignored due to missing content')
                    return

                if core_activity['type'] == 'Lock':  # Post or comment lock
                    mod = user
                    post = None
                    post_reply = None
                    if '/post/' in core_activity['object']:
                        post = Post.get_by_ap_id(core_activity['object'])
                    elif '/comment/' in core_activity['object']:
                        post_reply = PostReply.get_by_ap_id(core_activity['object'])
                    else:
                        post = Post.get_by_ap_id(core_activity['object'])
                        if post is None:
                            post_reply = PostReply.get_by_ap_id(core_activity['object'])
                    reason = core_activity['summary'] if 'summary' in core_activity else ''
                    if post:
                        if post.community.is_moderator(mod) or post.community.is_instance_admin(mod):
                            post.comments_enabled = False
                            session.commit()
                            add_to_modlog('lock_post', actor=mod, target_user=post.author, reason=reason,
                                          community=post.community, post=post,
                                          link_text=shorten_string(post.title), link=f'post/{post.id}')
                            log_incoming_ap(id, APLOG_LOCK, APLOG_SUCCESS, saved_json)
                        else:
                            log_incoming_ap(id, APLOG_LOCK, APLOG_FAILURE, saved_json, 'Lock: Does not have permission')
                    elif post_reply:
                        if post_reply.community.is_moderator(mod) or post.community.is_instance_admin(mod):
                            post_reply.replies_enabled = False
                            session.execute(text(
                                'update post_reply set replies_enabled = :replies_enabled where path @> ARRAY[:parent_id]'),
                                               {'parent_id': post_reply.id, 'replies_enabled': False})
                            session.commit()
                            add_to_modlog('lock_post_reply', actor=mod, target_user=post.author, reason=reason,
                                          community=post.community, reply=post_reply,
                                          link_text=shorten_string(post_reply.body), link=f'post/{post_reply.post_id}#comment_{post_reply.id}')
                            log_incoming_ap(id, APLOG_LOCK, APLOG_SUCCESS, saved_json)
                        else:
                            log_incoming_ap(id, APLOG_LOCK, APLOG_FAILURE, saved_json, 'Lock: Does not have permission')
                    else:
                        log_incoming_ap(id, APLOG_LOCK, APLOG_FAILURE, saved_json, 'Lock: post not found')
                    return

                if core_activity['type'] == 'Add':  # Add mods, sticky a post, or add a community to a feed
                    if user is not None:
                        mod = user
                    if not announced and not feed:
                        community = find_community(core_activity)
                    if feed and 'id' in core_activity['object']:
                        community_to_add = find_actor_or_create_cached(core_activity['object']['id'], community_only=True)
                        # if community found or created - add the FeedItem and update Feed info
                        if community_to_add and isinstance(community_to_add, Community):
                            feed_item = FeedItem(feed_id=feed.id, community_id=community_to_add.id)
                            session.add(feed_item)
                            feed.num_communities += 1
                            session.commit()
                        # also autosubscribe any feedmembers to the new community
                        feed_members = session.query(FeedMember).filter_by(feed_id=feed.id).all()
                        for fm in feed_members:
                            fm_user = session.query(User).get(fm.user_id)
                            if fm_user.id == feed.user_id:
                                continue
                            if fm_user.is_local() and fm_user.feed_auto_follow:
                                # user is local so lets auto-subscribe them to the community
                                from app.community.routes import do_subscribe
                                actor = community_to_add.ap_id if community_to_add.ap_id else community_to_add.name
                                do_subscribe(actor, fm_user.id, joined_via_feed=True)
                    elif community:
                        if not community.is_moderator(mod) and not community.is_instance_admin(mod):
                            log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Does not have permission')
                            return
                        target = core_activity['target']
                        if not community.ap_featured_url:
                            community.ap_featured_url = community.ap_profile_id + '/featured'
                        featured_url = community.ap_featured_url
                        moderators_url = community.ap_moderators_url
                        if target.lower() == featured_url.lower():
                            post = Post.get_by_ap_id(core_activity['object'])
                            if post:
                                post.sticky = True
                                session.commit()
                                log_incoming_ap(id, APLOG_ADD, APLOG_SUCCESS, saved_json)
                            else:
                                log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json,
                                                'Cannot find: ' + core_activity['object'])
                            return
                        if target == moderators_url:
                            new_mod = find_actor_or_create_cached(core_activity['object'])
                            if new_mod:
                                existing_membership = session.query(CommunityMember).filter_by(community_id=community.id,
                                                                                      user_id=new_mod.id).first()
                                if existing_membership:
                                    existing_membership.is_moderator = True
                                else:
                                    new_membership = CommunityMember(community_id=community.id, user_id=new_mod.id,
                                                                     is_moderator=True)
                                    session.add(new_membership)
                                add_to_modlog('add_mod', actor=mod, target_user=new_mod, community=community,
                                              link_text=new_mod.display_name(), link=new_mod.link())
                                session.commit()
                                cache.delete_memoized(moderating_communities, new_mod.id)
                                cache.delete_memoized(joined_communities, new_mod.id)
                                cache.delete_memoized(community_moderators, community.id)
                                cache.delete_memoized(moderating_communities_ids, new_mod.id)
                                cache.delete_memoized(moderating_communities_ids_all_users)
                                cache.delete_memoized(Community.moderators, community)
                                log_incoming_ap(id, APLOG_ADD, APLOG_SUCCESS, saved_json)
                            else:
                                log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json,
                                                'Cannot find: ' + core_activity['object'])
                            return
                        log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Unknown target for Add')
                    else:
                        log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Add: cannot find community or feed')
                    return

                if core_activity['type'] == 'Remove':  # Remove mods, unsticky a post, or remove a community from a feed
                    if user is not None:
                        mod = user
                    if not announced and not feed:
                        community = find_community(core_activity)
                    if feed and 'id' in core_activity['object']:
                        community_to_remove = find_actor_or_create_cached(core_activity['object']['id'], community_only=True)
                        # if community found or created - remove the FeedItem and update Feed info
                        if community_to_remove and isinstance(community_to_remove, Community):
                            feed_item = session.query(FeedItem).filter_by(feed_id=feed.id,
                                                                 community_id=community_to_remove.id).first()
                            session.delete(feed_item)
                            feed.num_communities -= 1
                            session.commit()
                            # also auto-unsubscribe any feedmembers from the community
                            # who have feed_auto_leave enabled
                            feed_members = session.query(FeedMember).filter_by(feed_id=feed.id).all()
                            for fm in feed_members:
                                fm_user = session.query(User).get(fm.user_id)
                                if fm_user.id == feed.user_id:
                                    continue
                                if fm_user.is_local() and fm_user.feed_auto_leave:
                                    subscription = community_membership(fm_user, community_to_remove)
                                    cm = session.query(CommunityMember).filter_by(user_id=fm_user.id,
                                                                         community_id=community_to_remove.id).first()
                                    if subscription != SUBSCRIPTION_OWNER and cm.joined_via_feed:
                                        proceed = True
                                        # Undo the Follow
                                        if not community_to_remove.is_local():  # this is a remote community, so activitypub is needed
                                            if not community_to_remove.instance.gone_forever:
                                                follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{gibberish(15)}"
                                                if community_to_remove.instance.domain == 'ovo.st':
                                                    join_request = session.query(CommunityJoinRequest).filter_by(user_id=fm_user.id,
                                                                                                        community_id=community_to_remove.id).first()
                                                    if join_request:
                                                        follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
                                                undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15)
                                                follow = {'actor': fm_user.public_url(),
                                                          'to': [community_to_remove.public_url()],
                                                          'object': community_to_remove.public_url(), 'type': 'Follow',
                                                          'id': follow_id}
                                                undo = {'actor': fm_user.public_url(),
                                                        'to': [community_to_remove.public_url()], 'type': 'Undo',
                                                        'id': undo_id, 'object': follow}
                                                send_post_request(community_to_remove.ap_inbox_url, undo,
                                                                  fm_user.private_key, fm_user.public_url() + '#main-key',
                                                                  timeout=10)

                                        if proceed:
                                            session.query(CommunityMember).filter_by(user_id=fm_user.id,
                                                                                        community_id=community_to_remove.id).delete()
                                            session.query(CommunityJoinRequest).filter_by(user_id=fm_user.id,
                                                                                             community_id=community_to_remove.id).delete()
                                            community_to_remove.subscriptions_count -= 1
                                            session.commit()
                                            log_incoming_ap(id, APLOG_REMOVE, APLOG_SUCCESS, saved_json,
                                                            f'{fm_user.user_name} auto-unfollowed {community_to_remove.ap_public_url} during a feed/remove')
                    elif community:
                        if not community.is_moderator(mod) and not community.is_instance_admin(mod):
                            log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Does not have permission')
                            return
                        target = core_activity['target']
                        if not community.ap_featured_url:
                            community.ap_featured_url = community.ap_profile_id + '/featured'
                        featured_url = community.ap_featured_url
                        moderators_url = community.ap_moderators_url
                        if target.lower() == featured_url.lower():
                            post = Post.get_by_ap_id(core_activity['object'])
                            if post:
                                post.sticky = False
                                session.commit()
                                log_incoming_ap(id, APLOG_REMOVE, APLOG_SUCCESS, saved_json)
                            else:
                                log_incoming_ap(id, APLOG_REMOVE, APLOG_FAILURE, saved_json,
                                                'Cannot find: ' + core_activity['object'])
                            return
                        if target == moderators_url:
                            old_mod = find_actor_or_create_cached(core_activity['object'])
                            if old_mod:
                                existing_membership = session.query(CommunityMember).filter_by(community_id=community.id,
                                                                                      user_id=old_mod.id).first()
                                if existing_membership:
                                    existing_membership.is_moderator = False
                                    session.commit()
                                    cache.delete_memoized(moderating_communities, old_mod.id)
                                    cache.delete_memoized(joined_communities, old_mod.id)
                                    cache.delete_memoized(community_moderators, community.id)
                                    cache.delete_memoized(moderating_communities_ids, old_mod.id)
                                    cache.delete_memoized(moderating_communities_ids_all_users)
                                    cache.delete_memoized(Community.moderators, community)
                                    log_incoming_ap(id, APLOG_REMOVE, APLOG_SUCCESS, saved_json)
                                add_to_modlog('remove_mod', actor=mod, target_user=old_mod, community=community,
                                              link_text=old_mod.display_name(), link=old_mod.link())
                            else:
                                log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json,
                                                'Cannot find: ' + core_activity['object'])
                            return
                        log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Unknown target for Remove')
                    else:
                        log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Remove: cannot find community or feed')
                    return

                if core_activity['type'] == 'Move':
                    origin_community: Community = find_actor_or_create_cached(core_activity['origin'], community_only=True)
                    target_community = find_actor_or_create_cached(core_activity['target'], community_only=True)
                    post = Post.get_by_ap_id(core_activity['object'])
                    if post is None and origin_community and target_community:
                        # retrieve post from remote instance
                        post = resolve_remote_post_from_search(core_activity['object'].replace('/context', ''))
                    if origin_community and target_community and post:
                        if user.id == post.user_id or origin_community.is_moderator(user) or (origin_community.instance_id == user.instance_id and origin_community.is_instance_admin(user)):
                            post.move_to(target_community)
                            session.commit()

                            add_to_modlog('move_post', actor=user, target_user=post.author, reason='',
                                          community=target_community, post=post,
                                          link_text=shorten_string(post.title), link=f'post/{post.id}')
                            if origin_community.is_local():
                                announce_activity_to_followers(origin_community, user, request_json)
                            log_incoming_ap(id, APLOG_MOVE, APLOG_SUCCESS, saved_json,
                                            f'{user.user_name} moved post to {target_community.link()}')
                if core_activity['type'] == 'Block':  # User Ban
                    """
                    Sent directly (not Announced) if a remote Admin is banning one of their own users from their site
                    (e.g. lemmy.ml is banning lemmy.ml/u/troll)

                    Also send directly if a remote Admin or Mod is banning one of our users from one of their communities
                    (e.g. lemmy.ml is banning piefed.social/u/troll from lemmy.ml/c/memes)

                    Is Announced if a remote Admin or Mod is banning a remote user from one of their communities (a remote user could also be one of our local users)
                    (e.g. lemmy.ml is banning piefed.social/u/troll or lemmy.world/u/troll from lemmy.ml/c/memes)

                    Same activity can be sent direct and Announced, but one will be filtered out when shared_inbox() checks for it as a duplicate

                    We currently don't receive a Block if a remote Admin is banning a user of a different instance from their site (it's hacked by all the relevant communities Announcing a community ban)
                    This may change in the future, so it's something to monitor
                    If / When this changes, the code below will need updating, and we'll have to do extra work
                    """
                    if not announced and store_ap_json:
                        core_activity['cc'] = []  # cut very long list of instances

                    blocker = user
                    already_banned = False
                    blocked_ap_id = core_activity['object'].lower()
                    blocked = session.query(User).filter_by(ap_profile_id=blocked_ap_id).first()
                    if not blocked:
                        log_incoming_ap(id, APLOG_USERBAN, APLOG_IGNORED, saved_json, 'Does not exist here')
                        return
                    if blocked.banned:  # We may have already banned them - we don't want remote temp bans to over-ride our permanent bans
                        already_banned = True

                    remove_data = core_activity['removeData'] if 'removeData' in core_activity else False
                    if 'target' in core_activity:
                        target = core_activity['target']
                        if target.count('/') < 4:  # site ban
                            if not blocker.is_instance_admin():
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_FAILURE, saved_json, 'Does not have permission')
                                return
                            if blocked.is_local():
                                ban_user(blocker, blocked, None, core_activity)
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_MONITOR, request_json,
                                                'Remote Admin in banning one of our users from their site')
                                return
                            if blocked.instance_id != blocker.instance_id:
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_MONITOR, request_json,
                                                'Remote Admin is banning a user of a different instance from their site')
                                return

                            if not already_banned:
                                blocked.banned = True
                                if 'expires' in core_activity:
                                    blocked.ban_until = core_activity['expires']
                                elif 'endTime' in core_activity:
                                    blocked.ban_until = core_activity['endTime']
                                session.commit()

                            if remove_data:
                                site_ban_remove_data(blocker.id, blocked)
                            log_incoming_ap(id, APLOG_USERBAN, APLOG_SUCCESS, saved_json)
                        else:  # community ban (community will already known if activity was Announced)
                            community = community if community else find_actor_or_create_cached(target, create_if_not_found=False,
                                                                                         community_only=True)
                            if not community:
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_IGNORED, saved_json,
                                                'Blocked or unfound community')
                                return
                            if not community.is_moderator(blocker) and not community.is_instance_admin(blocker):
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_FAILURE, saved_json, 'Does not have permission')
                                return

                            if remove_data:
                                community_ban_remove_data(blocker.id, community.id, blocked)
                            if not already_banned:
                                ban_user(blocker, blocked, community, core_activity)
                            log_incoming_ap(id, APLOG_USERBAN, APLOG_SUCCESS, saved_json)
                    else:  # Mastodon does not have a target when blocking, only object
                        if 'object' in core_activity and isinstance(core_activity['object'], str):
                            if not blocker.has_blocked_user(blocked.id):
                                session.add(UserBlock(blocker_id=blocker.id, blocked_id=blocked.id))
                                session.commit()

                    return

                if core_activity['type'] == 'Undo':
                    if core_activity['object']['type'] == 'Follow':  # Unsubscribe from a community or user
                        target_ap_id = core_activity['object']['object']
                        target = find_actor_or_create_cached(target_ap_id)
                        if isinstance(target, Community):
                            community = target
                            member = session.query(CommunityMember).filter_by(user_id=user.id, community_id=community.id).first()
                            join_request = session.query(CommunityJoinRequest).filter_by(user_id=user.id,
                                                                                community_id=community.id).first()
                            if member:
                                session.delete(member)
                                community.subscriptions_count -= 1
                                community.last_active = utcnow()
                                user.last_seen = utcnow()
                            if join_request:
                                session.delete(join_request)
                            session.commit()
                            cache.delete_memoized(community_membership, user, community)
                            log_incoming_ap(id, APLOG_UNDO_FOLLOW, APLOG_SUCCESS, saved_json)
                            return
                        if isinstance(target, Feed):
                            feed = target
                            member = session.query(FeedMember).filter_by(user_id=user.id, feed_id=feed.id).first()
                            join_request = session.query(FeedJoinRequest).filter_by(user_id=user.id, feed_id=feed.id).first()
                            if member:
                                session.delete(member)
                                feed.subscriptions_count -= 1
                            if join_request:
                                session.delete(join_request)
                            session.commit()
                            cache.delete_memoized(feed_membership, user, feed)
                            log_incoming_ap(id, APLOG_UNDO_FOLLOW, APLOG_SUCCESS, saved_json)
                            return
                        if isinstance(target, User):
                            local_user = target
                            remote_user = user
                            follower = session.query(UserFollower).filter_by(local_user_id=local_user.id,
                                                                    remote_user_id=remote_user.id, is_accepted=True).first()
                            if follower:
                                session.delete(follower)
                                session.commit()
                                log_incoming_ap(id, APLOG_UNDO_FOLLOW, APLOG_SUCCESS, saved_json)
                            return
                        if not target:
                            log_incoming_ap(id, APLOG_UNDO_FOLLOW, APLOG_FAILURE, saved_json, 'Unfound target')
                        return

                    if core_activity['object']['type'] == 'Delete':  # Restore something previously deleted
                        if isinstance(core_activity['object']['object'], str):
                            ap_id = core_activity['object']['object']  # lemmy
                        else:
                            ap_id = core_activity['object']['object']['id']  # kbin

                        restorer = user
                        to_restore = find_liked_object(
                            ap_id)  # a user or a mod/admin is undoing the delete of a post or reply
                        if to_restore:
                            if not to_restore.deleted:
                                log_incoming_ap(id, APLOG_UNDO_DELETE, APLOG_IGNORED, saved_json,
                                                'Activity about local content which is already restored')
                            else:
                                reason = core_activity['object']['summary'] if 'summary' in core_activity['object'] else ''
                                restore_post_or_comment(restorer, to_restore, store_ap_json, request_json, reason)
                                if not announced:
                                    announce_activity_to_followers(to_restore.community, user, request_json)
                        else:
                            # no content found. check if it was a PM
                            updated_message = session.query(ChatMessage).filter_by(ap_id=ap_id, sender_id=restorer.id).first()
                            if updated_message:
                                updated_message.deleted = False
                                session.commit()
                                log_incoming_ap(id, APLOG_UNDO_DELETE, APLOG_SUCCESS, saved_json,
                                            f"Delete: PM {ap_id} restored")
                        return

                    if core_activity['object']['type'] == 'Like' or core_activity['object']['type'] == 'Dislike':  # Undoing an upvote or downvote
                        post = comment = None
                        target_ap_id = core_activity['object']['object']
                        post_or_comment = undo_vote(comment, post, target_ap_id, user)
                        if post_or_comment:
                            log_incoming_ap(id, APLOG_UNDO_VOTE, APLOG_SUCCESS, saved_json)
                            if not announced:
                                announce_activity_to_followers(post_or_comment.community, user, request_json, can_batch=True)
                        else:
                            log_incoming_ap(id, APLOG_UNDO_VOTE, APLOG_FAILURE, saved_json,
                                            'Unfound object ' + target_ap_id)
                        return

                    if core_activity['object']['type'] == 'Lock':  # Undo of post lock
                        mod = user
                        post = None
                        post_reply = None
                        target_ap_id = core_activity['object']['object']
                        if '/post/' in core_activity['object']:
                            post = Post.get_by_ap_id(target_ap_id)
                        elif '/comment/' in core_activity['object']:
                            post_reply = PostReply.get_by_ap_id(target_ap_id)
                        else:
                            post = Post.get_by_ap_id(target_ap_id)
                            if post is None:
                                post_reply = PostReply.get_by_ap_id(target_ap_id)
                        reason = core_activity['summary'] if 'summary' in core_activity else ''
                        if post:
                            if post.community.is_moderator(mod) or post.community.is_instance_admin(mod):
                                post.comments_enabled = True
                                session.commit()
                                add_to_modlog('unlock_post', actor=mod, target_user=post.author, reason=reason,
                                              community=post.community, post=post,
                                              link_text=shorten_string(post.title), link=f'post/{post.id}')
                                log_incoming_ap(id, APLOG_LOCK, APLOG_SUCCESS, saved_json)
                            else:
                                log_incoming_ap(id, APLOG_LOCK, APLOG_FAILURE, saved_json, 'Unlock: Does not have permission')
                        if post_reply:
                            if post_reply.community.is_moderator(mod) or post_reply.community.is_instance_admin(mod):
                                post_reply.replies_enabled = True
                                db.session.execute(text(
                                    'update post_reply set replies_enabled = :replies_enabled where path @> ARRAY[:parent_id]'),
                                                   {'parent_id': post_reply.id, 'replies_enabled': True})
                                session.commit()
                                add_to_modlog('unlock_post_reply', actor=mod, target_user=post.author, reason=reason,
                                              community=post.community, reply=post_reply,
                                              link_text=shorten_string(post_reply.body), link=f'post/{post_reply.post_id}#comment_{post_reply.id}')
                                log_incoming_ap(id, APLOG_LOCK, APLOG_SUCCESS, saved_json)
                            else:
                                log_incoming_ap(id, APLOG_LOCK, APLOG_FAILURE, saved_json, 'Unlock: Does not have permission')
                        else:
                            log_incoming_ap(id, APLOG_LOCK, APLOG_FAILURE, saved_json, 'Unlock: post not found')
                        return

                    if core_activity['object']['type'] == 'Block':  # Undo of user ban
                        if announced and store_ap_json:
                            core_activity['cc'] = []  # cut very long list of instances
                            core_activity['object']['cc'] = []

                        unblocker = user
                        unblocked_ap_id = core_activity['object']['object'].lower()
                        unblocked = session.query(User).filter_by(ap_profile_id=unblocked_ap_id).first()
                        if not unblocked:
                            log_incoming_ap(id, APLOG_USERBAN, APLOG_IGNORED, saved_json, 'Does not exist here')
                            return
                        # in future, we'll need to know who banned a user, so this activity doesn't unban a user that was bannned by a local admin

                        # (no removeData field in an undo/ban - cannot restore without knowing if deletion was part of ban, or different moderator action)
                        target = core_activity['object']['target']
                        if target.count('/') < 4:  # undo of site ban
                            if not unblocker.is_instance_admin():
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_FAILURE, saved_json, 'Does not have permission')
                                return
                            if unblocked.is_local():
                                unban_user(user, unblocked, None, core_activity)
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_MONITOR, request_json,
                                                'Remote Admin in unbanning one of our users from their site')
                                current_app.logger.error(
                                    'Remote Admin in unbanning one of our users from their site: ' + str(request_json))
                                return
                            if unblocked.instance_id != unblocker.instance_id:
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_MONITOR, request_json,
                                                'Remote Admin is unbanning a user of a different instance from their site')
                                current_app.logger.error(
                                    'Remote Admin is unbanning a user of a different instance from their site: ' + str(
                                        request_json))
                                return

                            unblocked.banned = False
                            unblocked.banned_until = None
                            session.commit()
                            log_incoming_ap(id, APLOG_USERBAN, APLOG_SUCCESS, saved_json)
                        else:  # undo community ban (community will already known if activity was Announced)
                            community = community if community else find_actor_or_create_cached(target, community_only=True)
                            if not community:
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_IGNORED, saved_json,
                                                'Blocked or unfound community')
                                return
                            if not community.is_moderator(unblocker) and not community.is_instance_admin(unblocker):
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_FAILURE, saved_json, 'Does not have permission')
                                return

                            unban_user(unblocker, unblocked, community, core_activity)
                            log_incoming_ap(id, APLOG_USERBAN, APLOG_SUCCESS, saved_json)
                        return

                    if core_activity['object']['type'] == 'ChooseAnswer':
                        if isinstance(core_activity['object'], str):
                            target_ap_id = core_activity['object']
                        else:
                            target_ap_id = core_activity['object']['object']
                        post_reply = PostReply.get_by_ap_id(target_ap_id)
                        if post_reply:
                            with redis_client.lock(f"lock:post_reply:{post_reply.id}", timeout=10, blocking_timeout=6):
                                post_reply.answer = False
                                session.commit()
                            log_incoming_ap(id, APLOG_LOCK, APLOG_SUCCESS, saved_json)
                        return

                    log_incoming_ap(id, APLOG_MONITOR, APLOG_PROCESSING, request_json, 'Unmatched activity')

                if core_activity['type'] == 'QuoteRequest':
                    post_ap = core_activity['object']
                    their_post_ap = core_activity['instrument']['id']
                    process_quote_boost(core_activity, post_ap, their_post_ap)
                    log_incoming_ap(id, APLOG_QUOTEBOOST, APLOG_SUCCESS, saved_json)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def process_delete_request(request_json, store_ap_json):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                # this function processes self-deletes (retain case here, as user_removed_from_remote_server() uses a JSON request)
                saved_json = request_json if store_ap_json else None
                id = request_json['id']
                user_ap_id = request_json['actor']
                user = session.query(User).filter_by(ap_profile_id=user_ap_id.lower()).first()
                if user:
                    if ('removeData' in request_json and request_json['removeData'] is True) or user.created_very_recently():
                        user.purge_content()
                    user.deleted = True
                    user.deleted_by = user.id
                    user.delete_dependencies()
                    session.commit()
                    with patch_db_session(session):
                        log_incoming_ap(id, APLOG_DELETE, APLOG_SUCCESS, saved_json)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# Announces incoming activity back out to subscribers
# if is_flag is set, the report is just sent to any remote mods and the reported user's instance
def announce_activity_to_followers(community: Community, creator: User, activity, can_batch=False,
                                   is_flag=False, admin_instance_id=1):
    from app.activitypub.signature import default_context

    # avoid announcing activity sent to local users unless it is also in a local community
    if not community.is_local():
        return

    if creator.banned:
        return

    # remove context from what will be inner object
    if '@context' in activity:
        del activity["@context"]

    announce_activity = {
        '@context': default_context(),
        "actor": community.public_url(),
        "to": [
            "https://www.w3.org/ns/activitystreams#Public"
        ],
        "object": activity,
        "cc": [
            f"{community.public_url()}/followers"
        ],
        "type": "Announce",
        "id": f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
    }

    if is_flag:
        instances = community.following_instances(include_dormant=True, mod_hosts_only=True)
        if admin_instance_id != 1 and not any(i.id == admin_instance_id for i in instances):
            admin_instance = db.session.query(Instance).get(admin_instance_id)
            if admin_instance:
                instances.append(admin_instance)
    else:
        instances = community.following_instances(include_dormant=True)

    send_async = []
    for instance in instances:
        # awaken dormant instances if they've been sleeping for long enough to be worth trying again
        awaken_dormant_instance(instance)

        # All good? Send!
        if instance and instance.online() and instance.inbox and not instance_banned(instance.inbox):
            if creator.instance_id != instance.id:  # don't send it to the instance that hosts the creator as presumably they already have the content
                if can_batch and instance.software == 'piefed':
                    db.session.add(ActivityBatch(instance_id=instance.id, community_id=community.id,
                                                 payload=activity))
                    db.session.commit()
                else:
                    if current_app.config['NOTIF_SERVER'] and is_vote(announce_activity):   # Votes make up a very high percentage of activities, so it is more efficient to send them via piefed_notifs. However piefed_notifs does not retry failed sends. For votes this is acceptable.
                        send_async.append(HttpSignature.signed_request(instance.inbox, announce_activity,
                                                                       community.private_key,
                                                                       community.ap_profile_id + '#main-key',
                                                                       send_via_async=True))
                    else:
                        send_to_remote_instance_fast(instance.inbox, community.private_key, community.ap_profile_id, announce_activity)

    if len(send_async):
        from app import redis_client
        # send announce_activity via redis pub/sub to piefed_notifs service
        redis_client.publish("http_posts:activity", json.dumps({'urls': [url[0] for url in send_async],
                                                                'headers': [url[1] for url in send_async],
                                                                'data': send_async[0][2].decode('utf-8')}))


@bp.route('/c/<actor>/outbox', methods=['GET'])
def community_outbox(actor):
    actor = actor.strip()
    community = Community.query.filter_by(name=actor, banned=False, ap_id=None).first()
    if community is not None:
        sticky_posts = Post.query.filter(Post.community_id == community.id).filter(Post.sticky == True, Post.deleted == False,
                                         Post.status > POST_STATUS_REVIEWING).order_by(desc(Post.posted_at)).limit(50).all()
        remaining_limit = 50 - len(sticky_posts)
        remaining_posts = Post.query.filter(Post.community_id == community.id).filter(Post.sticky == False, Post.deleted == False,
                                            Post.status > POST_STATUS_REVIEWING).order_by(desc(Post.posted_at)).limit(remaining_limit).all()
        posts = sticky_posts + remaining_posts

        community_data = {
            "@context": default_context(),
            "type": "OrderedCollection",
            "id": f"{current_app.config['SERVER_URL']}/c/{actor}/outbox",
            "totalItems": len(posts),
            "orderedItems": []
        }

        for post in posts:
            community_data['orderedItems'].append(post_to_activity(post, community))

        resp = jsonify(community_data)
        resp.content_type = 'application/activity+json'
        resp.headers.set('Cache-Control', 'public, max-age=10')
        return resp
    else:
        abort(404)


@bp.route('/c/<actor>/featured', methods=['GET'])
def community_featured(actor):
    actor = actor.strip()
    community = Community.query.filter_by(name=actor, banned=False, ap_id=None).first()
    if community is not None:
        posts = Post.query.filter_by(community_id=community.id, sticky=True, deleted=False).all()

        community_data = {
            "@context": default_context(),
            "type": "OrderedCollection",
            "id": f"{current_app.config['SERVER_URL']}/c/{actor}/featured",
            "totalItems": len(posts),
            "orderedItems": []
        }

        for post in posts:
            community_data['orderedItems'].append(post_to_page(post))

        resp = jsonify(community_data)
        resp.content_type = 'application/activity+json'
        return resp
    else:
        abort(404)


@bp.route('/c/<actor>/moderators', methods=['GET'])
def community_moderators_route(actor):
    actor = actor.strip()
    community = Community.query.filter_by(name=actor, banned=False, ap_id=None).first()
    if community is not None:
        moderator_ids = community_moderators(community.id)
        moderators = db.session.query(User).filter(User.id.in_([mod.user_id for mod in moderator_ids])).all()
        community_data = {
            "@context": default_context(),
            "type": "OrderedCollection",
            "id": f"{current_app.config['SERVER_URL']}/c/{actor}/moderators",
            "totalItems": len(moderators),
            "orderedItems": []
        }

        for moderator in moderators:
            community_data['orderedItems'].append(moderator.public_url())

        resp = jsonify(community_data)
        resp.content_type = 'application/activity+json'
        resp.headers.set('Cache-Control', 'public, max-age=120')
        return resp
    else:
        abort(404)


@bp.route('/c/<actor>/followers', methods=['GET'])
def community_followers(actor):
    actor = actor.strip()
    community = Community.query.filter_by(name=actor, banned=False, ap_id=None).first()
    if community is not None:
        result = {
            "@context": default_context(),
            "id": f'{current_app.config["SERVER_URL"]}/c/{actor}/followers',
            "type": "Collection",
            "totalItems": community_members(community.id),
            "items": []
        }
        resp = jsonify(result)
        resp.content_type = 'application/activity+json'
        resp.headers.set('Cache-Control', 'public, max-age=10')
        return resp
    else:
        abort(404)


@bp.route('/u/<actor>/followers', methods=['GET'])
def user_followers(actor):
    actor = actor.strip()
    user = db.session.query(User).filter_by(user_name=actor, banned=False, ap_id=None).first()
    if user is not None and user.ap_followers_url:
        # Get all followers, except those that are blocked by user by doing an outer join
        followers = db.session.query(User).join(UserFollower, User.id == UserFollower.remote_user_id) \
            .outerjoin(UserBlock,
                       (User.id == UserBlock.blocker_id) & (UserFollower.local_user_id == UserBlock.blocked_id)) \
            .filter((UserFollower.local_user_id == user.id) & (UserBlock.id == None)) \
            .all()

        items = []
        for f in followers:
            items.append(f.ap_public_url)
        result = {
            "@context": default_context(),
            "id": user.ap_followers_url,
            "type": "Collection",
            "totalItems": len(items),
            "items": items
        }
        resp = jsonify(result)
        resp.content_type = 'application/activity+json'
        resp.headers.set('Cache-Control', 'public, max-age=15')
        resp.headers.set('Vary', 'Accept')
        return resp
    else:
        abort(404)


@bp.route('/comment/<int:comment_id>', methods=['GET', 'HEAD'])
def comment_ap(comment_id):
    reply = PostReply.query.get_or_404(comment_id)
    if is_activitypub_request():
        if reply.community.local_only or reply.community.private:
            abort(403)
        reply_data = comment_model_to_json(reply) if request.method == 'GET' else []
        resp = jsonify(reply_data)
        resp.content_type = 'application/activity+json'
        resp.headers.set('Vary', 'Accept')
        resp.headers.set('Cache-Control', 'public, max-age=120')
        resp.headers.set('Link',
                         f'<https://{current_app.config["SERVER_NAME"]}/comment/{reply.id}>; rel="alternate"; type="text/html"')
        return resp
    else:
        return continue_discussion(reply.post.id, comment_id)


@bp.route('/post/<int:post_id>/', methods=['GET', 'HEAD'])
def post_ap2(post_id):
    return redirect(url_for('activitypub.post_ap', post_id=post_id))


@bp.route('/post/<int:post_id>', methods=['GET', 'HEAD', 'POST'])
def post_ap(post_id):
    if (request.method == 'GET' or request.method == 'HEAD') and is_activitypub_request():
        post: Post = Post.query.get_or_404(post_id)
        if post.is_local():
            if post.community.local_only or post.community.private or post.status < POST_STATUS_PUBLISHED:
                abort(403)
            if request.method == 'GET':
                post_data = post_to_page(post)
                post_data['@context'] = default_context()
            else:  # HEAD request
                post_data = []
            resp = jsonify(post_data)
            resp.content_type = 'application/activity+json'
            resp.headers.set('Cache-Control', 'public, max-age=120')
            resp.headers.set('Vary', 'Accept')
            if post.slug:
                resp.headers.set('Link',
                                 f'<https://{current_app.config["SERVER_NAME"]}{post.slug}>; rel="alternate"; type="text/html"')
            else:
                resp.headers.set('Link',
                                 f'<https://{current_app.config["SERVER_NAME"]}/post/{post.id}>; rel="alternate"; type="text/html"')
            return resp
        else:
            return redirect(post.ap_id, code=301)
    else:
        block_honey_pot()
        return show_post(post_id,
                         low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1',
                         sort=request.args.get('sort', 'hot' if current_user.is_anonymous else current_user.default_comment_sort or 'hot'),
                         autoplay=request.args.get('autoplay', False))


@bp.route('/c/<community_name>/p/<int:post_id>/<slug>', methods=['GET', 'HEAD', 'POST'])
def post_nice(community_name, post_id, slug):
    return post_ap(post_id)


@bp.route('/post/<int:post_id>/replies', methods=['GET'])
def post_replies_ap(post_id):
    if (request.method == 'GET' or request.method == 'HEAD') and is_activitypub_request():
        post = Post.query.get_or_404(post_id)

        if request.method == 'GET':
            replies = post_replies_for_ap(post.id)
            replies_collection = {"type": "OrderedCollection", "totalItems": len(replies), "orderedItems": replies}
        else:
            replies_collection = {}
        replies_collection['@context'] = default_context()
        resp = jsonify(replies_collection)
        resp.content_type = 'application/activity+json'
        resp.headers.set('Vary', 'Accept')
        resp.headers.set('Cache-Control', 'public, max-age=15')
        return resp


@bp.route('/post/<int:post_id>/context', methods=['GET'])
def post_ap_context(post_id):
    if (request.method == 'GET' or request.method == 'HEAD') and is_activitypub_request():
        post = Post.query.get_or_404(post_id)
        if post.deleted:
            abort(404)
        if request.method == 'GET':
            replies = PostReply.query.filter_by(post_id=post_id, deleted=False).order_by(PostReply.posted_at).limit(2000)
            urls = [reply.ap_id for reply in replies]
            urls = [post.ap_id] + urls
            replies_collection = {"type": "OrderedCollection", "totalItems": len(urls), "orderedItems": urls}
        else:
            replies_collection = {}
        replies_collection['@context'] = default_context()
        replies_collection['id'] = f'{post.public_url()}/context'
        replies_collection['name'] = post.title
        replies_collection['attributedTo'] = post.community.profile_id()
        replies_collection['audience'] = post.community.profile_id()

        resp = jsonify(replies_collection)
        resp.content_type = 'application/activity+json'
        resp.headers.set('Vary', 'Accept')
        resp.headers.set('Cache-Control', 'public, max-age=15')
        return resp
    else:
        abort(400)


@bp.route('/activities/<type>/<id>')
@cache.cached(timeout=2400)
def activities_json(type, id):
    activity = ActivityPubLog.query.filter_by(
        activity_id=f"{current_app.config['SERVER_URL']}/activities/{type}/{id}").first()
    if activity:
        if activity.activity_json is not None:
            activity_json = json.loads(activity.activity_json)
        else:
            activity_json = {}
        resp = jsonify(activity_json)
        resp.content_type = 'application/activity+json'
    else:
        resp = make_response('', 404)
    resp.headers['Cache-Control'] = 'public, max-age=2400'
    return resp


# Other instances can query the result of their POST to the inbox by using this endpoint. The ID of the activity they
# sent (minus the https:// on the front) is the id parameter. e.g. https://piefed.ngrok.app/activity_result/piefed.ngrok.app/activities/announce/EfjyZ3BE5SzQK0C
@bp.route('/activity_result/<path:id>')
def activity_result(id):
    activity = ActivityPubLog.query.filter_by(activity_id=f'https://{id}').first()
    if activity:
        if activity.result == 'success':
            return jsonify('Ok')
        else:
            return jsonify({'error': activity.result, 'message': activity.exception_message})
    else:
        abort(404)


def process_new_content(user, community, store_ap_json, request_json, announced):
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    if not announced:
        in_reply_to = request_json['object']['inReplyTo'] if 'inReplyTo' in request_json['object'] else None
        ap_id = request_json['object']['id']
        announce_id = None
        activity_json = request_json
    else:
        in_reply_to = request_json['object']['object']['inReplyTo'] if 'inReplyTo' in request_json['object']['object'] else None
        ap_id = request_json['object']['object']['id']
        announce_id = shorten_string(request_json['id'], 100)
        activity_json = request_json['object']

    # announce / create IDs that are too long will crash the app. Not referred to again, so it shouldn't matter if they're truncated
    activity_json['id'] = shorten_string(activity_json['id'], 100)

    if not in_reply_to:  # Creating a new post
        post = Post.get_by_ap_id(ap_id)
        if post:
            if activity_json['type'] == 'Create':
                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Create processed after Update')
                return
            if user.id == post.user_id or post.community.is_moderator(user) or post.community.is_instance_admin(user):
                update_post_from_activity(post, activity_json)
                log_incoming_ap(id, APLOG_UPDATE, APLOG_SUCCESS, saved_json)
                if not announced:
                    announce_activity_to_followers(post.community, post.author, request_json)
                return
            else:
                log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json, 'Edit attempt denied')
                return
        else:
            if can_create_post(user, community):
                try:
                    post = create_post(store_ap_json, community, activity_json, user, announce_id=announce_id)
                    if post:
                        # confirm that an Update didn't lose an async race with a Create
                        if activity_json['type'] == 'Update' and post.edited_at is None:
                            update_post_from_activity(post, activity_json)
                            log_incoming_ap(id, APLOG_UPDATE, APLOG_SUCCESS, saved_json)
                        else:
                            log_incoming_ap(id, APLOG_CREATE, APLOG_SUCCESS, saved_json)

                        if not announced:
                            announce_activity_to_followers(community, user, request_json)
                        return
                    else:  # The post was not allowed - send a 'Delete' to remove it from the remote instance
                        if community.is_local():
                            proactively_delete_content(community, ap_id)
                except TypeError:
                    current_app.logger.error('TypeError: ' + str(request_json))
                    log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'TypeError. See log file.')
                    return
            else:
                if community.is_local():
                    proactively_delete_content(community, ap_id)
                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'User cannot create post in Community')
                return
    else:  # Creating a reply / comment
        reply = PostReply.get_by_ap_id(ap_id)
        if reply:
            if activity_json['type'] == 'Create':
                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Create processed after Update')
                return
            if user.id == reply.user_id or reply.community.is_moderator(user):
                if can_create_post_reply(user, community):
                    update_post_reply_from_activity(reply, activity_json)
                    log_incoming_ap(id, APLOG_UPDATE, APLOG_SUCCESS, saved_json)
                    if not announced:
                        announce_activity_to_followers(reply.community, reply.author, request_json)
                return
            else:
                log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json, 'Edit attempt denied')
                return
        else:
            if can_create_post_reply(user, community):
                try:
                    reply = create_post_reply(store_ap_json, community, in_reply_to, activity_json, user,
                                              announce_id=announce_id)
                    if reply:
                        # confirm that an Update didn't lose an async race with a Create
                        if activity_json['type'] == 'Update' and reply.edited_at is None:
                            update_post_reply_from_activity(reply, activity_json)
                            log_incoming_ap(id, APLOG_UPDATE, APLOG_SUCCESS, saved_json)
                        else:
                            log_incoming_ap(id, APLOG_CREATE, APLOG_SUCCESS, saved_json)

                        if not announced:
                            announce_activity_to_followers(community, user, request_json)
                    else:  # The reply was not allowed - send a 'Delete' to remove it from the remote instance
                        if community.is_local():
                            proactively_delete_content(community, ap_id)
                    return
                except TypeError:
                    current_app.logger.error('TypeError: ' + str(request_json))
                    log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'TypeError. See log file.')
                    return
            else:
                # The reply was not allowed - send a 'Delete' to remove it from the remote instance
                if community.is_local():
                    proactively_delete_content(community, ap_id)

                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'User cannot create reply in Community')
                return


def process_upvote(user, store_ap_json, request_json, announced):
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    ap_id = request_json['object'] if not announced else request_json['object']['object']
    if not announced:
        emoji = request_json['content'] if 'content' in request_json else None
    else:
        emoji = request_json['object']['content'] if 'content' in request_json['object'] else None

    if isinstance(ap_id, dict) and 'id' in ap_id:
        ap_id = ap_id['id']
    liked = find_liked_object(ap_id)
    if liked is None:
        log_incoming_ap(id, APLOG_LIKE, APLOG_FAILURE, saved_json, 'Unfound object ' + ap_id)
        return
    if can_upvote(user, liked.community) and not instance_banned(user.instance.domain):
        if isinstance(liked, (Post, PostReply)) and user.id not in blocked_users(liked.author.id):
            liked.vote(user, 'upvote', emoji)
            log_incoming_ap(id, APLOG_LIKE, APLOG_SUCCESS, saved_json)
            if not announced:
                announce_activity_to_followers(liked.community, user, request_json, can_batch=True)
    else:
        log_incoming_ap(id, APLOG_LIKE, APLOG_IGNORED, saved_json, 'Cannot upvote this')


def process_downvote(user, store_ap_json, request_json, announced):
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    ap_id = request_json['object'] if not announced else request_json['object']['object']
    if isinstance(ap_id, dict) and 'id' in ap_id:
        ap_id = ap_id['id']
    liked = find_liked_object(ap_id)
    if liked is None:
        log_incoming_ap(id, APLOG_DISLIKE, APLOG_FAILURE, saved_json, 'Unfound object ' + ap_id)
        return
    if can_downvote(user, liked.community) and not instance_banned(user.instance.domain):
        if isinstance(liked, (Post, PostReply)) and user.id not in blocked_users(liked.author.id):
            liked.vote(user, 'downvote', None)
            log_incoming_ap(id, APLOG_DISLIKE, APLOG_SUCCESS, saved_json)
            if not announced:
                announce_activity_to_followers(liked.community, user, request_json, can_batch=True)
        else:
            log_incoming_ap(id, APLOG_DISLIKE, APLOG_IGNORED, saved_json, 'Cannot downvote this')
    else:
        log_incoming_ap(id, APLOG_DISLIKE, APLOG_IGNORED, saved_json, 'Cannot downvote this')


def process_poll_vote(user, store_ap_json, request_json, announced):
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    ap_id = request_json['object'] if not announced else request_json['object']['object']
    choice_text = request_json['choice_text'] if not announced else request_json['object']['choice_text']
    if isinstance(ap_id, dict) and 'id' in ap_id:
        ap_id = ap_id['id']

    post = Post.get_by_ap_id(ap_id)
    if post is None:
        log_incoming_ap(id, APLOG_RATE, APLOG_FAILURE, saved_json, 'Unfound object ' + ap_id)
        return
    if not instance_banned(user.instance.domain):
        poll = db.session.query(Poll).get(post.id)
        choice = db.session.query(PollChoice).filter(PollChoice.choice_text == choice_text,
                                                     PollChoice.post_id == post.id).first()
        if choice:
            poll.vote_for_choice(choice.id, user.id)
            log_incoming_ap(id, APLOG_RATE, APLOG_SUCCESS, saved_json)
            if not announced:
                announce_activity_to_followers(post.community, user, request_json)
        else:
            log_incoming_ap(id, APLOG_RATE, APLOG_FAILURE, saved_json, 'Unfound poll choice ' + choice_text)
    else:
        log_incoming_ap(id, APLOG_RATE, APLOG_IGNORED, saved_json, 'Cannot rate this')


def process_question_answer(user, store_ap_json, request_json, announced):
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    ap_id = request_json['object'] if not announced else request_json['object']['object']
    if isinstance(ap_id, dict) and 'id' in ap_id:
        ap_id = ap_id['id']

    post_reply = PostReply.get_by_ap_id(ap_id)
    if post_reply is None:
        log_incoming_ap(id, APLOG_QA, APLOG_FAILURE, saved_json, 'Unfound object ' + ap_id)
        return
    if (not instance_banned(user.instance.domain)) and (post_reply.user_id == post_reply.post.user_id or post_reply.community.is_moderator(user) or post_reply.author.is_instance_admin()):
        from app import redis_client
        with redis_client.lock(f"lock:post_reply:{post_reply.id}", timeout=10, blocking_timeout=6):
            post_reply.answer = True
            if post_reply.author.is_local():
                targets_data = {'gen': '0',
                                'post_id': post_reply.post_id,
                                'requestor_id': user.id,
                                'author_user_name': post_reply.author.display_name(),
                                'post_title': shorten_string(post_reply.post.title, 100)}
                notify = Notification(title='Answer was chosen', url=post_reply.post.slug,
                                      user_id=post_reply.user_id,
                                      author_id=user.id, notif_type=NOTIF_ANSWER,
                                      subtype='answer_chosen',
                                      targets=targets_data)
                db.session.add(notify)
                post_reply.author.unread_notifications += 1
            db.session.commit()
        log_incoming_ap(id, APLOG_QA, APLOG_SUCCESS, saved_json)
        if not announced:
            announce_activity_to_followers(post_reply.community, user, request_json)
    else:
        log_incoming_ap(id, APLOG_QA, APLOG_IGNORED, saved_json, 'Cannot set answer')



# Private Messages, for both Create / ChatMessage (PieFed / Lemmy), and Create / Note (Mastodon, NodeBB)
# returns True if Create / Note was a PM (irrespective of whether the chat was successful)
def process_chat(user, store_ap_json, core_activity, session):
    saved_json = core_activity if store_ap_json else None
    id = core_activity['id']
    sender = session.query(User).get(user.id)
    recipient_ap_id = None

    # activity['object']['to'] must exist in the activity
    if 'to' in core_activity['object']:
        if isinstance(core_activity['object']['to'], str):
            # activity['object']['to'] is a sole string
            # jsonld arrays can be represented by the value alone to indicate the only element
            recipient_ap_id = core_activity['object']['to']
        elif isinstance(core_activity['object']['to'], list) and len(core_activity['object']['to']) > 0:
            # activity['object']['to'] is an array with at least 1 element
            recipient_ap_id = core_activity['object']['to'][0]
        
    if recipient_ap_id == None:
        log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Chat recipient is invalid')
        return False
        
    recipient = find_actor_or_create_cached(recipient_ap_id)
    if recipient and recipient.is_local():
        recipient = session.query(User).get(recipient.id)  # for some reason find_actor_or_create_cached was giving me a user from the wrong DB session, causing an exception later on.
        if sender.created_very_recently() and user.ap_domain != 'fediseer.com':
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Sender is too new')
            return True
        elif recipient.has_blocked_user(sender.id) or recipient.has_blocked_instance(sender.instance_id):
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Sender blocked by recipient')
            return True
        elif recipient.accept_private_messages is None or recipient.accept_private_messages == 0:
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Recipient has turned off PMs')
            return True
        elif recipient.accept_private_messages == 1:
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Recipient only accepts local PMs')
            return True
        elif recipient.accept_private_messages == 2 and not sender.instance.trusted:
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Sender from untrusted instance')
            return True
        else:
            blocked_phrases_list = blocked_phrases()
            if core_activity['object']['content']:
                for blocked_phrase in blocked_phrases_list:
                    if blocked_phrase in core_activity['object']['content']:
                        log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json,
                                        f'Blocked because phrase {blocked_phrase}')
                        return True
            # Find existing conversation to add to
            existing_conversation = Conversation.find_existing_conversation(recipient=recipient, sender=sender)
            if not existing_conversation:
                existing_conversation = Conversation(user_id=sender.id)
                existing_conversation.members.append(recipient)
                existing_conversation.members.append(sender)
                session.add(existing_conversation)
                session.commit()
            # Save ChatMessage to DB
            encrypted = core_activity['object']['encrypted'] if 'encrypted' in core_activity['object'] else None
            updated_message = session.query(ChatMessage).filter_by(ap_id=core_activity['object']['id']).first()
            if not updated_message:
                new_message = ChatMessage(sender_id=sender.id, recipient_id=recipient.id,
                                          conversation_id=existing_conversation.id,
                                          body_html=core_activity['object']['content'],
                                          body=html_to_text(core_activity['object']['content']),
                                          encrypted=encrypted,
                                          ap_id=core_activity['object']['id'])
                session.add(new_message)
                existing_conversation.updated_at = utcnow()
                session.commit()

                notification_text = 'New message from '
                message_id = new_message.id
            else:
                updated_message.body_html = core_activity['object']['content']
                updated_message.body = html_to_text(core_activity['object']['content'])
                updated_message.read = False
                existing_conversation.updated_at = utcnow()
                session.commit()

                notification_text = 'Updated message from '
                message_id = updated_message.id

            if recipient.is_local():
                publish_sse_event(f"messages:{recipient.id}", json.dumps({'conversation': existing_conversation.id}))
                # Notify recipient
                targets_data = {'gen': '0', 'conversation_id': existing_conversation.id, 'message_id': message_id}
                notify = Notification(title=shorten_string(notification_text + sender.display_name()),
                                      url=f'/chat/{existing_conversation.id}#message_{message_id}',
                                      user_id=recipient.id,
                                      author_id=sender.id, notif_type=NOTIF_MESSAGE, subtype='chat_message',
                                      targets=targets_data)
                session.add(notify)
                recipient.unread_notifications += 1
                existing_conversation.read = False
                session.commit()
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_SUCCESS, saved_json)

        return True

    log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'ChatMessage target is not local')
    return False


# ---- Feeds ----

@bp.route('/f/<actor>')
@bp.route('/f/<actor>/<feed_owner>', methods=['GET'])
def feed_profile(actor, feed_owner=None):
    """ Requests to this endpoint can be for a JSON representation of the feed, or an HTML rendering of it.
        The two types of requests are differentiated by the header """
    actor = actor.strip()
    if feed_owner is not None:
        feed_owner = feed_owner.strip()
        actor = actor + '/' + feed_owner
    if '@' in actor:
        # don't provide activitypub info for remote communities
        if 'application/ld+json' in request.headers.get('Accept',
                                                        '') or 'application/activity+json' in request.headers.get(
                'Accept', ''):
            abort(400)
        feed: Feed = db.session.query(Feed).filter_by(ap_id=actor.lower(), banned=False).first()
    else:
        feed: Feed = db.session.query(Feed).filter_by(name=actor.lower(), ap_id=None).first()
    if feed is not None:
        if is_activitypub_request():
            # check if feed is public, if not abort
            # with 403 (forbidden)
            if not feed.public:
                abort(403)
            server = current_app.config['SERVER_NAME']
            actor_data = {"@context": default_context(),
                          "type": "Feed",
                          "id": f"https://{server}/f/{actor}",
                          "name": feed.title,
                          "sensitive": True if feed.nsfw or feed.nsfl else False,
                          "preferredUsername": actor,
                          "inbox": f"https://{server}/f/{actor}/inbox",
                          "outbox": f"https://{server}/f/{actor}/outbox",
                          "followers": f"https://{server}/f/{actor}/followers",
                          "following": f"https://{server}/f/{actor}/following",
                          "moderators": f"https://{server}/f/{actor}/moderators",
                          "attributedTo": f"https://{server}/f/{actor}/moderators",
                          "url": f"https://{server}/f/{actor}",
                          "publicKey": {
                              "id": f"https://{server}/f/{actor}#main-key",
                              "owner": f"https://{server}/f/{actor}",
                              "publicKeyPem": feed.public_key
                          },
                          "endpoints": {
                              "sharedInbox": f"{current_app.config['SERVER_URL']}/inbox"
                          },
                          "published": ap_datetime(feed.created_at),
                          "updated": ap_datetime(feed.last_edit),
                          }
            if feed.description_html:
                actor_data["summary"] = feed.description_html
                actor_data['source'] = {'content': feed.description, 'mediaType': 'text/markdown'}
            if feed.icon_id is not None:
                icon_image = feed.icon_image()
                if icon_image.startswith('http'):
                    actor_data["icon"] = {
                        "type": "Image",
                        "url": feed.icon_image()
                    }
                else:
                    actor_data["icon"] = {
                        "type": "Image",
                        "url": f"{current_app.config['SERVER_URL']}{feed.icon_image()}"
                    }
            if feed.image_id is not None:
                header_image = feed.header_image()
                if header_image.startswith('http'):
                    actor_data["image"] = {
                        "type": "Image",
                        "url": feed.header_image()
                    }
                else:
                    actor_data["image"] = {
                        "type": "Image",
                        "url": f"{current_app.config['SERVER_URL']}{feed.header_image()}"
                    }
            actor_data['childFeeds'] = []
            for child_feed in feed.children.all():
                actor_data['childFeeds'].append(child_feed.ap_profile_id)
            resp = jsonify(actor_data)
            resp.content_type = 'application/activity+json'
            resp.headers.set('Cache-Control', 'public, max-age=5')
            resp.headers.set('Link',
                             f'<https://{current_app.config["SERVER_NAME"]}/f/{actor}>; rel="alternate"; type="text/html"')
            return resp
        else:  # browser request - return html
            return show_feed(feed)
    else:
        abort(404)


@bp.route('/f/<actor>/inbox', methods=['POST'])
def feed_inbox(actor):
    return shared_inbox()


@bp.route('/f/<actor>/outbox', methods=['GET'])
def feed_outbox(actor):
    # every AP actor has to have an /outbox
    # but I dont think it makes sense to have the Add/Remove activities in a list
    # for a Feed, so for now this will just be the same as the /following collection
    actor = actor.strip()
    if '@' in actor:
        # don't provide activitypub info for remote feeds
        abort(400)
    else:
        feed: Feed = db.session.query(Feed).filter_by(name=actor.lower(), ap_id=None).first()

    # check if feed is public, if not abort
    # with 403 (forbidden)
    if not feed.public:
        abort(403)

        # get the feed items
    feed_items = db.session.query(FeedItem).join(Feed, FeedItem.feed_id == feed.id).order_by(desc(FeedItem.id)).all()
    # make the ap data json
    items = []
    for fi in feed_items:
        c = Community.query.get(fi.community_id)
        items.append(c.ap_public_url)
    result = {
        "@context": default_context(),
        "id": feed.ap_outbox_url,
        "type": "Collection",
        "totalItems": len(items),
        "items": items
    }
    resp = jsonify(result)
    resp.content_type = 'application/activity+json'
    resp.headers.set('Cache-Control', 'public, max-age=5')
    return resp


@bp.route('/f/<actor>/following', methods=['GET'])
def feed_following(actor):
    actor = actor.strip()
    if '@' in actor:
        # don't provide activitypub info for remote feeds
        abort(400)
    else:
        feed: Feed = db.session.query(Feed).filter_by(name=actor.lower(), ap_id=None).first()

    # check if feed is public, if not abort
    # with 403 (forbidden)
    if not feed.public:
        abort(403)

        # get the feed items
    feed_items = db.session.query(FeedItem).join(Feed, FeedItem.feed_id == feed.id).order_by(desc(FeedItem.id)).all()
    # make the ap data json
    items = []
    for fi in feed_items:
        c = Community.query.get(fi.community_id)
        if c.local_only or c.private:
            continue
        items.append(c.public_url())
    result = {
        "@context": default_context(),
        "id": feed.ap_following_url,
        "type": "Collection",
        "totalItems": len(items),
        "items": items
    }
    resp = jsonify(result)
    resp.content_type = 'application/activity+json'
    resp.headers.set('Cache-Control', 'public, max-age=10')
    return resp


@bp.route('/f/<actor>/moderators', methods=['GET'])
def feed_moderators_route(actor):
    actor = actor.strip()
    if '@' in actor:
        # don't provide activitypub info for remote feeds
        abort(400)
    else:
        feed: Feed = db.session.query(Feed).filter_by(name=actor.lower(), ap_id=None).first()
    if feed is not None:
        # currently feeds only have the one owner, but lets make this a list in case we want to 
        # expand that in the future
        moderators = [db.session.query(User).get(feed.user_id)]
        moderators_data = {
            "@context": default_context(),
            "type": "OrderedCollection",
            "id": f"{current_app.config['SERVER_URL']}/f/{actor}/moderators",
            "totalItems": len(moderators),
            "orderedItems": []
        }

        for moderator in moderators:
            moderators_data['orderedItems'].append(moderator.ap_profile_id)

        resp = jsonify(moderators_data)
        resp.content_type = 'application/activity+json'
        resp.headers.set('Cache-Control', 'public, max-age=10')
        return resp


@bp.route('/f/<actor>/followers', methods=['GET'])
def feed_followers(actor):
    actor = actor.strip()
    if '@' in actor:
        # don't provide activitypub info for remote feeds
        abort(400)
    else:
        feed: Feed = db.session.query(Feed).filter_by(name=actor.lower(), ap_id=None).first()
        if feed is not None:
            result = {
                "@context": default_context(),
                "id": f'{current_app.config["SERVER_URL"]}/f/{actor}/followers',
                "type": "Collection",
                "totalItems": db.session.query(FeedMember).filter_by(feed_id=feed.id).count(),
                "items": []
            }
            resp = jsonify(result)
            resp.content_type = 'application/activity+json'
            resp.headers.set('Cache-Control', 'public, max-age=15')
            return resp
        else:
            abort(404)


@bp.route('/activitypub/externalInteraction', methods=['GET'])
def activitypub_external_interaction():
    uri = request.args.get('uri')
    if uri:
        community = find_actor_or_create_cached(uri, community_only=True)
        if community:
            return redirect(f'/community/{community.link()}/subscribe')


@bp.route('/quote_boost_auth')
def quote_boost_auth():
    import urllib.parse
    if request.args.get('stamp') is None:
        return abort(404)
    stamp = request.args.get('stamp')
    local_post_id = stamp[:stamp.index(';')]
    remote_post_id = stamp[stamp.index(';')+1:]
    response_payload = {
        '@context': [
            'https://www.w3.org/ns/activitystreams',
            {
                'gts': 'https://gotosocial.org/ns#',
                'QuoteAuthorization': {
                    '@id': 'https://w3id.org/fep/044f#QuoteAuthorization',
                    '@type': '@id',
                },
                'interactingObject': {
                    '@id': 'gts:interactingObject',
                },
                'interactionTarget': {
                    '@id': 'gts:interactionTarget',
                },
            },
        ],
        'id': current_app.config['SERVER_URL'] + '/quote_boost_auth?stamp=' + urllib.parse.quote(local_post_id, safe='') + ';' + urllib.parse.quote(remote_post_id, safe=''),
        'type': 'QuoteAuthorization',
        'attributedTo': current_app.config['SERVER_URL'],
        'interactionTarget': local_post_id,
        'interactingObject': remote_post_id,
    }
    response = make_response(response_payload)
    response.headers['Content-Type'] = 'application/activity+json'
    return response
