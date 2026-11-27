"""HTTP integration tests for POST/PUT /api/alpha/community (create/edit).

GET /api/alpha/community is covered in test_get_community.py.
"""


def test_create_community(client, auth_headers):
    r = client.post('/api/alpha/community', headers=auth_headers, json={
        'name': 'My Test Comm',
        'title': 'My Test Community',
        'description': 'a community created by a test',
        'nsfw': False,
    })
    assert r.status_code == 200
    body = r.get_json()
    community = body['community_view']['community']
    assert community['name'] == 'my_test_comm'  # name is slugified
    assert community['title'] == 'My Test Community'

    # and it is fetchable afterwards
    r = client.get('/api/alpha/community', query_string={'id': community['id']})
    assert r.status_code == 200
    assert r.get_json()['community_view']['community']['name'] == 'my_test_comm'


def test_create_community_anonymous_rejected(client):
    r = client.post('/api/alpha/community', json={'name': 'anoncomm', 'title': 'Anon'})
    assert r.status_code == 400
    assert r.get_json()['message'] == 'incorrect_login'


def test_create_community_missing_required_fields(client, auth_headers):
    r = client.post('/api/alpha/community', headers=auth_headers, json={'title': 'No name given'})
    # flask-smorest validation errors are converted to 400 by the shared error handler
    assert r.status_code == 400
    body = r.get_json()
    assert body['message'] == 'Validation failed'
    # status is the structured marshmallow messages dict, keyed by location then field
    assert body['status']['json']['name'] == ['Missing data for required field.']


def test_create_duplicate_name_rejected(client, auth_headers):
    payload = {'name': 'dupecomm', 'title': 'First'}
    r = client.post('/api/alpha/community', headers=auth_headers, json=payload)
    assert r.status_code == 200
    r = client.post('/api/alpha/community', headers=auth_headers, json=payload)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'community with that name already exists'


def test_edit_own_community(client, auth_headers):
    r = client.post('/api/alpha/community', headers=auth_headers,
                    json={'name': 'editme', 'title': 'Before edit'})
    assert r.status_code == 200
    community_id = r.get_json()['community_view']['community']['id']

    r = client.put('/api/alpha/community', headers=auth_headers, json={
        'community_id': community_id,
        'title': 'After edit',
        'description': 'updated description',
        'nsfw': True,
    })
    assert r.status_code == 200
    community = r.get_json()['community_view']['community']
    assert community['title'] == 'After edit'
    assert community['nsfw'] is True

    # persisted, not just echoed back
    r = client.get('/api/alpha/community', query_string={'id': community_id})
    assert r.status_code == 200
    community = r.get_json()['community_view']['community']
    assert community['title'] == 'After edit'
    assert community['description'] == 'updated description'


def test_edit_community_not_owner_rejected(client, auth_headers, make_auth_headers, make_user, make_community):
    community_id = make_community()  # owned by user 1 (the auth_headers user)
    other_user = make_user()

    r = client.put('/api/alpha/community', headers=make_auth_headers(other_user),
                   json={'community_id': community_id, 'title': 'Hijacked'})
    assert r.status_code == 400
    assert r.get_json()['message'] == 'incorrect_login'


def test_edit_nonexistent_community(client, auth_headers):
    r = client.put('/api/alpha/community', headers=auth_headers,
                   json={'community_id': 999999, 'title': 'Ghost'})
    assert r.status_code == 400


def test_edit_missing_community_id(client, auth_headers):
    r = client.put('/api/alpha/community', headers=auth_headers, json={'title': 'No id'})
    assert r.status_code == 400
    body = r.get_json()
    assert body['message'] == 'Validation failed'
    assert body['status']['json']['community_id'] == ['Missing data for required field.']
