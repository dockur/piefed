from typing import List
from urllib.parse import urlparse
from datetime import datetime
import gzip
import orjson

from flask import current_app
from flask_login import current_user
from sqlalchemy import desc, asc, text, or_

from app import db, cache
from app.constants import POST_TYPE_LINK, POST_TYPE_IMAGE, POST_TYPE_VIDEO, POST_TYPE_POLL
from app.models import PostReply, Post, Community, User, Language, utcnow
from app.utils import blocked_or_banned_instances, blocked_users, is_video_hosting_site, get_request


@cache.memoize(timeout=600)
def retrieve_archived_post(archived_url: str) -> dict:
    """Load archived post data from S3 or local disk"""
    if not archived_url:
        return None
        
    try:
        if archived_url.startswith('http'):
            # Load from S3 via HTTP
            response = get_request(archived_url)
            if response.status_code == 200:
                # Try to decompress, but if it fails (i.e., Cloudflare already decompressed it during transit),
                # use the content directly
                try:
                    data = gzip.decompress(response.content)
                except gzip.BadGzipFile:
                    # Content was already decompressed by CDN/proxy
                    data = response.content
                return orjson.loads(data)
        else:
            # Load from local disk
            with gzip.open(archived_url, 'rb') as f:
                data = f.read()
                return orjson.loads(data)
    except Exception as e:
        current_app.logger.error(f"Failed to load archived data from {archived_url}: {e}")
        return None
    
    return None


def convert_archived_replies_to_tree(archived_replies: list, post: Post) -> List[dict]:
    """Convert archived reply data back to the expected tree format using PostReply models"""
    if not archived_replies:
        return []
    
    def create_real_reply(reply_data):
        # Create a PostReply instance (not persisted to DB)
        post_reply = PostReply()
        post_reply.id = reply_data.get('id')
        post_reply.body = reply_data.get('body', '')
        post_reply.body_html = reply_data.get('body_html', '')
        # Parse datetime strings back to datetime objects
        if reply_data.get('posted_at'):
            post_reply.posted_at = datetime.fromisoformat(reply_data['posted_at'])
        if reply_data.get('edited_at'):
            post_reply.edited_at = datetime.fromisoformat(reply_data['edited_at'])
        post_reply.score = reply_data.get('score', 0)
        post_reply.ranking = reply_data.get('ranking', 0)
        post_reply.parent_id = reply_data.get('parent_id')
        post_reply.distinguished = reply_data.get('distinguished', False)
        post_reply.deleted = reply_data.get('deleted', False)
        post_reply.deleted_by = reply_data.get('deleted_by')
        post_reply.user_id = reply_data.get('user_id')
        post_reply.post_id = post.id
        post_reply.depth = reply_data.get('depth', 0)
        post_reply.language_id = reply_data.get('language_id')
        post_reply.replies_enabled = False
        post_reply.community_id = reply_data.get('community_id')
        post_reply.up_votes = reply_data.get('up_votes', 0)
        post_reply.down_votes = reply_data.get('down_votes', 0)
        post_reply.child_count = reply_data.get('child_count', 0)
        post_reply.path = reply_data.get('path', [])
        post_reply.reports = 0
        
        # Post relationship
        post_reply.post = post
        
        # Community relationship
        post_reply.community = post.community
        
        # Author from archived data (or fetch if we have user_id)
        #if reply_data.get('user_id'):
        #    post_reply.author = User.query.get(reply_data['user_id'])

        # Create a minimal User instance for display
        author = User()
        author.id = reply_data.get('author_id')
        author.title = reply_data.get('author_name', 'Unknown')
        author.indexable = reply_data.get('author_indexable', True)
        author.ap_id = reply_data.get('author_ap_id')
        author.ap_profile_id = reply_data.get('author_ap_profile_id')
        author.user_name = reply_data.get('author_user_name')
        author.deleted = reply_data.get('author_deleted')
        author.bot = reply_data.get('author_bot', False)
        author.banned = reply_data.get('author_banned', False)
        if reply_data.get('author_created'):
            author.created = datetime.fromisoformat(reply_data['author_created'])
        else:
            author.created = utcnow()
        author.instance_id = 1
        author.ap_domain = reply_data.get('author_ap_domain', '')
        author.reputation = reply_data.get('author_reputation', 1)

        post_reply.author = author
        
        # Language
        if reply_data.get('language_id'):
            post_reply.language = Language.query.get(reply_data['language_id'])
        
        return {
            'comment': post_reply,
            'replies': [create_real_reply(child) for child in reply_data.get('replies', [])]
        }
    
    return [create_real_reply(reply) for reply in archived_replies]


def find_comment_branch_in_archived(archived_replies: list, comment_id: int) -> list:
    """Find a specific comment branch in archived data"""
    def search_tree(replies, target_id):
        for reply_data in replies:
            if reply_data.get('id') == target_id:
                return [reply_data]  # Found the target comment
            # Search in child replies
            found = search_tree(reply_data.get('replies', []), target_id)
            if found:
                return found
        return []
    
    return search_tree(archived_replies, comment_id)


# replies to a post, in a tree, sorted by a variety of methods
def post_replies(post: Post, sort_by: str, viewer: User, db_only=False) -> List[PostReply]:
    # If post is archived, load from archived data
    if post.archived and db_only is False:
        archived_data = retrieve_archived_post(post.archived)
        if archived_data and 'replies' in archived_data:
            archived_replies = convert_archived_replies_to_tree(archived_data['replies'], post)
            return archived_replies
    
    comments = db.session.query(PostReply).filter_by(post_id=post.id)
    if viewer:
        instance_ids = blocked_or_banned_instances(viewer.id)
        if instance_ids:
            comments = comments.filter(or_(PostReply.instance_id.not_in(instance_ids), PostReply.instance_id == None))
        if viewer.ignore_bots == 1:
            comments = comments.filter(PostReply.from_bot == False)
        blocked_accounts = blocked_users(viewer.id)
        if blocked_accounts:
            comments = comments.filter(PostReply.user_id.not_in(blocked_accounts))
        if viewer.reply_hide_threshold and not (viewer.is_admin_or_staff() or post.community.is_moderator()):
            comments = comments.filter(PostReply.score > viewer.reply_hide_threshold)
        if viewer.read_language_ids and len(viewer.read_language_ids) > 0:
            comments = comments.filter(
                or_(PostReply.language_id.in_(tuple(viewer.read_language_ids)), PostReply.language_id == None))
    else:
        comments.filter(PostReply.score > -20)

    if sort_by == 'hot':
        comments = comments.order_by(desc(PostReply.ranking))
    elif sort_by == 'top':
        comments = comments.order_by(desc(PostReply.score))
    elif sort_by == 'new':
        comments = comments.order_by(desc(PostReply.posted_at))
    elif sort_by == 'old':
        comments = comments.order_by(asc(PostReply.posted_at))

    comments = comments.limit(2000)  # paginating indented replies is too hard so just get the first 2000.

    comments_dict = {comment.id: {'comment': comment, 'replies': []} for comment in comments.all()}

    for comment in comments:
        if comment.parent_id is not None:
            parent_comment = comments_dict.get(comment.parent_id)
            if parent_comment:
                parent_comment['replies'].append(comments_dict[comment.id])

    return [comment for comment in comments_dict.values() if comment['comment'].parent_id is None]


def get_comment_branch(post: Post, comment_id: int, sort_by: str, viewer: User) -> List[PostReply]:
    # If post is archived, load from archived data
    if post.archived:
        archived_data = retrieve_archived_post(post.archived)
        if archived_data and 'replies' in archived_data:
            branch_data = find_comment_branch_in_archived(archived_data['replies'], comment_id)
            if branch_data:
                return convert_archived_replies_to_tree(branch_data, post)
            else:
                return []
    
    # Fetch the specified parent comment and its replies
    parent_comment = PostReply.query.get(comment_id)
    if parent_comment is None:
        return []

    comments = PostReply.query.filter(PostReply.post_id == post.id)
    if viewer:
        instance_ids = blocked_or_banned_instances(viewer.id)
        if instance_ids:
            comments = comments.filter(or_(PostReply.instance_id.not_in(instance_ids), PostReply.instance_id == None))
        if viewer.ignore_bots == 1:
            comments = comments.filter(PostReply.from_bot == False)
        blocked_accounts = blocked_users(viewer.id)
        if blocked_accounts:
            comments = comments.filter(PostReply.user_id.not_in(blocked_accounts))
        if viewer.reply_hide_threshold and not (viewer.is_admin_or_staff() or post.community.is_moderator()):
            comments = comments.filter(PostReply.score > viewer.reply_hide_threshold)
        if viewer.read_language_ids and len(viewer.read_language_ids) > 0:
            comments = comments.filter(
                or_(PostReply.language_id.in_(tuple(viewer.read_language_ids)), PostReply.language_id == None))
    else:
        comments.filter(PostReply.score > -20)

    if sort_by == 'hot':
        comments = comments.order_by(desc(PostReply.ranking))
    elif sort_by == 'top':
        comments = comments.order_by(desc(PostReply.score))
    elif sort_by == 'new':
        comments = comments.order_by(desc(PostReply.posted_at))
    elif sort_by == 'old':
        comments = comments.order_by(asc(PostReply.posted_at))

    comments_dict = {comment.id: {'comment': comment, 'replies': []} for comment in comments.all()}

    for comment in comments:
        if comment.parent_id is not None:
            parent_comment = comments_dict.get(comment.parent_id)
            if parent_comment:
                parent_comment['replies'].append(comments_dict[comment.id])

    return [comment for comment in comments_dict.values() if comment['comment'].id == comment_id]


# The number of replies a post has
def get_post_reply_count(post_id) -> int:
    return db.session.execute(
        text('SELECT COUNT(*) as c FROM "post_reply" WHERE post_id = :post_id AND deleted is false'),
        {'post_id': post_id}).scalar()


def tags_to_string(post: Post) -> str:
    if len(post.tags) > 0:
        return ', '.join([tag.display_as for tag in post.tags])


def flair_to_string(post: Post) -> str:
    if len(post.flair) > 0:
        return ', '.join([flair.flair for flair in post.flair])     # flair flair flair flair flfl flfl flflflfl


def body_has_no_archive_link(body):
    if body:
        return 'https://archive.' not in body and 'https://12ft.io' not in body
    else:
        return True


def url_needs_archive(url) -> bool:
    paywalled_sites = ['washingtonpost.com', 'nytimes.com', 'wsj.com', 'economist.com', 'ft.com', 'telegraph.co.uk',
                       'bild.de', 'theatlantic.com', 'lemonde.fr', 'nzherald.co.nz', 'theverge.com', 'reuters.com',
                       'hilltimes.com']
    if url:
        try:
            parsed_url = urlparse(url.replace('www.', ''))
            hostname = parsed_url.hostname.lower()
        except:
            return False
        if hostname == 'nytimes.com' and 'unlocked_article_code' in url:
            return False
        if hostname == 'theatlantic.com' and 'gift' in url:
            return False
        return hostname in paywalled_sites
    else:
        return False


def generate_archive_link(url) -> bool:
    return 'https://archive.ph/' + url


# Forms like the cross post form need the type for the url
def post_type_to_form_url_type(post_type: int, post_url: str):
    if post_type == POST_TYPE_LINK or is_video_hosting_site(post_url):
        return 'link'
    elif post_type == POST_TYPE_IMAGE:
        return 'image'
    elif post_type == POST_TYPE_VIDEO:
        return 'video'
    elif post_type == POST_TYPE_POLL:
        return 'poll'
    else:
        return ''
