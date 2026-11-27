"""HTTP integration tests for POST /api/alpha/site/block and its effect on /post/list."""


def test_cannot_block_local_instance(client, auth_headers):
    r = client.post('/api/alpha/site/block', json={'instance_id': 1, 'block': True},
                    headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'You cannot block the local instance.'


def test_block_instance_hides_its_posts(client, auth_headers, make_instance, make_community, make_post):
    remote_instance_id = make_instance()
    community_id = make_community(name='remotecomm', instance_id=remote_instance_id)
    make_post(community_id, instance_id=remote_instance_id)

    def post_count():
        r = client.get('/api/alpha/post/list', query_string={'community_id': community_id},
                       headers=auth_headers)
        assert r.status_code == 200
        return len(r.get_json()['posts'])

    assert post_count() > 0

    r = client.post('/api/alpha/site/block', json={'instance_id': remote_instance_id, 'block': True},
                    headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['blocked'] is True
    assert post_count() == 0

    r = client.post('/api/alpha/site/block', json={'instance_id': remote_instance_id, 'block': False},
                    headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['blocked'] is False
    assert post_count() > 0
