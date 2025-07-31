from flask import render_template, request

from app import db
from app.errors import bp
from app.models import CmsPage


@bp.app_errorhandler(404)
def not_found_error(error):
    # Skip page lookup for static files and known non-page paths to avoid unnecessary DB hits
    if (request.path.startswith('/static/') or
            request.path.startswith('/api/') or
            request.path.startswith('/.well-known/') or
            request.path.startswith('/admin/') or
            request.path.startswith('/auth/') or
            request.path.endswith(('.jpg', '.jpeg', '.png', '.gif', '.css', '.js', '.ico', '.svg', '.woff', '.woff2',
                                   '.ttf', '.webp', '.txt'))):
        return render_template('errors/404.html'), 404

    # Check if there's a page for this URL
    cms_page = CmsPage.query.filter(CmsPage.url == request.path).first()
    if cms_page:
        return render_template('cms_page.html', page=cms_page)

    # Fall back to standard 404 page
    return render_template('errors/404.html'), 404


@bp.app_errorhandler(500)
def internal_error_500(error):
    db.session.rollback()
    return render_template('errors/500.html'), 500


@bp.app_errorhandler(401)
def internal_error_401(error):
    db.session.rollback()
    return render_template('errors/401.html'), 401


@bp.app_errorhandler(429)
def rate_limited_429(error):
    return render_template('errors/429.html', error=error), 429
