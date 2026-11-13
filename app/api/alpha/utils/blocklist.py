from types import SimpleNamespace

from flask import g
from slugify import slugify
from sqlalchemy import desc

from app import db
from app.api.alpha.views import blocklist_view
from app.blocklist.util import apply_blocklist_url_rules
from app.constants import *
from app.models import User, Blocklist, BlocklistMember
from app.shared.blocklist import join_blocklist, leave_blocklist, make_blocklist, edit_blocklist, delete_blocklist
from app.utils import authorise_api_user


def _subscribed_blocklist_ids(user_id) -> list:
    if not user_id:
        return []
    return [bm.blocklist_id for bm in db.session.query(BlocklistMember).filter_by(user_id=user_id)]


def get_blocklist_list(auth, data, user_id=None) -> dict:
    mine_only = data['mine_only'] if 'mine_only' in data else False
    subscribed_only = data['subscribed_only'] if 'subscribed_only' in data else False
    include_blocked = data['include_blocked'] if 'include_blocked' in data else False
    page = int(data['page']) if 'page' in data else 1
    limit = int(data['limit']) if 'limit' in data else 50

    if auth:
        user_id = authorise_api_user(auth)
        g.user = User.query.get(user_id)

    if (mine_only or subscribed_only) and not user_id:
        raise Exception('incorrect_login')

    subscribed = _subscribed_blocklist_ids(user_id)

    blocklists = Blocklist.query
    if mine_only:
        blocklists = blocklists.filter(Blocklist.user_id == user_id)
    if subscribed_only:
        blocklists = blocklists.filter(Blocklist.id.in_(subscribed))
    blocklists = blocklists.order_by(desc(Blocklist.subscriptions_count), Blocklist.title).\
        paginate(page=page, per_page=limit, error_out=False)

    return {
        "blocklists": [blocklist_view(blocklist, user_id=user_id, subscribed=subscribed, include_blocked=include_blocked)
                       for blocklist in blocklists.items],
        "next_page": str(blocklists.next_num) if blocklists.next_num else None
    }


def get_blocklist(auth, data, user_id=None) -> dict:
    id = data['id'] if 'id' in data else False
    name = data['name'] if 'name' in data else ''

    if auth:
        user_id = authorise_api_user(auth)
        g.user = User.query.get(user_id)

    if id:
        blocklist = Blocklist.query.get(id)
    elif name and '@' in name:
        parts = name.lstrip('%').split('@')
        blocklist = Blocklist.query.filter(Blocklist.machine_name == parts[0].lower(),
                                           Blocklist.ap_domain == parts[1].lower()).first()
    else:
        raise Exception('invalid_request')

    if blocklist is None:
        raise Exception('blocklist_not_found')

    return blocklist_view(blocklist, user_id=user_id, subscribed=_subscribed_blocklist_ids(user_id), include_blocked=True)


def post_blocklist_follow(auth, data):
    blocklist_id = data['blocklist_id']
    follow = data['follow']

    user = authorise_api_user(auth, return_type='model')
    blocklist = Blocklist.query.get(blocklist_id)
    if not blocklist:
        raise Exception('blocklist_not_found')

    membership = blocklist.subscribed(user.id)
    if follow:
        if membership == SUBSCRIPTION_NONMEMBER:
            join_blocklist(blocklist, SRC_API, auth)
    else:
        if membership in (SUBSCRIPTION_MEMBER, SUBSCRIPTION_PENDING):
            leave_blocklist(blocklist, SRC_API, auth)
        elif membership == SUBSCRIPTION_OWNER:
            raise Exception('cannot_leave_own_blocklist')

    return blocklist


def post_blocklist(auth, data):
    user = authorise_api_user(auth, return_type='model')
    if user.banned:
        raise Exception('incorrect_login')

    url = SimpleNamespace(data=slugify(data['name'].strip(), separator='_').lower(), errors=[])
    if not apply_blocklist_url_rules(url):
        raise Exception(str(url.errors[0]))

    input_data = {'url': url.data, 'title': data['title'],
                  'description': data['description'] if 'description' in data else '',
                  'members': data['members'] if 'members' in data else ''}

    blocklist, unresolved = make_blocklist(input_data, SRC_API, auth)

    result = get_blocklist(auth, {'id': blocklist.id})
    result['unresolved_members'] = unresolved
    return result


def put_blocklist(auth, data):
    blocklist_id = data['blocklist_id']
    blocklist = Blocklist.query.get(blocklist_id)
    if not blocklist or not blocklist.is_local():
        raise Exception('blocklist_not_found')

    input_data = {'title': data['title'] if 'title' in data else blocklist.title,
                  'description': data['description'] if 'description' in data else (blocklist.description or ''),
                  'members': data['members'] if 'members' in data else None}   # None = leave the accounts unchanged

    # edit_blocklist does the auth and ownership checks
    unresolved = edit_blocklist(input_data, blocklist, SRC_API, auth)

    result = get_blocklist(auth, {'id': blocklist_id})
    result['unresolved_members'] = unresolved
    return result


def post_blocklist_delete(auth, data):
    blocklist_id = data['blocklist_id']
    deleted = data['deleted']

    user = authorise_api_user(auth, return_type='model')
    blocklist = Blocklist.query.get(blocklist_id)
    if not blocklist or not blocklist.is_local() or (blocklist.user_id != user.id and not user.is_admin()):
        raise Exception('blocklist_not_found')

    # Capture view before deletion since delete_blocklist removes the record
    blocklist_json = get_blocklist(auth, {'id': blocklist_id})

    if deleted:
        delete_blocklist(blocklist, SRC_API, auth)

    return blocklist_json
