import httpx
from flask import redirect, url_for, flash, request, current_app
from flask_login import logout_user, current_user
from flask_babel import _, lazy_gettext as _l

from app import db
from app.models import User, CmsPage
from app.user import bp
from app.utils import render_template, login_required


@bp.route('/donate')
def choose_plan():
    cms_page = CmsPage.query.filter(CmsPage.url == '/donate').first()
    if current_user.is_authenticated:
        # inspired by https://stripe.com/docs/payments/checkout/subscriptions/starting
        if current_user.stripe_subscription_id is not None:
            flash(_('Thank you for supporting %(instance_name)s! Any choice you make below will replace your current donation plan.', instance_name=current_app.config['SERVER_NAME']))

        if current_app.config['STRIPE_SECRET_KEY']:
            return render_template('user/choose_plan.html', title=_('Choose a donation plan'),
                                   instance_name=current_app.config['SERVER_NAME'], monthly_small=current_app.config['STRIPE_MONTHLY_SMALL'],
                                   monthly_big=current_app.config['STRIPE_MONTHLY_BIG'], monthly_small_text=current_app.config['STRIPE_MONTHLY_SMALL_TEXT'],
                                   monthly_big_text=current_app.config['STRIPE_MONTHLY_BIG_TEXT']
                                   )
        else:
            return render_template('donate.html', title=_('Donate'), cms_page=cms_page)
    else:
        if current_app.config['STRIPE_SECRET_KEY']:
            flash(_('Log in to donate to %(instance_name)s or donate to the PieFed project using the button below.', instance_name=current_app.config['SERVER_NAME']))
        return render_template('donate.html', title=_('Donate'), cms_page=cms_page)


@bp.route('/stripe_redirect/<plan>', methods=['GET'])
@login_required
def stripe_redirect(plan):
    import stripe
    stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
    if current_user.stripe_customer_id is None or current_user.stripe_customer_id == '':             # Stripe won't let us use both customer and email, so
        stripe_customer = None                              # only specify the email for new subscriptions
        stripe_customer_email = current_user.email
    else:
        stripe_customer = current_user.stripe_customer_id
        stripe_customer_email = None

    stripe_session = None

    if plan == 'monthly_small':
        stripe_session = stripe.checkout.Session.create(
            client_reference_id=current_user.id,
            customer=stripe_customer,
            customer_email=stripe_customer_email,
            # payment_method_types=['card'],
            line_items=[{
                'price': current_app.config['STRIPE_MONTHLY_SMALL'],
                'quantity': 1,
            }],
            mode='subscription',
            success_url=url_for('user.stripe_result', result='success', _external=True),
            cancel_url=url_for('user.stripe_result', result='failure', _external=True),
        )
    elif plan == 'monthly_big':
        stripe_session = stripe.checkout.Session.create(
            client_reference_id=current_user.id,
            customer=stripe_customer,
            customer_email=stripe_customer_email,
            # payment_method_types=['card'],
            line_items=[{
                'price': current_app.config['STRIPE_MONTHLY_BIG'],
                'quantity': 1,
            }],
            mode='subscription',
            success_url=url_for('user.stripe_result', result='success', _external=True),
            cancel_url=url_for('user.stripe_result', result='failure', _external=True),
        )
    elif plan == 'billing':
        with httpx.Client(timeout=10) as client:
            response = client.post(
                'https://api.stripe.com/v1/billing_portal/sessions',
                data={'customer': stripe_customer, 'return_url': url_for('user.choose_plan', _external=True)},
                auth=(current_app.config['STRIPE_SECRET_KEY'], '')
            )

        stripe_session = response.json()
        return redirect(stripe_session['url'])

    return render_template('user/stripe_redirect.html', title=_('Please wait...'),
                           key=current_app.config['STRIPE_PUBLISHABLE_KEY'], stripe_session=stripe_session)


@bp.route('/stripe_webhook', methods=['POST'])
def stripe_webhook():
    import stripe
    # inspired by https://stripe.com/docs/payments/checkout/fulfillment#webhooks
    # During development, run this in a terminal:
    # stripe listen --forward-to https://your_dev_url/stripe_webhook
    stripe.api_key = current_app.config['STRIPE_SECRET_KEY']

    payload = request.data.decode("utf-8")
    sig_header = request.headers.get("Stripe-Signature", None)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, current_app.config['WEBHOOK_SIGNING_SECRET'])
    except ValueError:
        # Invalid payload
        return 'invalid payload', 400
    except stripe.error.SignatureVerificationError:
        # Invalid signature
        return 'could not verify signature', 400

    # Handle the checkout.session.completed event
    # Fulfill the purchase...
    if event['type'] == 'checkout.session.completed':
        stripe_session = event['data']['object']
        u = User.query.get(stripe_session['client_reference_id'])
        if u is None:   # could not find user, bail
            return 'Ok'
        u.stripe_customer_id = stripe_session['customer']
        if 'subscription' in stripe_session and stripe_session['subscription'] is not None:
            if u.stripe_subscription_id is not None and u.stripe_subscription_id != stripe_session['subscription']:  # Remove previous subscription, if any
                try:
                    stripe.Subscription.delete(u.stripe_subscription_id)
                except:
                    pass

            u.stripe_subscription_id = stripe_session['subscription']

        db.session.commit()
    # Handle the customer.subscription.deleted event
    elif event['type'] == 'customer.subscription.deleted':
        subscription = event['data']['object']
        u = User.query.filter_by(stripe_subscription_id=subscription['id']).first()
        if u is not None:
            u.stripe_subscription_id = None
            db.session.commit()

    # more subscription types at https://stripe.com/docs/api/events/types

    return 'ok'


@bp.route('/stripe_result/<result>')
@login_required
def stripe_result(result):
    if result == 'success' or result == 'failure':
        return render_template('user/stripe_result.html', result=result)
    else:
        return ''


@bp.route('/plan_unsubscribe/<account_id>/<subscription>')
@login_required
def plan_unsubscribe(account_id, subscription):
    import stripe
    if current_user.stripe_subscription_id is None:
        return render_template('generic_message.html', title=_('You are not donating'), message=_('There are no regular donations set to occur in the future.'))
    elif current_user.stripe_subscription_id == subscription:
        stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
        stripe.Subscription.delete(current_user.stripe_subscription_id)
        current_user.stripe_subscription_id = None
        db.session.commit()
        return render_template('generic_message.html', title=_('Regular donation cancelled'), message=_('Your donation has been cancelled.'))

