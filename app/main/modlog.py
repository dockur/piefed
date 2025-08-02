"""
Modlog main route
This is the main modlog display route
"""

from random import randint

from flask import request, url_for, make_response, g
from flask_login import current_user
from flask_babel import _, get_locale
from sqlalchemy import or_, desc, func
import arrow

from app import db
from app.models import ModLog, Community, User, Instance
from sqlalchemy.orm import joinedload
from app.main import bp
from app.utils import render_template
from app.inoculation import inoculation
from app.main.modlog_helpers import (
    lookup_user,
    parse_community_ids,
    build_date_filter,
)


@bp.route("/modlog", methods=["GET"])
def modlog():
    page = request.args.get("page", 1, type=int)
    low_bandwidth = request.cookies.get("low_bandwidth", "0") == "1"
    can_see_names = False

    search = request.args.get("search", "")
    action_type = request.args.get("action_type", "")
    mod_type = request.args.get("mod_type", "")
    date_range = request.args.get("date_range", "")
    community_id = request.args.get("community_id", type=int)
    community_ids = request.args.get("community_ids", "")
    moderator_name = request.args.get("moderator_name", "")
    target_user = request.args.get("target_user", "")
    has_reason = request.args.get("has_reason", type=bool)
    post_id = request.args.get("post_id", type=int)
    reply_id = request.args.get("reply_id", type=int)

    default_local_remote = "all" if (moderator_name or target_user) else "local"
    local_remote = request.args.get("local_remote", default_local_remote)

    if current_user.is_authenticated:
        if current_user.is_admin() or current_user.is_staff():
            modlog_entries = db.session.query(ModLog).options(
                joinedload(ModLog.target_user),
                joinedload(ModLog.author),
                joinedload(ModLog.community),
                joinedload(ModLog.post),
                joinedload(ModLog.reply),
            )
            can_see_names = True
        else:
            modlog_entries = (
                db.session.query(ModLog)
                .filter(ModLog.public)
                .options(
                    joinedload(ModLog.target_user),
                    joinedload(ModLog.author),
                    joinedload(ModLog.community),
                    joinedload(ModLog.post),
                    joinedload(ModLog.reply),
                )
            )
    else:
        modlog_entries = (
            db.session.query(ModLog)
            .filter(ModLog.public)
            .options(
                joinedload(ModLog.target_user),
                joinedload(ModLog.author),
                joinedload(ModLog.community),
                joinedload(ModLog.post),
                joinedload(ModLog.reply),
            )
        )

    if search:
        search_term = f"%{search}%"

        user_found = None
        if "@" in search or " " not in search:
            user_found = lookup_user(search, exact_match=False)

        if user_found:
            modlog_entries = modlog_entries.filter(
                or_(
                    ModLog.user_id == user_found.id,
                    ModLog.target_user_id == user_found.id,
                )
            )
        else:
            modlog_entries = modlog_entries.filter(
                or_(
                    ModLog.action.ilike(search_term),
                    ModLog.reason.ilike(search_term),
                    ModLog.link_text.ilike(search_term),
                )
            )

    if action_type and action_type != "all":
        modlog_entries = modlog_entries.filter(ModLog.action == action_type)

    if mod_type and mod_type != "all":
        modlog_entries = modlog_entries.filter(ModLog.type == mod_type)

    if has_reason:
        modlog_entries = modlog_entries.filter(ModLog.reason is not None)

    if not date_range:
        date_range = "week"
    
    if date_range != "all":
        start_date = build_date_filter(date_range)
        if start_date:
            modlog_entries = modlog_entries.filter(ModLog.created_at >= start_date)

    community_filter_ids = parse_community_ids(community_ids)
    if not community_filter_ids and community_id:
        community_filter_ids = [community_id]

    if community_filter_ids:
        modlog_entries = modlog_entries.filter(
            ModLog.community_id.in_(community_filter_ids)
        )

    if local_remote and local_remote != "all":
        if local_remote == "local":
            modlog_entries = modlog_entries.join(ModLog.author).filter(
                User.instance_id == 1
            )
        elif local_remote == "remote":
            modlog_entries = modlog_entries.join(ModLog.author).filter(
                User.instance_id != 1
            )

    if moderator_name:
        moderator = lookup_user(moderator_name, exact_match=True)
        if moderator:
            modlog_entries = modlog_entries.filter(ModLog.user_id == moderator.id)

    target = None
    if target_user:
        target = lookup_user(target_user, exact_match=True)

    content_filters = []

    if post_id:
        content_filters.append(ModLog.post_id == post_id)

    if reply_id:
        content_filters.append(ModLog.reply_id == reply_id)

    if target and (post_id or reply_id):
        content_filters.append(ModLog.target_user_id == target.id)
    elif target and not (post_id or reply_id):
        modlog_entries = modlog_entries.filter(ModLog.target_user_id == target.id)

    if content_filters and (post_id or reply_id):
        modlog_entries = modlog_entries.filter(or_(*content_filters))

    modlog_entries = modlog_entries.order_by(desc(ModLog.created_at))

    total_entries = modlog_entries.count()
    action_counts = {}
    if can_see_names and total_entries > 0:
        stats_query = (
            modlog_entries.order_by(None)
            .with_entities(ModLog.action, func.count(ModLog.action))
            .group_by(ModLog.action)
        )

        for action, count in stats_query.all():
            action_counts[action] = count

    modlog_entries = modlog_entries.paginate(
        page=page, per_page=100 if not low_bandwidth else 50, error_out=False
    )

    filter_params = {k: v for k, v in request.args.items() if k != "page" and v}
    next_url = (
        url_for("main.modlog", page=modlog_entries.next_num, **filter_params)
        if modlog_entries.has_next
        else None
    )
    prev_url = (
        url_for("main.modlog", page=modlog_entries.prev_num, **filter_params)
        if modlog_entries.has_prev and page != 1
        else None
    )

    instances = {}
    for instance in Instance.query.all():
        instances[instance.id] = instance.domain

    communities = []
    if can_see_names:
        communities = Community.query.order_by(Community.name).all()

    response = make_response(
        render_template(
            "modlog.html",
            title=_("Moderation Log"),
            modlog_entries=modlog_entries,
            can_see_names=can_see_names,
            next_url=next_url,
            prev_url=prev_url,
            low_bandwidth=low_bandwidth,
            instances=instances,
            communities=communities,
            total_entries=total_entries,
            action_counts=action_counts,
            # Current filter values
            search=search,
            action_type=action_type,
            mod_type=mod_type,
            local_remote=local_remote,
            community_ids=community_ids,
            date_range=date_range,
            community_id=community_id,
            moderator_name=moderator_name,
            target_user=target_user,
            has_reason=has_reason,
            # Check if any non-default filters are active
            # Default values that don't count as active filters:
            # - local_remote defaults to 'local' (unless searching for users)
            # - date_range defaults to 'week'
            # - empty strings for text fields
            has_query_params=(
                bool(search) or
                (action_type and action_type != "all") or
                (mod_type and mod_type != "all") or
                (local_remote != default_local_remote) or
                (date_range and date_range not in ["week", ""]) or
                bool(community_ids) or
                bool(community_id) or
                bool(moderator_name) or
                bool(target_user) or
                bool(has_reason) or
                bool(post_id) or
                bool(reply_id)
            ),
            locale=str(get_locale()),
            arrow=arrow,
            inoculation=(
                inoculation[randint(0, len(inoculation) - 1)]
                if g.site.show_inoculation_block
                else None
            ),
        )
    )

    response.headers["X-Robots-Tag"] = (
        "noindex, nofollow, noarchive, nosnippet, noimageindex"
    )

    return response
