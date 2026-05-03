import time

from flask import current_app, request, jsonify
from flask_smorest import abort

from app import limiter
from app.api.alpha import bp, site_bp, misc_bp, comm_bp, feed_bp, topic_bp, user_bp, \
    reply_bp, post_bp, private_message_bp, upload_bp, admin_bp
from app.api.alpha.utils.community import get_community, get_community_list, post_community_follow, \
    post_community_block, post_community, put_community, put_community_subscribe, post_community_delete, \
    get_community_moderate_bans, put_community_moderate_unban, post_community_moderate_ban, \
    post_community_moderate_post_nsfw, post_community_mod, post_community_flair_create, put_community_flair_edit, \
    post_community_flair_delete, post_community_leave_all
from app.api.alpha.utils.domain import post_domain_block
from app.api.alpha.utils.feed import get_feed_list, get_feed, post_feed_follow, post_feed, put_feed, post_feed_delete
from app.api.alpha.utils.misc import get_search, get_resolve_object, get_suggestion, get_modlog
from app.api.alpha.utils.post import get_post_list, get_post, post_post_like, put_post_save, put_post_subscribe, \
    post_post, put_post, post_post_delete, post_post_report, post_post_lock, post_post_feature, post_post_remove, \
    post_post_mark_as_read, get_post_replies, get_post_like_list, put_post_set_flair, get_post_list2, post_poll_vote, \
    post_post_hide
from app.api.alpha.utils.private_message import get_private_message_list, post_private_message, \
    post_private_message_mark_as_read, get_private_message_conversation, put_private_message, post_private_message_delete, \
    post_private_message_report, post_leave_conversation
from app.api.alpha.utils.reply import get_reply_list, post_reply_like, put_reply_save, put_reply_subscribe, post_reply, \
    put_reply, post_reply_delete, post_reply_report, post_reply_remove, post_reply_mark_as_read, get_reply, \
    post_reply_lock, get_reply_like_list, post_reply_mark_as_answer, get_reply_report_list
from app.api.alpha.utils.site import get_site, post_site_block, get_federated_instances, get_site_instance_chooser, \
    get_site_instance_chooser_search, get_site_version, get_site_metadata
from app.api.alpha.utils.topic import get_topic_list
from app.api.alpha.utils.upload import post_upload_image, post_upload_community_image, post_upload_user_image, \
    post_image_delete
from app.api.alpha.utils.user import get_user, post_user_block, get_user_unread_count, get_user_replies, \
    post_user_mark_all_as_read, put_user_subscribe, put_user_save_user_settings, \
    get_user_notifications, put_user_notification_state, get_user_notifications_count, \
    put_user_mark_all_notifications_read, post_user_verify_credentials, post_user_set_flair, get_user_details, \
    get_user_media, post_user_set_note, post_user_ban, post_user_unban, post_user_register, get_user_captcha
from app.api.alpha.utils.admin import get_registration_list, put_registration_approve
from app.constants import *
from app.utils import orjson_response, get_setting
from app.api.alpha.schema import *


def enable_api():
    return True if current_app.debug or current_app.config['ENABLE_ALPHA_API'] == 'true' else False


def is_trusted_request():
    if current_app.debug:
        return True
    if request.remote_addr in current_app.config['SKIP_RATE_LIMIT_IPS']:
        return True
    return False


get_site_response = GetSiteResponse()


# Site
@site_bp.route('/site', methods=['GET'])
@site_bp.doc(summary="Gets the site, and your user data.")
@site_bp.response(200, GetSiteResponse)
@site_bp.alt_response(400, schema=DefaultError)
def get_alpha_site():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    with limiter.limit('20/minute'):
        resp = get_site(auth)
    validated = get_site_response.load(resp)
    return orjson_response(validated)


@site_bp.route('/site/version', methods=['GET'])
@site_bp.doc(summary="Gets version of PieFed.")
@site_bp.response(200, GetSiteVersionResponse)
@site_bp.alt_response(400, schema=DefaultError)
def get_alpha_site_version():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_site_version(auth)
    return GetSiteVersionResponse().load(resp)


@site_bp.route('/site/block', methods=['POST'])
@site_bp.doc(summary="Block an instance.")
@site_bp.arguments(BlockInstanceRequest)
@site_bp.response(200, BlockInstanceResponse)
@site_bp.alt_response(400, schema=DefaultError)
def post_alpha_site_block(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_site_block(auth, data)
    return BlockInstanceResponse().load(resp)


# Site instance chooser
@site_bp.route('/site/instance_chooser', methods=['GET'])
@site_bp.doc(summary="Gets the site info for use by other instances in the Instance Chooser functionality.")
@site_bp.response(200, GetSiteInstanceChooserResponse)
@site_bp.alt_response(400, schema=DefaultError)
def get_alpha_site_instance_chooser():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    if get_setting('enable_instance_chooser', False):
        auth = request.headers.get('Authorization')
        resp = get_site_instance_chooser(auth)
        return GetSiteInstanceChooserResponse().load(resp)
    else:
        return abort(404)


@site_bp.route('/site/instance_chooser_search', methods=['GET'])
@site_bp.doc(summary="Search for other instances.")
@site_bp.arguments(SearchInstanceChooser, location="query")
@site_bp.response(200, GetSiteInstanceChooserSearchResponse)
@site_bp.alt_response(400, schema=DefaultError)
def get_alpha_site_instance_chooser_search(data):
    if get_setting('enable_instance_chooser', False):
        resp = get_site_instance_chooser_search(data)
        return GetSiteInstanceChooserSearchResponse().load(resp)
    else:
        return abort(404)


# Misc
@misc_bp.route('/search', methods=["GET"])
@misc_bp.doc(summary="Search PieFed.")
@misc_bp.arguments(SearchRequest, location="query")
@misc_bp.response(200, SearchResponse)
@misc_bp.alt_response(400, schema=DefaultError)
def get_alpha_search(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_search(auth, data)
    return SearchResponse().load(resp)


@misc_bp.route('/resolve_object', methods=['GET'])
@misc_bp.doc(summary="Fetch a non-local / federated object.")
@misc_bp.arguments(ResolveObjectRequest, location="query")
@misc_bp.response(200, ResolveObjectResponse)
@misc_bp.alt_response(400, schema=DefaultError)
def get_alpha_resolve_object(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_resolve_object(auth, data)
    return ResolveObjectResponse().load(resp)



@misc_bp.route('/federated_instances', methods=['GET'])
@misc_bp.doc(summary="Fetch federated instances.")
@misc_bp.response(200, GetFederatedInstancesResponse)
@misc_bp.alt_response(400, schema=DefaultError)
def get_alpha_federated_instances():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    data = {"include_federation_state": False}
    resp = get_federated_instances(data)
    return GetFederatedInstancesResponse().load(resp)


@misc_bp.route('/suggest_completion', methods=['GET'])
@misc_bp.doc(summary="Suggest people and communities while users type.")
@comm_bp.arguments(GetSuggestCompletionRequest, location="query")
@misc_bp.response(200, GetSuggestCompletionResponse)
@misc_bp.alt_response(400, schema=DefaultError)
def get_alpha_suggest_completion(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    resp = get_suggestion(data)
    return GetSuggestCompletionResponse().load(resp)


# Community
@comm_bp.route("/community", methods=["GET"])
@comm_bp.doc(summary="Get / fetch a community.")
@comm_bp.arguments(GetCommunityRequest, location="query")
@comm_bp.response(200, GetCommunityResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def get_alpha_community(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_community(auth, data)
    return GetCommunityResponse().load(resp)


@comm_bp.route("/community/list", methods=["GET"])
@comm_bp.doc(summary="List communities, with various filters.")
@comm_bp.arguments(ListCommunitiesRequest, location="query")
@comm_bp.response(200, ListCommunitiesResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def get_alpha_community_list(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_community_list(auth, data)
    return ListCommunitiesResponse().load(resp)


@comm_bp.route("/community/follow", methods=["POST"])
@comm_bp.doc(summary="Follow / subscribe to a community.")
@comm_bp.arguments(FollowCommunityRequest)
@comm_bp.response(200, CommunityResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def post_alpha_community_follow(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_community_follow(auth, data)
    return CommunityResponse().load(resp)


@comm_bp.route("/community/leave_all", methods=["POST"])
@comm_bp.doc(summary="Leave all communities and feeds for which the user is not a moderator or owner.")
@comm_bp.response(200, UserMeResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def post_alpha_leave_all():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_community_leave_all(auth)
    return UserMeResponse().load(resp)


@comm_bp.route("/community/block", methods=["POST"])
@comm_bp.doc(summary="Block a community.")
@comm_bp.arguments(BlockCommunityRequest)
@comm_bp.response(200, BlockCommunityResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def post_alpha_community_block(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_community_block(auth, data)
    return BlockCommunityResponse().load(resp)


@comm_bp.route("/community", methods=["POST"])
@comm_bp.doc(summary="Create a new community.")
@comm_bp.arguments(CreateCommunityRequest)
@comm_bp.response(200, CommunityResponse)
@comm_bp.alt_response(400, schema=DefaultError)
@comm_bp.alt_response(429, schema=DefaultError)
def post_alpha_community(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    with limiter.limit('10/day'):
        auth = request.headers.get('Authorization')
        resp = post_community(auth, data)
        return CommunityResponse().load(resp)


@comm_bp.route("/community", methods=["PUT"])
@comm_bp.doc(summary="Edit community.")
@comm_bp.arguments(EditCommunityRequest)
@comm_bp.response(200, CommunityResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def put_alpha_community(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_community(auth, data)
    return CommunityResponse().load(resp)


@comm_bp.route("/community/subscribe", methods=["PUT"])
@comm_bp.doc(summary="Subscribe to activities in a community.")
@comm_bp.arguments(SubscribeCommunityRequest)
@comm_bp.response(200, CommunityResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def put_alpha_community_subscribe(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_community_subscribe(auth, data)
    return CommunityResponse().load(resp)


@comm_bp.route("/community/delete", methods=["POST"])
@comm_bp.doc(summary="Delete a community.")
@comm_bp.arguments(DeleteCommunityRequest)
@comm_bp.response(200, CommunityResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def post_alpha_community_delete(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_community_delete(auth, data)
    return CommunityResponse().load(resp)


@comm_bp.route('/community/mod', methods=['POST'])
@comm_bp.doc(summary="Add or remove a moderator for your community.")
@comm_bp.arguments(ModCommunityRequest)
@comm_bp.response(200, ModCommunityResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def post_alpha_community_mod(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_community_mod(auth, data)
    return ModCommunityResponse().load(resp)


@comm_bp.route('/community/moderate/bans', methods=['GET'])
@comm_bp.doc(summary="Get the list of banned users for a community.")
@comm_bp.arguments(CommunityModerationBansListRequest, location="query")
@comm_bp.response(200, CommunityModerationBansListResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def get_alpha_community_moderate_bans(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_community_moderate_bans(auth, data)
    return CommunityModerationBansListResponse().load(resp)


@comm_bp.route('/community/moderate/unban', methods=['PUT'])
@comm_bp.doc(summary="Unban a user from a community.")
@comm_bp.arguments(CommunityModerationUnbanRequest)
@comm_bp.response(200, CommunityModerationBanItem)
@comm_bp.alt_response(400, schema=DefaultError)
def put_alpha_community_moderate_unban(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_community_moderate_unban(auth, data)
    return CommunityModerationBanItem().load(resp)


@comm_bp.route('/community/moderate/ban', methods=['POST'])
@comm_bp.doc(summary="Ban a user from a community.")
@comm_bp.arguments(CommunityModerationBanRequest)
@comm_bp.response(200, CommunityModerationBanItem)
@comm_bp.alt_response(400, schema=DefaultError)
def post_alpha_community_moderate_ban(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_community_moderate_ban(auth, data)
    return CommunityModerationBanItem().load(resp)


@comm_bp.route('/community/moderate/post/nsfw', methods=['POST'])
@comm_bp.doc(summary="Mark or unmark a post as NSFW.")
@comm_bp.arguments(CommunityModerationNsfwRequest)
@comm_bp.response(200, PostView)
@comm_bp.alt_response(400, schema=DefaultError)
def post_alpha_community_moderate_post_nsfw(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_community_moderate_post_nsfw(auth, data)
    return PostView().load(resp)


@comm_bp.route('/community/flair', methods=['POST'])
@comm_bp.doc(summary="Create a new post flair in the community")
@comm_bp.arguments(CommunityFlairCreateRequest)
@comm_bp.response(200, CommunityFlairCreateResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def post_alpha_community_flair(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_community_flair_create(auth, data)
    return CommunityFlairCreateResponse().load(resp)


@comm_bp.route('/community/flair', methods=['PUT'])
@comm_bp.doc(summary="Edit an existing post flair in the community")
@comm_bp.arguments(CommunityFlairEditRequest)
@comm_bp.response(200, CommunityFlairEditResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def put_alpha_community_flair(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_community_flair_edit(auth, data)
    return CommunityFlairEditResponse().load(resp)


@comm_bp.route('/community/flair/delete', methods=['POST'])
@comm_bp.doc(summary="Delete a post flair in a community")
@comm_bp.arguments(CommunityFlairDeleteRequest)
@comm_bp.response(200, CommunityFlairDeleteResponse)
@comm_bp.alt_response(400, schema=DefaultError)
def post_alpha_community_flair_delete(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_community_flair_delete(auth, data)
    return CommunityFlairDeleteResponse().load(resp)


# Feed
@feed_bp.route('/feed', methods=["GET"])
@feed_bp.doc(summary="Get a feed")
@feed_bp.arguments(GetFeedRequest, location="query")
@feed_bp.response(200, FeedView)
@feed_bp.alt_response(400, schema=DefaultError)
def get_alpha_feed(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_feed(auth, data)
    validated = FeedView().load(resp)
    return orjson_response(validated)


@feed_bp.route('/feed/list', methods=["GET"])
@feed_bp.doc(summary="Get list of feeds")
@feed_bp.arguments(FeedListRequest, location="query")
@feed_bp.response(200, FeedListResponse)
@feed_bp.alt_response(400, schema=DefaultError)
def get_alpha_feed_list(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_feed_list(auth, data)
    validated = FeedListResponse().load(resp)
    return orjson_response(validated)


@feed_bp.route("/feed/follow", methods=["POST"])
@feed_bp.doc(summary="Follow / subscribe / leave / unsubscribe to a feed.")
@feed_bp.arguments(FollowFeedRequest)
@feed_bp.response(200, FeedView)
@feed_bp.alt_response(400, schema=DefaultError)
def post_alpha_feed_follow(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    feed = post_feed_follow(auth, data)
    resp = get_feed(auth, {'id': feed.id})
    validated = FeedView().load(resp)
    return orjson_response(validated)


@feed_bp.route("/feed", methods=["POST"])
@feed_bp.doc(summary="Create a new feed.")
@feed_bp.arguments(CreateFeedRequest)
@feed_bp.response(200, FeedView)
@feed_bp.alt_response(400, schema=DefaultError)
@feed_bp.alt_response(429, schema=DefaultError)
def post_alpha_feed(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    with limiter.limit('10/day'):
        auth = request.headers.get('Authorization')
        resp = post_feed(auth, data)
        return FeedView().load(resp)


@feed_bp.route("/feed", methods=["PUT"])
@feed_bp.doc(summary="Edit feed.")
@feed_bp.arguments(EditFeedRequest)
@feed_bp.response(200, FeedView)
@feed_bp.alt_response(400, schema=DefaultError)
def put_alpha_feed(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_feed(auth, data)
    return FeedView().load(resp)


@feed_bp.route("/feed/delete", methods=["POST"])
@feed_bp.doc(summary="Delete a feed.")
@feed_bp.arguments(DeleteFeedRequest)
@feed_bp.response(200, FeedView)
@feed_bp.alt_response(400, schema=DefaultError)
def post_alpha_feed_delete(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_feed_delete(auth, data)
    return FeedView().load(resp)


list_posts_response = ListPostsResponse()


# Post
@post_bp.route('/post/list', methods=['GET'])
@post_bp.doc(summary="List posts.")
@post_bp.arguments(ListPostsRequest, location="query", unknown=INCLUDE)
@post_bp.response(200, ListPostsResponse)
@post_bp.alt_response(400, schema=DefaultError)
def get_alpha_post_list(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_post_list(auth, data)
    if data.get('debug'):
        validated = list_posts_response.load(resp)
        return orjson_response(validated)
    else:
        return orjson_response(resp)


@post_bp.route('/post/list2', methods=['GET'])
@post_bp.doc(summary="List posts. For testing only, do not use.")
@post_bp.arguments(ListPostsRequest2, location="query", unknown=INCLUDE)
@post_bp.response(200, ListPostsResponse)
@post_bp.alt_response(400, schema=DefaultError)
def get_alpha_post_list2(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_post_list2(auth, data)
    if data.get('debug'):
        validated = ListPostsResponse().load(resp)
        return orjson_response(validated)
    else:
        return orjson_response(resp)


@post_bp.route('/post', methods=['GET'])
@post_bp.doc(summary="Get/fetch a post")
@post_bp.arguments(GetPostRequest, location="query")
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
def get_alpha_post(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_post(auth, data)
    return GetPostResponse().load(resp)


get_post_replies_response = GetPostRepliesResponse()


@post_bp.route('/post/replies', methods=['GET'])
@post_bp.doc(summary="Get replies/comments for a post with nested structure.")
@post_bp.arguments(GetPostRepliesRequest, location="query")
@post_bp.response(200, GetPostRepliesResponse)
@post_bp.alt_response(400, schema=DefaultError)
def get_alpha_post_replies(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_post_replies(auth, data)
    validated = get_post_replies_response.load(resp)
    return orjson_response(validated)


@post_bp.route('/post/site_metadata', methods=['GET'])  #
@post_bp.doc(summary="Get metadata about a url.")
@post_bp.arguments(GetSiteMetadataRequest, location="query")
@post_bp.response(200, GetSiteMetadataResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_metadata(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    with limiter.limit('20/minute'):
        return GetSiteMetadataResponse().load(get_site_metadata(auth, data))


@post_bp.route('/post/like', methods=['POST'])
@post_bp.doc(summary="Like or unlike a post.")
@post_bp.arguments(LikePostRequest)
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_like(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    return GetPostResponse().load(post_post_like(auth, data))


@post_bp.route('/post/save', methods=['PUT'])
@post_bp.doc(summary="Save or unsave a post.")
@post_bp.arguments(SavePostRequest)
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
def put_alpha_post_save(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_post_save(auth, data)
    return GetPostResponse().load(resp)


@post_bp.route('/post/subscribe', methods=['PUT'])
@post_bp.doc(summary="Subscribe or unsubscribe to a post.")
@post_bp.arguments(SubscribePostRequest)
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
def put_alpha_post_subscribe(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_post_subscribe(auth, data)
    return GetPostResponse().load(resp)


@post_bp.route('/post', methods=['POST'])
@post_bp.doc(summary="Create a new post.")
@post_bp.arguments(CreatePostRequest)
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
@post_bp.alt_response(429, schema=DefaultError)
def post_alpha_post(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    with limiter.limit('3/minute'):
        auth = request.headers.get('Authorization')
        resp = post_post(auth, data)
        return GetPostResponse().load(resp)


@post_bp.route('/post', methods=['PUT'])
@post_bp.doc(summary="Edit a post.")
@post_bp.arguments(EditPostRequest)
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
def put_alpha_post(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_post(auth, data)
    return GetPostResponse().load(resp)


@post_bp.route('/post/delete', methods=['POST'])
@post_bp.doc(summary="Delete or restore a post.")
@post_bp.arguments(DeletePostRequest)
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_delete(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_post_delete(auth, data)
    return GetPostResponse().load(resp)


@post_bp.route('/post/report', methods=['POST'])
@post_bp.doc(summary="Report a post.")
@post_bp.arguments(ReportPostRequest)
@post_bp.response(200, PostReportResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_report(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_post_report(auth, data)
    return PostReportResponse().load(resp)


@post_bp.route('/post/lock', methods=['POST'])
@post_bp.doc(summary="Lock or unlock a post.")
@post_bp.arguments(LockPostRequest)
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_lock(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_post_lock(auth, data)
    return GetPostResponse().load(resp)


@post_bp.route('/post/hide', methods=['POST'])
@post_bp.doc(summary="Hide or unhide a post.")
@post_bp.arguments(HidePostRequest)
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_hide(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_post_hide(auth, data)
    return GetPostResponse().load(resp)


@post_bp.route('/post/feature', methods=['POST'])
@post_bp.doc(summary="Feature or unfeature a post.")
@post_bp.arguments(FeaturePostRequest)
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_feature(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_post_feature(auth, data)
    return GetPostResponse().load(resp)


@post_bp.route('/post/remove', methods=['POST'])
@post_bp.doc(summary="Remove or restore a post as a moderator.")
@post_bp.arguments(RemovePostRequest)
@post_bp.response(200, GetPostResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_remove(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_post_remove(auth, data)
    return GetPostResponse().load(resp)


@post_bp.route('/post/mark_as_read', methods=['POST'])
@post_bp.doc(summary="Mark one or more posts as read or unread.")
@post_bp.arguments(MarkPostAsReadRequest)
@post_bp.response(200, SuccessResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_mark_as_read(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_post_mark_as_read(auth, data)
    return SuccessResponse().load(resp)


@post_bp.route('/post/like/list', methods=['GET'])
@post_bp.doc(summary="View post votes as a moderator.")
@post_bp.arguments(ListPostLikesRequest, location="query")
@post_bp.response(200, ListPostLikesResponse)
@post_bp.alt_response(400, schema=DefaultError)
def get_alpha_post_like_list(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_post_like_list(auth, data)
    validated = ListPostLikesResponse().load(resp)
    return orjson_response(validated)


@post_bp.route('/post/assign_flair', methods=['POST'])
@post_bp.doc(summary="Add/remove flair from a post")
@post_bp.arguments(PostSetFlairRequest)
@post_bp.response(200, PostSetFlairResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_set_flair(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_post_set_flair(auth, data)
    return PostSetFlairResponse().load(resp)


@post_bp.route('/post/poll_vote', methods=['POST'])
@post_bp.doc(summary="Vote in a poll")
@post_bp.arguments(PollVoteRequest)
@post_bp.response(200, PollVoteResponse)
@post_bp.alt_response(400, schema=DefaultError)
def post_alpha_post_poll_vote(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_poll_vote(auth, data)
    return PollVoteResponse().load(resp)


list_comments_response = ListCommentsResponse()


# Reply
@reply_bp.route('/comment/list', methods=['GET'])
@reply_bp.doc(summary="List comments, with various filters.")
@reply_bp.arguments(ListCommentsRequest, location="query")
@reply_bp.response(200, ListCommentsResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def get_alpha_comment_list(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_reply_list(auth, data)
    if data.get('debug'):
        validated = list_comments_response.load(resp)
        return orjson_response(validated)
    else:
        return orjson_response(resp)


@reply_bp.route('/comment/like', methods=['POST'])
@reply_bp.doc(summary="Like / vote on a comment.")
@reply_bp.arguments(LikeCommentRequest)
@reply_bp.response(200, GetCommentResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def post_alpha_comment_like(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_reply_like(auth, data)
    return GetCommentResponse().load(resp)


@reply_bp.route('/comment/save', methods=['PUT'])
@reply_bp.doc(summary="Save a comment.")
@reply_bp.arguments(SaveCommentRequest)
@reply_bp.response(200, GetCommentResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def put_alpha_comment_save(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_reply_save(auth, data)
    return GetCommentResponse().load(resp)


@reply_bp.route('/comment/subscribe', methods=['PUT'])
@reply_bp.doc(summary="Subscribe to a comment.")
@reply_bp.arguments(SubscribeCommentRequest)
@reply_bp.response(200, GetCommentResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def put_alpha_comment_subscribe(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_reply_subscribe(auth, data)
    return GetCommentResponse().load(resp)


@reply_bp.route('/comment', methods=['POST'])
@reply_bp.doc(summary="Create a comment.")
@reply_bp.arguments(CreateCommentRequest)
@reply_bp.response(200, GetCommentResponse)
@reply_bp.alt_response(400, schema=DefaultError)
@reply_bp.alt_response(429, schema=DefaultError)
def post_alpha_comment(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    with limiter.limit('3/minute'):
        auth = request.headers.get('Authorization')
        resp = post_reply(auth, data)
        return GetCommentResponse().load(resp)


@reply_bp.route('/comment', methods=['PUT'])
@reply_bp.doc(summary="Edit a comment.")
@reply_bp.arguments(EditCommentRequest)
@reply_bp.response(200, GetCommentResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def put_alpha_comment(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_reply(auth, data)
    return GetCommentResponse().load(resp)


@reply_bp.route('/comment/delete', methods=['POST'])
@reply_bp.doc(summary="Delete a comment.")
@reply_bp.arguments(DeleteCommentRequest)
@reply_bp.response(200, GetCommentResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def post_alpha_comment_delete(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_reply_delete(auth, data)
    return GetCommentResponse().load(resp)


@reply_bp.route('/comment/report', methods=['POST'])
@reply_bp.doc(summary="Report a comment.")
@reply_bp.arguments(ReportCommentRequest)
@reply_bp.response(200, GetCommentReportResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def post_alpha_comment_report(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_reply_report(auth, data)
    return GetCommentReportResponse().load(resp)


@reply_bp.route('/comment/report/list', methods=['GET'])
@reply_bp.doc(summary="Get list of comment reports.")
@reply_bp.arguments(GetCommentReportListRequest, location="query")
@reply_bp.response(200, GetCommentReportListResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def get_alpha_comment_report_list(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_reply_report_list(auth, data)
    return GetCommentReportListResponse().load(resp)


@reply_bp.route('/comment/remove', methods=['POST'])
@reply_bp.doc(summary="Remove a comment as a moderator.")
@reply_bp.arguments(RemoveCommentRequest)
@reply_bp.response(200, GetCommentResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def post_alpha_comment_remove(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_reply_remove(auth, data)
    return GetCommentResponse().load(resp)


@reply_bp.route('/comment/mark_as_read', methods=['POST'])
@reply_bp.doc(summary="Mark a comment reply as read.")
@reply_bp.arguments(MarkCommentAsReadRequest)
@reply_bp.response(200, GetCommentReplyResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def post_alpha_comment_mark_as_read(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_reply_mark_as_read(auth, data)
    return GetCommentReplyResponse().load(resp)


@reply_bp.route('/comment/mark_as_answer', methods=['POST'])
@reply_bp.doc(summary="Mark a comment as the preferred answer to a question.")
@reply_bp.arguments(MarkCommentAsAnswerRequest)
@reply_bp.response(200, GetCommentReplyResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def post_alpha_comment_mark_as_answer(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_reply_mark_as_answer(auth, data)
    return GetCommentReplyResponse().load(resp)


@reply_bp.route("/comment", methods=["GET"])
@reply_bp.doc(summary="Get / fetch a comment.")
@reply_bp.arguments(GetCommentRequest, location="query")
@reply_bp.response(200, GetCommentResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def get_alpha_comment(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_reply(auth, data)
    return GetCommentResponse().load(resp)


@reply_bp.route('/comment/lock', methods=['POST'])
@reply_bp.doc(summary="Lock a comment chain as a moderator.")
@reply_bp.arguments(LockCommentRequest)
@reply_bp.response(200, GetCommentResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def post_alpha_comment_lock(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_reply_lock(auth, data)
    return GetCommentResponse().load(resp)


@reply_bp.route('/comment/like/list', methods=['GET'])
@reply_bp.doc(summary="View comment votes as a moderator.")
@reply_bp.arguments(ListCommentLikesRequest, location="query")
@reply_bp.response(200, ListCommentLikesResponse)
@reply_bp.alt_response(400, schema=DefaultError)
def get_alpha_comment_like_list(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_reply_like_list(auth, data)
    validated = ListCommentLikesResponse().load(resp)
    return orjson_response(validated)


# Private Message
@private_message_bp.route('/private_message/list', methods=['GET'])
@private_message_bp.doc(summary="List private messages.")
@private_message_bp.arguments(ListPrivateMessagesRequest, location="query")
@private_message_bp.response(200, ListPrivateMessagesResponse)
@private_message_bp.alt_response(400, schema=DefaultError)
def get_alpha_private_message_list(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_private_message_list(auth, data)
    return ListPrivateMessagesResponse().load(resp)


@private_message_bp.route('/private_message/conversation', methods=['GET'])
@private_message_bp.doc(summary="Get conversation with a specific person.")
@private_message_bp.arguments(GetPrivateMessageConversationRequest, location="query")
@private_message_bp.response(200, GetPrivateMessageConversationResponse)
@private_message_bp.alt_response(400, schema=DefaultError)
def get_alpha_private_message_conversation(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_private_message_conversation(auth, data)
    return GetPrivateMessageConversationResponse().load(resp)


@private_message_bp.route('/private_message/conversation/leave', methods=['POST'])
@private_message_bp.doc(summary="Leave a conversation")
@private_message_bp.arguments(LeaveConversationRequest)
@private_message_bp.response(200)
@private_message_bp.alt_response(400, schema=DefaultError)
def post_alpha_conversation_leave(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    post_leave_conversation(auth, data)


@private_message_bp.route('/private_message', methods=['POST'])
@private_message_bp.doc(summary="Create a new private message.")
@private_message_bp.arguments(CreatePrivateMessageRequest)
@private_message_bp.response(200, PrivateMessageResponse)
@private_message_bp.alt_response(400, schema=DefaultError)
@private_message_bp.alt_response(429, schema=DefaultError)
def post_alpha_private_message(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    with limiter.limit('3/minute'):
        auth = request.headers.get('Authorization')
        resp = post_private_message(auth, data)
        return PrivateMessageResponse().load(resp)


@private_message_bp.route('/private_message', methods=['PUT'])
@private_message_bp.doc(summary="Edit a private message.")
@private_message_bp.arguments(EditPrivateMessageRequest)
@private_message_bp.response(200, PrivateMessageResponse)
@private_message_bp.alt_response(400, schema=DefaultError)
def put_alpha_private_message(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_private_message(auth, data)
    return PrivateMessageResponse().load(resp)


@private_message_bp.route('/private_message/mark_as_read', methods=['POST'])
@private_message_bp.doc(summary="Mark a private message as read or unread.")
@private_message_bp.arguments(MarkPrivateMessageAsReadRequest)
@private_message_bp.response(200, PrivateMessageResponse)
@private_message_bp.alt_response(400, schema=DefaultError)
def post_alpha_private_message_mark_as_read(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_private_message_mark_as_read(auth, data)
    return PrivateMessageResponse().load(resp)


@private_message_bp.route('/private_message/delete', methods=['POST'])
@private_message_bp.doc(summary="Delete or restore a private message.")
@private_message_bp.arguments(DeletePrivateMessageRequest)
@private_message_bp.response(200, PrivateMessageResponse)
@private_message_bp.alt_response(400, schema=DefaultError)
def post_alpha_private_message_delete(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_private_message_delete(auth, data)
    return PrivateMessageResponse().load(resp)


@private_message_bp.route('/private_message/report', methods=['POST'])
@private_message_bp.doc(summary="Report a private message.")
@private_message_bp.arguments(ReportPrivateMessageRequest)
@private_message_bp.response(200, PrivateMessageResponse)
@private_message_bp.alt_response(400, schema=DefaultError)
def post_alpha_private_message_report(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_private_message_report(auth, data)
    return PrivateMessageResponse().load(resp)


# Topic
@topic_bp.route('/topic/list', methods=["GET"])
@topic_bp.doc(summary="Get list of topics")
@topic_bp.arguments(TopicListRequest, location="query")
@topic_bp.response(200, TopicListResponse)
@topic_bp.alt_response(400, schema=DefaultError)
def get_alpha_topic_list(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_topic_list(auth, data)
    validated = TopicListResponse().load(resp)
    return orjson_response(validated)


# Domain
@user_bp.route('/domain/block', methods=['POST'])
@user_bp.doc(summary="Block or unblock a domain")
@user_bp.arguments(DomainBlockRequest)
@user_bp.response(200, DomainBlockResponse)
@user_bp.alt_response(400, schema=DefaultError)
def post_alpha_domain_block(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_domain_block(auth, data)
    return {'blocked': resp}


# User
@user_bp.route("/user", methods=["GET"])
@user_bp.doc(summary="Get the details for a person")
@user_bp.arguments(GetUserRequest, location="query")
@user_bp.response(200, GetUserResponse)
@user_bp.alt_response(400, schema=DefaultError)
def get_alpha_user(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_user(auth, data)
    validated = GetUserResponse().load(resp)
    return orjson_response(validated)


@user_bp.route("/user/me", methods=["GET"])
@user_bp.doc(summary="Get the details for the current user")
@user_bp.response(200, UserMeResponse)
@user_bp.alt_response(400, schema=DefaultError)
def get_alpha_user_details():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_user_details(auth)
    validated = UserMeResponse().load(resp)
    return orjson_response(validated)


@user_bp.route("/user/login", methods=["POST"])
@user_bp.doc(summary="Log into PieFed")
@user_bp.arguments(UserLoginRequest)
@user_bp.response(200, UserLoginResponse)
@user_bp.alt_response(400, schema=DefaultError)
@user_bp.alt_response(429, schema=DefaultError)
def post_alpha_user_login(data):
    from app.shared.auth import log_user_in
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    with limiter.limit('20/hour', exempt_when=is_trusted_request):
        resp = log_user_in(data, SRC_API)
        return UserLoginResponse().load(resp)


@user_bp.route('/user/unread_count', methods=['GET'])
@user_bp.doc(summary="Get your unread counts")
@user_bp.response(200, UserUnreadCountsResponse)
@user_bp.alt_response(400, schema=DefaultError)
def get_alpha_user_unread_count():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_user_unread_count(auth)
    return UserUnreadCountsResponse().load(resp)


@user_bp.route('/user/replies', methods=['GET'])
@user_bp.doc(summary="Get comment replies")
@user_bp.arguments(UserRepliesRequest, location="query")
@user_bp.response(200, UserRepliesResponse)
@user_bp.alt_response(400, schema=DefaultError)
def get_alpha_user_replies(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_user_replies(auth, data)
    return UserRepliesResponse().load(resp)


@user_bp.route('/user/mentions', methods=['GET'])
@user_bp.doc(summary="Get mentions of your account made in comments")
@user_bp.arguments(UserMentionsRequest, location="query")
@user_bp.response(200, UserMentionsResponse)
@user_bp.alt_response(400, schema=DefaultError)
def get_alpha_user_mentions(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_user_replies(auth, data, mentions=True)
    return UserMentionsResponse().load(resp)


@user_bp.route('/user/media', methods=['GET'])
@user_bp.doc(summary="Get media the current user has uploaded")
@user_bp.arguments(UserMediaRequest, location="query")
@user_bp.response(200, UserMediaResponse)
@user_bp.alt_response(400, schema=DefaultError)
def get_alpha_user_media(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_user_media(auth, data)
    return UserMediaResponse().load(resp)


@user_bp.route('/user/block', methods=['POST'])
@user_bp.doc(summary="Block or unblock a person")
@user_bp.arguments(UserBlockRequest)
@user_bp.response(200, UserBlockResponse)
@user_bp.alt_response(400, schema=DefaultError)
def post_alpha_user_block(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_user_block(auth, data)
    return UserBlockResponse().load(resp)


@user_bp.route('/user/mark_all_as_read', methods=['POST'])
@user_bp.doc(summary="Mark all notifications and messages as read")
@user_bp.response(200, UserMarkAllReadResponse)
@user_bp.alt_response(400, schema=DefaultError)
def post_alpha_user_mark_all_as_read():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_user_mark_all_as_read(auth)
    return UserMarkAllReadResponse().load(resp)


@user_bp.route('/user/subscribe', methods=['PUT'])
@user_bp.doc(summary="Subscribe or unsubscribe to activites of another user")
@user_bp.arguments(UserSubscribeRequest)
@user_bp.response(200, UserSubscribeResponse)
@user_bp.alt_response(400, schema=DefaultError)
def put_alpha_user_subscribe(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_user_subscribe(auth, data)
    return UserSubscribeResponse().load(resp)


# not all settings implemented yet, nor all choices for settings
@user_bp.route('/user/save_user_settings', methods=['PUT'])
@user_bp.doc(summary="Save your user settings")
@user_bp.arguments(UserSaveSettingsRequest)
@user_bp.response(200, UserSaveSettingsResponse)
@user_bp.alt_response(400, schema=DefaultError)
def put_alpha_user_save_user_settings(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_user_save_user_settings(auth, data)
    return UserSaveSettingsResponse().load(resp)


@user_bp.route('/user/notifications', methods=['GET'])
@user_bp.doc(summary="Get your user notifications (not all notification types supported yet)")
@user_bp.arguments(UserNotificationsRequest, location="query")
@user_bp.response(200, UserNotificationsResponse)
@user_bp.alt_response(400, schema=DefaultError)
def get_alpha_user_notifications(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_user_notifications(auth, data)
    return UserNotificationsResponse().load(resp)


@user_bp.route('/user/notification_state', methods=['PUT'])
@user_bp.doc(summary="Set the read status of a given notification (not all notification types supported yet)")
@user_bp.arguments(UserNotificationStateRequest)
@user_bp.response(200, UserNotificationItemView)
@user_bp.alt_response(400, schema=DefaultError)
def put_alpha_user_notification_state(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_user_notification_state(auth, data)
    return UserNotificationItemView().load(resp)


@user_bp.route('/user/notifications_count', methods=['GET'])
@user_bp.doc(summary="Get user unread notifications count")
@user_bp.response(200, UserNotificationsCountResponse)
@user_bp.alt_response(400, schema=DefaultError)
def get_alpha_user_notifications_count():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_user_notifications_count(auth)
    return UserNotificationsCountResponse().load(resp)


@user_bp.route('/user/mark_all_notifications_read', methods=['PUT'])
@user_bp.doc(summary="Mark all notifications as read")
@user_bp.response(200, UserMarkAllNotifsReadResponse)
@user_bp.alt_response(400, schema=DefaultError)
def put_alpha_user_notifications_read():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = put_user_mark_all_notifications_read(auth)
    return UserMarkAllNotifsReadResponse().load(resp)


@user_bp.route('/user/verify_credentials', methods=['POST'])
@user_bp.doc(summary="Verify username/password credentials")
@user_bp.arguments(UserLoginRequest)
@user_bp.response(200)
@user_bp.alt_response(400, schema=DefaultError)
def post_alpha_user_verify_credentials(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    with limiter.limit('6/hour', exempt_when=is_trusted_request):
        post_user_verify_credentials(data)


@user_bp.route('/user/set_flair', methods=['POST'])
@user_bp.doc(summary="Set your flair for a community")
@user_bp.arguments(UserSetFlairRequest)
@user_bp.response(200, UserSetFlairResponse)
@user_bp.alt_response(400, schema=DefaultError)
def post_alpha_user_set_flair(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_user_set_flair(auth, data)
    return UserSetFlairResponse().load(resp)

@user_bp.route('/user/note', methods=['POST'])
@user_bp.doc(summary="Set a note for a user")
@user_bp.arguments(UserSetNoteRequest)
@user_bp.response(200, UserSetNoteResponse)
@user_bp.alt_response(400, schema=DefaultError)
def post_alpha_user_note(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_user_set_note(auth, data)
    return UserSetNoteResponse().load(resp)


@user_bp.route('/user/ban', methods=['POST'])
@user_bp.doc(summary="Ban a user")
@user_bp.arguments(UserBanRequest)
@user_bp.response(200, UserBanResponse)
@user_bp.alt_response(400, schema=DefaultError)
def post_alpha_user_ban(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_user_ban(auth, data)
    return UserBanResponse().load(resp)


@user_bp.route('/user/unban', methods=['POST'])
@user_bp.doc(summary="Unban a user")
@user_bp.arguments(UserUnbanRequest)
@user_bp.response(200, UserBanResponse)
@user_bp.alt_response(400, schema=DefaultError)
def post_alpha_user_unban(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_user_unban(auth, data)
    return UserBanResponse().load(resp)


@user_bp.route('/api/alpha/user/register', methods=['POST'])
@user_bp.doc(summary="Register a new user")
@user_bp.arguments(UserRegistrationRequest)
@user_bp.response(200, UserRegistrationResponse)
@user_bp.alt_response(400, schema=DefaultError)
def post_alpha_user_register(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    resp = post_user_register(data)
    return UserRegistrationRequest().load(resp)


@user_bp.route('/api/alpha/user/get_captcha', methods=['GET'])
@user_bp.doc(summary="Fetch a Captcha")
@user_bp.response(200, FetchCaptchaResponse)
@user_bp.alt_response(400, schema=DefaultError)
def get_alpha_user_get_captcha():
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    resp = get_user_captcha()
    return FetchCaptchaResponse().load(resp)


# Upload
@upload_bp.route('/upload/image', methods=['POST'])
@upload_bp.doc(summary="Upload a general image.")
@upload_bp.arguments(ImageUploadRequest, location="files")
@upload_bp.response(200, ImageUploadResponse)
@upload_bp.alt_response(400, schema=DefaultError)
@upload_bp.alt_response(429, schema=DefaultError)
def post_alpha_upload_image(files_data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    with limiter.limit('15/hour'):
        auth = request.headers.get('Authorization')
        image_file = files_data['file']
        resp = post_upload_image(auth, image_file)
        return ImageUploadResponse().load(resp)


@upload_bp.route('/upload/community_image', methods=['POST'])
@upload_bp.doc(summary="Upload a community image.")
@upload_bp.arguments(ImageUploadRequest, location="files")
@upload_bp.response(200, ImageUploadResponse)
@upload_bp.alt_response(400, schema=DefaultError)
@upload_bp.alt_response(429, schema=DefaultError)
def post_alpha_upload_community_image(files_data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    with limiter.limit('20/day'):
        auth = request.headers.get('Authorization')
        image_file = files_data['file']
        resp = post_upload_community_image(auth, image_file)
        return ImageUploadResponse().load(resp)


@upload_bp.route('/upload/user_image', methods=['POST'])
@upload_bp.doc(summary="Upload a user image.")
@upload_bp.arguments(ImageUploadRequest, location="files")
@upload_bp.response(200, ImageUploadResponse)
@upload_bp.alt_response(400, schema=DefaultError)
@upload_bp.alt_response(429, schema=DefaultError)
def post_alpha_upload_user_image(files_data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    with limiter.limit('20/day'):
        auth = request.headers.get('Authorization')
        image_file = files_data['file']
        resp = post_upload_user_image(auth, image_file)
        return ImageUploadResponse().load(resp)


@upload_bp.route('/image/delete', methods=['POST'])
@upload_bp.doc(summary="Delete a user image.")
@upload_bp.arguments(ImageDeleteRequest)
@upload_bp.response(200, ImageDeleteResponse)
@upload_bp.alt_response(400, schema=DefaultError)
@upload_bp.alt_response(429, schema=DefaultError)
def post_alpha_user_image_delete(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = post_image_delete(auth, data)
    return ImageDeleteResponse().load(resp)


# Admin routes, only some are selectively implemented. Everything else intended to be done through web ui

@admin_bp.route('/admin/registration_application/list', methods=['GET'])
@admin_bp.doc(summary="Get the list of applications ready for admin review")
@admin_bp.arguments(GetRegistrationList, location="query")
@admin_bp.response(200, GetRegistrationListResponse)
@admin_bp.alt_response(400, schema=DefaultError)
def get_alpha_admin_registration_list(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_registration_list(auth, data)
    return GetRegistrationListResponse().load(resp)


@admin_bp.route('/admin/registration_application/approve', methods=['PUT'])
@admin_bp.doc(summary="Approve or deny a registration")
@admin_bp.arguments(RegistrationApproveRequest)
@admin_bp.response(200)
@admin_bp.alt_response(400, schema=DefaultError)
def put_alpha_admin_registration_approve(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    put_registration_approve(auth, data)


# Not yet implemented. Copied from lemmy's V3 api, so some aren't needed, and some need changing

# Site - not yet implemented
@bp.route('/api/alpha/site', methods=['POST'])  # Create New Site. No plans to implement
@bp.route('/api/alpha/site', methods=['PUT'])  # Edit Site. Not available in app
def alpha_site():
    return jsonify({"error": "not_yet_implemented"}), 400


# Miscellaneous
@misc_bp.route('/modlog', methods=['GET'])  # Get Modlog
@misc_bp.doc(summary="Get modlog.")
@misc_bp.arguments(GetModLogRequest, location="query")
@misc_bp.response(200, GetModLogResponse)
@misc_bp.alt_response(400, schema=DefaultError)
@misc_bp.alt_response(429, schema=DefaultError)
def alpha_miscellaneous_modlog(data):
    if not enable_api():
        return abort(400, message="alpha api is not enabled")
    auth = request.headers.get('Authorization')
    resp = get_modlog(auth, data)
    return GetModLogResponse().load(resp)


# Community - not yet implemented
# @bp.route('/api/alpha/community', methods=['POST'])                               # (none
# @bp.route('/api/alpha/community', methods=['PUT'])                                #  of
@bp.route('/api/alpha/community/hide', methods=['PUT'])  # these
# @bp.route('/api/alpha/community/delete', methods=['POST'])                        #  are
@bp.route('/api/alpha/community/remove', methods=['POST'])  # available
@bp.route('/api/alpha/community/transfer', methods=['POST'])  # in
@bp.route('/api/alpha/community/ban_user', methods=['POST'])  # the app)
def alpha_community():
    return jsonify({"error": "not_yet_implemented"}), 400


# Post - not yet implemented
@bp.route('/api/alpha/post/report/resolve', methods=['PUT'])  # Stage 2
@bp.route('/api/alpha/post/report/list', methods=['GET'])  # Stage 2
def alpha_post():
    return jsonify({"error": "not_yet_implemented"}), 400


# Reply - not yet implemented
@bp.route('/api/alpha/comment/distinguish', methods=['POST'])  # Not really used
@bp.route('/api/alpha/comment/report/resolve', methods=['PUT'])  # Stage 2
def alpha_reply():
    return jsonify({"error": "not_yet_implemented"}), 400


# Chat
@bp.route('/api/alpha/private_message/report/resolve', methods=['PUT'])  # Stage 2
@bp.route('/api/alpha/private_message/report/list', methods=['GET'])  # Stage 2
def alpha_chat():
    return jsonify({"error": "not_yet_implemented"}), 400


# User - not yet implemented
@bp.route('/api/alpha/user/mention/mark_as_read',
          methods=['POST'])  # No DB support / Not available in app (using mark_all instead)
@bp.route('/api/alpha/user/banned', methods=['GET'])  # Admin function. No plans to implement
@bp.route('/api/alpha/user/delete_account', methods=['POST'])  # Not available in app
@bp.route('/api/alpha/user/password_reset', methods=['POST'])  # Not available in app
@bp.route('/api/alpha/user/password_change', methods=['POST'])  # Not available in app
@bp.route('/api/alpha/user/change_password', methods=['PUT'])  # Stage 2
@bp.route('/api/alpha/user/report_count', methods=['GET'])  # Stage 2
@bp.route('/api/alpha/user/verify_email', methods=['POST'])  # Admin function. No plans to implement
@bp.route('/api/alpha/user/leave_admin', methods=['POST'])  # Admin function. No plans to implement
@bp.route('/api/alpha/user/totp/generate', methods=['POST'])  # Not available in app
@bp.route('/api/alpha/user/totp/update', methods=['POST'])  # Not available in app
@bp.route('/api/alpha/user/export_settings', methods=['GET'])  # Not available in app
@bp.route('/api/alpha/user/import_settings', methods=['POST'])  # Not available in app
@bp.route('/api/alpha/user/list_logins', methods=['GET'])  # Not available in app
@bp.route('/api/alpha/user/validate_auth', methods=['GET'])  # Not available in app
@bp.route('/api/alpha/user/logout', methods=['POST'])  # Stage 2
def alpha_user():
    return jsonify({"error": "not_yet_implemented"}), 400

@bp.route('/api/alpha/user/mention', methods=['GET'])
def alpha_user_mention():
    return jsonify({"error": "renamed to user/mentions"}), 400


# Admin - not yet implemented
@bp.route('/api/alpha/admin/add', methods=['POST'])
@bp.route('/api/alpha/admin/registration_application/count', methods=['GET'])  # (no plans to
@bp.route('/api/alpha/admin/purge/person', methods=['POST'])  # implement
@bp.route('/api/alpha/admin/purge/community', methods=['POST'])  # any more
@bp.route('/api/alpha/admin/purge/post', methods=['POST'])  # endpoints
@bp.route('/api/alpha/admin/purge/comment', methods=['POST'])  # for admin user)
def alpha_admin():
    return jsonify({"error": "not_yet_implemented"}), 400


# CustomEmoji - not yet implemented
@bp.route('/api/alpha/custom_emoji', methods=['PUT'])  # (doesn't
@bp.route('/api/alpha/custom_emoji', methods=['POST'])  # seem
@bp.route('/api/alpha/custom_emoji/delete', methods=['POST'])  # important)
def alpha_emoji():
    return jsonify({"error": "not_yet_implemented"}), 400
