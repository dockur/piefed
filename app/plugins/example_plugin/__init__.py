import os

from app.plugins.hooks import hook


@hook("before_post_create")
def example_before_post_creation(post_data):
    """Debug hook that prints when a post is about to be created"""
    if int(os.environ.get('FLASK_DEBUG', '0')):
        print(f"[PLUGIN DEBUG] About to create post: {post_data.get('title', 'No title')}")
        print(f"[PLUGIN DEBUG] Post content preview: {post_data.get('content', '')[:50]}...")
    return post_data


@hook("after_post_create")
def example_after_post_creation(post_data):
    """Hook that runs after a post is created"""
    if int(os.environ.get('FLASK_DEBUG', '0')) and post_data:
        if hasattr(post_data, "title"):
            print(f"[PLUGIN DEBUG] Post created successfully: {post_data.title}")
    return post_data


@hook("new_user")
def example_new_user(user):
    """Hook that runs after a new user is registered/created and verified"""
    if int(os.environ.get('FLASK_DEBUG', '0')):
        print(f"[PLUGIN DEBUG] New user is verified: {user.user_name}")
    return user


@hook("new_registration_for_approval")
def example_new_registration_for_approval(application):
    """Hook that runs when a new user registration requires approval"""
    if int(os.environ.get('FLASK_DEBUG', '0')):
        print(f"[PLUGIN DEBUG] New user application for user {application.user.user_name}")
    return application


@hook("new_remote_community")
def example_new_remote_community(community):
    """Hook that runs when a new remote community is added to the instance"""
    if int(os.environ.get('FLASK_DEBUG', '0')):
        print(f"[PLUGIN DEBUG] Remote community of {community.lemmy_link()} has been added")
    return community


@hook("new_local_community")
def example_new_local_community(community):
    """Hook that runs when a new local community is added to the instance"""
    if int(os.environ.get('FLASK_DEBUG', '0')):
        print(f"[PLUGIN DEBUG] Local community of {community.lemmy_link()} has been added")
    return community


@hook("webhook")
def example_webhook(data):
    """Hook that runs whenever a webhook is received by the instance"""
    if int(os.environ.get('FLASK_DEBUG', '0')):
        print(f"[PLUGIN DEBUG] Data received by webhook: {data}")
    return data

@hook("new_modlog_item")
def example_new_modlog_item(modlog_item):
    """Hook that runs when a new item is added to the modlog."""
    if int(os.environ.get('FLASK_DEBUG', '0')):
        print(f"[PLUGIN DEBUG] New modlog item: {modlog_item.type} {modlog_item.action_to_str().lower()}")
    return modlog_item


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