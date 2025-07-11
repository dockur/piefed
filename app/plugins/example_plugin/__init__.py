from flask import current_app

from app.plugins.hooks import hook


@hook("before_post_create")
def example_before_post_creation(post_data):
    """Debug hook that prints when a post is about to be created"""
    if current_app.debug:
        print(f"[PLUGIN DEBUG] About to create post: {post_data.get('title', 'No title')}")
        print(f"[PLUGIN DEBUG] Post content preview: {post_data.get('content', '')[:50]}...")
    return post_data


@hook("after_post_create")
def example_after_post_creation(post_data):
    """Hook that runs after a post is created"""
    if current_app.debug:
        print(f"[PLUGIN DEBUG] Post created successfully: {post_data.get('title', 'No title')}")
    return post_data


def plugin_info():
    """Plugin metadata"""
    return {
        "name": "Example Plugin",
        "version": "1.0.0",
        "description": "A simple example plugin that demonstrates hook usage",
        "license": "AGPL-3.0",                      # Must be AGPL-compatible
        "source_url": "https://github.com/...",     # Required
        "author": "PieFed Team"
    }