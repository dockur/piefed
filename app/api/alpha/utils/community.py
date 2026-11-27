from datetime import datetime

from dateutil.relativedelta import relativedelta
from flask import current_app
from sqlalchemy import desc, or_, text, asc
from sqlalchemy.orm.exc import NoResultFound

from app import db, cache
from app.api.alpha.views import community_view, user_view, post_view, cached_modlist_for_community, flair_view
from app.community.util import search_for_community
from app.constants import *
from app.models import Community, CommunityMember, User, CommunityBan, Notification, CommunityJoinRequest, \
    NotificationSubscription, Post, CommunityFlair, Feed, utcnow
from app.shared.community import join_community, leave_community, block_community, unblock_community, make_community, \
    edit_community, subscribe_community, delete_community, restore_community, add_mod_to_community, \
    remove_mod_from_community
from app.shared.feed import leave_feed
from app.shared.tasks import task_selector
from app.utils import authorise_api_user, communities_banned_from_all_users, moderating_communities_ids, \
    blocked_or_banned_instances
from app.utils import communities_banned_from, blocked_instances, blocked_communities, shorten_string, \
    joined_communities, moderating_communities, expand_hex_color, community_membership, subscribed_feeds, \
    feed_membership


def get_community_list(auth, data):
    type_ = data['type_'] if 'type_' in data else "All"
    sort = data['sort'] if 'sort' in data else "Hot"
    page = int(data['page']) if 'page' in data else 1
    limit = int(data['limit']) if 'limit' in data else 10
    show_nsfw = data['show_nsfw'] if 'show_nsfw' in data else False
    show_nsfl = show_nsfw
    show_genai = data['show_genai'] if 'show_genai' in data else True

    user = authorise_api_user(auth, return_type='model') if auth else None
    user_id = user.id if user else None

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    query = data['q'] if 'q' in data else ''
    if user_id and '@' in query and '.' in query and query.startswith('!'):
        search_for_community(query)
        query = query[1:]

    if user_id and type_ == 'Subscribed':
        communities = Community.query.filter_by(banned=False).join(CommunityMember).filter(
            CommunityMember.user_id == user_id)
    elif type_ == 'Local':
        communities = Community.query.filter_by(ap_id=None, banned=False)
    elif type_ == 'ModeratorView' or type_ == 'Moderating':
        communities = Community.query.filter(Community.id.in_(moderating_communities_ids(user_id)))
    else:
        communities = Community.query.filter_by(banned=False)

    # filter private communities: show only to members
    if user_id:
        # for authenticated users, show non-private communities OR private communities where they are members
        member_check = db.session.query(CommunityMember.community_id).filter(
            CommunityMember.user_id == user_id,
            CommunityMember.is_banned == False
        )
        communities = communities.filter(
            or_(
                Community.private == False,
                Community.id.in_(member_check)
            )
        )
    else:
        # for anonymous users, only show non-private communities
        communities = communities.filter(Community.private == False)

    if user_id:
        banned_from = communities_banned_from(user_id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))
        blocked_instance_ids = blocked_or_banned_instances(user_id)
        if blocked_instance_ids:
            communities = communities.filter(Community.instance_id.not_in(blocked_instance_ids))
        blocked_community_ids = blocked_communities(user_id)
        if blocked_community_ids:
            communities = communities.filter(Community.id.not_in(blocked_community_ids))
        if user.hide_nsfw and not show_nsfw:
            communities = communities.filter(Community.nsfw == False)
        if user.hide_nsfl and not show_nsfl:
            communities = communities.filter(Community.nsfl == False)
        if user.hide_gen_ai and not show_genai:
            communities = communities.filter(Community.ai_generated == False)
    else:
        if not show_nsfw:
            communities = communities.filter_by(nsfw=False)
        if not show_nsfl:
            communities = communities.filter_by(nsfl=False)

    if query:
        communities = communities.filter(or_(Community.title.ilike(f"%{query}%"), Community.ap_id.ilike(f"%{query}%")))

    if sort == 'New':
        communities = communities.order_by(desc(Community.created_at))
    elif sort.startswith("Top") and sort != "TopSubscribers":
        communities = communities.order_by(desc(Community.post_count))
    elif sort == 'Old':
        communities = communities.order_by(asc(Community.created_at))
    elif sort == "NewFederated":
        communities = communities.order_by(desc(Community.first_federated_at))
    elif sort == "OldFederated":
        communities = communities.order_by(asc(Community.first_federated_at))
    elif sort == "TopSubscribers":
        communities = communities.order_by(desc(Community.total_subscriptions_count))
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
    if 'id' not in data and 'name' not in data:
        raise Exception('id or name required')
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
    community_id = data['community_id']
    follow = data['follow']

    user_id = join_community(community_id, SRC_API, auth) if follow else leave_community(community_id, SRC_API, auth)
    community_json = community_view(community=community_id, variant=4, stub=False, user_id=user_id)
    return community_json

def post_community_leave_all(auth):
    user = authorise_api_user(auth, return_type='model') if auth else None

    if not user:
        raise Exception('incorrect login')

    all_communities = Community.query.filter_by(banned=False)
    user_joined_communities = joined_communities(user_id=user.id)

    joined_ids = []
    for jc in user_joined_communities:
        joined_ids.append(jc.id)

    # filter down to just the joined communities
    communities = all_communities.filter(Community.id.in_(joined_ids))

    user_id = None
    for community in communities.all():
        subscription = community_membership(user, community)
        if subscription is not False and subscription < SUBSCRIPTION_MODERATOR:
            # send leave requests to celery - also handles db commits and cache busting, ignore returned value
            user_id = leave_community(community_id=community.id, src=SRC_API, auth=auth, bulk_leave=True)

    joined_feed_ids = subscribed_feeds(user.id)

    if joined_feed_ids:
        for feed_id in joined_feed_ids:
            feed = Feed.query.get(feed_id)
            subscription = feed_membership(user, feed)
            if subscription != SUBSCRIPTION_OWNER:
                # send leave requests to celery - also handles db commits and cache busting, ignore returned value
                user_id = leave_feed(feed=feed, src=SRC_API, auth=auth, bulk_leave=True)

    return user_view(user=user, variant=6, user_id=user_id)


def post_community_block(auth, data):
    community_id = data['community_id']
    block = data['block']

    user_id = block_community(community_id, SRC_API, auth) if block else unblock_community(community_id, SRC_API, auth)
    community_json = community_view(community=community_id, variant=5, user_id=user_id)
    return community_json


def post_community(auth, data):
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
    question_answer = data['question_answer'] if 'question_answer' in data else False
    ai_generated = data['ai_generated'] if 'ai_generated' in data else False
    downvote_accept_mode = data['downvote_accept_mode'] if 'downvote_accept_mode' in data else 0

    input = {'name': name, 'title': title, 'description': description, 'rules': rules,
             'icon_url': icon_url, 'banner_url': banner_url,
             'nsfw': nsfw, 'restricted_to_mods': restricted_to_mods, 'local_only': local_only,
             'discussion_languages': discussion_languages, 'question_answer': question_answer,
             'ai_generated': ai_generated, 'downvote_accept_mode': downvote_accept_mode}

    user_id, community_id = make_community(input, SRC_API, auth)
    community_json = community_view(community=community_id, variant=4, user_id=user_id)
    return community_json


def put_community(auth, data):
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
    question_answer = data['question_answer'] if 'question_answer' in data else community.question_answer
    restricted_to_mods = data['restricted_to_mods'] if 'restricted_to_mods' in data else community.restricted_to_mods
    local_only = data['local_only'] if 'local_only' in data else community.local_only
    ai_generated = data['ai_generated'] if 'ai_generated' in data else community.ai_generated
    downvote_accept_mode = data['downvote_accept_mode'] if 'downvote_accept_mode' in data else community.downvote_accept_mode
    if 'discussion_languages' in data:
        discussion_languages = data['discussion_languages']
    else:
        discussion_languages = []
        for cm in community.languages:
            discussion_languages.append(cm.id)

    input = {'community_id': community_id, 'title': title, 'description': description, 'rules': rules,
             'icon_url': icon_url, 'banner_url': banner_url,
             'nsfw': nsfw, 'restricted_to_mods': restricted_to_mods, 'local_only': local_only,
             'discussion_languages': discussion_languages, 'question_answer': question_answer,
             'ai_generated': ai_generated, 'downvote_accept_mode': downvote_accept_mode}

    user_id = edit_community(input, community, SRC_API, auth)
    community_json = community_view(community=community, variant=4, user_id=user_id)
    return community_json


def put_community_subscribe(auth, data):
    community_id = data['community_id']
    subscribe = data['subscribe']

    user_id = subscribe_community(community_id, subscribe, SRC_API, auth)
    community_json = community_view(community=community_id, variant=4, user_id=user_id)
    return community_json


def post_community_delete(auth, data):
    community_id = data['community_id']
    deleted = data['deleted']

    if deleted:
        user_id = delete_community(community_id, SRC_API, auth)
    else:
        user_id = restore_community(community_id, SRC_API, auth)
    community_json = community_view(community=community_id, variant=4, user_id=user_id)
    return community_json


def get_community_moderate_bans(auth, data):
    # get the community_id from the data
    community_id = int(data['community_id'])
    community = Community.query.filter_by(id=community_id).one()

    # get the user_id from the auth
    user = authorise_api_user(auth, return_type='model')

    # get the page for pagination from the data.page
    page = int(data['page']) if 'page' in data else 1
    limit = int(data['limit']) if 'limit' in data else 10

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

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
        ban_json['community'] = community_view(community, variant=1)
        ban_json['banned_user'] = user_view(user=cb.user_id, variant=1)
        ban_json['banned_by'] = user_view(user=cb.banned_by, variant=1)
        if not cb.ban_until:
            # Permanent ban
            ban_json['expired'] = False
            ban_json['expires_at'] = None
            ban_json['expired_at'] = None
        elif cb.ban_until < datetime.now():
            ban_json['expired'] = True
            ban_json['expired_at'] = cb.ban_until.isoformat(timespec="microseconds") + "Z"
        else:
            ban_json['expired'] = False
            ban_json['expires_at'] = cb.ban_until.isoformat(timespec="microseconds") + "Z"

        items.append(ban_json)

    # return that info as json
    res = {}
    res['items'] = items
    res['next_page'] = str(community_bans.next_num) if community_bans.next_num is not None else None

    return res


def put_community_moderate_unban(auth, data):
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
        cache.delete_memoized(communities_banned_from_all_users)
        cache.delete_memoized(joined_communities, blocked.id)
        cache.delete_memoized(moderating_communities, blocked.id)

        # return the res{} json
    return res


def post_community_moderate_ban(auth, data):
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
    elif isinstance(data.get('expires_at', None), str):
        ban_until = datetime.strptime(data['expires_at'], '%Y-%m-%dT%H:%M:%S.%fZ')
        if ban_until < datetime.now():
            raise Exception("expires_at must be a time in the future. - "
                            f"Current time: {utcnow().isoformat(timespec='microseconds') +  'Z'} - "
                            f"Time provided: {ban_until.isoformat(timespec='microseconds') + 'Z'}")
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
    cache.delete_memoized(communities_banned_from_all_users)
    cache.delete_memoized(joined_communities, blocked.id)
    cache.delete_memoized(moderating_communities, blocked.id)

    # build the response
    res = {}
    res['reason'] = cb.reason
    res['expires_at'] = cb.ban_until.isoformat(timespec="microseconds") + "Z" if cb.ban_until else None
    res['community'] = community_view(community, variant=1)
    res['banned_user'] = user_view(user=cb.user_id, variant=1)
    res['banned_by'] = user_view(user=cb.banned_by, variant=1)
    res['expired'] = False

    # return the res{} json
    return res


def post_community_moderate_post_nsfw(auth, data):
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


def post_community_flair_create(auth, data):
    user = authorise_api_user(auth, return_type='model')
    community = Community.query.get(data['community_id'])

    if not (community.is_owner(user) or community.is_moderator(user) or user.is_admin_or_staff()):
        raise Exception('insufficient permissions')
    
    if 'text_color' not in data:
        data['text_color'] = "#000000"
    elif len(data['text_color']) == 4:
        # Go ahead and expand this out to the full notation for consistency
        data['text_color'] = expand_hex_color(data['text_color'])

    if 'background_color' not in data:
        data['background_color'] = "#DEDDDA"
    elif len(data['background_color']) == 4:
        # Go ahead and expand this out to the full notation for consistency
        data['background_color'] = expand_hex_color(data['background_color'])

    if 'blur_images' not in data:
        data['blur_images'] = False
    
    try:
        CommunityFlair.query.filter_by(community_id=community.id, flair=data['flair_title'],
                                       text_color=data['text_color'], background_color=data['background_color'],
                                       blur_images=data['blur_images']).one()
        raise Exception("Flair already exists")
    except NoResultFound:
        # Flair is new, create it
        new_flair = CommunityFlair(community_id = community.id)
        db.session.add(new_flair)
        new_flair.flair = data['flair_title'].strip()
        new_flair.text_color = data['text_color']
        new_flair.background_color = data['background_color']
        new_flair.blur_images = data['blur_images']

        # Need a commit here or else the flair id is not defined for the ap_id
        db.session.commit()
        
        new_flair.ap_id = new_flair.get_ap_id()
        db.session.commit()

        task_selector('edit_community', user_id=user.id, community_id=community.id)
    
    return flair_view(new_flair)


def put_community_flair_edit(auth, data):
    user = authorise_api_user(auth, return_type='model')
    flair = CommunityFlair.query.get(data['flair_id'])

    if not flair:
        raise Exception(f"No matching flair with id={data['flair_id']} found.")
    
    community = Community.query.get(flair.community_id)

    if not (community.is_owner(user) or community.is_moderator(user) or user.is_admin_or_staff()):
        raise Exception('insufficient permissions')
    
    if 'flair_title' in data:
        flair.flair = data['flair_title']
    
    if 'text_color' in data:
        if len(data['text_color']) == 4:
            data['text_color'] = expand_hex_color(data['text_color'])
        
        flair.text_color = data['text_color']
    
    if 'background_color' in data:
        if len(data['background_color']) == 4:
            data['background_color'] = expand_hex_color(data['background_color'])
        
        flair.background_color = data['background_color']
    
    if 'blur_images' in data:
        flair.blur_images = data['blur_images']
    
    if not flair.ap_id:
        flair.ap_id = flair.get_ap_id()
    
    db.session.commit()

    task_selector('edit_community', user_id=user.id, community_id=community.id)

    return flair_view(flair)


def post_community_flair_delete(auth, data):
    user = authorise_api_user(auth, return_type='model')
    flair = CommunityFlair.query.get(data['flair_id'])

    if not flair:
        raise Exception(f"No matching flair with id={data['flair_id']} found.")
    
    community = Community.query.get(flair.community_id)

    if not (community.is_owner(user) or community.is_moderator(user) or user.is_admin_or_staff()):
        raise Exception('insufficient permissions')
    
    db.session.execute(text('DELETE FROM "post_flair" WHERE flair_id = :flair_id'), {'flair_id': flair.id})
    db.session.query(CommunityFlair).filter(CommunityFlair.id == flair.id).delete()
    db.session.commit()

    task_selector('edit_community', user_id=user.id, community_id=community.id)

    # Return Community View that includes updated flair list
    community_json = community_view(community=community, variant=3, stub=False, user_id=user.id)
    return community_json
