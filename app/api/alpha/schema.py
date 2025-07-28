from datetime import datetime
from marshmallow import Schema, fields, validate, ValidationError, EXCLUDE


# Lists used in schema for validation
reg_mode_list = ["Closed", "RequireApplication", "Open"]
sort_list = ["Active", "Hot", "New", "TopHour", "TopSixHour", "TopTwelveHour", "TopDay", "TopWeek", "TopMonth",
             "TopThreeMonths", "TopSixMonths", "TopNineMonths", "TopYear", "TopAll", "Scaled"]
comment_sort_list = ["Hot", "Top", "New", "Old"]
community_sort_list = ["Hot", "Top", "New"]
listing_type_list = ["All", "Local", "Subscribed", "Popular", "Moderating"]
community_listing_type_list = ["All", "Local", "Subscribed"]
content_type_list = ["Communities", "Posts", "Users", "Url"]
subscribed_type_list = ["Subscribed", "NotSubscribed", "Pending"]


def validate_datetime_string(text):
    try:
        # Ensures that string matches timestamp format used by lemmy/piefed api
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ")
        return True
    except ValueError:
        raise ValidationError(f"Bad datetime string: {text}")


class DefaultError(Schema):
    message = fields.String()


class DefaultSchema(Schema):
    class Meta:
        unknown = EXCLUDE
        datetimeformat = "%Y-%m-%dT%H:%M:%S.%fZ"


class Person(DefaultSchema):
    actor_id = fields.Url(required=True, metadata={"example": "https://piefed.social/u/rimu"})
    banned = fields.Boolean(required=True)
    bot = fields.Boolean(required=True)
    deleted = fields.Boolean(required=True)
    id = fields.Integer(required=True)
    instance_id = fields.Integer(required=True)
    local = fields.Boolean(required=True)
    user_name = fields.String(required=True)
    about = fields.String(metadata={"format": "markdown"})
    avatar = fields.Url(allow_none=True)
    banner = fields.Url(allow_none=True)
    flair = fields.String()
    published = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    title = fields.String(allow_none=True)


class PersonAggregates(DefaultSchema):
    comment_count = fields.Integer(required=True)
    person_id = fields.Integer(required=True)
    post_count = fields.Integer(required=True)


class PersonView(DefaultSchema):
    activity_alert = fields.Boolean(required=True)
    counts = fields.Nested(PersonAggregates, required=True)
    is_admin = fields.Boolean(required=True)
    person = fields.Nested(Person, required=True)


class LanguageView(DefaultSchema):
    code = fields.String(metadata={"example": "en"})
    id = fields.Integer(metadata={"example": "2"})
    name = fields.String(metadata={"example": "English"})


class Site(DefaultSchema):
    actor_id = fields.Url(required=True, metadata={"example": "https://piefed.social"})
    name = fields.String(required=True)
    all_languages = fields.List(fields.Nested(LanguageView))
    description = fields.String()
    enable_downvotes = fields.Boolean()
    icon = fields.Url(allow_none=True)
    registration_mode = fields.String(validate=validate.OneOf(reg_mode_list))
    sidebar = fields.String(metadata={"format": "html"})
    sidebar_md = fields.String(metadata={"format": "markdown"})
    user_count = fields.Integer()


class Community(DefaultSchema):
    actor_id = fields.Url(required=True, metadata={"example": "https://piefed.social/c/piefed_meta"})
    ap_domain = fields.String(metadata={"example": "piefed.social"})
    deleted = fields.Boolean(required=True)
    hidden = fields.Boolean(required=True)
    id = fields.Integer(required=True)
    instance_id = fields.Integer(required=True)
    local = fields.Boolean(required=True)
    name = fields.String(required=True)
    nsfw = fields.Boolean(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    removed = fields.Boolean(required=True)
    restricted_to_mods = fields.Boolean(required=True)
    title = fields.String(required=True)
    banned = fields.Boolean()
    banner = fields.Url(allow_none=True)
    description = fields.String(metadata={"format": "markdown"})
    icon = fields.Url(allow_none=True)
    posting_warning = fields.String(allow_none=True)
    updated = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class CommunityBlockView(DefaultSchema):
    community = fields.Nested(Community)
    person = fields.Nested(Person)


class CommunityFollowerView(DefaultSchema):
    community = fields.Nested(Community, required=True)
    follower = fields.Nested(Person, required=True)


class Instance(DefaultSchema):
    domain = fields.String(required=True, metadata={"example": "piefed.social"})
    id = fields.Integer(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    software = fields.String()
    updated = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    version = fields.String()


class InstanceBlockView(DefaultSchema):
    instance = fields.Nested(Instance, required=True)
    person = fields.Nested(Person, required=True)


class LocalUser(DefaultSchema):
    default_comment_sort_type = fields.String(required=True, validate=validate.OneOf(comment_sort_list))
    default_listing_type = fields.String(required=True, validate=validate.OneOf(listing_type_list))
    default_sort_type = fields.String(required=True, validate=validate.OneOf(sort_list))
    show_bot_accounts = fields.Boolean(required=True)
    show_nsfl = fields.Boolean(required=True)
    show_nsfw = fields.Boolean(required=True)
    show_read_posts = fields.Boolean(required=True)
    show_scores = fields.Boolean(required=True)


class LocalUserView(DefaultSchema):
    counts = fields.Nested(PersonAggregates, required=True)
    local_user = fields.Nested(LocalUser, required=True)
    person = fields.Nested(Person, required=True)


class CommunityModeratorView(DefaultSchema):
    community = fields.Nested(Community, required=True)
    moderator = fields.Nested(Person, required=True)


class PersonBlockView(DefaultSchema):
    person = fields.Nested(Person, required=True)
    target = fields.Nested(Person, required=True)


class MyUserInfo(DefaultSchema):
    community_blocks = fields.List(fields.Nested(CommunityBlockView), required=True)
    discussion_languages = fields.List(fields.Nested(LanguageView), required=True)
    follows = fields.List(fields.Nested(CommunityFollowerView), required=True)
    instance_blocks = fields.List(fields.Nested(InstanceBlockView), required=True)
    local_user_view = fields.Nested(LocalUserView, required=True)
    moderates = fields.List(fields.Nested(CommunityModeratorView), required=True)
    person_blocks = fields.List(fields.Nested(PersonBlockView), required=True)


class GetSiteResponse(DefaultSchema):
    admins = fields.List(fields.Nested(PersonView), required=True)
    site = fields.Nested(Site, required=True)
    version = fields.String(required=True, metadata={"title": "Software version"})
    my_user = fields.Nested(MyUserInfo)


class BlockInstanceRequest(DefaultSchema):
    block = fields.Boolean(required=True)
    instance_id = fields.Integer(required=True)


class BlockInstanceResponse(DefaultSchema):
    blocked = fields.Boolean(required=True)


class SearchRequest(DefaultSchema):
    q = fields.String(required=True)
    type_ = fields.String(required=True, validate=validate.OneOf(content_type_list))
    limit = fields.Integer()
    listing_type = fields.String(validate=validate.OneOf(listing_type_list))
    page = fields.Integer()
    sort = fields.String(validate=validate.OneOf(sort_list))
    


class Post(DefaultSchema):
    ap_id = fields.Url(required=True)
    community_id = fields.Integer(required=True)
    deleted = fields.Boolean(required=True)
    id = fields.Integer(required=True)
    language_id = fields.Integer(required=True)
    local = fields.Boolean(required=True)
    locked = fields.Boolean(required=True)
    nsfw = fields.Boolean(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    removed = fields.Boolean(required=True)
    sticky = fields.Boolean(required=True)
    title = fields.String(required=True)
    user_id = fields.Integer(required=True)
    alt_text = fields.String()
    body = fields.String(metadata={"format": "markdown"})
    small_thumbnail_url = fields.Url()
    thumbnail_url = fields.Url()
    updated = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    url = fields.Url()   


class PostAggregates(DefaultSchema):
    comments = fields.Integer(required=True)
    downvotes = fields.Integer(required=True)
    newest_comment_time = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    post_id = fields.Integer(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    score = fields.Integer(required=True)
    upvotes = fields.Integer(required=True)


class PostView(DefaultSchema):
    banned_from_community = fields.Boolean(required=True)
    community = fields.Nested(Community, required=True)
    counts = fields.Nested(PostAggregates, required=True)
    creator = fields.Nested(Person, required=True)
    creator_banned_from_community = fields.Boolean(required=True)
    creator_is_admin = fields.Boolean(required=True)
    creator_is_moderator = fields.Boolean(required=True)
    hidden = fields.Boolean(required=True)
    post = fields.Nested(Post, required=True)
    read = fields.Boolean(required=True)
    saved = fields.Boolean(required=True)
    subscribed = fields.String(required=True, validate=validate.OneOf(subscribed_type_list))
    unread_comments = fields.Integer(required=True)
    activity_alert = fields.Boolean()
    my_vote = fields.Integer()


class CommunityAggregates(DefaultSchema):
    id = fields.Integer(required=True)
    post_count = fields.Integer(required=True)
    post_reply_count = fields.Integer(required=True)
    published = fields.String(required=True)
    subscriptions_count = fields.Integer(required=True)
    total_subscriptions_count = fields.Integer(required=True)
    active_daily = fields.Integer()
    active_weekly = fields.Integer()
    active_monthly = fields.Integer()
    active_6monthly = fields.Integer()


class CommunityView(DefaultSchema):
    activity_alert = fields.Boolean(required=True)
    blocked = fields.Boolean(required=True)
    community = fields.Nested(Community, required=True)
    counts = fields.Nested(CommunityAggregates, required=True)
    subscribed = fields.String(required=True, validate=validate.OneOf(subscribed_type_list))


class Comment(DefaultSchema):
    ap_id = fields.Url(required=True)
    body = fields.String(required=True, metadata={"format": "markdown"})
    deleted = fields.Boolean(required=True)
    id = fields.Integer(required=True)
    language_id = fields.Integer(required=True)
    local = fields.Boolean(required=True)
    path = fields.String(required=True)
    post_id = fields.Integer(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    removed = fields.Boolean(required=True)
    user_id = fields.Integer(required=True)
    distinguished = fields.Boolean()
    updated = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class CommentAggregates(DefaultSchema):
    child_count = fields.Integer(required=True)
    comment_id = fields.Integer(required=True)
    downvotes = fields.Integer(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    score = fields.Integer(required=True)
    upvotes = fields.Integer(required=True)


class CommentView(DefaultSchema):
    activity_alert = fields.Boolean(required=True)
    banned_from_community = fields.Boolean(required=True)
    comment = fields.Nested(Comment, required=True)
    community = fields.Nested(Community, required=True)
    counts = fields.Nested(CommentAggregates, required=True)
    creator = fields.Nested(Person, required=True)
    creator_banned_from_community = fields.Boolean(required=True)
    creator_blocked = fields.Boolean(required=True)
    creator_is_admin = fields.Boolean(required=True)
    creator_is_moderator = fields.Boolean(required=True) 
    post = fields.Nested(Post, required=True)
    saved = fields.Boolean(required=True)
    subscribed = fields.String(required=True)
    my_vote = fields.Integer()
    can_auth_user_moderate = fields.Boolean()


class SearchResponse(DefaultSchema):
    type_ = fields.String(required=True, validate=validate.OneOf(content_type_list))
    communities = fields.List(fields.Nested(CommunityView), required=True)
    posts = fields.List(fields.Nested(PostView), required=True)
    users = fields.List(fields.Nested(PersonView), required=True)
    comments = fields.List(fields.Nested(CommentView), required=True)


class ResolveObjectRequest(DefaultSchema):
    q = fields.String(required=True)


class ResolveObjectResponse(DefaultSchema):
    comment = fields.Nested(CommentView)
    post = fields.Nested(PostView)
    community = fields.Nested(CommunityView)
    person = fields.Nested(PersonView)


class InstanceWithoutFederationState(DefaultSchema):
    domain = fields.String(required=True)
    id = fields.Integer(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    software = fields.String()
    updated = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    version = fields.String()


class FederatedInstancesView(DefaultSchema):
    allowed = fields.List(fields.Nested(InstanceWithoutFederationState), required=True)
    blocked = fields.List(fields.Nested(InstanceWithoutFederationState), required=True)
    linked = fields.List(fields.Nested(InstanceWithoutFederationState), required=True)


class GetFederatedInstancesResponse(DefaultSchema):
    federated_instances = fields.Nested(FederatedInstancesView)


class GetCommunityRequest(DefaultSchema):
    id = fields.Integer()
    name = fields.String()


class GetCommunityResponse(DefaultSchema):
    community_view = fields.Nested(CommunityView, required=True)
    discussion_languages = fields.List(fields.Integer(), required=True)
    moderators = fields.List(fields.Nested(CommunityModeratorView), required=True)
    site = fields.Nested(Site)


class CreateCommunityRequest(DefaultSchema):
    name = fields.String(required=True)
    title = fields.String(required=True)
    banner_url = fields.Url(allow_none=True)
    description = fields.String(metadata={"format": "markdown"})
    discussion_languages = fields.List(fields.Integer())
    icon_url = fields.Url(allow_none=True)
    local_only = fields.Boolean()
    nsfw = fields.Boolean()
    restricted_to_mods = fields.Boolean()
    rules = fields.String()


class CommunityResponse(DefaultSchema):
    community_view = fields.Nested(CommunityView, required=True)
    discussion_languages = fields.List(fields.Integer(), required=True)


class EditCommunityRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    title = fields.String(required=True)
    banner_url = fields.Url(allow_none=True)
    description = fields.String(metadata={"format": "markdown"})
    discussion_languages = fields.List(fields.Integer())
    icon_url = fields.Url(allow_none=True)
    local_only = fields.Boolean()
    nsfw = fields.Boolean()
    restricted_to_mods = fields.Boolean()
    rules = fields.String()


class DeleteCommunityRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    deleted = fields.Boolean(required=True)


class ListCommunitiesRequest(DefaultSchema):
    limit = fields.Integer()
    page = fields.Integer()
    show_nsfw = fields.Boolean()
    sort = fields.String(validate=validate.OneOf(community_sort_list))
    type_ = fields.String(validate=validate.OneOf(community_listing_type_list))


class ListCommunitiesResponse(DefaultSchema):
    communities = fields.List(fields.Nested(CommunityView), required=True)


class FollowCommunityRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    follow = fields.Boolean(required=True)


class BlockCommunityRequest(DefaultSchema):
    block = fields.Boolean(required=True)
    community_id = fields.Integer(required=True)


class BlockCommunityResponse(DefaultSchema):
    community_view = fields.Nested(CommunityView, required=True)
    blocked = fields.Boolean(required=True)


class ModCommunityRequest(DefaultSchema):
    added = fields.Boolean(required=True)
    community_id = fields.Integer(required=True)
    person_id = fields.Integer(required=True)


class ModCommunityResponse(DefaultSchema):
    moderators = fields.List(fields.Nested(CommunityModeratorView), required=True)


class SubscribeCommunityRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    subscribe = fields.Boolean(required=True)


class CommunityModerationBansListRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    limit = fields.Integer(metadata={"default": 10})
    page = fields.Integer(metadata={"default": 1})


class CommunityModerationBanItem(DefaultSchema):
    banned_by = fields.Nested(Person)
    banned_user = fields.Nested(Person)
    community = fields.Nested(Community)
    expired = fields.Boolean()
    expired_at = fields.String(required=True, allow_none=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z, null=permanent ban", "format": "datetime"})
    reason = fields.String()


class CommunityModerationBansListResponse(DefaultSchema):
    items = fields.List(fields.Nested(CommunityModerationBanItem))
    next_page = fields.String(allow_none=True)


class CommunityModerationBanRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    reason = fields.String(required=True)
    user_id = fields.Integer(required=True)
    expires = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    permanent = fields.Boolean()


class CommunityModerationUnbanRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    user_id = fields.Integer(required=True)


class CommunityModerationNsfwRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    nsfw_status = fields.Boolean(required=True)