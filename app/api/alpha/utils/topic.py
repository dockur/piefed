from datetime import timedelta

from flask import current_app, g
from sqlalchemy import desc, text

from app import db
from app.api.alpha.views import feed_view, topic_view
from app.constants import *
from app.models import User
from app.shared.topic import subscribe_topic
from app.utils import authorise_api_user, blocked_communities, blocked_or_banned_instances, filtered_out_communities, \
    communities_banned_from, moderating_communities_ids, joined_or_modding_communities, feed_tree_public, feed_tree, \
    subscribed_feeds, topic_tree


def get_topic_list(auth, data, user_id=None) -> dict:

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

    topics = topic_tree()

    if user_id:
        banned_from = communities_banned_from(user_id)
        communities_moderating = moderating_communities_ids(user_id)
        communities_joined = joined_or_modding_communities(user_id)
    else:
        user_id = None
        banned_from = []
        communities_moderating = []
        communities_joined = []

    def process_nested_topics(feed_tree_dict):
        """Process nested feed tree while preserving nested structure"""
        processed_feeds = []

        for item in feed_tree_dict:
            view = topic_view(topic=item['topic'], variant=1,
                             communities_moderating=communities_moderating,
                             banned_from=banned_from, communities_joined=communities_joined,
                             blocked_community_ids=blocked_community_ids,
                             blocked_instance_ids=blocked_instance_ids,
                             include_communities=include_communities, user_id=user_id)

            # Process nested children
            if item['children']:
                view['children'] = process_nested_topics(item['children'])
            else:
                view['children'] = []

            processed_feeds.append(view)

        return processed_feeds

    feedlist = process_nested_topics(topics)

    list_json = {
        "topics": feedlist
    }

    return list_json


def put_topic_subscribe(auth, data):
    topic_id = data["topic_id"]
    subscribe = data["subscribe"]

    # Auth is done in subscribe_topic
    user_id = subscribe_topic(topic_id, subscribe, SRC_API, auth)

    banned_from = communities_banned_from(user_id)
    communities_moderating = moderating_communities_ids(user_id)
    communities_joined = joined_or_modding_communities(user_id)
    blocked_community_ids = blocked_communities(user_id)
    blocked_instance_ids = blocked_or_banned_instances(user_id)

    return topic_view(
        topic=topic_id,
        variant=1,
        banned_from=banned_from,
        communities_joined=communities_joined,
        communities_moderating=communities_moderating,
        blocked_community_ids=blocked_community_ids,
        blocked_instance_ids=blocked_instance_ids,
        include_communities=True,
        user_id=user_id,
    )
