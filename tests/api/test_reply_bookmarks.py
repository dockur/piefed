"""HTTP integration tests for PUT /api/alpha/comment/save."""


def test_save_unsave_reply(client, auth_headers, make_community, make_post, make_reply):
    community_id = make_community()
    reply_id = make_reply(make_post(community_id), community_id)

    r = client.put('/api/alpha/comment/save', json={'comment_id': reply_id, 'save': True},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['comment_view']['saved'] is True

    r = client.put('/api/alpha/comment/save', json={'comment_id': reply_id, 'save': False},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['comment_view']['saved'] is False


def test_unsave_not_bookmarked(client, auth_headers, make_community, make_post, make_reply):
    community_id = make_community()
    reply_id = make_reply(make_post(community_id), community_id)
    r = client.put('/api/alpha/comment/save', json={'comment_id': reply_id, 'save': False},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'This comment was not bookmarked.'


def test_save_twice(client, auth_headers, make_community, make_post, make_reply):
    community_id = make_community()
    reply_id = make_reply(make_post(community_id), community_id)
    r = client.put('/api/alpha/comment/save', json={'comment_id': reply_id, 'save': True},
                   headers=auth_headers)
    assert r.status_code == 200
    r = client.put('/api/alpha/comment/save', json={'comment_id': reply_id, 'save': True},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'This comment has already been bookmarked.'


def test_save_deleted_reply(client, auth_headers, make_community, make_post, make_reply):
    # bookmark_reply() has no deleted-guard: saving a deleted comment succeeds
    community_id = make_community()
    reply_id = make_reply(make_post(community_id), community_id, deleted=True)
    r = client.put('/api/alpha/comment/save', json={'comment_id': reply_id, 'save': True},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['comment_view']['saved'] is True


def test_save_reply_on_deleted_post(client, auth_headers, make_community, make_post, make_reply):
    community_id = make_community()
    reply_id = make_reply(make_post(community_id, deleted=True), community_id)
    r = client.put('/api/alpha/comment/save', json={'comment_id': reply_id, 'save': True},
                   headers=auth_headers)
    assert r.status_code == 200
