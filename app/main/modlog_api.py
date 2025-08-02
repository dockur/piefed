"""
Modlog routes and API endpoints
Separated to minimize changes to main routes.py
"""

from flask import request, jsonify
from flask_login import current_user, login_required
from sqlalchemy import or_
import arrow

from app.models import ModLog, Post, PostReply, Community
from app.main import bp


@bp.route("/api/modlog/<int:modlog_id>/removed-content")
def modlog_removed_content(modlog_id):
    """API endpoint to fetch removed content for modlog entries"""
    # Get the modlog entry
    modlog_entry = ModLog.query.get_or_404(modlog_id)

    # Check permissions - admins/staff can see all, others only public entries
    can_see_details = False
    if current_user.is_authenticated:
        if current_user.is_admin() or current_user.is_staff():
            can_see_details = True
        elif modlog_entry.public:
            can_see_details = True
    else:
        # Anonymous users can only see public entries
        if modlog_entry.public:
            can_see_details = True

    if not can_see_details:
        return jsonify({"error": "Unauthorized"}), 403

    # Fetch the removed content based on action type
    if modlog_entry.action == "delete_post":
        post_id = modlog_entry.post_id

        # If post_id is not set, try to extract it from the link
        if not post_id and modlog_entry.link and modlog_entry.link.startswith("post/"):
            try:
                post_id = int(modlog_entry.link.split("/")[-1])
            except (ValueError, IndexError):
                pass

        if post_id:
            # Try to fetch the deleted post
            post = Post.query.get(post_id)
            if post:
                return jsonify(
                    {
                        "type": "post",
                        "title": post.title,
                        "body": post.body,
                        "url": post.url,
                        "author": (post.author.user_name if post.author else None),
                        "created_at": (
                            arrow.get(post.created_at).format("MMM DD, YYYY HH:mm")
                            if post.created_at
                            else None
                        ),
                    }
                )

    elif modlog_entry.action == "delete_post_reply":
        reply_id = modlog_entry.reply_id

        # If reply_id is not set, try to extract it from the link
        if not reply_id and modlog_entry.link and "comment/" in modlog_entry.link:
            try:
                reply_id = int(modlog_entry.link.split("comment/")[-1])
            except (ValueError, IndexError):
                pass

        if reply_id:
            # Try to fetch the deleted comment
            comment = PostReply.query.get(reply_id)
            if comment:
                parent_context = None
                if comment.parent_id:
                    parent = PostReply.query.get(comment.parent_id)
                    if parent:
                        parent_context = (
                            parent.body[:200] + "..."
                            if len(parent.body) > 200
                            else parent.body
                        )

                return jsonify(
                    {
                        "type": "comment",
                        "body": comment.body,
                        "author": (
                            comment.author.user_name if comment.author else None
                        ),
                        "created_at": (
                            arrow.get(comment.created_at).format("MMM DD, YYYY HH:mm")
                            if comment.created_at
                            else None
                        ),
                        "parent_context": parent_context,
                    }
                )

    # If we couldn't find the content
    return jsonify({"error": "Content not found"}), 404


@bp.route("/api/search/communities")
@login_required
def api_search_communities():
    """API endpoint to search for communities"""
    query = request.args.get("q", "").strip()
    limit = request.args.get("limit", 10, type=int)

    if not query or len(query) < 2:
        return jsonify([])

    # Search for communities
    search_term = f"%{query}%"
    communities = (
        Community.query.filter(
            or_(
                Community.name.ilike(search_term),
                Community.title.ilike(search_term),
            )
        )
        .order_by(Community.subscriptions_count.desc())
        .limit(limit)
        .all()
    )

    # Format response
    result = []
    for community in communities:
        result.append(
            {
                "id": community.id,
                "name": community.name,
                "display_name": community.title or community.name,
                "instance": (
                    community.instance.domain if community.instance_id != 1 else None
                ),
                "subscribers": community.subscriptions_count,
            }
        )

    return jsonify(result)


@bp.route("/api/communities/details", methods=["POST"])
@login_required
def api_communities_details():
    """API endpoint to get details for multiple communities by ID"""
    data = request.get_json()
    if not data or "ids" not in data:
        return jsonify([])

    community_ids = data["ids"]
    if not isinstance(community_ids, list):
        return jsonify([])

    # Clean and validate IDs
    valid_ids = []
    for cid in community_ids:
        try:
            valid_ids.append(int(cid))
        except (ValueError, TypeError):
            continue

    if not valid_ids:
        return jsonify([])

    # Fetch communities
    communities = Community.query.filter(Community.id.in_(valid_ids)).all()

    # Format response
    result = []
    for community in communities:
        result.append(
            {
                "id": community.id,
                "name": community.name,
                "display_name": community.title or community.name,
                "instance": (
                    community.instance.domain if community.instance_id != 1 else None
                ),
                "subscribers": community.subscriptions_count,
            }
        )

    return jsonify(result)
