import os
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))


class Config(object):
    SERVER_NAME = os.environ.get('SERVER_NAME') or 'localhost'
    HTTP_PROTOCOL = os.environ.get('HTTP_PROTOCOL') or 'https'
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'you-will-never-guesss'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
                              'sqlite:///' + os.path.join(basedir, 'app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAIL_SERVER = os.environ.get('MAIL_SERVER') or None
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 25)
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS') is not None
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME') or None
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD') or None
    MAIL_FROM = os.environ.get('MAIL_FROM') or 'noreply@' + os.environ.get('SERVER_NAME')
    MAIL_ERRORS = os.environ.get('MAIL_ERRORS') is not None
    ERRORS_TO = os.environ.get('ERRORS_TO') or ''
    LANGUAGES = ['ca', 'de', 'en', 'es', 'fr', 'ja', 'zh']
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

    BOUNCE_ADDRESS = os.environ.get('BOUNCE_ADDRESS') or MAIL_FROM or ''    # Warning: all emails in this inbox will be deleted!
    BOUNCE_HOST = os.environ.get('BOUNCE_HOST') or ''
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

    GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID') or ''
    GOOGLE_OAUTH_SECRET = os.environ.get('GOOGLE_OAUTH_SECRET') or ''

    # enable the aplha api
    ENABLE_ALPHA_API = os.environ.get('ENABLE_ALPHA_API') or False

    IMAGE_HASHING_ENDPOINT = os.environ.get('IMAGE_HASHING_ENDPOINT') or ''

    FLAG_THROWAWAY_EMAILS = os.environ.get('FLAG_THROWAWAY_EMAILS') or False

    NOTIF_SERVER = os.environ.get('NOTIF_SERVER') or ''
