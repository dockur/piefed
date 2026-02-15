from __future__ import annotations

from flask import current_app, g
from sqlalchemy import text, func, or_
from sqlalchemy.orm.exc import NoResultFound

from app import cache, db
from app.activitypub.util import active_month
from app.constants import *
from app.models import ChatMessage, Community, Language, Instance, Post, PostReply, User, \
    AllowedInstances, BannedInstances, utcnow, Site, Feed, FeedItem, Topic, CommunityFlair, \
    UserNote, Poll, Event, PollChoice
from app.post.util import tags_to_string, flair_to_string
from app.utils import blocked_communities, blocked_or_banned_instances, blocked_users, communities_banned_from, get_setting, \
    num_topics, moderating_communities_ids, moderating_communities, joined_communities, \
    moderating_communities_ids_all_users
from app.shared.community import get_comm_flair_list
from app.shared.post import get_post_flair_list


# 'stub' param: set to True to exclude optional fields


def post_view(post: Post | int, variant, stub=False, user_id=None, my_vote=0, communities_moderating=None, banned_from=None,
              bookmarked_posts=None, post_subscriptions=None, communities_joined=None, read_posts=None, content_filters=None) -> dict:
    if isinstance(post, int):
        post = Post.query.get(post)
        if post is None:
            raise NoResultFound

    # Variant 1 - models/post/post.dart
    if variant == 1:
        include = ['id', 'title', 'user_id', 'community_id', 'deleted', 'sticky', 'instance_sticky', 'emoji_reactions']
        v1 = {column.name: getattr(post, column.name) for column in post.__table__.columns if column.name in include}

        v1.update({'published': post.posted_at.isoformat(timespec="microseconds") + 'Z',
                   'ap_id': post.profile_id(),
                   'local': post.is_local(),
                   'language_id': post.language_id if post.language_id else 0,
                   'removed': False,
                   'locked': not post.comments_enabled,
                   'nsfw': post.nsfw if post.nsfw is not None else False,
                   'ai_generated': post.ai_generated if post.ai_generated is not None else False})
        if post.body:
            v1['body'] = post.body
        if post.deleted and (user_id is None or user_id not in g.admin_ids):
            v1['body'] = ''
        if post.edited_at:
            v1['edited_at'] = post.edited_at.isoformat(timespec="microseconds") + 'Z'
        if post.deleted == True:
            if post.deleted_by and post.user_id != post.deleted_by:
                v1['removed'] = True
                v1['deleted'] = False
        v1['post_type'] = POST_TYPE_NAMES.get(post.type, "Discussion")
        if post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO:
            if post.url:
                v1['url'] = post.url
            if post.image_id:
                valid_url = post.image.medium_url()
                if valid_url:
                    v1['thumbnail_url'] = valid_url
                valid_url = post.image.thumbnail_url()
                if valid_url:
                    v1['small_thumbnail_url'] = valid_url
                if post.image.alt_text:
                    v1['alt_text'] = post.image.alt_text
        if post.type == POST_TYPE_IMAGE:
            if post.image_id:
                valid_url = post.image.view_url()
                if valid_url:
                    v1['url'] = valid_url
                valid_url = post.image.medium_url()
                if valid_url:
                    v1['thumbnail_url'] = valid_url
                valid_url = post.image.thumbnail_url()
                if valid_url:
                    v1['small_thumbnail_url'] = valid_url
                if post.image.alt_text:
                    v1['alt_text'] = post.image.alt_text
        if post.cross_posts:
            v1['cross_posts'] = []
            cross_post_data = db.session.execute(text('SELECT p.id, reply_count, c.title FROM "post" as p INNER JOIN "community" as c ON p.community_id = c.id WHERE p.id IN :cross_posts'),
                                                 {'cross_posts': tuple(post.cross_posts)}).all()
            for cross_post in cross_post_data:
                v1['cross_posts'].append({'post_id': cross_post[0], 'reply_count': cross_post[1], 'community_name': cross_post[2]})
        else:
            v1['cross_posts'] = []
        if post.image_id and post.image.width and post.image.height:
            v1['image_details'] = {
                'width': post.image.width,
                'height': post.image.height,
            }

        v1['tags'] = tags_to_string(post) or ''
        v1['flair'] = flair_to_string(post) or ''

        return v1

    # Variant 2 - views/post_view.dart - /post/list api endpoint
    if variant == 2:
        # counts - models/post/post_aggregates.dart
        counts = {'post_id': post.id, 'comments': post.reply_count, 'score': post.up_votes - post.down_votes, 'upvotes': post.up_votes,
                  'downvotes': post.down_votes,
                  'published': post.posted_at.isoformat(timespec="microseconds") + 'Z',
                  'newest_comment_time': post.last_active.isoformat(timespec="microseconds") + 'Z',
                  'cross_posts': len(post.cross_posts) if post.cross_posts else 0}
        if user_id:
            if bookmarked_posts is None:
                bookmarked = db.session.execute(
                    text('SELECT user_id FROM "post_bookmark" WHERE post_id = :post_id and user_id = :user_id'),
                    {'post_id': post.id, 'user_id': user_id}).scalar()
            else:
                bookmarked = post.id in bookmarked_posts

            if post_subscriptions is None:
                post_sub = db.session.execute(text(
                    'SELECT user_id FROM "notification_subscription" WHERE type = :type and entity_id = :entity_id and user_id = :user_id'),
                                              {'type': NOTIF_POST, 'entity_id': post.id, 'user_id': user_id}).scalar()
            else:
                post_sub = post.id in post_subscriptions

            if communities_joined is None:
                followed = db.session.execute(text(
                    'SELECT user_id FROM "community_member" WHERE community_id = :community_id and user_id = :user_id'),
                                              {"community_id": post.community_id, "user_id": user_id}).scalar()
            else:
                followed = post.community_id in communities_joined

            if read_posts is None:
                read_post = db.session.execute(
                    text('SELECT user_id FROM "read_posts" WHERE read_post_id = :post_id and user_id = :user_id'),
                    {'post_id': post.id, 'user_id': user_id}).scalar()
            else:
                read_post = post.id in read_posts
        else:
            bookmarked = post_sub = followed = read_post = False
        if not stub:
            if banned_from is None:
                banned = post.community_id in communities_banned_from(post.user_id)
            else:
                banned = post.community_id in banned_from[post.user_id] if post.user_id in banned_from else []
            if communities_moderating is None:
                moderator = post.community.is_moderator(post.author) or post.community.is_owner(post.author)
            else:
                moderator = post.community_id in communities_moderating[post.user_id] if post.user_id in communities_moderating else []
            admin = post.user_id in g.admin_ids
        else:
            banned = False
            moderator = False
            admin = False
        if my_vote == 0 and user_id is not None:
            post_vote = db.session.execute(
                text('SELECT effect FROM "post_vote" WHERE post_id = :post_id and user_id = :user_id'),
                {'post_id': post.id, 'user_id': user_id}).scalar()
            effect = post_vote if post_vote else 0
        else:
            effect = my_vote

        my_vote = int(effect)
        saved = True if bookmarked else False
        read = True if read_post else False
        activity_alert = True if post_sub else False
        creator_banned_from_community = True if banned else False
        creator_is_moderator = True if moderator else False
        creator_is_admin = True if admin else False
        subscribe_type = 'Subscribed' if followed else 'NotSubscribed'
        can_auth_user_moderate = True if user_id and communities_moderating and user_id in communities_moderating and \
                                         post.community_id in communities_moderating[user_id] else False
        v2 = {'post': post_view(post=post, variant=1, stub=stub), 'counts': counts, 'banned_from_community': False,
              'subscribed': subscribe_type,
              'saved': saved, 'read': read, 'hidden': False, 'unread_comments': post.reply_count, 'my_vote': my_vote,
              'filtered': post.blocked_by_content_filter(content_filters, user_id) == '-1',
              'blurred': post.blurred(g.user if hasattr(g, 'user') else None),
              'activity_alert': activity_alert,
              'creator_banned_from_community': creator_banned_from_community,
              'creator_is_moderator': creator_is_moderator, 'creator_is_admin': creator_is_admin,
              'can_auth_user_moderate': can_auth_user_moderate}
        
        post_flair =[]
        
        flair_list = get_post_flair_list(post)
        for flair in flair_list:
            post_flair.append(flair_view(flair))
        
        v2['flair_list'] = post_flair

        creator = user_view(user=post.author, variant=1, stub=True, flair_community_id=post.community_id, user_id=user_id)
        community = community_view(community=post.community, variant=1, stub=True)
        if user_id:
            if hasattr(g, 'user'):
                user = g.user
            else:
                user = User.query.get(user_id)
            can_auth_user_moderate = post.community.is_moderator(user)
            v2.update({'can_auth_user_moderate': can_auth_user_moderate})

        v2.update({'creator': creator, 'community': community})

        # Add event and poll data when not stub
        if not stub:
            # Event data
            if post.type == POST_TYPE_EVENT:
                event = Event.query.filter_by(post_id=post.id).first()
                if event:
                    event_data = {
                        'start': event.start.isoformat(timespec="microseconds") + 'Z',
                        'timezone': event.timezone,
                        'max_attendees': event.max_attendees,
                        'participant_count': event.participant_count,
                        'full': event.full,
                        'join_mode': event.join_mode,
                        'anonymous_participation': event.anonymous_participation,
                        'online': event.online
                    }
                    if event.end:
                        event_data['end'] = event.end.isoformat(timespec="microseconds") + 'Z'
                    if event.online_link:
                        event_data['online_link'] = event.online_link
                    if event.external_participation_url:
                        event_data['external_participation_url'] = event.external_participation_url
                    if event.buy_tickets_link:
                        event_data['buy_tickets_link'] = event.buy_tickets_link
                    if event.event_fee_currency:
                        event_data['event_fee_currency'] = event.event_fee_currency
                    if event.event_fee_amount:
                        event_data['event_fee_amount'] = event.event_fee_amount
                    if event.location:
                        event_data['location'] = event.location

                    v2['post']['event'] = event_data

            # Poll data
            if post.type == POST_TYPE_POLL:
                poll = Poll.query.filter_by(post_id=post.id).first()
                if poll:
                    poll_data = {
                        'mode': poll.mode,
                        'local_only': poll.local_only,
                        'choices': []
                    }
                    if poll.end_poll:
                        poll_data['end_poll'] = poll.end_poll.isoformat(timespec="microseconds") + 'Z'
                    if poll.latest_vote:
                        poll_data['latest_vote'] = poll.latest_vote.isoformat(timespec="microseconds") + 'Z'

                    # Get poll choices
                    choices = PollChoice.query.filter_by(post_id=post.id).order_by(PollChoice.sort_order).all()
                    for choice in choices:
                        choice_data = {
                            'id': choice.id,
                            'choice_text': choice.choice_text,
                            'sort_order': choice.sort_order,
                            'num_votes': choice.num_votes
                        }
                        poll_data['choices'].append(choice_data)
                    
                    if user_id:
                        my_poll_votes = poll.user_votes(user_id)
                        if my_poll_votes:
                            poll_data['my_votes'] = [vote.choice_id for vote in my_poll_votes]

                    v2['post']['poll'] = poll_data

        return v2

    # Variant 3 - models/post/get_post_response.dart - /post api endpoint
    if variant == 3:
        modlist = cached_modlist_for_community(post.community_id)
        if communities_moderating is None:
            communities_moderating = moderating_communities_ids_all_users() if user_id else []

        xplist = []
        if post.cross_posts:
            for xp_id in post.cross_posts:
                try:
                    entry = post_view(post=xp_id, variant=2, stub=True, communities_moderating=communities_moderating)
                    xplist.append(entry)
                except NoResultFound:
                    continue

        v3 = {'post_view': post_view(post=post, variant=2, user_id=user_id, communities_moderating=communities_moderating),
              'community_view': community_view(community=post.community, variant=2, user_id=user_id),
              'moderators': modlist,
              'cross_posts': xplist}

        return v3

    # Variant 4 - models/post/post_response.dart - api endpoint for /post/like and post/save
    if variant == 4:
        if communities_moderating is None:
            communities_moderating = moderating_communities_ids_all_users() if user_id else []
        v4 = {'post_view': post_view(post=post, variant=2, user_id=user_id, communities_moderating=communities_moderating)}

        return v4

    # Variant 5 - from resolve_object
    if variant == 5:
        if communities_moderating is None:
            communities_moderating = moderating_communities_ids_all_users() if user_id else []
        v5 = {'post': post_view(post=post, variant=2, user_id=user_id, communities_moderating=communities_moderating)}

        return v5


# 'user' param can be anyone (including the logged in user), 'user_id' param belongs to the user making the request
def user_view(user: User | int, variant, stub=False, user_id=None, flair_community_id=None) -> dict:
    if isinstance(user, int):
        user = User.query.get(user)

    # Variant 1 - models/person/person.dart
    if variant == 1:
        include = ['id', 'user_name', 'title', 'banned', 'deleted', 'bot']
        v1 = {column.name: getattr(user, column.name) for column in user.__table__.columns if column.name in include}
        v1['bot'] = v1['bot'] or user.bot_override or False
        v1.update({'published': user.created.isoformat(timespec="microseconds") + 'Z',
                   'actor_id': user.public_url(),
                   'local': user.is_local(),
                   'instance_id': user.instance_id if user.instance_id else 1})
        if user.about and not stub:
            v1['about'] = user.about
        if user.about_html and not stub:
            v1['about_html'] = user.about_html
        if user.avatar_id:
            valid_url = user.avatar.medium_url()
            if valid_url:
                v1['avatar'] = valid_url
        if user.cover_id and not stub:
            valid_url = user.cover.medium_url()
            if valid_url:
                v1['banner'] = valid_url
        if not v1['title']:
            v1['title'] = v1['user_name']
        if flair_community_id:
            flair = user.community_flair(flair_community_id)
            if flair:
                v1['flair'] = flair
        if user.extra_fields:
            v1['extra_fields'] = []
            for field in user.extra_fields.limit(4):
                user_field = {}
                user_field['id'] = field.id
                user_field['label'] = field.label
                user_field['text'] = field.text
                v1['extra_fields'].append(user_field)
        if user_id:
            usernote = UserNote.query.filter(UserNote.target_id == user.id, UserNote.user_id == user_id).first()
            if usernote:
                v1['note'] = usernote.body

        return v1

    # Variant 2 - views/person_view.dart
    if variant == 2:
        counts = {'person_id': user.id, 'post_count': user.post_count, 'comment_count': user.post_reply_count}
        user_sub = False
        if user_id and user_id != user.id:
            user_sub = db.session.execute(text(
                'SELECT user_id FROM "notification_subscription" WHERE type = :type and entity_id = :entity_id and user_id = :user_id'),
                                          {'type': NOTIF_USER, 'entity_id': user.id, 'user_id': user_id}).scalar()
        activity_alert = True if user_sub else False
        v2 = {'person': user_view(user=user, variant=1, flair_community_id=flair_community_id, user_id=user_id), 'activity_alert': activity_alert, 'counts': counts,
              'is_admin': user.is_admin()}
        return v2

    # Variant 3 - models/user/get_person_details.dart - /user?person_id api endpoint
    if variant == 3:
        modlist = cached_modlist_for_user(user)

        v3 = {'person_view': user_view(user=user, variant=2, user_id=user_id),
              'moderates': modlist,
              'posts': [],
              'comments': []}
        return v3

    # Variant 4 - models/user/block_person_response.dart - /user/block api endpoint
    if variant == 4:
        block = db.session.execute(
            text('SELECT blocker_id FROM "user_block" WHERE blocker_id = :blocker_id and blocked_id = :blocked_id'),
            {'blocker_id': user_id, 'blocked_id': user.id}).scalar()
        blocked = True if block else False
        v4 = {'person_view': user_view(user=user, variant=2, user_id=user_id),
              'blocked': blocked}
        return v4

    # Variant 5 - PersonResponse (for user activity_alert subscriptions, to be consistent with the response to community activity_alert subscriptions)
    if variant == 5:
        v5 = {'person_view': user_view(user=user, variant=2, user_id=user_id, flair_community_id=flair_community_id)}
        return v5

    # Variant 6 - User Settings - api/user.dart saveUserSettings
    if variant == 6:
        extra_fields = []
        if user.extra_fields:
            for field in user.extra_fields.limit(4):
                user_field = {}
                user_field['id'] = field.id
                user_field['label'] = field.label
                user_field['text'] = field.text
                extra_fields.append(user_field)
        
        visibility_options = ["Show", "Hide", "Blur", "Transparent"]
        private_message_options = ["None", "Local", "Trusted", "All"]
        ai_visibility_options = ["Show", "Hide", "Label", "Transparent"]

        v6 = {
            "local_user_view": {
                "local_user": {
                    "show_nsfw": not user.hide_nsfw == 1,
                    "nsfw_visibility": visibility_options[user.hide_nsfw],
                    "show_nsfl": not user.hide_nsfl == 1,
                    "nsfl_visibility": visibility_options[user.hide_nsfl],
                    "default_sort_type": user.default_sort.capitalize() if user.default_sort else 'Hot',
                    "default_comment_sort_type": user.default_comment_sort.capitalize() if user.default_comment_sort else 'Hot',
                    "default_listing_type": user.default_filter.capitalize() if user.default_filter else 'Popular',
                    "show_scores": True,
                    "show_bot_accounts": not user.ignore_bots == 1,
                    "show_read_posts": not user.hide_read_posts == True,
                    "reply_collapse_threshold": user.reply_collapse_threshold,
                    "reply_hide_threshold": user.reply_hide_threshold,
                    "hide_low_quality": user.hide_low_quality if user.hide_low_quality else False,
                    "community_keyword_filter": user.community_keyword_filter.split(", ") if user.community_keyword_filter else [],
                    "bot_visibility": visibility_options[user.ignore_bots],
                    "accept_private_messages": private_message_options[user.accept_private_messages],
                    "newsletter": user.newsletter,
                    "email_unread": user.email_unread,
                    "searchable": user.searchable,
                    "indexable": user.indexable,
                    "federate_votes": not user.vote_privately == True,
                    "feed_auto_follow": user.feed_auto_follow,
                    "feed_auto_leave": user.feed_auto_leave,
                    "ai_visibility": ai_visibility_options[user.hide_gen_ai]
                },
                "person": {
                    "id": user.id,
                    "user_name": user.user_name,
                    "banned": user.banned,
                    "published": user.created.isoformat(timespec="microseconds") + 'Z',
                    "actor_id": user.public_url(),
                    "local": True,
                    "deleted": user.deleted,
                    "bot": user.bot,
                    "instance_id": 1,
                    "title": user.display_name(),
                    "avatar": user.avatar.medium_url() if user.avatar_id else None,
                    "banner": user.cover.medium_url() if user.cover_id else None,
                    "about": user.about,
                    "about_html": user.about_html,
                    "extra_fields": extra_fields,
                },
                "counts": {
                    "person_id": user.id,
                    "post_count": user.post_count,
                    "comment_count": user.post_reply_count
                }
            },
            "moderates": moderating_communities_view(user),
            "follows": joined_communities_view(user),
            "community_blocks": blocked_communities_view(user),
            "instance_blocks": blocked_instances_view(user),
            "person_blocks": blocked_people_view(user),
            "discussion_languages": []  # TODO
        }

        # Delete some fields if they are null for backwards compatibility
        if not v6["local_user_view"]["person"]["about"]:
            del v6["local_user_view"]["person"]["about"]
        if not v6["local_user_view"]["person"]["about_html"]:
            del v6["local_user_view"]["person"]["about_html"]
        if not v6["local_user_view"]["person"]["extra_fields"]:
            del v6["local_user_view"]["person"]["extra_fields"]

        return v6

    # Variant 7 - from resolve_object
    if variant == 7:
        v7 = {'person': user_view(user=user, variant=2, user_id=user_id)}
        return v7


def community_view(community: Community | int | str, variant, stub=False, user_id=None) -> dict:
    if isinstance(community, int):
        community = Community.query.filter_by(id=community).one()
    elif isinstance(community, str):
        name, ap_domain = community.strip().split('@')
        community = Community.query.filter_by(name=name, ap_domain=ap_domain).first()
        if community is None:
            community = Community.query.filter(func.lower(Community.name) == name.lower(),
                                               func.lower(Community.ap_domain) == ap_domain.lower()).one()

    # Variant 1 - models/community/community.dart
    if variant == 1:
        include = ['id', 'name', 'title', 'banned', 'nsfw', 'restricted_to_mods', 'question_answer', 'ai_generated']
        v1 = {column.name: getattr(community, column.name) for column in community.__table__.columns if
              column.name in include}
        v1.update({'published': community.created_at.isoformat(timespec="microseconds") + 'Z',
                   'updated': community.created_at.isoformat(timespec="microseconds") + 'Z',
                   'deleted': community.banned,
                   'removed': False,
                   'actor_id': community.public_url(),
                   'local': community.is_local(),
                   'hidden': not community.show_all,
                   'instance_id': community.instance_id if community.instance_id else 1,
                   'ap_domain': community.ap_domain,
                   'ai_generated': bool(v1['ai_generated'])})
        if community.description and not stub:
            v1['description'] = community.description
        if not stub:
            v1['posting_warning'] = community.posting_warning
        if community.icon_id:
            valid_url = community.icon.medium_url()
            if valid_url:
                v1['icon'] = valid_url
        if community.image_id and not stub:
            valid_url = community.image.medium_url()
            if valid_url:
                v1['banner'] = valid_url

        return v1

    # Variant 2 - views/community_view.dart - /community/list api endpoint
    if variant == 2:
        # counts - models/community/community_aggregates
        include = ['id', 'subscriptions_count', 'total_subscriptions_count', 'post_count', 'post_reply_count',
                   'active_daily', 'active_weekly', 'active_monthly', 'active_6monthly', 'average_rating']
        counts = {column.name: getattr(community, column.name) for column in community.__table__.columns if
                  column.name in include}
        if counts['total_subscriptions_count'] == None or counts['total_subscriptions_count'] == 0:
            counts['total_subscriptions_count'] = counts['subscriptions_count']
        counts.update({'published': community.created_at.isoformat(timespec="microseconds") + 'Z'})
        
        # Return zero if stats are None
        stats_list = ['active_daily', 'active_weekly', 'active_monthly', 'active_6monthly']
        for stat in stats_list:
            if not counts[stat]:
                counts[stat] = 0
        
        if user_id:
            followed = db.session.execute(text(
                'SELECT user_id FROM "community_member" WHERE community_id = :community_id and user_id = :user_id'),
                                          {"community_id": community.id, "user_id": user_id}).scalar()
            blocked = True if community.id in blocked_communities(user_id) else False
            community_sub = db.session.execute(text(
                'SELECT user_id FROM "notification_subscription" WHERE type = :type and entity_id = :entity_id and user_id = :user_id'),
                                               {'type': NOTIF_COMMUNITY, 'entity_id': community.id,
                                                'user_id': user_id}).scalar()
        else:
            followed = blocked = community_sub = False
        subscribe_type = 'Subscribed' if followed else 'NotSubscribed'
        activity_alert = True if community_sub else False
        v2 = {'community': community_view(community=community, variant=1, stub=stub), 'subscribed': subscribe_type,
              'blocked': blocked, 'activity_alert': activity_alert, 'counts': counts,}
        
        comm_flair = []
        flair_list = get_comm_flair_list(community)
        for flair in flair_list:
            comm_flair.append(flair_view(flair))
        
        v2['flair_list'] = comm_flair
        
        return v2

    # Variant 3 - models/community/get_community_response.dart - /community api endpoint
    if variant == 3:
        modlist = cached_modlist_for_community(community.id)

        v3 = {'community_view': community_view(community=community, variant=2, stub=False, user_id=user_id),
              'moderators': modlist, 'discussion_languages': []}
        
        return v3

    # Variant 4 - models/community/community_response.dart - /community/follow api endpoint
    if variant == 4:
        v4 = {'community_view': community_view(community=community, variant=2, stub=False, user_id=user_id),
              'discussion_languages': []}
        return v4

    # Variant 5 - models/community/block_community_response.dart - /community/block api endpoint
    if variant == 5:
        block = db.session.execute(
            text('SELECT user_id FROM "community_block" WHERE user_id = :user_id and community_id = :community_id'),
            {'user_id': user_id, 'community_id': community.id}).scalar()
        blocked = True if block else False
        v5 = {'community_view': community_view(community=community, variant=2, stub=False, user_id=user_id),
              'blocked': blocked}
        return v5

    # Variant 6 - from resolve_object
    if variant == 6:
        v6 = {'community': community_view(community=community, variant=2, stub=False, user_id=user_id)}
        return v6


def flair_view(flair: CommunityFlair | int):
    if isinstance(flair, int):
        flair = CommunityFlair.query.filter_by(id=flair).one()
    
    flair_item = {}
    flair_item["id"] = flair.id
    flair_item["community_id"] = flair.community_id
    flair_item["flair_title"] = flair.flair
    flair_item["text_color"] = flair.text_color
    flair_item["background_color"] = flair.background_color

    if flair.blur_images:
        flair_item["blur_images"] = flair.blur_images
    else:
        flair_item["blur_images"] = False
    
    flair_item["ap_id"] = flair.get_ap_id()
    
    return flair_item


# emergency function - shouldn't be called in normal circumstances
@cache.memoize(timeout=86400)
def calculate_path(reply):
    path = [0, reply.id]
    if reply.depth == 1:
        path = [0, reply.parent_id, reply.id]
    elif reply.depth > 1:
        path = [0]
        parent_id = reply.parent_id
        depth = reply.depth - 1
        path_ids = [reply.id, reply.parent_id]
        while depth > 0:
            pid = db.session.execute(text('SELECT parent_id FROM "post_reply" WHERE id = :parent_id'),
                                     {'parent_id': parent_id}).scalar()
            path_ids.append(pid)
            parent_id = pid
            depth -= 1
        for pid in path_ids[::-1]:
            path.append(pid)
    reply.path = path
    db.session.commit()


# emergency function - shouldn't be called in normal circumstances
def calculate_child_count(reply):
    child_count = db.session.execute(
        text('select count(*) as c from post_reply where :id = ANY(path) and id != :id and deleted = false'),
        {"id": reply.id}).scalar()
    reply.child_count = child_count
    db.session.commit()


def reply_view(reply: PostReply | int, variant: int, user_id=None,
                        is_user_banned_from_community=None,
                        is_user_following_community=None,
                        is_reply_bookmarked=None,
                        is_creator_blocked=None,
                        vote_effect=None,
                        is_reply_subscribed=None,
                        is_creator_banned_from_community=None,
                        is_creator_moderator=None,
                        is_creator_admin=None,
                        is_user_moderator=None,
                        add_creator_in_view=True,
                        add_post_in_view=True,
                        add_community_in_view=True,
                        read_comment_ids=None) -> dict:
    if isinstance(reply, int):
        reply = PostReply.query.get(reply)

    if read_comment_ids is None:
        read_comment_ids = []

    # Variant 1 - Comment model
    if variant == 1:
        include = ['id', 'user_id', 'post_id', 'body', 'deleted', 'answer', 'emoji_reactions']
        v1 = {column.name: getattr(reply, column.name) for column in reply.__table__.columns if column.name in include}

        v1.update({'published': reply.posted_at.isoformat(timespec="microseconds") + 'Z',
                   'ap_id': reply.profile_id(),
                   'local': reply.is_local(),
                   'language_id': reply.language_id if reply.language_id else 0,
                   'distinguished': reply.distinguished if reply.distinguished else False,
                   'locked': not reply.replies_enabled if reply.replies_enabled is not None else False,
                   'removed': False})
        v1['repliesEnabled'] = not v1['locked']

        if not reply.path:
            calculate_path(reply)
        v1['path'] = '.'.join(str(id) for id in reply.path)
        if reply.edited_at:
            v1['updated'] = reply.edited_at.isoformat(timespec="microseconds") + 'Z'
        if reply.deleted == True:
            v1['body'] = ''
            if reply.deleted_by and reply.user_id != reply.deleted_by:
                v1['removed'] = True
                v1['deleted'] = False

        return v1

    # Variant 2 - CommentAggregatesModel
    if variant == 2:
        v2 = {'comment_id': reply.id, 'score': reply.up_votes - reply.down_votes, 'upvotes': reply.up_votes,
              'downvotes': reply.down_votes,
              'published': reply.posted_at.isoformat(timespec="microseconds") + 'Z',
              'child_count': reply.child_count if reply.child_count is not None else 0}

        return v2

    # Variant 3 - CommentView
    if variant == 3:
        if is_user_banned_from_community is not None:
            user_banned = is_user_banned_from_community
        else:
            user_banned = reply.community_id in communities_banned_from(user_id) if user_id else False

        if is_user_following_community is not None:
            subscribe_type = 'Subscribed' if is_user_following_community else 'NotSubscribed'
        else:
            subscribe_type = 'NotSubscribed'
            if user_id:
                followed = db.session.execute(text(
                            'SELECT user_id FROM "community_member" WHERE community_id = :community_id and user_id = :user_id'),
                            {"community_id": reply.community_id, "user_id": user_id}).scalar()
                if followed:
                    subscribe_type = 'Subscribed'

        if is_reply_bookmarked is not None:
            saved = is_reply_bookmarked
        else:
            bookmarked = db.session.execute(text(
                'SELECT user_id FROM "post_reply_bookmark" WHERE post_reply_id = :post_reply_id and user_id = :user_id'),
                {'post_reply_id': reply.id, 'user_id': user_id}).scalar() if user_id else False
            saved = True if bookmarked else False

        if is_creator_blocked is not None:
            creator_blocked = is_creator_blocked
        else:
            creator_blocked = reply.user_id in blocked_users(user_id) if user_id else False

        if vote_effect is not None:
            my_vote = vote_effect
        else:
            reply_vote = db.session.execute(text(
                'SELECT effect FROM "post_reply_vote" WHERE post_reply_id = :post_reply_id and user_id = :user_id'),
                {'post_reply_id': reply.id, 'user_id': user_id}).scalar() if user_id else 0
            effect = reply_vote if reply_vote else 0
            my_vote = int(effect)

        if is_reply_subscribed is not None:
            activity_alert = is_reply_subscribed
        else:
            reply_sub = db.session.execute(text(
                'SELECT user_id FROM "notification_subscription" WHERE type = :type and entity_id = :entity_id and user_id = :user_id'),
                {'type': NOTIF_REPLY, 'entity_id': reply.id, 'user_id': user_id}).scalar() if user_id else False
            activity_alert = True if reply_sub else False

        if is_creator_banned_from_community is not None:
            creator_banned = is_creator_banned_from_community
        else:
            creator_banned = reply.community_id in communities_banned_from(reply.user_id)

        if is_creator_moderator is not None:
            creator_is_moderator = is_creator_moderator
        else:
            creator_is_moderator = reply.community.is_moderator(reply.author)

        if is_creator_admin is not None:
            creator_is_admin = is_creator_admin
        else:
            creator_is_admin = reply.user_id in g.admin_ids

        if is_user_moderator is not None:
            can_auth_user_moderate = is_user_moderator
        else:
            can_auth_user_moderate = any(
                moderator.user_id == user_id for moderator in reply.community.moderators()) if user_id else False

        v3 = {
            'comment': reply_view(reply=reply, variant=1),
            'counts': reply_view(reply=reply, variant=2),
            'banned_from_community': user_banned,
            'subscribed': subscribe_type,
            'saved': saved,
            'creator_blocked': creator_blocked,
            'my_vote': my_vote,
            'activity_alert': activity_alert,
            'creator_banned_from_community': creator_banned,
            'creator_is_moderator': creator_is_moderator,
            'creator_is_admin': creator_is_admin,
            'can_auth_user_moderate': can_auth_user_moderate
        }
        if add_creator_in_view:
            v3['creator'] = user_view(user=reply.author, variant=1, stub=True, flair_community_id=reply.community_id,
                                      user_id=user_id)
        if add_post_in_view:
            v3['post'] = post_view(post=reply.post, variant=1)
        if add_community_in_view:
            v3['community'] = community_view(community=reply.community, variant=1, stub=True)

        return v3

    # Variant 4 - CommentResponse
    if variant == 4:
        v4 = {
            'comment_view': reply_view(reply=reply, variant=3, user_id=user_id,
                                                is_reply_bookmarked=is_reply_bookmarked,
                                                is_reply_subscribed=is_reply_subscribed)
        }

        return v4

    # Variant 5 - ResolveObjectResponse
    if variant == 5:
        v5 = {
            'comment': reply_view(reply=reply, variant=3, user_id=user_id)
        }

        return v5

    # Variant 6 - CommentReply model (part of GetRepliesResponse to /user/replies endpoint)
    if variant == 6:
        v6 = {
            'id': reply.id, 'recipient_id': user_id, 'comment_id': reply.id,
            'read': reply.id in read_comment_ids,
            'published': reply.posted_at.isoformat(timespec="microseconds") + 'Z'
        }

        return v6


def reply_report_view(report, reply_id, user_id) -> dict:
    # /comment/report api endpoint
    # similar to a reply_view in many ways, except that the 'creator' is the report creator,
    # not the reported comment's creator.
    is_creator_blocked = report.reporter_id in blocked_users(user_id)
    is_creator_banned_from_community = report.suspect_community_id in communities_banned_from(report.reporter_id)
    is_creator_moderator = report.suspect_community_id in moderating_communities_ids(report.reporter_id)
    is_creator_admin = report.reporter_id in g.admin_ids
    report_json = reply_view(reply=reply_id, variant=3, user_id=user_id,
                             is_creator_blocked=is_creator_blocked,
                             is_creator_banned_from_community=is_creator_banned_from_community,
                             is_creator_moderator=is_creator_moderator,
                             is_creator_admin=is_creator_admin)
    report_json['comment_creator'] = user_view(user=report.suspect_user_id, variant=1, stub=True, user_id=user_id)
    report_json['creator'] = user_view(user=user_id, variant=1, stub=True, user_id=user_id)

    report_json['comment_report'] = {
        'id': report.id,
        'creator_id': report.reporter_id,
        'comment_id': report.suspect_post_reply_id,
        'original_comment_text': report_json['comment']['body'],
        'reason': report.reasons,
        'resolved': report.status == 3,
        'published': report.created_at.isoformat(timespec="microseconds") + 'Z'
    }
    # TODO when it's easy to get a report's resolver
    # - add resolver_id to 'comment_report'
    # - add resolver{} user_view

    v1 = {
        'comment_report_view': report_json
    }
    return v1


def post_report_view(report, post_id, user_id) -> dict:
    # views/post_report_view.dart - /post/report api endpoint
    post_json = post_view(post=post_id, variant=2, user_id=user_id)
    community_json = community_view(community=post_json['post']['community_id'], variant=1, stub=True)

    banned = db.session.execute(
        text('SELECT user_id FROM "community_ban" WHERE user_id = :user_id and community_id = :community_id'),
        {'user_id': report.reporter_id, 'community_id': community_json['id']}).scalar()
    moderator = db.session.execute(
        text('SELECT is_moderator FROM "community_member" WHERE user_id = :user_id and community_id = :community_id'),
        {'user_id': report.reporter_id, 'community_id': community_json['id']}).scalar()
    admin = db.session.execute(text('SELECT user_id FROM "user_role" WHERE user_id = :user_id and role_id = 4'),
                               {'user_id': report.reporter_id}).scalar()

    creator_banned_from_community = True if banned else False
    creator_is_moderator = True if moderator else False
    creator_is_admin = True if admin else False

    v1 = {
        'post_report_view': {
            'post_report': {
                'id': report.id,
                'creator_id': report.reporter_id,
                'post_id': report.suspect_post_id,
                'original_post_name': post_json['post']['title'],
                'original_post_body': '',
                'reason': report.reasons,
                'resolved': report.status == 3,
                'published': report.created_at.isoformat(timespec="microseconds") + 'Z'
            },
            'post': post_json['post'],
            'community': community_json,
            'creator': user_view(user=user_id, variant=1, stub=True, user_id=user_id),
            'post_creator': user_view(user=report.suspect_user_id, variant=1, stub=True, user_id=user_id),
            'counts': post_json['counts'],
            'creator_banned_from_community': creator_banned_from_community,
            'creator_is_moderator': creator_is_moderator,
            'creator_is_admin': creator_is_admin,
            'creator_blocked': False,
            'subscribed': post_json['subscribed'],
            'saved': post_json['saved']
        }
    }
    return v1


def search_view(type) -> dict:
    v1 = {
        'type_': type,
        'comments': [],
        'posts': [],
        'communities': [],
        'users': []
    }
    return v1


def instance_view(instance: Instance | int, variant) -> dict:
    if isinstance(instance, int):
        instance = Instance.query.filter_by(id=instance).one()

    if variant == 1:
        include = ['id', 'domain', 'software', 'version']
        v1 = {column.name: getattr(instance, column.name) for column in instance.__table__.columns if
              column.name in include}
        if not v1['version']:
            v1.update({'version': '0.0.1'})
        v1.update(
            {'published': instance.created_at.isoformat(timespec="microseconds") + 'Z',
             'updated': instance.updated_at.isoformat(timespec="microseconds") + 'Z'})

        return v1


def feed_view(feed: Feed | int, variant: int, user_id, subscribed, include_communities, communities_moderating,
              banned_from, communities_joined, blocked_community_ids, blocked_instance_ids, ) -> dict:
    if isinstance(feed, int):
        feed = Feed.query.get(feed)

    if variant == 1:
        include = ['id', 'user_id', 'title', 'name', 'machine_name', 'description', 'description_html', 'nsfw', 'nsfl',
                   'subscriptions_count', 'num_communities', 'public', 'parent_feed_id',
                   'is_instance_feed', 'ap_domain', 'show_posts_in_children']
        v1 = {column.name: getattr(feed, column.name) for column in feed.__table__.columns if
              column.name in include}
        
        # Rename some fields for consistency with other endpoints
        v1["communities_count"] = v1.pop("num_communities")
        v1["show_posts_from_children"] = v1.pop("show_posts_in_children")

        if v1["public"]:
            v1["actor_id"] = feed.public_url()
        else:
            if not user_id:
                raise Exception("insufficient permissions")
            if not user_id == feed.user_id:
                user = User.query.get(user_id)
                if not user.is_admin():
                    raise Exception("insufficient permissions")
            
            v1["actor_id"] = feed.public_url() + "/" + feed.name.rsplit("/", 1)[1]

        if feed.icon_id:
            valid_url = feed.icon.medium_url()
            if valid_url:
                v1['icon'] = valid_url
        if feed.image_id:
            valid_url = feed.image.medium_url()
            if valid_url:
                v1['banner'] = valid_url

        v1['subscribed'] = feed.id in subscribed
        v1['owner'] = user_id == feed.user_id
        v1['local'] = feed.is_local()

        v1['communities'] = []
        if include_communities:
            for community in Community.query.filter(Community.banned == False).\
                join(FeedItem, FeedItem.community_id == Community.id).filter(FeedItem.feed_id == feed.id):
                if community.id not in blocked_community_ids and \
                        community.instance_id not in blocked_instance_ids and \
                        community.id not in banned_from:
                    v1['communities'].append(community_view(community, variant=1, stub=True))

        v1.update(
            {'published': feed.created_at.isoformat(timespec="microseconds") + 'Z',
             'updated': feed.last_edit.isoformat(timespec="microseconds") + 'Z'})

        return v1

    # This variant is from the resolve_object endpoint    
    elif variant == 2:
        v2 = {"feed": feed_view(feed=feed, variant=1, user_id=user_id, subscribed=subscribed,
                                include_communities=include_communities, communities_moderating=communities_moderating,
                                banned_from=banned_from, communities_joined=communities_joined,
                                blocked_community_ids=blocked_community_ids, blocked_instance_ids=blocked_instance_ids)}
        
        v2["feed"]["children"] = []
        return v2


def private_message_view(cm: ChatMessage, variant, report=None) -> dict:
    creator = user_view(cm.sender_id, variant=1)
    recipient = user_view(cm.recipient_id, variant=1)
    is_local = (creator['instance_id'] == 1 and recipient['instance_id'] == 1)
    ap_id = cm.ap_id
    if ap_id is None:
        # old CMs will be missing an ap_id
        if creator['instance_id'] == 1:
            ap_id = f"{current_app.config['SERVER_URL']}/private_message/{cm.id}"
        else:
            # CMs from remote instances can't be re-created so fudge something here to keep validator happy
            ap_id = f"{creator['actor_id']}/message/{cm.id}"

    v1 = {
        'private_message': {
            'id': cm.id,
            'creator_id': cm.sender_id,
            'recipient_id': cm.recipient_id,
            'content': cm.body if not cm.deleted else 'Deleted by author',
            'deleted': cm.deleted if cm.deleted is not None else False,
            'read': cm.read,
            'published': cm.created_at.isoformat(timespec="microseconds") + 'Z',
            'ap_id': ap_id,
            'local': is_local
        },
        'creator': creator,
        'recipient': recipient,
        'conversation_id': cm.conversation_id
    }

    if variant == 1:
        return v1

    v2 = {
        'private_message_view': v1
    }

    if variant == 2:
        return v2

    v3 = {
        'private_message_report_view': {
            'private_message_report': {
                'id': report.id,
                'creator_id': report.reporter_id,
                'private_message_id': cm.id,
                'original_pm_text': cm.body,
                'reason': report.reasons,
                'resolved': report.status == 3,
                'published': report.created_at.isoformat(timespec="microseconds") + 'Z'
            },
            'private_message': {
                'id': cm.id,
                'creator_id': cm.sender_id,
                'recipient_id': cm.recipient_id,
                'content': cm.body if not cm.deleted else 'Deleted by author',
                'deleted': cm.deleted,
                'read': cm.read,
                'published': cm.created_at.isoformat(timespec="microseconds") + 'Z',
                'ap_id': ap_id,
                'local': is_local
            },
            'private_message_creator': creator,
            'creator': user_view(report.reporter_id, variant=1)
        }
    }

    if variant == 3:
        return v3


def topic_view(topic: Topic | int, variant: int, communities_moderating, banned_from,
               communities_joined, blocked_community_ids, blocked_instance_ids,
               include_communities) -> dict:
    if isinstance(topic, int):
        topic = Topic.query.get(topic)

    if variant == 1:
        include = ['id', 'machine_name', 'name', 'num_communities', 'parent_id', 'show_posts_in_children']
        v1 = {column.name: getattr(topic, column.name) for column in topic.__table__.columns if
              column.name in include}

        # Rename some fields for consistency with other endpoints
        v1["title"] = v1.pop("name")
        v1["name"] = v1.pop("machine_name")
        v1["communities_count"] = v1.pop("num_communities")
        v1["show_posts_from_children"] = v1.pop("show_posts_in_children") or False
        v1["parent_topic_id"] = v1.pop("parent_id")

        v1['communities'] = []
        if include_communities:
            for community in Community.query.filter(Community.banned == False, Community.topic_id == topic.id):
                if community.id not in blocked_community_ids and \
                        community.instance_id not in blocked_instance_ids and \
                        community.id not in banned_from:
                    v1['communities'].append(community_view(community, variant=1, stub=True))

        return v1


def site_view(user) -> dict:
    logo = g.site.logo if g.site.logo else '/static/images/piefed_logo_icon_t_75.png'

    site = {
        "enable_downvotes": g.site.enable_downvotes,
        "icon": f"{current_app.config['SERVER_URL']}{logo}",
        "registration_mode": g.site.registration_mode,
        "name": g.site.name,
        "actor_id": f"{current_app.config['SERVER_URL']}/",
        "user_count": users_total(),
        "all_languages": []
    }

    if g.site.sidebar and g.site.sidebar_html:
        if g.site.sidebar == g.site.sidebar_html:
            site['sidebar'] = g.site.sidebar_html
        else:
            site['sidebar_md'] = g.site.sidebar
            site['sidebar'] = g.site.sidebar_html
    elif g.site.sidebar:
        site['sidebar'] = g.site.sidebar
    
    if g.site.description:
        site['description'] = g.site.description
    for language in Language.query.all():
        site["all_languages"].append({
            "id": language.id,
            "code": language.code,
            "name": language.name
        })

    v1 = {
        "version": current_app.config['VERSION'],
        "admins": [],
        "site": site
    }
    for admin in Site.admins():
        v1['admins'].append(user_view(user=admin, variant=2))
    if user:
        v1['my_user'] = user_view(user=user, variant=6)

    return v1


def site_instance_chooser_view():
    logo = g.site.logo if g.site.logo else '/static/images/piefed_logo_icon_t_75.png'
    language = Language.query.get(g.site.language_id)
    defed_list = BannedInstances.query.filter(or_(BannedInstances.domain == 'hexbear.net',
                                                  BannedInstances.domain == 'lemmygrad.ml',
                                                  BannedInstances.domain == 'hilariouschaos.com',
                                                  BannedInstances.domain == 'gregtech.eu',
                                                  BannedInstances.domain == 'lemmy.ml')).order_by(BannedInstances.domain).all()
    trusted_list = Instance.query.filter(Instance.trusted).all()
    maturity = 0
    if get_setting('financial_stability', False):
        maturity += 1
    if get_setting('number_of_admins', 0) > 1:
        maturity += 1
    if get_setting('daily_backups', False):
        maturity += 1

    if maturity >= 3:
        maturity = 'High'
    elif maturity == 2:
        maturity = 'Medium'
    elif maturity == 1:
        maturity = 'Low'
    elif maturity <= 0:
        maturity = 'Embryonic'

    result = {
        'language': {
            "id": language.id,
            "code": language.code,
            "name": language.name
        },
        'nsfw': g.site.enable_nsfw,
        'newbie_friendly': num_topics() >= 3,
        'name': g.site.name,
        'elevator_pitch': get_setting('elevator_pitch', ''),
        'description': g.site.description or '',
        'about': g.site.about_html or '',
        'sidebar': g.site.sidebar_html or '',
        'logo_url': f"{current_app.config['SERVER_URL']}{logo}",
        'maturity': maturity,
        'mau': active_month(),
        'can_make_communities': not g.site.community_creation_admin_only,
        'defederation': list(set([instance.domain for instance in defed_list])),
        'trusts': list(set([instance.domain for instance in trusted_list])),
        'tos_url': g.site.tos_url,
        'registration_mode': g.site.registration_mode
    }
    return result


@cache.memoize(timeout=600)
def federated_instances_view():
    instances = Instance.query.filter(Instance.id != 1, Instance.gone_forever == False).all()
    linked = []
    allowed = []
    blocked = []
    for instance in AllowedInstances.query.all():
        allowed.append({"id": instance.id,
                        "domain": instance.domain,
                        "published": utcnow().isoformat(timespec="microseconds") + "Z",
                        "updated": utcnow().isoformat(timespec="microseconds") + "Z"})
    for instance in BannedInstances.query.all():
        blocked.append({"id": instance.id,
                        "domain": instance.domain,
                        "published": utcnow().isoformat(timespec="microseconds") + "Z",
                        "updated": utcnow().isoformat(timespec="microseconds") + "Z"})
    for instance in instances:
        instance_data = {"id": instance.id,
                         "domain": instance.domain,
                         "published": instance.created_at.isoformat(timespec="microseconds") + "Z",
                         "updated": instance.updated_at.isoformat(timespec="microseconds") + "Z"}
        if instance.software:
            instance_data['software'] = instance.software
        if instance.version:
            instance_data['version'] = instance.version
        if not any(blocked_instance.get('domain') == instance.domain for blocked_instance in blocked):
            linked.append(instance_data)
    v1 = {
        "federated_instances": {
            "linked": linked,
            "allowed": allowed,
            "blocked": blocked
        }
    }
    return v1


@cache.memoize(timeout=60)
def cached_modlist_for_community(community_id):
    moderator_ids = db.session.execute(
        text('SELECT user_id FROM "community_member" WHERE community_id = :community_id and (is_moderator = True or is_owner = True)'),
        {'community_id': community_id}).scalars()
    modlist = []
    for m_id in moderator_ids:
        entry = {
            'community': community_view(community=community_id, variant=1, stub=True),
            'moderator': user_view(user=m_id, variant=1, stub=True)
        }
        modlist.append(entry)
    return modlist


@cache.memoize(timeout=60)
def cached_modlist_for_user(user):
    community_ids = db.session.execute(
        text('SELECT community_id FROM "community_member" WHERE user_id = :user_id and (is_moderator = True or is_owner = True)'),
        {'user_id': user.id}).scalars()
    modlist = []
    for c_id in community_ids:
        entry = {
            'community': community_view(community=c_id, variant=1, stub=True),
            'moderator': user_view(user=user, variant=1, stub=True)
        }
        modlist.append(entry)
    return modlist


@cache.memoize(timeout=3000)
def users_total():
    return db.session.execute(text(
        'SELECT COUNT(*) as c FROM "user" WHERE ap_id is null AND verified is true AND banned is false AND deleted is false')).scalar()


def moderating_communities_view(user):
    moderates = []
    inner_user_view = user_view(user, variant=1, stub=True)
    for community in moderating_communities(user.id):
        moderates.append({'community': community_view(community, variant=1, stub=True), 'moderator': inner_user_view})
    return moderates


def joined_communities_view(user):
    follows = []
    inner_user_view = user_view(user, variant=1, stub=True)
    for community in joined_communities(user.id):
        follows.append({'community': community_view(community, variant=1, stub=True), 'follower': inner_user_view})
    return follows


def blocked_people_view(user) -> list[dict]:
    blocked_ids = blocked_users(user.id)
    blocked = []
    for blocked_user in User.query.filter(User.id.in_(blocked_ids)).all():
        blocked.append({'person': user_view(user, variant=1, stub=True), 'target': user_view(blocked_user, variant=1, stub=True)})
    return blocked


def blocked_communities_view(user) -> list[dict]:
    blocked_ids = blocked_communities(user.id)
    blocked = []
    for blocked_comm in Community.query.filter(Community.id.in_(blocked_ids)).all():
        blocked.append({'person': user_view(user, variant=1, stub=True), 'community': community_view(blocked_comm, variant=1, stub=True)})
    return blocked


def blocked_instances_view(user) -> list[dict]:
    blocked_ids = blocked_or_banned_instances(user.id)
    blocked = []
    for blocked_instance in Instance.query.filter(Instance.id.in_(blocked_ids)).all():
        blocked.append({'person': user_view(user, variant=1, stub=True), 'instance': instance_view(blocked_instance, variant=1)})
    return blocked


