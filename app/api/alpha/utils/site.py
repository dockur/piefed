from sqlalchemy import or_, cast, String

from app.api.alpha.views import site_view, federated_instances_view, site_instance_chooser_view
from app.constants import SRC_API, VERSION
from app.models import InstanceBlock, InstanceChooser, Language
from app.shared.site import block_remote_instance, unblock_remote_instance
from app.utils import authorise_api_user, instance_banned, opengraph_parse


def get_site(auth):
    if auth:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = None
    site_json = site_view(user)
    return site_json


def get_site_version(auth):
    return {'version': VERSION}


def get_site_instance_chooser(auth):
    return site_instance_chooser_view()


def get_site_instance_chooser_search(query_params):
    result = {
        'result': []
    }

    instances = InstanceChooser.query
    if query_params.get('q', '') != '':
        instances = instances.filter(or_(InstanceChooser.domain.ilike(f"%{query_params['q']}%"),
                                         cast(
                                             InstanceChooser.data['elevator_pitch'],
                                             String
                                         ).ilike(f"%{query_params['q']}%")))
    if query_params.get('nsfw', '') != '':
        if query_params['nsfw'] == 'yes':
            instances = instances.filter(InstanceChooser.nsfw == True)
        elif query_params['nsfw'] == 'no':
            instances = instances.filter(InstanceChooser.nsfw == False)
    if query_params.get('language', '') != '':
        language_id = Language.query.filter(Language.code == query_params['language']).first()
        if language_id:
            instances = instances.filter(InstanceChooser.language_id == language_id.id)
    if query_params.get('newbie', '') != '':
        if query_params['newbie'] == 'yes':
            instances = instances.filter(InstanceChooser.newbie_friendly == True)
        elif query_params['newbie'] == 'no':
            instances = instances.filter(InstanceChooser.newbie_friendly == False)

    for instance in instances.all():
        if instance.hide or instance_banned(instance.domain):
            continue
        instance_data = instance.data
        if instance_data['registration_mode'] == 'Closed':
            continue
        instance_data['domain'] = instance.domain
        instance_data['id'] = instance.id
        instance_data['language'] = instance_data['language']['name']
        result['result'].append(instance_data)
    return result


def get_federated_instances(data):
    return federated_instances_view()


def get_site_metadata(auth, data):
    if auth:
        user = authorise_api_user(auth)
    else:
        user = None

    metadata = opengraph_parse(data['url'])
    if metadata:
        return {'metadata': {
            'title': metadata['og:title'] if 'og:title' in metadata else '',
            'description': metadata['description'] if 'description' in metadata else '',
            'image': metadata['og:image'] if 'og:image' in metadata else '',
            'embed_video_url': ''
        }}
    else:
        raise Exception('fetch_failed')


def post_site_block(auth, data):
    instance_id = data['instance_id']
    block = data['block']

    user_id = block_remote_instance(instance_id, SRC_API, auth) if block else unblock_remote_instance(instance_id, SRC_API, auth)
    blocked = InstanceBlock.query.filter_by(user_id=user_id, instance_id=instance_id).first()
    block = True if blocked else False
    data = {
        "blocked": block
    }
    return data
