import logging
import os

from dotenv import load_dotenv

import app.constants

logger = logging.getLogger(__name__)

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

def safe_int_env(var_name: str, default: int = 0) -> int:
    raw = os.getenv(var_name, default)
    try:
        return int(raw)
    except(ValueError, TypeError):
        logger.warning(f"env {var_name}={raw} is not a valid int. Setting default: {default}")
        return default


class Config(object):
    SERVER_NAME = os.environ.get('SERVER_NAME').lower() or 'localhost'
    SECRET_KEY = os.environ.get('SECRET_KEY')
    JWT_EXPIRY_DAYS = int(os.environ.get('JWT_EXPIRY_DAYS') or 365)
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
                              'sqlite:///' + os.path.join(basedir, 'app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAIL_SERVER = os.environ.get('MAIL_SERVER') or None
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 25)
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS') is not None
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME') or None
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD') or None
    MAIL_FROM = os.environ.get('MAIL_FROM') or 'noreply@' + os.environ.get('SERVER_NAME')
    ERRORS_TO = os.environ.get('ERRORS_TO') or ''
    LANGUAGES = ['ca', 'de', 'en', 'es', 'fr', 'ja', 'uk', 'zh']
    FULL_AP_CONTEXT = bool(int(os.environ.get('FULL_AP_CONTEXT', 0)))
    CACHE_TYPE = os.environ.get('CACHE_TYPE') or 'FileSystemCache'
    CACHE_REDIS_URL = os.environ.get('CACHE_REDIS_URL') or 'redis://localhost:6379/1'
    CACHE_DIR = os.environ.get('CACHE_DIR') or '/dev/shm/pyfedi'
    CACHE_DEFAULT_TIMEOUT = 300
    CACHE_THRESHOLD = 1000
    CACHE_KEY_PREFIX = 'pyfedi'
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL') or 'redis://localhost:6379/0'
    RESULT_BACKEND = os.environ.get('RESULT_BACKEND') or 'redis://localhost:6379/0'
    SQLALCHEMY_ECHO = False     # set to true to see SQL in console
    WTF_CSRF_TIME_LIMIT = None  # a value of None ensures csrf token is valid for the lifetime of the session
    HTTP_PROTOCOL = os.environ.get('HTTP_PROTOCOL') or 'https'  # useful during development

    BOUNCE_ADDRESS = os.environ.get('BOUNCE_ADDRESS') or MAIL_FROM or ''    # Warning: all emails in this inbox will be deleted!
    BOUNCE_HOST = os.environ.get('BOUNCE_HOST') or ''
    BOUNCE_HOST_TYPE = os.environ.get('BOUNCE_HOST_TYPE') or ''     # imap or pop3
    BOUNCE_USERNAME = os.environ.get('BOUNCE_USERNAME') or ''
    BOUNCE_PASSWORD = os.environ.get('BOUNCE_PASSWORD') or ''

    BOOTSTRAP_SERVE_LOCAL = True

    SENTRY_DSN = os.environ.get('SENTRY_DSN') or None

    AWS_REGION = os.environ.get('AWS_REGION') or None

    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', '1') in ('1', 'true', 'True')
    SESSION_COOKIE_HTTPONLY = os.environ.get('SESSION_COOKIE_HTTPONLY', '1') in ('1', 'true', 'True')
    SESSION_COOKIE_SAMESITE = 'Lax'

    CLOUDFLARE_API_TOKEN = os.environ.get('CLOUDFLARE_API_TOKEN') or ''
    CLOUDFLARE_ZONE_ID = os.environ.get('CLOUDFLARE_ZONE_ID') or ''

    SPICY_UNDER_10 = float(os.environ.get('SPICY_UNDER_10', 1.0))
    SPICY_UNDER_30 = float(os.environ.get('SPICY_UNDER_30', 1.0))
    SPICY_UNDER_60 = float(os.environ.get('SPICY_UNDER_60', 1.0))

    IPINFO_TOKEN = os.environ.get('IPINFO_TOKEN') or ''

    NUM_CPU = int(os.environ.get('NUM_CPU') or 0)

    DB_POOL_SIZE = os.environ.get('DB_POOL_SIZE') or 10
    DB_MAX_OVERFLOW = os.environ.get('DB_MAX_OVERFLOW') or 30

    LOG_ACTIVITYPUB_TO_DB = os.environ.get('LOG_ACTIVITYPUB_TO_DB') or False
    LOG_ACTIVITYPUB_TO_FILE = os.environ.get('LOG_ACTIVITYPUB_TO_FILE') or False

    STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY') or ''
    STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY') or ''
    STRIPE_MONTHLY_SMALL = os.environ.get('STRIPE_MONTHLY_SMALL') or ''
    STRIPE_MONTHLY_BIG = os.environ.get('STRIPE_MONTHLY_BIG') or ''
    STRIPE_MONTHLY_SMALL_TEXT = os.environ.get('STRIPE_MONTHLY_SMALL_TEXT') or ''
    STRIPE_MONTHLY_BIG_TEXT = os.environ.get('STRIPE_MONTHLY_BIG_TEXT') or ''
    WEBHOOK_SIGNING_SECRET = os.environ.get('WEBHOOK_SIGNING_SECRET') or ''

    S3_REGION = os.environ.get('S3_REGION') or ''
    S3_ENDPOINT = os.environ.get('S3_ENDPOINT') or ''
    S3_BUCKET = os.environ.get('S3_BUCKET') or ''
    S3_ACCESS_KEY = os.environ.get('S3_ACCESS_KEY') or ''
    S3_ACCESS_SECRET = os.environ.get('S3_ACCESS_SECRET') or ''
    S3_PUBLIC_URL = os.environ.get('S3_PUBLIC_URL') or ''
    S3_PUBLIC_ACL = os.environ.get('S3_PUBLIC_ACL') or False
    S3_STORAGE_CLASS = os.environ.get('S3_STORAGE_CLASS') or ''

    GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID') or ''
    GOOGLE_OAUTH_SECRET = os.environ.get('GOOGLE_OAUTH_SECRET') or ''

    MASTODON_OAUTH_CLIENT_ID = os.environ.get('MASTODON_OAUTH_CLIENT_ID') or ""
    MASTODON_OAUTH_SECRET = os.environ.get("MASTODON_OAUTH_SECRET") or ""
    MASTODON_OAUTH_DOMAIN = os.environ.get("MASTODON_OAUTH_DOMAIN") or ""

    DISCORD_OAUTH_CLIENT_ID = os.environ.get('DISCORD_OAUTH_CLIENT_ID') or ""
    DISCORD_OAUTH_SECRET = os.environ.get('DISCORD_OAUTH_SECRET') or ""

    # enable the aplha api
    ENABLE_ALPHA_API = os.environ.get('ENABLE_ALPHA_API') or False
    SKIP_RATE_LIMIT_IPS = os.environ.get('SKIP_RATE_LIMIT_IPS') or ['127.0.0.1']
    SERVE_API_DOCS = os.environ.get('SERVE_API_DOCS') or False

    IMAGE_HASHING_ENDPOINT = os.environ.get('IMAGE_HASHING_ENDPOINT') or ''

    FLAG_THROWAWAY_EMAILS = os.environ.get('FLAG_THROWAWAY_EMAILS') or False

    NOTIF_SERVER = os.environ.get('NOTIF_SERVER') or ''

    # CORS configuration
    CORS_ALLOW_ORIGIN = os.environ.get('CORS_ALLOW_ORIGIN') or '*'

    PAGE_LENGTH = int(os.environ.get('PAGE_LENGTH') or 100)

    # Image formats
    MEDIA_IMAGE_MAX_DIMENSION = int(os.environ.get('MEDIA_IMAGE_MAX_DIMENSION') or 2000)

    MEDIA_IMAGE_FORMAT = os.environ.get('MEDIA_IMAGE_FORMAT') or ''
    MEDIA_IMAGE_QUALITY = int(os.environ.get('MEDIA_IMAGE_QUALITY') or 90)

    MEDIA_IMAGE_MEDIUM_FORMAT = os.environ.get('MEDIA_IMAGE_MEDIUM_FORMAT') or 'WEBP'
    MEDIA_IMAGE_MEDIUM_QUALITY = int(os.environ.get('MEDIA_IMAGE_MEDIUM_QUALITY') or 93)

    MEDIA_IMAGE_THUMBNAIL_FORMAT = os.environ.get('MEDIA_IMAGE_THUMBNAIL_FORMAT') or 'WEBP'
    MEDIA_IMAGE_THUMBNAIL_QUALITY = int(os.environ.get('MEDIA_IMAGE_THUMBNAIL_QUALITY') or 93)

    FILE_UPLOAD_QUOTA = int(os.environ.get('FILE_UPLOAD_QUOTA') or 52428800)   # default 50 MB

    # LDAP configuration - common config
    LDAP_SERVER = os.environ.get('LDAP_SERVER') or ''
    LDAP_PORT = int(os.environ.get('LDAP_PORT') or 389)
    LDAP_USE_SSL = os.environ.get('LDAP_USE_SSL', '0') in ('1', 'true', 'True')
    LDAP_USE_TLS = os.environ.get('LDAP_USE_TLS', '0') in ('1', 'true', 'True')
    LDAP_BASE_DN = os.environ.get('LDAP_BASE_DN') or ''

    # LDAP configuration - used to write to, so other services can use their instance credentials to log in
    LDAP_WRITE_ENABLE = os.environ.get('LDAP_WRITE_ENABLE', '0') in ('1', 'true', 'True')
    LDAP_WRITE_BIND_DN = os.environ.get('LDAP_WRITE_BIND_DN') or ''
    LDAP_WRITE_BIND_PASSWORD = os.environ.get('LDAP_WRITE_BIND_PASSWORD') or ''
    LDAP_WRITE_USER_FILTER = os.environ.get('LDAP_WRITE_USER_FILTER') or '(uid={username})'
    LDAP_WRITE_ATTR_USERNAME = os.environ.get('LDAP_WRITE_ATTR_USERNAME') or 'uid'
    LDAP_WRITE_ATTR_EMAIL = os.environ.get('LDAP_WRITE_ATTR_EMAIL') or 'mail'
    LDAP_WRITE_ATTR_PASSWORD = os.environ.get('LDAP_WRITE_ATTR_PASSWORD') or 'userPassword'

    # LDAP configuration - used to log in to this instance
    LDAP_READ_ENABLE = os.environ.get('LDAP_READ_ENABLE', '0') in ('1', 'true', 'True')
    LDAP_READ_USER_FILTER = os.environ.get('LDAP_READ_USER_FILTER') or '(uid={username})'
    LDAP_READ_ATTR_USERNAME = os.environ.get('LDAP_READ_ATTR_USERNAME') or 'uid'
    LDAP_READ_ATTR_EMAIL = os.environ.get('LDAP_READ_ATTR_EMAIL') or 'mail'

    VERSION = app.constants.VERSION

    # How long to keep post voting data ( months )
    KEEP_LOCAL_VOTE_DATA_TIME = int(os.environ.get('KEEP_LOCAL_VOTE_DATA_TIME') or 6)
    KEEP_REMOTE_VOTE_DATA_TIME = int(os.environ.get('KEEP_REMOTE_VOTE_DATA_TIME') or 6)

    # Country Header sourcing
    COUNTRY_SOURCE_HEADER = os.environ.get('COUNTRY_SOURCE_HEADER') or ''

    # render a post+replies to json and delete from DB after this many months
    ARCHIVE_POSTS = int(os.environ.get('ARCHIVE_POSTS') or 0)

    # How long to keep content in remote communities before automatically deleting posts. See 'class EditCommunityForm()' for allowed values
    DEFAULT_CONTENT_RETENTION = int(os.environ.get('DEFAULT_CONTENT_RETENTION') or -1)  # -1 = forever, no deletion

    # How long to keep bot content (that isn't stickied and has no replies), in months
    BOT_CONTENT_RETENTION = int(os.environ.get('BOT_CONTENT_RETENTION') or 6)  # -1 = forever, no deletion

    CONTENT_WARNING = int(os.environ.get('CONTENT_WARNING') or 0)

    TRANSLATE_ENDPOINT = os.environ.get('TRANSLATE_ENDPOINT') or ''
    TRANSLATE_KEY = os.environ.get('TRANSLATE_KEY') or ''

    ALLOW_AI_CRAWLERS = os.environ.get('ALLOW_AI_CRAWLERS') or False

    DETECT_AI_ENDPOINT = os.environ.get('DETECT_AI_ENDPOINT') or ''

    REDIS_MEMORY_LIMIT = int(os.environ.get('REDIS_MEMORY_LIMIT') or 200000000)

    VOTE_QUOTA = int(os.environ.get('VOTE_QUOTA') or 240)
