import os
from typing import List, Tuple

import boto3
import httpx
from flask import g, current_app, flash, render_template
from flask_babel import _
from flask_login import current_user
from sqlalchemy import text, desc

from app import db, celery
from app.activitypub.signature import default_context, send_post_request
from app.constants import POST_TYPE_IMAGE
from app.models import User, Community, Instance, CommunityMember, Post
from app.utils import gibberish, topic_tree, get_request, store_files_in_s3, ensure_directory_exists, guess_mime_type, get_task_session, patch_db_session


def unsubscribe_from_everything_then_delete(user_id):
    if current_app.debug:
        unsubscribe_from_everything_then_delete_task(user_id)
    else:
        unsubscribe_from_everything_then_delete_task.delay(user_id)


@celery.task
def unsubscribe_from_everything_then_delete_task(user_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                user = User.query.get(user_id)
                if user:
                    # unsubscribe
                    communities = CommunityMember.query.filter_by(user_id=user_id).all()
                    for membership in communities:
                        community = Community.query.get(membership.community_id)
                        unsubscribe_from_community(community, user)

                    # federate deletion of account
                    if user.is_local():
                        instances = Instance.query.filter(Instance.dormant == False, Instance.gone_forever == False).all()
                        payload = {
                            "@context": default_context(),
                            "actor": user.public_url(),
                            "id": f"{current_app.config['SERVER_URL']}/activities/delete/{gibberish(15)}",
                            "object": user.public_url(),
                            "to": [
                                "https://www.w3.org/ns/activitystreams#Public"
                            ],
                            "type": "Delete"
                        }
                        for instance in instances:
                            if instance.inbox and instance.online() and instance.id != 1:  # instance id 1 is always the current instance
                                send_post_request(instance.inbox, payload, user.private_key, f"{user.public_url()}#main-key")

                user.banned = True
                user.deleted = True
                user.delete_dependencies()
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def unsubscribe_from_community(community, user):
    if community.instance.gone_forever:
        return

    undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15)
    follow = {
        "actor": user.public_url(),
        "to": [community.public_url()],
        "object": community.public_url(),
        "type": "Follow",
        "id": f"{current_app.config['SERVER_URL']}/activities/follow/{gibberish(15)}"
    }
    undo = {
        'actor': user.public_url(),
        'to': [community.public_url()],
        'type': 'Undo',
        'id': undo_id,
        'object': follow
    }
    send_post_request(community.ap_inbox_url, undo, user.private_key, user.public_url() + '#main-key')


def send_newsletter(form):
    recipients = User.query.filter(User.newsletter == True, User.banned == False, User.ap_id == None).\
        order_by(desc(User.id)).limit(40000)

    from app.email import send_email

    if recipients.count() == 0:
        flash(_('No recipients'), 'error')

    for recipient in recipients:
        body_text = render_template('email/newsletter.txt',
                                    recipient=recipient if not form.test.data else current_user,
                                    content=form.body_text.data)
        body_html = render_template('email/newsletter.html',
                                    recipient=recipient if not form.test.data else current_user,
                                    content=form.body_html.data,
                                    domain=current_app.config['SERVER_NAME'])
        if form.test.data:
            to = current_user.email
        else:
            to = recipient.email

        send_email(subject=form.subject.data, sender=f'{g.site.name} <{current_app.config["MAIL_FROM"]}>',
                   recipients=[to],
                   text_body=body_text, html_body=body_html)

        if form.test.data:
            break


def topics_for_form(current_topic: int) -> List[Tuple[int, str]]:
    result = [(-1, _('None'))]
    topics = topic_tree()
    for topic in topics:
        if topic['topic'].id != current_topic:
            result.append((topic['topic'].id, topic['topic'].name))
        if topic['children']:
            result.extend(topics_for_form_children(topic['children'], current_topic, 1))
    return result


def topics_for_form_children(topics, current_topic: int, depth: int) -> List[Tuple[int, str]]:
    result = []
    for topic in topics:
        if topic['topic'].id != current_topic:
            result.append((topic['topic'].id, '--' * depth + ' ' + topic['topic'].name))
        if topic['children']:
            result.extend(topics_for_form_children(topic['children'], current_topic, depth + 1))
    return result


@celery.task
def move_community_images_to_here(community_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                db.session.execute(text(
                    'UPDATE "post" SET instance_id = 1, ap_create_id = null, ap_announce_id = null WHERE community_id = :community_id'),
                                   {'community_id': community_id})
                db.session.execute(text(
                    'UPDATE "post_reply" SET instance_id = 1, ap_create_id = null, ap_announce_id = null WHERE community_id = :community_id'),
                                   {'community_id': community_id})
                db.session.commit()
                server_name = current_app.config['SERVER_NAME']
                db.session.execute(text(f"""
                    UPDATE "post"
                    SET ap_id = 'https://{server_name}/post/' || id
                    WHERE community_id = :community_id
                """), {'community_id': community_id})

                db.session.execute(text(f"""
                    UPDATE "post_reply"
                    SET ap_id = 'https://{server_name}/comment/' || id
                    WHERE community_id = :community_id
                """), {'community_id': community_id})
                db.session.commit()

                import shutil
                post_ids = list(db.session.execute(text(
                    'SELECT id FROM "post" WHERE type = :post_type AND community_id = :community_id AND deleted is false AND image_id is not null'),
                                                   {'post_type': POST_TYPE_IMAGE, 'community_id': community_id}).scalars())

                if store_files_in_s3():
                    extra_args = {'ContentType': content_type}
                    if current_app.config.get('S3_STORAGE_CLASS'):
                        extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
                    if current_app.config.get('S3_PUBLIC_ACL'):
                        extra_args['ACL'] = 'public-read'
                    boto3_session = boto3.session.Session()
                    s3 = boto3_session.client(
                        service_name='s3',
                        region_name=current_app.config['S3_REGION'],
                        endpoint_url=current_app.config['S3_ENDPOINT'],
                        aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                        aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
                    )
                    for post_id in post_ids:
                        post = Post.query.get(post_id)
                        if post.image.source_url and not post.image.source_url.startswith(
                                f"https://{current_app.config['S3_PUBLIC_URL']}"):
                            if post.image.source_url.startswith('app/static/media'):
                                if os.path.isfile(post.image.source_url):
                                    content_type = guess_mime_type(post.image.source_url)
                                    new_path = post.image.source_url.replace('app/static/media/', "")
                                    s3.upload_file(post.image.source_url, current_app.config['S3_BUCKET'], new_path,
                                                   ExtraArgs=extra_args)
                                    os.unlink(post.image.source_url)
                                    post.image.source_url = f"https://{current_app.config['S3_PUBLIC_URL']}/{new_path}"
                                    db.session.commit()
                            else:
                                # download the image to app/static/tmp
                                try:
                                    # Download the image
                                    response = get_request(post.image.source_url)
                                    if response.status_code == 200:
                                        content_type = response.headers.get('content-type')
                                        if content_type and content_type.startswith('image'):
                                            # Generate file extension from mime type
                                            if ';' in content_type:
                                                content_type_parts = content_type.split(';')
                                                content_type = content_type_parts[0]
                                            content_type_parts = content_type.split('/')
                                            if content_type_parts:
                                                file_extension = '.' + content_type_parts[-1]
                                                if file_extension == '.jpeg':
                                                    file_extension = '.jpg'
                                            else:
                                                file_extension = os.path.splitext(post.image.source_url)[1]
                                                file_extension = file_extension.replace('%3f', '?')
                                                if '?' in file_extension:
                                                    file_extension = file_extension.split('?')[0]

                                            # Save to a temporary file first
                                            new_filename = gibberish(15)
                                            tmp_directory = 'app/static/tmp'
                                            ensure_directory_exists(tmp_directory)
                                            tmp_file = os.path.join(tmp_directory, new_filename + file_extension)
                                            with open(tmp_file, 'wb') as f:
                                                f.write(response.content)
                                            response.close()

                                            # Upload to S3
                                            content_type = guess_mime_type(tmp_file)
                                            extra_args = {'ContentType': content_type}
                                            if current_app.config.get('S3_STORAGE_CLASS'):
                                                extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
                                            if current_app.config.get('S3_PUBLIC_ACL'):
                                                extra_args['ACL'] = 'public-read'
                                            new_path = f"posts/{new_filename[0:2]}/{new_filename[2:4]}/{new_filename}{file_extension}"
                                            s3.upload_file(tmp_file, current_app.config['S3_BUCKET'], new_path,
                                                           ExtraArgs=extra_args)

                                            # Delete temporary file
                                            os.unlink(tmp_file)

                                            # Update post.image.source_url with the S3 url
                                            post.image.source_url = f"https://{current_app.config['S3_PUBLIC_URL']}/{new_path}"
                                            db.session.commit()
                                except Exception as e:
                                    current_app.logger.error(f"Error downloading image for post {post_id}: {str(e)}")
                                    continue
                else:
                    for post_id in post_ids:
                        post = Post.query.get(post_id)
                        if post.image.source_url and not post.image.source_url.startswith(f"{current_app.config['SERVER_URL']}"):
                            if post.image.source_url.startswith('app/static/media'):
                                # If it's already on this server but doesn't have SERVER_NAME in the URL, just update the URL
                                if os.path.isfile(post.image.source_url):
                                    new_path = f"static/media/{post.image.source_url.replace('app/static/media/', '')}"
                                    post.image.source_url = f"{current_app.config['SERVER_URL']}/{new_path}"
                                    db.session.commit()
                            else:
                                # Download the image to app/static/tmp, then move to app/static/media
                                try:
                                    # Download the image
                                    response = get_request(post.image.source_url)
                                    if response.status_code == 200:
                                        content_type = response.headers.get('content-type')
                                        if content_type and content_type.startswith('image'):
                                            # Generate file extension from mime type
                                            if ';' in content_type:
                                                content_type_parts = content_type.split(';')
                                                content_type = content_type_parts[0]
                                            content_type_parts = content_type.split('/')
                                            if content_type_parts:
                                                file_extension = '.' + content_type_parts[-1]
                                                if file_extension == '.jpeg':
                                                    file_extension = '.jpg'
                                            else:
                                                file_extension = os.path.splitext(post.image.source_url)[1]
                                                file_extension = file_extension.replace('%3f', '?')
                                                if '?' in file_extension:
                                                    file_extension = file_extension.split('?')[0]

                                            # Save to a temporary file first
                                            new_filename = gibberish(15)
                                            tmp_directory = 'app/static/tmp'
                                            ensure_directory_exists(tmp_directory)
                                            tmp_file = os.path.join(tmp_directory, new_filename + file_extension)
                                            with open(tmp_file, 'wb') as f:
                                                f.write(response.content)
                                            response.close()

                                            # Now move to the proper directory
                                            directory = 'app/static/media/posts/' + new_filename[0:2] + '/' + new_filename[2:4]
                                            ensure_directory_exists(directory)
                                            final_place = os.path.join(directory, new_filename + file_extension)

                                            # Move file from tmp to final location
                                            shutil.move(tmp_file, final_place)

                                            # Update the post image source_url
                                            new_path = f"static/media/posts/{new_filename[0:2]}/{new_filename[2:4]}/{new_filename}{file_extension}"
                                            post.image.source_url = f"{current_app.config['SERVER_URL']}/{new_path}"
                                            db.session.commit()
                                except httpx.HTTPError as e:
                                    current_app.logger.error(f"Error downloading image for post {post_id}: {str(e)}")
                                    continue
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def switch_to_silenced(instance_id):
    db.session.execute(text('UPDATE "community" SET show_all = false, show_popular = false, topic_id = null WHERE instance_id = :instance_id'),
                       {'instance_id': instance_id})


def switch_to_unsilenced(instance_id, trusted: bool):
    db.session.execute(
        text('UPDATE "community" SET show_all = true, show_popular = :show_popular WHERE instance_id = :instance_id'),
        {'show_popular': trusted, 'instance_id': instance_id})
