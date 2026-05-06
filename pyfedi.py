# This file is part of PieFed, which is licensed under the GNU Affero General Public License (AGPL) version 3.0.
# You should have received a copy of the GPL along with this program. If not, see <http://www.gnu.org/licenses/>.
from datetime import datetime
import os

import flask
from flask_babel import get_locale, ngettext
from flask_login import current_user
from flask_wtf.csrf import generate_csrf

from app import create_app, db, cli
import pendulum
from flask import session, g, json, request, current_app
from sqlalchemy import text
from app.constants import POST_TYPE_LINK, POST_TYPE_IMAGE, POST_TYPE_ARTICLE, POST_TYPE_VIDEO, POST_TYPE_POLL, \
    SUBSCRIPTION_MODERATOR, SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, SUBSCRIPTION_PENDING, ROLE_ADMIN, VERSION, \
    POST_TYPE_EVENT
from app.models import Site
from app.utils import getmtime, gibberish, shorten_string, shorten_url, digits, user_access, community_membership, \
    can_create_post, can_upvote, can_downvote, shorten_number, ap_datetime, current_theme, community_link_to_href, \
    in_sorted_list, role_access, first_paragraph, person_link_to_href, feed_membership, html_to_text, remove_images, \
    notif_id_to_string, feed_link_to_href, get_setting, set_setting, show_explore, human_filesize, can_upload_video, \
    debug_checkpoint, compaction_level, humanize_number, round_invisible_digits, get_site_as_dict, localize_datetime, \
    display_back_button

app = create_app()
cli.register(app)


@app.context_processor
def app_context_processor():
    return dict(getmtime=getmtime, instance_domain=current_app.config['SERVER_NAME'], debug_mode=current_app.debug,
                pendulum=pendulum, locale=g.locale if hasattr(g, 'locale') else None, notif_server=current_app.config['NOTIF_SERVER'],
                site=g.site if hasattr(g, 'site') else None, nonce=g.nonce if hasattr(g, 'nonce') else None,
                admin_ids=g.admin_ids if hasattr(g, 'admin_ids') else [], low_bandwidth=g.low_bandwidth if hasattr(g, 'low_bandwidth') else None,
                can_translate=current_app.config['TRANSLATE_ENDPOINT'] != '', can_detect_ai=current_app.config['DETECT_AI_ENDPOINT'] != '',
                POST_TYPE_LINK=POST_TYPE_LINK, POST_TYPE_IMAGE=POST_TYPE_IMAGE, notif_id_to_string=notif_id_to_string,
                POST_TYPE_ARTICLE=POST_TYPE_ARTICLE, POST_TYPE_VIDEO=POST_TYPE_VIDEO, POST_TYPE_POLL=POST_TYPE_POLL,
                POST_TYPE_EVENT=POST_TYPE_EVENT,
                SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, VERSION=VERSION)


@app.shell_context_processor
def make_shell_context():
    return {'db': db, 'app': app}


with app.app_context():
    app.jinja_env.globals['len'] = len
    app.jinja_env.globals['digits'] = digits
    app.jinja_env.globals['str'] = str
    app.jinja_env.globals['shorten_number'] = shorten_number
    app.jinja_env.globals['community_membership'] = community_membership
    app.jinja_env.globals['feed_membership'] = feed_membership
    app.jinja_env.globals['json_loads'] = json.loads
    app.jinja_env.globals['user_access'] = user_access
    app.jinja_env.globals['role_access'] = role_access
    app.jinja_env.globals['ap_datetime'] = ap_datetime
    app.jinja_env.globals['can_create'] = can_create_post
    app.jinja_env.globals['can_upvote'] = can_upvote
    app.jinja_env.globals['can_downvote'] = can_downvote
    app.jinja_env.globals['can_upload_video'] = can_upload_video
    app.jinja_env.globals['show_explore'] = show_explore
    app.jinja_env.globals['in_sorted_list'] = in_sorted_list
    app.jinja_env.globals['theme'] = current_theme
    app.jinja_env.globals['file_exists'] = os.path.exists
    app.jinja_env.globals['first_paragraph'] = first_paragraph
    app.jinja_env.globals['ngettext'] = ngettext
    app.jinja_env.globals['html_to_text'] = html_to_text
    app.jinja_env.globals['csrf_token'] = generate_csrf
    app.jinja_env.globals['debug_checkpoint'] = debug_checkpoint
    app.jinja_env.globals['compaction_level'] = compaction_level
    app.jinja_env.globals['humanize_number'] = humanize_number
    app.jinja_env.globals['round_invisible_digits'] = round_invisible_digits
    app.jinja_env.globals['localize_datetime'] = localize_datetime
    app.jinja_env.globals['display_back_button'] = display_back_button
    app.jinja_env.filters['community_links'] = community_link_to_href
    app.jinja_env.filters['feed_links'] = feed_link_to_href
    app.jinja_env.filters['person_links'] = person_link_to_href
    app.jinja_env.filters['shorten'] = shorten_string
    app.jinja_env.filters['shorten_url'] = shorten_url
    app.jinja_env.filters['remove_images'] = remove_images
    app.jinja_env.filters["human_filesize"] = human_filesize

@app.before_request
def before_request():
    # Handle CORS preflight requests for all routes
    if request.method == 'OPTIONS':
        return '', 200
    
    # Store nonce in g (g is per-request, unlike session)
    g.nonce = gibberish()
    g.locale = str(get_locale())
    g.low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    if request.path != '/inbox' and not request.path.startswith('/static/'):        # do not load g.site on shared inbox, to increase chance of duplicate detection working properly
        site = get_site_as_dict()
        g.site = Site(**site)
        g.admin_ids = get_setting('admin_ids')    # get_setting is cached in redis
        if g.admin_ids is None:
            g.admin_ids = list(db.session.execute(
                text("""SELECT u.id FROM "user" u WHERE u.id = 1
                        UNION
                        SELECT u.id
                        FROM "user" u
                        JOIN user_role ur ON u.id = ur.user_id AND ur.role_id = :role_admin AND u.deleted = false AND u.banned = false
                        ORDER BY id"""),
                {'role_admin': ROLE_ADMIN}).scalars())
            set_setting('admin_ids', g.admin_ids)

    if current_user.is_authenticated:
        current_user.last_seen = datetime.utcnow()
        current_user.email_unread_sent = False
    else:
        if 'Windows' in request.user_agent.string:
            current_user.font = 'inter'
        else:
            current_user.font = ''
        if session.get('Referer') is None and \
                request.headers.get('Referer') is not None and \
                current_app.config['SERVER_NAME'] not in request.headers.get('Referer'):
            session['Referer'] = request.headers.get('Referer')


@app.after_request
def after_request(response):
    # Add CORS headers to all responses
    response.headers['Access-Control-Allow-Origin'] = current_app.config.get('CORS_ALLOW_ORIGIN', '*')
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, Accept, User-Agent'
    
    # Don't set cookies for static resources or ActivityPub responses to make them cachable
    if request.path.startswith('/static/') or request.path.startswith('/bootstrap/static/') or response.content_type == 'application/activity+json':
        # Remove session cookies that mess up caching
        if 'session' in dir(flask):
            from flask import session
            session.modified = False
        # Cache headers for static resources
        if request.path.startswith('/static/') or request.path.startswith('/bootstrap/static/'):
            response.headers['Cache-Control'] = 'public, max-age=31536000'  # 1 year
    else:
        if not current_app.config['ALLOW_AI_CRAWLERS']:
            response.headers.add('Link', f'<https://{current_app.config["SERVER_NAME"]}/rsl.xml>; rel="license"; type="application/rsl+xml"')
        if 'auth/register' not in request.path:
            if hasattr(g, 'nonce') and "api/alpha/swagger" not in request.path:
                # Don't set CSP header for htmx fragment requests - they use parent page's CSP
                is_htmx = request.headers.get('HX-Request') == 'true'
                if not is_htmx and response.status_code != 304:
                    # strict-dynamic allows scripts dynamically added by nonce-validated scripts (needed for htmx)
                    if current_user.is_authenticated:
                        response.headers['Content-Security-Policy'] = f"script-src 'self' 'nonce-{g.nonce}' 'strict-dynamic'; object-src 'none'; base-uri 'none';"
            if current_app.config['HTTP_PROTOCOL'] == 'https':
                response.headers['Strict-Transport-Security'] = 'max-age=63072000; includeSubDomains; preload'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            if '/embed' not in request.path:
                response.headers['X-Frame-Options'] = 'DENY'

    # API responses must never be cached by proxies
    if request.path.startswith('/api/'):
        response.headers.setdefault('Cache-Control', 'no-store')

    # Caching headers for html pages - pages are automatically translated and should not be cached while logged in.
    if response.content_type.startswith('text/html'):
        if current_user.is_authenticated or request.path.startswith('/auth/') or "api/alpha/swagger" in request.path:
            response.headers.setdefault(
                'Cache-Control',
                'no-store, no-cache, must-revalidate, private'
            )
        else:
            # Don't let flask set a session cookie for logged out users
            flask.session.modified = False
        response.headers.setdefault('Vary', 'Accept-Language, Cookie')
    return response


@app.teardown_appcontext
def shutdown_session(exception=None):
    if exception:
        db.session.rollback()
    db.session.remove()
