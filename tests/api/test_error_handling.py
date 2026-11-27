"""Tests for the shared /api/alpha error handler, in particular which failures
are reported to Sentry: response validation errors (server bugs) yes,
request validation errors (client bugs) no.
"""
import sentry_sdk


def test_request_validation_error_not_sent_to_sentry(client, auth_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(sentry_sdk, 'capture_exception', lambda e: calls.append(e))

    r = client.post('/api/alpha/community', headers=auth_headers, json={'title': 'no name'})
    assert r.status_code == 400
    body = r.get_json()
    assert body['message'] == 'Validation failed'
    assert body['status']['json']['name'] == ['Missing data for required field.']
    assert calls == []


def test_response_validation_error_sent_to_sentry(client, monkeypatch):
    calls = []
    monkeypatch.setattr(sentry_sdk, 'capture_exception', lambda e: calls.append(e))
    # force a response schema validation failure: get_site returns data that
    # cannot be loaded into GetSiteResponse
    import app.api.alpha.routes as alpha_routes
    monkeypatch.setattr(alpha_routes, 'get_site', lambda auth: {})

    r = client.get('/api/alpha/site')
    # server-side schema violations are a 500, not a client 400
    assert r.status_code == 500
    body = r.get_json()
    assert body['code'] == 500
    assert body['message'] == 'Response validation failed'
    assert len(calls) == 1


def test_business_logic_error_still_sent_to_sentry(client, auth_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(sentry_sdk, 'capture_exception', lambda e: calls.append(e))

    # subscribing twice raises a server-side exception (not a validation error)
    import app.api.alpha.routes as alpha_routes
    monkeypatch.setattr(alpha_routes, 'put_community_subscribe',
                        lambda auth, data: (_ for _ in ()).throw(Exception('A subscription for this community already existed.')))
    r = client.put('/api/alpha/community/subscribe',
                   headers=auth_headers, json={'community_id': 1, 'subscribe': True})
    assert r.status_code == 400
    assert r.get_json()['message'] == 'A subscription for this community already existed.'
    assert len(calls) == 1
