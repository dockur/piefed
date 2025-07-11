import random
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

from app import cache, db
from app.constants import NOTIF_REGISTRATION
from app.email import send_verification_email
from app.ldap_utils import sync_user_to_ldap
from app.models import IpBan, Notification, Site, User, UserRegistration, utcnow
from app.utils import banned_ip_addresses, blocked_referrers, finalize_user_setup, get_request, get_setting, gibberish, \
    ip_address, markdown_to_html, render_template, user_cookie_banned, user_ip_banned


# Return a random string of 6 letter/digits.
def random_token(length=6) -> str:
    return "".join(
        [random.choice('0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ') for x in range(length)])


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
    targets_data = {'gen': '0', 'application_id': application.id, 'user_id': application.user_id}
    for admin in Site.admins():
        notify = Notification(title='New registration',
                              url=f'/admin/approve_registrations?account={application.user_id}', user_id=admin.id,
                              author_id=application.user_id, notif_type=NOTIF_REGISTRATION,
                              subtype='new_registration_for_approval',
                              targets=targets_data)
        admin.unread_notifications += 1
        db.session.add(notify)


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

    if contains_banned_username_patterns(form.user_name.data):
        return redirect_with_session_cookie("auth.please_wait")

    if is_restricted_by_referrer():
        return redirect_with_session_cookie("auth.please_wait")

    if is_country_blocked(country):
        return render_template(
            "generic_message.html",
            title=_("Application declined"),
            message=_("Sorry, we are not accepting registrations from your country."),
        )

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


def contains_banned_username_patterns(username):
    if "88" in username:
        flash(_("Sorry, this username pattern is not allowed."), "error")
        return True
    return False


def redirect_with_session_cookie(route):
    response = make_response(redirect(url_for(route)))
    response.set_cookie(
        "sesion", "17489047567495", expires=datetime(year=2099, month=12, day=30)
    )
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
    return redirect(url_for("auth.trump_musk"))


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
        instance_id=1,
        ip_address=ip,
        banned=user_ip_banned() or user_cookie_banned(),
        email_unread_sent=False,
        referrer=session.get("Referer", ""),
        alt_user_name=gibberish(random.randint(8, 20)),
        font=get_font_preference(),
        ip_address_country=country,
        timezone=form.timezone.data,
    )
    user.set_password(form.password.data)

    db.session.add(user)
    db.session.commit()
    return user


def get_font_preference():
    if "Windows" in request.user_agent.string:
        return "inter"  # Default to "Inter" font for better Windows rendering
    return ""


def requires_email_verification(user):
    return get_setting("email_verification", True) and not user.verified


def send_email_verification(user):
    user.verified = False
    send_verification_email(user)
    if current_app.debug:
        current_app.logger.info(
            "Verify account:"
            + url_for(
                "auth.verify_email", token=user.verification_token, _external=True
            )
        )


def requires_approval(user):
    return (
        g.site.registration_mode == "RequireApplication" and g.site.application_question
    )


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

    return render_template(
        "auth/register.html",
        title=_("Register"),
        form=form,
        site=g.site,
        google_oauth=current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
        mastodon_oauth=current_app.config["MASTODON_OAUTH_CLIENT_ID"],
        discord_oauth=current_app.config["DISCORD_OAUTH_CLIENT_ID"],
        patreon_oauth=current_app.config["PATREON_CLIENT_ID"],
    )


def redirect_next_page():
    next_page = request.args.get("next")
    if not next_page or urlsplit(next_page).netloc != "":
        next_page = url_for("main.index")
    return redirect(next_page)


def process_login(form):
    ip = ip_address()
    country = get_country(ip)
    user = find_user(form)

    if not user:
        flash(_("No account exists with that user name."), "error")
        return redirect(url_for("auth.login"))

    if not validate_user_login(user, form.password.data, ip):
        return redirect(url_for("auth.login"))

    return log_user_in(user, form, ip, country)


def find_user(form):
    username = form.user_name.data.strip()
    user = (
        User.query.filter(func.lower(User.user_name) == func.lower(username))
        .filter_by(ap_id=None, deleted=False)
        .first()
    )

    if not user:
        user = User.query.filter_by(email=username, ap_id=None, deleted=False).first()
    if not user:
        ap_id = f"https://{current_app.config['SERVER_NAME']}/u/{username.lower()}"
        user = User.query.filter(
            User.ap_profile_id.ilike(ap_id), User.deleted.is_(False)
        ).first()

    return user


def validate_user_login(user, password, ip):
    if user.deleted:
        flash(_("No account exists with that user name."), "error")
        return False

    if not user.check_password(password):
        handle_invalid_password(user)
        return False

    if user.id != 1 and (user.banned or user_ip_banned() or user_cookie_banned()):
        handle_banned_user(user, ip)
        return False

    if user.deleted:
        flash(_("This account has been deleted."), "error")
        return False

    if user.waiting_for_approval():
        return redirect(url_for("auth.please_wait"))

    return True


def handle_invalid_password(user):
    if user.password_hash is None:
        message = Markup(
            _(
                'Invalid password. Please <a href="/auth/reset_password_request">reset your password</a>.'
            )
        )
        flash(message, "error")
    else:
        flash(_("Invalid password"), "error")


def handle_banned_user(user, ip):
    flash(_("You have been banned."), "error")
    response = make_response(redirect(url_for("auth.login")))

    if user.banned and not user_ip_banned():
        new_ip_ban = IpBan(ip_address=ip, notes=f"{user.user_name} used new IP address")
        db.session.add(new_ip_ban)
        db.session.commit()
        cache.delete_memoized(banned_ip_addresses)

    response.set_cookie(
        "sesion", "17489047567495", expires=datetime(year=2099, month=12, day=30)
    )
    return response


def log_user_in(user, form, ip, country):
    login_user(user, remember=True)
    update_user_session(user, form, ip, country)
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
        next_page = url_for(
            "auth.trump_musk" if not current_user.communities() else "main.index"
        )
    return next_page


def configure_bandwidth_cookies(response, low_bandwidth_mode):
    mode = "1" if low_bandwidth_mode else "0"
    response.set_cookie(
        "low_bandwidth", mode, expires=datetime(year=2099, month=12, day=30)
    )


def render_login_form(form):
    return render_template(
        "auth/login.html",
        title=_("Login"),
        form=form,
        google_oauth=current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
        mastodon_oauth=current_app.config["MASTODON_OAUTH_CLIENT_ID"],
        discord_oauth=current_app.config["DISCORD_OAUTH_CLIENT_ID"],
        patreon_oauth=current_app.config["PATREON_CLIENT_ID"],
    )