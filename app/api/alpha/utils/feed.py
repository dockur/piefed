from flask import current_app, g

from app import db
from app.api.alpha.views import feed_view
from app.constants import *
from app.models import User, Feed, FeedItem, Community
from app.shared.feed import leave_feed, join_feed, make_feed, edit_feed, delete_feed, subscribe_feed
from app.utils import authorise_api_user, blocked_communities, blocked_or_banned_instances, filtered_out_communities, \
    communities_banned_from, moderating_communities_ids, joined_or_modding_communities, feed_tree_public, feed_tree, \
    subscribed_feeds


def get_feed_list(auth, data, user_id=None) -> dict:

    mine_only = data['mine_only'] if 'mine_only' in data else False
    include_communities = data['include_communities'] if 'include_communities' in data else True

    if auth:
        user_id = authorise_api_user(auth)

    # get the user to check if the user has hide_read posts set later down the function
    if user_id:
        user = User.query.get(user_id)
        g.user = user

        blocked_community_ids = blocked_communities(user_id)
        blocked_instance_ids = blocked_or_banned_instances(user_id)
    else:
        blocked_community_ids = []
        blocked_instance_ids = []

    if user_id and mine_only:
        feeds = feed_tree(user_id)
    else:
        feeds = feed_tree_public()

    if user_id:
        subscribed = subscribed_feeds(user_id)
        banned_from = communities_banned_from(user_id)
        communities_moderating = moderating_communities_ids(user_id)
        communities_joined = joined_or_modding_communities(user_id)
    else:
        subscribed = []
        banned_from = []
        communities_moderating = []
        communities_joined = []

    def process_nested_feeds(feed_tree):
        """Process nested feed tree while preserving nested structure"""
        processed_feeds = []
        
        for item in feed_tree:
            view = feed_view(feed=item['feed'], variant=1, user_id=user_id, subscribed=subscribed,
                             include_communities=include_communities, communities_moderating=communities_moderating,
                             banned_from=banned_from, communities_joined=communities_joined,
                             blocked_community_ids=blocked_community_ids,
                             blocked_instance_ids=blocked_instance_ids)
            
            # Process nested children
            if item['children']:
                view['children'] = process_nested_feeds(item['children'])
            else:
                view['children'] = []
            
            processed_feeds.append(view)
        
        return processed_feeds
    
    feedlist = process_nested_feeds(feeds)

    list_json = {
        "feeds": feedlist
    }

    return list_json


def get_feed(auth, data, user_id=None):
    id = data['id'] if 'id' in data else False
    name = data['name'] if 'name' in data else ''

    if auth:
        user_id = authorise_api_user(auth)

    if user_id:
        user = User.query.get(user_id)
        g.user = user

        blocked_community_ids = blocked_communities(user_id)
        blocked_instance_ids = blocked_or_banned_instances(user_id)

        subscribed = subscribed_feeds(user_id)
        banned_from = communities_banned_from(user_id)
        communities_moderating = moderating_communities_ids(user_id)
        communities_joined = joined_or_modding_communities(user_id)
    else:
        subscribed = []
        banned_from = []
        communities_moderating = []
        communities_joined = []
        blocked_community_ids = []
        blocked_instance_ids = []

    if id:
        feed = Feed.query.get(id)
    elif name:
        parts = name.split('@')
        feed = Feed.query.filter(Feed.name == parts[0], Feed.ap_domain == parts[1]).first()
    else:
        raise Exception('invalid_request')
    if feed:
        if feed.public or feed.user_id == user_id:
            return feed_view(feed, variant=2, user_id=user_id, subscribed=subscribed,
                                     include_communities=True, communities_moderating=communities_moderating,
                                     banned_from=banned_from, communities_joined=communities_joined,
                                     blocked_community_ids=blocked_community_ids,
                                     blocked_instance_ids=blocked_instance_ids)['feed']
        else:
            raise Exception('access_denied')
    else:
        raise Exception('feed_not_found')


def post_feed_follow(auth, data):
    feed_id = data['feed_id']
    follow = data['follow']

    user = authorise_api_user(auth, return_type='model')
    feed = Feed.query.get(feed_id)

    if not feed:
        raise Exception('could not find feed')

    if follow:
        join_feed(feed.link(), user.id, SRC_API)
    else:
        leave_feed(feed, SRC_API, auth)

    return feed


def post_feed(auth, data):
    from slugify import slugify

    user = authorise_api_user(auth, return_type='model')
    public = data['public'] if 'public' in data else True
    url = slugify(data['name'].strip().split('/')[0], separator='_').lower()
    if not public:
        url = url + '/' + user.user_name.lower()
    title = data['title']
    description = data['description'] if 'description' in data else ''
    icon_url = data['icon_url'] if 'icon_url' in data else None
    banner_url = data['banner_url'] if 'banner_url' in data else None
    nsfw = data['nsfw'] if 'nsfw' in data else False
    nsfl = data['nsfl'] if 'nsfl' in data else False
    communities = data['communities'] if 'communities' in data else ''
    is_instance_feed = data['is_instance_feed'] if 'is_instance_feed' in data else False
    show_child_posts = data['show_child_posts'] if 'show_child_posts' in data else False
    parent_feed_id = data['parent_feed_id'] if 'parent_feed_id' in data else None

    input_data = {'url': url, 'title': title, 'public': public, 'description': description,
                  'icon_url': icon_url, 'banner_url': banner_url, 'nsfw': nsfw, 'nsfl': nsfl,
                  'communities': communities, 'is_instance_feed': is_instance_feed,
                  'show_child_posts': show_child_posts, 'parent_feed_id': parent_feed_id}

    make_feed(input_data, SRC_API, auth)

    feed = Feed.query.filter_by(name=url, user_id=user.id).first()
    return get_feed(auth, {'id': feed.id})


def put_feed(auth, data):
    feed_id = data['feed_id']
    feed = Feed.query.get(feed_id)
    if not feed:
        raise Exception('not_found')

    url = data['url'] if 'url' in data else feed.name
    title = data['title'] if 'title' in data else feed.title
    description = data['description'] if 'description' in data else (feed.description or '')
    icon_url = data['icon_url'] if 'icon_url' in data else (feed.icon.source_url if feed.icon_id else None)
    banner_url = data['banner_url'] if 'banner_url' in data else (feed.image.source_url if feed.image_id else None)
    nsfw = data['nsfw'] if 'nsfw' in data else feed.nsfw
    nsfl = data['nsfl'] if 'nsfl' in data else feed.nsfl
    public = data['public'] if 'public' in data else feed.public
    is_instance_feed = data['is_instance_feed'] if 'is_instance_feed' in data else feed.is_instance_feed
    show_child_posts = data['show_child_posts'] if 'show_child_posts' in data else feed.show_posts_in_children
    parent_feed_id = data['parent_feed_id'] if 'parent_feed_id' in data else feed.parent_feed_id
    if 'communities' in data:
        communities = data['communities']
    else:
        feed_items = FeedItem.query.filter_by(feed_id=feed_id).all()
        ap_ids = [c.ap_id if c.ap_id else c.name + '@' + feed.ap_domain
                  for fi in feed_items if (c := Community.query.get(fi.community_id))]
        communities = '\n'.join(ap_ids)

    input_data = {'url': url, 'title': title, 'public': public, 'description': description,
                  'icon_url': icon_url, 'banner_url': banner_url, 'nsfw': nsfw, 'nsfl': nsfl,
                  'communities': communities, 'is_instance_feed': is_instance_feed,
                  'show_child_posts': show_child_posts, 'parent_feed_id': parent_feed_id}

    edit_feed(input_data, feed, SRC_API, auth)
    return get_feed(auth, {'id': feed_id})


def post_feed_delete(auth, data):
    feed_id = data['feed_id']
    deleted = data['deleted']

    user_id = authorise_api_user(auth)
    feed = Feed.query.get(feed_id)
    if not feed or feed.user_id != user_id:
        raise Exception('not_found')

    # Capture view before deletion since delete_feed removes the record
    feed_json = get_feed(auth, {'id': feed_id})

    if deleted:
        delete_feed(feed_id, SRC_API, auth)

    return feed_json


def put_feed_subscribe(auth, data):
    feed_id = data["feed_id"]
    subscribe = data["subscribe"]

    # Auth is done in subscribe_feed and get_feed
    _ = subscribe_feed(feed_id, subscribe, SRC_API, auth)

    return get_feed(auth, {'id': feed_id})