"""HTTP integration tests for GET /api/alpha/site and /api/alpha/site/version."""


def test_get_site_anonymous(client):
    r = client.get('/api/alpha/site')
    assert r.status_code == 200
    body = r.get_json()
    assert body['site']['name'] == 'TestSite'
    assert 'version' in body
    assert 'my_user' not in body


def test_get_site_authenticated(client, auth_headers):
    r = client.get('/api/alpha/site', headers=auth_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert 'my_user' in body
    assert body['my_user']['local_user_view']['person']['user_name'] == 'testuser'


def test_get_site_version(client):
    r = client.get('/api/alpha/site/version')
    assert r.status_code == 200
    assert 'version' in r.get_json()


def test_user_me_anonymous_rejected(client):
    r = client.get('/api/alpha/user/me')
    assert r.status_code == 400
    body = r.get_json()
    # shared error envelope used by all /api/alpha routes
    assert body['code'] == 400
    assert 'message' in body and 'status' in body


def test_user_me_authenticated(client, auth_headers):
    r = client.get('/api/alpha/user/me', headers=auth_headers)
    assert r.status_code == 200
    assert 'local_user_view' in r.get_json()


def test_unknown_route_404(client):
    r = client.get('/api/alpha/does_not_exist')
    assert r.status_code == 404


def test_missing_params_error_envelope(client):
    # /community requires id or name; with neither the route must fail with
    # the standard JSON error shape, not an HTML error page
    r = client.get('/api/alpha/community')
    assert r.status_code == 400
    assert r.is_json
    body = r.get_json()
    assert body['code'] == 400
    assert 'message' in body and 'status' in body
