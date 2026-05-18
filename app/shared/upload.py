import os

import boto3
from PIL import Image, ImageOps
from flask import current_app
from pillow_heif import register_heif_opener
from sqlalchemy import text

from app import db
from app.models import File, user_file
from app.utils import gibberish, ensure_directory_exists, store_files_in_s3, guess_mime_type, sanitize_svg


def process_upload(image_file, destination='posts', user_id=None):
    # should have errored earlier if no upload, but just to be paranoid
    if not image_file or image_file.filename == '':
        raise Exception('file not uploaded')

    allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.mpo', '.avif', '.svg']
    file_ext = os.path.splitext(image_file.filename)[1]
    if file_ext.lower() not in allowed_extensions:
        raise Exception('filetype not allowed')

    new_filename = gibberish(15)
    # set up the storage directory
    if store_files_in_s3():
        directory = 'app/static/tmp'
    else:
        directory = 'app/static/media/' + destination + '/' + new_filename[0:2] + '/' + new_filename[2:4]
    ensure_directory_exists(directory)

    # save the file
    final_place = os.path.join(directory, new_filename + file_ext)
    image_file.seek(0)
    image_file.save(final_place)
    file_size = os.path.getsize(final_place)

    final_ext = file_ext.lower()  # track file extension for conversion

    # Sanitize SVG files to remove potentially dangerous elements
    if final_ext == '.svg':
        sanitize_svg(final_place)

    if file_ext.lower() == '.heic':
        register_heif_opener()
    if file_ext.lower() == '.avif':
        import pillow_avif  # NOQA

    Image.MAX_IMAGE_PIXELS = 89478485

    # Use environment variables to determine image max dimension, format, and quality
    image_max_dimension = current_app.config['MEDIA_IMAGE_MAX_DIMENSION']
    image_format = current_app.config['MEDIA_IMAGE_FORMAT']
    image_quality = current_app.config['MEDIA_IMAGE_QUALITY']

    if image_format == 'AVIF':
        import pillow_avif  # NOQA

    if not final_place.endswith('.svg') and not final_place.endswith('.gif'):
        img = Image.open(final_place)
        if '.' + img.format.lower() in allowed_extensions:
            img = ImageOps.exif_transpose(img)
            img = img.convert('RGB' if (image_format == 'JPEG' or final_ext in ['.jpg', '.jpeg']) else 'RGBA')
            img.thumbnail((image_max_dimension, image_max_dimension), resample=Image.LANCZOS)

            kwargs = {}
            if image_format:
                kwargs['format'] = image_format.upper()
                final_ext = '.' + image_format.lower()
                final_place = os.path.splitext(final_place)[0] + final_ext
            if image_quality:
                kwargs['quality'] = int(image_quality)

            img.save(final_place, optimize=True, **kwargs)

            file_size = os.path.getsize(final_place)

            url = f"{current_app.config['SERVER_URL']}/{final_place.replace('app/', '')}"
        else:
            raise Exception('filetype not allowed')
    else:
        url = f"{current_app.config['SERVER_URL']}/{final_place.replace('app/', '')}"

    # Move uploaded file to S3
    if store_files_in_s3():
        session = boto3.session.Session()
        extra_args = {'ContentType': guess_mime_type(final_place)}
        if current_app.config.get('S3_STORAGE_CLASS'):
            extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
        s3 = session.client(
            service_name='s3',
            region_name=current_app.config['S3_REGION'],
            endpoint_url=current_app.config['S3_ENDPOINT'],
            aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
            aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
        )
        s3.upload_file(final_place, current_app.config['S3_BUCKET'], destination + '/' +
                       new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext,
                       ExtraArgs=extra_args)
        url = f"https://{current_app.config['S3_PUBLIC_URL']}/{destination}/" + \
              new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext
        s3.close()
        os.unlink(final_place)

    # associate file with uploader. Only provide user_id to this function when the image is not being used for a community icon, user avatar, etc where there is some other way to associate the image with the user.
    if user_id:
        file = File(source_url=url)
        db.session.add(file)
        db.session.commit()
        db.session.execute(text('INSERT INTO "user_file" (file_id, user_id, size) VALUES (:file_id, :user_id, :size)'),
                           {'file_id': file.id, 'user_id': user_id, 'size': file_size})
        db.session.commit()

    if not url:
        raise Exception('unable to process upload')

    return url


def process_file_delete(url: str, user_id: int):
    if user_id:
        file = db.session.query(File).join(user_file).filter(user_file.c.file_id == File.id, user_file.c.user_id == user_id)\
            .filter(File.source_url == url).first()
        if file:
            file.delete_from_disk()
            db.session.execute(text('DELETE FROM "user_file" WHERE file_id = :file_id'), {'file_id': file.id})
            db.session.delete(file)
            db.session.commit()
