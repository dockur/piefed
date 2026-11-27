"""HTTP integration tests for PUT /api/alpha/post/save."""


def test_save_unsave_post(client, auth_headers, make_community, make_post):
    post_id = make_post(make_community())

    r = client.put('/api/alpha/post/save', json={'post_id': post_id, 'save': True},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['post_view']['saved'] is True

    r = client.put('/api/alpha/post/save', json={'post_id': post_id, 'save': False},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['post_view']['saved'] is False


def test_unsave_not_bookmarked(client, auth_headers, make_community, make_post):
    post_id = make_post(make_community())
    r = client.put('/api/alpha/post/save', json={'post_id': post_id, 'save': False},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'This post was not bookmarked.'


def test_save_twice(client, auth_headers, make_community, make_post):
    post_id = make_post(make_community())
    r = client.put('/api/alpha/post/save', json={'post_id': post_id, 'save': True},
                   headers=auth_headers)
    assert r.status_code == 200
    r = client.put('/api/alpha/post/save', json={'post_id': post_id, 'save': True},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'This post has already been bookmarked.'


def test_save_deleted_post(client, auth_headers, make_community, make_post):
    # bookmark_post() has no deleted-guard: saving a deleted post succeeds
    post_id = make_post(make_community(), deleted=True)
    r = client.put('/api/alpha/post/save', json={'post_id': post_id, 'save': True},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['post_view']['saved'] is True
