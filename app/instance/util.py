from flask import current_app

from app import celery
from app.constants import SRC_API
from app.models import User
from app.shared.user import follow_user
from app.user.utils import search_for_user
from app.utils import validate_email, get_task_session, patch_db_session


def is_fedi_handle(handle):
    return validate_email(handle) or validate_email(handle[1:])


@celery.task
def bulk_follow(user_id, to_follow):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                following_user = session.query(User).get(user_id)
                auth_token = f"Bearer {following_user.encode_jwt_token()}"
                for tf in to_follow:
                    user = search_for_user(tf)
                    if user and following_user.is_following(user) == 'no':
                        follow_user(user.id, src=SRC_API, auth=auth_token)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
