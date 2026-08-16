import pytest
from flask import g
from sqlalchemy import desc

from app import create_app
from app.models import User, Community
from config import Config


class TestConfig(Config):
    """Test configuration that inherits from the main Config"""
    TESTING = True
    WTF_CSRF_ENABLED = False
    # Disable real email sending during tests
    MAIL_SUPPRESS_SEND = True


@pytest.fixture
def app():
    """Create and configure a Flask app for testing using the app factory"""
    app = create_app(TestConfig)
    return app


def test_api_post_list(app):
    with app.app_context():
        from app.api.alpha.utils.site import post_site_block
        from app.api.alpha.utils.post import get_post_list

        user_id = 1
        user = User.query.get(user_id)
        assert user is not None and hasattr(user, 'id')
        jwt = user.encode_jwt_token()
        assert jwt is not None
        auth = f'Bearer {jwt}'

        high_post_community = Community.query.filter(Community.instance_id != 1).order_by(
            desc(Community.post_count)).first()
        assert high_post_community is not None and hasattr(high_post_community, 'id')

        # post list should be more than 0
        g.admin_ids = [1]
        data = {"community_id": high_post_community.id}
        response = get_post_list(auth, data)
        assert 'posts' in response and len(response['posts']) > 0


