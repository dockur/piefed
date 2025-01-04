from app.api.alpha import bp
from app.api.alpha.utils import get_site, post_site_block, \
                                get_search, \
                                get_post_list, get_post, post_post_like, put_post_save, put_post_subscribe, \
                                get_reply_list, post_reply_like, put_reply_save, put_reply_subscribe, post_reply, put_reply, post_reply_delete, post_reply_report, \
                                get_community_list, get_community, post_community_follow, post_community_block, \
                                get_user, post_user_block, get_user_unread_count

from app.shared.auth import log_user_in

from flask import current_app, jsonify, request


# Site
@bp.route('/api/alpha/site', methods=['GET'])
def get_alpha_site():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        return jsonify(get_site(auth))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/site/block', methods=['POST'])
def get_alpha_site_block():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_site_block(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# Misc
@bp.route('/api/alpha/search', methods=['GET'])
def get_alpha_search():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_search(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# Community
@bp.route('/api/alpha/community', methods=['GET'])
def get_alpha_community():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_community(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/community/list', methods=['GET'])
def get_alpha_community_list():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_community_list(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/community/follow', methods=['POST'])
def post_alpha_community_follow():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_community_follow(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/community/block', methods=['POST'])
def post_alpha_community_block():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_community_block(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# Post
@bp.route('/api/alpha/post/list', methods=['GET'])
def get_alpha_post_list():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_post_list(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post', methods=['GET'])
def get_alpha_post():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_post(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/like', methods=['POST'])
def post_alpha_post_like():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_post_like(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/save', methods=['PUT'])
def put_alpha_post_save():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_post_save(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/post/subscribe', methods=['PUT'])
def put_alpha_post_subscribe():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_post_subscribe(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# Reply
@bp.route('/api/alpha/comment/list', methods=['GET'])
def get_alpha_comment_list():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_reply_list(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/like', methods=['POST'])
def post_alpha_comment_like():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_reply_like(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/save', methods=['PUT'])
def put_alpha_comment_save():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_reply_save(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/subscribe', methods=['PUT'])
def put_alpha_comment_subscribe():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_reply_subscribe(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment', methods=['POST'])
def post_alpha_comment():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_reply(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment', methods=['PUT'])
def put_alpha_comment():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(put_reply(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/delete', methods=['POST'])
def post_alpha_comment_delete():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_reply_delete(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/comment/report', methods=['POST'])
def post_alpha_comment_report():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_reply_report(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# User
@bp.route('/api/alpha/user', methods=['GET'])
def get_alpha_user():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.args.to_dict() or None
        return jsonify(get_user(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/user/login', methods=['POST'])
def post_alpha_user_login():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        SRC_API = 3                                     # would be in app.constants
        data = request.get_json(force=True) or {}
        return jsonify(log_user_in(data, SRC_API))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/user/block', methods=['POST'])
def post_alpha_user_block():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        data = request.get_json(force=True) or {}
        return jsonify(post_user_block(auth, data))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.route('/api/alpha/user/unread_count', methods=['GET'])
def get_alpha_user_unread_count():
    if not current_app.debug:
        return jsonify({'error': 'alpha api routes only available in debug mode'})
    try:
        auth = request.headers.get('Authorization')
        return jsonify(get_user_unread_count(auth))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


# Not yet implemented. Copied from lemmy's V3 api, so some aren't needed, and some need changing

# Site - not yet implemented
@bp.route('/api/alpha/site', methods=['POST'])
@bp.route('/api/alpha/site', methods=['PUT'])
def alpha_site():
    return jsonify({"error": "not_yet_implemented"}), 400

# Miscellaneous - not yet implemented
@bp.route('/api/alpha/modlog', methods=['GET'])
@bp.route('/api/alpha/resolve_object', methods=['GET'])
@bp.route('/api/alpha/federated_instances', methods=['GET'])
def alpha_miscellaneous():
    return jsonify({"error": "not_yet_implemented"}), 400

# Community - not yet implemented
@bp.route('/api/alpha/community', methods=['POST'])
@bp.route('/api/alpha/community', methods=['PUT'])
@bp.route('/api/alpha/community/hide', methods=['PUT'])
@bp.route('/api/alpha/community/delete', methods=['POST'])
@bp.route('/api/alpha/community/remove', methods=['POST'])
@bp.route('/api/alpha/community/transfer', methods=['POST'])
@bp.route('/api/alpha/community/ban_user', methods=['POST'])
@bp.route('/api/alpha/community/mod', methods=['POST'])
def alpha_community():
    return jsonify({"error": "not_yet_implemented"}), 400

# Post - not yet implemented
@bp.route('/api/alpha/post', methods=['PUT'])
@bp.route('/api/alpha/post', methods=['POST'])
@bp.route('/api/alpha/post/delete', methods=['POST'])
@bp.route('/api/alpha/post/remove', methods=['POST'])
@bp.route('/api/alpha/post/lock', methods=['POST'])
@bp.route('/api/alpha/post/feature', methods=['POST'])
@bp.route('/api/alpha/post/report', methods=['POST'])
@bp.route('/api/alpha/post/report/resolve', methods=['PUT'])
@bp.route('/api/alpha/post/report/list', methods=['GET'])
@bp.route('/api/alpha/post/site_metadata', methods=['GET'])
def alpha_post():
    return jsonify({"error": "not_yet_implemented"}), 400

# Reply - not yet implemented
@bp.route('/api/alpha/comment', methods=['GET'])
@bp.route('/api/alpha/comment/remove', methods=['POST'])
@bp.route('/api/alpha/comment/mark_as_read', methods=['POST'])
@bp.route('/api/alpha/comment/distinguish', methods=['POST'])
@bp.route('/api/alpha/comment/report/resolve', methods=['PUT'])
@bp.route('/api/alpha/comment/report/list', methods=['GET'])
def alpha_reply():
    return jsonify({"error": "not_yet_implemented"}), 400

# Chat - not yet implemented
@bp.route('/api/alpha/private_message/list', methods=['GET'])
@bp.route('/api/alpha/private_message', methods=['PUT'])
@bp.route('/api/alpha/private_message', methods=['POST'])
@bp.route('/api/alpha/private_message/delete', methods=['POST'])
@bp.route('/api/alpha/private_message/mark_as_read', methods=['POST'])
@bp.route('/api/alpha/private_message/report', methods=['POST'])
@bp.route('/api/alpha/private_message/report/resolve', methods=['PUT'])
@bp.route('/api/alpha/private_message/report/list', methods=['GET'])
def alpha_chat():
    return jsonify({"error": "not_yet_implemented"}), 400

# User - not yet implemented
@bp.route('/api/alpha/user/register', methods=['POST'])
@bp.route('/api/alpha/user/get_captcha', methods=['GET'])
@bp.route('/api/alpha/user/mention', methods=['GET'])
@bp.route('/api/alpha/user/mention/mark_as_read', methods=['POST'])
@bp.route('/api/alpha/user/replies', methods=['GET'])
@bp.route('/api/alpha/user/ban', methods=['POST'])
@bp.route('/api/alpha/user/banned', methods=['GET'])
@bp.route('/api/alpha/user/delete_account', methods=['POST'])
@bp.route('/api/alpha/user/password_reset', methods=['POST'])
@bp.route('/api/alpha/user/password_change', methods=['POST'])
@bp.route('/api/alpha/user/mark_all_as_read', methods=['POST'])
@bp.route('/api/alpha/user/save_user_settings', methods=['PUT'])
@bp.route('/api/alpha/user/change_password', methods=['PUT'])
@bp.route('/api/alpha/user/repost_count', methods=['GET'])
@bp.route('/api/alpha/user/verify_email', methods=['POST'])
@bp.route('/api/alpha/user/leave_admin', methods=['POST'])
@bp.route('/api/alpha/user/totp/generate', methods=['POST'])
@bp.route('/api/alpha/user/totp/update', methods=['POST'])
@bp.route('/api/alpha/user/export_settings', methods=['GET'])
@bp.route('/api/alpha/user/import_settings', methods=['POST'])
@bp.route('/api/alpha/user/list_logins', methods=['GET'])
@bp.route('/api/alpha/user/validate_auth', methods=['GET'])
@bp.route('/api/alpha/user/logout', methods=['POST'])
def alpha_user():
    return jsonify({"error": "not_yet_implemented"}), 400

# Admin - not yet implemented
@bp.route('/api/alpha/admin/add', methods=['POST'])
@bp.route('/api/alpha/admin/registration_application/count', methods=['GET'])
@bp.route('/api/alpha/admin/registration_application/list', methods=['GET'])
@bp.route('/api/alpha/admin/registration_application/approve', methods=['PUT'])
@bp.route('/api/alpha/admin/purge/person', methods=['POST'])
@bp.route('/api/alpha/admin/purge/community', methods=['POST'])
@bp.route('/api/alpha/admin/purge/post', methods=['POST'])
@bp.route('/api/alpha/admin/purge/comment', methods=['POST'])
@bp.route('/api/alpha/post/like/list', methods=['GET'])
@bp.route('/api/alpha/comment/like/list', methods=['GET'])
def alpha_admin():
    return jsonify({"error": "not_yet_implemented"}), 400

# CustomEmoji - not yet implemented
@bp.route('/api/alpha/custom_emoji', methods=['PUT'])
@bp.route('/api/alpha/custom_emoji', methods=['POST'])
@bp.route('/api/alpha/custom_emoji/delete', methods=['POST'])
def alpha_emoji():
    return jsonify({"error": "not_yet_implemented"}), 400


# HTML routes
from flask import abort, render_template
from app.utils import current_theme
import os

@bp.route('/api/alpha/', methods=['GET'])
def get_alpha():
    if not current_app.debug:
        abort(404)

    template_name = "index.html"

    theme = current_theme()
    if theme != '' and os.path.exists(f'app/templates/themes/{theme}/{template_name}'):
        return render_template(f'themes/{theme}/{template_name}')
    else:
        return render_template(template_name)


@bp.route('/api/alpha/auth/login', methods=['GET'])
def get_alpha_auth_login():
    if not current_app.debug:
        abort(404)

    template_name = "auth/login.html"

    theme = current_theme()
    if theme != '' and os.path.exists(f'app/templates/themes/{theme}/{template_name}'):
        return render_template(f'themes/{theme}/{template_name}')
    else:
        return render_template(template_name)


@bp.route('/api/alpha/auth/logout', methods=['GET'])
def get_alpha_auth_logout():
    if not current_app.debug:
        abort(404)

    template_name = "auth/logout.html"

    theme = current_theme()
    if theme != '' and os.path.exists(f'app/templates/themes/{theme}/{template_name}'):
        return render_template(f'themes/{theme}/{template_name}')
    else:
        return render_template(template_name)


@bp.route('/api/alpha/communities', methods=['GET'])
def get_alpha_communities():
    if not current_app.debug:
        abort(404)

    template_name = "list_communities.html"

    theme = current_theme()
    if theme != '' and os.path.exists(f'app/templates/themes/{theme}/{template_name}'):
        return render_template(f'themes/{theme}/{template_name}')
    else:
        return render_template(template_name)
