from flask import current_app


def task_selector(task_key, send_async=True, **kwargs):
    # Import tasks here to avoid circular imports
    from app.shared.tasks.follows import join_community, leave_community, leave_feed, follow_user, unfollow_user
    from app.shared.tasks.likes import vote_for_post, vote_for_reply, vote_for_poll
    from app.shared.tasks.notes import make_reply, edit_reply, choose_answer, unchoose_answer
    from app.shared.tasks.deletes import delete_reply, restore_reply, delete_post, restore_post, delete_community, \
        restore_community, delete_posts_with_blocked_images, delete_pm, restore_pm
    from app.shared.tasks.flags import report_reply, report_post
    from app.shared.tasks.pages import make_post, edit_post, move_post
    from app.shared.tasks.locks import lock_post, unlock_post, lock_post_reply, unlock_post_reply
    from app.shared.tasks.adds import sticky_post, add_mod
    from app.shared.tasks.removes import unsticky_post, remove_mod
    from app.shared.tasks.groups import edit_community
    from app.shared.tasks.users import check_user_application
    from app.shared.tasks.blocks import ban_from_community, unban_from_community, ban_from_site, unban_from_site
    tasks = {
        'join_community': join_community,
        'leave_community': leave_community,
        'leave_feed': leave_feed,
        'vote_for_post': vote_for_post,
        'vote_for_reply': vote_for_reply,
        'make_reply': make_reply,
        'edit_reply': edit_reply,
        'delete_reply': delete_reply,
        'restore_reply': restore_reply,
        'report_reply': report_reply,
        'make_post': make_post,
        'edit_post': edit_post,
        'delete_post': delete_post,
        'restore_post': restore_post,
        'report_post': report_post,
        'lock_post': lock_post,
        'unlock_post': unlock_post,
        'sticky_post': sticky_post,
        'unsticky_post': unsticky_post,
        'lock_post_reply': lock_post_reply,
        'unlock_post_reply': unlock_post_reply,
        'edit_community': edit_community,
        'delete_community': delete_community,
        'restore_community': restore_community,
        'delete_posts_with_blocked_images': delete_posts_with_blocked_images,
        'check_application': check_user_application,
        'ban_from_community': ban_from_community,
        'unban_from_community': unban_from_community,
        'ban_from_site': ban_from_site,
        'unban_from_site': unban_from_site,
        'add_mod': add_mod,
        'remove_mod': remove_mod,
        'delete_pm': delete_pm,
        'restore_pm': restore_pm,
        'vote_for_poll': vote_for_poll,
        'choose_answer': choose_answer,
        'unchoose_answer': unchoose_answer,
        'move_post': move_post,
        'follow_user': follow_user,
        'unfollow_user': unfollow_user
    }

    if current_app.debug:
        send_async = False

    if send_async:
        tasks[task_key].delay(send_async=send_async, **kwargs)
    else:
        return tasks[task_key](send_async=send_async, **kwargs)

