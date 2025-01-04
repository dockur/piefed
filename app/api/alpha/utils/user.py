from app.api.alpha.views import user_view
from app.utils import authorise_api_user
from app.api.alpha.utils.post import get_post_list
from app.api.alpha.utils.reply import get_reply_list
from app.api.alpha.utils.validators import required, integer_expected, boolean_expected
from app.shared.user import block_another_user, unblock_another_user
from app.models import User, Notification


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

    user_json = user_view(user=person_id, variant=3)
    user_json['posts'] = post_list['posts']
    user_json['comments'] = reply_list['comments']
    return user_json


# get the counts for a user's unread replies, mentions, and private messages
def get_user_unread_count(auth):
    # user_id = logged in user
    user_id = None
    if auth:
        user_id = authorise_api_user(auth)
    
    # get the user's unread notifications
    # we have reply notifications and chat notifications
    # TO-DO - add the mentions when we have those

    # run a command to get the users notifications, then those that are unread, 
    # then of those get the ones with urls that begin with /chat/, then get the count
    chat_count = Notification.query.filter_by(user_id=user_id).filter_by(read=False).filter(Notification.url.like('%/chat/%')).count()

    # run a command to get the users notifications, then those that are unread, 
    # then of those get the ones with urls that begin with /post/, then get the count
    reply_count = Notification.query.filter_by(user_id=user_id).filter_by(read=False).filter(Notification.url.like('%/post/%')).count()

    # build the json
    unread_json = {
        "replies": reply_count,
        "mentions": 0,
        "private_messages": chat_count
    }
    
    return unread_json

# would be in app/constants.py
SRC_API = 3

def post_user_block(auth, data):
    required(['person_id', 'block'], data)
    integer_expected(['post_id'], data)
    boolean_expected(['block'], data)

    person_id = data['person_id']
    block = data['block']

    user_id = block_another_user(person_id, SRC_API, auth) if block else unblock_another_user(person_id, SRC_API, auth)
    user_json = user_view(user=person_id, variant=4, user_id=user_id)
    return user_json
