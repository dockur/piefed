"""
Helper functions for the modlog to reduce complexity
"""

from sqlalchemy import or_, and_
from app.models import User


def lookup_user(search_term, exact_match=True):
    """
    Look up a user by username or ap_id

    Args:
        search_term: Username or AP ID to search for
        exact_match: Whether to do exact match first before partial match

    Returns:
        User object or None
    """
    if not search_term:
        return None

    # For local users: user_name matches AND ap_id is NULL
    # For remote users: ap_id matches exactly (since ap_id is stored as
    # username@domain)
    if exact_match:
        user = User.query.filter(
            or_(
                and_(User.user_name == search_term, User.ap_id is None),
                User.ap_id == search_term,
            )
        ).first()

        if user:
            return user

    # If no exact match or exact_match is False, try partial match
    search_pattern = f"%{search_term}%"
    user = User.query.filter(
        or_(
            and_(User.user_name.ilike(search_pattern), User.ap_id is None),
            User.ap_id.ilike(search_pattern),
        )
    ).first()

    return user


def parse_community_ids(community_ids_str):
    """
    Parse comma-separated community IDs string into list of integers

    Args:
        community_ids_str: Comma-separated string of IDs

    Returns:
        List of valid integer IDs
    """
    if not community_ids_str:
        return []

    valid_ids = []
    for cid in community_ids_str.split(","):
        try:
            id_value = int(cid.strip())
            valid_ids.append(id_value)
        except (ValueError, TypeError):
            continue

    return valid_ids


def build_date_filter(date_range):
    """
    Build datetime filter based on date range string

    Args:
        date_range: String like 'today', 'month', '3months', 'year'

    Returns:
        datetime object for filter start date or None
    """
    from datetime import datetime, timedelta

    if not date_range:
        return None

    now = datetime.utcnow()

    if date_range == "today":
        return now - timedelta(hours=24)
    elif date_range == "week":
        return now - timedelta(days=7)
    elif date_range == "month":
        return now - timedelta(days=30)
    elif date_range == "3months":
        return now - timedelta(days=90)
    elif date_range == "year":
        return now - timedelta(days=365)

    return None
