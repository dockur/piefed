from __future__ import annotations

import random
import string
import secrets
from datetime import datetime, timedelta
from typing import Any
from unicodedata import normalize
from urllib.parse import urlsplit

from flask import current_app, flash, g, make_response, redirect, request, session, url_for
from flask_babel import _
from flask_login import current_user, login_user
from markupsafe import Markup
from sqlalchemy import func
from wtforms import Label

from app import cache, db, plugins
from app.activitypub.util import users_total
from app.auth.forms import LoginForm
from app.constants import NOTIF_REGISTRATION
from app.email import send_verification_email
from app.ldap_utils import sync_user_to_ldap, login_with_ldap
from app.models import IpBan, Notification, Site, User, UserRegistration, utcnow, Role
from app.utils import banned_ip_addresses, blocked_referrers, finalize_user_setup, get_request, get_setting, gibberish, \
    ip_address, markdown_to_html, render_template, user_cookie_banned, user_ip_banned, role_access, actor_contains_blocked_words


ALPHABET = string.ascii_letters + string.digits


# Return a random string of 6 letter/digits.
def random_token(length=6) -> str:
    return ''.join(secrets.choice(ALPHABET) for _ in range(length))


def normalize_utf(username):
    return normalize('NFKC', username)


def ip2location(ip: str):
    """ city, region and country for the requester, using the ipinfo.io service """
    if ip is None or ip == '':
        return {}
    ip = '208.97.120.117' if ip == '127.0.0.1' else ip
    # test
    data = cache.get('ip_' + ip)
    if data is None:
        if not current_app.config['IPINFO_TOKEN']:
            return {}
        url = 'http://ipinfo.io/' + ip + '?token=' + current_app.config['IPINFO_TOKEN']
        response = get_request(url)
        if response.status_code == 200:
            data = response.json()
            cache.set('ip_' + ip, data, timeout=86400)
        else:
            return {}

    if 'postal' in data:
        postal = data['postal']
    else:
        postal = ''
    return {'city': data['city'], 'region': data['region'], 'country': data['country'], 'postal': postal,
            'timezone': data['timezone']}


def get_country(ip: str, fallback: Any = '') -> str:
    country_header = current_app.config['COUNTRY_SOURCE_HEADER']

    if country_header and country_header in request.headers:
        return request.headers[country_header]
    if ip is None or ip.strip() == '':
        return fallback

    return ip2location(ip).get('country', fallback)


def no_admins_logged_in_recently():
    a_week_ago = utcnow() - timedelta(days=7)
    for user in Site.admins():
        if user.last_seen > a_week_ago:
            return False

    for user in Site.staff():
        if user.last_seen > a_week_ago:
            return False

    return True


def create_user_application(user: User, registration_answer: str):
    application = UserRegistration(user_id=user.id, answer='Signed in with Google')
    db.session.add(application)
    targets_data = {'application_id': application.id, 'user_id': user.id}
    for admin in Site.admins():
        notify = Notification(title='New registration', url=f'/admin/approve_registrations?account={user.id}',
                              user_id=admin.id,
                              author_id=user.id, notif_type=NOTIF_REGISTRATION,
                              subtype='new_registration_for_approval',
                              targets=targets_data)
        admin.unread_notifications += 1
        db.session.add(notify)
        # todo: notify everyone with the "approve registrations" permission, instead of just all admins
    db.session.commit()


def notify_admins_of_registration(application):
    """Notify admins when a registration application is ready for review"""

    # commit is needed here first so that db information available later to plugin
    db.session.commit()

    targets_data = {'gen': '0', 'application_id': application.id, 'user_id': application.user_id}
    for admin in Site.admins():
        notify = Notification(title='New registration',
                              url=f'/admin/approve_registrations?account={application.user_id}', user_id=admin.id,
                              author_id=application.user_id, notif_type=NOTIF_REGISTRATION,
                              subtype='new_registration_for_approval',
                              targets=targets_data)
        admin.unread_notifications += 1
        db.session.add(notify)
    if role_access('approve registrations', 3):
        for admin in Site.staff():
            notify = Notification(title='New registration',
                                  url=f'/admin/approve_registrations?account={application.user_id}', user_id=admin.id,
                                  author_id=application.user_id, notif_type=NOTIF_REGISTRATION,
                                  subtype='new_registration_for_approval',
                                  targets=targets_data)
            admin.unread_notifications += 1
            db.session.add(notify)
    
    plugins.fire_hook("new_registration_for_approval", application)


def create_registration_application(user, answer):
    """Create a UserRegistration application with proper status based on email verification"""
    # Set status to -1 if email verification needed, 0 if not
    status = -1 if get_setting('email_verification', True) and not user.verified else 0
    application = UserRegistration(user_id=user.id, answer=answer, status=status)
    db.session.add(application)

    # Only notify admins if application is ready for review (status >= 0)
    if status >= 0:
        notify_admins_of_registration(application)

    return application

def handle_abandoned_open_instance():
    if g.site.registration_mode == "Open" and no_admins_logged_in_recently():
        g.site.registration_mode = "Closed"


def process_registration_form(form):
    disallowed_usernames = ["admin"]
    ip = ip_address()
    country = get_country(ip)

    if is_invalid_email_or_username(form, disallowed_usernames):
        return redirect(url_for("auth.register"))

    if actor_contains_blocked_words(form.user_name.data):
        flash(_("Sorry, this username pattern is not allowed."), "error")
        return redirect(url_for("auth.register"))

    if is_restricted_by_referrer():
        return redirect_with_session_cookie("auth.please_wait")

    if is_country_blocked(country):
        return render_template("generic_message.html", title=_("Application declined"),
                               message=_("Sorry, we are not accepting registrations from your country."))

    return register_new_user(form, ip, country)


def is_invalid_email_or_username(form, disallowed_usernames):
    if form.email.data.strip():
        return False

    if form.real_email.data.lower().startswith(("postmaster@", "abuse@", "noc@")):
        flash(_("Sorry, you cannot use that email address"), "error")
        return True

    if form.user_name.data in disallowed_usernames:
        flash(_("Sorry, you cannot use that user name"), "error")
        return True

    return False


def redirect_with_session_cookie(route):
    response = make_response(redirect(url_for(route)))
    response.set_cookie("sesion", "17489047567495", expires=datetime(year=2099, month=12, day=30))
    return response


def is_restricted_by_referrer():
    for referrer in blocked_referrers():
        if referrer in session.get("Referer", ""):
            flash(_("Registration not allowed based on your referrer."), "error")
            return True
    return False


def is_country_blocked(country):
    if not country:
        return False

    blocked_countries = get_setting("auto_decline_countries", "").split("\n")
    return any(
        country_code.strip().upper() == country.upper()
        for country_code in blocked_countries
    )


def register_new_user(form, ip, country):
    verification_token = random_token(16)
    form.user_name.data = form.user_name.data.strip()
    normalize_username(form)

    user = create_new_user(form, ip, country, verification_token)

    if requires_email_verification(user):
        send_email_verification(user)

    sync_user_with_ldap(user, form.password.data)

    if requires_approval(user):
        return handle_user_application(user, form)

    if requires_email_verification(user):
        return redirect(url_for("auth.check_email"))

    finalize_user_registration(user, form)
    return redirect(url_for("auth.filter_selection"))


def normalize_username(form):
    before_normalize = form.user_name.data
    form.user_name.data = normalize_utf(form.user_name.data)
    if before_normalize != form.user_name.data:
        flash(
            _(
                "Your username contained special letters so it was changed to %(name)s.",
                name=form.user_name.data,
            ),
            "warning",
        )


def create_new_user(form, ip, country, verification_token):
    user = User(
        user_name=form.user_name.data,
        title=form.user_name.data,
        email=form.real_email.data,
        verification_token=verification_token,
        verified=not get_setting("email_verification", True),
        instance_id=1,
        ip_address=ip,
        banned=user_ip_banned() or user_cookie_banned(),
        email_unread_sent=False,
        referrer=session.get("Referer", ""),
        alt_user_name=gibberish(random.randint(8, 20)),
        font=get_font_preference(),
        ip_address_country=country,
        timezone=form.timezone.data,
        language_id=g.site.language_id
    )
    if current_app.config['CONTENT_WARNING']:
        user.hide_nsfw = 0
    
    user.set_password(form.password.data)

    db.session.add(user)
    db.session.commit()
    return user


def create_new_user_from_ldap(user_name, email, password, ip):

    user = User(
        user_name=user_name,
        title=user_name,
        email=email,
        verified=True,
        instance_id=1,
        ip_address=ip,
        banned=False,
        email_unread_sent=False,
        referrer=session.get("Referer", ""),
        alt_user_name=gibberish(random.randint(8, 20)),
        font=get_font_preference(),
        language_id=g.site.language_id,
        last_seen=utcnow()
    )
    if current_app.config['CONTENT_WARNING']:
        user.hide_nsfw = 0
    user.set_password(password)

    db.session.add(user)
    db.session.commit()
    finalize_user_setup(user)
    if users_total() == 0:
        user.roles.append(Role.query.get(4))
        db.session.commit()
    return user


def get_font_preference():
    if "Windows" in request.user_agent.string:
        return "inter"  # Default to "Inter" font for better Windows rendering
    return ""


def requires_email_verification(user):
    return get_setting("email_verification", True) and not user.verified


def send_email_verification(user):
    send_verification_email(user)
    if current_app.debug:
        current_app.logger.info("Verify account:" +
                                url_for("auth.verify_email", token=user.verification_token, _external=True))


def requires_approval(user):
    return g.site.registration_mode == "RequireApplication" and g.site.application_question


def handle_user_application(user, form):
    application = create_registration_application(user, form.question.data)
    db.session.commit()

    if get_setting("ban_check_servers", ""):
        from app.shared.tasks import task_selector

        task_selector("check_application", application_id=application.id)

    return redirect(
        url_for("auth.check_email")
        if requires_email_verification(user)
        else url_for("auth.please_wait")
    )


def finalize_user_registration(user, form):
    if user.verified and not user.waiting_for_approval():
        finalize_user_setup(user)
        try:
            sync_user_to_ldap(user.user_name, user.email, form.password.data.strip())
        except Exception as e:
            current_app.logger.error(f"LDAP sync failed for user {user.user_name}: {e}")
        login_user(user, remember=True)


def render_registration_form(form):
    if g.site.registration_mode == "RequireApplication" and g.site.application_question:
        form.question.label = Label("question", markdown_to_html(g.site.application_question))
    if g.site.tos_url is None or not g.site.tos_url.strip():
        del form.terms

    return render_template("auth/register.html", title=_("Register"), form=form, site=g.site,
                           instance_chooser_enabled=get_setting('enable_instance_chooser', False),
                           #google_oauth=current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
                           mastodon_oauth=current_app.config["MASTODON_OAUTH_CLIENT_ID"],
                           discord_oauth=current_app.config["DISCORD_OAUTH_CLIENT_ID"])


def redirect_next_page():
    next_page = request.args.get("next")
    if not next_page or urlsplit(next_page).netloc != "":
        next_page = url_for("main.index")
    return redirect(next_page)


def process_login(form: LoginForm):
    ip = ip_address()
    country = get_country(ip)
    username = form.user_name.data.strip()
    password = form.password.data.strip()

    # attempt authentication with LDAP first (if enabled), then fall back to local
    user = None
    ldap_sync = True

    if current_app.config['LDAP_READ_ENABLE']:
        user = validate_user_ldap_login(username, password, ip)
        if user is not None:  # LDAP authentication succeeded
            ldap_sync = False  # setting it to false avoids writing to LDAP later on

    if user is None:
        # Either LDAP is disabled or LDAP auth failed - try local authentication
        user = find_user(username)
        if not user:
            flash(_("No account exists with that user name."), "error")
            return redirect(url_for("auth.login"))

        if not validate_user_login(user, password, ip):
            return redirect(url_for("auth.login"))

    if requires_email_verification(user):
        return redirect(url_for("auth.check_email"))

    if user.waiting_for_approval():
        return redirect(url_for("auth.please_wait"))

    return log_user_in(user, form, ip, country, ldap_sync=ldap_sync)


def find_user(user_name):
    username = user_name.strip()
    user = User.query.filter(func.lower(User.user_name) == func.lower(username)).filter_by(ap_id=None, deleted=False).first()

    if not user:
        user = User.query.filter_by(email=username, ap_id=None, deleted=False).first()
    if not user:
        ap_id = f"{current_app.config['SERVER_URL']}/u/{username.lower()}"
        user = User.query.filter(User.ap_profile_id.ilike(ap_id), User.deleted.is_(False)).first()

    return user


def validate_user_login(user, password, ip):
    if user.deleted:
        flash(_("No account exists with that user name."), "error")
        return False
    
    if not user.check_password(password):
        if user.password_hash is None:
            message = Markup(_('Invalid password. Please <a href="/auth/reset_password_request">reset your password</a>.'))
            flash(message, "error")
        else:
            flash(_("Invalid password"), "error")
        return False

    if user.id != 1 and (user.banned or user_ip_banned() or user_cookie_banned()):
        handle_banned_user(user, ip)
        return False

    if user.deleted:
        flash(_("This account has been deleted."), "error")
        return False

    return True


def validate_user_ldap_login(user_name: str, password: str, ip: str) -> User | None:
    result = login_with_ldap(user_name, password)
    if result is False:
        flash(_('Login failed'))
        return None
    else:
        user = find_user(user_name)
        if user is None:
            user = create_new_user_from_ldap(user_name, result, password, ip)
        return user


def handle_banned_user(user, ip):
    flash(_("You have been banned."), "error")
    response = make_response(redirect(url_for("auth.login")))

    if user.banned and not user_ip_banned():
        new_ip_ban = IpBan(ip_address=ip, notes=f"{user.user_name} used new IP address")
        db.session.add(new_ip_ban)
        db.session.commit()
        cache.delete_memoized(banned_ip_addresses)

    response.set_cookie("sesion", "17489047567495", expires=datetime(year=2099, month=12, day=30))
    return response


def log_user_in(user, form, ip, country, ldap_sync=True):
    login_user(user, remember=True)
    update_user_session(user, form, ip, country)
    if ldap_sync:
        sync_user_with_ldap(user, form.password.data)

    next_page = determine_next_page()
    response = make_response(redirect(next_page))
    configure_bandwidth_cookies(response, form.low_bandwidth_mode.data)
    return response


def update_user_session(user, form, ip, country):
    user.last_seen = utcnow()
    user.ip_address = ip
    if user.timezone is None:
        user.timezone = form.timezone.data
    user.ip_address_country = country or user.ip_address_country
    db.session.commit()


def sync_user_with_ldap(user, password):
    try:
        sync_user_to_ldap(user.user_name, user.email, password.strip())
    except Exception as e:
        current_app.logger.error(f"LDAP sync failed for user {user.user_name}: {e}")


def determine_next_page():
    next_page = request.args.get("next")
    if not next_page or urlsplit(next_page).netloc != "":
        check_user_finished_onboarding()
        next_page = url_for(
            "auth.filter_selection" if not current_user.finished_onboarding else "main.index"
        )
    return next_page


def check_user_finished_onboarding():
    if not current_user.finished_onboarding and current_user.communities():
        current_user.finished_onboarding = True
        db.session.commit()


def configure_bandwidth_cookies(response, low_bandwidth_mode):
    mode = "1" if low_bandwidth_mode else "0"
    response.set_cookie("low_bandwidth", mode, expires=datetime(year=2099, month=12, day=30))


def render_login_form(form):
    return render_template("auth/login.html", title=_("Login"), form=form,
                           google_oauth=current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
                           mastodon_oauth=current_app.config["MASTODON_OAUTH_CLIENT_ID"],
                           discord_oauth=current_app.config["DISCORD_OAUTH_CLIENT_ID"])
