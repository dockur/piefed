"""
Helper functions for the reports system
"""

from datetime import timedelta

from app import db
from app.models import Post, PostReply, User, Report, CommunityBan
from app.constants import (
    REPORT_STATE_NEW,
    REPORT_STATE_ESCALATED,
    REPORT_STATE_RESOLVED,
)
from app.utils import add_to_modlog, utcnow
from app.post.routes import post_delete_post
from app.shared.reply import mod_remove_reply
from app.constants import SRC_WEB
from app.shared.tasks.blocks import ban_from_site, ban_from_community


def update_content_report_counter(report, decrement=True):
    """
    Update report counter for content associated with report

    Args:
        report: Report instance
        decrement: Whether to decrement (True) or increment (False) the counter
    """
    delta = -1 if decrement else 1

    if report.suspect_post_id:
        Post.query.filter_by(id=report.suspect_post_id).filter(
            Post.reports > 0 if decrement else True
        ).update({Post.reports: Post.reports + delta}, synchronize_session=False)
    elif report.suspect_post_reply_id:
        PostReply.query.filter_by(id=report.suspect_post_reply_id).filter(
            PostReply.reports > 0 if decrement else True
        ).update(
            {PostReply.reports: PostReply.reports + delta},
            synchronize_session=False,
        )
    elif report.suspect_user_id:
        User.query.filter_by(id=report.suspect_user_id).filter(
            User.reports > 0 if decrement else True
        ).update({User.reports: User.reports + delta}, synchronize_session=False)


def update_report_counters_bulk(reports):
    """
    Update report counters for multiple reports efficiently

    Args:
        reports: List of Report instances
    """
    post_ids = {}
    reply_ids = {}
    user_ids = {}

    for report in reports:
        if report.suspect_post_id:
            post_ids[report.suspect_post_id] = (
                post_ids.get(report.suspect_post_id, 0) + 1
            )
        elif report.suspect_post_reply_id:
            reply_ids[report.suspect_post_reply_id] = (
                reply_ids.get(report.suspect_post_reply_id, 0) + 1
            )
        elif report.suspect_user_id:
            user_ids[report.suspect_user_id] = (
                user_ids.get(report.suspect_user_id, 0) + 1
            )

    for post_id, count in post_ids.items():
        Post.query.filter_by(id=post_id).filter(Post.reports >= count).update(
            {Post.reports: Post.reports - count}, synchronize_session=False
        )

    for reply_id, count in reply_ids.items():
        PostReply.query.filter_by(id=reply_id).filter(
            PostReply.reports >= count
        ).update(
            {PostReply.reports: PostReply.reports - count},
            synchronize_session=False,
        )

    for user_id, count in user_ids.items():
        User.query.filter_by(id=user_id).filter(User.reports >= count).update(
            {User.reports: User.reports - count}, synchronize_session=False
        )


def process_user_ban(
    user_id,
    ban_scope,
    duration_hours,
    reason,
    moderator_id,
    community_id=None,
    delete_content=False,
):
    """
    Process a user ban with all necessary steps

    Args:
        user_id: ID of user to ban
        ban_scope: 'site' or 'community'
        duration_hours: Hours to ban (string), '0' for permanent
        reason: Ban reason
        moderator_id: ID of moderator performing ban
        community_id: Community ID for community bans
        delete_content: Whether to delete user's content

    Returns:
        tuple: (success, error_message)
    """
    user = User.query.get(user_id)
    if not user:
        return False, "User not found"

    duration_hours_int = int(duration_hours) if duration_hours != "0" else None
    expiry = (
        utcnow() + timedelta(hours=duration_hours_int) if duration_hours_int else None
    )

    if ban_scope == "site":
        user.banned = True
        if expiry:
            user.ban_until = expiry
        db.session.commit()

        ban_from_site.delay(False, user_id, moderator_id, expiry, reason)
    else:
        if not community_id:
            return False, "Community ID required for community ban"

        community_ban = CommunityBan.query.filter_by(
            user_id=user_id, community_id=community_id
        ).first()

        if not community_ban:
            community_ban = CommunityBan(
                user_id=user_id,
                community_id=community_id,
                banned_by=moderator_id,
                reason=reason,
                ban_until=expiry,
            )
            db.session.add(community_ban)
        else:
            community_ban.banned_by = moderator_id
            community_ban.reason = reason
            if expiry:
                community_ban.ban_until = expiry

        db.session.commit()

        ban_from_community.delay(
            False, user_id, moderator_id, community_id, expiry, reason
        )

    if delete_content:
        delete_user_content(user_id, reason, moderator_id)

    moderator = User.query.get(moderator_id)
    add_to_modlog("ban_user", moderator, target_user=user, reason=reason)

    return True, None


def delete_user_content(user_id, reason, moderator_id):
    """
    Delete all content from a user

    Args:
        user_id: ID of user whose content to delete
        reason: Reason for deletion
        moderator_id: ID of moderator performing deletion
    """
    user = User.query.get(user_id)
    moderator = User.query.get(moderator_id)

    if not user or not moderator:
        return

    posts = Post.query.filter_by(user_id=user_id, deleted=False).all()
    for post in posts:
        post.deleted = True
        post.deleted_by = moderator_id
        add_to_modlog(
            "delete_post",
            moderator,
            target_user=user,
            post=post,
            reason=f"Banned user content removal: {reason}",
        )

    comments = PostReply.query.filter_by(user_id=user_id, deleted=False).all()
    for comment in comments:
        comment.deleted = True
        comment.deleted_by = moderator_id
        add_to_modlog(
            "delete_reply",
            moderator,
            target_user=user,
            reply=comment,
            reason=f"Banned user content removal: {reason}",
        )

    db.session.commit()


def remove_reported_content(report, moderator_id, reason):
    """
    Remove content referenced in a report

    Args:
        report: Report instance
        moderator_id: ID of moderator performing removal
        reason: Removal reason

    Returns:
        tuple: (success, error_message)
    """
    if report.suspect_post_id:
        post = Post.query.get(report.suspect_post_id)
        if post and not post.deleted:
            post_delete_post(post.community, post, moderator_id, reason)
            return True, "Post has been removed."
        else:
            return False, "Post not found or already deleted."

    elif report.suspect_post_reply_id:
        reply = PostReply.query.get(report.suspect_post_reply_id)
        if reply and not reply.deleted:
            mod_remove_reply(reply.id, SRC_WEB, None, reason)
            return True, "Comment has been removed."
        else:
            return False, "Comment not found or already deleted."
    else:
        return False, "No content to remove for this report."


def resolve_related_reports(report):
    """
    Resolve all reports related to the same content

    Args:
        report: Reference report

    Returns:
        int: Number of reports resolved
    """
    related_reports = []

    if report.suspect_post_id:
        related_reports = Report.query.filter(
            Report.id != report.id,
            Report.suspect_post_id == report.suspect_post_id,
            Report.status.in_([REPORT_STATE_NEW, REPORT_STATE_ESCALATED]),
        ).all()
    elif report.suspect_post_reply_id:
        related_reports = Report.query.filter(
            Report.id != report.id,
            Report.suspect_post_reply_id == report.suspect_post_reply_id,
            Report.status.in_([REPORT_STATE_NEW, REPORT_STATE_ESCALATED]),
        ).all()
    elif report.suspect_user_id:
        related_reports = Report.query.filter(
            Report.id != report.id,
            Report.suspect_user_id == report.suspect_user_id,
            Report.status.in_([REPORT_STATE_NEW, REPORT_STATE_ESCALATED]),
        ).all()

    for related_report in related_reports:
        related_report.status = REPORT_STATE_RESOLVED

    db.session.commit()
    return len(related_reports)


def validate_report_filters(request_args):
    """
    Validate and sanitize report filter parameters

    Args:
        request_args: Flask request arguments

    Returns:
        dict: Validated filter parameters
    """
    filters = {}

    page = request_args.get("page", 1, type=int)
    filters["page"] = max(1, page)

    filters["filter_type"] = request_args.get("filter_type", "all")

    status_values = [
        "unresolved",
        "new",
        "escalated",
        "appealed",
        "resolved",
        "dismissed",
        "all",
    ]
    filters["filter_status"] = request_args.get("filter_status", "unresolved")
    if filters["filter_status"] not in status_values:
        filters["filter_status"] = "unresolved"

    filters["sort_by"] = request_args.get("sort_by", "newest")
    if filters["sort_by"] not in ["newest", "oldest"]:
        filters["sort_by"] = "newest"

    filters["local_remote"] = request_args.get("local_remote", "all")
    if filters["local_remote"] not in ["all", "local", "remote"]:
        filters["local_remote"] = "all"

    date_ranges = ["today", "week", "month", "3months", "year", "all"]
    filters["date_range"] = request_args.get("date_range", "week")
    if filters["date_range"] not in date_ranges:
        filters["date_range"] = "week"

    filters["filter_reporter"] = request_args.get("filter_reporter", "").strip()[:100]
    filters["search"] = request_args.get("search", "").strip()[:200]

    filters["filter_community"] = request_args.get("filter_community", "")
    filters["community_ids"] = request_args.get("community_ids", "")

    if filters["community_ids"]:
        try:
            ids = [
                int(id.strip())
                for id in filters["community_ids"].split(",")
                if id.strip()
            ]
            filters["community_ids"] = ",".join(str(id) for id in ids[:50])
        except (ValueError, TypeError):
            filters["community_ids"] = ""

    return filters


def handle_resolve_action(report, data):
    """Handle report resolution"""
    report.status = REPORT_STATE_RESOLVED
    update_content_report_counter(report, decrement=True)
    db.session.commit()

    if data.get("resolve_similar"):
        similar_count = resolve_related_reports(report)
        return True, (
            f"Report and {similar_count} similar reports resolved successfully."
            if similar_count
            else "Report resolved successfully."
        )

    return True, "Report resolved successfully."


def handle_dismiss_action(report, data):
    """Handle report dismissal"""
    from app.constants import REPORT_STATE_DISCARDED

    report.status = REPORT_STATE_DISCARDED
    update_content_report_counter(report, decrement=True)
    db.session.commit()

    return True, "Report dismissed successfully."


def handle_ban_action(report, data, moderator_id):
    """Handle user ban from report"""
    if not report.suspect_user_id:
        return False, "No user to ban for this report."

    success, error = process_user_ban(
        report.suspect_user_id,
        data.get("ban_scope", "community"),
        data.get("ban_duration", "0"),
        data.get("reason", ""),
        moderator_id,
        report.in_community_id,
        data.get("delete_content", False),
    )

    if success:
        report.status = REPORT_STATE_RESOLVED
        db.session.commit()

        if data.get("resolve_similar"):
            resolve_related_reports(report)

    return success, error or "User banned successfully."


def handle_remove_content_action(report, data, moderator_id):
    """Handle content removal from report"""
    success, message = remove_reported_content(
        report, moderator_id, data.get("reason", "")
    )

    if success:
        report.status = REPORT_STATE_RESOLVED
        db.session.commit()

        resolve_related_reports(report)

    return success, message


REPORT_ACTION_HANDLERS = {
    "resolve": handle_resolve_action,
    "dismiss": handle_dismiss_action,
    "ban_user": handle_ban_action,
    "remove_content": handle_remove_content_action,
}
