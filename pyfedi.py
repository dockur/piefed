# This file is part of pyfedi, which is licensed under the GNU General Public License (GPL) version 3.0.
# You should have received a copy of the GPL along with this program. If not, see <http://www.gnu.org/licenses/>.
from datetime import datetime

from flask_babel import get_locale
from flask_login import current_user

from app import create_app, db, cli
import os, click
from flask import session, g, json, request, current_app
from app.constants import POST_TYPE_LINK, POST_TYPE_IMAGE, POST_TYPE_ARTICLE, POST_TYPE_VIDEO
from app.models import Site
from app.utils import getmtime, gibberish, shorten_string, shorten_url, digits, user_access, community_membership, \
    can_create_post, can_upvote, can_downvote, shorten_number, ap_datetime, current_theme, community_link_to_href, \
    in_sorted_list

app = create_app()
cli.register(app)


@app.context_processor
def app_context_processor():
    def getmtime(filename):
        return os.path.getmtime('app/static/' + filename)
    return dict(getmtime=getmtime, POST_TYPE_LINK=POST_TYPE_LINK, POST_TYPE_IMAGE=POST_TYPE_IMAGE,
                POST_TYPE_ARTICLE=POST_TYPE_ARTICLE, POST_TYPE_VIDEO=POST_TYPE_VIDEO)


@app.shell_context_processor
def make_shell_context():
    return {'db': db, 'app': app}


with app.app_context():
    app.jinja_env.globals['getmtime'] = getmtime
    app.jinja_env.globals['len'] = len
    app.jinja_env.globals['digits'] = digits
    app.jinja_env.globals['str'] = str
    app.jinja_env.globals['shorten_number'] = shorten_number
    app.jinja_env.globals['community_membership'] = community_membership
    app.jinja_env.globals['json_loads'] = json.loads
    app.jinja_env.globals['user_access'] = user_access
    app.jinja_env.globals['ap_datetime'] = ap_datetime
    app.jinja_env.globals['can_create'] = can_create_post
    app.jinja_env.globals['can_upvote'] = can_upvote
    app.jinja_env.globals['can_downvote'] = can_downvote
    app.jinja_env.globals['in_sorted_list'] = in_sorted_list
    app.jinja_env.globals['theme'] = current_theme
    app.jinja_env.globals['file_exists'] = os.path.exists
    app.jinja_env.filters['community_links'] = community_link_to_href
    app.jinja_env.filters['shorten'] = shorten_string
    app.jinja_env.filters['shorten_url'] = shorten_url


@app.before_request
def before_request():
    session['nonce'] = gibberish()
    g.locale = str(get_locale())
    if request.path != '/inbox' and not request.path.startswith('/static/'):        # do not load g.site on shared inbox, to increase chance of duplicate detection working properly
        g.site = Site.query.get(1)
    if current_user.is_authenticated:
        current_user.last_seen = datetime.utcnow()
        current_user.email_unread_sent = False
    else:
        if session.get('Referer') is None and \
                request.headers.get('Referer') is not None and \
                current_app.config['SERVER_NAME'] not in request.headers.get('Referer'):
            session['Referer'] = request.headers.get('Referer')


@app.after_request
def after_request(response):
    if 'auth/register' not in request.path:
        response.headers['Content-Security-Policy'] = f"script-src 'self' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net 'nonce-{session['nonce']}'"
        response.headers['Strict-Transport-Security'] = 'max-age=63072000; includeSubDomains; preload'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
    return response
