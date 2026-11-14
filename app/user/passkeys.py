import io

import pyotp
import segno
from flask import redirect, url_for, flash, request, make_response, session, current_app, g, abort, jsonify
import base64
from webauthn import options_to_json, generate_registration_options, verify_registration_response
from webauthn.helpers import parse_registration_credential_json
from webauthn.helpers.exceptions import InvalidRegistrationResponse
from webauthn.helpers.structs import PublicKeyCredentialDescriptor, \
    AttestationConveyancePreference, AuthenticatorSelectionCriteria, ResidentKeyRequirement
from flask_login import current_user
from flask_babel import _

from app import db, cache
from app.user import bp
from app.user.forms import TotpConfirmForm
from app.models import Passkey, UserTotpSecret, UserTrustedDevice
from app.utils import render_template, login_required


# ----------------------------------------------------------------------
@bp.route('/user/passkeys', methods=['GET'])
@login_required
def user_passkey_list():
    return render_template('user/passkeys.html',
                           title='Security', add_passkey=request.args.get('add') is not None,
                           totp_secrets=current_user.totp_secrets,
                           trusted_devices=current_user.trusted_devices,
                           passkeys=current_user.passkeys, user=current_user)


# ----------------------------------------------------------------------
@bp.route('/user/totp/setup', methods=['GET', 'POST'])
@login_required
def user_totp_setup():
    # keep the same pending secret across page reloads so the QR code stays valid
    if 'totp_pending_secret' not in session:
        session['totp_pending_secret'] = pyotp.random_base32()
    secret = session['totp_pending_secret']

    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=current_user.email, issuer_name=g.site.name)
    qr = segno.make_qr(uri)
    output = io.BytesIO()
    qr.save(output, kind='png', scale=5)
    qr_base64 = base64.b64encode(output.getvalue()).decode()

    form = TotpConfirmForm()
    if form.validate_on_submit():
        if totp.verify(form.code.data.strip(), valid_window=1):
            totp_secret = UserTotpSecret(user_id=current_user.id)
            totp_secret.set_secret(secret)
            db.session.add(totp_secret)
            db.session.commit()
            session.pop('totp_pending_secret', None)
            flash(_('Two-factor authentication enabled.'), 'success')
            return redirect(url_for('user.user_passkey_list'))
        flash(_('Invalid code, please try again.'), 'error')
        return redirect(url_for('user.user_totp_setup'))

    return render_template('user/totp_setup.html', title='Set up two-factor authentication',
                           form=form, qr_base64=qr_base64, secret=secret, user=current_user)


# ----------------------------------------------------------------------
@bp.route('/user/totp/delete/<int:totp_id>', methods=['POST'])
@login_required
def user_totp_delete(totp_id):
    totp_secret = UserTotpSecret.query.filter(UserTotpSecret.id == totp_id,
                                              UserTotpSecret.user_id == current_user.id).first()
    if totp_secret:
        db.session.delete(totp_secret)
        db.session.commit()
    return redirect(url_for('user.user_passkey_list'))


# ----------------------------------------------------------------------
@bp.route('/user/trusted_device/delete/<int:device_id>', methods=['POST'])
@login_required
def user_trusted_device_delete(device_id):
    device = UserTrustedDevice.query.filter(UserTrustedDevice.id == device_id,
                                            UserTrustedDevice.user_id == current_user.id).first()
    if device:
        db.session.delete(device)
        db.session.commit()
    return redirect(url_for('user.user_passkey_list'))


# ----------------------------------------------------------------------
@bp.route('/user/passkeys/delete/<int:passkey_id>', methods=['POST'])
@login_required
def user_passkey_delete(passkey_id):
    passkey = Passkey.query.filter(Passkey.id == passkey_id, Passkey.user_id == current_user.id).first()
    if passkey:
        db.session.delete(passkey)
        db.session.commit()
    return redirect(url_for('user.user_passkey_list'))


# ----------------------------------------------------------------------
@bp.route('/user/passkeys/registration/options', methods=['GET', 'POST'])
@login_required(csrf=False)
def user_passkey_options():
    options = generate_registration_options(
        rp_id=request.host,
        rp_name=g.site.name,
        user_name=current_user.user_name,
        user_id=generate_user_handle(),
        user_display_name=current_user.display_name(),
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED),
        exclude_credentials=exclude_credentials(),
        timeout=320000,
    )
    cache.set(f'challenge_{current_user.id}', options.challenge, timeout=3600)
    json_obj = options_to_json(options)
    response = make_response(json_obj)
    response.content_type = 'application/json'
    return response


# ----------------------------------------------------------------------
def exclude_credentials():
    if current_user.passkeys.count():
        return [PublicKeyCredentialDescriptor(id=base64.b64decode(pk.passkey_id)) for pk in current_user.passkeys]
    else:
        return []


# ----------------------------------------------------------------------
def integer_to_bytes(n):
    # Calculate the number of bytes required to represent the integer
    num_bytes = (n.bit_length() + 7) // 8
    return n.to_bytes(num_bytes, 'big')


# ----------------------------------------------------------------------
def bytes_to_integer(byte_array):
    return int.from_bytes(byte_array, 'big')


# ----------------------------------------------------------------------
def generate_user_handle() -> bytes:
    return integer_to_bytes(current_user.id)


# ----------------------------------------------------------------------
@bp.route('/user/passkeys/registration/verification', methods=['POST'])
@login_required(csrf=False)
def user_passkey_verification():
    request_json = request.get_json(force=True)
    registration_credential = parse_registration_credential_json(request_json['response'])
    registration_device = request_json['device']
    try:
        registration_verification = verify_registration_response(
            credential = registration_credential,
            expected_challenge = cache.get(f'challenge_{current_user.id}'),
            expected_origin = f"https://{request.host}",
            expected_rp_id = request.host,
            require_user_verification = False,
        )
    except InvalidRegistrationResponse:
        flash('Passkey registration failed.', 'error')
        return 'FAILED'

    # store credential - encode credential_id as base64 string, but STORE RAW PUBLIC KEY
    credential_id_b64 = base64.b64encode(registration_verification.credential_id).decode('utf-8')

    # Store the raw public key bytes directly (SQLAlchemy will handle binary data)
    db.session.add(Passkey(passkey_id=credential_id_b64, user_id=current_user.id,
                           public_key=registration_verification.credential_public_key,
                           device=registration_device))
    db.session.commit()
    flash(f'{registration_device} passkey registered. Next time you log in, click LOG IN WITH PASSKEY.')
    return current_user.user_name
