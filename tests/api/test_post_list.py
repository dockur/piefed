"""HTTP integration tests for GET /api/alpha/post/list."""


def test_post_list_returns_posts(client, auth_headers, make_community, make_post):
    community_id = make_community()
    make_post(community_id)
    make_post(community_id)

    r = client.get('/api/alpha/post/list', query_string={'community_id': community_id},
                   headers=auth_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert 'posts' in body
    assert len(body['posts']) == 2


def test_post_list_empty_community(client, auth_headers, make_community):
    community_id = make_community()
    r = client.get('/api/alpha/post/list', query_string={'community_id': community_id},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['posts'] == []
