from time import sleep

from flask import current_app
from sqlalchemy import text
import random
import string

from app import celery, httpx_client, db
from app.models import UserRegistration
from app.utils import get_setting, get_task_session


@celery.task
def check_user_application(application_id, send_async=True):
    session = get_task_session()
    try:
        application = session.query(UserRegistration).get(application_id)
        if not application or not application.user:
            return

        num_banned = 0

        for domain in get_setting('ban_check_servers', '').split('\n'):
            if not domain.strip():
                continue

            try:
                # Generate fake IP addresses
                fake_ips = []
                for _ in range(3):
                    ip = f"{random.randint(1, 223)}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
                    fake_ips.append(ip)

                ip_index = random.randint(0, len(fake_ips))
                ip_list = fake_ips[:]
                ip_list.insert(ip_index, application.user.ip_address)
                ip_response = httpx_client.post(
                    f'https://{domain.strip()}/api/is_ip_banned',
                    data={'ip_addresses': ','.join(ip_list)},
                    timeout=5
                )

                if ip_response.status_code == 200:
                    ip_results = ip_response.json()
                    # Check the result at the index where real IP was inserted
                    if ip_results and len(ip_results) > ip_index and ip_results[ip_index]:
                        num_banned += 1
                ip_response.close()

                sleep(random.randint(1, 30))

                # Generate fake email addresses
                fake_emails = []
                domains = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "protonmail.com", "mail.com", "zoho.com", "yandex.com", "msn.com", "live.com", "me.com"]
                for _ in range(3):
                    username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(5, 12)))
                    domain_name = random.choice(domains)
                    fake_emails.append(f"{username}@{domain_name}")

                email_index = random.randint(0, len(fake_emails))
                email_list = fake_emails[:]
                email_list.insert(email_index, application.user.email)
                email_response = httpx_client.post(
                    f'https://{domain.strip()}/api/is_email_banned',
                    data={'emails': ','.join(email_list)},
                    timeout=5
                )



                if email_response.status_code == 200:
                    email_results = email_response.json()
                    # Check the result at the index where real email was inserted
                    if email_results and len(email_results) > email_index and email_results[email_index]:
                        num_banned += 1

            except Exception as e:
                current_app.logger.error(f"Error checking bans on {domain}: {str(e)}")
                continue

        if num_banned > 0:
            session.execute(text('UPDATE "user_registration" SET warning = :warning WHERE id = :id',
                                    {'warning': f"{num_banned} instances have banned this account.", 'id': application_id}))
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
