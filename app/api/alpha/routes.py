from app import limiter
from app.api.alpha import bp
from app.constants import *
from app.api.alpha.utils import get_site, post_site_block, \
                                get_search, \
                                get_post_list, get_post, post_post_like, put_post_save, put_post_subscribe, post_post, \
                                put_post, post_post_delete, post_post_report, post_post_lock, post_post_feature, post_post_remove, \
                                get_reply_list, post_reply_like, put_reply_save, put_reply_subscribe, post_reply, put_reply, post_reply_mark_as_read, get_reply, \
                                post_reply_delete, post_reply_report, post_reply_remove, \
                                get_community_list, get_community, post_community_follow, post_community_block, post_community, put_community, put_community_subscribe, post_community_delete, \
                                get_user, post_user_block, get_user_unread_count, get_user_replies, post_user_mark_all_as_read, put_user_subscribe, put_user_save_user_settings, \
                                get_private_message_list, \
                                post_upload_image, post_upload_community_image, post_upload_user_image, \
                                get_user_notifications, get_user_notifs_no_auth
from app.shared.auth import log_user_in

from flask import current_app, jsonify, request
from flask_limiter import RateLimitExceeded

from sqlalchemy.orm.exc import NoResultFound

def enable_api():
    return True if current_app.debug else False

# Site
@bp.route('/api/alpha/site', methods=['GET'])
def get_alpha_site():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        return jsonify(get_site(auth))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/site/block', methods=['POST'])
def get_alpha_site_block():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_site_block(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# Misc
@bp.route('/api/alpha/search', methods=['GET'])
def get_alpha_search():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_search(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# Community
@bp.route('/api/alpha/community', methods=['GET'])
def get_alpha_community():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_community(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/community/list', methods=['GET'])
def get_alpha_community_list():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_community_list(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/community/follow', methods=['POST'])
def post_alpha_community_follow():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_community_follow(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/community/block', methods=['POST'])
def post_alpha_community_block():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_community_block(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/community', methods=['POST'])
def post_alpha_community():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        with limiter.limit('1/day'):
            auth = request.headers.get('Authorization')
            data = request.get_json(force=True) or {}
            return jsonify(post_community(auth, data))
    except RateLimitExceeded as ex:
        return jsonify({"error": str(ex)}), 429
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/community', methods=['PUT'])
def put_alpha_community():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_community(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/community/subscribe', methods=['PUT'])
def put_alpha_community_subscribe():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_community_subscribe(auth, data))
    except NoResultFound:
        return jsonify({"error": "Community not found"}), 400
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/community/delete', methods=['POST'])
def post_alpha_community_delete():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_community_delete(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# Post
@bp.route('/api/alpha/post/list', methods=['GET'])
def get_alpha_post_list():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_post_list(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post', methods=['GET'])
def get_alpha_post():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_post(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/like', methods=['POST'])
def post_alpha_post_like():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_post_like(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/save', methods=['PUT'])
def put_alpha_post_save():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_post_save(auth, data))
    except NoResultFound:
        return jsonify({"error": "Post not found"}), 400
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/subscribe', methods=['PUT'])
def put_alpha_post_subscribe():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_post_subscribe(auth, data))
    except NoResultFound:
        return jsonify({"error": "Post not found"}), 400
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post', methods=['POST'])
def post_alpha_post():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        with limiter.limit('1/minute, 10/hour, 50/day'):
            auth = request.headers.get('Authorization')
            data = request.get_json(force=True) or {}
            return jsonify(post_post(auth, data))
    except RateLimitExceeded as ex:
        return jsonify({"error": str(ex)}), 429
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post', methods=['PUT'])
def put_alpha_post():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_post(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/delete', methods=['POST'])
def post_alpha_post_delete():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_post_delete(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/report', methods=['POST'])
def post_alpha_post_report():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_post_report(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/lock', methods=['POST'])
def post_alpha_post_lock():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_post_lock(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/feature', methods=['POST'])
def post_alpha_post_feature():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_post_feature(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/remove', methods=['POST'])
def post_alpha_post_remove():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_post_remove(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# Reply
@bp.route('/api/alpha/comment/list', methods=['GET'])
def get_alpha_comment_list():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_reply_list(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/like', methods=['POST'])
def post_alpha_comment_like():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_reply_like(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/save', methods=['PUT'])
def put_alpha_comment_save():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_reply_save(auth, data))
    except NoResultFound:
        return jsonify({"error": "Comment not found"}), 400
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/subscribe', methods=['PUT'])
def put_alpha_comment_subscribe():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_reply_subscribe(auth, data))
    except NoResultFound:
        return jsonify({"error": "Comment not found"}), 400
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment', methods=['POST'])
def post_alpha_comment():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        with limiter.limit('1/minute, 10/hour, 50/day'):
            auth = request.headers.get('Authorization')
            data = request.get_json(force=True) or {}
            return jsonify(post_reply(auth, data))
    except RateLimitExceeded as ex:
        return jsonify({"error": str(ex)}), 429
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment', methods=['PUT'])
def put_alpha_comment():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_reply(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/delete', methods=['POST'])
def post_alpha_comment_delete():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_reply_delete(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/report', methods=['POST'])
def post_alpha_comment_report():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_reply_report(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/remove', methods=['POST'])
def post_alpha_comment_remove():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_reply_remove(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/mark_as_read', methods=['POST'])
def post_alpha_comment_mark_as_read():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_reply_mark_as_read(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment', methods=['GET'])
def get_alpha_comment():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_reply(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400

# Private Message
@bp.route('/api/alpha/private_message/list', methods=['GET'])
def get_alpha_private_message_list():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_private_message_list(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# User
@bp.route('/api/alpha/user', methods=['GET'])
def get_alpha_user():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_user(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/user/login', methods=['POST'])
def post_alpha_user_login():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        data = request.get_json(force=True) or {}
        return jsonify(log_user_in(data, SRC_API))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/user/unread_count', methods=['GET'])
def get_alpha_user_unread_count():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        return jsonify(get_user_unread_count(auth))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/user/replies', methods=['GET'])
def get_alpha_user_replies():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_user_replies(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/user/block', methods=['POST'])
def post_alpha_user_block():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_user_block(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/user/mark_all_as_read', methods=['POST'])
def post_alpha_user_mark_all_as_read():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        return jsonify(post_user_mark_all_as_read(auth))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/user/subscribe', methods=['PUT'])
def put_alpha_user_subscribe():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_user_subscribe(auth, data))
    except NoResultFound:
        return jsonify({"error": "User not found"}), 400
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# currently handles hide_nsfw, hide_read_posts, and user.about 
@bp.route('/api/alpha/user/save_user_settings', methods=['PUT'])
def put_alpha_user_save_user_settings():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_user_save_user_settings(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/user/notifications/<status>')
def get_alpha_notifications(status):
    # if not enable_api():
    #     return jsonify({'error': 'alpha api is not enabled'})
    # try:
    #     auth = request.headers.get('Authorization')
    #     data = request.get_json(force=True) or {}
    #     data['status_request'] = status
    #     return jsonify(get_user_notifications(auth, data))
    # except Exception as ex:
    #     return jsonify({"error": str(ex)}), 400    
    data = request.get_json(force=True) or {}
    data['status_request'] = status
    res = jsonify(get_user_notifs_no_auth(data))
    return res 


# Upload
@bp.route('/api/alpha/upload/image', methods=['POST'])
def post_alpha_upload_image():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        with limiter.limit('5/hour'):
            auth = request.headers.get('Authorization')
            image_file = request.files['file']
            return jsonify(post_upload_image(auth, image_file))
    except RateLimitExceeded as ex:
        return jsonify({"error": str(ex)}), 429
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/upload/community_image', methods=['POST'])
def post_alpha_upload_community_image():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        with limiter.limit('3/day'):
            auth = request.headers.get('Authorization')
            image_file = request.files['file']
            return jsonify(post_upload_community_image(auth, image_file))
    except RateLimitExceeded as ex:
        return jsonify({"error": str(ex)}), 429
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/upload/user_image', methods=['POST'])
def post_alpha_upload_user_image():
    if not enable_api():
        return jsonify({'error': 'alpha api is not enabled'})
    try:
        with limiter.limit('3/day'):
            auth = request.headers.get('Authorization')
            image_file = request.files['file']
            return jsonify(post_upload_user_image(auth, image_file))
    except RateLimitExceeded as ex:
        return jsonify({"error": str(ex)}), 429
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# Not yet implemented. Copied from lemmy's V3 api, so some aren't needed, and some need changing

# Site - not yet implemented
@bp.route('/api/alpha/site', methods=['POST'])                                    # Create New Site. No plans to implement
@bp.route('/api/alpha/site', methods=['PUT'])                                     # Edit Site. Not available in app
def alpha_site():
    return jsonify({"error": "not_yet_implemented"}), 400

# Miscellaneous - not yet implemented
@bp.route('/api/alpha/modlog', methods=['GET'])                                   # Get Modlog. Not usually public
@bp.route('/api/alpha/resolve_object', methods=['GET'])                           # Stage 2
@bp.route('/api/alpha/federated_instances', methods=['GET'])                      # No plans to implement - only V3 version needed
def alpha_miscellaneous():
    return jsonify({"error": "not_yet_implemented"}), 400

# Community - not yet implemented
#@bp.route('/api/alpha/community', methods=['POST'])                               # (none
#@bp.route('/api/alpha/community', methods=['PUT'])                                #  of
@bp.route('/api/alpha/community/hide', methods=['PUT'])                           #  these
#@bp.route('/api/alpha/community/delete', methods=['POST'])                        #  are
@bp.route('/api/alpha/community/remove', methods=['POST'])                        #  available
@bp.route('/api/alpha/community/transfer', methods=['POST'])                      #  in
@bp.route('/api/alpha/community/ban_user', methods=['POST'])                      #  the
@bp.route('/api/alpha/community/mod', methods=['POST'])                           #  app)
def alpha_community():
    return jsonify({"error": "not_yet_implemented"}), 400

# Post - not yet implemented
@bp.route('/api/alpha/post/report/resolve', methods=['PUT'])                      # Stage 2
@bp.route('/api/alpha/post/report/list', methods=['GET'])                         # Stage 2
@bp.route('/api/alpha/post/site_metadata', methods=['GET'])                       # Not available in app
def alpha_post():
    return jsonify({"error": "not_yet_implemented"}), 400

# Reply - not yet implemented
@bp.route('/api/alpha/comment/distinguish', methods=['POST'])                     # Not really used
@bp.route('/api/alpha/comment/report/resolve', methods=['PUT'])                   # Stage 2
@bp.route('/api/alpha/comment/report/list', methods=['GET'])                      # Stage 2
def alpha_reply():
    return jsonify({"error": "not_yet_implemented"}), 400

# Chat - not yet implemented
@bp.route('/api/alpha/private_message', methods=['PUT'])                          # Not available in app
@bp.route('/api/alpha/private_message', methods=['POST'])                         # Not available in app
@bp.route('/api/alpha/private_message/delete', methods=['POST'])                  # Not available in app
@bp.route('/api/alpha/private_message/mark_as_read', methods=['POST'])            # Not available in app
@bp.route('/api/alpha/private_message/report', methods=['POST'])                  # Not available in app
@bp.route('/api/alpha/private_message/report/resolve', methods=['PUT'])           # Stage 2
@bp.route('/api/alpha/private_message/report/list', methods=['GET'])              # Stage 2
def alpha_chat():
    return jsonify({"error": "not_yet_implemented"}), 400

# User - not yet implemented
@bp.route('/api/alpha/user/register', methods=['POST'])                           # Not available in app
@bp.route('/api/alpha/user/get_captcha', methods=['GET'])                         # Not available in app
@bp.route('/api/alpha/user/mention', methods=['GET'])                             # No DB support
@bp.route('/api/alpha/user/mention/mark_as_read', methods=['POST'])               # No DB support / Not available in app (using mark_all instead)
@bp.route('/api/alpha/user/ban', methods=['POST'])                                # Admin function. No plans to implement
@bp.route('/api/alpha/user/banned', methods=['GET'])                              # Admin function. No plans to implement
@bp.route('/api/alpha/user/delete_account', methods=['POST'])                     # Not available in app
@bp.route('/api/alpha/user/password_reset', methods=['POST'])                     # Not available in app
@bp.route('/api/alpha/user/password_change', methods=['POST'])                    # Not available in app
@bp.route('/api/alpha/user/change_password', methods=['PUT'])                     # Stage 2
@bp.route('/api/alpha/user/report_count', methods=['GET'])                        # Stage 2
@bp.route('/api/alpha/user/verify_email', methods=['POST'])                       # Admin function. No plans to implement
@bp.route('/api/alpha/user/leave_admin', methods=['POST'])                        # Admin function. No plans to implement
@bp.route('/api/alpha/user/totp/generate', methods=['POST'])                      # Not available in app
@bp.route('/api/alpha/user/totp/update', methods=['POST'])                        # Not available in app
@bp.route('/api/alpha/user/export_settings', methods=['GET'])                     # Not available in app
@bp.route('/api/alpha/user/import_settings', methods=['POST'])                    # Not available in app
@bp.route('/api/alpha/user/list_logins', methods=['GET'])                         # Not available in app
@bp.route('/api/alpha/user/validate_auth', methods=['GET'])                       # Not available in app
@bp.route('/api/alpha/user/logout', methods=['POST'])                             # Stage 2
def alpha_user():
    return jsonify({"error": "not_yet_implemented"}), 400

# Admin - not yet implemented
@bp.route('/api/alpha/admin/add', methods=['POST'])
@bp.route('/api/alpha/admin/registration_application/count', methods=['GET'])     # (no
@bp.route('/api/alpha/admin/registration_application/list', methods=['GET'])      #  plans
@bp.route('/api/alpha/admin/registration_application/approve', methods=['PUT'])   #  to
@bp.route('/api/alpha/admin/purge/person', methods=['POST'])                      #  implement
@bp.route('/api/alpha/admin/purge/community', methods=['POST'])                   #  any
@bp.route('/api/alpha/admin/purge/post', methods=['POST'])                        #  endpoints
@bp.route('/api/alpha/admin/purge/comment', methods=['POST'])                     #  for
@bp.route('/api/alpha/post/like/list', methods=['GET'])                           #  admin
@bp.route('/api/alpha/comment/like/list', methods=['GET'])                        #  use)
def alpha_admin():
    return jsonify({"error": "not_yet_implemented"}), 400

# CustomEmoji - not yet implemented
@bp.route('/api/alpha/custom_emoji', methods=['PUT'])                             # (doesn't
@bp.route('/api/alpha/custom_emoji', methods=['POST'])                            #  seem
@bp.route('/api/alpha/custom_emoji/delete', methods=['POST'])                     #  important)
def alpha_emoji():
    return jsonify({"error": "not_yet_implemented"}), 400


