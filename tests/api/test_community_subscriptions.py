"""HTTP integration tests for PUT /api/alpha/community/subscribe."""
from app import db


def test_subscribe_unsubscribe(client, auth_headers, make_community):
    community_id = make_community()

    r = client.put('/api/alpha/community/subscribe',
                   json={'community_id': community_id, 'subscribe': True}, headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['community_view']['activity_alert'] is True

    r = client.put('/api/alpha/community/subscribe',
                   json={'community_id': community_id, 'subscribe': False}, headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['community_view']['activity_alert'] is False


def test_unsubscribe_not_subscribed(client, auth_headers, make_community):
    community_id = make_community()
    r = client.put('/api/alpha/community/subscribe',
                   json={'community_id': community_id, 'subscribe': False}, headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'A subscription for this community did not exist.'


def test_subscribe_twice(client, auth_headers, make_community):
    community_id = make_community()
    r = client.put('/api/alpha/community/subscribe',
                   json={'community_id': community_id, 'subscribe': True}, headers=auth_headers)
    assert r.status_code == 200
    r = client.put('/api/alpha/community/subscribe',
                   json={'community_id': community_id, 'subscribe': True}, headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'A subscription for this community already existed.'


def test_subscribe_banned_community(client, auth_headers, make_community):
    community_id = make_community(banned=True)
    r = client.put('/api/alpha/community/subscribe',
                   json={'community_id': community_id, 'subscribe': True}, headers=auth_headers)
    assert r.status_code == 400


def test_subscribe_community_user_is_banned_from(client, app, auth_headers, user_id, make_community):
    from app.models import CommunityBan
    community_id = make_community()
    with app.app_context():
        db.session.add(CommunityBan(user_id=user_id, community_id=community_id, banned_by=user_id))
        db.session.commit()

    r = client.put('/api/alpha/community/subscribe',
                   json={'community_id': community_id, 'subscribe': True}, headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'You are banned from this community.'
