from random import randint

from flask import flash, g, redirect, render_template, request, url_for, session
from flask_babel import _
from flask_login import login_user, current_user

from app import db, oauth
from app.auth.util import create_registration_application, get_country, handle_banned_user
from app.models import User, utcnow
from app.shared.tasks import task_selector
from app.utils import finalize_user_setup, get_setting, gibberish, ip_address, user_cookie_banned, user_ip_banned


def is_country_blocked(country: str) -> bool:
    """
    Checks if the user's country is blocked based on IP or settings.
    """
    if country:
        for country_code in get_setting('auto_decline_countries', '').split('\n'):
            if country_code.strip().upper() == country.upper():
                return True
    return False


def handle_user_verification(user, oauth_id_key, token, ip, country, user_info):
    """
    Handles user verification and registration logic.
    """
    if not user:
        email = user_info.get('email')
        username = user_info.get('username', '')

        # Register a new user
        user = initialize_new_user(email, username, oauth_id_key, user_info, ip, country)
        if g.site.registration_mode == 'RequireApplication' and g.site.application_question:
            task_selector('check_application', application_id=user.registration_application.id)
            return redirect(url_for('auth.please_wait'))
        return redirect_next_page()
    else:
        # Handle existing user
        return finalize_user_login(user, token, ip, country)


def initialize_new_user(email, username, oauth_id_key, user_info, ip, country):
    """
    Creates and registers a new user.
    """
    user = User(
        user_name=find_new_username(email),
        email=email,
        title=username,
        verified=True,
        verification_token="",
        instance_id=1,
        ip_address=ip,
        ip_address_country=country,
        banned=user_ip_banned() or user_cookie_banned(),
        referrer=session.get("Referer", ""),
        alt_user_name=gibberish(randint(8, 20)),
    )
    setattr(user, oauth_id_key, user_info['id'])  # Assign OAuth provider ID
    db.session.add(user)
    db.session.commit()

    # Handle registration mode requiring applications
    if g.site.registration_mode == 'RequireApplication' and g.site.application_question:
        user.registration_application = create_registration_application(user, f"Signed in with {oauth_id_key.title()}")
        db.session.commit()
        return user
    else:
        # Finalize registration and log user in
        finalize_user_setup(user)
        login_user(user, remember=True)
        return user


def get_token_and_user_info(provider, user_info_endpoint):
    """
    Retrieve OAuth token and user information for the provider.
    """
    try:
        oauth_provider = getattr(oauth, provider, None)
        if not oauth_provider:
            raise ValueError(f"OAuth provider '{provider}' is not configured.")

        token = oauth_provider.authorize_access_token()

        resp = oauth_provider.get(user_info_endpoint, token=token)
        return token, resp.json()
    except Exception as e:
        print(f"Error retrieving token or user info for {provider}: {e}")
        return None, None


def current_user_is_banned():
    """
    Check if the current user is banned.
    """
    if not current_user.is_authenticated:
        return False
    return current_user.banned or user_ip_banned() or user_cookie_banned()


def current_user_is_deleted():
    """
    Check if the current user account is deleted.
    """
    if not current_user.is_authenticated:
        return False
    return current_user.deleted


def can_user_register():
    """
    Check if the user can register or login based on the site's registration mode.
    """
    if g.site.registration_mode == 'Closed':
        flash(_('Account registrations are currently closed.'), 'error')
        return redirect(url_for('auth.login'))
    if g.site.registration_mode == 'RequireApplication' and not g.site.application_question:
        flash(_('Account registrations are currently closed.'), 'error')
        return redirect(url_for('auth.login'))
    if is_country_blocked(get_country(ip_address())):
        flash(_('Application declined'), 'error')
        return render_template('generic_message.html', title=_('Application declined'),
                               message=_('Sorry, we are not accepting registrations from your country.'))
    return True


def handle_oauth_authorize(provider, user_info_endpoint, oauth_id_key, form_class=None, transform_user_info=None):
    """
    Generalized handler for OAuth authorize routes.
    """
    token, user_info = get_token_and_user_info(provider, user_info_endpoint)

    if not token or not user_info:
        flash(_('Login failed due to a problem with the OAuth server.'), 'error')
        return redirect(url_for('auth.login'))
    if transform_user_info:
        user_info = transform_user_info(user_info)
    if not user_info:
        flash(_('Login failed due to missing user information.'), 'error')
        return redirect(url_for('auth.login'))

    can_user_authenticate = can_user_register()
    if can_user_authenticate is not True:
        if can_user_authenticate is False:
            return redirect(url_for('auth.login'))
        return can_user_authenticate

    ip = ip_address()
    country = get_country(ip)

    user = User.query.filter_by(**{oauth_id_key: user_info['id']}).first()
    if user:
        if user.id != 1 and (user.banned or user_ip_banned() or user_cookie_banned()):
            return handle_banned_user(user, ip)
        elif user.deleted:
            flash(_('This account has been deleted.'), 'error')
            return redirect(url_for('auth.login'))

    if not user:
        form = form_class() if form_class else None
        # For providers requiring a registration form
        if form_class and (request.method == "GET" or not form.validate_on_submit()):
            # session['code'] = request.args.get('code')
            session["user_info"] = user_info
            return render_template(f'auth/{provider}_authorize.html', form=form, user_info=user_info)

    return handle_user_verification(user, oauth_id_key, token, ip, country, user_info)


def finalize_user_login(user, token, ip, country):
    """
    Performs final steps of logging in a user once verified.
    """
    user.last_seen = utcnow()
    user.ip_address = ip
    user.ip_address_country = country
    db.session.commit()

    login_user(user, remember=True)
    return redirect_next_page()


def find_new_username(email: str) -> str:
    email_parts = email.lower().split('@')
    original_email_part = email_parts[0]
    attempts = 0

    while attempts < 1000:
        existing_user = User.query.filter(User.user_name == email_parts[0], User.ap_id == None).first()
        if existing_user is None:
            return email_parts[0]
        else:
            email_parts[0] = original_email_part + str(randint(1, 1000))
            attempts += 1

    return gibberish(10)
