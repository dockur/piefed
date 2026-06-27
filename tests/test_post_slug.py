import pytest
from unittest.mock import Mock, patch

from app import create_app, db
from app.models import Post, Community
from config import Config


class TestConfig(Config):
    """Test configuration that inherits from the main Config"""
    TESTING = True
    WTF_CSRF_ENABLED = False
    # Disable real email sending during tests
    MAIL_SUPPRESS_SEND = True
    HTTP_PROTOCOL = 'https'


@pytest.fixture
def app():
    """Create and configure a Flask app for testing using the app factory"""
    app = create_app(TestConfig)
    return app


def test_generate_slug_basic(app):
    """Test basic slug generation with no conflicts"""
    with app.app_context():
        # Create a mock community
        community = Mock()
        community.name = "testcommunity"
        community.link.return_value = "testcommunity"
        community.post_url_type = None

        # Create a mock post
        post = Post()
        post.id = 123
        post.title = "This is a Test Post"
        post.slug = None

        # Mock Post.get_by_slug to return None (no conflicts)
        with patch.object(Post, 'get_by_slug', return_value=None):
            post.generate_slug(community)

        # Verify the slug was generated correctly
        assert post.slug is not None
        assert post.slug.startswith("/c/testcommunity/p/123/")
        assert "this-is-a-test-post" in post.slug


def test_generate_slug_does_not_overwrite_existing_slug(app):
    """Test that generate_slug does nothing if slug already exists"""
    with app.app_context():
        community = Mock()
        community.name = "testcommunity"
        community.post_url_type = None

        post = Post()
        post.id = 111
        post.title = "Test Title"
        post.slug = "/existing/custom/slug"

        original_slug = post.slug

        # Call generate_slug - it should not modify the existing slug
        post.generate_slug(community)

        assert post.slug == original_slug


def test_generate_slug_with_empty_string_slug(app):
    """Test that generate_slug works when slug is empty string"""
    with app.app_context():
        community = Mock()
        community.name = "testcommunity"
        community.link.return_value = "testcommunity"
        community.post_url_type = None

        post = Post()
        post.id = 222
        post.title = "New Post"
        post.slug = ""

        with patch.object(Post, 'get_by_slug', return_value=None):
            post.generate_slug(community)

        assert post.slug != ""
        assert post.slug.startswith("/c/testcommunity/p/222/")


def test_generate_slug_with_special_characters(app):
    """Test slug generation with title containing special characters"""
    with app.app_context():
        community = Mock()
        community.name = "testcommunity"
        community.link.return_value = "testcommunity"
        community.post_url_type = None

        post = Post()
        post.id = 333
        post.title = "Hello! How are you? & Welcome #2024"
        post.slug = None

        with patch.object(Post, 'get_by_slug', return_value=None):
            post.generate_slug(community)

        # Special characters should be handled by slugify
        assert post.slug is not None
        assert "/p/333/" in post.slug
        # Verify no special characters remain (except hyphens and slashes in path)
        slug_part = post.slug.split("/p/333/")[1]
        assert all(c.isalnum() or c == "-" for c in slug_part)


def test_generate_slug_fallback_for_emoji_titles(app):
    """Test falling back to /post/post_id format when the post title can't be slugified (like when it is only emoji)"""
    with app.app_context():
        community = Mock()
        community.name = "testcommunity"
        community.post_url_type = None

        post = Post()
        post.id = 314
        post.title = "🥧🥧🥧"
        post.slug = None

        with patch.object(Post, 'get_by_slug', return_value=None):
            post.generate_slug(community)

        # slugify just makes empty string, fall back to old /post/post_id style
        assert post.slug is not None
        assert "/post/314" == post.slug


# Tests for generate_ap_id()


def test_generate_ap_id_basic(app):
    """Test basic AP ID generation with no conflicts"""
    with app.app_context():
        # Create a mock community
        community = Mock()
        community.name = "testcommunity"
        community.post_url_type = None

        # Create a mock post
        post = Post()
        post.id = 123
        post.title = "This is a Test Post"
        post.ap_id = None

        # Mock Post.get_by_ap_id to return None (no conflicts)
        with patch.object(Post, 'get_by_ap_id', return_value=None):
            post.generate_ap_id(community)

        # Verify the AP ID was generated correctly
        server_name = app.config["SERVER_NAME"]
        assert post.ap_id is not None
        assert post.ap_id.startswith("https://")
        assert f"/c/testcommunity@{server_name}/p/123/" in post.ap_id
        assert "this-is-a-test-post" in post.ap_id

        # Verify the slug was also set
        assert post.slug is not None
        assert post.slug.startswith(f"/c/testcommunity@{server_name}/p/123/")
        assert "this-is-a-test-post" in post.slug


def test_generate_ap_id_does_not_overwrite_existing(app):
    """Test that generate_ap_id does nothing if AP ID already exists"""
    with app.app_context():
        community = Mock()
        community.name = "testcommunity"
        community.post_url_type = None

        post = Post()
        post.id = 111
        post.title = "Test Title"
        post.ap_id = "https://remote.instance/post/original-ap-id"

        original_ap_id = post.ap_id

        # Call generate_ap_id - it should not modify the existing AP ID
        # Note: The condition checks for None, empty string, or length == 10
        post.generate_ap_id(community)

        assert post.ap_id == original_ap_id


def test_generate_ap_id_with_empty_string(app):
    """Test that generate_ap_id works when AP ID is empty string"""
    with app.app_context():
        community = Mock()
        community.name = "testcommunity"
        community.post_url_type = None

        post = Post()
        post.id = 222
        post.title = "New Post"
        post.ap_id = ""

        with patch.object(Post, 'get_by_ap_id', return_value=None):
            post.generate_ap_id(community)

        server_name = app.config["SERVER_NAME"]
        assert post.ap_id != ""
        assert post.ap_id.startswith("https://")
        assert f"/c/testcommunity@{server_name}/p/222/" in post.ap_id


def test_generate_ap_id_with_length_ten_string(app):
    """Test that generate_ap_id regenerates when AP ID length is exactly 10"""
    with app.app_context():
        community = Mock()
        community.name = "testcommunity"
        community.post_url_type = None

        post = Post()
        post.id = 333
        post.title = "New Post"
        post.ap_id = "0123456789"  # Exactly 10 characters

        with patch.object(Post, 'get_by_ap_id', return_value=None):
            post.generate_ap_id(community)

        # Should regenerate despite having an AP ID (length == 10 is a special case)
        assert post.ap_id != "0123456789"
        assert post.ap_id.startswith("https://")


def test_generate_ap_id_with_special_characters(app):
    """Test AP ID generation with title containing special characters"""
    with app.app_context():
        community = Mock()
        community.name = "testcommunity"
        community.post_url_type = None

        post = Post()
        post.id = 444
        post.title = "Hello! How are you? & Welcome #2024"
        post.ap_id = None

        with patch.object(Post, 'get_by_ap_id', return_value=None):
            post.generate_ap_id(community)

        # Special characters should be handled by slugify
        assert post.ap_id is not None
        assert "/p/444/" in post.ap_id
        # Verify no special characters remain in the slug part
        ap_id_slug_part = post.ap_id.split("/p/444/")[1]
        assert all(c.isalnum() or c == "-" for c in ap_id_slug_part)


def test_ap_id_fallback_for_emoji_titles(app):
    """Test falling back to /post/post_id format when the post title can't be slugified (like when it is only emoji)"""
    with app.app_context():
        community = Mock()
        community.name = "testcommunity"
        community.post_url_type = None

        post = Post()
        post.id = 314
        post.title = "🥧🥧🥧"
        post.ap_id = None

        with patch.object(Post, 'get_by_slug', return_value=None):
            post.generate_ap_id(community)

        # slugify just makes empty string, fall back to old /post/post_id style
        assert post.ap_id is not None
        assert post.ap_id.endswith("/post/314")
