VERSION = '1.7.0-dev'

POST_TYPE_LINK = 1
POST_TYPE_ARTICLE = 2
POST_TYPE_IMAGE = 3
POST_TYPE_VIDEO = 4
POST_TYPE_POLL = 5
POST_TYPE_EVENT = 6

POST_TYPE_NAMES = {
    POST_TYPE_LINK: "Link",
    POST_TYPE_ARTICLE: "Discussion",
    POST_TYPE_IMAGE: "Image",
    POST_TYPE_VIDEO: "Video",
    POST_TYPE_POLL: "Poll",
    POST_TYPE_EVENT: "Event",
}

POST_STATUS_SCHEDULED = -2
POST_STATUS_DRAFT = -1
POST_STATUS_REVIEWING = 0
POST_STATUS_PUBLISHED = 1

DATETIME_MS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# Community subscription levels
SUBSCRIPTION_OWNER = 3
SUBSCRIPTION_MODERATOR = 2
SUBSCRIPTION_MEMBER = 1
SUBSCRIPTION_NONMEMBER = 0
SUBSCRIPTION_PENDING = -1
SUBSCRIPTION_BANNED = -2

THREAD_CUTOFF_DEPTH = 5

REPORT_STATE_NEW = 0
REPORT_STATE_ESCALATED = 1
REPORT_STATE_APPEALED = 2
REPORT_STATE_RESOLVED = 3
REPORT_STATE_DISCARDED = -1

REPORT_TYPE_USER = 0
REPORT_TYPE_POST = 1
REPORT_TYPE_REPLY = 2
REPORT_TYPE_COMMUNITY = 3
REPORT_TYPE_MESSAGE = 4

# different types of content notification that people can have. e.g. when a new post is made by a user or in a community.
# see NotificationSubscription in models.py

# -- user level ---
NOTIF_USER = 0  # if I am following userA I get notified if that user posts something
NOTIF_COMMUNITY = 1  # if I elect to get notified about new posts in a community
NOTIF_TOPIC = 2  # if I elect to get notified about new posts in communities in a topic
NOTIF_POST = 3  # this is new top-level comments on a post I am subscribed to
# I am auto subscribed to my posts, but I can elect to sub to other posts
NOTIF_REPLY = 4  # replies to a comment I made, or one I subscribed to
NOTIF_FEED = 5  # not actually used anywhere yet, but will be the same as NOTIF_TOPIC
# but for communities in a feed
NOTIF_MENTION = 6  # I have been mentioned in a post or comment
NOTIF_MESSAGE = 7  # I have a new chat message
NOTIF_BAN = 8  # I have been banned from a community
NOTIF_UNBAN = 9  # I have been un-banned from a community
NOTIF_NEW_MOD = 10  # I have been made a moderator for a community
NOTIF_REMINDER = 11  # A reminder that the user set before
NOTIF_ANSWER = 12  # An answer was accepted as the preferred one

# --- mod/admin level ---
NOTIF_REPORT = 20  # a user, post, comment, or community have been reported

# --- admin level ---
NOTIF_REPORT_ESCALATION = 40  # a USER, POST, or COMMENT report has been escalated from mods to admins
NOTIF_REGISTRATION = 41  # a new registration / sign up has been generated

# --model/db default--
NOTIF_DEFAULT = 999  # default entry

ROLE_STAFF = 3
ROLE_ADMIN = 4

DOWNVOTE_ACCEPT_NONE = -1
DOWNVOTE_ACCEPT_ALL = 0
DOWNVOTE_ACCEPT_MEMBERS = 2
DOWNVOTE_ACCEPT_INSTANCE = 4
DOWNVOTE_ACCEPT_TRUSTED = 6

MICROBLOG_APPS = ["mastodon", "misskey", "akkoma", "iceshrimp", "pleroma", "fedibird"]

SRC_WEB = 1
SRC_PUB = 2
SRC_API = 3
SRC_PLD = 4  # admin preload form to seed communities
SRC_PLG = 5  # plugins

APLOG_IN = True

APLOG_MONITOR = (True, 'Debug this')

APLOG_SUCCESS = (True, 'success')
APLOG_FAILURE = (True, 'failure')
APLOG_IGNORED = (True, 'ignored')
APLOG_PROCESSING = (True, 'processing')

APLOG_NOTYPE = (True, 'Unknown')
APLOG_DUPLICATE = (True, 'Duplicate')
APLOG_FOLLOW = (True, 'Follow')
APLOG_ACCEPT = (True, 'Accept')
APLOG_DELETE = (True, 'Delete')
APLOG_CHATMESSAGE = (True, 'Create ChatMessage')
APLOG_CREATE = (True, 'Create')
APLOG_UPDATE = (True, 'Update')
APLOG_LIKE = (True, 'Like')
APLOG_DISLIKE = (True, 'Dislike')
APLOG_RATE = (True, 'Rate')
APLOG_QA = (True, 'QA')
APLOG_REPORT = (True, 'Report')
APLOG_USERBAN = (True, 'User Ban')
APLOG_LOCK = (True, 'Post Lock')
APLOG_QUOTEBOOST = (True, 'Quote Boost')

APLOG_UNDO_FOLLOW = (True, 'Undo Follow')
APLOG_UNDO_DELETE = (True, 'Undo Delete')
APLOG_UNDO_VOTE = (True, 'Undo Vote')
APLOG_UNDO_USERBAN = (True, 'Undo User Ban')

APLOG_ADD = (True, 'Add')
APLOG_REMOVE = (True, 'Remove')
APLOG_MOVE = (True, 'Move')

APLOG_ANNOUNCE = (True, 'Announce')
APLOG_PT_VIEW = (True, 'PeerTube View')

REQUEST_TIMEOUT = 2

INVITE_APPLY = 1
INVITE_MEMBERS_ONLY = 2
INVITE_MODS_ONLY = 3
INVITE_OWNER_ONLY = 4
