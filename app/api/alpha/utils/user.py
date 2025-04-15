import re
from app import db
from app.api.alpha.views import user_view, reply_view, post_view, community_view
from app.utils import authorise_api_user
from app.api.alpha.utils.post import get_post_list
from app.api.alpha.utils.reply import get_reply_list
from app.api.alpha.utils.validators import required, integer_expected, boolean_expected
from app.models import Conversation, ChatMessage, Notification, PostReply, User, Post, Community
from app.shared.user import block_another_user, unblock_another_user, subscribe_user
from app.constants import *

from sqlalchemy import text, desc


def get_user(auth, data):
    if not data or ('person_id' not in data and 'username' not in data):
        raise Exception('missing_parameters')

    # user_id = logged in user, person_id = person who's posts, comments etc are being fetched
    # when 'username' is requested, user_id and person_id are the same

    person_id = None
    if 'person_id' in data:
        person_id = int(data['person_id'])

    user_id = None
    if auth:
        user_id = authorise_api_user(auth)
        if 'username' in data:
            data['person_id'] = user_id
            person_id = int(user_id)
        auth = None                 # avoid authenticating user again in get_post_list and get_reply_list

    # bit unusual. have to help construct the json here rather than in views, to avoid circular dependencies
    post_list = get_post_list(auth, data, user_id)
    reply_list = get_reply_list(auth, data, user_id)

    user_json = user_view(user=person_id, variant=3, user_id=user_id)
    user_json['posts'] = post_list['posts']
    user_json['comments'] = reply_list['comments']
    return user_json


def get_user_list(auth, data):
    # only support 'api/alpha/search?q&type_=Users&sort=Top&listing_type=Local&page=1&limit=15' for now
    # (enough for instance view)

    type = data['type_'] if data and 'type_' in data else "All"
    page = int(data['page']) if data and 'page' in data else 1
    limit = int(data['limit']) if data and 'limit' in data else 10

    query = data['q'] if data and 'q' in data else ''

    user_id = authorise_api_user(auth) if auth else None

    if type == 'Local':
        users = User.query.filter_by(instance_id=1, deleted=False).order_by(User.id)
    else:
        users = User.query.filter(User.instance_id != 1, User.deleted == False).order_by(desc(User.id))

    if query:
        if '@' in query:
            users = users.filter(User.ap_id.ilike(f"%{query}%"))
        else:
            users = users.filter(User.user_name.ilike(f"%{query}%"))

    users = users.paginate(page=page, per_page=limit, error_out=False)

    user_list = []
    for user in users:
        user_list.append(user_view(user, variant=2, stub=True, user_id=user_id))
    list_json = {
        "users": user_list
    }

    return list_json


def post_user_block(auth, data):
    required(['person_id', 'block'], data)
    integer_expected(['post_id'], data)
    boolean_expected(['block'], data)

    person_id = data['person_id']
    block = data['block']

    user_id = block_another_user(person_id, SRC_API, auth) if block else unblock_another_user(person_id, SRC_API, auth)
    user_json = user_view(user=person_id, variant=4, user_id=user_id)
    return user_json


def get_user_unread_count(auth):
    user = authorise_api_user(auth, return_type='model')
    unread_replies = unread_messages = 0
    unread_notifications = user.unread_notifications
    if unread_notifications > 0:
        unread_replies = db.session.execute(text("SELECT COUNT(id) as c FROM notification WHERE user_id = :user_id AND read = false AND url LIKE '%comment%'"), {'user_id': user.id}).scalar()
        unread_messages = db.session.execute(text("SELECT COUNT(id) as c FROM chat_message WHERE recipient_id = :user_id AND read = false"), {'user_id': user.id}).scalar()

    # "other" is things like reports and activity alerts that this endpoint isn't really intended to support
    # replies and mentions are merged together in 'replies' as that's what get_user_replies() currently expects

    unread_count = {
        "replies": unread_replies,
        "mentions": 0,
        "private_messages": unread_messages,
        "other": unread_notifications - unread_replies - unread_messages
    }

    return unread_count


def get_user_replies(auth, data):
    page = int(data['page']) if data and 'page' in data else 1
    limit = int(data['limit']) if data and 'limit' in data else 10

    user_id = authorise_api_user(auth)

    unread_urls = db.session.execute(text("select url from notification where user_id = :user_id and read = false and url ilike '%comment%'"), {'user_id': user_id}).scalars()
    unread_ids = []
    for url in unread_urls:
        if '#comment_' in url:                                  # reply format
            unread_ids.append(url.rpartition('_')[-1])
        elif '/comment/' in url:                                # mention format
            unread_ids.append(url.rpartition('/')[-1])

    replies = PostReply.query.filter(PostReply.id.in_(unread_ids)).order_by(desc(PostReply.posted_at)).paginate(page=page, per_page=limit, error_out=False)

    reply_list = []
    for reply in replies:
        reply_list.append(reply_view(reply=reply, variant=5, user_id=user_id))
    list_json = {
        "replies": reply_list
    }

    return list_json


def post_user_mark_all_as_read(auth):
    user = authorise_api_user(auth, return_type='model')

    notifications = Notification.query.filter_by(user_id=user.id, read=False)
    for notification in notifications:
        notification.read = True

    user.unread_notifications = 0

    conversations = Conversation.query.filter_by(read=False).join(ChatMessage, ChatMessage.conversation_id == Conversation.id).filter_by(recipient_id=user.id)
    for conversation in conversations:
        conversation.read = True

    chat_messages = ChatMessage.query.filter_by(recipient_id=user.id)
    for chat_message in chat_messages:
        chat_message.read = True

    db.session.commit()

    return {'replies': []}


def put_user_subscribe(auth, data):
    required(['person_id', 'subscribe'], data)
    integer_expected(['person_id'], data)
    boolean_expected(['subscribe'], data)

    person_id = data['person_id']
    subscribe = data['subscribe']

    user_id = subscribe_user(person_id, subscribe, SRC_API, auth)
    user_json = user_view(user=person_id, variant=5, user_id=user_id)
    return user_json


def put_user_save_user_settings(auth, data):
    user = authorise_api_user(auth, return_type='model')
    show_nfsw = data['show_nsfw'] if 'show_nsfw' in data else None
    show_read_posts = data['show_read_posts'] if 'show_read_posts' in data else None
    about = data['bio'] if 'bio' in data else None

    # english is fun, so lets do the reversing and update the user settings
    if show_nfsw == True:
        user.hide_nsfw = 0
    elif show_nfsw == False:
        user.hide_nsfw = 1

    if show_read_posts == True:
        user.hide_read_posts = False
    elif show_read_posts == False:
        user.hide_read_posts = True

    if isinstance(about, str):
        from app.utils import markdown_to_html
        user.about = about
        user.about_html = markdown_to_html(about)

    # save the change to the db
    db.session.commit()

    user_json = {"my_user": user_view(user=user, variant=6)}
    return user_json


def get_user_notifications(auth, data):
    pass


def get_user_notifs_no_auth(data):
    # get the user from data.user_id
    user = User.query.get(data['user_id'])
    # get the status from data.status_request
    status = data['status_request']

    # it could be optimized better, but lets just get the info one by one for now

    # NOTIF_USER
    all_notif_user_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_USER).count()
    new_notif_user_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_USER).filter_by(read=False).count()

    # NOTIF_COMMUNITY
    all_notif_community_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_COMMUNITY).count()
    new_notif_community_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_COMMUNITY).filter_by(read=False).count()

    # NOTIF_TOPIC
    all_notif_topic_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_TOPIC).count()
    new_notif_topic_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_TOPIC).filter_by(read=False).count()

    # NOTIF_POST
    all_notif_post_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_POST).count()
    new_notif_post_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_POST).filter_by(read=False).count()

    # NOTIF_REPLY
    all_notif_reply_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_REPLY).count()
    new_notif_reply_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_REPLY).filter_by(read=False).count()

    # NOTIF_MENTION
    all_notif_mention_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_MENTION).count()
    new_notif_mention_count = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_MENTION).filter_by(read=False).count()


    # get counts for new/read/all
    counts = {}
    counts['total_notifications'] = all_notif_user_count + all_notif_community_count + \
        all_notif_topic_count + all_notif_post_count + all_notif_reply_count + all_notif_mention_count
    counts['new_notifications'] = new_notif_user_count + new_notif_community_count + \
        new_notif_topic_count + new_notif_post_count + new_notif_reply_count + new_notif_mention_count

    counts['all_notif_user_count'] = all_notif_user_count
    counts['all_notif_community_count'] = all_notif_community_count
    counts['all_notif_topic_count'] = all_notif_topic_count
    counts['all_notif_post_count'] = all_notif_post_count
    counts['all_notif_reply_count'] = all_notif_reply_count
    counts['all_notif_mention_count'] = all_notif_mention_count

    counts['new_notif_user_count'] = new_notif_user_count
    counts['new_notif_community_count'] = new_notif_community_count
    counts['new_notif_topic_count'] = new_notif_topic_count
    counts['new_notif_post_count'] = new_notif_post_count
    counts['new_notif_reply_count'] = new_notif_reply_count
    counts['new_notif_mention_count'] = new_notif_mention_count

    counts['read_notifications'] = counts['total_notifications'] - counts['new_notifications']

    # items dict
    items = []

    # new
    if status == 'new':
        # get all the unread NOTIF_USER
        # for each one
        #  - get the author info
        #  - get the postid from re.findall and then get the post info
        new_notif_user_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_USER).filter_by(read=False).all()
        for item in new_notif_user_items:
            author = User.query.get(item.author_id)
            post_id = re.findall(r'\d+', item.url)[0]
            post = Post.query.get(post_id)
            notification_json = {}
            notification_json['notif_type'] = NOTIF_USER
            notification_json['notif_type_subtype'] = 'new_post_from_followed_user'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            items.append(notification_json)

        # get all the unread NOTIF_COMMUNITY
        # for each one
        #  - get the author info
        #  - get the postid from re.findall and then get the post info
        #  - get the community info from the post.community_id
        new_notif_community_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_COMMUNITY).filter_by(read=False).all()
        for item in new_notif_community_items:
            author = User.query.get(item.author_id)
            post_id = re.findall(r'\d+', item.url)[0]
            post = Post.query.get(post_id)
            community = Community.query.get(post.community_id)
            notification_json = {}
            notification_json['notif_type'] = NOTIF_COMMUNITY
            notification_json['notif_type_subtype'] = 'new_post_in_followed_community'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            notification_json['community'] = community_view(community, variant=2)
            items.append(notification_json)

        # get all the unread NOTIF_TOPIC
        # for each one
        #  - get the author info
        #  - get the postid from re.findall and then get the post info
        new_notif_topic_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_TOPIC).filter_by(read=False).all()
        for item in new_notif_topic_items:
            author = User.query.get(item.author_id)
            post_id = re.findall(r'\d+', item.url)[0]
            post = Post.query.get(post_id)
            notification_json = {}
            notification_json['notif_type'] = NOTIF_TOPIC
            notification_json['notif_type_subtype'] = 'new_post_in_followed_topic'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            items.append(notification_json)

        # get all the unread NOTIF_POST
        # for each one
        #  - get the author info
        #  - get the postid and commentid from re.findall 
        #  - get the post info
        #  - get the comment info
        new_notif_post_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_POST).filter_by(read=False).all()
        for item in new_notif_post_items:
            author = User.query.get(item.author_id)
            # returns a list[] of numbers found, left-to-right in the url string
            post_and_comment_ids = re.findall(r'\d+', item.url) 
            post = Post.query.get(post_and_comment_ids[0])
            comment = PostReply.query.get(post_and_comment_ids[1])
            notification_json = {}
            notification_json['notif_type'] = NOTIF_POST
            notification_json['notif_type_subtype'] = 'top_level_comment_on_followed_post'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            notification_json['comment'] = reply_view(comment, variant=1)
            items.append(notification_json)

        # get all the unread NOTIF_REPLY
        # for each one
        #  - get the author info
        #  - get the postid and commentid from re.findall 
        #  - get the post info
        #  - get the comment info
        new_notif_reply_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_REPLY).filter_by(read=False).all()
        for item in new_notif_reply_items:
            author = User.query.get(item.author_id)
            # returns a list[] of numbers found, left-to-right in the url string
            post_and_comment_ids = re.findall(r'\d+', item.url) 
            post = Post.query.get(post_and_comment_ids[0])
            comment = PostReply.query.get(post_and_comment_ids[1])
            notification_json = {}
            notification_json['notif_type'] = NOTIF_REPLY
            notification_json['notif_type_subtype'] = 'new_reply_on_followed_comment'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            notification_json['comment'] = reply_view(comment, variant=1)
            items.append(notification_json)            

        # get all the unread NOTIF_MENTION
        # for each one
        #  - get the author info
        #  - figure out if it's a post mention or comment mention
        #  - get the post/commentid from re.findall and then get the post/comment info
        new_notif_mention_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_MENTION).filter_by(read=False).all()
        for item in new_notif_mention_items:
            notification_json = {}
            if 'post' in item.url:
                author = User.query.get(item.author_id)
                post_id = re.findall(r'\d+', item.url)[0]
                post = Post.query.get(post_id)
                notification_json['author'] = user_view(user=author.id, variant=3)
                notification_json['post'] = post_view(post, variant=2)
                notification_json['notif_type'] = NOTIF_MENTION
                notification_json['notif_type_subtype'] = 'post_mention'
                items.append(notification_json)
            if 'comment' in item.url:
                author = User.query.get(item.author_id)
                comment_id = re.findall(r'\d+', item.url)[0]
                comment = PostReply.query.get(comment_id)
                notification_json['author'] = user_view(user=author.id, variant=3)
                notification_json['comment'] = reply_view(comment, variant=1)
                notification_json['notif_type'] = NOTIF_MENTION
                notification_json['notif_type_subtype'] = 'comment_mention'
                items.append(notification_json)

    # all
    elif status == 'all':
        # get all the NOTIF_USER
        # for each one
        #  - get the author info
        #  - get the postid from re.findall and then get the post info
        all_notif_user_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_USER).all()
        for item in all_notif_user_items:
            author = User.query.get(item.author_id)
            post_id = re.findall(r'\d+', item.url)[0]
            post = Post.query.get(post_id)
            notification_json = {}
            notification_json['notif_type'] = NOTIF_USER
            notification_json['notif_type_subtype'] = 'new_post_from_followed_user'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            items.append(notification_json)        

        # get all the NOTIF_COMMUNITY
        # for each one
        #  - get the author info
        #  - get the postid from re.findall and then get the post info
        #  - get the community info from the post.community_id
        all_notif_community_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_COMMUNITY).all()
        for item in all_notif_community_items:
            author = User.query.get(item.author_id)
            post_id = re.findall(r'\d+', item.url)[0]
            post = Post.query.get(post_id)
            community = Community.query.get(post.community_id)
            notification_json = {}
            notification_json['notif_type'] = NOTIF_COMMUNITY
            notification_json['notif_type_subtype'] = 'new_post_in_followed_community'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            notification_json['community'] = community_view(community, variant=2)
            items.append(notification_json)

        # get all the NOTIF_TOPIC
        # for each one
        #  - get the author info
        #  - get the postid from re.findall and then get the post info
        all_notif_topic_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_TOPIC).all()
        for item in all_notif_topic_items:
            author = User.query.get(item.author_id)
            post_id = re.findall(r'\d+', item.url)[0]
            post = Post.query.get(post_id)
            notification_json = {}
            notification_json['notif_type'] = NOTIF_TOPIC
            notification_json['notif_type_subtype'] = 'new_post_in_followed_topic'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            items.append(notification_json)              

        # get all the NOTIF_POST
        # for each one
        #  - get the author info
        #  - get the postid and commentid from re.findall 
        #  - get the post info
        #  - get the comment info
        all_notif_post_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_POST).all()
        for item in all_notif_post_items:
            author = User.query.get(item.author_id)
            # returns a list[] of numbers found, left-to-right in the url string
            post_and_comment_ids = re.findall(r'\d+', item.url) 
            post = Post.query.get(post_and_comment_ids[0])
            comment = PostReply.query.get(post_and_comment_ids[1])
            notification_json = {}
            notification_json['notif_type'] = NOTIF_POST
            notification_json['notif_type_subtype'] = 'top_level_comment_on_followed_post'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            notification_json['comment'] = reply_view(comment, variant=1)
            items.append(notification_json)            

        # get all the NOTIF_REPLY
        # for each one
        #  - get the author info
        #  - get the postid and commentid from re.findall 
        #  - get the post info
        #  - get the comment info
        all_notif_reply_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_REPLY).all()
        for item in all_notif_reply_items:
            author = User.query.get(item.author_id)
            # returns a list[] of numbers found, left-to-right in the url string
            post_and_comment_ids = re.findall(r'\d+', item.url) 
            post = Post.query.get(post_and_comment_ids[0])
            comment = PostReply.query.get(post_and_comment_ids[1])
            notification_json = {}
            notification_json['notif_type'] = NOTIF_REPLY
            notification_json['notif_type_subtype'] = 'new_reply_on_followed_comment'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            notification_json['comment'] = reply_view(comment, variant=1)
            items.append(notification_json)               

        # get all the NOTIF_MENTION
        # for each one
        #  - get the author info
        #  - figure out if it's a post mention or comment mention
        #  - get the post/commentid from re.findall and then get the post/comment info
        all_notif_mention_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_MENTION).all()
        for item in all_notif_mention_items:
            notification_json = {}
            if 'post' in item.url:
                author = User.query.get(item.author_id)
                post_id = re.findall(r'\d+', item.url)[0]
                post = Post.query.get(post_id)
                notification_json['author'] = user_view(user=author.id, variant=3)
                notification_json['post'] = post_view(post, variant=2)
                notification_json['notif_type'] = NOTIF_MENTION
                notification_json['notif_type_subtype'] = 'post_mention'
                items.append(notification_json)            
            if 'comment' in item.url:
                author = User.query.get(item.author_id)
                comment_id = re.findall(r'\d+', item.url)[0]
                comment = PostReply.query.get(comment_id)
                notification_json['author'] = user_view(user=author.id, variant=3)
                notification_json['comment'] = reply_view(comment, variant=1)
                notification_json['notif_type'] = NOTIF_MENTION
                notification_json['notif_type_subtype'] = 'comment_mention'
                items.append(notification_json)            

    # read
    elif status == 'read':
        # get all the read NOTIF_USER
        # for each one
        #  - get the author info
        #  - get the postid from re.findall and then get the post info
        read_notif_user_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_USER).filter_by(read=True).all()
        for item in read_notif_user_items:
            author = User.query.get(item.author_id)
            post_id = re.findall(r'\d+', item.url)[0]
            post = Post.query.get(post_id)
            notification_json = {}
            notification_json['notif_type'] = NOTIF_USER
            notification_json['notif_type_subtype'] = 'new_post_from_followed_user'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            items.append(notification_json)        

        # get all the read NOTIF_COMMUNITY
        # for each one
        #  - get the author info
        #  - get the postid from re.findall and then get the post info
        #  - get the community info from the post.community_id
        read_notif_community_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_COMMUNITY).filter_by(read=True).all()
        for item in read_notif_community_items:
            author = User.query.get(item.author_id)
            post_id = re.findall(r'\d+', item.url)[0]
            post = Post.query.get(post_id)
            community = Community.query.get(post.community_id)
            notification_json = {}
            notification_json['notif_type'] = NOTIF_COMMUNITY
            notification_json['notif_type_subtype'] = 'new_post_in_followed_community'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            notification_json['community'] = community_view(community, variant=2)
            items.append(notification_json)

        # get all the read NOTIF_TOPIC
        # for each one
        #  - get the author info
        #  - get the postid from re.findall and then get the post info
        read_notif_topic_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_TOPIC).filter_by(read=True).all()
        for item in read_notif_topic_items:
            author = User.query.get(item.author_id)
            post_id = re.findall(r'\d+', item.url)[0]
            post = Post.query.get(post_id)
            notification_json = {}
            notification_json['notif_type'] = NOTIF_TOPIC
            notification_json['notif_type_subtype'] = 'new_post_in_followed_topic'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            items.append(notification_json) 

        # get all the read NOTIF_POST
        # for each one
        #  - get the author info
        #  - get the postid and commentid from re.findall 
        #  - get the post info
        #  - get the comment info
        read_notif_post_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_POST).filter_by(read=True).all()
        for item in read_notif_post_items:
            author = User.query.get(item.author_id)
            # returns a list[] of numbers found, left-to-right in the url string
            post_and_comment_ids = re.findall(r'\d+', item.url) 
            post = Post.query.get(post_and_comment_ids[0])
            comment = PostReply.query.get(post_and_comment_ids[1])
            notification_json = {}
            notification_json['notif_type'] = NOTIF_POST
            notification_json['notif_type_subtype'] = 'top_level_comment_on_followed_post'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            notification_json['comment'] = reply_view(comment, variant=1)
            items.append(notification_json)            

        # get all the read NOTIF_REPLY
        # for each one
        #  - get the author info
        #  - get the postid and commentid from re.findall 
        #  - get the post info
        #  - get the comment info
        read_notif_reply_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_REPLY).filter_by(read=True).all()
        for item in read_notif_reply_items:
            author = User.query.get(item.author_id)
            # returns a list[] of numbers found, left-to-right in the url string
            post_and_comment_ids = re.findall(r'\d+', item.url) 
            post = Post.query.get(post_and_comment_ids[0])
            comment = PostReply.query.get(post_and_comment_ids[1])
            notification_json = {}
            notification_json['notif_type'] = NOTIF_REPLY
            notification_json['notif_type_subtype'] = 'new_reply_on_followed_comment'
            notification_json['author'] = user_view(user=author.id, variant=3)
            notification_json['post'] = post_view(post, variant=2)
            notification_json['comment'] = reply_view(comment, variant=1)
            items.append(notification_json)               

        # get all the read NOTIF_MENTION
        # for each one
        #  - get the author info
        #  - figure out if it's a post mention or comment mention
        #  - get the post/commentid from re.findall and then get the post/comment info
        read_notif_mention_items = Notification.query.filter_by(user_id=user.id).filter_by(notif_type=NOTIF_MENTION).filter_by(read=True).all()
        for item in read_notif_mention_items:
            notification_json = {}
            if 'post' in item.url:
                author = User.query.get(item.author_id)
                post_id = re.findall(r'\d+', item.url)[0]
                post = Post.query.get(post_id)
                notification_json['author'] = user_view(user=author.id, variant=3)
                notification_json['post'] = post_view(post, variant=2)
                notification_json['notif_type'] = NOTIF_MENTION
                notification_json['notif_type_subtype'] = 'post_mention'
                items.append(notification_json)            
            if 'comment' in item.url:
                author = User.query.get(item.author_id)
                comment_id = re.findall(r'\d+', item.url)[0]
                comment = PostReply.query.get(comment_id)
                notification_json['author'] = user_view(user=author.id, variant=3)
                notification_json['comment'] = reply_view(comment, variant=1)
                notification_json['notif_type'] = NOTIF_MENTION
                notification_json['notif_type_subtype'] = 'comment_mention'

    # make dicts of that and pass back
    res = {}
    res['user'] = user.user_name
    res['status'] = status
    res['counts'] = counts
    res['items'] = items
    return res
