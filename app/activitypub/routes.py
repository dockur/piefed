import werkzeug.exceptions
from flask import request, current_app, abort, jsonify, json, g, url_for, redirect, make_response, flash
from flask_babel import _
from flask_login import current_user
from psycopg2 import IntegrityError
from sqlalchemy import desc, or_, text

from app import db, cache, celery, limiter
from app.activitypub import bp
from app.activitypub.signature import HttpSignature, VerificationError, default_context, LDSignature, \
    send_post_request
from app.activitypub.util import users_total, active_half_year, active_month, local_posts, local_comments, \
    post_to_activity, find_actor_or_create, find_liked_object, \
    lemmy_site_data, is_activitypub_request, delete_post_or_comment, community_members, \
    create_post, create_post_reply, update_post_reply_from_activity, \
    update_post_from_activity, undo_vote, post_to_page, find_reported_object, \
    process_report, ensure_domains_match, resolve_remote_post, refresh_community_profile, \
    comment_model_to_json, restore_post_or_comment, ban_user, unban_user, \
    log_incoming_ap, find_community, site_ban_remove_data, community_ban_remove_data, verify_object_from_source, \
    post_replies_for_ap
from app.community.routes import show_community
from app.community.util import send_to_remote_instance
from app.constants import *
from app.feed.routes import show_feed
from app.models import User, Community, CommunityJoinRequest, CommunityMember, CommunityBan, ActivityPubLog, Post, \
    PostReply, Instance, AllowedInstances, BannedInstances, utcnow, Site, Notification, \
    ChatMessage, Conversation, UserFollower, UserBlock, Poll, PollChoice, Feed, FeedItem, FeedMember, FeedJoinRequest, \
    IpBan, ActivityBatch
from app.post.routes import continue_discussion, show_post
from app.shared.tasks import task_selector
from app.user.routes import show_profile
from app.utils import gibberish, get_setting, community_membership, ap_datetime, ip_address, can_downvote, \
    can_upvote, can_create_post, awaken_dormant_instance, shorten_string, can_create_post_reply, sha256_digest, \
    community_moderators, html_to_text, add_to_modlog, instance_banned, get_redis_connection, \
    feed_membership, get_task_session, patch_db_session, \
    blocked_phrases


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
def webfinger():
    if request.args.get('resource'):
        feed = False
        query = request.args.get('resource')  # acct:alice@tada.club
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
                "aliases": [f"https://{current_app.config['SERVER_NAME']}/actor"],
                "links": [
                    {
                        "rel": "http://webfinger.net/rel/profile-page",
                        "type": "text/html",
                        "href": f"https://{current_app.config['SERVER_NAME']}/about"
                    },
                    {
                        "rel": "self",
                        "type": "application/activity+json",
                        "href": f"https://{current_app.config['SERVER_NAME']}/actor",
                    }
                ]
            }
            resp = jsonify(webfinger_data)
            resp.headers.add_header('Access-Control-Allow-Origin', '*')
            return resp

        if not feed:
            # look for the User first, then the Community, then the Feed that matches
            seperator = 'u'
            type = 'Person'
            user = User.query.filter(
                or_(User.user_name == actor.strip(), User.alt_user_name == actor.strip())).filter_by(deleted=False,
                                                                                                     banned=False,
                                                                                                     ap_id=None).first()
            if user is None:
                community = Community.query.filter_by(name=actor.strip(), ap_id=None).first()
                seperator = 'c'
                type = 'Group'
                if community is None:
                    feed = Feed.query.filter_by(name=actor.strip(), ap_id=None).first()
                    if feed is None:
                        return ''
                    seperator = 'f'
                    type = 'Feed'
        else:
            feed = Feed.query.filter_by(name=actor.strip(), ap_id=None).first()
            if feed is None:
                return ''
            seperator = 'f'
            type = 'Feed'

        webfinger_data = {
            "subject": f"acct:{actor}@{current_app.config['SERVER_NAME']}",
            "aliases": [f"https://{current_app.config['SERVER_NAME']}/{seperator}/{actor}"],
            "links": [
                {
                    "rel": "http://webfinger.net/rel/profile-page",
                    "type": "text/html",
                    "href": f"https://{current_app.config['SERVER_NAME']}/{seperator}/{actor}"
                },
                {
                    "rel": "self",
                    "type": "application/activity+json",
                    "href": f"https://{current_app.config['SERVER_NAME']}/{seperator}/{actor}",
                    "properties": {
                        "https://www.w3.org/ns/activitystreams#type": type
                    }
                }
            ]
        }
        resp = jsonify(webfinger_data)
        resp.headers.add_header('Access-Control-Allow-Origin', '*')
        return resp
    else:
        abort(404)


@bp.route('/.well-known/nodeinfo')
@cache.cached(timeout=600)
def nodeinfo():
    nodeinfo_data = {"links": [{"rel": "https://www.w3.org/ns/activitystreams#Application",
                                "href": f"https://{current_app.config['SERVER_NAME']}"},
                               {"rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                                "href": f"https://{current_app.config['SERVER_NAME']}/nodeinfo/2.0"},
                               {"rel": "http://nodeinfo.diaspora.software/ns/schema/2.1",
                                "href": f"https://{current_app.config['SERVER_NAME']}/nodeinfo/2.1"},
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
        "metadata": {}
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
    return jsonify(lemmy_site_data())


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
    return jsonify({
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
            user: User = User.query.filter(or_(User.user_name == actor)).filter_by(ap_id=None).first()
            if user is None:
                user = User.query.filter_by(ap_profile_id=f'https://{current_app.config["SERVER_NAME"]}/u/{actor.lower()}', ap_id=None).first()
    else:
        if '@' in actor:
            user: User = User.query.filter_by(ap_id=actor.lower()).first()
        else:
            user: User = User.query.filter(or_(User.user_name == actor)).filter_by(ap_id=None).first()
            if user is None:
                user = User.query.filter_by(ap_profile_id=f'https://{current_app.config["SERVER_NAME"]}/u/{actor.lower()}', ap_id=None).first()

    if user is not None:
        if request.method == 'HEAD':
            if is_activitypub_request():
                resp = jsonify('')
                resp.content_type = 'application/activity+json'
                return resp
            else:
                return ''
        if is_activitypub_request():
            server = current_app.config['SERVER_NAME']
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
                              "sharedInbox": f"https://{server}/inbox"
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
                        "url": f"https://{current_app.config['SERVER_NAME']}{user.avatar_image()}"
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
                        "url": f"https://{current_app.config['SERVER_NAME']}{user.cover_image()}"
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
        'id': f"https://{current_app.config['SERVER_NAME']}/u/{actor}/outbox",
        'orderedItems': [],
        'totalItems': 0
    }
    resp = jsonify(outbox)
    resp.content_type = 'application/activity+json'
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
        community: Community = Community.query.filter_by(name=actor, ap_id=None).first()
    if community is not None:
        if is_activitypub_request():
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
                          "newModsWanted": community.new_mods_wanted,
                          "privateMods": community.private_mods,
                          "url": f"https://{server}/c/{actor}",
                          "publicKey": {
                              "id": f"https://{server}/c/{actor}#main-key",
                              "owner": f"https://{server}/c/{actor}",
                              "publicKeyPem": community.public_key
                          },
                          "endpoints": {
                              "sharedInbox": f"https://{server}/inbox"
                          },
                          "published": ap_datetime(community.created_at),
                          "updated": ap_datetime(community.last_active),
                          "lemmy:tagsForPosts": community.flair_for_ap()
                          }
            if community.description_html:
                actor_data["summary"] = community.description_html
                actor_data['source'] = {'content': community.description, 'mediaType': 'text/markdown'}
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
                        "url": f"https://{current_app.config['SERVER_NAME']}{community.icon_image()}"
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
                        "url": f"https://{current_app.config['SERVER_NAME']}{community.header_image()}"
                    }
            resp = jsonify(actor_data)
            resp.content_type = 'application/activity+json'
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


@bp.route('/inbox', methods=['POST'])
def shared_inbox():
    from app import redis_client
    try:
        request_json = request.get_json(force=True)
    except werkzeug.exceptions.BadRequest as e:
        log_incoming_ap('', APLOG_NOTYPE, APLOG_FAILURE, None, 'Unable to parse json body: ' + e.description)
        return '', 200

    g.site = Site.query.get(1)  # g.site is not initialized by @app.before_request when request.path == '/inbox'
    store_ap_json = g.site.log_activitypub_json or False
    saved_json = request_json if store_ap_json else None

    if not 'id' in request_json or not 'type' in request_json or not 'actor' in request_json or not 'object' in request_json:
        log_incoming_ap('', APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'Missing minimum expected fields in JSON')
        return '', 200

    id = request_json['id']
    if request_json['type'] == 'Announce' and isinstance(request_json['object'], dict):
        object = request_json['object']
        if not 'id' in object or not 'type' in object or not 'actor' in object or not 'object' in object:
            if 'type' in object and (object['type'] == 'Page' or object['type'] == 'Note'):
                log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Intended for Mastodon')
            else:
                log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json,
                                'Missing minimum expected fields in JSON Announce object')
            return '', 200

        if isinstance(object['actor'], str) and object['actor'].startswith(
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

    # Ignore account deletion requests from users that do not already exist here
    account_deletion = False
    if (request_json['type'] == 'Delete' and
            'object' in request_json and isinstance(request_json['object'], str) and
            request_json['actor'] == request_json['object']):
        account_deletion = True
        actor = db.session.query(User).filter_by(ap_profile_id=request_json['actor'].lower()).first()
        if not actor:
            log_incoming_ap(id, APLOG_DELETE, APLOG_IGNORED, saved_json, 'Does not exist here')
            return '', 200
    else:
        actor = find_actor_or_create(request_json['actor'])

    if not actor:
        actor_name = request_json['actor']
        log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, f'Actor could not be found 1 - : {actor_name}, actor object: {actor}')
        return '', 200

    if actor.is_local():  # should be impossible (can be Announced back, but not sent without access to privkey)
        log_incoming_ap(id, APLOG_NOTYPE, APLOG_FAILURE, saved_json, 'ActivityPub activity from a local actor')
        return '', 200

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
        if not 'id' in object or not 'type' in object or not 'actor' in object or not 'object' in object:
            if 'type' in object and (object['type'] == 'Page' or object['type'] == 'Note'):
                log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, request_json, 'REPLAY: Intended for Mastodon')
            else:
                log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, request_json, 'REPLAY: Missing minimum expected fields in JSON Announce object')
            return

        if isinstance(object['actor'], str) and object['actor'].startswith(
                'https://' + current_app.config['SERVER_NAME']):
            log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, request_json, 'REPLAY: Activity about local content which is already present')
            return

    # Ignore unutilised PeerTube activity
    if isinstance(request_json['actor'], str) and request_json['actor'].endswith('accounts/peertube'):
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
        actor = find_actor_or_create(request_json['actor'])

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
            # although process_inbox_request uses session instead of db.session, many of the functions it calls, like find_actor_or_create, do not which makes this necessary.
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
                feed = community = None
                if request_json['type'] == 'Announce' or request_json['type'] == 'Accept' or request_json['type'] == 'Reject':
                    community = find_actor_or_create(actor_id, community_only=True, create_if_not_found=False)
                    if not community:
                        feed = find_actor_or_create(actor_id, feed_only=True, create_if_not_found=False)
                    if not community and not feed:
                        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Actor was not a feed or a community')
                        return
                else:
                    actor = find_actor_or_create(actor_id, create_if_not_found=False)
                    if actor and isinstance(actor, User):
                        user = actor
                        # Update user's last_seen in a separate transaction to avoid deadlocks
                        with redis_client.lock(f"lock:user:{user.id}", timeout=10, blocking_timeout=6):
                            session.execute(text('UPDATE "user" SET last_seen=:last_seen WHERE id = :user_id'),
                                               {"last_seen": utcnow(), "user_id": user.id})
                            session.commit()
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
                            if obj['audience'] == request_json['actor']:    # Check if same community. This ensures the HTTP Sig on the original announce is valid for all objects.
                                fake_activity = request_json.copy()
                                fake_activity['object'] = obj
                                process_inbox_request(fake_activity, store_ap_json)  # Process the Announce (with single object) as normal
                        return

                    if not feed:
                        user_ap_id = request_json['object']['actor']
                        user = find_actor_or_create(user_ap_id)
                        if user and isinstance(user, User):
                            if user.banned:
                                log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, f'{user_ap_id} is banned')
                                return

                            with redis_client.lock(f"lock:user:{user.id}", timeout=10, blocking_timeout=6):
                                user.last_seen = utcnow()
                                user.instance.last_seen = utcnow()
                                user.instance.dormant = False
                                user.instance.gone_forever = False
                                user.instance.failures = 0
                                session.commit()
                        else:
                            log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Blocked or unfound user for Announce object actor ' + user_ap_id)
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
                    target = find_actor_or_create(target_ap_id, create_if_not_found=False)
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
                                      "id": f"https://{current_app.config['SERVER_NAME']}/activities/reject/" + gibberish(32)}
                            send_post_request(user.ap_inbox_url, reject, community.private_key, f"{community.public_url()}#main-key")
                        else:
                            existing_member = session.query(CommunityMember).filter_by(user_id=user.id, community_id=community.id).first()
                            if not existing_member:
                                member = CommunityMember(user_id=user.id, community_id=community.id)
                                session.add(member)
                                community.subscriptions_count += 1
                                community.last_active = utcnow()
                                session.commit()
                                cache.delete_memoized(community_membership, user, community)
                                # send accept message to acknowledge the follow
                                accept = {"@context": default_context(), "actor": community.public_url(),
                                          "to": [user.public_url()],
                                          "object": {"actor": user.public_url(), "to": None,
                                                     "object": community.public_url(), "type": "Follow", "id": follow_id},
                                          "type": "Accept",
                                          "id": f"https://{current_app.config['SERVER_NAME']}/activities/accept/" + gibberish(32)}
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
                                      "id": f"https://{current_app.config['SERVER_NAME']}/activities/reject/" + gibberish(32)}
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
                                          "id": f"https://{current_app.config['SERVER_NAME']}/activities/accept/" + gibberish(32)}
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
                                      "id": f"https://{current_app.config['SERVER_NAME']}/activities/accept/" + gibberish(32)}
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
                        user_ap_id = core_activity['object']['actor']
                        user = find_actor_or_create(user_ap_id, create_if_not_found=False)
                        if user and user.banned:
                            log_incoming_ap(id, APLOG_ACCEPT, APLOG_FAILURE, saved_json, f'{user_ap_id} is banned')
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
                        user_ap_id = core_activity['object']['actor']
                        user = find_actor_or_create(user_ap_id, create_if_not_found=False)
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
                                    session.commit()
                                    log_incoming_ap(id, APLOG_CREATE, APLOG_SUCCESS, saved_json)
                                    if post_being_replied_to.author.is_local():
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
                        new_content_types = ['Page', 'Article', 'Link', 'Note', 'Question']
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
                            refresh_community_profile(community.id, core_activity['object'])
                            log_incoming_ap(id, APLOG_UPDATE, APLOG_SUCCESS, saved_json)
                        else:
                            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Unacceptable type (create): ' + object_type)
                    return

                if core_activity['type'] == 'Delete':
                    # check if its a feed being deleted
                    if isinstance(core_activity['object'], dict) and core_activity['object']['type'] == 'Feed':
                        # find the user in the traffic
                        user = find_actor_or_create(actor_id)
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
                    return

                if core_activity['type'] == 'Like' or core_activity['type'] == 'EmojiReact':  # Upvote
                    process_upvote(user, store_ap_json, request_json, announced)
                    return

                if core_activity['type'] == 'Dislike':  # Downvote
                    process_downvote(user, store_ap_json, request_json, announced)
                    return

                if core_activity['type'] == 'Flag':  # Reported content
                    reported = find_reported_object(core_activity['object'])
                    if reported:
                        process_report(user, reported, core_activity)
                        log_incoming_ap(id, APLOG_REPORT, APLOG_SUCCESS, saved_json)
                        announce_activity_to_followers(reported.community, user, request_json)
                    else:
                        log_incoming_ap(id, APLOG_REPORT, APLOG_IGNORED, saved_json,
                                        'Report ignored due to missing content')
                    return

                if core_activity['type'] == 'Lock':  # Post lock
                    mod = user
                    post = Post.get_by_ap_id(core_activity['object'])
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
                    else:
                        log_incoming_ap(id, APLOG_LOCK, APLOG_FAILURE, saved_json, 'Lock: post not found')
                    return

                if core_activity['type'] == 'Add':  # Add mods, sticky a post, or add a community to a feed
                    if user is not None:
                        mod = user
                    if not announced and not feed:
                        community = find_community(core_activity)
                    if feed and 'id' in core_activity['object']:
                        community_to_add = find_actor_or_create(core_activity['object']['id'], community_only=True)
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
                            new_mod = find_actor_or_create(core_activity['object'])
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
                        community_to_remove = find_actor_or_create(core_activity['object']['id'], community_only=True)
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
                                                follow_id = f"https://{current_app.config['SERVER_NAME']}/activities/follow/{gibberish(15)}"
                                                if community_to_remove.instance.domain == 'a.gup.pe':
                                                    join_request = session.query(CommunityJoinRequest).filter_by(user_id=fm_user.id,
                                                                                                        community_id=community_to_remove.id).first()
                                                    if join_request:
                                                        follow_id = f"https://{current_app.config['SERVER_NAME']}/activities/follow/{join_request.uuid}"
                                                undo_id = f"https://{current_app.config['SERVER_NAME']}/activities/undo/" + gibberish(15)
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
                            old_mod = find_actor_or_create(core_activity['object'])
                            if old_mod:
                                existing_membership = session.query(CommunityMember).filter_by(community_id=community.id,
                                                                                      user_id=old_mod.id).first()
                                if existing_membership:
                                    existing_membership.is_moderator = False
                                    session.commit()
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
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_MONITOR, request_json,
                                                'Remote Admin in banning one of our users from their site')
                                current_app.logger.error('Remote Admin in banning one of our users from their site: ' + str(request_json))
                                return
                            if blocked.instance_id != blocker.instance_id:
                                log_incoming_ap(id, APLOG_USERBAN, APLOG_MONITOR, request_json,
                                                'Remote Admin is banning a user of a different instance from their site')
                                current_app.logger.error('Remote Admin is banning a user of a different instance from their site: ' + str(request_json))
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
                            community = community if community else find_actor_or_create(target, create_if_not_found=False,
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
                        target = find_actor_or_create(target_ap_id, create_if_not_found=False)
                        if isinstance(target, Community):
                            community = target
                            member = session.query(CommunityMember).filter_by(user_id=user.id, community_id=community.id).first()
                            join_request = session.query(CommunityJoinRequest).filter_by(user_id=user.id,
                                                                                community_id=community.id).first()
                            if member:
                                session.delete(member)
                                community.subscriptions_count -= 1
                                community.last_active = utcnow()
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
                            log_incoming_ap(id, APLOG_UNDO_DELETE, APLOG_FAILURE, saved_json,
                                            'Undo delete: cannot find ' + ap_id)
                        return

                    if core_activity['object']['type'] == 'Like' or core_activity['object']['type'] == 'Dislike':  # Undoing an upvote or downvote
                        post = comment = None
                        target_ap_id = core_activity['object']['object']
                        post_or_comment = undo_vote(comment, post, target_ap_id, user)
                        if post_or_comment:
                            log_incoming_ap(id, APLOG_UNDO_VOTE, APLOG_SUCCESS, saved_json)
                            if not announced:
                                announce_activity_to_followers(post_or_comment.community, user, request_json)
                        else:
                            log_incoming_ap(id, APLOG_UNDO_VOTE, APLOG_FAILURE, saved_json,
                                            'Unfound object ' + target_ap_id)
                        return

                    if core_activity['object']['type'] == 'Lock':  # Undo of post lock
                        mod = user
                        post = Post.get_by_ap_id(core_activity['object']['object'])
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
                                log_incoming_ap(id, APLOG_LOCK, APLOG_FAILURE, saved_json, 'Lock: Does not have permission')
                        else:
                            log_incoming_ap(id, APLOG_LOCK, APLOG_FAILURE, saved_json, 'Lock: post not found')
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
                            community = community if community else find_actor_or_create(target, create_if_not_found=False,
                                                                                         community_only=True)
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

                    log_incoming_ap(id, APLOG_MONITOR, APLOG_PROCESSING, request_json, 'Unmatched activity')
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
                    if 'removeData' in request_json and request_json['removeData'] is True:
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


def announce_activity_to_followers(community: Community, creator: User, activity, can_batch=False):
    # avoid announcing activity sent to local users unless it is also in a local community
    if not community.is_local():
        return

    if creator.banned:
        return

    # remove context from what will be inner object
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
        "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}"
    }

    for instance in community.following_instances(include_dormant=True):
        # awaken dormant instances if they've been sleeping for long enough to be worth trying again
        awaken_dormant_instance(instance)

        # All good? Send!
        if instance and instance.online() and not instance_banned(instance.inbox):
            if creator.instance_id != instance.id:  # don't send it to the instance that hosts the creator as presumably they already have the content
                if can_batch and current_app.config['FEP_AWESOME'] and instance.software == 'piefed':
                    db.session.add(ActivityBatch(instance_id=instance.id, community_id=community.id,
                                                 payload=activity))
                    db.session.commit()
                else:
                    send_to_remote_instance(instance.id, community.id, announce_activity)


@bp.route('/c/<actor>/outbox', methods=['GET'])
def community_outbox(actor):
    actor = actor.strip()
    community = Community.query.filter_by(name=actor, banned=False, ap_id=None).first()
    if community is not None:
        sticky_posts = community.posts.filter(Post.sticky == True, Post.deleted == False,
                                              Post.status > POST_STATUS_REVIEWING).order_by(desc(Post.posted_at)).limit(50).all()
        remaining_limit = 50 - len(sticky_posts)
        remaining_posts = community.posts.filter(Post.sticky == False, Post.deleted == False,
                                                 Post.status > POST_STATUS_REVIEWING).order_by(
            desc(Post.posted_at)).limit(remaining_limit).all()
        posts = sticky_posts + remaining_posts

        community_data = {
            "@context": default_context(),
            "type": "OrderedCollection",
            "id": f"https://{current_app.config['SERVER_NAME']}/c/{actor}/outbox",
            "totalItems": len(posts),
            "orderedItems": []
        }

        for post in posts:
            community_data['orderedItems'].append(post_to_activity(post, community))

        return jsonify(community_data)
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
            "id": f"https://{current_app.config['SERVER_NAME']}/c/{actor}/featured",
            "totalItems": len(posts),
            "orderedItems": []
        }

        for post in posts:
            community_data['orderedItems'].append(post_to_page(post))

        return jsonify(community_data)
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
            "id": f"https://{current_app.config['SERVER_NAME']}/c/{actor}/moderators",
            "totalItems": len(moderators),
            "orderedItems": []
        }

        for moderator in moderators:
            community_data['orderedItems'].append(moderator.public_url())

        resp = jsonify(community_data)
        resp.content_type = 'application/activity+json'
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
            "id": f'https://{current_app.config["SERVER_NAME"]}/c/{actor}/followers',
            "type": "Collection",
            "totalItems": community_members(community.id),
            "items": []
        }
        resp = jsonify(result)
        resp.content_type = 'application/activity+json'
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
        return resp
    else:
        abort(404)


@bp.route('/comment/<int:comment_id>', methods=['GET', 'HEAD'])
def comment_ap(comment_id):
    reply = PostReply.query.get_or_404(comment_id)
    if is_activitypub_request():
        reply_data = comment_model_to_json(reply) if request.method == 'GET' else []
        resp = jsonify(reply_data)
        resp.content_type = 'application/activity+json'
        resp.headers.set('Vary', 'Accept')
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
        post = Post.query.get_or_404(post_id)
        if request.method == 'GET':
            post_data = post_to_page(post)
            post_data['@context'] = default_context()
        else:  # HEAD request
            post_data = []
        resp = jsonify(post_data)
        resp.content_type = 'application/activity+json'
        resp.headers.set('Vary', 'Accept')
        resp.headers.set('Link',
                         f'<https://{current_app.config["SERVER_NAME"]}/post/{post.id}>; rel="alternate"; type="text/html"')
        return resp
    else:
        return show_post(post_id)


@bp.route('/post/<int:post_id>/replies', methods=['GET'])
def post_replies_ap(post_id):
    if request.method == 'GET' or request.method == 'HEAD' and is_activitypub_request():
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
        return resp


@bp.route('/activities/<type>/<id>')
@cache.cached(timeout=600)
def activities_json(type, id):
    activity = ActivityPubLog.query.filter_by(
        activity_id=f"https://{current_app.config['SERVER_NAME']}/activities/{type}/{id}").first()
    if activity:
        if activity.activity_json is not None:
            activity_json = json.loads(activity.activity_json)
        else:
            activity_json = {}
        resp = jsonify(activity_json)
        resp.content_type = 'application/activity+json'
        return resp
    else:
        abort(404)


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
            if user.id == post.user_id:
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
                        log_incoming_ap(id, APLOG_CREATE, APLOG_SUCCESS, saved_json)
                        if not announced:
                            announce_activity_to_followers(community, user, request_json)
                        return
                except TypeError:
                    current_app.logger.error('TypeError: ' + str(request_json))
                    log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'TypeError. See log file.')
                    return
            else:
                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'User cannot create post in Community')
                return
    else:  # Creating a reply / comment
        reply = PostReply.get_by_ap_id(ap_id)
        if reply:
            if activity_json['type'] == 'Create':
                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Create processed after Update')
                return
            if user.id == reply.user_id:
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
                        log_incoming_ap(id, APLOG_CREATE, APLOG_SUCCESS, saved_json)
                        if not announced:
                            announce_activity_to_followers(community, user, request_json)
                    return
                except TypeError:
                    current_app.logger.error('TypeError: ' + str(request_json))
                    log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'TypeError. See log file.')
                    return
            else:
                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'User cannot create reply in Community')
                return


def process_upvote(user, store_ap_json, request_json, announced):
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    ap_id = request_json['object'] if not announced else request_json['object']['object']
    if isinstance(ap_id, dict) and 'id' in ap_id:
        ap_id = ap_id['id']
    liked = find_liked_object(ap_id)
    if liked is None:
        log_incoming_ap(id, APLOG_LIKE, APLOG_FAILURE, saved_json, 'Unfound object ' + ap_id)
        return
    if can_upvote(user, liked.community):
        if isinstance(liked, (Post, PostReply)):
            liked.vote(user, 'upvote')
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
    if can_downvote(user, liked.community):
        if isinstance(liked, (Post, PostReply)):
            liked.vote(user, 'downvote')
            log_incoming_ap(id, APLOG_DISLIKE, APLOG_SUCCESS, saved_json)
            if not announced:
                announce_activity_to_followers(liked.community, user, request_json, can_batch=True)
    else:
        log_incoming_ap(id, APLOG_DISLIKE, APLOG_IGNORED, saved_json, 'Cannot downvote this')


# Private Messages, for both Create / ChatMessage (PieFed / Lemmy), and Create / Note (Mastodon, NodeBB)
# returns True if Create / Note was a PM (irrespective of whether the chat was successful)
def process_chat(user, store_ap_json, core_activity, session):
    saved_json = core_activity if store_ap_json else None
    id = core_activity['id']
    sender = user
    if not ('to' in core_activity['object'] and
            isinstance(core_activity['object']['to'], list) and
            len(core_activity['object']['to']) > 0):
        return False
    recipient_ap_id = core_activity['object']['to'][0]
    recipient = find_actor_or_create(recipient_ap_id, create_if_not_found=False)
    if recipient and recipient.is_local():
        if sender.ap_profile_id != 'https://fediseer.com/api/v1/user/fediseer' and (sender.created_recently() or sender.reputation <= -10):
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Sender not eligible to send', session=session)
            return True
        elif recipient.has_blocked_user(sender.id) or recipient.has_blocked_instance(sender.instance_id):
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Sender blocked by recipient', session=session)
            return True
        elif recipient.accept_private_messages is None or recipient.accept_private_messages == 0:
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Recipient has turned off PMs', session=session)
            return True
        elif recipient.accept_private_messages == 1:
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Recipient only accepts local PMs', session=session)
            return True
        elif recipient.accept_private_messages == 2 and not sender.instance.trusted:
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, 'Sender from untrusted instance', session=session)
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
            new_message = ChatMessage(sender_id=sender.id, recipient_id=recipient.id,
                                      conversation_id=existing_conversation.id,
                                      body_html=core_activity['object']['content'],
                                      body=html_to_text(core_activity['object']['content']),
                                      encrypted=encrypted,
                                      ap_id=core_activity['object']['id'])
            session.add(new_message)
            existing_conversation.updated_at = utcnow()
            session.commit()

            # Notify recipient
            targets_data = {'gen': '0', 'conversation_id': existing_conversation.id, 'message_id': new_message.id}
            notify = Notification(title=shorten_string('New message from ' + sender.display_name()),
                                  url=f'/chat/{existing_conversation.id}#message_{new_message.id}',
                                  user_id=recipient.id,
                                  author_id=sender.id, notif_type=NOTIF_MESSAGE, subtype='chat_message',
                                  targets=targets_data)
            session.add(notify)
            recipient.unread_notifications += 1
            existing_conversation.read = False
            session.commit()
            log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_SUCCESS, saved_json, session=session)

        return True

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
                              "sharedInbox": f"https://{server}/inbox"
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
                        "url": f"https://{current_app.config['SERVER_NAME']}{feed.icon_image()}"
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
                        "url": f"https://{current_app.config['SERVER_NAME']}{feed.header_image()}"
                    }
            actor_data['childFeeds'] = []
            for child_feed in feed.children.all():
                actor_data['childFeeds'].append(child_feed.ap_profile_id)
            resp = jsonify(actor_data)
            resp.content_type = 'application/activity+json'
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
            "id": f"https://{current_app.config['SERVER_NAME']}/f/{actor}/moderators",
            "totalItems": len(moderators),
            "orderedItems": []
        }

        for moderator in moderators:
            moderators_data['orderedItems'].append(moderator.ap_profile_id)

        return jsonify(moderators_data)


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
                "id": f'https://{current_app.config["SERVER_NAME"]}/f/{actor}/followers',
                "type": "Collection",
                "totalItems": db.session.query(FeedMember).filter_by(feed_id=feed.id).count(),
                "items": []
            }
            resp = jsonify(result)
            resp.content_type = 'application/activity+json'
            return resp
        else:
            abort(404)
