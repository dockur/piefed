"""HTTP integration tests for PUT /api/alpha/comment/subscribe."""


def test_subscribe_unsubscribe_reply(client, auth_headers, make_community, make_post, make_reply):
    community_id = make_community()
    reply_id = make_reply(make_post(community_id), community_id)

    r = client.put('/api/alpha/comment/subscribe', json={'comment_id': reply_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['comment_view']['activity_alert'] is True

    r = client.put('/api/alpha/comment/subscribe', json={'comment_id': reply_id, 'subscribe': False},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['comment_view']['activity_alert'] is False


def test_unsubscribe_not_subscribed(client, auth_headers, make_community, make_post, make_reply):
    community_id = make_community()
    reply_id = make_reply(make_post(community_id), community_id)
    r = client.put('/api/alpha/comment/subscribe', json={'comment_id': reply_id, 'subscribe': False},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'A subscription for this comment did not exist.'


def test_subscribe_twice(client, auth_headers, make_community, make_post, make_reply):
    community_id = make_community()
    reply_id = make_reply(make_post(community_id), community_id)
    r = client.put('/api/alpha/comment/subscribe', json={'comment_id': reply_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 200
    r = client.put('/api/alpha/comment/subscribe', json={'comment_id': reply_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'A subscription for this comment already existed.'


def test_subscribe_deleted_reply(client, auth_headers, make_community, make_post, make_reply):
    community_id = make_community()
    reply_id = make_reply(make_post(community_id), community_id, deleted=True)
    r = client.put('/api/alpha/comment/subscribe', json={'comment_id': reply_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 400
