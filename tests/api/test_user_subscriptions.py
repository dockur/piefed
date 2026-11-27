"""HTTP integration tests for PUT /api/alpha/user/subscribe."""
from app import db


def test_subscribe_unsubscribe_user(client, auth_headers, make_user):
    person_id = make_user()

    r = client.put('/api/alpha/user/subscribe', json={'person_id': person_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['person_view']['activity_alert'] is True
    assert r.get_json()['subscribed'] is True

    r = client.put('/api/alpha/user/subscribe', json={'person_id': person_id, 'subscribe': False},
                   headers=auth_headers)
    assert r.status_code == 200
    # 'subscribed' is set directly from the request; 'activity_alert' comes from a
    # memoized user_view (600s) and can be stale right after unsubscribing
    assert r.get_json()['subscribed'] is False


def test_unsubscribe_not_subscribed(client, auth_headers, make_user):
    person_id = make_user()
    r = client.put('/api/alpha/user/subscribe', json={'person_id': person_id, 'subscribe': False},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'A subscription for this user did not exist.'


def test_subscribe_twice(client, auth_headers, make_user):
    person_id = make_user()
    r = client.put('/api/alpha/user/subscribe', json={'person_id': person_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 200
    r = client.put('/api/alpha/user/subscribe', json={'person_id': person_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'A subscription for this user already existed.'


def test_subscribe_banned_user(client, auth_headers, make_user):
    person_id = make_user(banned=True)
    r = client.put('/api/alpha/user/subscribe', json={'person_id': person_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 400


def test_subscribe_self(client, auth_headers, user_id):
    r = client.put('/api/alpha/user/subscribe', json={'person_id': user_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 400


def test_subscribe_user_who_blocked_you(client, app, auth_headers, user_id, make_user):
    from app.models import UserBlock
    person_id = make_user()
    with app.app_context():
        db.session.add(UserBlock(blocker_id=person_id, blocked_id=user_id))
        db.session.commit()

    r = client.put('/api/alpha/user/subscribe', json={'person_id': person_id, 'subscribe': True},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.get_json()['message'] == 'This user has blocked you.'
