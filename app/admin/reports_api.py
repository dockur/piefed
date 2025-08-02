from datetime import timedelta
from flask import jsonify, render_template, request, make_response
from flask_login import current_user, login_required
from flask_babel import _, get_locale, ngettext
from sqlalchemy import or_
import arrow

from app import db
from app.admin import bp
from app.utils import permission_required, moderating_communities_ids
from app.models import (
    User,
    Report,
    Post,
    PostReply,
    Community,
    Notification,
    Instance,
    utcnow,
)
from app.constants import (
    REPORT_STATE_NEW,
    REPORT_STATE_ESCALATED,
    REPORT_STATE_APPEALED,
    REPORT_STATE_RESOLVED,
    REPORT_STATE_DISCARDED,
)
from app.admin.reports_forms import AdminReportActionForm
from app.admin.reports_helpers import (
    update_report_counters_bulk,
    process_user_ban,
    remove_reported_content,
    validate_report_filters,
    REPORT_ACTION_HANDLERS,
)


def get_reports_data(request_args, moderator_community_ids):
    """Build reports query based on filters"""
    filters = validate_report_filters(request_args)

    page = filters["page"]
    filter_type = filters["filter_type"]
    filter_status = filters["filter_status"]
    filter_reporter = filters["filter_reporter"]
    filter_community = filters["filter_community"]
    community_ids = filters["community_ids"]
    sort_by = filters["sort_by"]
    search = filters["search"]
    local_remote = filters["local_remote"]
    date_range = filters["date_range"]

    reports = Report.query

    if not current_user.is_admin() and moderator_community_ids:
        reports = reports.filter(Report.in_community_id.in_(moderator_community_ids))

    if filter_type and filter_type != "all":
        reports = reports.filter(Report.type == int(filter_type))

    if filter_status == "unresolved":
        reports = reports.filter(
            Report.status.in_(
                [
                    REPORT_STATE_NEW,
                    REPORT_STATE_ESCALATED,
                    REPORT_STATE_APPEALED,
                ]
            )
        )
    elif filter_status == "new":
        reports = reports.filter(Report.status == REPORT_STATE_NEW)
    elif filter_status == "escalated":
        reports = reports.filter(Report.status == REPORT_STATE_ESCALATED)
    elif filter_status == "appealed":
        reports = reports.filter(Report.status == REPORT_STATE_APPEALED)
    elif filter_status == "resolved":
        reports = reports.filter(Report.status == REPORT_STATE_RESOLVED)
    elif filter_status == "dismissed":
        reports = reports.filter(Report.status == REPORT_STATE_DISCARDED)
    # elif filter_status == 'all': no filter, show all statuses

    if search:
        reports = reports.filter(
            or_(
                Report.description.ilike(f"%{search}%"),
                Report.reasons.ilike(f"%{search}%"),
            )
        )

    if filter_reporter:
        reporter = User.query.filter(
            or_(
                User.user_name.ilike(f"%{filter_reporter}%"),
                User.email.ilike(f"%{filter_reporter}%"),
            )
        ).first()
        if reporter:
            reports = reports.filter(Report.reporter_id == reporter.id)

    if local_remote == "local":
        reports = reports.filter(Report.source_instance_id == 1)
    elif local_remote == "remote":
        reports = reports.filter(Report.source_instance_id != 1)
    # elif local_remote == 'all': no filter, show all origins

    now = utcnow()
    if date_range == "today":
        start_date = now - timedelta(hours=24)
        reports = reports.filter(Report.created_at >= start_date)
    elif date_range == "week":
        start_date = now - timedelta(days=7)
        reports = reports.filter(Report.created_at >= start_date)
    elif date_range == "month":
        start_date = now - timedelta(days=30)
        reports = reports.filter(Report.created_at >= start_date)
    elif date_range == "3months":
        start_date = now - timedelta(days=90)
        reports = reports.filter(Report.created_at >= start_date)
    elif date_range == "year":
        start_date = now - timedelta(days=365)
        reports = reports.filter(Report.created_at >= start_date)
    # elif date_range == 'all': show everything, no filter needed

    if filter_community:
        try:
            community_id = int(filter_community)
            reports = reports.filter(Report.in_community_id == community_id)
        except (ValueError, TypeError):
            pass

    if community_ids:
        try:
            ids = [int(id.strip()) for id in community_ids.split(",") if id.strip()]
            if ids:
                reports = reports.filter(Report.in_community_id.in_(ids))
        except (ValueError, TypeError):
            pass

    if sort_by == "oldest":
        reports = reports.order_by(Report.created_at.asc())
    else:  # newest
        reports = reports.order_by(Report.created_at.desc())

    total_reports = reports.count()

    reports = reports.paginate(page=page, per_page=20, error_out=False)

    return reports, total_reports


def process_report_context(report):
    """Process a single report's context data"""
    targets = {}

    targets["type"] = report.type
    targets["description"] = report.description
    targets["status"] = report.status
    targets["created_at"] = report.created_at

    if report.reporter_id:
        reporter = User.query.get(report.reporter_id)
        if reporter:
            targets["reporter_user_name"] = reporter.user_name
            targets["reporter_user_link"] = reporter.link()
            targets["reporter_ap_id"] = reporter.ap_id

    if report.in_community_id:
        community = Community.query.get(report.in_community_id)
        if community:
            targets["community_name"] = community.name
            targets["community_title"] = community.title
            targets["community_link"] = community.link()

    if report.suspect_post_id:
        post = Post.query.get(report.suspect_post_id)
        if post:
            targets["suspect_post_id"] = post.id
            targets["orig_post_title"] = post.title
            targets["orig_post_body"] = post.body
            targets["orig_post_url"] = post.url
            targets["post_score"] = post.score
            targets["post_comment_count"] = post.reply_count
            targets["post_deleted"] = post.deleted
            if post.user_id:
                author = User.query.get(post.user_id)
                if author:
                    targets["author_banned"] = author.banned

    if report.suspect_post_reply_id:
        reply = PostReply.query.get(report.suspect_post_reply_id)
        if reply:
            targets["suspect_comment_id"] = reply.id
            targets["orig_comment_body"] = reply.body
            targets["comment_deleted"] = reply.deleted
            if reply.post:
                targets["post_id"] = reply.post.id
                targets["post_title"] = reply.post.title
            # Check if author is banned
            if reply.user_id:
                author = User.query.get(reply.user_id)
                if author:
                    targets["author_banned"] = author.banned

    if report.suspect_user_id:
        user = User.query.get(report.suspect_user_id)
        if user:
            targets["suspect_user_id"] = user.id
            targets["suspect_username"] = user.user_name
            targets["suspect_user_link"] = user.link()
            targets["user_bio"] = (
                user.about_html if hasattr(user, "about_html") else None
            )
            targets["user_banned"] = user.banned
            targets["user_deleted"] = user.deleted
            if user.instance_id and user.instance_id != 1:
                instance = db.session.get(Instance, user.instance_id)
                if instance:
                    targets["source_instance_domain"] = instance.domain

    if report.suspect_conversation_id:
        targets["suspect_conversation_id"] = report.suspect_conversation_id

    if report.suspect_community_id:
        community = Community.query.get(report.suspect_community_id)
        if community:
            targets["community_name"] = community.name
            targets["community_description"] = community.description
            if community.instance_id and community.instance_id != 1:
                instance = db.session.get(Instance, community.instance_id)
                if instance:
                    targets["source_instance_domain"] = instance.domain

    return targets


@bp.route("/reports", methods=["GET"])
@permission_required("administer all users")
@login_required
def admin_reports():
    """Enhanced admin reports page with modern UI"""
    moderator_community_ids = moderating_communities_ids(current_user.get_id())

    reports, total_reports = get_reports_data(request.args, moderator_community_ids)

    report_contexts = {}
    for report in reports.items:
        targets = process_report_context(report)
        report_contexts[report.id] = {
            "targets": targets,
            "post_context": None,
            "comment_thread_context": None,
        }
        report.targets = targets

        if report.reporter_id:
            report.reporter_report_count = Report.query.filter_by(
                reporter_id=report.reporter_id
            ).count()

        if report.suspect_user_id:
            report.suspect_user = User.query.get(report.suspect_user_id)

        if report.suspect_post_id:
            report.related_reports_count = Report.query.filter(
                Report.id != report.id,
                Report.suspect_post_id == report.suspect_post_id,
            ).count()
        elif report.suspect_post_reply_id:
            report.related_reports_count = Report.query.filter(
                Report.id != report.id,
                Report.suspect_post_reply_id == report.suspect_post_reply_id,
            ).count()
        elif report.suspect_user_id:
            report.related_reports_count = Report.query.filter(
                Report.id != report.id,
                Report.suspect_user_id == report.suspect_user_id,
            ).count()
        else:
            report.related_reports_count = 0

        if report.suspect_post_id:
            post = Post.query.get(report.suspect_post_id)
            if post:
                author = User.query.get(post.user_id) if post.user_id else None
                if author and author.instance_id:
                    author.instance = db.session.get(Instance, author.instance_id)
                report_contexts[report.id]["post_context"] = {
                    "author": author,
                    "created_at": post.created_at,
                    "deleted": post.deleted,
                }
                report.post_context = report_contexts[report.id]["post_context"]

        if report.suspect_post_reply_id:
            reply = PostReply.query.get(report.suspect_post_reply_id)
            if reply:
                if reply.post_id:
                    post = Post.query.get(reply.post_id)
                    if post and post.user_id:
                        post.author = User.query.get(post.user_id)
                else:
                    post = None

                if reply.user_id:
                    reply.author = User.query.get(reply.user_id)
                    if reply.author and reply.author.instance_id:
                        reply.author.instance = db.session.get(
                            Instance, reply.author.instance_id
                        )

                parent_comment = None
                if reply.parent_id:
                    parent_comment = PostReply.query.get(reply.parent_id)
                    if parent_comment and parent_comment.user_id:
                        parent_comment.author = User.query.get(parent_comment.user_id)

                report_contexts[report.id]["comment_thread_context"] = {
                    "post": post,
                    "comment": reply,
                    "parent_comment": parent_comment,
                }
                report.comment_thread_context = report_contexts[report.id][
                    "comment_thread_context"
                ]

    if current_user.is_admin():
        communities = Community.query.order_by(Community.title).all()
    else:
        communities = (
            Community.query.filter(Community.id.in_(moderator_community_ids))
            .order_by(Community.title)
            .all()
        )

    response = make_response(
        render_template(
            "admin/reports.html",
            reports=reports,
            report_contexts=report_contexts,
            total_reports=total_reports,
            communities=communities,
            filter_type=request.args.get("filter_type", ""),
            filter_status=request.args.get("filter_status", ""),
            filter_reporter=request.args.get("filter_reporter", ""),
            filter_community=request.args.get("filter_community", ""),
            community_ids=request.args.get("community_ids", ""),
            sort_by=request.args.get("sort_by", ""),
            search=request.args.get("search", ""),
            local_remote=request.args.get("local_remote", ""),
            date_range=request.args.get("date_range", ""),
            has_query_params=bool(request.args),
            locale=str(get_locale()),
            arrow=arrow,
            REPORT_STATE_NEW=REPORT_STATE_NEW,
            REPORT_STATE_ESCALATED=REPORT_STATE_ESCALATED,
            REPORT_STATE_RESOLVED=REPORT_STATE_RESOLVED,
            REPORT_STATE_DISCARDED=REPORT_STATE_DISCARDED,
            low_bandwidth=request.cookies.get("low_bandwidth", "0") == "1",
            _=_,
        )
    )

    return response


@bp.route("/reports/action", methods=["POST"])
@permission_required("administer all users")
@login_required
def admin_report_action():
    """Handle individual report actions"""
    if request.is_json:
        data = request.get_json()
        report_id = data.get("report_id")
        action = data.get("action")
        reason = data.get("reason", "")

        notify_reporter = data.get("notify_reporter", False)
        resolve_similar = data.get("resolve_similar", False)
        ban_duration = data.get("ban_duration", "0")
        ban_scope = data.get("ban_scope", "community")
        delete_content = data.get("delete_content", False)
    else:
        form = AdminReportActionForm()
        if not form.validate_on_submit():
            return jsonify({"success": False, "error": "Invalid form submission"})

        report_id = form.report_id.data
        action = form.action.data
        reason = form.reason.data

        notify_reporter = form.notify_reporter.data
        resolve_similar = form.resolve_similar.data
        ban_duration = form.ban_duration.data
        ban_scope = form.ban_scope.data
        delete_content = form.delete_content.data

    report = Report.query.get_or_404(report_id)

    try:
        response_data = {"success": True, "action": action}

        action_data = {
            "reason": reason,
            "resolve_similar": resolve_similar,
            "ban_duration": ban_duration,
            "ban_scope": ban_scope,
            "delete_content": delete_content,
        }

        if action in REPORT_ACTION_HANDLERS:
            handler = REPORT_ACTION_HANDLERS[action]
            if action in ["ban_user", "remove_content"]:
                success, message = handler(report, action_data, current_user.id)
            else:
                success, message = handler(report, action_data)

            response_data["success"] = success
            if success:
                response_data["message"] = _(message)
                if action == "ban_user":
                    response_data["user_banned"] = True
                elif action == "remove_content":
                    response_data["content_removed"] = True
            else:
                response_data["error"] = _(message)
        else:
            response_data["success"] = False
            response_data["error"] = _("Invalid action")

        if (
            notify_reporter
            and report.reporter_id
            and response_data.get("success", False)
        ):
            notification = Notification(
                user_id=report.reporter_id,
                title="Report Update",
                url=f"/reports/{report.id}",
                author_id=current_user.id,
            )
            db.session.add(notification)

        db.session.commit()
        return jsonify(response_data)

    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)})


@bp.route("/reports/bulk-action", methods=["POST"])
@permission_required("administer all users")
@login_required
def admin_reports_bulk_action():
    """Handle bulk report actions"""
    data = request.get_json()

    report_ids = data.get("report_ids", [])
    action = data.get("action")
    reason = data.get("reason", "")

    if not report_ids or not action:
        return jsonify({"success": False, "error": "Invalid request"})

    try:
        reports = Report.query.filter(Report.id.in_(report_ids)).all()

        if action == "resolve":
            for report in reports:
                report.status = REPORT_STATE_RESOLVED

            update_report_counters_bulk(reports)

            db.session.commit()
            message = ngettext(
                "%(count)s report resolved successfully.",
                "%(count)s reports resolved successfully.",
                len(reports),
                count=len(reports),
            )

        elif action == "dismiss":
            for report in reports:
                report.status = REPORT_STATE_DISCARDED

            update_report_counters_bulk(reports)

            db.session.commit()
            message = ngettext(
                "%(count)s report dismissed successfully.",
                "%(count)s reports dismissed successfully.",
                len(reports),
                count=len(reports),
            )

        elif action == "ban_user":
            user_ids = set()
            for report in reports:
                if report.suspect_user_id:
                    user_ids.add(report.suspect_user_id)

            ban_duration = data.get("ban_duration", "0")
            ban_scope = data.get("ban_scope", "community")
            delete_content = data.get("delete_content", False)

            banned_count = 0
            for user_id in user_ids:
                if ban_scope == "community":
                    community_ids = set()
                    for report in reports:
                        if report.suspect_user_id == user_id and report.in_community_id:
                            community_ids.add(report.in_community_id)

                    for community_id in community_ids:
                        success, error = process_user_ban(
                            user_id,
                            ban_scope,
                            ban_duration,
                            reason,
                            current_user.id,
                            community_id,
                            delete_content,
                        )
                        if success:
                            banned_count += 1
                else:
                    success, error = process_user_ban(
                        user_id,
                        ban_scope,
                        ban_duration,
                        reason,
                        current_user.id,
                        None,
                        delete_content,
                    )
                    if success:
                        banned_count += 1

            for report in reports:
                report.status = REPORT_STATE_RESOLVED

            db.session.commit()
            message = ngettext(
                "%(count)s user banned and reports resolved.",
                "%(count)s users banned and reports resolved.",
                banned_count,
                count=banned_count,
            )

        elif action == "remove_content":
            removed_count = 0

            for report in reports:
                success, msg = remove_reported_content(report, current_user.id, reason)
                if success:
                    removed_count += 1
                    report.status = REPORT_STATE_RESOLVED

            db.session.commit()
            message = ngettext(
                "%(count)s content item removed and reports resolved.",
                "%(count)s content items removed and reports resolved.",
                removed_count,
                count=removed_count,
            )

        else:
            return jsonify({"success": False, "error": "Invalid action"})

        return jsonify({"success": True, "message": message})

    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)})
