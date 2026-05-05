import re

from datetime import datetime
from marshmallow import Schema, fields, validate, ValidationError, EXCLUDE, validates_schema, INCLUDE


# Lists used in schema for validation
reg_mode_list = ["Closed", "RequireApplication", "Open"]
sort_list = ["Active", "Hot", "New", "Top", "TopHour", "TopSixHour", "TopTwelveHour", "TopDay", "TopWeek", "TopMonth",
             "TopThreeMonths", "TopSixMonths", "TopNineMonths", "TopYear", "TopAll", "Scaled", "Old", "Relevance",
             "TopPosts", "TopSubscribers", "NewFederated", "OldFederated"]
default_sorts_list = ["Hot", "Top", "New", "Active", "Old", "Scaled"]
default_comment_sorts_list = ["Hot", "Top", "New", "Old"]
post_sort_list = ["Hot", "Top", "TopHour", "TopSixHour", "TopTwelveHour", "TopWeek", "TopDay", "TopMonth",
                  "TopThreeMonths", "TopSixMonths", "TopNineMonths", "TopYear", "TopAll", "New", "Old", "Scaled", "Active"]
comment_sort_list = ["Hot", "Top", "TopAll", "New", "Old", "Controversial"]
community_sort_list = ["Hot", "Top", "New", "Old", "Active", "TopAll", "TopPosts", "TopSubscribers", "NewFederated", "OldFederated"]
listing_type_list = ["All", "Local", "Subscribed", "Popular", "Moderating", "ModeratorView"]
nsfw_type_list = ["Exclude", "Only", "Include"]
community_listing_type_list = ["All", "Local", "Subscribed", "Moderating", "ModeratorView"]
content_type_list = ["Communities", "Posts", "Users", "Url", "Comments"]
subscribed_type_list = ["Subscribed", "NotSubscribed", "Pending"]
notification_status_list = ["All", "Unread", "Read", "New"]
feature_type_list = ["Community", "Local"]
post_type_list = ["Link", "Discussion", "Image", "Video", "Poll", "Event"]
nsfw_visibility_list = ["Show", "Blur", "Hide", "Transparent"]
ai_visibility_list = ["Show", "Hide", "Label", "Transparent"]
private_message_list = ["None", "Local", "Trusted", "All"]
search_nsfw = ['Exclude', 'Include', 'Only']
modlog_type_list = ["All", "ModRemovePost", "ModLockPost", "ModFeaturePost", "ModRemoveComment", "ModRemoveCommunity", "ModBanFromCommunity", "ModAddCommunity", "ModTransferCommunity", "ModAdd", "ModBan", "ModHideCommunity", "AdminPurgePerson", "AdminPurgeCommunity", "AdminPurgePost", "AdminPurgeComment"]


def validate_datetime_string(text):
    try:
        # Ensures that string matches timestamp format used by lemmy/piefed api
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ")
        return True
    except ValueError:
        raise ValidationError(f"Bad datetime string: {text}")


def validate_color_code(text):
    try:
        # Ensures that hex color code strings have the correct format
        color_pattern = re.compile(r'^#([a-fA-F0-9]{6}|[a-fA-F0-9]{3})$')
        return bool(re.match(color_pattern, text))
    except:
        raise ValidationError(f"Bad hex color code string: {text}")

def validate_non_empty_string(text):
    # Makes sure a string has length >0 after removing whitespace
    stripped_title = re.sub(r'\s+', "", text, flags=re.U)
    if len(stripped_title) >= 1:
        return True
    else:
        raise ValidationError("Non-empty string required")


class DefaultError(Schema):
    message = fields.String()


class DefaultSchema(Schema):
    class Meta:
        unknown = EXCLUDE
        datetimeformat = "%Y-%m-%dT%H:%M:%S.%fZ"


class UserExtraField(DefaultSchema):
    id = fields.Integer(required=True)
    label = fields.String(required=True, metadata={"example": "Pronouns"})
    text = fields.String(required=True, metadata={"example": "he/him, she/her, they/them, etc."})


class Person(DefaultSchema):
    actor_id = fields.String(required=True, metadata={"example": "https://piefed.social/u/rimu"})
    banned = fields.Boolean(required=True)
    bot = fields.Boolean(required=True)
    deleted = fields.Boolean(required=True)
    id = fields.Integer(required=True)
    instance_id = fields.Integer(required=True)
    local = fields.Boolean(required=True)
    user_name = fields.String(required=True)
    about = fields.String(metadata={"format": "markdown"})
    about_html = fields.String(metadata={"format": "html"})
    avatar = fields.String(allow_none=True, metadata={"format": "url"})
    banner = fields.String(allow_none=True, metadata={"format": "url"})
    extra_fields = fields.List(fields.Nested(UserExtraField), validate=validate.Length(max=4))
    note = fields.String(validate=validate.Length(max=50))
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
    announcement_md = fields.String(
        metadata={"description": "The banner at the top of the home page", "format": "markdown"})
    announcement = fields.String(
        metadata={"description": "The banner at the top of the home page", "format": "html"})
    name = fields.String(required=True)
    all_languages = fields.List(fields.Nested(LanguageView))
    description = fields.String()
    enable_downvotes = fields.Boolean()
    icon = fields.String(allow_none=True, metadata={"format": "url"})
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
    ai_generated = fields.Boolean(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    removed = fields.Boolean(required=True)
    restricted_to_mods = fields.Boolean(required=True)
    title = fields.String(required=True)
    banned = fields.Boolean()
    question_answer = fields.Boolean()
    banner = fields.String(allow_none=True, metadata={"format": "url"})
    description = fields.String(metadata={"format": "markdown"})
    icon = fields.String(allow_none=True, metadata={"format": "url"})
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
    accept_private_messages = fields.String(required=True, validate=validate.OneOf(private_message_list),
                                            metadata={"description": 'Accept private messages from nobody, local users only, "trusted" instances, or any instance'})
    bot_visibility = fields.String(required=True, validate=validate.OneOf(nsfw_visibility_list))
    ai_visibility = fields.String(required=True, validate=validate.OneOf(ai_visibility_list))
    community_keyword_filter = fields.List(fields.String(),
                                           metadata={"description": "Filter out communities with these words in their name"})
    default_comment_sort_type = fields.String(required=True, validate=validate.OneOf(comment_sort_list))
    default_listing_type = fields.String(required=True, validate=validate.OneOf(listing_type_list))
    default_sort_type = fields.String(validate=validate.OneOf(sort_list))
    email_unread = fields.Boolean(required=True, metadata={"description": "Receive email about missed notifications (if set up by local admin)"})
    federate_votes = fields.Boolean(required=True, metadata={"description": "If false, votes are only counted on local instance instead of federated remotely"})
    feed_auto_follow = fields.Boolean(required=True, metadata={"description": "Automatically follow communities in a subscribed feed"})
    feed_auto_leave = fields.Boolean(required=True, metadata={"description": "Automatically leave communities when unsubscribing from a feed. Does not impact communities joined outside of a feed auto-follow."})
    hide_low_quality = fields.Boolean(required=True, 
                                      metadata={"description": "Hide posts from communities marked as low-quality by the local instance admin"})
    indexable = fields.Boolean(required=True, metadata={"description": "If posts can show up in search results"})
    newsletter = fields.Boolean(required=True, metadata={"description": "Subscribe to the email newsletter that the local instance admin can send"})
    nsfl_visibility = fields.String(required=True, validate=validate.OneOf(nsfw_visibility_list))
    nsfw_visibility = fields.String(required=True, validate=validate.OneOf(nsfw_visibility_list))
    reply_collapse_threshold = fields.Integer(required=True, metadata={"description": "Collapse replies with a score at or below this level"})
    reply_hide_threshold = fields.Integer(required=True, metadata={"description": "Hide replies with a score at or below this level"})
    searchable = fields.Boolean(required=True, metadata={"description": "If profile shows up in the user list on the instance"})
    show_bot_accounts = fields.Boolean(required=True, metadata={"description": "True for any visibility option other than Hide"})
    show_nsfl = fields.Boolean(required=True, metadata={"description": "True for any visibility option other than Hide"})
    show_nsfw = fields.Boolean(required=True, metadata={"description": "True for any visibility option other than Hide"})
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


class GetSiteVersionResponse(DefaultSchema):
    version = fields.String(required=True)


class GetSiteInstanceChooserResponse(DefaultSchema):
    language = fields.Nested(LanguageView, required=True)
    nsfw = fields.Boolean(required=True)
    newbie_friendly = fields.Boolean(required=True)
    name = fields.String(required=True)
    elevator_pitch = fields.String(required=True)
    description = fields.String(required=True)
    about = fields.String(required=True)
    sidebar = fields.String(required=True)
    logo_url = fields.String(required=True, metadata={"format": "url"})
    maturity = fields.String(required=True)
    tos_url = fields.String(required=True, metadata={"format": "url"})
    mau = fields.Integer(required=True)
    can_make_communities = fields.Boolean(required=True)
    defederation = fields.List(fields.String(), required=True)
    trusts = fields.List(fields.String(), required=True)
    registration_mode = fields.String(required=True)


class GetSiteInstanceChooserSearchResponseItem(DefaultSchema):
    id = fields.Integer(required=True)
    name = fields.String(required=True)
    domain = fields.String(required=True)
    elevator_pitch = fields.String(required=True)
    description = fields.String(required=True)
    about = fields.String(required=True)
    sidebar = fields.String(required=True)
    logo_url = fields.String(required=True, metadata={"format": "url"})
    maturity = fields.String(required=True)
    tos_url = fields.String(required=True, metadata={"format": "url"})
    uptime = fields.String(required=True)
    mau = fields.Integer(required=True)
    can_make_communities = fields.Boolean(required=True)
    newbie_friendly = fields.Boolean(required=True)
    defederation = fields.List(fields.String(), required=True)
    trusts = fields.List(fields.String(), required=True)
    registration_mode = fields.String(required=True)
    language = fields.String(required=True)
    monthsmonitored = fields.Integer(required=True)



class GetSiteInstanceChooserSearchResponse(DefaultSchema):
    result = fields.List(fields.Nested(GetSiteInstanceChooserSearchResponseItem), required=True)


class BlockInstanceRequest(DefaultSchema):
    block = fields.Boolean(required=True)
    instance_id = fields.Integer(required=True)


class BlockInstanceResponse(DefaultSchema):
    blocked = fields.Boolean(required=True)


class SearchRequest(DefaultSchema):
    q = fields.String(required=True)
    type_ = fields.String(required=True, validate=validate.OneOf(content_type_list))
    limit = fields.Integer()
    listing_type = fields.String(validate=validate.OneOf(listing_type_list), metadata={
        "description": "Only some types are supported for each `type_`.\n\n"
        "- `Comments` and `Communities`: `Popular` will return the same results as `All`.\n"
        "- `Users`: only `Local` will differ from `All`.\n"
        "- `Posts` and `Url`: These types are equivalent."
    })
    page = fields.Integer()
    sort = fields.String(validate=validate.OneOf(sort_list), metadata={
        "description": "Only some sorting options supported for each `type_`. `NewFederated` and `OldFederated` are "
        "equivalent to `New` and `Old` respectively for each type except for `Communities`.\n\n"
        "- `Comments`: `Scaled` is not supported and `Hot` will be returned instead.\n"
        "- `Communities`: `Top` sorts that are not present in `/community/list` are equivalent to `Top`.\n"
        "- `Users`: only `New`, `Old`, and `Top` (by number of posts, all `Top` sorts are equivalent) are supported. "
        "Otherwise will be sorted by user id.\n"
        "- `Posts` and `Url`: These types are equivalent. `TopPosts` and `TopSubscribers` are equivalent to `Top`."
    })
    community_name = fields.String()
    community_id = fields.Integer()
    minimum_upvotes = fields.Integer()
    nsfw = fields.String(validate=validate.OneOf(search_nsfw))


class SearchInstanceChooser(DefaultSchema):
    q = fields.String()
    nsfw = fields.String()
    language = fields.String()
    newbie = fields.String()


class WidthHeight(DefaultSchema):
    width = fields.Integer()
    height = fields.Integer()


class MiniCrossPosts(DefaultSchema):
    post_id = fields.Integer()
    reply_count = fields.Integer()
    community_name = fields.String()


class PostEvent(DefaultSchema):
    start = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    end = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    timezone = fields.String(metadata={"example": "America/New_York"})
    max_attendees = fields.Integer(metadata={"default": 0})
    participant_count = fields.Integer(metadata={"default": 0})
    full = fields.Boolean(metadata={"default": False})
    online_link = fields.String(allow_none=True, metadata={"format": "url"})
    join_mode = fields.String(metadata={"example": "free", "description": "free, restricted, external, invite"})
    external_participation_url = fields.String(allow_none=True, metadata={"format": "url"})
    anonymous_participation = fields.Boolean(metadata={"default": False})
    online = fields.Boolean(metadata={"default": False})
    buy_tickets_link = fields.String(allow_none=True, metadata={"format": "url"})
    event_fee_currency = fields.String(metadata={"example": "USD"})
    event_fee_amount = fields.Float(metadata={"default": 0})
    location = fields.Dict(allow_none=True, metadata={"description": "JSON object containing location details"})


class PollChoice(DefaultSchema):
    id = fields.Integer(required=True)
    choice_text = fields.String(required=True)
    sort_order = fields.Integer(required=True)
    num_votes = fields.Integer(metadata={"default": 0, "description": "Value is ignored when creating/editing a poll"})


class PostPoll(DefaultSchema):
    end_poll = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    mode = fields.String(required=True, validate=validate.OneOf(['single', 'multiple']), metadata={"example": "single", "description": "single or multiple - determines whether people can vote for one or multiple options"})
    local_only = fields.Boolean(metadata={"default": False})
    latest_vote = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    choices = fields.List(fields.Nested(PollChoice), required=True, validate=validate.Length(max=10))
    my_votes = fields.List(fields.Integer())


class Reactions(DefaultSchema):
    url = fields.String(allow_none=True)
    token = fields.String()
    authors = fields.List(fields.String())
    count = fields.Integer()


class Post(DefaultSchema):
    ap_id = fields.String(required=True)
    community_id = fields.Integer(required=True)
    deleted = fields.Boolean(required=True)
    id = fields.Integer(required=True)
    language_id = fields.Integer(required=True)
    local = fields.Boolean(required=True)
    locked = fields.Boolean(required=True)
    nsfw = fields.Boolean(required=True)
    ai_generated = fields.Boolean(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    removed = fields.Boolean(required=True)
    sticky = fields.Boolean(required=True)
    instance_sticky = fields.Boolean(required=True)
    title = fields.String(required=True)
    user_id = fields.Integer(required=True)
    alt_text = fields.String()
    body = fields.String(metadata={"format": "markdown"})
    small_thumbnail_url = fields.String(metadata={"format": "url"})
    thumbnail_url = fields.String(metadata={"format": "url"})
    updated = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    url = fields.String(metadata={"format": "url"})
    image_details = fields.Nested(WidthHeight)
    cross_posts = fields.List(fields.Nested(MiniCrossPosts))
    post_type = fields.String(required=True, validate=validate.OneOf(post_type_list))
    tags = fields.String(allow_none=True)
    flair = fields.String(allow_none=True)
    emoji_reactions = fields.List(fields.Nested(Reactions), allow_none=True)
    event = fields.Nested(PostEvent)
    poll = fields.Nested(PostPoll)


class PostAggregates(DefaultSchema):
    comments = fields.Integer(required=True)
    downvotes = fields.Integer(required=True)
    newest_comment_time = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    post_id = fields.Integer(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    score = fields.Integer(required=True)
    upvotes = fields.Integer(required=True)
    cross_posts = fields.Integer(required=True)


class CommunityFlair(DefaultSchema):
    id = fields.Integer(required=True)
    community_id = fields.Integer(required=True)
    flair_title = fields.String(required=True)
    text_color = fields.String(required=True, validate=validate_color_code,
                               metadata={"example": "#000000", "description": "Hex color code for the text of the flair"})
    background_color = fields.String(required=True, validate=validate_color_code,
                                     metadata={"example": "#DEDDDA", "description": "Hex color code for the background of the flair"})
    blur_images = fields.Boolean(required=True)
    ap_id = fields.String(required=True, allow_none=True, metadata={
        "description": "Legacy tags that existed prior to 1.2 and some tags for remote communities might not have a defined ap_id",
        "format": "url"})


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
    alt_text = fields.String()
    my_vote = fields.Integer()
    flair_list = fields.List(fields.Nested(CommunityFlair), metadata={"description": "See also the simpler 'flair' on post which can be used when editing"})
    can_auth_user_moderate = fields.Boolean()


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
    flair_list = fields.List(fields.Nested(CommunityFlair))


class CommunityFlairCreateRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    flair_title = fields.String(required=True)
    text_color = fields.String(
        validate=validate_color_code,
        metadata={
            "example": "#000 or #000000",
            "default": "#000000",
            "description": "Hex color code for the text of the flair."})
    background_color = fields.String(
        validate=validate_color_code,
        metadata={
            "example": "#fff or #FFFFFF",
            "default": "#DEDDDA",
            "description": "Hex color code for the background of the flair."})
    blur_images = fields.Boolean(metadata={"default": False})


class CommunityFlairCreateResponse(CommunityFlair):
    pass


class CommunityFlairEditRequest(DefaultSchema):
    flair_id = fields.Integer(required=True)
    flair_title = fields.String()
    text_color = fields.String(
        validate=validate_color_code,
        metadata={
            "example": "#000 or #000000",
            "description": "Hex color code for the text of the flair."})
    background_color = fields.String(
        validate=validate_color_code,
        metadata={
            "example": "#fff or #FFFFFF",
            "description": "Hex color code for the background of the flair."})
    blur_images = fields.Boolean()


class CommunityFlairEditResponse(CommunityFlair):
    pass


class Comment(Schema):
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
    locked = fields.Boolean()
    answer = fields.Boolean()
    emoji_reactions = fields.List(fields.Nested(Reactions), allow_none=True)

    class Meta:
        unknown = INCLUDE # let the not-consistent-with-anything 'repliesEnabled' through for Boost
        datetimeformat = "%Y-%m-%dT%H:%M:%S.%fZ"


class CommentReport(DefaultSchema):
    id = fields.Integer(required=True)
    creator_id = fields.Integer(required=True)
    comment_id = fields.Integer(required=True)
    original_comment_text = fields.String()
    reason = fields.String(metadata={"description": "Categories of report selected by the reporter"})
    description = fields.String(metadata={"description": "Any additional information provided by the reporter"})
    resolved = fields.Boolean(required=True)
    # TODO: resolver_id = fields.Integer(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
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
    subscribed = fields.String(
        required=True,
        metadata={"description": "Indicates whether auth'ed user is subscribed to the community this comment is in or not."},
        validate=validate.OneOf(subscribed_type_list))
    my_vote = fields.Integer()
    can_auth_user_moderate = fields.Boolean()


class FeedView(DefaultSchema):
    actor_id = fields.Url(required=True)
    ap_domain = fields.String(required=True)
    children = fields.List(fields.Nested(lambda: FeedView()), required=True,
                           metadata={"description": "Always empty list for resolve_object endpoint"})
    communities = fields.List(fields.Nested(Community), required=True,
                              metadata={"description": "Always empty list for resolve_object endpoint"})
    communities_count = fields.Integer(required=True)
    id = fields.Integer(required=True)
    is_instance_feed = fields.Boolean(required=True)
    local = fields.Boolean(required=True)
    name = fields.String(required=True)
    nsfl = fields.Boolean(required=True)
    nsfw = fields.Boolean(required=True)
    owner = fields.Boolean(required=True, metadata={"description": "Is the authorized user the creator of the feed?"})
    public = fields.Boolean(required=True)
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    show_posts_from_children = fields.Boolean(required=True)
    subscribed = fields.Boolean(required=True)
    subscriptions_count = fields.Integer(required=True)
    title = fields.String(required=True)
    updated = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    user_id = fields.Integer(required=True, metadata={"description": "user_id of the feed creator/owner"}) 
    banner = fields.String(allow_none=True, metadata={"format": "url"})
    description = fields.String(allow_none=True, metadata={"format": "markdown"})
    description_html = fields.String(allow_none=True, metadata={"format": "html"})
    icon = fields.String(allow_none=True, metadata={"format": "url"})
    parent_feed_id = fields.Integer(allow_none=True)


class CommentReportView(CommentView):
    comment_report = fields.Nested(CommentReport, required=True)
    comment_creator = fields.Nested(Person, required=True)
    # TODO: resolver = fields.Nested(Person, required=True)


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
    feed = fields.Nested(FeedView)


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


class GetFeedRequest(DefaultSchema):
    id = fields.Integer()
    name = fields.String()


class GetSuggestCompletionRequest(DefaultSchema):
    q = fields.String()


class GetSuggestCompletionResponse(DefaultSchema):
    result = fields.List(fields.String(), required=True)


class CommunityFlairDeleteRequest(DefaultSchema):
    flair_id = fields.Integer(required=True)


class CommunityFlairDeleteResponse(GetCommunityResponse):
    pass


class CreateCommunityRequest(DefaultSchema):
    name = fields.String(required=True)
    title = fields.String(required=True)
    banner_url = fields.String(allow_none=True, metadata={"format": "url"})
    description = fields.String(metadata={"format": "markdown"})
    discussion_languages = fields.List(fields.Integer())
    icon_url = fields.String(allow_none=True, metadata={"format": "url"})
    local_only = fields.Boolean()
    nsfw = fields.Boolean()
    question_answer = fields.Boolean()
    restricted_to_mods = fields.Boolean()
    rules = fields.String()


class CommunityResponse(DefaultSchema):
    community_view = fields.Nested(CommunityView, required=True)
    discussion_languages = fields.List(fields.Integer(), required=True)


class EditCommunityRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    title = fields.String()
    banner_url = fields.String(allow_none=True, metadata={"format": "url"})
    description = fields.String(metadata={"format": "markdown"})
    discussion_languages = fields.List(fields.Integer())
    icon_url = fields.String(allow_none=True, metadata={"format": "url"})
    local_only = fields.Boolean()
    nsfw = fields.Boolean()
    restricted_to_mods = fields.Boolean()
    question_answer = fields.Boolean()
    rules = fields.String()


class DeleteCommunityRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    deleted = fields.Boolean(required=True)


class CreateFeedRequest(DefaultSchema):
    name = fields.String(required=True, metadata={"description": "URL-safe name/slug for the feed"})
    title = fields.String(required=True)
    description = fields.String(metadata={"format": "markdown"})
    icon_url = fields.String(allow_none=True, metadata={"format": "url"})
    banner_url = fields.String(allow_none=True, metadata={"format": "url"})
    nsfw = fields.Boolean()
    nsfl = fields.Boolean()
    public = fields.Boolean(metadata={"description": "Whether the feed is publicly visible", "default": True})
    communities = fields.String(metadata={"description": "Newline-separated list of community ap_ids to include in the feed"})
    is_instance_feed = fields.Boolean(metadata={"description": "Whether this is an instance-level feed (admin only)"})
    show_child_posts = fields.Boolean(metadata={"description": "Whether to show posts from child feeds"})
    parent_feed_id = fields.Integer(allow_none=True, metadata={"description": "ID of parent feed, if any"})


class EditFeedRequest(DefaultSchema):
    feed_id = fields.Integer(required=True)
    url = fields.String(metadata={"description": "URL-safe name/slug for the feed"})
    title = fields.String()
    description = fields.String(metadata={"format": "markdown"})
    icon_url = fields.String(allow_none=True, metadata={"format": "url"})
    banner_url = fields.String(allow_none=True, metadata={"format": "url"})
    nsfw = fields.Boolean()
    nsfl = fields.Boolean()
    public = fields.Boolean()
    communities = fields.String(metadata={"description": "Newline-separated list of community ap_ids; omit to keep existing communities"})
    is_instance_feed = fields.Boolean()
    show_child_posts = fields.Boolean()
    parent_feed_id = fields.Integer(allow_none=True)


class DeleteFeedRequest(DefaultSchema):
    feed_id = fields.Integer(required=True)
    deleted = fields.Boolean(required=True)


class ListCommunitiesRequest(DefaultSchema):
    limit = fields.Integer()
    page = fields.Integer()
    show_nsfw = fields.Boolean()
    sort = fields.String(validate=validate.OneOf(community_sort_list))
    type_ = fields.String(validate=validate.OneOf(community_listing_type_list))


class ListCommunitiesResponse(DefaultSchema):
    communities = fields.List(fields.Nested(CommunityView), required=True)
    next_page = fields.String(allow_none=True)


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
    expired_at = fields.String(allow_none=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z, null=permanent ban", "format": "datetime"})
    expires_at = fields.String(allow_none=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z, null=permanent ban", "format": "datetime"})
    reason = fields.String()


class CommunityModerationBansListResponse(DefaultSchema):
    items = fields.List(fields.Nested(CommunityModerationBanItem))
    next_page = fields.String(allow_none=True)


class CommunityModerationBanRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    reason = fields.String(required=True)
    user_id = fields.Integer(required=True)
    expires_at = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    permanent = fields.Boolean()


class CommunityModerationUnbanRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    user_id = fields.Integer(required=True)


class CommunityModerationNsfwRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    nsfw_status = fields.Boolean(required=True)


class FeedListRequest(DefaultSchema):
    include_communities = fields.Boolean(metadata={"description": "include list of communities in each feed with result", "default": True})
    mine_only = fields.Boolean(metadata={"description": "only return feeds created by the authorized user", "default": False})


class FeedListResponse(DefaultSchema):
    feeds = fields.List(fields.Nested(FeedView), required=True)


class FollowFeedRequest(DefaultSchema):
    feed_id = fields.Integer(required=True)
    follow = fields.Boolean(required=True)


class TopicView(DefaultSchema):
    children = fields.List(fields.Nested(lambda: TopicView()), required=True)
    communities = fields.List(fields.Nested(Community), required=True)
    communities_count = fields.Integer(required=True)
    id = fields.Integer(required=True)
    name = fields.String(required=True)
    show_posts_from_children = fields.Boolean(required=True)
    title = fields.String(required=True)
    parent_topic_id = fields.Integer(allow_none=True)


class TopicListRequest(DefaultSchema):
    include_communities = fields.Boolean(metadata={"description": "include list of communities in each topic with result", "default": True})


class TopicListResponse(DefaultSchema):
    topics = fields.List(fields.Nested(TopicView), required=True)


class GetUserRequest(DefaultSchema):
    person_id = fields.Integer(metadata={"description": "One of either person_id or username must be specified"})
    username = fields.String(metadata={"description": "One of either person_id or username must be specified"})
    sort = fields.String(validate=validate.OneOf(sort_list))
    page = fields.Integer(metadata={"default": 1})
    limit = fields.Integer(metadata={"default": 20})  # Previous defaults were 50 for posts, 10 for comments
    community_id = fields.Integer(metadata={"description": "Limit posts/comments to just a single community"})
    saved_only = fields.Boolean(metadata={"default": False})
    include_content = fields.Boolean(metadata={"default": False})

    @validates_schema
    def validate_input(self, data, **kwargs):
        if "person_id" not in data and "username" not in data:
            raise ValidationError("One of either person_id or username must be specified")


class GetUserResponse(DefaultSchema):
    comments = fields.List(fields.Nested(CommentView), required=True)
    moderates = fields.List(fields.Nested(CommunityModeratorView), required=True)
    person_view = fields.Nested(PersonView, required=True)
    posts = fields.List(fields.Nested(PostView), required=True)
    site = fields.Nested(Site)


class UserLoginRequest(DefaultSchema):
    username = fields.String(required=True)
    password = fields.String(required=True)


class UserLoginResponse(DefaultSchema):
    jwt = fields.String(required=True)


class UserUnreadCountsResponse(DefaultSchema):
    mentions = fields.Integer(required=True, metadata={"description": "Post and comment mentions"})
    private_messages = fields.Integer(required=True)
    replies = fields.Integer(required=True, metadata={"description": "Replies to posts and comments"})
    other = fields.Integer(required=True, metadata={"description": "Any other type of notification (reports, activity alerts, etc.)"})


class UserRepliesRequest(DefaultSchema):
    limit = fields.Integer(metadata={"default": 10})
    page = fields.Integer(metadata={"default": 1})
    sort = fields.String(validate=validate.OneOf(comment_sort_list), metadata={"default": "New"})
    unread_only = fields.Boolean(metadata={"default": True})


class CommentReply(DefaultSchema):
    id = fields.Integer(required=True)  # redundant with comment_id
    comment_id = fields.Integer(required=True)  # redundant with id
    published = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    read = fields.Boolean(required=True)
    recipient_id = fields.Integer(required=True)


class CommentReplyView(DefaultSchema):
    activity_alert = fields.Boolean(required=True)
    comment = fields.Nested(Comment, required=True)
    comment_reply = fields.Nested(CommentReply, required=True)
    community = fields.Nested(Community, required=True)
    counts = fields.Nested(CommentAggregates, required=True)
    creator = fields.Nested(Person, required=True)
    creator_banned_from_community = fields.Boolean(required=True)
    creator_blocked = fields.Boolean(required=True)
    creator_is_admin = fields.Boolean(required=True)
    creator_is_moderator = fields.Boolean(required=True)
    distinguished = fields.Boolean()
    my_vote = fields.Integer(required=True)
    post = fields.Nested(Post, required=True)
    recipient = fields.Nested(Person, required=True)
    saved = fields.Boolean(required=True)
    subscribed = fields.String(required=True, validate=validate.OneOf(subscribed_type_list))


class DomainBlockRequest(DefaultSchema):
    block = fields.Boolean(required=True)
    domain = fields.String(required=True)


class DomainBlockResponse(DefaultSchema):
    blocked = fields.Boolean(required=True)


class UserRepliesResponse(DefaultSchema):
    next_page = fields.String(allow_none=True)
    replies = fields.List(fields.Nested(CommentReplyView), required=True)


class UserMentionsRequest(DefaultSchema):
    limit = fields.Integer(metadata={"default": 10})
    page = fields.Integer(metadata={"default": 1})
    sort = fields.String(validate=validate.OneOf(comment_sort_list), metadata={"default": "New"})
    unread_only = fields.Boolean(metadata={"default": True})


class UserMentionsResponse(DefaultSchema):
    next_page = fields.String(allow_none=True)
    replies = fields.List(fields.Nested(CommentReplyView), required=True)


class MediaView(DefaultSchema):
    url = fields.String(metadata={"format": "url"})
    name = fields.String()


class UserMediaRequest(DefaultSchema):
    limit = fields.Integer(metadata={"default": 10})
    page = fields.Integer(metadata={"default": 1})
    sort = fields.String(validate=validate.OneOf(comment_sort_list), metadata={"default": "New"})
    unread_only = fields.Boolean(metadata={"default": True})


class UserMediaResponse(DefaultSchema):
    next_page = fields.String(allow_none=True)
    media = fields.List(fields.Nested(MediaView), required=True)


class UserBlockRequest(DefaultSchema):
    block = fields.Boolean(required=True)
    person_id = fields.Integer(required=True)


class UserBlockResponse(DefaultSchema):
    blocked = fields.Boolean(required=True)
    person_view = fields.Nested(PersonView, required=True)


class UserMarkAllReadResponse(DefaultSchema):
    replies = fields.List(fields.Nested(CommentReplyView), required=True, metadata={"description": "Should be empty list"})


class UserSubscribeRequest(DefaultSchema):
    person_id = fields.Integer(required=True)
    subscribe = fields.Boolean(required=True)


class UserSubscribeResponse(DefaultSchema):
    person_view = fields.Nested(PersonView, required=True)
    subscribed = fields.Boolean(required=True)  # Added field


class UserSetFlairRequest(DefaultSchema):
    community_id = fields.Integer(required=True)
    flair_text = fields.String(allow_none=True, validate=validate.Length(max=50),
                               metadata={"description": "Either omit or set to null to remove existing flair"})


class UserSetFlairResponse(DefaultSchema):
    person_view = fields.Nested(PersonView)


class UserSetNoteRequest(DefaultSchema):
    person_id = fields.Integer(required=True)
    note = fields.String(required=True, allow_none=True, validate=validate.Length(max=50),
                         metadata={"description": "Pass a value of null to remove existing note"})


class UserBanRequest(DefaultSchema):
    person_id = fields.Integer(required=True)
    ban_ip_address = fields.Boolean(required=True, allow_none=True)
    purge_content = fields.Boolean(required=True, allow_none=True)
    reason = fields.String(required=True, allow_none=True, validate=validate.Length(max=50),
                           metadata={"description": "Note to add to modlog"})


class UserUnbanRequest(DefaultSchema):
    person_id = fields.Integer(required=True)


class UserBanResponse(DefaultSchema):
    person_view = fields.Nested(PersonView)


class UserSetNoteResponse(UserSetFlairResponse):
    pass


class NewUserExtraField(DefaultSchema):
    id = fields.Integer(
        allow_none=True,
        metadata={
            "description": "Pass an id of an existing extra field with null/missing/empty label or text to remove a field. "
            "Pass an id of an existing extra field with both label and text to edit an existing extra field."})
    label = fields.String(
        allow_none=True,
        metadata={"description": "Pass a label and text without an id to create a new extra field."})
    text = fields.String(
        allow_none=True,
        metadata={"description": "Pass a label and text without an id to create a new extra field."})


class UserSaveSettingsRequest(DefaultSchema):
    accept_private_messages = fields.String(validate=validate.OneOf(private_message_list),
                                            metadata={"description": 'Accept private messages from nobody, local users only, "trusted" instances, or any instance'})
    avatar = fields.String(allow_none=True, metadata={"format": "url", "description": "Pass a null value to remove the image"})
    bio = fields.String(metadata={"format": "markdown"})
    bot = fields.Boolean(metadata={"description": "This user is a bot"})
    bot_visibility = fields.String(validate=validate.OneOf(nsfw_visibility_list))
    community_keyword_filter = fields.List(fields.String(), allow_none=True,
                                           metadata={"description": "Filter out communities with these words in their name. Pass null to remove any filters."})
    cover = fields.String(allow_none=True, metadata={"format": "url", "description": "Pass a null value to remove the image"})
    default_comment_sort_type = fields.String(validate=validate.OneOf(default_comment_sorts_list))
    default_sort_type = fields.String(validate=validate.OneOf(default_sorts_list))
    display_name = fields.String(allow_none=True, validate=validate.Length(max=255), metadata={"description": "Pass a null value to remove the display name"})
    email_unread = fields.Boolean(metadata={"description": "Receive email about missed notifications (if set up by local admin)"})
    extra_fields = fields.List(fields.Nested(NewUserExtraField),
                               metadata={"description": "A user can't have more than four total extra fields."})
    federate_votes = fields.Boolean(metadata={"description": "If false, votes are only counted on local instance instead of federated remotely"})
    feed_auto_follow = fields.Boolean(metadata={"description": "Automatically follow communities in a subscribed feed"})
    feed_auto_leave = fields.Boolean(metadata={"description": "Automatically leave communities when unsubscribing from a feed. Does not impact communities joined outside of a feed auto-follow."})
    hide_low_quality = fields.Boolean(metadata={"description": "Hide posts from communities marked as low-quality by the local instance admin"})
    indexable = fields.Boolean(metadata={"description": "If posts can show up in search results"})
    newsletter = fields.Boolean(metadata={"description": "Subscribe to the email newsletter that the local instance admin can send"})
    nsfl_visibility = fields.String(validate=validate.OneOf(nsfw_visibility_list),
                                    metadata={"description": "Overrides the show_nsfl field if provided"})
    nsfw_visibility = fields.String(validate=validate.OneOf(nsfw_visibility_list),
                                    metadata={"description": "Overrides the show_nsfw field if provided"})
    genai_visibility = fields.String(validate=validate.OneOf(ai_visibility_list),
                                    metadata={"description": "Overrides the show_genai field if provided"})
    reply_collapse_threshold = fields.Integer(metadata={"description": "Collapse replies with a score at or below this level"})
    reply_hide_threshold = fields.Integer(metadata={"description": "Hide replies with a score at or below this level"})
    show_nsfw = fields.Boolean()
    show_nsfl = fields.Boolean()
    show_read_posts = fields.Boolean()
    searchable = fields.Boolean(metadata={"description": "If profile shows up in the user list on the instance"})


class UserSaveSettingsResponse(DefaultSchema):
    my_user = fields.Nested(MyUserInfo)


class UserMeResponse(MyUserInfo):
    ...


class UserNotificationsRequest(DefaultSchema):
    status = fields.String(required=True, validate=validate.OneOf(notification_status_list))
    limit = fields.Integer(metadata={"default": 10})
    page = fields.Integer(metadata={"default": 1})


class UserNotificationItemView(DefaultSchema):
    author = fields.Nested(Person, required=True, metadata={"description": "returned for all notif types"})
    notif_body = fields.String(required=True, metadata={"description": "returned for all notif types"})
    notif_id = fields.Integer(required=True, metadata={"description": "returned for all notif types"})
    notif_subtype = fields.String(required=True, metadata={"description": "returned for all notif types"})
    notif_type = fields.Integer(required=True, metadata={"description": "returned for all notif types"})
    status = fields.String(validate=validate.OneOf(["Unread", "Read"]), required=True, metadata={"description": "returned for all notif types"})
    comment = fields.Nested(Comment, metadata={"description": "returned for notif_types: 3, 4, 6 (comment_mention subtype)"})
    comment_view = fields.Nested(CommentView, metadata={"description": "returned for notif_types: 3, 4, 6 (comment_mention subtype)"})
    comment_id = fields.Integer(metadata={"description": "returned for notif_types: 3, 4, 6 (comment_mention subtype)"})
    community = fields.Nested(Community, metadata={"description": "returned for notif_type 1"})
    post = fields.Nested(PostView, metadata={"description": "returned for notif_types: 0, 1, 2, 3, 4, 5, 6 (post_mention subtype)"})
    post_id = fields.Integer(metadata={"description": "returned for notif_types: 0, 1, 2, 3, 4, 5, 6 (post_mention subtype)"})

class UserNotificationsCounts(DefaultSchema):
    unread = fields.Integer(required=True)
    read = fields.Integer(required=True)
    total = fields.Integer(required=True)


class UserNotificationsResponse(DefaultSchema):
    counts = fields.Nested(UserNotificationsCounts, required=True)
    items = fields.List(fields.Nested(UserNotificationItemView), required=True)
    status = fields.String(required=True, validate=validate.OneOf(notification_status_list))
    username = fields.String(required=True)
    next_page = fields.String(allow_none=True)


class UserNotificationStateRequest(DefaultSchema):
    notif_id = fields.Integer(required=True)
    read_state = fields.Boolean(required=True, metadata={"description": "true sets notification as read, false marks it unread"})


class UserNotificationsCountResponse(DefaultSchema):
    count = fields.Integer(required=True)


class UserMarkAllNotifsReadResponse(DefaultSchema):
    mark_all_notifications_as_read = fields.String(required=True, metadata={"example": "complete"})


class ListCommentsRequest(DefaultSchema):
    limit = fields.Integer(metadata={"default": 10})
    page = fields.Integer(metadata={"default": 1})
    sort = fields.String(validate=validate.OneOf(comment_sort_list), metadata={"default": "New"})
    type_ = fields.String(validate=validate.OneOf(listing_type_list), metadata={"default": "All"})
    liked_only = fields.Boolean()
    saved_only = fields.Boolean()
    person_id = fields.Integer()
    community_id = fields.Integer()
    post_id = fields.Integer()
    parent_id = fields.Integer()
    max_depth = fields.Integer()
    depth_first = fields.Boolean(metadata={"description": "guarantee parent comments are on the same page as any fetched comments"})


class ListCommentsResponse(DefaultSchema):
    comments = fields.List(fields.Nested(CommentView), required=True)
    next_page = fields.String(allow_none=True)


class GetCommentRequest(DefaultSchema):
    id = fields.Integer(required=True)


class GetCommentResponse(DefaultSchema):
    comment_view = fields.Nested(CommentView, required=True)


class LikeCommentRequest(DefaultSchema):
    comment_id = fields.Integer(required=True)
    score = fields.Integer(required=True, metadata={"example": 1, "description": "-1 to downvote, 1 to upvote, 0 to revert previous vote"})
    private = fields.Boolean(metadata={"description": "private votes are not federated to other instances", "default": False})
    emoji = fields.String()


class SaveCommentRequest(DefaultSchema):
    comment_id = fields.Integer(required=True)
    save = fields.Boolean(required=True)


class SubscribeCommentRequest(DefaultSchema):
    comment_id = fields.Integer(required=True)
    subscribe = fields.Boolean(required=True)


class CreateCommentRequest(DefaultSchema):
    body = fields.String(required=True)
    post_id = fields.Integer(required=True)
    parent_id = fields.Integer()
    language_id = fields.Integer()


class EditCommentRequest(DefaultSchema):
    body = fields.String(required=True)
    comment_id = fields.Integer(required=True)
    language_id = fields.Integer()
    distinguished = fields.Boolean(metadata={"default": False, "description": "Visibly mark reply as from a moderator in the web UI"})


class DeleteCommentRequest(DefaultSchema):
    comment_id = fields.Integer(required=True)
    deleted = fields.Boolean(required=True)


class ReportCommentRequest(DefaultSchema):
    comment_id = fields.Integer(required=True)
    reason = fields.String(required=True)
    description = fields.String()
    report_remote = fields.Boolean(metadata={"default": True, "description": "Also send report to originating instance"})


class GetCommentReportResponse(DefaultSchema):
    comment_report_view = fields.Nested(CommentReportView, required=True)


class GetCommentReportListRequest(DefaultSchema):
    comment_id = fields.Integer(metadata={"description": "Get the reports for a single comment"})
    community_id = fields.Integer(metadata={"description": "Limit reports to within a single community"})
    limit = fields.Integer(metadata={"default": 20})
    page = fields.Integer(metadata={"default": 1})
    unresolved_only = fields.Boolean(metadata={"default": True})


class GetCommentReportListResponse(DefaultSchema):
    comment_reports = fields.List(fields.Nested(CommentReportView), required=True)
    next_page = fields.String(allow_none=True)


class RemoveCommentRequest(DefaultSchema):
    comment_id = fields.Integer(required=True)
    removed = fields.Boolean(required=True)
    reason = fields.String()


class MarkCommentAsReadRequest(DefaultSchema):
    comment_reply_id = fields.Integer(required=True)
    read = fields.Boolean(required=True)


class MarkCommentAsAnswerRequest(DefaultSchema):
    comment_reply_id = fields.Integer(required=True)
    answer = fields.Boolean(required=True)


class MarkCommentAsDistinguishedRequest(DefaultSchema):
    comment_reply_id = fields.Integer(required=True)
    distinguished = fields.Boolean(required=True)


class GetCommentReplyResponse(DefaultSchema):
    comment_reply_view = fields.Nested(CommentReplyView, required=True)


class LockCommentRequest(DefaultSchema):
    comment_id = fields.Integer(required=True)
    locked = fields.Boolean(required=True)


class ListCommentLikesRequest(DefaultSchema):
    comment_id = fields.Integer(required=True)
    page = fields.Integer(metadata={"default": 1})
    limit = fields.Integer(metadata={"default": 50})


class CommentLikeView(DefaultSchema):
    score = fields.Integer(required=True)
    creator_banned_from_community = fields.Boolean(required=True)
    creator_banned = fields.Boolean(required=True)
    creator = fields.Nested(Person, required=True)


class ListCommentLikesResponse(DefaultSchema):
    comment_likes = fields.List(fields.Nested(CommentLikeView), required=True)
    next_page = fields.String(allow_none=True)


class ListPostLikesRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    page = fields.Integer(metadata={"default": 1})
    limit = fields.Integer(metadata={"default": 50})


class PostLikeView(CommentLikeView):
    pass


class ListPostLikesResponse(DefaultSchema):
    post_likes = fields.List(fields.Nested(PostLikeView), required=True)
    next_page = fields.String(allow_none=True)


class GetPostRequest(DefaultSchema):
    id = fields.Integer(required=True)


class GetPostResponse(DefaultSchema):
    post_view = fields.Nested(PostView, required=True)
    community_view = fields.Nested(CommunityView)
    moderators = fields.List(fields.Nested(CommunityModeratorView))
    cross_posts = fields.List(fields.Nested(PostView))


class GetSiteMetadataRequest(DefaultSchema):
    url = fields.String()

class SiteMetadataView(DefaultSchema):
    title = fields.String()
    description = fields.String()
    image = fields.String()
    embed_video_url = fields.String()


class GetSiteMetadataResponse(DefaultSchema):
    metadata = fields.Nested(SiteMetadataView, required=True)


class LikePostRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    score = fields.Integer(required=True)
    private = fields.Boolean()
    emoji = fields.String()


class SavePostRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    save = fields.Boolean(required=True)


class SubscribePostRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    subscribe = fields.Boolean(required=True)


class CreatePostRequest(DefaultSchema):
    title = fields.String(required=True, validate=validate_non_empty_string)
    community_id = fields.Integer(required=True)
    alt_text = fields.String(metadata={"description": "Will be used for image posts or link posts that point to images"})
    body = fields.String()
    url = fields.String(metadata={"format": "url"})
    nsfw = fields.Boolean()
    ai_generated = fields.Boolean()
    language_id = fields.Integer()
    event = fields.Nested(PostEvent, allow_none=True)
    poll = fields.Nested(PostPoll, allow_none=True)


class EditPostRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    alt_text = fields.String(allow_none=True, metadata={"description": "Pass null to remove the existing alt text"})
    title = fields.String()
    body = fields.String()
    url = fields.String(allow_none=True, metadata={"description": "Pass value of null to remove the post url", "format": "url"})
    nsfw = fields.Boolean()
    language_id = fields.Integer()
    event = fields.Nested(PostEvent, allow_none=True)
    poll = fields.Nested(PostPoll, allow_none=True)
    tags = fields.String(allow_none=True, metadata={"description": "Hashtags, separated by commas with no hash character"})
    flair = fields.String(allow_none=True, metadata={"description": "Flair, separated by commas with no hash character"})


class DeletePostRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    deleted = fields.Boolean(required=True)


class ReportPostRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    reason = fields.String(required=True)
    description = fields.String()
    report_remote = fields.Boolean(metadata={"default": True, "description": "Also send report to originating instance"})


class PostReport(DefaultSchema):
    id = fields.Integer(required=True)
    creator_id = fields.Integer(required=True)
    post_id = fields.Integer(required=True)
    original_post_name = fields.String(required=True)
    original_post_body = fields.String(required=True)
    reason = fields.String(required=True)
    resolved = fields.Boolean(required=True)
    published = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class PostReportView(DefaultSchema):
    post_report = fields.Nested(PostReport, required=True)
    post = fields.Nested(Post, required=True)
    community = fields.Nested(Community, required=True)
    creator = fields.Nested(Person, required=True)
    post_creator = fields.Nested(Person, required=True)
    counts = fields.Nested(PostAggregates, required=True)
    creator_banned_from_community = fields.Boolean(required=True)
    creator_is_moderator = fields.Boolean(required=True)
    creator_is_admin = fields.Boolean(required=True)
    creator_blocked = fields.Boolean(required=True)
    subscribed = fields.String(validate=validate.OneOf(subscribed_type_list), required=True)
    saved = fields.Boolean(required=True)


class PostReportResponse(DefaultSchema):
    post_report_view = fields.Nested(PostReportView, required=True)


class LockPostRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    locked = fields.Boolean(required=True)


class HidePostRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    hidden = fields.Boolean(required=True)


class FeaturePostRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    featured = fields.Boolean(required=True)
    feature_type = fields.String(validate=validate.OneOf(feature_type_list), metadata={"default": "Community"})


class RemovePostRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    removed = fields.Boolean(required=True)
    reason = fields.String()


class MarkPostAsReadRequest(DefaultSchema):
    read = fields.Boolean(required=True)
    post_id = fields.Integer()
    post_ids = fields.List(fields.Integer())


class SuccessResponse(DefaultSchema):
    success = fields.Boolean(required=True)


class GetPostRepliesRequest(DefaultSchema):
    post_id = fields.Integer()
    parent_id = fields.Integer()
    sort = fields.String(validate=validate.OneOf(comment_sort_list))
    max_depth = fields.Integer()
    page = fields.String()
    limit = fields.Integer()
    parent_id = fields.Integer()


class PostReplyView(CommentView):
    post = fields.Nested(Post)
    community = fields.Nested(Community)
    replies = fields.List(fields.Nested(lambda: PostReplyView))


class GetPostRepliesResponse(DefaultSchema):
    comments = fields.List(fields.Nested(PostReplyView))
    next_page = fields.String(allow_none=True)


class ListPostsRequest(Schema):
    q = fields.String()
    sort = fields.String(validate=validate.OneOf(post_sort_list), metadata={"default": "Hot"})
    type_ = fields.String(validate=validate.OneOf(listing_type_list), metadata={"default": "All"})
    community_name = fields.String()
    community_id = fields.Integer()
    saved_only = fields.Boolean(metadata={"default": False})
    nsfw = fields.String(validate=validate.OneOf(nsfw_type_list), metadata={"default": 'Include'}, required=False)
    person_id = fields.Integer()
    limit = fields.Integer(metadata={"default": 50})
    page = fields.Integer(metadata={"default": 1})
    liked_only = fields.Boolean(metadata={"default": False})
    feed_id = fields.Integer()
    topic_id = fields.Integer()
    ignore_sticky = fields.Boolean(metadata={"default": False, "description": "Ignores a post's sticky state when sorting"})


class ListPostsRequest2(ListPostsRequest):
    page = fields.String(metadata={"default": ""})


# Private Message Schemas
class PrivateMessage(DefaultSchema):
    id = fields.Integer(required=True)
    creator_id = fields.Integer(required=True)
    recipient_id = fields.Integer(required=True)
    content = fields.String(required=True)
    deleted = fields.Boolean(required=True)
    read = fields.Boolean(required=True)
    published = fields.String(validate=validate_datetime_string, required=True, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    ap_id = fields.Url(required=True)
    local = fields.Boolean(required=True)


class PrivateMessageView(DefaultSchema):
    private_message = fields.Nested(PrivateMessage, required=True)
    creator = fields.Nested(Person, required=True)
    recipient = fields.Nested(Person, required=True)
    conversation_id = fields.Integer()


class PrivateMessageResponse(DefaultSchema):
    private_message_view = fields.Nested(PrivateMessageView, required=True)


class ListPrivateMessagesRequest(DefaultSchema):
    page = fields.Integer()
    limit = fields.Integer()
    unread_only = fields.Boolean()


class ListPrivateMessagesResponse(DefaultSchema):
    private_messages = fields.List(fields.Nested(PrivateMessageView), required=True)


class GetPrivateMessageConversationRequest(DefaultSchema):
    person_id = fields.Integer(metadata={"description": "One of either person_id or conversation_id must be specified"})
    conversation_id = fields.Integer(metadata={"description": "One of either person_id or conversation_id must be specified"})
    page = fields.Integer(metadata={"default": 1})
    limit = fields.Integer(metadata={"default": 10})

    @validates_schema
    def validate_input(self, data, **kwargs):
        if "person_id" not in data and "conversation_id" not in data:
            raise ValidationError("One of either person_id or conversation_id must be specified")


class GetPrivateMessageConversationResponse(DefaultSchema):
    private_messages = fields.List(fields.Nested(PrivateMessageView), required=True)


class LeaveConversationRequest(DefaultSchema):
    conversation_id = fields.Integer(required=True)


class CreatePrivateMessageRequest(DefaultSchema):
    content = fields.String(required=True)
    recipient_id = fields.Integer(required=True)


class EditPrivateMessageRequest(DefaultSchema):
    private_message_id = fields.Integer(required=True)
    content = fields.String(required=True)


class MarkPrivateMessageAsReadRequest(DefaultSchema):
    private_message_id = fields.Integer(required=True)
    read = fields.Boolean(required=True)


class DeletePrivateMessageRequest(DefaultSchema):
    private_message_id = fields.Integer(required=True)
    deleted = fields.Boolean(required=True)


class ReportPrivateMessageRequest(DefaultSchema):
    private_message_id = fields.Integer(required=True)
    reason = fields.String(required=True)


# Upload Schemas
class ImageUploadRequest(DefaultSchema):
    file = fields.Raw(required=True, metadata={"type": "string", "format": "binary"})


class ImageUploadResponse(DefaultSchema):
    url = fields.String(required=True, metadata={"format": "url"})
    liked_only = fields.Boolean()
    saved_only = fields.Boolean()
    q = fields.String()


class ImageDeleteRequest(DefaultSchema):
    file = fields.String(required=True)


class ImageDeleteResponse(DefaultSchema):
    result = fields.String(required=True)


class ListPostsResponse(Schema):
    posts = fields.List(fields.Nested(PostView), required=True)
    next_page = fields.String(allow_none=True)


class PostSetFlairRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    flair_id_list = fields.List(
        fields.Integer(),
        allow_none=True,
        metadata={"description": "A list of all the flair id to assign to the post. Either pass an empty list or null to remove flair"})


class PostSetFlairResponse(PostView):
    pass


class PollVoteRequest(DefaultSchema):
    post_id = fields.Integer(required=True)
    choice_id = fields.List(fields.Integer(), required=True, metadata={"description": "Must have a length of 1 for a poll in single vote mode."})


class PollVoteResponse(DefaultSchema):
    post_view = fields.Nested(PostView, required=True)


class GetModLogRequest(DefaultSchema):
    mod_person_id = fields.Integer()
    community_id = fields.Integer()
    page = fields.Integer()
    limit = fields.Integer()
    type_ = fields.String(validate=validate.OneOf(modlog_type_list))
    other_person_id = fields.Integer()
    post_id = fields.Integer()
    comment_id = fields.Integer()


class ModRemovePost(DefaultSchema):
    id = fields.Integer(required=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    post_id = fields.Integer(required=True, allow_none=True)
    reason = fields.String(allow_none=True)
    removed = fields.Boolean(required=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModRemovePostView(DefaultSchema):
    mod_remove_post = fields.Nested(ModRemovePost, required=True)
    moderator = fields.Nested(Person, allow_none=True)
    post = fields.Nested(Post, allow_none=True)
    community = fields.Nested(Community, allow_none=True)


class ModLockPost(DefaultSchema):
    id = fields.Integer(required=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    post_id = fields.Integer(required=True, allow_none=True)
    locked = fields.Boolean(required=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModLockPostView(DefaultSchema):
    mod_lock_post = fields.Nested(ModLockPost, required=True)
    moderator = fields.Nested(Person, allow_none=True)
    post = fields.Nested(Post, allow_none=True)
    community = fields.Nested(Community, allow_none=True)


class ModFeaturePost(DefaultSchema):
    id = fields.Integer(required=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    post_id = fields.Integer(required=True, allow_none=True)
    featured = fields.Boolean(required=True)
    is_featured_community = fields.Boolean(required=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModFeaturePostView(DefaultSchema):
    mod_feature_post = fields.Nested(ModFeaturePost, required=True)
    moderator = fields.Nested(Person, allow_none=True)
    post = fields.Nested(Post, allow_none=True)
    community = fields.Nested(Community, allow_none=True)


class ModRemoveComment(DefaultSchema):
    id = fields.Integer(required=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    comment_id = fields.Integer(required=True, allow_none=True)
    reason = fields.String(allow_none=True)
    removed = fields.Boolean(required=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModRemoveCommentView(DefaultSchema):
    mod_remove_comment = fields.Nested(ModRemoveComment, required=True)
    moderator = fields.Nested(Person, allow_none=True)
    comment = fields.Nested(Comment, allow_none=True)
    commenter = fields.Nested(Person, allow_none=True)
    post = fields.Nested(Post, allow_none=True)
    community = fields.Nested(Community, allow_none=True)


class ModRemoveCommunity(DefaultSchema):
    id = fields.Integer(required=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    community_id = fields.Integer(required=True, allow_none=True)
    reason = fields.String(allow_none=True)
    removed = fields.Boolean(required=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModRemoveCommunityView(DefaultSchema):
    mod_remove_community = fields.Nested(ModRemoveCommunity, required=True)
    moderator = fields.Nested(Person, allow_none=True)
    community = fields.Nested(Community, allow_none=True)


class ModBanFromCommunity(DefaultSchema):
    id = fields.Integer(required=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    other_person_id = fields.Integer(required=True, allow_none=True)
    community_id = fields.Integer(required=True, allow_none=True)
    reason = fields.String(allow_none=True)
    banned = fields.Boolean(required=True)
    expires = fields.String(allow_none=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModBanFromCommunityView(DefaultSchema):
    mod_ban_from_community = fields.Nested(ModBanFromCommunity, required=True)
    moderator = fields.Nested(Person, allow_none=True)
    community = fields.Nested(Community, allow_none=True)
    banned_person = fields.Nested(Person, allow_none=True)


class ModBan(DefaultSchema):
    id = fields.Integer(required=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    other_person_id = fields.Integer(required=True, allow_none=True)
    reason = fields.String(allow_none=True)
    banned = fields.Boolean(required=True)
    expires = fields.String(allow_none=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModBanView(DefaultSchema):
    mod_ban = fields.Nested(ModBan, required=True)
    moderator = fields.Nested(Person, allow_none=True)
    banned_person = fields.Nested(Person, allow_none=True)


class ModAddCommunity(DefaultSchema):
    id = fields.Integer(required=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    other_person_id = fields.Integer(required=True, allow_none=True)
    community_id = fields.Integer(required=True, allow_none=True)
    removed = fields.Boolean(required=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModAddCommunityView(DefaultSchema):
    mod_add_community = fields.Nested(ModAddCommunity, required=True)
    moderator = fields.Nested(Person, allow_none=True)
    community = fields.Nested(Community, allow_none=True)
    modded_person = fields.Nested(Person, allow_none=True)


class ModTransferCommunity(DefaultSchema):
    id = fields.Integer(required=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    other_person_id = fields.Integer(required=True, allow_none=True)
    community_id = fields.Integer(required=True, allow_none=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModTransferCommunityView(DefaultSchema):
    mod_transfer_community = fields.Nested(ModTransferCommunity, required=True)
    moderator = fields.Nested(Person, allow_none=True)
    community = fields.Nested(Community, required=True)
    modded_person = fields.Nested(Person, allow_none=True)


class ModAdd(DefaultSchema):
    id = fields.Integer(required=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    other_person_id = fields.Integer(required=True, allow_none=True)
    removed = fields.Boolean(required=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModAddView(DefaultSchema):
    mod_add = fields.Nested(ModAdd, required=True)
    moderator = fields.Nested(Person, allow_none=True)
    modded_person = fields.Nested(Person, allow_none=True)


class AdminPurgePerson(DefaultSchema):
    id = fields.Integer(required=True)
    admin_person_id = fields.Integer(required=True)
    reason = fields.String(allow_none=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class AdminPurgePersonView(DefaultSchema):
    admin_purge_person = fields.Nested(AdminPurgePerson, required=True)
    admin = fields.Nested(Person)


class AdminPurgeCommunity(DefaultSchema):
    id = fields.Integer(required=True)
    admin_person_id = fields.Integer(required=True)
    reason = fields.String(allow_none=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class AdminPurgeCommunityView(DefaultSchema):
    admin_purge_community = fields.Nested(AdminPurgeCommunity, required=True)
    admin = fields.Nested(Person)


class AdminPurgePost(DefaultSchema):
    id = fields.Integer(required=True)
    admin_person_id = fields.Integer(required=True)
    community_id = fields.Integer(required=True, allow_none=True)
    reason = fields.String(allow_none=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class AdminPurgePostView(DefaultSchema):
    admin_purge_post = fields.Nested(AdminPurgePost, required=True)
    admin = fields.Nested(Person)
    community = fields.Nested(Community, required=True)


class AdminPurgeComment(DefaultSchema):
    id = fields.Integer(required=True)
    admin_person_id = fields.Integer(required=True)
    post_id = fields.Integer(required=True)
    reason = fields.String(allow_none=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class AdminPurgeCommentView(DefaultSchema):
    admin_purge_comment = fields.Nested(AdminPurgeComment, required=True)
    admin = fields.Nested(Person)
    post = fields.Nested(Post, required=True)


class ModHideCommunity(DefaultSchema):
    id = fields.Integer(required=True)
    community_id = fields.Integer(required=True, allow_none=True)
    mod_person_id = fields.Integer(required=True, allow_none=True)
    reason = fields.String(allow_none=True)
    hidden = fields.Boolean(required=True)
    when_ = fields.String(required=True, validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})


class ModHideCommunityView(DefaultSchema):
    mod_hide_community = fields.Nested(ModHideCommunity, required=True)
    admin = fields.Nested(Person)
    community = fields.Nested(Community, required=True)


class GetModLogResponse(DefaultSchema):
    removed_posts = fields.List(fields.Nested(ModRemovePostView), required=True)
    locked_posts = fields.List(fields.Nested(ModLockPostView), required=True)
    featured_posts = fields.List(fields.Nested(ModFeaturePostView), required=True)
    removed_comments = fields.List(fields.Nested(ModRemoveCommentView), required=True)
    removed_communities = fields.List(fields.Nested(ModRemoveCommunityView), required=True)
    banned_from_community = fields.List(fields.Nested(ModBanFromCommunityView), required=True)
    banned = fields.List(fields.Nested(ModBanView), required=True)
    added_to_community = fields.List(fields.Nested(ModAddCommunityView), required=True)
    transferred_to_community = fields.List(fields.Nested(ModTransferCommunityView), required=True)
    added = fields.List(fields.Nested(ModAddView), required=True)
    admin_purged_persons = fields.List(fields.Nested(AdminPurgePersonView), required=True)
    admin_purged_communities = fields.List(fields.Nested(AdminPurgeCommunityView), required=True)
    admin_purged_posts = fields.List(fields.Nested(AdminPurgePostView), required=True)
    admin_purged_comments = fields.List(fields.Nested(AdminPurgeCommentView), required=True)
    hidden_communities = fields.List(fields.Nested(ModHideCommunityView), required=True)


class UserRegistration(DefaultSchema):
    answer = fields.String(required=True, allow_none=True)
    applied_at = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    country_code = fields.String()
    email = fields.String(required=True, allow_none=True)
    ip_address = fields.String(required=True, allow_none=True)
    throwaway_email = fields.Boolean()
    user_id = fields.Integer(required=True)
    user_name = fields.String(required=True)
    status = fields.String(validate=validate.OneOf(["approved", "awaiting review"]), required=True)
    approved_by = fields.Nested(Person)
    approved_at = fields.String(validate=validate_datetime_string, metadata={"example": "2025-06-07T02:29:07.980084Z", "format": "datetime"})
    referrer = fields.String()


class GetRegistrationList(DefaultSchema):
    limit = fields.Integer(metadata={"default": 30})
    page = fields.Integer(metadata={"default": 1})
    pending_only = fields.Boolean(metadata={"default": True})
    sort = fields.String(validate=validate.OneOf(["Old", "New"]), metadata={"default": "New"})


class GetRegistrationListResponse(DefaultSchema):
    registrations = fields.List(fields.Nested(UserRegistration), required=True)


class RegistrationApproveRequest(DefaultSchema):
    approve = fields.Boolean(required=True)
    user_id = fields.Integer(required=True)


class UserRegistrationRequest(DefaultSchema):
    username = fields.String(required=True)
    password = fields.String(required=True)
    password_verify = fields.String(required=True)
    show_nsfw = fields.Boolean()
    email = fields.String()
    captcha_uuid = fields.String()
    captcha_answer = fields.String()
    honeypot = fields.String()
    answer = fields.String()


class UserRegistrationResponse(DefaultSchema):
    jwt = fields.String()
    registration_created = fields.Boolean()
    verify_email_sent = fields.Boolean()


class CaptchaFields(DefaultSchema):
    png = fields.String()
    wav = fields.String()
    uuid = fields.String()


class FetchCaptchaResponse(DefaultSchema):
    ok = fields.List(fields.Nested(CaptchaFields))
