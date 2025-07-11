#!flask/bin/python
import os
from datetime import datetime
import cProfile
import pstats
import io

import arrow
from flask import session, g, json, request, current_app
from flask_babel import get_locale
from flask_login import current_user
from werkzeug.middleware.profiler import ProfilerMiddleware
from app import create_app, db, cli
from app.models import Site
from app.utils import gibberish, shorten_number, community_membership, getmtime, digits, user_access, ap_datetime, \
    can_create_post, can_upvote, can_downvote, current_theme, shorten_string, shorten_url, feed_membership, role_access, \
    in_sorted_list, first_paragraph, html_to_text, community_link_to_href, person_link_to_href, remove_images, \
    notif_id_to_string, feed_link_to_href
from app.constants import *

app = create_app()


@app.context_processor
def app_context_processor():
    return {'getmtime': getmtime, 'instance_domain': current_app.config['SERVER_NAME'], 'debug_mode': current_app.debug,
                'arrow': arrow, 'locale': g.locale if hasattr(g, 'locale') else None, 'notif_server': current_app.config['NOTIF_SERVER'],
                'site': g.site if hasattr(g, 'site') else None, 'nonce': g.nonce if hasattr(g, 'nonce') else None,
                'POST_TYPE_LINK': POST_TYPE_LINK, 'POST_TYPE_IMAGE': POST_TYPE_IMAGE, 'notif_id_to_string': notif_id_to_string,
                'POST_TYPE_ARTICLE': POST_TYPE_ARTICLE, 'POST_TYPE_VIDEO': POST_TYPE_VIDEO, 'POST_TYPE_POLL': POST_TYPE_POLL,
                'SUBSCRIPTION_MODERATOR': SUBSCRIPTION_MODERATOR, 'SUBSCRIPTION_MEMBER': SUBSCRIPTION_MEMBER,
                'SUBSCRIPTION_OWNER': SUBSCRIPTION_OWNER, 'SUBSCRIPTION_PENDING': SUBSCRIPTION_PENDING}


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
    app.jinja_env.globals['in_sorted_list'] = in_sorted_list
    app.jinja_env.globals['theme'] = current_theme
    app.jinja_env.globals['file_exists'] = os.path.exists
    app.jinja_env.globals['first_paragraph'] = first_paragraph
    app.jinja_env.globals['html_to_text'] = html_to_text
    app.jinja_env.filters['community_links'] = community_link_to_href
    app.jinja_env.filters['feed_links'] = feed_link_to_href
    app.jinja_env.filters['person_links'] = person_link_to_href
    app.jinja_env.filters['shorten'] = shorten_string
    app.jinja_env.filters['shorten_url'] = shorten_url
    app.jinja_env.filters['remove_images'] = remove_images
    app.config['PROFILE'] = True
    app.wsgi_app = ProfilerMiddleware(app.wsgi_app, restrictions=[100])
    app.run(debug = True, host='127.0.0.1')


@app.before_request
def before_request():
    g.profiler = cProfile.Profile()
    g.profiler.enable()

    # Handle CORS preflight requests for API routes
    if request.method == 'OPTIONS' and request.path.startswith('/api/'):
        return '', 200

    # Store nonce in g (g is per-request, unlike session)
    g.nonce = gibberish()
    g.locale = str(get_locale())
    if request.path != '/inbox' and not request.path.startswith(
            '/static/'):  # do not load g.site on shared inbox, to increase chance of duplicate detection working properly
        g.site = Site.query.get(1)
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
    # Add CORS headers for API routes
    if request.path.startswith('/api/'):
        response.headers['Access-Control-Allow-Origin'] = current_app.config['CORS_ALLOW_ORIGIN']
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'

    # Don't set cookies for static resources or ActivityPub responses to make them cachable
    if request.path.startswith('/static/') or request.path.startswith(
            '/bootstrap/static/') or response.content_type == 'application/activity+json':
        # Remove session cookies that mess up caching
        if 'Set-Cookie' in response.headers:
            del response.headers['Set-Cookie']
        # Cache headers for static resources
        if request.path.startswith('/static/') or request.path.startswith('/bootstrap/static/'):
            response.headers['Cache-Control'] = 'public, max-age=31536000'  # 1 year
    else:
        if 'auth/register' not in request.path:
            response.headers['Content-Security-Policy'] = f"script-src 'self' 'nonce-{g.nonce}'"
            response.headers['Strict-Transport-Security'] = 'max-age=63072000; includeSubDomains; preload'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            if '/embed' not in request.path:
                response.headers['X-Frame-Options'] = 'DENY'

    profiler = getattr(g, 'profiler', None)
    if profiler:
        profiler.disable()
        s = io.StringIO()
        ps = pstats.Stats(profiler, stream=s).sort_stats('cumulative')
        ps.print_stats(50)  # Top 50 lines by cumulative time

        # Output to stderr, or save to file, or add to response
        print(f"--- PROFILE ({request.path}) ---\n{s.getvalue()}")

    return response
