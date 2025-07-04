import os

import boto3
from PIL import Image, ImageOps
from flask import current_app
from pillow_heif import register_heif_opener

from app.utils import gibberish, ensure_directory_exists, store_files_in_s3, guess_mime_type


def process_upload(image_file, destination='posts'):
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

    final_ext = file_ext  # track file extension for conversion

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

            url = f"https://{current_app.config['SERVER_NAME']}/{final_place.replace('app/', '')}"
        else:
            raise Exception('filetype not allowed')
    else:
        url = f"https://{current_app.config['SERVER_NAME']}/{final_place.replace('app/', '')}"

    # Move uploaded file to S3
    if store_files_in_s3():
        session = boto3.session.Session()
        s3 = session.client(
            service_name='s3',
            region_name=current_app.config['S3_REGION'],
            endpoint_url=current_app.config['S3_ENDPOINT'],
            aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
            aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
        )
        s3.upload_file(final_place, current_app.config['S3_BUCKET'], destination + '/' +
                       new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext,
                       ExtraArgs={'ContentType': guess_mime_type(final_place)})
        url = f"https://{current_app.config['S3_PUBLIC_URL']}/{destination}/" + \
              new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext
        s3.close()
        os.unlink(final_place)

    if not url:
        raise Exception('unable to process upload')

    return url
