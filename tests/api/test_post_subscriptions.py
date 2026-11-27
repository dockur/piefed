"""HTTP integration tests for PUT /api/alpha/post/subscribe."""


def test_subscribe_unsubscribe_post(client, auth_headers, make_community, make_post):
    post_id = make_post(make_community())

    r = client.put('/api/alpha/post/subscribe', json={'post_id': post_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['post_view']['activity_alert'] is True

    r = client.put('/api/alpha/post/subscribe', json={'post_id': post_id, 'subscribe': False},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['post_view']['activity_alert'] is False


def test_unsubscribe_not_subscribed(client, auth_headers, make_community, make_post):
    post_id = make_post(make_community())
    r = client.put('/api/alpha/post/subscribe', json={'post_id': post_id, 'subscribe': False},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'A subscription for this post did not exist.'


def test_subscribe_twice(client, auth_headers, make_community, make_post):
    post_id = make_post(make_community())
    r = client.put('/api/alpha/post/subscribe', json={'post_id': post_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 200
    r = client.put('/api/alpha/post/subscribe', json={'post_id': post_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'A subscription for this post already existed.'


def test_subscribe_deleted_post(client, auth_headers, make_community, make_post):
    post_id = make_post(make_community(), deleted=True)
    r = client.put('/api/alpha/post/subscribe', json={'post_id': post_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 400
