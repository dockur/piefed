from app.models import User
from flask import current_app
from flask_login import current_user
from sqlalchemy import text

from app import db
from app.shared.upload import process_upload, process_file_delete
from app.utils import authorise_api_user


def post_upload_image(auth, image_file=None):
    try:
        user: User = authorise_api_user(auth, return_type="model")
    except Exception:
        if current_user.is_authenticated:
            user = current_user
        else:
            raise Exception('incorrect_login')

    user_id = user.id
    total_size = 0
    file_sizes = db.session.execute(text('SELECT file_id, size FROM "user_file" WHERE user_id = :user_id'),
                                    {'user_id': user_id}).all()
    for fs in file_sizes:
        total_size += fs[1]

    if total_size > current_app.config['FILE_UPLOAD_QUOTA']:
        raise Exception('quota_exceeded')

    url = process_upload(image_file, user=user)
    return {'url': url}


def post_upload_community_image(auth, image_file=None):
    authorise_api_user(auth)
    url = process_upload(image_file, destination='communities')
    return {'url': url}


def post_upload_user_image(auth, image_file=None):
    authorise_api_user(auth)
    url = process_upload(image_file, destination='users')
    return {'url': url}


def post_image_delete(auth, data):
    try:
        user_id = authorise_api_user(auth)
    except Exception:
        if current_user.is_authenticated:
            user_id = current_user.id
        else:
            raise Exception('incorrect_login')
    process_file_delete(data['file'], user_id=user_id)
    return {'result': 'ok'}
