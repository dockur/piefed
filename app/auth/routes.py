import os.path
from datetime import date, datetime
from random import randint
from flask import redirect, url_for, flash, request, make_response, session, Markup, current_app, g
from sqlalchemy import func
from werkzeug.urls import url_parse
from flask_login import login_user, logout_user, current_user
from flask_babel import _
from wtforms import Label

from app import db, cache, limiter, oauth
from app.auth import bp
from app.auth.forms import LoginForm, RegistrationForm, ResetPasswordRequestForm, ResetPasswordForm
from app.auth.util import random_token, normalize_utf, ip2location, no_admins_logged_in_recently
from app.constants import NOTIF_REGISTRATION, NOTIF_REPORT
from app.email import send_verification_email, send_password_reset_email, send_registration_approved_email
from app.models import User, utcnow, IpBan, UserRegistration, Notification, Site
from app.shared.tasks import task_selector
from app.utils import render_template, ip_address, user_ip_banned, user_cookie_banned, banned_ip_addresses, \
    finalize_user_setup, blocked_referrers, gibberish, get_setting, notify_admin


@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("100 per day;20 per 5 minutes", methods=['POST'])
def login():
    if current_user.is_authenticated:
        next_page = request.args.get('next')
        if not next_page or url_parse(next_page).netloc != '':
            next_page = url_for('main.index')
        return redirect(next_page)
    form = LoginForm()
    if form.validate_on_submit():
        form.user_name.data = form.user_name.data.strip()
        user = User.query.filter(func.lower(User.user_name) == func.lower(form.user_name.data)).filter_by(ap_id=None).filter_by(deleted=False).first()
        if user is None:
            user = User.query.filter_by(email=form.user_name.data, ap_id=None, deleted=False).first()
        if user is None:    # ap_profile_id is always lower case so compare it with what_they_typed.lower()
            user = User.query.filter(User.ap_profile_id.ilike(f"https://{current_app.config['SERVER_NAME']}/u/{form.user_name.data.lower()}"), User.deleted == False).first()
        if user is None:
            flash(_('No account exists with that user name.'), 'error')
            return redirect(url_for('auth.login'))
        if user.deleted:
            flash(_('No account exists with that user name.'), 'error')
            return redirect(url_for('auth.login'))
        if not user.check_password(form.password.data):
            if user.password_hash is None:
                message = Markup(_('Invalid password. Please <a href="/auth/reset_password_request">reset your password</a>.'))
                flash(message, 'error')
                return redirect(url_for('auth.login'))
            flash(_('Invalid password'))
            return redirect(url_for('auth.login'))
        if user.id != 1 and (user.banned or user_ip_banned() or user_cookie_banned()):
            flash(_('You have been banned.'), 'error')

            response = make_response(redirect(url_for('auth.login')))
            # Detect if a banned user tried to log in from a new IP address
            if user.banned and not user_ip_banned():
                # If so, ban their new IP address as well
                new_ip_ban = IpBan(ip_address=ip_address(), notes=user.user_name + ' used new IP address')
                db.session.add(new_ip_ban)
                db.session.commit()
                cache.delete_memoized(banned_ip_addresses)

            # Set a cookie so we have another way to track banned people
            response.set_cookie('sesion', '17489047567495', expires=datetime(year=2099, month=12, day=30))
            return response
        if user.waiting_for_approval():
            return redirect(url_for('auth.please_wait'))
        login_user(user, remember=True)
        session['ui_language'] = user.interface_language
        current_user.last_seen = utcnow()
        current_user.ip_address = ip_address()
        current_user.timezone = form.timezone.data
        ip_address_info = ip2location(current_user.ip_address)
        current_user.ip_address_country = ip_address_info['country'] if ip_address_info else current_user.ip_address_country
        db.session.commit()
        [limiter.limiter.clear(limit.limit, *limit.request_args) for limit in  limiter.current_limits]
        next_page = request.args.get('next')
        if not next_page or url_parse(next_page).netloc != '':
            if len(current_user.communities()) == 0:
                next_page = url_for('auth.trump_musk')
            else:
                next_page = url_for('main.index')
        response = make_response(redirect(next_page))
        if form.low_bandwidth_mode.data:
            response.set_cookie('low_bandwidth', '1', expires=datetime(year=2099, month=12, day=30))
        else:
            response.set_cookie('low_bandwidth', '0', expires=datetime(year=2099, month=12, day=30))
        return response
    return render_template('auth/login.html', title=_('Login'), form=form, google_oauth=current_app.config['GOOGLE_OAUTH_CLIENT_ID'])


@bp.route('/logout')
def logout():
    logout_user()
    response = make_response(redirect(url_for('main.index')))
    response.set_cookie('low_bandwidth', '0', expires=datetime(year=2099, month=12, day=30))
    return response


@bp.route('/register', methods=['GET', 'POST'])
@limiter.limit("100 per day;20 per 5 minutes", methods=['POST'])
def register():
    disallowed_usernames = ['admin', 'owner']
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    # Abandoned open instances automatically close registrations after one week
    if g.site.registration_mode == 'Open' and no_admins_logged_in_recently():
        g.site.registration_mode = 'Closed'

    form = RegistrationForm()
    if g.site.registration_mode != 'RequireApplication':
        form.question.validators = ()
    if form.validate_on_submit():
        if form.email.data == '': # ignore any registration where the email field is filled out. spam prevention
            if form.real_email.data.lower().startswith('postmaster@') or form.real_email.data.lower().startswith('abuse@') or \
                    form.real_email.data.lower().startswith('noc@'):
                flash(_('Sorry, you cannot use that email address'), 'error')
            if form.user_name.data in disallowed_usernames:
                flash(_('Sorry, you cannot use that user name'), 'error')
            else:
                # Nazis use 88 in their user names very often.
                if '88' in form.user_name.data:
                    resp = make_response(redirect(url_for('auth.please_wait')))
                    resp.set_cookie('sesion', '17489047567495', expires=datetime(year=2099, month=12, day=30))
                    return resp

                for referrer in blocked_referrers():
                    if referrer in session.get('Referer', ''):
                        resp = make_response(redirect(url_for('auth.please_wait')))
                        resp.set_cookie('sesion', '17489047567495', expires=datetime(year=2099, month=12, day=30))
                        return resp

                # Country-based registration blocking
                ip_address_info = ip2location(ip_address())
                if ip_address_info and ip_address_info['country']:
                    for country_code in get_setting('auto_decline_countries', '').split('\n'):
                        if country_code and country_code.strip().upper() == ip_address_info['country'].upper():
                            return render_template('generic_message.html', title=_('Application declined'),
                                                   message=_('Sorry, we are not accepting registrations from your country.'))

                verification_token = random_token(16)
                form.user_name.data = form.user_name.data.strip()
                before_normalize = form.user_name.data
                form.user_name.data = normalize_utf(form.user_name.data)
                if before_normalize != form.user_name.data:
                    flash(_('Your username contained special letters so it was changed to %(name)s.', name=form.user_name.data), 'warning')
                font = ''
                if 'Windows' in request.user_agent.string:
                    font = 'inter'  # the default font on Windows doesn't look great so defaut to Inter. A windows computer will tend to have a connection that won't notice the 300KB font file.
                user = User(user_name=form.user_name.data, title=form.user_name.data, email=form.real_email.data,
                            verification_token=verification_token, instance_id=1, ip_address=ip_address(),
                            banned=user_ip_banned() or user_cookie_banned(), email_unread_sent=False,
                            referrer=session.get('Referer', ''), alt_user_name=gibberish(randint(8, 20)), font=font)
                user.set_password(form.password.data)
                user.ip_address_country = ip_address_info['country'] if ip_address_info else ''
                user.timezone = form.timezone.data
                if get_setting('email_verification', True):
                    user.verified = False
                else:
                    user.verified = True
                db.session.add(user)
                db.session.commit()
                send_verification_email(user)
                if current_app.debug:
                    current_app.logger.info('Verify account:' + url_for('auth.verify_email', token=user.verification_token, _external=True))
                if g.site.registration_mode == 'RequireApplication' and g.site.application_question:
                    application = UserRegistration(user_id=user.id, answer=form.question.data)
                    db.session.add(application)
                    targets_data = {'application_id':application.id,'user_id':user.id}
                    for admin in Site.admins():
                        notify = Notification(title='New registration', url=f'/admin/approve_registrations?account={user.id}', user_id=admin.id,
                                          author_id=user.id, notif_type=NOTIF_REGISTRATION,
                                          subtype='new_registration_for_approval',
                                          targets=targets_data)
                        admin.unread_notifications += 1
                        db.session.add(notify)
                        # todo: notify everyone with the "approve registrations" permission, instead of just all admins
                    db.session.commit()
                    if get_setting('ban_check_servers', 'piefed.social'):
                        task_selector('check_application', application_id=application.id)
                    return redirect(url_for('auth.please_wait'))
                else:
                    if current_app.config['FLAG_THROWAWAY_EMAILS'] and os.path.isfile('app/static/tmp/disposable_domains.txt'):
                        with open('app/static/tmp/disposable_domains.txt', 'r', encoding='utf-8') as f:
                            disposable_domains = [line.rstrip('\n') for line in f]
                        if user.email_domain() in disposable_domains:
                            # todo: notify everyone with the "approve registrations" permission, instead of just all admins?
                            targets_data = {'suspect_user_id': user.id, 'reporter_id': 1}
                            notify_admin(_('Throwaway email used for account %(username)s', username=user.user_name),
                                         url=f'/u/{user.link()}', author_id=1, notif_type=NOTIF_REPORT,
                                         subtype='user_reported', targets=targets_data)
                    if user.verified:
                        finalize_user_setup(user)
                        login_user(user, remember=True)
                        return redirect(url_for('auth.trump_musk'))
                    else:
                        return redirect(url_for('auth.check_email'))

        resp = make_response(redirect(url_for('auth.trump_musk')))
        if user_ip_banned():
            resp.set_cookie('sesion', '17489047567495', expires=datetime(year=2099, month=12, day=30))
        return resp
    else:
        if g.site.registration_mode == 'RequireApplication' and g.site.application_question != '':
            form.question.label = Label('question', g.site.application_question)
        if g.site.registration_mode != 'RequireApplication':
            del form.question
        return render_template('auth/register.html', title=_('Register'), form=form, 
                               google_oauth=current_app.config['GOOGLE_OAUTH_CLIENT_ID'])


@bp.route('/please_wait', methods=['GET'])
def please_wait():
    return render_template('auth/please_wait.html', title=_('Account under review'))


@bp.route('/check_email', methods=['GET'])
def check_email():
    return render_template('auth/check_email.html', title=_('Check your email'))


@bp.route('/reset_password_request', methods=['GET', 'POST'])
@limiter.limit("20 per day;10 per 5 minutes", methods=['POST'])
def reset_password_request():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    form = ResetPasswordRequestForm()
    if form.validate_on_submit():
        form.email.data = form.email.data.strip()
        if form.email.data.lower().startswith('postmaster@') or form.email.data.lower().startswith('abuse@') or \
                form.email.data.lower().startswith('noc@'):
            flash(_('Sorry, you cannot use that email address.'), 'error')
        else:
            user = User.query.filter(func.lower(User.email) == func.lower(form.email.data)).filter_by(ap_id=None, deleted=False).first()
            if user:
                send_password_reset_email(user)
                flash(_('Check your email for a link to reset your password.'))
                return redirect(url_for('auth.login'))
            else:
                flash(_('No account with that email address exists'), 'warning')
    return render_template('auth/reset_password_request.html',
                           title=_('Reset Password'), form=form)


@bp.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    user = User.verify_reset_password_token(token)
    if not user:
        return redirect(url_for('main.index'))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()
        flash(_('Your password has been reset. Please use it to log in with user name of %(name)s.', name=user.user_name))
        return redirect(url_for('auth.login'))
    return render_template('auth/reset_password.html', form=form)


@bp.route('/verify_email/<token>')
def verify_email(token):
    if token != '':
        user = User.query.filter_by(verification_token=token).first()
        if user is not None:
            if user.banned:
                flash(_('You have been banned.'), 'error')
                return redirect(url_for('main.index'))
            if user.verified:   # guard against users double-clicking the link in the email
                flash(_('Thank you for verifying your email address.'))
                return redirect(url_for('main.index'))
            user.verified = True
            db.session.commit()
            if not user.waiting_for_approval() and user.private_key is None:    # only finalize user set up if this is a brand new user. People can also end up doing this process when they change their email address in which case we DO NOT want to reset their keys, etc!
                finalize_user_setup(user)
            flash(_('Thank you for verifying your email address.'))
        else:
            flash(_('Email address validation failed.'), 'error')
            return redirect(url_for('main.index'))

        if user.waiting_for_approval():
            return redirect(url_for('auth.please_wait'))
        else:
            # Two things need to happen - email verification and (usually) admin approval. They can happen in any order.
            if g.site.registration_mode == 'RequireApplication':
                send_registration_approved_email(user)
            else:
                ...
                #send_welcome_email(user) #not written yet

            login_user(user, remember=True)
            if len(user.communities()) == 0:
                return redirect(url_for('auth.trump_musk'))
            else:
                return redirect(url_for('main.index'))


@bp.route('/validation_required')
def validation_required():
    return render_template('auth/validation_required.html')


@bp.route('/permission_denied')
def permission_denied():
    return render_template('auth/permission_denied.html')


@bp.route('/google_login')
def google_login():
    return oauth.google.authorize_redirect(redirect_uri=url_for('auth.google_authorize', _external=True))


@bp.route('/google_authorize')
def google_authorize():
    try:
        token = oauth.google.authorize_access_token()
    except Exception as e:
        current_app.logger.exception("Google OAuth error")
        flash(_('Login failed due to a problem with Google.'), 'error')
        return redirect(url_for('auth.login'))
    resp = oauth.google.get('oauth2/v2/userinfo', token=token)
    user_info = resp.json()
    google_id = user_info['id']
    email = user_info['email']
    name = user_info.get('name', '')

    # Try to find user by google_id first
    user = User.query.filter_by(google_oauth_id=google_id).first()

    # If not found, check by email and link
    if not user:
        user = User.query.filter_by(email=email).first()
        if user:
            user.google_oauth_id = google_id
        else:
            # Check if registrations are closed
            if g.site.registration_mode == 'Closed':
                flash(_('Account registrations are currently closed.'), 'error')
                return redirect(url_for('auth.login'))

            # Country-based registration blocking
            ip_address_info = ip2location(ip_address())
            if ip_address_info and ip_address_info['country']:
                for country_code in get_setting('auto_decline_countries', '').split('\n'):
                    if country_code and country_code.strip().upper() == ip_address_info['country'].upper():
                        return redirect(url_for('auth.please_wait'))

            user = User(user_name=find_new_username(email), title=name, email=email, verified=True,
                        verification_token='', instance_id=1, ip_address=ip_address(),
                        banned=user_ip_banned() or user_cookie_banned(), email_unread_sent=False,
                        referrer='', alt_user_name=gibberish(randint(8, 20)), google_oauth_id=google_id)
            user.ip_address_country = ip_address_info['country'] if ip_address_info else ''
            db.session.add(user)

        db.session.commit()

        if g.site.registration_mode == 'RequireApplication' and g.site.application_question:
            application = UserRegistration(user_id=user.id, answer='Signed in with Google')
            db.session.add(application)
            targets_data = {'application_id':application.id,'user_id':user.id}
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
            if get_setting('ban_check_servers', 'piefed.social'):
                task_selector('check_application', application_id=application.id)
            return redirect(url_for('auth.please_wait'))
        else:
            if user.verified:
                finalize_user_setup(user)
                login_user(user, remember=True)
                return redirect(url_for('auth.trump_musk'))
            else:
                return redirect(url_for('auth.check_email'))
    else:
        # user already exists
        if user.id != 1 and (user.banned or user_ip_banned() or user_cookie_banned()):
            flash(_('You have been banned.'), 'error')

            response = make_response(redirect(url_for('auth.login')))
            # Detect if a banned user tried to log in from a new IP address
            if user.banned and not user_ip_banned():
                # If so, ban their new IP address as well
                new_ip_ban = IpBan(ip_address=ip_address(), notes=user.user_name + ' used new IP address')
                db.session.add(new_ip_ban)
                db.session.commit()
                cache.delete_memoized(banned_ip_addresses)

            # Set a cookie so we have another way to track banned people
            response.set_cookie('sesion', '17489047567495', expires=datetime(year=2099, month=12, day=30))
            return response
        if user.waiting_for_approval():
            return redirect(url_for('auth.please_wait'))
        else:
            login_user(user, remember=True)
            session['ui_language'] = user.interface_language
            current_user.last_seen = utcnow()
            current_user.ip_address = ip_address()
            ip_address_info = ip2location(current_user.ip_address)
            current_user.ip_address_country = ip_address_info[
                'country'] if ip_address_info else current_user.ip_address_country
            db.session.commit()
            [limiter.limiter.clear(limit.limit, *limit.request_args) for limit in limiter.current_limits]
            next_page = request.args.get('next')
            if not next_page or url_parse(next_page).netloc != '':
                if len(current_user.communities()) == 0:
                    next_page = url_for('auth.trump_musk')
                else:
                    next_page = url_for('main.index')
            return redirect(next_page)


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
