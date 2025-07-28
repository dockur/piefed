from datetime import datetime

from dateutil.relativedelta import relativedelta
from flask import current_app
from sqlalchemy import desc, or_

from app import db, cache
from app.api.alpha.utils.validators import required, integer_expected, boolean_expected, string_expected, \
    array_of_integers_expected
from app.api.alpha.views import community_view, user_view, post_view, cached_modlist_for_community
from app.community.util import search_for_community
from app.constants import *
from app.models import Community, CommunityMember, User, CommunityBan, Notification, CommunityJoinRequest, \
    NotificationSubscription, Post, utcnow
from app.shared.community import join_community, leave_community, block_community, unblock_community, make_community, \
    edit_community, subscribe_community, delete_community, restore_community, add_mod_to_community, \
    remove_mod_from_community
from app.shared.tasks import task_selector
from app.utils import authorise_api_user
from app.utils import communities_banned_from, blocked_instances, blocked_communities, shorten_string, \
    joined_communities, moderating_communities


def get_community_list(auth, data):
    type_ = data['type_'] if data and 'type_' in data else "All"
    sort = data['sort'] if data and 'sort' in data else "Hot"
    page = int(data['page']) if data and 'page' in data else 1
    limit = int(data['limit']) if data and 'limit' in data else 10
    show_nsfw = data['show_nsfw'] if data and 'show_nsfw' in data else 'false'
    show_nsfw = True if show_nsfw == 'true' else False
    show_nsfl = show_nsfw

    user = authorise_api_user(auth, return_type='model') if auth else None
    user_id = user.id if user else None

    query = data['q'] if data and 'q' in data else ''
    if user_id and '@' in query and '.' in query and query.startswith('!'):
        search_for_community(query)
        query = query[1:]

    if user_id and type_ == 'Subscribed':
        communities = Community.query.filter_by(banned=False).join(CommunityMember).filter(
            CommunityMember.user_id == user_id)
    elif type_ == 'Local':
        communities = Community.query.filter_by(ap_id=None, banned=False)
    else:
        communities = Community.query.filter_by(banned=False)

    if user_id:
        banned_from = communities_banned_from(user_id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))
        blocked_instance_ids = blocked_instances(user_id)
        if blocked_instance_ids:
            communities = communities.filter(Community.instance_id.not_in(blocked_instance_ids))
        blocked_community_ids = blocked_communities(user_id)
        if blocked_community_ids:
            communities = communities.filter(Community.id.not_in(blocked_community_ids))
        if user.hide_nsfw and not show_nsfw:
            communities = communities.filter(Community.nsfw == False)
        if user.hide_nsfl and not show_nsfl:
            communities = communities.filter(Community.nsfl == False)
    else:
        if not show_nsfw:
            communities = communities.filter_by(nsfw=False)
        if not show_nsfl:
            communities = communities.filter_by(nsfl=False)

    if query:
        communities = communities.filter(or_(Community.title.ilike(f"%{query}%"), Community.ap_id.ilike(f"%{query}%")))

    if sort == 'New':
        communities = communities.order_by(desc(Community.created_at))
    elif sort.startswith('Top'):
        communities = communities.order_by(desc(Community.post_count))
    else:
        communities = communities.order_by(desc(Community.last_active))

    communities = communities.paginate(page=page, per_page=limit, error_out=False)

    communitylist = []
    for community in communities:
        communitylist.append(community_view(community=community, variant=2, stub=False, user_id=user_id))
    list_json = {
        "communities": communitylist,
        'next_page': str(communities.next_num) if communities.next_num else None
    }

    return list_json


def get_community(auth, data):
    if not data or ('id' not in data and 'name' not in data):
        raise Exception('missing parameters for community')
    if 'id' in data:
        community = int(data['id'])
    elif 'name' in data:
        community = data['name']
        if '@' not in community:
            community = f"{community}@{current_app.config['SERVER_NAME']}"

    user_id = authorise_api_user(auth) if auth else None

    try:
        community_json = community_view(community=community, variant=3, stub=False, user_id=user_id)
        return community_json
    except:
        if 'name' in data:
            query = data['name']
            if user_id and '@' in query and '.' in query:
                if not query.startswith('!'):
                    query = '!' + query
                search_for_community(query)
        raise Exception('error - unknown community. Please wait a sec and try again.')


def post_community_follow(auth, data):
    required(['community_id', 'follow'], data)
    integer_expected(['community_id'], data)
    boolean_expected(['follow'], data)

    community_id = data['community_id']
    follow = data['follow']

    user_id = join_community(community_id, SRC_API, auth) if follow else leave_community(community_id, SRC_API, auth)
    community_json = community_view(community=community_id, variant=4, stub=False, user_id=user_id)
    return community_json


def post_community_block(auth, data):
    required(['community_id', 'block'], data)
    integer_expected(['community_id'], data)
    boolean_expected(['block'], data)

    community_id = data['community_id']
    block = data['block']

    user_id = block_community(community_id, SRC_API, auth) if block else unblock_community(community_id, SRC_API, auth)
    community_json = community_view(community=community_id, variant=5, user_id=user_id)
    return community_json


def post_community(auth, data):
    required(['name', 'title'], data)
    string_expected(['name', 'title', 'description', 'rules', 'icon_url', 'banner_url'], data)
    boolean_expected(['nsfw', 'restricted_to_mods', 'local_only'], data)
    array_of_integers_expected(['discussion_languages'], data)

    name = data['name']
    title = data['title']
    description = data['description'] if 'description' in data else ''
    rules = data['rules'] if 'rules' in data else ''
    icon_url = data['icon_url'] if 'icon_url' in data else None
    banner_url = data['banner_url'] if 'banner_url' in data else None
    nsfw = data['nsfw'] if 'nsfw' in data else False
    restricted_to_mods = data['restricted_to_mods'] if 'restricted_to_mods' in data else False
    local_only = data['local_only'] if 'local_only' in data else False
    discussion_languages = data['discussion_languages'] if 'discussion_languages' in data else [2]  # FIXME: use site language

    input = {'name': name, 'title': title, 'description': description, 'rules': rules,
             'icon_url': icon_url, 'banner_url': banner_url,
             'nsfw': nsfw, 'restricted_to_mods': restricted_to_mods, 'local_only': local_only,
             'discussion_languages': discussion_languages}

    user_id, community_id = make_community(input, SRC_API, auth)
    community_json = community_view(community=community_id, variant=4, user_id=user_id)
    return community_json


def put_community(auth, data):
    required(['community_id'], data)
    integer_expected(['community_id'], data)
    string_expected(['title', 'description', 'rules', 'icon_url', 'banner_url'], data)
    boolean_expected(['nsfw', 'restricted_to_mods', 'local_only'], data)
    array_of_integers_expected(['discussion_languages'], data)

    community_id = data['community_id']
    community = Community.query.filter_by(id=community_id).one()

    title = data['title'] if 'title' in data else community.title
    description = data['description'] if 'description' in data else community.description
    rules = data['rules'] if 'rules' in data else community.rules
    if 'icon_url' in data:
        icon_url = data['icon_url']
    elif community.icon_id:
        icon_url = community.icon.medium_url()
    else:
        icon_url = None
    if 'banner_url' in data:
        banner_url = data['banner_url']
    elif community.image_id:
        banner_url = community.image.medium_url()
    else:
        banner_url = None
    nsfw = data['nsfw'] if 'nsfw' in data else community.nsfw
    restricted_to_mods = data['restricted_to_mods'] if 'restricted_to_mods' in data else community.restricted_to_mods
    local_only = data['local_only'] if 'local_only' in data else community.local_only
    if 'discussion_languages' in data:
        discussion_languages = data['discussion_languages']
    else:
        discussion_languages = []
        for cm in community.languages:
            discussion_languages.append(cm.id)

    input = {'community_id': community_id, 'title': title, 'description': description, 'rules': rules,
             'icon_url': icon_url, 'banner_url': banner_url,
             'nsfw': nsfw, 'restricted_to_mods': restricted_to_mods, 'local_only': local_only,
             'discussion_languages': discussion_languages}

    user_id = edit_community(input, community, SRC_API, auth)
    community_json = community_view(community=community, variant=4, user_id=user_id)
    return community_json


def put_community_subscribe(auth, data):
    required(['community_id', 'subscribe'], data)
    integer_expected(['community_id'], data)
    boolean_expected(['subscribe'], data)

    community_id = data['community_id']
    subscribe = data['subscribe']

    user_id = subscribe_community(community_id, subscribe, SRC_API, auth)
    community_json = community_view(community=community_id, variant=4, user_id=user_id)
    return community_json


def post_community_delete(auth, data):
    required(['community_id', 'deleted'], data)
    integer_expected(['community_id'], data)
    boolean_expected(['deleted'], data)

    community_id = data['community_id']
    deleted = data['deleted']

    if deleted:
        user_id = delete_community(community_id, SRC_API, auth)
    else:
        user_id = restore_community(community_id, SRC_API, auth)
    community_json = community_view(community=community_id, variant=4, user_id=user_id)
    return community_json


def get_community_moderate_bans(auth, data):
    required(['community_id'], data)

    # get the community_id from the data
    community_id = int(data['community_id'])
    community = Community.query.filter_by(id=community_id).one()

    # get the user_id from the auth
    user = authorise_api_user(auth, return_type='model')

    # get the page for pagination from the data.page
    page = int(data['page']) if data and 'page' in data else 1
    limit = int(data['limit']) if data and 'limit' in data else 10

    # validate that the user is a mod or owner of the community, or an instance admin
    if not (community.is_owner(user) or community.is_moderator(user) or user.is_admin_or_staff()):
        raise Exception('incorrect_login')
    if not community.is_local():
        raise Exception('Community not local to this instance.')

    # get the list of all banned users and their stats
    community_bans = CommunityBan.query.filter_by(community_id=community_id).paginate(page=page, per_page=limit, error_out=False)

    # setup the items for the json
    items = []
    for cb in community_bans:
        ban_json = {}
        ban_json['reason'] = cb.reason
        ban_json['expired_at'] = cb.ban_until.isoformat(timespec="microseconds") + "Z" if cb.ban_until else None
        ban_json['community'] = community_view(community, variant=1)
        ban_json['banned_user'] = user_view(user=cb.user_id, variant=1)
        ban_json['banned_by'] = user_view(user=cb.banned_by, variant=1)
        if not cb.ban_until:
            ban_json['expired'] = False
        else:
            ban_json['expired'] = True if cb.ban_until < datetime.now() else False
        items.append(ban_json)

    # return that info as json
    res = {}
    res['items'] = items
    res['next_page'] = str(community_bans.next_num) if community_bans.next_num is not None else None

    return res


def put_community_moderate_unban(auth, data):
    required(['community_id', 'user_id'], data)
    integer_expected(['community_id'], data)
    integer_expected(['user_id'], data)

    # get the user to unban
    user_id = data['user_id']
    blocked = User.query.get(user_id)

    # get the community from the data
    community = Community.query.filter_by(id=data['community_id']).one()

    # get the user from the auth and make sure they are allowed to conduct this action
    user = authorise_api_user(auth, return_type='model')

    # validate that the user is a mod or owner of the community, or an instance admin
    if not (community.is_owner(user) or community.is_moderator(user) or user.is_admin_or_staff()):
        raise Exception('incorrect_login')
    if not community.is_local():
        raise Exception('Community not local to this instance.')

    # find the ban record
    cb = CommunityBan.query.filter_by(community_id=community.id, user_id=blocked.id).first()

    if not cb:
        raise Exception("Specified ban does not exist")

    # build the response before deleting the record in the db
    res = {}
    res['reason'] = cb.reason
    res['expired_at'] = utcnow().isoformat(timespec="microseconds") + "Z"
    res['community'] = community_view(community, variant=1)
    res['banned_user'] = user_view(user=cb.user_id, variant=1)
    res['banned_by'] = user_view(user=cb.banned_by, variant=1)
    res['expired'] = True

    # unban in the db
    db.session.query(CommunityBan).filter(CommunityBan.community_id == community.id,
                                          CommunityBan.user_id == blocked.id).delete()
    community_membership_record = CommunityMember.query.filter_by(community_id=community.id, user_id=blocked.id).first()
    if community_membership_record:
        community_membership_record.is_banned = False
    db.session.commit()

    # federate the unban
    task_selector('unban_from_community', user_id=user_id, mod_id=user.id, community_id=community.id,
                  expiry=res['expired_at'], reason=res['reason'])

    # notify the unbanned user if they are local to this instance
    if blocked.is_local():
        # Notify unbanned person
        targets_data = {'gen': '0', 'community_id': community.id}
        notify = Notification(title=shorten_string('You have been unbanned from ' + community.display_name()),
                              url=f'/chat/ban_from_mod/{blocked.id}/{community.id}', user_id=blocked.id,
                              author_id=user.id, notif_type=NOTIF_UNBAN,
                              subtype='user_unbanned_from_community',
                              targets=targets_data)
        db.session.add(notify)
        if not current_app.debug:  # user.unread_notifications += 1 hangs app if 'user' is the same person
            blocked.unread_notifications += 1  # who pressed 'Re-submit this activity'.

        db.session.commit()

        cache.delete_memoized(communities_banned_from, blocked.id)
        cache.delete_memoized(joined_communities, blocked.id)
        cache.delete_memoized(moderating_communities, blocked.id)

        # return the res{} json
    return res


def post_community_moderate_ban(auth, data):
    required(['community_id', 'user_id', 'reason'], data)
    integer_expected(['community_id'], data)
    integer_expected(['user_id'], data)
    string_expected(['reason'], data)
    string_expected(['expires'], data)
    boolean_expected(['permanent'], data)

    # get the user to ban
    user_id = data['user_id']
    blocked = User.query.get(user_id)

    # get the community from the data
    community = Community.query.filter_by(id=data['community_id']).one()

    # get the user from the auth and make sure they are allowed to conduct this action
    blocker = authorise_api_user(auth, return_type='model')

    # validate that the user is a mod or owner of the community, or an instance admin
    if not (community.is_owner(blocker) or community.is_moderator(blocker) or blocker.is_admin_or_staff()):
        raise Exception('incorrect_login')
    if not community.is_local():
        raise Exception('Community not local to this instance.')

    # check if ban is permanent or get the ban_until time if it exists, fall back to default of one year
    if data.get('permanent', False):
        ban_until = None
    elif isinstance(data.get('expires', None), str):
        ban_until = datetime.strptime(data['expires'], '%Y-%m-%dT%H:%M:%S.%fZ')
    else:
        ban_until = datetime.now() + relativedelta(years=1)

    # create the community ban
    cb = CommunityBan.query.filter(CommunityBan.user_id == blocked.id, CommunityBan.community_id == community.id).first()
    if not cb:
        cb = CommunityBan(user_id=blocked.id, community_id=community.id, banned_by=blocker.id,
                          reason=data['reason'], ban_until=ban_until)
        db.session.add(cb)
    elif cb.ban_until != ban_until:
        cb.ban_until = ban_until
    community_membership_record = CommunityMember.query.filter_by(community_id=community.id, user_id=blocked.id).first()
    if community_membership_record:
        community_membership_record.is_banned = True
    db.session.commit()

    # federate the ban
    task_selector('ban_from_community', user_id=user_id, mod_id=blocker.id, community_id=community.id, expiry=ban_until,
                  reason=data['reason'])

    if blocked.is_local():
        db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.community_id == community.id,
                                                      CommunityJoinRequest.user_id == blocked.id).delete()

        # Notify banned person
        targets_data = {'gen': '0', 'community_id': community.id}
        notify = Notification(title=shorten_string('You have been banned from ' + community.title),
                              url=f'/chat/ban_from_mod/{blocked.id}/{community.id}', user_id=blocked.id,
                              author_id=blocker.id, notif_type=NOTIF_BAN, subtype='user_banned_from_community',
                              targets=targets_data)
        db.session.add(notify)
        if not current_app.debug:  # user.unread_notifications += 1 hangs app if 'user' is the same person
            blocked.unread_notifications += 1  # who pressed 'Re-submit this activity'.

        # Remove their notification subscription,  if any
        db.session.query(NotificationSubscription).filter(NotificationSubscription.entity_id == community.id,
                                                          NotificationSubscription.user_id == blocked.id,
                                                          NotificationSubscription.type == NOTIF_COMMUNITY).delete()
        db.session.commit()

    cache.delete_memoized(communities_banned_from, blocked.id)
    cache.delete_memoized(joined_communities, blocked.id)
    cache.delete_memoized(moderating_communities, blocked.id)

    # build the response
    res = {}
    res['reason'] = cb.reason
    res['expired_at'] = cb.ban_until.isoformat(timespec="microseconds") + "Z" if cb.ban_until else None
    res['community'] = community_view(community, variant=1)
    res['banned_user'] = user_view(user=cb.user_id, variant=1)
    res['banned_by'] = user_view(user=cb.banned_by, variant=1)
    res['expired'] = False

    # return the res{} json
    return res


def post_community_moderate_post_nsfw(auth, data):
    required(['post_id', 'nsfw_status'], data)
    integer_expected(['post_id'], data)
    boolean_expected(['nsfw_status'], data)

    # get the user from the auth and make sure they are allowed to conduct this action
    mod_user = authorise_api_user(auth, return_type='model')

    # get the post from the data
    post_id = int(data['post_id'])
    post = Post.query.get(post_id)

    # get the community from the post
    community = Community.query.get(post.community_id)

    # validate that the user is a mod or owner of the community, or an instance admin
    if not (community.is_owner(mod_user) or community.is_moderator(mod_user) or mod_user.is_admin_or_staff()):
        raise Exception('incorrect_login')
    if not community.is_local():
        raise Exception('Community not local to this instance.')

    # set the post.nsfw to nsfw_status
    post.nsfw = data['nsfw_status']
    db.session.commit()

    # build the post view response
    res = post_view(post=post, variant=2, stub=True, user_id=mod_user.id)

    return res


def post_community_mod(auth, data):
    required(['community_id', 'person_id', 'added'], data)
    integer_expected(['community_id', 'person_id'], data)
    boolean_expected(['added'], data)

    community_id = data['community_id']
    person_id = data['person_id']
    added = data['added']

    if added:
        add_mod_to_community(community_id, person_id, SRC_API, auth)
    else:
        remove_mod_from_community(community_id, person_id, SRC_API, auth)
    cache.delete_memoized(cached_modlist_for_community)
    community_json = {
        'moderators': cached_modlist_for_community(community_id)
    }
    return community_json
