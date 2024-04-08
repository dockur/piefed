# This file is part of PieFed, which is licensed under the GNU General Public License (GPL) version 3.0.
# You should have received a copy of the GPL along with this program. If not, see <http://www.gnu.org/licenses/>.

import logging
from logging.handlers import SMTPHandler, RotatingFileHandler
import os
from flask import Flask, request, current_app
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_bootstrap import Bootstrap5
from flask_mail import Mail
from flask_moment import Moment
from flask_babel import Babel, lazy_gettext as _l
from flask_caching import Cache
from celery import Celery
from sqlalchemy_searchable import make_searchable

from config import Config


def get_locale():
    try:
        return request.accept_languages.best_match(current_app.config['LANGUAGES'])
    except:
        return 'en'


db = SQLAlchemy()  # engine_options={'pool_size': 5, 'max_overflow': 10} # session_options={"autoflush": False}
migrate = Migrate()
login = LoginManager()
login.login_view = 'auth.login'
login.login_message = _l('Please log in to access this page.')
mail = Mail()
bootstrap = Bootstrap5()
moment = Moment()
babel = Babel(locale_selector=get_locale)
cache = Cache()
celery = Celery(__name__, broker=Config.CELERY_BROKER_URL)


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    if app.config['SENTRY_DSN']:
        import sentry_sdk
        sentry_sdk.init(
            dsn=app.config["SENTRY_DSN"],
            enable_tracing=True
        )

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    login.init_app(app)
    mail.init_app(app)
    bootstrap.init_app(app)
    moment.init_app(app)
    make_searchable(db.metadata)
    babel.init_app(app, locale_selector=get_locale)
    cache.init_app(app)
    celery.conf.update(app.config)

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

    from app.topic import bp as topic_bp
    app.register_blueprint(topic_bp)

    from app.chat import bp as chat_bp
    app.register_blueprint(chat_bp)

    from app.search import bp as search_bp
    app.register_blueprint(search_bp)

    # send error reports via email
    if app.config['MAIL_SERVER'] and app.config['MAIL_ERRORS']:
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
            toaddrs=app.config['ADMINS'], subject='PieFed error',
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

    return app


from app import models
