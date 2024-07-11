from flask import render_template, g, request
from app import db
from app.errors import bp


# 404 error handler removed because a lot of 404s are just images in /static/* and it doesn't make sense to waste cpu cycles presenting a nice page.
# Also rendering a page requires populating g.site which means hitting the DB.
@bp.app_errorhandler(404)
def not_found_error(error):
    if request.path.startswith('/u/') or request.path.startswith('/c/'):
        return render_template('errors/404.html'), 404
    else:
        return """<!doctype html>
                  <html lang=en>
                  <title>404 Not Found</title>
                  <h1>Not Found</h1>
                  <p>The requested URL was not found on the server. If you entered the URL manually please check your spelling and try again.</p>""", 404

@bp.app_errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return render_template('errors/500.html'), 500


@bp.app_errorhandler(401)
def internal_error(error):
    db.session.rollback()
    return render_template('errors/401.html'), 401
