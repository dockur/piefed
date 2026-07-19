from datetime import timedelta

from flask import current_app, g
from sqlalchemy import desc, text, and_, exists, asc, or_
from sqlakeyset import get_page
from sqlalchemy.exc import IntegrityError

from app import db, plugins, cache
from app.api.alpha.views import post_view, post_report_view, reply_view, community_view, user_view, flair_view
from app.constants import *
from app.feed.routes import get_all_child_feed_ids
from app.models import Post, Community, CommunityMember, utcnow, User, Feed, FeedItem, Topic, PostReply, PostVote, \
    CommunityFlair, read_posts, Poll, Report
from app.shared.post import vote_for_post, bookmark_post, remove_bookmark_post, subscribe_post, make_post, edit_post, \
    delete_post, restore_post, report_post, lock_post, sticky_post, mod_remove_post, mod_restore_post, mark_post_read, \
    vote_for_poll, hide_post
from app.post.util import post_replies, get_comment_branch, tags_to_string, flair_to_string
from app.topic.routes import get_all_child_topic_ids
from app.utils import authorise_api_user, blocked_users, blocked_communities, blocked_or_banned_instances, \
    recently_upvoted_posts, \
    site_language_id, filtered_out_communities, joined_or_modding_communities, \
    user_filters_home, user_filters_posts, in_sorted_list, instance_sticky_posts, instance_sticky_post_ids, \
    communities_banned_from_all_users, moderating_communities_ids_all_users, blocked_domains, SqlKeysetPagination, \
    community_membership_private, paginate_post_ids, post_ids_to_models, user_access, moderating_communities_ids
from app.shared.tasks import task_selector


def get_post_list(auth, data, user_id=None, search_type='Posts') -> dict:
    type = data['type_'] if 'type_' in data else "All"
    sort = data['sort'] if 'sort' in data else "Hot"
    if 'page_cursor' in data:
        page = int(data['page_cursor'])
    elif 'page' in data:
        page = int(data['page'])
    else:
        page = 1
    limit = int(data['limit']) if 'limit' in data else 50
    liked_only = data['liked_only'] if 'liked_only' in data else False
    saved_only = data['saved_only'] if 'saved_only' in data else False
    ignore_sticky = data['ignore_sticky'] if 'ignore_sticky' in data else False
    minimum_upvotes = data['minimum_upvotes'] if 'minimum_upvotes' in data and data['minimum_upvotes'] else None
    nsfw = data['nsfw'] if 'nsfw' in data else ''

    query = data['q'] if 'q' in data else ''

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    if auth:
        user_id = authorise_api_user(auth)

    # get the user to check if the user has hide_read posts set later down the function
    if user_id:
        user = User.query.get(user_id)
        g.user = user   # save the currently logged in user into g, to save loading it up again and again in post_view.

    # user_id: the logged in user
    # person_id: the author of the posts being requested

    community_id = int(data['community_id']) if 'community_id' in data else None
    feed_id = int(data['feed_id']) if 'feed_id' in data else None
    topic_id = int(data['topic_id']) if 'topic_id' in data else None
    community_name = data['community_name'] if 'community_name' in data else None
    person_id = int(data['person_id']) if 'person_id' in data else None

    if community_id or community_name:
        search_by_community = True
        # check private community access
        if community_id:
            community = Community.query.get(community_id)
        else:
            # parse community_name to get the community
            if '@' not in community_name:
                community_name_lookup = f"{community_name}@{current_app.config['SERVER_NAME']}"
            else:
                community_name_lookup = community_name
            name, ap_domain = community_name_lookup.split('@')
            community = Community.query.filter_by(name=name, ap_domain=ap_domain).first()

        if community and community.private:
            # community is private, check if user is a member
            if not user_id:
                raise Exception('Private community - authentication required')
            else:
                if not community.is_member(user):
                    raise Exception('Private community - membership required')
    else:
        search_by_community = False

    if user_id and user_id != person_id:
        blocked_person_ids = blocked_users(user_id)
        blocked_community_ids = blocked_communities(user_id)
        blocked_instance_ids = blocked_or_banned_instances(user_id)
        blocked_domain_ids = blocked_domains(user_id)
        private_community_ids = community_membership_private(user_id)
    else:
        blocked_person_ids = []
        blocked_community_ids = []
        blocked_instance_ids = []
        blocked_domain_ids = []
        private_community_ids = []

    content_filters = {}
    u_rp_ids = []

    # Post.user_id.not_in(blocked_person_ids)               # exclude posts by blocked users
    # Post.community_id.not_in(blocked_community_ids)       # exclude posts in blocked communities
    # Post.instance_id.not_in(blocked_instance_ids)         # exclude posts by users on blocked instances
    # Community.instance_id.not_in(blocked_instance_ids)    # exclude posts in communities on blocked instances

    # Depending on filters, determine whether instance stickies should be listed first or not
    segregate_instance_stickies = True

    use_faster_query = True     # Set use_faster_query = False if a code path that cannot use raw sql is hit
    post_query_criteria = []    # these will be combined using ' AND '.join(post_query_criteria) later on
    post_query_parameters = {}  # this dict will be used to do parameter substitution on the above query
    sql_order_by = []           # for sorting - build ORDER BY clause

    # As well as building a sqlalchemy query in the posts variable, build a plain text SQL query
    # This view will provide faster results most of the time but cannot be used for full-text searching - so we need both.
    #post_query_criteria.append(f'deleted is false AND status > {POST_STATUS_REVIEWING}')

    if blocked_person_ids:
        post_query_criteria.append('p.user_id NOT IN :blocked_person_ids')
        post_query_parameters['blocked_person_ids'] = tuple(blocked_person_ids)

    if blocked_community_ids:
        post_query_criteria.append('community_id NOT IN :blocked_community_ids')
        post_query_parameters['blocked_community_ids'] = tuple(blocked_community_ids)

    if blocked_domain_ids and not (type == "ModeratorView" or type == "Moderating"):
        post_query_criteria.append('(p.domain_id is null OR p.domain_id NOT IN :blocked_domain_ids)')
        post_query_parameters['blocked_domain_ids'] = tuple(blocked_domain_ids)

    if blocked_instance_ids:
        post_query_criteria.append('c.instance_id NOT IN :blocked_instance_ids AND p.instance_id NOT IN :blocked_instance_ids')
        post_query_parameters['blocked_instance_ids'] = tuple(blocked_instance_ids)

    if type == "Local":
        posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                  Post.user_id.not_in(blocked_person_ids),
                                  or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                  Post.community_id.not_in(blocked_community_ids)).\
            join(Community, Community.id == Post.community_id).filter_by(ap_id=None)
        post_query_criteria.append('c.ap_id is null')
    elif type == "Popular":
        posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                  Post.user_id.not_in(blocked_person_ids),
                                  Post.community_id.not_in(blocked_community_ids),
                                  or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                  Post.instance_id.not_in(blocked_instance_ids)). \
            join(Community, Community.id == Post.community_id).filter(Community.show_popular == True, Post.score > 100,
                                                                      Community.instance_id.not_in(
                                                                          blocked_instance_ids))
        
        post_query_criteria.append('score > 100')
        post_query_criteria.append('show_popular is true')

    elif type == "Subscribed" and user_id is not None:
        posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                  Post.user_id.not_in(blocked_person_ids),
                                  Post.community_id.not_in(blocked_community_ids),
                                  or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                  Post.instance_id.not_in(blocked_instance_ids)). \
            join(CommunityMember, Post.community_id == CommunityMember.community_id).filter_by(is_banned=False,
                                                                                               user_id=user_id). \
            join(Community, Community.id == CommunityMember.community_id).filter(
            Community.instance_id.not_in(blocked_instance_ids))
        
        post_query_criteria.append('community_id IN (SELECT community_id FROM community_member WHERE user_id = :user_id AND is_banned = FALSE)')
        post_query_parameters['user_id'] = user_id

    elif (type == "ModeratorView" or type == "Moderating") and user_id is not None:
        posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                  Post.user_id.not_in(blocked_person_ids),
                                  Post.community_id.not_in(blocked_community_ids),
                                  Post.instance_id.not_in(blocked_instance_ids)). \
            join(CommunityMember, Post.community_id == CommunityMember.community_id).filter_by(user_id=user_id,
                                                                                               is_moderator=True). \
            join(Community, Community.id == CommunityMember.community_id).filter(
            Community.instance_id.not_in(blocked_instance_ids))
        
        post_query_criteria.append('community_id IN (SELECT community_id FROM community_member WHERE user_id = :user_id AND is_moderator = TRUE)')
        post_query_parameters['user_id'] = user_id

    else:  # type == "All"
        if community_name:
            use_faster_query = False
            segregate_instance_stickies = False
            if not '@' in community_name:
                community_name = f"{community_name}@{current_app.config['SERVER_NAME']}"
            name, ap_domain = community_name.split('@')
            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.name == name,
                                                                          Community.ap_domain == ap_domain,
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))

            post_query_criteria.append('community_name = :community_name')
            post_query_criteria.append('ap_domain = :ap_domain')
            post_query_parameters.update({
                'community_name': name,
                'ap_domain': ap_domain
            })
            
            content_filters = user_filters_posts(user_id) if user_id else {}
        elif community_id:
            use_faster_query = False
            segregate_instance_stickies = False
            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.id == community_id,
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))
            
            # Build SQL query for materialized view - community_id
            post_query_criteria.append('community_id = :community_id')
            post_query_parameters['community_id'] = community_id
            
            content_filters = user_filters_posts(user_id) if user_id else {}
        elif feed_id:
            use_faster_query = False
            segregate_instance_stickies = False
            feed = Feed.query.get(feed_id)
            if feed.show_posts_in_children:  # include posts from child feeds
                feed_ids = get_all_child_feed_ids(feed)
            else:
                feed_ids = [feed.id]

            # for each feed get the community ids (FeedItem) in the feed
            # used for the posts searching
            feed_community_ids = []
            for fid in feed_ids:
                feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == fid).all()
                for item in feed_items:
                    feed_community_ids.append(item.community_id)

            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.id.in_(feed_community_ids),
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))
            
            # Build SQL query for materialized view - feed_id
            if feed_community_ids:
                post_query_criteria.append('community_id IN :feed_community_ids')
                post_query_parameters['feed_community_ids'] = tuple(feed_community_ids)
            
            content_filters = user_filters_posts(user_id) if user_id else {}
        elif topic_id:
            use_faster_query = False
            segregate_instance_stickies = False
            topic = Topic.query.get(topic_id)
            if topic.show_posts_in_children:  # include posts from child feeds
                topic_ids = get_all_child_topic_ids(topic)
            else:
                topic_ids = [topic.id]

            # for each feed get the community ids (FeedItem) in the feed
            # used for the posts searching
            topic_community_ids = []
            for tid in topic_ids:
                communities = Community.query.filter(Community.topic_id == tid).all()
                for item in communities:
                    topic_community_ids.append(item.id)

            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.id.in_(topic_community_ids),
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))
            
            # for materialized view - topic_id
            if topic_community_ids:
                post_query_criteria.append('community_id IN :topic_community_ids')
                post_query_parameters['topic_community_ids'] = tuple(topic_community_ids)
            
            content_filters = user_filters_posts(user_id) if user_id else {}
        elif person_id:
            use_faster_query = False
            segregate_instance_stickies = False
            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids), Post.user_id == person_id). \
                join(Community, Community.id == Post.community_id).filter(
                Community.instance_id.not_in(blocked_instance_ids))
            
            # for materialized view - person_id
            post_query_criteria.append('user_id = :person_id')
            post_query_parameters['person_id'] = person_id

        else:
            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.show_all == True,
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))
            
            post_query_criteria.append('show_all is true')
            
            content_filters = user_filters_home(user_id) if user_id else {}

    posts = posts.filter(or_(Community.private == False, Community.id.in_(private_community_ids)))
    
    # for materialized view - private community filtering
    if private_community_ids:
        post_query_criteria.append('(c.private is false OR community_id IN :private_community_ids)')
        post_query_parameters['private_community_ids'] = tuple(private_community_ids)

    if query:
        segregate_instance_stickies = False
        if search_type == 'Url':
            posts = posts.filter(Post.url.ilike(f"%{query}%"))
        else:
            use_faster_query = False
            posts = posts.search(query, sort=sort == 'Relevance').filter(Post.indexable == True)

    if user_id:
        if liked_only:
            use_faster_query = False
            segregate_instance_stickies = False
            upvoted_post_ids = recently_upvoted_posts(user_id)
            posts = posts.filter(Post.id.in_(upvoted_post_ids), Post.user_id != user_id)
            
            # SQL query building for liked_only
            post_query_criteria.append('post_id IN :upvoted_post_ids')
            post_query_criteria.append('user_id != :user_id')
            post_query_parameters['upvoted_post_ids'] = tuple(upvoted_post_ids)
            post_query_parameters['user_id'] = user_id
        elif saved_only:
            use_faster_query = False
            segregate_instance_stickies = False
            bookmarked_post_ids = tuple(db.session.execute(text('SELECT post_id FROM "post_bookmark" WHERE user_id = :user_id'),
                                                     {"user_id": user_id}).scalars())
            posts = posts.filter(Post.id.in_(bookmarked_post_ids))
            
            # SQL query building for saved_only
            post_query_criteria.append('post_id IN :bookmarked_post_ids')
            post_query_parameters['bookmarked_post_ids'] = bookmarked_post_ids
        else:
            u_rp_ids = tuple(db.session.execute(text('SELECT read_post_id FROM "read_posts" WHERE user_id = :user_id'),
                                          {"user_id": user_id}).scalars())
            if user.ignore_bots == 1:
                posts = posts.filter(Post.from_bot == False)
                post_query_criteria.append('from_bot = false')
            if user.hide_nsfl == 1:
                posts = posts.filter(Post.nsfl == False)
                post_query_criteria.append('p.nsfl = false')
            if nsfw == '' and user.hide_nsfw == 1:
                posts = posts.filter(Post.nsfw == False)
                post_query_criteria.append('p.nsfw = false')
            else:
                if nsfw == 'Exclude':
                    posts = posts.filter(Post.nsfw == False)
                    post_query_criteria.append('p.nsfw = false')
                elif nsfw == 'Only':
                    posts = posts.filter(Post.nsfw == True)
                    post_query_criteria.append('p.nsfw = true')
                elif nsfw == 'Include':
                    pass
            if user.hide_gen_ai == 1:
                posts = posts.filter(Post.ai_generated == False)
                post_query_criteria.append('p.ai_generated = false')
            if user.hide_read_posts and not query:
                # Alias the read_posts table
                rp = read_posts.alias()

                # Filter posts that the user has NOT read, using ~exists
                posts = posts.filter(
                    ~exists().where(
                        and_(
                            rp.c.user_id == user_id,
                            rp.c.read_post_id == Post.id
                        )
                    )
                )
                # SQL query building for hide_read_posts
                if len(u_rp_ids) > 0:
                    post_query_criteria.append('p.id NOT IN :read_post_ids')
                    post_query_parameters['read_post_ids'] = tuple(u_rp_ids)

        filtered_out_community_ids = filtered_out_communities(user)
        if len(filtered_out_community_ids):
            posts = posts.filter(Post.community_id.not_in(filtered_out_community_ids))
            post_query_criteria.append('community_id NOT IN :filtered_out_community_ids')
            post_query_parameters['filtered_out_community_ids'] = tuple(filtered_out_community_ids)
    else:
        if nsfw == 'Exclude' or nsfw == '':
            posts = posts.filter(Post.nsfw == False)
            post_query_criteria.append('p.nsfw = false')
        elif nsfw == 'Only':
            posts = posts.filter(Post.nsfw == True)
            post_query_criteria.append('p.nsfw = true')
        elif nsfw == 'Include':
            pass

    if minimum_upvotes:
        posts = posts.filter(Post.up_votes - Post.down_votes >= minimum_upvotes)
        post_query_criteria.append('score >= :minimum_upvotes')
        post_query_parameters['minimum_upvotes'] = minimum_upvotes
    
    if search_by_community and not ignore_sticky:
        posts = posts.order_by(desc(Post.sticky))
        sql_order_by.append('sticky DESC')
    
    if segregate_instance_stickies and not ignore_sticky:
        posts = posts.order_by(desc(Post.instance_sticky))
        sql_order_by.append('instance_sticky DESC')

    if sort == "Hot":
        posts = posts.order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
        sql_order_by.extend(['ranking DESC', 'posted_at DESC'])
    elif sort == "Top" or sort == "TopDay":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=1)).order_by(
            desc(Post.score))
        post_query_criteria.append('posted_at > :top_day_threshold')
        post_query_parameters['top_day_threshold'] = utcnow() - timedelta(days=1)
        sql_order_by.append('score DESC')
    elif sort == "TopHour":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=1)).order_by(
            desc(Post.score))
        post_query_criteria.append('posted_at > :top_hour_threshold')
        post_query_parameters['top_hour_threshold'] = utcnow() - timedelta(hours=1)
        sql_order_by.append('score DESC')
    elif sort == "TopSixHour":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=6)).order_by(
            desc(Post.score))
        post_query_criteria.append('posted_at > :top_six_hour_threshold')
        post_query_parameters['top_six_hour_threshold'] = utcnow() - timedelta(hours=6)
        sql_order_by.append('score DESC')
    elif sort == "TopTwelveHour":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=12)).order_by(
            desc(Post.score))
        post_query_criteria.append('posted_at > :top_twelve_hour_threshold')
        post_query_parameters['top_twelve_hour_threshold'] = utcnow() - timedelta(hours=12)
        sql_order_by.append('score DESC')
    elif sort == "TopWeek":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=7)).order_by(
            desc(Post.score))
        post_query_criteria.append('posted_at > :top_week_threshold')
        post_query_parameters['top_week_threshold'] = utcnow() - timedelta(days=7)
        sql_order_by.append('score DESC')
    elif sort == "TopMonth":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=28)).order_by(
            desc(Post.score))
        post_query_criteria.append('posted_at > :top_month_threshold')
        post_query_parameters['top_month_threshold'] = utcnow() - timedelta(days=28)
        sql_order_by.append('score DESC')
    elif sort == "TopThreeMonths":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=90)).order_by(desc(Post.score))
        post_query_criteria.append('posted_at > :top_three_months_threshold')
        post_query_parameters['top_three_months_threshold'] = utcnow() - timedelta(days=90)
        sql_order_by.append('score DESC')
    elif sort == "TopSixMonths":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=180)).order_by(desc(Post.score))
        post_query_criteria.append('posted_at > :top_six_months_threshold')
        post_query_parameters['top_six_months_threshold'] = utcnow() - timedelta(days=180)
        sql_order_by.append('score DESC')
    elif sort == "TopNineMonths":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=270)).order_by(desc(Post.score))
        post_query_criteria.append('posted_at > :top_nine_months_threshold')
        post_query_parameters['top_nine_months_threshold'] = utcnow() - timedelta(days=270)
        sql_order_by.append('score DESC')
    elif sort == "TopYear":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=365)).order_by(desc(Post.score))
        post_query_criteria.append('posted_at > :top_year_threshold')
        post_query_parameters['top_year_threshold'] = utcnow() - timedelta(days=365)
        sql_order_by.append('score DESC')
    elif sort == "TopAll":
        posts = posts.order_by(desc(Post.score))
        sql_order_by.append('score DESC')
    elif sort.startswith("Top"):
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=1)).order_by(desc(Post.score))
        post_query_criteria.append('posted_at > :top_default_threshold')
        post_query_parameters['top_default_threshold'] = utcnow() - timedelta(days=1)
        sql_order_by.append('score DESC')
    elif sort.startswith("New"):
        posts = posts.order_by(desc(Post.posted_at))
        sql_order_by.append('posted_at DESC')
    elif sort == "Scaled":
        posts = posts.filter(Post.ranking_scaled != None, Post.from_bot == False).order_by(desc(Post.ranking_scaled)).order_by(
            desc(Post.ranking)).order_by(desc(Post.posted_at))
        post_query_criteria.extend(['ranking_scaled IS NOT NULL', 'from_bot = false'])
        sql_order_by.extend(['ranking_scaled DESC', 'ranking DESC', 'posted_at DESC'])
    elif sort == "Active":
        posts = posts.filter(Post.last_active != None)
        posts = posts.order_by(desc(Post.last_active))
        post_query_criteria.append('p.last_active is not null')
        sql_order_by.append('p.last_active DESC')
    elif sort.startswith("Old"):
        posts = posts.order_by(asc(Post.posted_at))
        sql_order_by.append('posted_at ASC')
    elif sort == "Relevance":
        pass    # sorting by relevance is already done by posts = posts.search(query, sort=True)

    if use_faster_query:
        sql = """SELECT
                    p.id as post_id,
                    p.user_id, p.domain_id, p.instance_id,
                    p.status, p.deleted, p.deleted_by,
                    p.type, p.nsfw, p.nsfl, p.sticky, p.instance_sticky, p.ai_generated,
                    p.from_bot, p.created_at, p.posted_at, p.last_active,
                    p.up_votes, p.down_votes, p.ranking, p.ranking_scaled, p.score,
                    p.reply_count, p.language_id,
                    c.id as community_id, c.show_all, c.show_popular, c.instance_id as community_instance_id,
                    c.nsfw as community_nsfw, c.banned as community_banned,
                    c.private, c.ap_id as community_ap_id, c.name as community_name, c.ap_domain
                FROM post p
                JOIN community c ON c.id = p.community_id WHERE p.deleted = FALSE AND p.status > 0 AND c.banned = FALSE AND """
        sql += ' AND '.join(post_query_criteria) + ' ORDER BY ' + ', '.join(sql_order_by)
        sql += ' LIMIT 1000'
        post_ids = db.session.execute(text(sql), post_query_parameters).scalars().all()
        has_next_page = len(post_ids) > page + 1 * limit
        post_ids = paginate_post_ids(post_ids, page - 1, page_length=limit)
        posts = post_ids_to_models(post_ids, sort.lower()).all()
    else:
        posts = posts.paginate(page=page, per_page=limit, error_out=False)

    if user_id:

        banned_from = communities_banned_from_all_users()

        bookmarked_posts = list(db.session.execute(text(
            'SELECT post_id FROM "post_bookmark" WHERE user_id = :user_id'),
            {'user_id': user_id}).scalars())
        if bookmarked_posts is None:
            bookmarked_posts = []

        post_subscriptions = list(db.session.execute(text(
            'SELECT entity_id FROM "notification_subscription" WHERE type = :type and user_id = :user_id'),
            {'type': NOTIF_POST, 'user_id': user_id}).scalars())
        if post_subscriptions is None:
            post_subscriptions = []

        read_post_set = set(u_rp_ids)  # lookups ("in") on a set is O(1), tuples/lists are O(n). read_posts can be very large so this makes a difference.

        communities_moderating = moderating_communities_ids_all_users()
        communities_joined = joined_or_modding_communities(user.id)

        # collect all unique author IDs from posts, then only get user_notes relating to them
        author_ids = set()
        post_ids = []
        for post in posts if use_faster_query else posts.items:
            author_ids.add(post.user_id)
            post_ids.append(post.id)

        if author_ids:
            usernotes_query = db.session.execute(
                text('SELECT target_id, body FROM "user_note" WHERE user_id = :user_id AND target_id IN :author_ids'),
                {'user_id': user_id, 'author_ids': tuple(author_ids)}
            ).all()
            usernotes = {note[0]: note[1] for note in usernotes_query}
        else:
            usernotes = None
        
        user_votes = get_post_votes_for_posts(user_id, post_ids)
    else:
        bookmarked_posts = []
        banned_from = {}
        post_subscriptions = []
        read_post_set = set()
        communities_moderating = []
        communities_joined = []
        usernotes = {}
        user_votes = {}

    postlist = []
    for post in posts:
        # Get the pre-fetched vote, default to 0 if not found
        my_vote = user_votes.get(post.id, 0)
        
        postlist.append(post_view(post=post, variant=2, stub=False, user_id=user_id,
                                  communities_moderating=communities_moderating,
                                  banned_from=banned_from, bookmarked_posts=bookmarked_posts,
                                  post_subscriptions=post_subscriptions, read_posts=read_post_set,
                                  communities_joined=communities_joined, content_filters=content_filters,
                                  usernotes=usernotes, my_vote=my_vote))
    if use_faster_query:
        list_json = {
            "posts": postlist,
            "next_page": str(page + 1) if has_next_page else None
        }
    else:
        list_json = {
            "posts": postlist,
            "next_page": str(posts.next_num) if posts.next_num is not None else None
        }

    return list_json


def get_post_votes_for_posts(user_id, post_ids):
    """Pre-fetch user votes for a list of posts to avoid N+1 queries in post_view()"""
    if not user_id or not post_ids:
        return {}
    
    votes = db.session.execute(
        text('SELECT post_id, effect FROM "post_vote" WHERE user_id = :user_id AND post_id IN :post_ids'),
        {'user_id': user_id, 'post_ids': tuple(post_ids)}
    ).all()
    
    # Convert to a dictionary for O(1) lookup
    return {vote[0]: vote[1] for vote in votes}


def get_post_list2(auth, data, user_id=None, search_type='Posts') -> dict:
    type = data['type_'] if 'type_' in data else "All"
    sort = data['sort'] if 'sort' in data else "Hot"
    if 'page_cursor' in data:
        bookmark = data['page_cursor']
    elif 'page' in data:
        bookmark = data['page']
    else:
        bookmark = None
    limit = int(data['limit']) if 'limit' in data else 50
    liked_only = data['liked_only'] if 'liked_only' in data else False
    saved_only = data['saved_only'] if 'saved_only' in data else False
    ignore_sticky = data['ignore_sticky'] if 'ignore_sticky' in data else False
    minimum_upvotes = data['minimum_upvotes'] if 'minimum_upvotes' in data and data['minimum_upvotes'] else None
    nsfw = data['nsfw'] if 'nsfw' in data else ''

    query = data['q'] if 'q' in data else ''

    if auth:
        user_id = authorise_api_user(auth)

    # get the user to check if the user has hide_read posts set later down the function

    if user_id:
        user = User.query.get(user_id)
        g.user = user   # save the currently logged in user into g, to save loading it up again and again in post_view.

    # user_id: the logged in user
    # person_id: the author of the posts being requested

    community_id = int(data['community_id']) if 'community_id' in data else None
    feed_id = int(data['feed_id']) if 'feed_id' in data else None
    topic_id = int(data['topic_id']) if 'topic_id' in data else None
    community_name = data['community_name'] if 'community_name' in data else None
    person_id = int(data['person_id']) if 'person_id' in data else None

    if community_id or community_name:
        search_by_community = True
        # check private community access
        if community_id:
            community = Community.query.get(community_id)
        else:
            # parse community_name to get the community
            if '@' not in community_name:
                community_name_lookup = f"{community_name}@{current_app.config['SERVER_NAME']}"
            else:
                community_name_lookup = community_name
            name, ap_domain = community_name_lookup.split('@')
            community = Community.query.filter_by(name=name, ap_domain=ap_domain).first()

        if community and community.private:
            # community is private, check if user is a member
            if not user_id:
                raise Exception('Private community - authentication required')
            else:
                if not community.is_member(user):
                    raise Exception('Private community - membership required')
    else:
        search_by_community = False

    if user_id and user_id != person_id:
        blocked_person_ids = blocked_users(user_id)
        blocked_community_ids = blocked_communities(user_id)
        blocked_instance_ids = blocked_or_banned_instances(user_id)
        blocked_domain_ids = blocked_domains(user_id)
        private_community_ids = community_membership_private(user_id)
    else:
        blocked_person_ids = []
        blocked_community_ids = []
        blocked_instance_ids = []
        blocked_domain_ids = []
        private_community_ids = []

    content_filters = {}
    u_rp_ids = []

    # Post.user_id.not_in(blocked_person_ids)               # exclude posts by blocked users
    # Post.community_id.not_in(blocked_community_ids)       # exclude posts in blocked communities
    # Post.instance_id.not_in(blocked_instance_ids)         # exclude posts by users on blocked instances
    # Community.instance_id.not_in(blocked_instance_ids)    # exclude posts in communities on blocked instances

    # Depending on filters, determine whether instance stickies should be listed first or not
    segregate_instance_stickies = True

    if type == "Local":
        posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                  Post.user_id.not_in(blocked_person_ids),
                                  or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                  Post.community_id.not_in(blocked_community_ids)). \
            join(Community, Community.id == Post.community_id).filter_by(ap_id=None)
    elif type == "Popular":
        posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                  Post.user_id.not_in(blocked_person_ids),
                                  Post.community_id.not_in(blocked_community_ids),
                                  or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                  Post.instance_id.not_in(blocked_instance_ids)). \
            join(Community, Community.id == Post.community_id).filter(Community.show_popular == True, Post.score > 100,
                                                                      Community.instance_id.not_in(
                                                                          blocked_instance_ids))
    elif type == "Subscribed" and user_id is not None:
        posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                  Post.user_id.not_in(blocked_person_ids),
                                  Post.community_id.not_in(blocked_community_ids),
                                  or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                  Post.instance_id.not_in(blocked_instance_ids)). \
            join(CommunityMember, Post.community_id == CommunityMember.community_id).filter_by(is_banned=False,
                                                                                               user_id=user_id). \
            join(Community, Community.id == CommunityMember.community_id).filter(
            Community.instance_id.not_in(blocked_instance_ids))
    elif (type == "ModeratorView" or type == "Moderating") and user_id is not None:
        posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                  Post.user_id.not_in(blocked_person_ids),
                                  Post.community_id.not_in(blocked_community_ids),
                                  Post.instance_id.not_in(blocked_instance_ids)). \
            join(CommunityMember, Post.community_id == CommunityMember.community_id).filter_by(user_id=user_id,
                                                                                               is_moderator=True). \
            join(Community, Community.id == CommunityMember.community_id).filter(
            Community.instance_id.not_in(blocked_instance_ids))
    else:  # type == "All"
        if community_name:
            segregate_instance_stickies = False
            if not '@' in community_name:
                community_name = f"{community_name}@{current_app.config['SERVER_NAME']}"
            name, ap_domain = community_name.split('@')
            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.name == name,
                                                                          Community.ap_domain == ap_domain,
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))
            content_filters = user_filters_posts(user_id) if user_id else {}
        elif community_id:
            segregate_instance_stickies = False
            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.id == community_id,
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))
            content_filters = user_filters_posts(user_id) if user_id else {}
        elif feed_id:
            feed = Feed.query.get(feed_id)
            if feed.show_posts_in_children:  # include posts from child feeds
                feed_ids = get_all_child_feed_ids(feed)
            else:
                feed_ids = [feed.id]

            # for each feed get the community ids (FeedItem) in the feed
            # used for the posts searching
            feed_community_ids = []
            for fid in feed_ids:
                feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == fid).all()
                for item in feed_items:
                    feed_community_ids.append(item.community_id)

            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.id.in_(feed_community_ids),
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))
            content_filters = user_filters_posts(user_id) if user_id else {}
        elif feed_id:
            segregate_instance_stickies = False
            feed = Feed.query.get(feed_id)
            if feed.show_posts_in_children:  # include posts from child feeds
                feed_ids = get_all_child_feed_ids(feed)
            else:
                feed_ids = [feed.id]

            # for each feed get the community ids (FeedItem) in the feed
            # used for the posts searching
            feed_community_ids = []
            for fid in feed_ids:
                feed_items = FeedItem.query.join(Feed, FeedItem.feed_id == fid).all()
                for item in feed_items:
                    feed_community_ids.append(item.community_id)

            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.id.in_(feed_community_ids),
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))
            content_filters = user_filters_posts(user_id) if user_id else {}
        elif topic_id:
            segregate_instance_stickies = False
            topic = Topic.query.get(topic_id)
            if topic.show_posts_in_children:  # include posts from child feeds
                topic_ids = get_all_child_topic_ids(topic)
            else:
                topic_ids = [topic.id]

            # for each feed get the community ids (FeedItem) in the feed
            # used for the posts searching
            topic_community_ids = []
            for tid in topic_ids:
                communities = Community.query.filter(Community.topic_id == tid).all()
                for item in communities:
                    topic_community_ids.append(item.id)

            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.id.in_(topic_community_ids),
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))
            content_filters = user_filters_posts(user_id) if user_id else {}
        elif person_id:
            segregate_instance_stickies = False
            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids), Post.user_id == person_id). \
                join(Community, Community.id == Post.community_id).filter(
                Community.instance_id.not_in(blocked_instance_ids))
        else:
            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                      Post.user_id.not_in(blocked_person_ids),
                                      Post.community_id.not_in(blocked_community_ids),
                                      or_(Post.domain_id == None, Post.domain_id.not_in(blocked_domain_ids)),
                                      Post.instance_id.not_in(blocked_instance_ids)). \
                join(Community, Community.id == Post.community_id).filter(Community.show_all == True,
                                                                          Community.instance_id.not_in(
                                                                              blocked_instance_ids))
            content_filters = user_filters_home(user_id) if user_id else {}

    posts = posts.filter(or_(Community.private == False, Community.id.in_(private_community_ids)))

    if query:
        segregate_instance_stickies = False
        if search_type == 'Url':
            posts = posts.filter(Post.url.ilike(f"%{query}%"))
        else:
            posts = posts.search(query, sort=sort == 'Relevance').filter(Post.indexable == True)

    if user_id:
        if liked_only:
            segregate_instance_stickies = False
            upvoted_post_ids = recently_upvoted_posts(user_id)
            posts = posts.filter(Post.id.in_(upvoted_post_ids), Post.user_id != user_id)
        elif saved_only:
            segregate_instance_stickies = False
            bookmarked_post_ids = tuple(db.session.execute(text('SELECT post_id FROM "post_bookmark" WHERE user_id = :user_id'),
                                                     {"user_id": user_id}).scalars())
            posts = posts.filter(Post.id.in_(bookmarked_post_ids))
        else:
            u_rp_ids = tuple(db.session.execute(text('SELECT read_post_id FROM "read_posts" WHERE user_id = :user_id'),
                                          {"user_id": user_id}).scalars())
            if user.ignore_bots == 1:
                posts = posts.filter(Post.from_bot == False)
            if user.hide_nsfl == 1:
                posts = posts.filter(Post.nsfl == False)
            if nsfw == '' and user.hide_nsfw == 1:
                posts = posts.filter(Post.nsfw == False)
            else:
                if nsfw == 'Exclude':
                    posts = posts.filter(Post.nsfw == False)
                elif nsfw == 'Only':
                    posts = posts.filter(Post.nsfw == True)
                elif nsfw == 'Include':
                    pass
            if user.hide_gen_ai == 1:
                posts = posts.filter(Post.ai_generated == False)
            if user.hide_read_posts and not query:
                # Alias the read_posts table
                rp = read_posts.alias()

                # Filter posts that the user has NOT read, using ~exists
                posts = posts.filter(
                    ~exists().where(
                        and_(
                            rp.c.user_id == user_id,
                            rp.c.read_post_id == Post.id
                        )
                    )
                )

        filtered_out_community_ids = filtered_out_communities(user)
        if len(filtered_out_community_ids):
            posts = posts.filter(Post.community_id.not_in(filtered_out_community_ids))
    else:
        if nsfw == 'Exclude':
            posts = posts.filter(Post.nsfw == False)
        elif nsfw == 'Only':
            posts = posts.filter(Post.nsfw == True)
        elif nsfw == 'Include':
            pass

    if minimum_upvotes:
        posts = posts.filter(Post.up_votes - Post.down_votes >= minimum_upvotes)
    
    if search_by_community and not ignore_sticky:
        posts = posts.order_by(desc(Post.sticky))
    
    if segregate_instance_stickies and not ignore_sticky:
        posts = posts.order_by(desc(Post.instance_sticky))

    if sort == "Hot":
        posts = posts.order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort == "Top" or sort == "TopDay":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=1)).order_by(
            desc(Post.score))
    elif sort == "TopHour":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=1)).order_by(
            desc(Post.score))
    elif sort == "TopSixHour":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=6)).order_by(
            desc(Post.score))
    elif sort == "TopTwelveHour":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=12)).order_by(
            desc(Post.score))
    elif sort == "TopWeek":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=7)).order_by(
            desc(Post.score))
    elif sort == "TopMonth":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=28)).order_by(
            desc(Post.score))
    elif sort == "TopThreeMonths":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=90)).order_by(
            desc(Post.score))
    elif sort == "TopSixMonths":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=180)).order_by(
            desc(Post.score))
    elif sort == "TopNineMonths":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=270)).order_by(
            desc(Post.score))
    elif sort == "TopYear":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=365)).order_by(
            desc(Post.score))
    elif sort == "TopAll":
        posts = posts.order_by(desc(Post.score))
    elif sort.startswith("Top"):
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=1)).order_by(
            desc(Post.score))
    elif sort == "New":
        posts = posts.order_by(desc(Post.posted_at))
    elif sort == "Scaled":
        posts = posts.filter(Post.ranking_scaled != None, Post.from_bot == False).order_by(desc(Post.ranking_scaled)).order_by(
            desc(Post.ranking)).order_by(desc(Post.posted_at))
    elif sort == "Active":
        posts = posts.filter(Post.reply_count > 0)
        posts = posts.order_by(desc(Post.last_active))
    elif sort.startswith("Old"):
        posts = posts.order_by(asc(Post.posted_at))
    elif sort == "Relevance":
        pass    # sorting by relevance is already done by posts = posts.search(query, sort=True)

    # change when polls and events are supported
    posts = posts.filter(Post.type != POST_TYPE_POLL).filter(Post.type != POST_TYPE_EVENT)

    if query:
        if search_type == 'Url':
            posts = posts.filter(Post.url.ilike(f"%{query}%"))
        else:
            posts = posts.filter(Post.title.ilike(f"%{query}%"))

    if user_id:
        if liked_only:
            upvoted_post_ids = recently_upvoted_posts(user_id)
            posts = posts.filter(Post.id.in_(upvoted_post_ids), Post.user_id != user_id)
        elif saved_only:
            bookmarked_post_ids = tuple(db.session.execute(text('SELECT post_id FROM "post_bookmark" WHERE user_id = :user_id'),
                                                     {"user_id": user_id}).scalars())
            posts = posts.filter(Post.id.in_(bookmarked_post_ids))
        else:
            u_rp_ids = tuple(db.session.execute(text('SELECT read_post_id FROM "read_posts" WHERE user_id = :user_id'),
                                          {"user_id": user_id}).scalars())
            if user.hide_read_posts:
                # Alias the read_posts table
                rp = read_posts.alias()

                # Filter posts that the user has NOT read, using ~exists
                posts = posts.filter(
                    ~exists().where(
                        and_(
                            rp.c.user_id == user_id,
                            rp.c.read_post_id == Post.id
                        )
                    )
                )

        filtered_out_community_ids = filtered_out_communities(user)
        if len(filtered_out_community_ids):
            posts = posts.filter(Post.community_id.not_in(filtered_out_community_ids))

    if sort == "Hot":
        posts = posts.order_by(desc(Post.ranking), desc(Post.posted_at), desc(Post.id))
    elif sort == "Top" or sort == "TopDay":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=1)).order_by(
            desc(Post.score, desc(Post.id)))
    elif sort == "TopHour":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=1)).order_by(
            desc(Post.score, desc(Post.id)))
    elif sort == "TopSixHour":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=6)).order_by(
            desc(Post.score, desc(Post.id)))
    elif sort == "TopTwelveHour":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(hours=12)).order_by(
            desc(Post.score, desc(Post.id)))
    elif sort == "TopWeek":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=7)).order_by(
            desc(Post.score, desc(Post.id)))
    elif sort == "TopMonth":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=28)).order_by(
            desc(Post.score, desc(Post.id)))
    elif sort == "TopThreeMonths":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=90)).order_by(
            desc(Post.score, desc(Post.id)))
    elif sort == "TopSixMonths":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=180)).order_by(
            desc(Post.score, desc(Post.id)))
    elif sort == "TopNineMonths":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=270)).order_by(
            desc(Post.score, desc(Post.id)))
    elif sort == "TopYear":
        posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=365)).order_by(
            desc(Post.score, desc(Post.id)))
    elif sort == "TopAll":
        posts = posts.order_by(desc(Post.score, desc(Post.id)))
    elif sort == "New":
        posts = posts.order_by(desc(Post.posted_at), desc(Post.id))
    elif sort == "Scaled":
        posts = posts.filter(Post.ranking_scaled != None).order_by(desc(Post.ranking_scaled),
            desc(Post.ranking), desc(Post.posted_at), desc(Post.id))
    elif sort == "Active":
        posts = posts.filter(Post.reply_count > 0)
        posts = posts.order_by(desc(Post.last_active), desc(Post.id))

    page_obj = get_page(posts, per_page=limit, page=bookmark)
    posts = SqlKeysetPagination(page_obj)

    if user_id:

        banned_from = communities_banned_from_all_users()

        bookmarked_posts = list(db.session.execute(text(
            'SELECT post_id FROM "post_bookmark" WHERE user_id = :user_id'),
            {'user_id': user_id}).scalars())
        if bookmarked_posts is None:
            bookmarked_posts = []

        post_subscriptions = list(db.session.execute(text(
            'SELECT entity_id FROM "notification_subscription" WHERE type = :type and user_id = :user_id'),
            {'type': NOTIF_POST, 'user_id': user_id}).scalars())
        if post_subscriptions is None:
            post_subscriptions = []

        read_post_set = set(u_rp_ids)  # lookups ("in") on a set is O(1), tuples/lists are O(n). read_posts can be very large so this makes a difference.

        communities_moderating = moderating_communities_ids_all_users()
        communities_joined = joined_or_modding_communities(user.id)
        
        # Pre-fetch user votes to avoid N+1 queries in post_view()
        # posts.items contains Post objects directly
        post_objects = list(posts.items)
        post_ids = [post.id for post in post_objects]
        user_votes = get_post_votes_for_posts(user_id, post_ids)
    else:
        bookmarked_posts = []
        banned_from = {}
        post_subscriptions = []
        read_post_set = set()
        communities_moderating = []
        communities_joined = []
        user_votes = {}

    postlist = []
    for post in posts.items:
        # Get the pre-fetched vote, default to 0 if not found
        my_vote = user_votes.get(post.id, 0)
        
        postlist.append(post_view(post=post, variant=2, stub=False, user_id=user_id,
                                  communities_moderating=communities_moderating,
                                  banned_from=banned_from, bookmarked_posts=bookmarked_posts,
                                  post_subscriptions=post_subscriptions, read_posts=read_post_set,
                                  communities_joined=communities_joined, content_filters=content_filters,
                                  my_vote=my_vote))

    list_json = {
        "posts": postlist,
        "next_page": posts.next_bookmark if posts.has_next else None
    }

    return list_json


def get_post(auth, data):
    if not data or 'id' not in data:
        raise Exception('missing parameters for post')

    id = int(data['id'])

    user_id = authorise_api_user(auth) if auth else None

    post_json = post_view(post=id, variant=3, user_id=user_id)
    return post_json


def get_post_replies(auth, data):
    sort = data['sort'] if 'sort' in data else 'New'
    max_depth = int(data['max_depth']) if 'max_depth' in data else None
    page_cursor = data['page'] if 'page' in data else None  # Expects reply ID or None for first page
    limit = int(data['limit']) if 'limit' in data else 20
    post_id = data['post_id'] if 'post_id' in data else None
    parent_id = data['parent_id'] if 'parent_id' in data else None

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    if auth:
        user_details = authorise_api_user(auth, return_type='dict')
        user_id = user_details['id']
        # get_comment_branch() is borrowed from the web-ui so needs the full User
        user = User.query.filter_by(id=user_id).one()
    else:
        user_details = {}
        user_id = None
        user = None

    if parent_id:
        parent = PostReply.query.get(parent_id)
        if post_id is None:
            post_id = parent.post_id
        post = Post.query.get(post_id)
        replies = get_comment_branch(post, parent.id, sort.lower(), user)
    else:
        post = Post.query.get(post_id)
        replies = post_replies(post, sort.lower(), user)

    is_user_banned_from_community = post.community_id in user_details['user_ban_community_ids'] if user_details else False
    is_user_following_community = post.community_id in user_details['followed_community_ids'] if user_details else False
    is_user_moderator = post.community_id in user_details['moderated_community_ids'] if user_details else False

    # Apply max_depth filter to the nested reply tree
    def filter_max_depth(reply_tree, current_depth=0, parent_depth=0):
        """Filter nested reply tree by max_depth"""
        if max_depth is None:
            return reply_tree

        filtered_tree = []
        for item in reply_tree:
            comment = item['comment']
            # Calculate depth relative to parent_id if specified, otherwise use absolute depth
            effective_depth = current_depth if parent_id else comment.depth

            if effective_depth <= max_depth:
                filtered_item = {
                    'comment': comment,
                    'replies': filter_max_depth(item['replies'], current_depth + 1, parent_depth)
                }
                filtered_tree.append(filtered_item)

        return filtered_tree

    # Apply max_depth filter
    if max_depth:
        replies = filter_max_depth(replies)

    # Apply cursor-based pagination
    def paginate_with_cursor(reply_tree, cursor_id, limit):
        """Paginate using reply ID cursor while keeping complete branches together"""
        if not reply_tree:
            return [], None

        # Find starting position based on cursor
        start_index = 0
        if cursor_id:
            try:
                cursor_id = int(cursor_id)
                # Find the top-level branch that matches this cursor
                for i, branch in enumerate(reply_tree):
                    if branch['comment'].id == cursor_id:
                        start_index = i  # Start from the cursor branch itself
                        break
                else:
                    # Cursor not found, return empty (past end)
                    return [], None
            except (ValueError, TypeError):
                # Invalid cursor, start from beginning
                start_index = 0

        # Count flattened items in branches to respect limit
        def get_branch_size(branch):
            """Count total items in a branch (including all descendants)"""
            count = 1  # The branch itself
            for child in branch['replies']:
                count += get_branch_size(child)
            return count

        # Collect branches starting from cursor position
        included_branches = []
        total_items = 0

        for i in range(start_index, len(reply_tree)):
            branch = reply_tree[i]
            branch_size = get_branch_size(branch)

            # Always include at least one branch, even if it exceeds limit
            if not included_branches:
                included_branches.append(branch)
                total_items += branch_size
            elif total_items + branch_size <= limit or total_items == 0:
                # Include this branch if it fits within limit
                included_branches.append(branch)
                total_items += branch_size
            else:
                # Would exceed limit, stop here
                break

        # Determine next cursor (ID of next top-level branch after those included)
        next_cursor = None
        if included_branches:
            last_included_index = start_index + len(included_branches) - 1
            if last_included_index + 1 < len(reply_tree):
                next_cursor = reply_tree[last_included_index + 1]['comment'].id

        return included_branches, next_cursor

    # Apply pagination
    replies, next_cursor = paginate_with_cursor(replies, page_cursor, limit)

    inner_post_view = None
    inner_community_view = None

    # Precompute once for all replies in this post
    community_moderator_ids = {m.user_id for m in post.community.moderators()}
    all_ban_data = communities_banned_from_all_users()

    # Process nested reply tree while preserving structure
    def process_nested_replies(reply_tree, is_top_level=True):
        """Process nested reply tree while preserving nested structure"""
        nonlocal inner_post_view, inner_community_view
        nonlocal is_user_banned_from_community, is_user_following_community, is_user_moderator
        processed_replies = []

        for item in reply_tree:
            reply = item['comment']
            is_reply_bookmarked = reply.id in user_details['bookmarked_reply_ids'] if user_details else None
            is_creator_blocked = reply.user_id in user_details['blocked_creator_ids'] if user_details else False
            vote_effect = 0
            if user_details and in_sorted_list(user_details['upvoted_reply_ids'], reply.id):
                vote_effect = 1
            elif user_details and in_sorted_list(user_details['downvoted_reply_ids'], reply.id):
                vote_effect = -1
            is_reply_subscribed = reply.id in user_details['subscribed_reply_ids'] if user_details else None
            is_creator_banned = post.community_id in all_ban_data.get(reply.user_id, [])
            is_creator_mod = reply.user_id in community_moderator_ids

            view = reply_view(reply=reply, variant=3, user_id=user_id,
                              is_user_banned_from_community=is_user_banned_from_community,
                              is_user_following_community=is_user_following_community,
                              is_reply_bookmarked=is_reply_bookmarked,
                              is_creator_blocked=is_creator_blocked,
                              vote_effect=vote_effect,
                              is_reply_subscribed=is_reply_subscribed,
                              is_user_moderator=is_user_moderator,
                              is_creator_banned_from_community=is_creator_banned,
                              is_creator_moderator=is_creator_mod,
                              add_post_in_view=False,
                              add_community_in_view=False)

            # Only include post and community info on top-level replies
            if is_top_level:
                if not inner_post_view:
                    inner_post_view = post_view(post, variant=1)
                view['post'] = inner_post_view
                if not inner_community_view:
                    inner_community_view = community_view(post.community, variant=1, stub=True)
                view['community'] = inner_community_view

            # Process nested replies
            if item['replies']:
                view['replies'] = process_nested_replies(item['replies'], is_top_level=False)
            else:
                view['replies'] = []

            processed_replies.append(view)

        return processed_replies

    replylist = process_nested_replies(replies)

    list_json = {
        "comments": replylist,
        "next_page": str(next_cursor) if next_cursor is not None else None
    }

    return list_json


def post_post_like(auth, data):
    user = authorise_api_user(auth, return_type="model")

    if not user:
        raise Exception("incorrect login")

    post_id = data['post_id']
    score = data['score']
    private = data['private'] if 'private' in data else bool(user.vote_privately)
    emoji = data['emoji'] if 'emoji' in data else None
    if score == 1:
        direction = 'upvote'
    elif score == -1:
        direction = 'downvote'
    else:
        score = 0
        direction = 'reversal'

    user_id = vote_for_post(post_id, direction, not private, emoji, SRC_API, auth)
    post_json = post_view(post=post_id, variant=4, user_id=user_id, my_vote=score)
    return post_json


def put_post_save(auth, data):
    post_id = data['post_id']
    save = data['save']

    user_id = bookmark_post(post_id, SRC_API, auth) if save else remove_bookmark_post(post_id, SRC_API, auth)
    post_json = post_view(post=post_id, variant=4, user_id=user_id)
    return post_json


def put_post_subscribe(auth, data):
    post_id = data['post_id']
    subscribe = data['subscribe']

    user_id = subscribe_post(post_id, subscribe, SRC_API, auth)
    post_json = post_view(post=post_id, variant=4, user_id=user_id)
    return post_json


def post_post(auth, data):
    title = data['title']
    community_id = data['community_id']
    body = data['body'] if 'body' in data else ''
    url = data['url'] if 'url' in data else None
    nsfw = data['nsfw'] if 'nsfw' in data else False
    ai_generated = data['ai_generated'] if 'ai_generated' in data else False
    alt_text = data['alt_text'] if 'alt_text' in data else title
    language_id = data['language_id'] if 'language_id' in data else site_language_id()
    if language_id < 2:
        language_id = site_language_id()
    
    user_id = authorise_api_user(auth)

    # Determine post type based on data provided
    if 'event' in data and data['event']:
        type = POST_TYPE_EVENT
    elif 'poll' in data and data['poll']:
        type = POST_TYPE_POLL
        if 'end_poll' not in data['poll']:
            # Default to ending the poll in three days
            end_poll = utcnow() + timedelta(days=3)
            data['poll']['end_poll'] = end_poll.isoformat(timespec="microseconds")
    elif url:
        type = POST_TYPE_LINK
    else:
        type = POST_TYPE_ARTICLE

    input = {'title': title, 'body': body, 'url': url, 'nsfw': nsfw, 'language_id': language_id, 'notify_author': True,
             'ai_generated': ai_generated, 'image_alt_text': alt_text}

    # Add event data if present
    if 'event' in data and data['event']:
        input['event'] = data['event']

    # Add poll data if present
    if 'poll' in data and data['poll']:
        input['poll'] = data['poll']

    community = Community.query.filter_by(id=community_id).one()

    # Fire hook for plugins
    post_data = {
        "title": title,
        "content": body,
        "community": community.name,
        "community_id": community.id,
        "post_type": type,
        "user_id": user_id,
    }
    plugins.fire_hook('before_post_create', post_data)

    user_id, post = make_post(input, community, type, SRC_API, auth)

    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def put_post(auth, data):
    post_id = data['post_id']
    post = Post.query.get(post_id)

    title = data['title'] if 'title' in data else post.title
    body = data['body'] if 'body' in data else post.body
    url = data['url'] if 'url' in data else post.url
    nsfw = data['nsfw'] if 'nsfw' in data else post.nsfw
    ai_generated = data['ai_generated'] if 'ai_generated' in data else post.ai_generated
    language_id = data['language_id'] if 'language_id' in data else post.language_id
    tags = data['tags'] if 'tags' in data else tags_to_string(post) or ''
    flair = data['flair'] if 'flair' in data else flair_to_string(post) or ''
    if language_id < 2:
        language_id = site_language_id()
    if 'alt_text' in data:
        alt_text = data['alt_text']
    elif post.image and post.image.alt_text:
        alt_text = post.image.alt_text
    else:
        alt_text = ''

    # Determine post type - keep existing type unless explicitly changed
    type = post.type
    # Only infer type change if URL is being changed
    if 'url' in data:
        if url:
            type = POST_TYPE_LINK
        else:
            type = POST_TYPE_ARTICLE

    input = {'title': title, 'body': body, 'url': url, 'nsfw': nsfw, 'language_id': language_id, 'notify_author': True,
             'tags': tags, 'flair': flair, 'ai_generated': ai_generated, 'image_alt_text': alt_text}

    # Add event data if present
    if 'event' in data and data['event']:
        input['event'] = data['event']

    # Add poll data if present
    if 'poll' in data and data['poll']:
        input['poll'] = data['poll']

    user_id, post = edit_post(input, post, type, SRC_API, auth=auth)

    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def post_post_delete(auth, data):
    post_id = data['post_id']
    deleted = data['deleted']

    if deleted:
        user_id, post = delete_post(post_id, True, SRC_API, auth)
    else:
        user_id, post = restore_post(post_id, SRC_API, auth)

    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def post_post_report(auth, data):
    post_id = data['post_id']
    reason = data['reason']
    description = data['description'] if 'description' in data else ''
    report_remote = data['report_remote'] if 'report_remote' in data else True
    input = {'reason': reason, 'description': description, 'report_remote': report_remote}

    post = Post.query.get(post_id)
    user_id, report = report_post(post, input, SRC_API, auth)

    post_json = post_report_view(report=report, post_id=post_id, user_id=user_id)
    return post_json


def get_post_report_list(auth, data):
    user = authorise_api_user(auth, return_type="model")

    if not user:
        raise Exception("incorrect login")

    post_id = data['post_id'] if 'post_id' in data else None
    community_id = data['community_id'] if 'community_id' in data else None
    limit = data['limit'] if 'limit' in data else 20
    page = data['page'] if 'page' in data else 1
    unresolved_only = data['unresolved_only'] if 'unresolved_only' in data else True

    if post_id:
        # Just get reports for a single post
        post = Post.query.get(post_id)
        mods = post.community.moderators()
        mod_ids = [mod.user_id for mod in mods]

        if user.id in mod_ids or user_access('administer all communities', user.id):
            reports = Report.query.filter(Report.suspect_post_id == post_id,
                                          Report.suspect_post_reply_id == None)
        else:
            raise Exception('incorrect login')
    elif community_id:
        # Just get reports for a single community
        community = Community.query.get(community_id)
        mods = community.moderators()
        mod_ids = [mod.user_id for mod in mods]

        if user.id in mod_ids or user_access("administer all communities", user.id):
            reports = Report.query.filter(Report.in_community_id == community_id,
                                          Report.suspect_post_id != None,
                                          Report.suspect_post_reply_id == None)
        else:
            raise Exception('incorrect login')
    else:
        # Don't restrict reports to single post or community
        if user_access('administer all communities', user.id):
            # Privileged user, don't filter by community
            reports = Report.query.filter(Report.suspect_post_id != None,
                                          Report.suspect_post_reply_id == None)
        else:
            modded_comm_ids = moderating_communities_ids(user.id)
            reports = Report.query.filter(Report.suspect_post_id != None,
                                          Report.in_community_id.in_(modded_comm_ids),
                                          Report.suspect_post_reply_id == None)
    
    if unresolved_only:
        reports = reports.filter(Report.status < REPORT_STATE_RESOLVED)
    
    reports = reports.paginate(page=page, per_page=limit, error_out=False)

    report_list = []
    for report in reports.items:
        report_list.append(post_report_view(report=report,
                                             post_id=report.suspect_post_id,
                                             user_id=user.id,
                                             variant=2))
    
    reply_json = dict()
    reply_json['post_reports'] = report_list
    reply_json['next_page'] = str(reports.next_num) if reports.next_num else None

    return reply_json


def put_post_report_resolve(auth, data):
    report_id = data['report_id']
    resolved = data['resolved']

    user = authorise_api_user(auth, return_type="model")

    if not user:
        raise Exception("incorrect login")
    
    report = Report.query.get(report_id)
    
    if not report.suspect_post_id and report.suspect_post_reply_id:
        raise Exception("invalid target of resolution")
    
    community = Community.query.get(report.in_community_id)
    mods = community.moderators()
    mod_ids = [mod.user_id for mod in mods]

    if user.id in mod_ids or user_access('administer all communities', user.id):
        if resolved:
            report.status = REPORT_STATE_RESOLVED
        else:
            report.status = REPORT_STATE_NEW

        db.session.commit()
    else:
        raise Exception("incorrect login")
    
    reply_json = post_report_view(report=report, post_id=report.suspect_post_id, user_id=user.id)
    return reply_json


def post_post_lock(auth, data):
    post_id = data['post_id']
    locked = data['locked']

    user_id, post = lock_post(post_id, locked, SRC_API, auth)

    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def post_post_hide(auth, data):
    post_id = data['post_id']
    hidden = data['hidden']

    user_id, post = hide_post(post_id, hidden, SRC_API, auth)

    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def post_post_feature(auth, data):
    post_id = data['post_id']
    featured = data['featured']
    feature_type = data['feature_type'] if 'feature_type' in data else 'Community'

    if feature_type == "Community":
        user_id, post = sticky_post(post_id, featured, SRC_API, auth)
    elif feature_type == "Local":
        user = authorise_api_user(auth, 'model')
        user_id = user.id
        post = Post.query.get(post_id)
        
        if user.is_admin():
            post.instance_sticky = featured

            cache.delete_memoized(instance_sticky_posts)
            cache.delete_memoized(instance_sticky_post_ids)
            db.session.commit()
        else:
            raise Exception('Only admin users can feature a post locally')

    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def post_post_remove(auth, data):
    post_id = data['post_id']
    removed = data['removed']

    if removed:
        reason = data['reason'] if 'reason' in data else 'Removed by mod'
        user_id, post = mod_remove_post(post_id, reason, SRC_API, auth)
    else:
        reason = data['reason'] if 'reason' in data else 'Restored by mod'
        user_id, post = mod_restore_post(post_id, reason, SRC_API, auth)

    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def post_post_mark_as_read(auth, data):
    if 'post_id' not in data and 'post_ids' not in data:
        raise Exception('post_id or post_ids required')

    user_id = authorise_api_user(auth)
    try:
        if 'post_id' in data:
            mark_post_read([data['post_id']], data['read'], user_id)
        elif 'post_ids' in data:
            mark_post_read(data['post_ids'], data['read'], user_id)
    except IntegrityError:
        return {"success": False}
    return {"success": True}


def get_post_like_list(auth, data):
    post_id = data['post_id']
    page = data['page'] if 'page' in data else 1
    limit = data['limit'] if 'limit' in data else 50

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    user = authorise_api_user(auth, return_type='model')
    post = Post.query.get(post_id)

    if post.community.is_moderator(user) or user.is_admin() or user.is_staff():
        banned_from_site_user_ids = list(db.session.execute(text('SELECT id FROM "user" WHERE banned = true')).scalars())
        banned_from_community_user_ids = list(db.session.execute(text
            ('SELECT user_id from "community_ban" WHERE community_id = :community_id'), {"community_id": post.community_id}).scalars())
        likes = PostVote.query.filter(PostVote.post_id == post_id, PostVote.effect != 0).order_by(PostVote.effect).order_by(
                  PostVote.created_at).paginate(page=page, per_page=limit, error_out=False)
        post_likes = []
        for like in likes:
            post_likes.append({
                'score': like.effect,
                'creator_banned_from_community': like.user_id in banned_from_community_user_ids,
                'creator_banned': like.user_id in banned_from_site_user_ids,
                'creator': user_view(user=like.user_id, variant=1, stub=True, user_id=user.id)
            })
        response_json = {
            'next_page': str(likes.next_num) if likes.next_num is not None else None,
            'post_likes': post_likes
        }
        return response_json
    else:
        raise Exception('Not a moderator')


def put_post_set_flair(auth, data):
    post_id = data['post_id']
    flair_list = data['flair_id_list'] if 'flair_id_list' in data else []

    post = Post.query.get(post_id)
    user = authorise_api_user(auth, return_type='model')
    
    if post.community.is_moderator(user) or user.is_admin_or_staff() or post.user_id == user.id:
        # Start by clearing the existing flair
        post.flair = []

        if flair_list:
            comm_flair = CommunityFlair.query.filter_by(community_id=post.community_id).all()
            flair_objs = [CommunityFlair.query.get(flair_id) for flair_id in flair_list]

            for flair in flair_objs:
                if flair in comm_flair:
                    # Flair from correct community, add it
                    post.flair.append(flair)

        db.session.commit()

        if post.status == POST_STATUS_PUBLISHED:
            task_selector('edit_post', post_id=post.id)
        
        return post_view(post=post, variant=2, stub=False, user_id=user.id)
    else:
        raise Exception("Insufficient permissions")


def post_poll_vote(auth, data):
    post_id = data['post_id']
    choice_id = data['choice_id']

    user_id = authorise_api_user(auth)

    vote_for_poll(post_id, choice_id, SRC_API, auth=auth)

    return {"post_view": post_view(post=post_id, variant=2, stub=False, user_id=user_id)}
