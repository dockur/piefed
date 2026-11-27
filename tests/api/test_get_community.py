"""HTTP integration tests for GET /api/alpha/community."""
from app import db


def test_get_community_by_id_anonymous(client, make_community):
    community_id = make_community(name='testcomm')
    r = client.get('/api/alpha/community', query_string={'id': community_id})
    assert r.status_code == 200
    body = r.get_json()
    assert body['community_view']['community']['id'] == community_id
    assert body['community_view']['community']['name'] == 'testcomm'


def test_get_community_by_name(client, make_community):
    community_id = make_community(name='lookupname')
    r = client.get('/api/alpha/community', query_string={'name': 'lookupname'})
    assert r.status_code == 200
    assert r.get_json()['community_view']['community']['id'] == community_id


def test_get_community_missing_params(client):
    r = client.get('/api/alpha/community')
    assert r.status_code == 400
    assert r.get_json()['message'] == 'id or name required'


def test_get_community_subscribed_status(client, app, auth_headers, user_id, make_community):
    from app.models import CommunityMember
    subscribed_id = make_community(name='subbed')
    other_id = make_community(name='notsubbed')
    with app.app_context():
        db.session.add(CommunityMember(user_id=user_id, community_id=subscribed_id))
        db.session.commit()

    r = client.get('/api/alpha/community', query_string={'id': subscribed_id}, headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['community_view']['subscribed'] == 'Subscribed'

    r = client.get('/api/alpha/community', query_string={'id': other_id}, headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['community_view']['subscribed'] == 'NotSubscribed'


def test_get_community_not_found(client):
    r = client.get('/api/alpha/community', query_string={'id': 999999})
    assert r.status_code == 400


def test_get_community_invalid_param_type(client):
    r = client.get('/api/alpha/community', query_string={'id': 'abc'})
    assert r.status_code == 400
    body = r.get_json()
    assert body['message'] == 'Validation failed'
    assert body['status']['query']['id'] == ['Not a valid integer.']
