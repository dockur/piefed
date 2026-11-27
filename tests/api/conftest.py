"""
Fixtures for HTTP-level integration tests of the /api/alpha/* routes.

These tests run the app with a temporary file-based SQLite
database, so no Postgres or Redis is required. Run them with:

    pytest tests/api/

The tests directly under tests/ call API util functions against the development Postgres database and define their
own app fixture, so the two suites should be run separately.
"""
import datetime
import json
import sqlite3
import re

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy import text as sa_text, bindparam
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy_utils.types import TSVectorType

from app import create_app, db
from config import Config
from app.models import Community, Post, PostReply, Instance, User


class ApiTestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    MAIL_SUPPRESS_SEND = True
    CACHE_TYPE = 'SimpleCache'        # in-memory cache: no Redis
    RATELIMIT_ENABLED = False         # no Redis needed for the rate limiter
    ENABLE_ALPHA_API = 'true'
    SERVER_NAME = 'localhost'
    SECRET_KEY = 'test-secret'
    # dummy DSN so the Sentry branches in the error handler are exercised. Tests monkeypatch sentry_sdk.capture_exception, so nothing is sent
    SENTRY_DSN = 'https://example@o0.ingest.sentry.invalid/0'
    SQLALCHEMY_DATABASE_URI = None    # set per-session in the app fixture


def _make_schema_portable():
    """Swap Postgres-only column types for portable ones so db.create_all() works on SQLite.

    Only safe to call when the database URI is SQLite as it mutates the shared metadata.
    """
    assert db.engine.url.get_backend_name() == 'sqlite'
    for table in db.metadata.tables.values():
        for col in table.c:
            t = col.type
            if isinstance(t, postgresql.ARRAY):
                col.type = MutableList.as_mutable(sa.JSON())
            elif isinstance(t, postgresql.UUID):
                col.type = sa.Uuid()
            elif isinstance(t, postgresql.BIT):
                col.type = sa.LargeBinary()
    # sqlalchemy-searchable emits Postgres trigger/function DDL; make it a no-op on SQLite
    compiles(TSVectorType, 'sqlite')(lambda element, compiler, **kw: 'TEXT')
    import sqlalchemy_searchable as sas
    from sqlalchemy_searchable import (CreateSearchFunctionSQL, CreateSearchTriggerSQL,
                                       DropSearchFunctionSQL, DropSearchTriggerSQL)
    for cls in (CreateSearchFunctionSQL, CreateSearchTriggerSQL,
                DropSearchFunctionSQL, DropSearchTriggerSQL):
        compiles(cls, 'sqlite')(lambda element, compiler, **kw: 'SELECT 1')
    event.remove(db.metadata, 'before_create', sas.sql_expressions)
    _register_sqlite_functions()
    _replace_pg_only_queries()
    _patch_expanding_in_bindings()


def _patch_expanding_in_bindings():
    """Several modules use raw text() with 'IN :param' and pass a tuple, which
    psycopg2 expands but SQLite does not. Wrap those modules' text() so the
    bindparam is marked expanding, which renders correctly on both dialects."""

    def make_text(orig_text):
        def text(clause):
            t = sa_text(clause)
            names = re.findall(r'\bIN\s+:(\w+)', clause, re.IGNORECASE)
            if names:
                t = t.bindparams(*[bindparam(n, expanding=True) for n in dict.fromkeys(names)])
            return t
        return text

    for module_name in ('app.api.alpha.utils.post', 'app.api.alpha.views', 'app.utils',
                        'app.tag.routes', 'app.activitypub.util', 'app.topic.routes',
                        'app.cli', 'app.user.routes', 'app.community.util'):
        module = __import__(module_name, fromlist=['x'])
        if hasattr(module, 'text'):
            module.text = make_text(module.text)


class _ArrayAgg:
    """SQLite stand-in for Postgres ARRAY_AGG (returns a JSON string)."""
    def __init__(self):
        self.items = []

    def step(self, value):
        if value is not None and value not in self.items:
            self.items.append(value)

    def finalize(self):
        return json.dumps(self.items)


def _register_sqlite_functions():
    """Register Postgres SQL functions used in raw SQL (now(), ARRAY_AGG) on SQLite."""
    @event.listens_for(Engine, 'connect')
    def _on_connect(dbapi_conn, _record):
        if isinstance(dbapi_conn, sqlite3.Connection):
            dbapi_conn.create_function('now', 0, lambda: datetime.datetime.utcnow().isoformat(sep=' '))
            dbapi_conn.create_aggregate('ARRAY_AGG', 1, _ArrayAgg)


def _replace_pg_only_queries():
    """Replace raw-SQL helpers that use ARRAY_AGG (whose result the app expects as a
    Python list, which SQLite aggregates cannot return) with portable ORM versions.
    Re-bound in the modules that imported them by name."""
    import app.utils as app_utils

    def communities_banned_from_all_users():
        from app.models import Community, CommunityBan, InstanceBan
        result = {}
        for ban in db.session.query(CommunityBan).all():
            result.setdefault(ban.user_id, []).append(ban.community_id)
        for ib in db.session.query(InstanceBan).all():
            for c in db.session.query(Community).filter_by(instance_id=ib.instance_id).all():
                result.setdefault(ib.user_id, []).append(c.id)
        return result

    def moderating_communities_ids_all_users():
        from app.models import Community, CommunityMember
        result = {}
        rows = db.session.query(CommunityMember, Community)\
            .join(Community, Community.id == CommunityMember.community_id)\
            .filter(Community.banned == False, CommunityMember.is_banned == False,
                    sa.or_(CommunityMember.is_moderator == True, CommunityMember.is_owner == True)).all()
        for cm, c in rows:
            result.setdefault(cm.user_id, []).append(c.id)
        return result

    app_utils.communities_banned_from_all_users = communities_banned_from_all_users
    app_utils.moderating_communities_ids_all_users = moderating_communities_ids_all_users
    for module_name in ('app.api.alpha.utils.post', 'app.api.alpha.utils.community', 'app.api.alpha.views'):
        module = __import__(module_name, fromlist=['x'])
        if hasattr(module, 'communities_banned_from_all_users'):
            module.communities_banned_from_all_users = communities_banned_from_all_users
        if hasattr(module, 'moderating_communities_ids_all_users'):
            module.moderating_communities_ids_all_users = moderating_communities_ids_all_users


@pytest.fixture(autouse=True)
def _clear_cache(app):
    """Many utils are @cache.memoize'd; clear between tests so they can't leak state."""
    from app import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(scope='session')
def app(tmp_path_factory):
    db_file = tmp_path_factory.mktemp('api') / 'test.db'
    ApiTestConfig.SQLALCHEMY_DATABASE_URI = f'sqlite:///{db_file}'
    app = create_app(ApiTestConfig)

    # pyfedi.py registers this in production; create_app() does not
    @app.before_request
    def _test_before_request():
        from flask import g, request
        if not request.path.startswith('/static/'):
            from app.models import Site
            g.site = Site.query.get(1)
            g.admin_ids = [1]

    with app.app_context():
        _make_schema_portable()
        db.create_all()
        from app.models import Site, Instance, User, Language
        from app.activitypub.signature import RsaKeys
        db.session.add(Site(name='TestSite', description='test site', private_instance=False))
        db.session.add(Instance(domain='localhost'))
        # Language rows are needed by community creation ('und' is mandatory, id 2 is the default discussion language)
        db.session.add(Language(id=1, code='und', name='Undetermined'))
        db.session.add(Language(id=2, code='en', name='English'))
        # authorise_api_user() rejects users with ap_id set or verified=False;
        # make_community() additionally requires a private_key
        private_key, public_key = RsaKeys.generate_keypair()
        db.session.add(User(user_name='testuser', password_hash='x', instance_id=1, verified=True,
                            private_key=private_key, public_key=public_key))
        db.session.commit()
    return app


@pytest.fixture(scope='session')
def client(app):
    return app.test_client()


@pytest.fixture(scope='session')
def user_id(app):
    from app.models import User
    with app.app_context():
        return User.query.filter_by(user_name='testuser').one().id


@pytest.fixture(scope='session')
def auth_headers(app, user_id):
    from app.models import User
    with app.app_context():
        jwt = User.query.get(user_id).encode_jwt_token()
    return {'Authorization': f'Bearer {jwt}'}


@pytest.fixture
def make_auth_headers(app):
    """Factory: auth headers for any user id (e.g. a non-admin user from make_user)."""
    from app.models import User
    def _make(uid):
        with app.app_context():
            jwt = User.query.get(uid).encode_jwt_token()
        return {'Authorization': f'Bearer {jwt}'}
    return _make


# --- Row factories ---------------------------------------------------------
# Each factory creates a fresh row per call and returns its id, so tests
# never interfere with each other despite sharing the session-scoped DB.
# Unique fields (names, ap_ids) use uuid4 because the DB persists across tests.

import uuid


def _uid():
    return uuid.uuid4().hex[:12]


@pytest.fixture
def make_user(app):
    def _make(**kwargs):
        uid = _uid()
        kwargs.setdefault('user_name', f'user{uid}')
        kwargs.setdefault('verified', True)
        kwargs.setdefault('instance_id', 1)
        kwargs.setdefault('ap_domain', 'localhost')
        with app.app_context():
            u = User(**kwargs)
            db.session.add(u)
            db.session.commit()
            return u.id
    return _make


@pytest.fixture
def make_instance(app):
    def _make(**kwargs):
        uid = _uid()
        kwargs.setdefault('domain', f'instance{uid}.example')
        with app.app_context():
            inst = Instance(**kwargs)
            db.session.add(inst)
            db.session.commit()
            return inst.id
    return _make


@pytest.fixture
def make_community(app, user_id):
    def _make(**kwargs):
        uid = _uid()
        kwargs.setdefault('name', f'comm{uid}')
        kwargs.setdefault('title', f'Community {uid}')
        kwargs.setdefault('ap_id', f'http://localhost/c/comm{uid}')
        kwargs.setdefault('ap_domain', 'localhost')
        kwargs.setdefault('instance_id', 1)
        kwargs.setdefault('user_id', user_id)
        with app.app_context():
            c = Community(**kwargs)
            db.session.add(c)
            db.session.commit()
            return c.id
    return _make


@pytest.fixture
def make_post(app, user_id):
    def _make(community_id, **kwargs):
        uid = _uid()
        kwargs.setdefault('user_id', user_id)
        kwargs.setdefault('community_id', community_id)
        kwargs.setdefault('instance_id', 1)
        kwargs.setdefault('title', f'Post {uid}')
        kwargs.setdefault('body', f'Body of post {uid}')
        kwargs.setdefault('ap_id', f'http://localhost/post/{uid}')
        with app.app_context():
            p = Post(**kwargs)
            db.session.add(p)
            db.session.commit()
            return p.id
    return _make


@pytest.fixture
def make_reply(app, user_id):

    def _make(post_id, community_id, **kwargs):
        uid = _uid()
        kwargs.setdefault('user_id', user_id)
        kwargs.setdefault('post_id', post_id)
        kwargs.setdefault('community_id', community_id)
        kwargs.setdefault('instance_id', 1)
        kwargs.setdefault('body', f'Reply {uid}')
        kwargs.setdefault('ap_id', f'http://localhost/comment/{uid}')
        with app.app_context():
            r = PostReply(**kwargs)
            db.session.add(r)
            db.session.commit()
            return r.id
    return _make
