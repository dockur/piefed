# This file is part of PieFed, which is licensed under the GNU General Public License (GPL) version 3.0.
# You should have received a copy of the GPL along with this program. If not, see <http://www.gnu.org/licenses/>.

import logging
from logging.handlers import SMTPHandler, RotatingFileHandler
import os
from flask import Flask, request, current_app, session
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_bootstrap import Bootstrap5
from flask_mail import Mail
from flask_babel import Babel, lazy_gettext as _l
from flask_caching import Cache
from flask_limiter import Limiter
from werkzeug.middleware.proxy_fix import ProxyFix
from celery import Celery
from sqlalchemy_searchable import make_searchable
import httpx
from authlib.integrations.flask_client import OAuth

from config import Config


def get_locale():
    try:
        if session.get('ui_language', None):
            return session['ui_language']
        else:
            try:
                return request.accept_languages.best_match(current_app.config['LANGUAGES'])
            except:
                return 'en'
    except:
        return 'en'


def get_ip_address() -> str:
    ip = request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For') or request.remote_addr
    if ',' in ip:  # Remove all but first ip addresses
        ip = ip[:ip.index(',')].strip()
    return ip


db = SQLAlchemy(session_options={"autoflush": False}, engine_options={'pool_size': Config.DB_POOL_SIZE, 'max_overflow': Config.DB_MAX_OVERFLOW, 'pool_recycle': 3600})
migrate = Migrate()
login = LoginManager()
login.login_view = 'auth.login'
login.login_message = _l('Please log in to access this page.')
mail = Mail()
bootstrap = Bootstrap5()
babel = Babel(locale_selector=get_locale)
cache = Cache()
limiter = Limiter(get_ip_address, storage_uri='redis+'+Config.CACHE_REDIS_URL if Config.CACHE_REDIS_URL.startswith("unix://") else Config.CACHE_REDIS_URL)
celery = Celery(__name__, broker=Config.CELERY_BROKER_URL)
httpx_client = httpx.Client(http2=True)
oauth = OAuth()
redis_client = None  # Will be initialized in create_app()


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    if app.config['SENTRY_DSN']:
        import sentry_sdk
        sentry_sdk.init(
            dsn=app.config["SENTRY_DSN"],
            enable_tracing=False,
        )

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    login.init_app(app)
    mail.init_app(app)
    bootstrap.init_app(app)
    make_searchable(db.metadata)
    babel.init_app(app, locale_selector=get_locale)
    cache.init_app(app)
    limiter.init_app(app)
    celery.conf.update(app.config)

    celery.conf.update(CELERY_ROUTES={
        'app.shared.tasks.users.check_user_application': {'queue': 'background'},
        'app.user.utils.purge_user_then_delete_task': {'queue': 'background'},
        'app.community.util.retrieve_mods_and_backfill': {'queue': 'background'},
        'app.activitypub.signature.post_request': {'queue': 'send'},
        # Maintenance tasks - all go to background queue
        'app.shared.tasks.maintenance.*': {'queue': 'background'},
    })

    # Initialize redis_client
    global redis_client
    from app.utils import get_redis_connection
    redis_client = get_redis_connection(app.config['CACHE_REDIS_URL'])

    oauth.init_app(app)
    if app.config['GOOGLE_OAUTH_CLIENT_ID']:
        oauth.register(
            name='google',
            client_id=app.config['GOOGLE_OAUTH_CLIENT_ID'],
            client_secret=app.config['GOOGLE_OAUTH_SECRET'],
            access_token_url='https://oauth2.googleapis.com/token',
            authorize_url='https://accounts.google.com/o/oauth2/v2/auth',
            api_base_url='https://www.googleapis.com/',
            client_kwargs={'scope': 'email profile'}
        )
    if app.config["MASTODON_OAUTH_CLIENT_ID"]:
        oauth.register(
            name="mastodon",
            client_id=app.config["MASTODON_OAUTH_CLIENT_ID"],
            client_secret=app.config["MASTODON_OAUTH_SECRET"],
            access_token_url=f"https://{app.config['MASTODON_OAUTH_DOMAIN']}/oauth/token",
            authorize_url=f"https://{app.config['MASTODON_OAUTH_DOMAIN']}/oauth/authorize",
            api_base_url=f"https://{app.config['MASTODON_OAUTH_DOMAIN']}/api/v1/",
            client_kwargs={"response_type": "code"}
        )

    if app.config["DISCORD_OAUTH_CLIENT_ID"]:
        oauth.register(
            name="discord",
            client_id=app.config["DISCORD_OAUTH_CLIENT_ID"],
            client_secret=app.config["DISCORD_OAUTH_SECRET"],
            access_token_url="https://discord.com/api/oauth2/token",
            authorize_url="https://discord.com/api/oauth2/authorize",
            api_base_url="https://discord.com/api/",
            client_kwargs={"scope": "identify email"}
        )
    
    if app.config['PATREON_OAUTH_CLIENT_ID']:
        oauth.register(
            name='patreon',
            client_id=app.config['PATREON_OAUTH_CLIENT_ID'],
            client_secret=app.config['PATREON_OAUTH_CLIENT_SECRET'],
            access_token_url='https://www.patreon.com/api/oauth2/token',
            authorize_url='https://www.patreon.com/oauth2/authorize',
            api_base_url='https://www.patreon.com/api/oauth2/v2/',
            client_kwargs={'scope': 'identity[email]'}
        )

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    from app.errors import bp as errors_bp
    app.register_blueprint(errors_bp)

    from app.admin import bp as admin_bp
    app.register_blueprint(admin_bp, url_prefix='/admin')

    from app.activitypub import bp as activitypub_bp
    app.register_blueprint(activitypub_bp)

    from app.auth import bp as auth_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')

    from app.community import bp as community_bp
    app.register_blueprint(community_bp, url_prefix='/community')

    from app.post import bp as post_bp
    app.register_blueprint(post_bp)

    from app.user import bp as user_bp
    app.register_blueprint(user_bp)

    from app.domain import bp as domain_bp
    app.register_blueprint(domain_bp)

    from app.feed import bp as feed_bp
    app.register_blueprint(feed_bp)

    from app.instance import bp as instance_bp
    app.register_blueprint(instance_bp)

    from app.topic import bp as topic_bp
    app.register_blueprint(topic_bp)

    from app.chat import bp as chat_bp
    app.register_blueprint(chat_bp)

    from app.search import bp as search_bp
    app.register_blueprint(search_bp)

    from app.tag import bp as tag_bp
    app.register_blueprint(tag_bp)

    from app.dev import bp as dev_bp
    app.register_blueprint(dev_bp)

    from app.api.alpha import bp as app_api_bp
    app.register_blueprint(app_api_bp)

    # send error reports via email
    if app.config['MAIL_SERVER'] and app.config['ERRORS_TO']:
        auth = None
        if app.config['MAIL_USERNAME'] or app.config['MAIL_PASSWORD']:
            auth = (app.config['MAIL_USERNAME'],
                    app.config['MAIL_PASSWORD'])
        secure = None
        if app.config['MAIL_USE_TLS']:
            secure = ()
        mail_handler = SMTPHandler(
            mailhost=(app.config['MAIL_SERVER'], app.config['MAIL_PORT']),
            fromaddr=(app.config['MAIL_FROM']),
            toaddrs=app.config['ERRORS_TO'], subject='PieFed error',
            credentials=auth, secure=secure)
        mail_handler.setLevel(logging.ERROR)
        app.logger.addHandler(mail_handler)

    # log rotation
    if not os.path.exists('logs'):
        os.mkdir('logs')
    file_handler = RotatingFileHandler('logs/pyfedi.log',
                                       maxBytes=1002400, backupCount=15)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s '
        '[in %(pathname)s:%(lineno)d]'))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)

    app.logger.setLevel(logging.INFO)
    app.logger.info('Started!') # let's go!

    # Load plugins
    from app.plugins import load_plugins
    load_plugins()

    return app


from app import models
