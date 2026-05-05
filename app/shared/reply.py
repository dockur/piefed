from __future__ import annotations

from flask import current_app, flash
from flask_babel import _, force_locale, gettext
from flask_login import current_user
from sqlalchemy import text

from app import db
from app.constants import *
from app.models import Notification, NotificationSubscription, Post, PostReply, PostReplyBookmark, Report, Site, User, \
    utcnow, Instance
from app.shared.tasks import task_selector
from app.utils import render_template, authorise_api_user, shorten_string, \
    piefed_markdown_to_lemmy_markdown, markdown_to_html, add_to_modlog, can_create_post_reply, \
    can_upvote, can_downvote, get_recipient_language


def vote_for_reply(reply_id: int, vote_direction, federate: bool, emoji: str | None, src, auth=None):
    if src == SRC_API:
        reply = db.session.query(PostReply).filter_by(id=reply_id).one()
        user = authorise_api_user(auth, return_type='model')
        if vote_direction == 'upvote' and not can_upvote(user, reply.community):
            return user.id
        elif vote_direction == 'downvote' and not can_downvote(user, reply.community):
            return user.id
    else:
        reply = db.session.query(PostReply).get_or_404(reply_id)
        user = current_user

    undo = reply.vote(user, vote_direction, emoji)

    task_selector('vote_for_reply', user_id=user.id, reply_id=reply_id, vote_to_undo=undo,
                  vote_direction=vote_direction, federate=federate, emoji=emoji)

    if src == SRC_API:
        return user.id
    else:
        recently_upvoted = []
        recently_downvoted = []
        if vote_direction == 'upvote' and undo is None:
            recently_upvoted = [reply_id]
        elif vote_direction == 'downvote' and undo is None:
            recently_downvoted = [reply_id]

        return render_template('post/_comment_voting_buttons.html', comment=reply,
                               recently_upvoted_replies=recently_upvoted,
                               recently_downvoted_replies=recently_downvoted,
                               community=reply.community)


def bookmark_reply(reply_id: int, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    existing_bookmark = PostReplyBookmark.query.filter_by(post_reply_id=reply_id, user_id=user_id).first()
    if not existing_bookmark:
        db.session.add(PostReplyBookmark(post_reply_id=reply_id, user_id=user_id))
        db.session.commit()
    else:
        msg = 'This comment has already been bookmarked.'
        if src == SRC_API:
            raise Exception(msg)
        else:
            flash(_(msg))

    if src == SRC_API:
        return user_id


def remove_bookmark_reply(reply_id: int, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    existing_bookmark = PostReplyBookmark.query.filter_by(post_reply_id=reply_id, user_id=user_id).first()
    if existing_bookmark:
        db.session.delete(existing_bookmark)
        db.session.commit()
    else:
        msg = 'This comment was not bookmarked.'
        if src == SRC_API:
            raise Exception(msg)
        else:
            flash(_(msg))

    if src == SRC_API:
        return user_id


def subscribe_reply(reply_id: int, subscribe, src, auth=None):
    reply = db.session.query(PostReply).filter_by(id=reply_id, deleted=False).join(Post, Post.id == PostReply.post_id).filter_by(deleted=False).one()
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    if src == SRC_WEB:
        subscribe = False if reply.notify_new_replies(user_id) else True

    existing_notification = NotificationSubscription.query.filter_by(entity_id=reply_id, user_id=user_id,
                                                                     type=NOTIF_REPLY).first()
    if subscribe == False:
        if existing_notification:
            db.session.delete(existing_notification)
            db.session.commit()
        else:
            msg = 'A subscription for this comment did not exist.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))

    else:
        if existing_notification:
            msg = 'A subscription for this comment already existed.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))
        else:
            new_notification = NotificationSubscription(name=shorten_string(_('Replies to my comment on %(post_title)s',
                                                                              post_title=reply.post.title)),
                                                        user_id=user_id, entity_id=reply_id,
                                                        type=NOTIF_REPLY)
            db.session.add(new_notification)
            db.session.commit()

    if src == SRC_API:
        return user_id
    else:
        return render_template('post/_reply_notification_toggle.html', comment={'comment': reply})


def extra_rate_limit_check(user):
    """
    The plan for this function is to do some extra limiting for an author who passes the rate limit for the route
    but who's comments are really unpopular and are probably spam
    """
    return False


def make_reply(input, post, parent_id, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
        if extra_rate_limit_check(user):
            raise Exception('rate_limited')
        content = input['body']
        notify_author = input['notify_author']
        language_id = input['language_id']
        distinguished = input['distinguished'] if 'distinguished' in input else False
        answer = False
    else:
        user = current_user
        content = input.body.data
        notify_author = input.notify_author.data
        language_id = input.language_id.data
        distinguished = input.distinguished.data
        answer = False

    if not post.community.is_moderator(user) and not post.community.is_owner(user) and not user.is_admin_or_staff():
        distinguished = False

    if parent_id:
        parent_reply = db.session.query(PostReply).filter_by(id=parent_id).one()
        if parent_reply.author.has_blocked_user(user.id) or parent_reply.author.has_blocked_instance(user.instance_id):
            raise Exception('The author of the parent reply has blocked the author or instance of the new reply.')
        if not parent_reply.replies_enabled:
            raise Exception('This comment cannot be replied to.')
    else:
        parent_reply = None

    if post.author.has_blocked_user(user.id) or post.author.has_blocked_instance(user.instance_id):
        raise Exception('The author of the parent post has blocked the author or instance of the new reply.')

    if not can_create_post_reply(user, post.community):
        raise Exception('You are not permitted to comment in this community')

    # WEBFORM would call 'make_reply' in a try block, so any exception from 'new' would bubble-up for it to handle
    reply = PostReply.new(user, post, in_reply_to=parent_reply, body=piefed_markdown_to_lemmy_markdown(content),
                          body_html=markdown_to_html(content), notify_author=notify_author,
                          language_id=language_id, distinguished=distinguished, answer=answer)

    user.language_id = language_id
    reply.ap_id = reply.profile_id()
    db.session.commit()
    if src == SRC_WEB:
        input.body.data = ''
        flash(_('Your comment has been added.'))

    task_selector('make_reply', reply_id=reply.id, parent_id=parent_id)

    if src == SRC_API:
        return user.id, reply
    else:
        return reply


def edit_reply(input, reply, post, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model', id_match=reply.user_id)
        content = input['body']
        notify_author = input['notify_author']
        language_id = input['language_id']
        distinguished = input['distinguished']
        if (not reply.distinguished and distinguished == True) or (reply.distinguished == True and distinguished == False):
            if not reply.community.is_moderator(user) and not reply.community.is_owner(user) and not user.is_staff() and not user.is_admin():
                raise Exception('Not a moderator')
    else:
        user = current_user
        content = input.body.data
        notify_author = input.notify_author.data
        language_id = input.language_id.data
        distinguished = input.distinguished.data

    reply.body = piefed_markdown_to_lemmy_markdown(content)
    reply.body_html = markdown_to_html(content)
    reply.notify_author = notify_author
    reply.community.last_active = utcnow()
    reply.edited_at = utcnow()
    reply.language_id = language_id
    if reply.community.is_moderator(user) or reply.community.is_owner(user) or user.is_admin_or_staff():
        reply.distinguished = distinguished
    db.session.commit()

    if src == SRC_WEB:
        flash(_('Your changes have been saved.'), 'success')

    task_selector('edit_reply', reply_id=reply.id, parent_id=reply.parent_id)

    if src == SRC_API:
        return user.id, reply
    else:
        return


# just for deletes by owner (mod deletes are classed as 'remove')
def delete_reply(reply_id, src, auth):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    reply = db.session.query(PostReply).filter_by(id=reply_id, user_id=user_id, deleted=False).one()
    reply.deleted = True
    reply.deleted_by = user_id

    if not reply.author.bot:
        reply.post.reply_count -= 1
    reply.author.post_reply_count -= 1
    if reply.path:
        db.session.execute(text('update post_reply set child_count = child_count - 1 where id in :parents'),
                           {'parents': tuple(reply.path[:-1])})
    db.session.commit()

    task_selector('delete_reply', user_id=user_id, reply_id=reply.id)

    if src == SRC_API:
        return user_id, reply
    else:
        return


def restore_reply(reply_id, src, auth):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    reply = db.session.query(PostReply).filter_by(id=reply_id, user_id=user_id, deleted=True).one()
    reply.deleted = False
    reply.deleted_by = None

    if not reply.author.bot:
        reply.post.reply_count += 1
    reply.author.post_reply_count += 1
    if reply.path:
        db.session.execute(text('update post_reply set child_count = child_count + 1 where id in :parents'),
                           {'parents': tuple(reply.path[:-1])})
    db.session.commit()
    if src == SRC_WEB:
        flash(_('Comment restored.'))

    task_selector('restore_reply', user_id=user_id, reply_id=reply.id)

    if src == SRC_API:
        return user_id, reply
    else:
        return


def report_reply(reply, input, src, auth=None):
    if src == SRC_API:
        reporter_user = authorise_api_user(auth, return_type='model')
        suspect_user = User.query.filter_by(id=reply.user_id).one()
        source_instance = Instance.query.filter_by(id=reply.instance_id).one()
        reason = input['reason']
        description = input['description']
        notify_admins = (any(x in reason.lower() for x in ['csam', 'dox']) or
                        any(x in description.lower() for x in ['csam', 'dox']))
        report_remote = input['report_remote']
    else:
        reporter_user = current_user
        suspect_user = User.query.get(reply.user_id)
        source_instance = Instance.query.get(suspect_user.instance_id)
        reason = input.reasons_to_string(input.reasons.data)
        description = input.description.data
        notify_admins = ('5' in input.reasons.data or '6' in input.reasons.data)
        report_remote = input.report_remote.data

    targets_data = {
        'gen': '0',
        'suspect_comment_id': reply.id,
        'suspect_user_id': reply.user_id,
        'suspect_user_user_name': suspect_user.ap_id if suspect_user.ap_id else suspect_user.user_name,
        'source_instance_id': source_instance.id,
        'source_instance_domain': source_instance.domain,
        'reporter_id': reporter_user.id,
        'reporter_user_name': reporter_user.user_name,
        'orig_comment_body': reply.body
    }
    # report.type 2 = 'reply'
    report = Report(reasons=reason,
                    description=description,
                    type=REPORT_TYPE_REPLY,
                    reporter_id=reporter_user.id,
                    suspect_post_id=reply.post_id,
                    suspect_community_id=reply.community_id,
                    suspect_user_id=reply.user_id,
                    suspect_post_reply_id=reply.id,
                    in_community_id=reply.community_id,
                    source_instance_id=reporter_user.instance_id,
                    targets=targets_data)
    db.session.add(report)

    # Notify local moderators, and send Flag to remote moderators
    # if user has not selected 'report_remote', just send to remote mods not on community's or suspect_users's instances
    already_notified = set()
    remote_instance_ids = set()
    for mod in reply.community.moderators():
        moderator = User.query.get(mod.user_id)
        if moderator:
            if moderator.is_local():
                with force_locale(get_recipient_language(moderator.id)):
                    notification = Notification(user_id=mod.user_id, title=gettext('A comment has been reported'),
                                                url=f"{current_app.config['SERVER_URL']}/comment/{reply.id}",
                                                author_id=reporter_user.id, notif_type=NOTIF_REPORT,
                                                subtype='comment_reported',
                                                targets=targets_data)
                    db.session.add(notification)
                    already_notified.add(mod.user_id)
            else:
                if not report_remote:
                    if moderator.instance_id != suspect_user.instance_id and moderator.instance_id != reply.community.instance_id:
                        remote_instance_ids.add(moderator.instance_id)
                else:
                    remote_instance_ids.add(moderator.instance_id)

    if notify_admins:
        for admin in Site.admins():
            if admin.id not in already_notified:
                notify = Notification(title='Suspicious content', url='/admin/reports', user_id=admin.id,
                                      author_id=reporter_user.id, notif_type=NOTIF_REPORT,
                                      subtype='comment_reported',
                                      targets=targets_data)
                db.session.add(notify)
                admin.unread_notifications += 1

    # Lemmy doesn't process or generate Announce / Flag, so Flags also have to be sent from here to user's and community's instances
    if report_remote:
        if not reply.community.is_local():
            if reply.community_id not in remote_instance_ids: # very unlikely, since it will typically have mods on same instance.
                remote_instance_ids.add(reply.community.instance_id)
        if not suspect_user.is_local():
            if suspect_user.instance_id not in remote_instance_ids:
                remote_instance_ids.add(suspect_user.instance_id)

    reply.reports += 1
    db.session.commit()

    if len(remote_instance_ids):
        summary = reason
        if description:
            summary += ' - ' + description

        task_selector('report_reply', user_id=reporter_user.id, reply_id=reply.id, summary=summary, instance_ids=list(remote_instance_ids))

    if src == SRC_API:
        return reporter_user.id, report
    else:
        return


# mod deletes
def mod_remove_reply(reply_id, reason, src, auth):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    reply = db.session.query(PostReply).filter_by(id=reply_id, deleted=False).one()
    if not reply.community.is_moderator(user) and not reply.community.is_instance_admin(user) and not user.is_admin_or_staff():
        raise Exception('Does not have permission')

    reply.deleted = True
    # set deleted_by to -1 if a mod is removing their own reply as part of a mod action, so it's shows as 'removed' rather than 'deleted'
    reply.deleted_by = user.id if user.id != reply.user_id else -1
    if not reply.author.bot:
        reply.post.reply_count -= 1
    reply.author.post_reply_count -= 1
    if reply.path:
        db.session.execute(text('update post_reply set child_count = child_count - 1 where id in :parents'),
                           {'parents': tuple(reply.path[:-1])})
    db.session.commit()
    if src == SRC_WEB:
        flash(_('Comment deleted.'))

    add_to_modlog('delete_post_reply', actor=user, target_user=reply.author, reason=reason,
                  community=reply.community, post=reply.post, reply=reply,
                  link_text=shorten_string(f'comment on {shorten_string(reply.post.title)}'),
                  link=f'post/{reply.post_id}#comment_{reply.id}')

    task_selector('delete_reply', user_id=user.id, reply_id=reply.id, reason=reason)

    if src == SRC_API:
        return user.id, reply
    else:
        return


def mod_restore_reply(reply_id, reason, src, auth):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    reply = db.session.query(PostReply).filter_by(id=reply_id, deleted=True).one()
    if not reply.community.is_moderator(user) and not reply.community.is_instance_admin(user):
        raise Exception('Does not have permission')

    reply.deleted = False
    reply.deleted_by = None
    if not reply.author.bot:
        reply.post.reply_count += 1
    reply.author.post_reply_count += 1
    if reply.path:
        db.session.execute(text('update post_reply set child_count = child_count + 1 where id in :parents'),
                           {'parents': tuple(reply.path[:-1])})

    db.session.commit()
    if src == SRC_WEB:
        flash(_('Comment restored.'))

    add_to_modlog('restore_post_reply', actor=user, target_user=reply.author, reason=reason,
                  community=reply.community, post=reply.post, reply=reply,
                  link_text=shorten_string(f'comment on {shorten_string(reply.post.title)}'),
                  link=f'post/{reply.post_id}#comment_{reply.id}')

    task_selector('restore_reply', user_id=user.id, reply_id=reply.id, reason=reason)

    if src == SRC_API:
        return user.id, reply
    else:
        return


def lock_post_reply(post_reply_id, locked, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
        post_reply = db.session.query(PostReply).filter_by(id=post_reply_id).one()
    else:
        user = current_user
        post_reply = db.session.query(PostReply).get(post_reply_id)

    if locked:
        replies_enabled = False
        modlog_type = 'lock_post_reply'
    else:
        replies_enabled = True
        modlog_type = 'unlock_post_reply'

    if post_reply.community.is_moderator(user) or post_reply.community.is_instance_admin(user):
        post_reply.replies_enabled = replies_enabled
        db.session.execute(text('update post_reply set replies_enabled = :replies_enabled where path @> ARRAY[:parent_id]'),
                           {'parent_id': post_reply.id, 'replies_enabled': replies_enabled})
        db.session.commit()
        add_to_modlog(modlog_type, actor=user, target_user=post_reply.author, reason='',
                      community=post_reply.community, reply=post_reply,
                      link_text=shorten_string(post_reply.body), link=f'post/{post_reply.post_id}#comment_{post_reply.id}')

        if locked:
            if src == SRC_WEB:
                flash(_('Comment has been locked.'))
            task_selector('lock_post_reply', user_id=user.id, post_reply_id=post_reply_id)
        else:
            if src == SRC_WEB:
                flash(_('Comment has been unlocked.'))
            task_selector('unlock_post_reply', user_id=user.id, post_reply_id=post_reply_id)

    if src == SRC_API:
        return user.id, post_reply


def choose_answer(post_reply_id, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post_reply = PostReply.query.get(post_reply_id)
    post_reply.answer = True
    with force_locale(get_recipient_language(post_reply.user_id)):
        title = _('Your answer was chosen as an answer to %(post_title)s',
                  post_title=shorten_string(post_reply.post.title, 100))
    targets_data = {'gen': '0',
                    'post_id': post_reply.post_id,
                    'requestor_id': user.id,
                    'author_user_name': post_reply.author.display_name(),
                    'post_title': shorten_string(post_reply.post.title, 100)}
    notify = Notification(title=title, url=post_reply.post.slug,
                          user_id=post_reply.user_id,
                          author_id=user.id, notif_type=NOTIF_ANSWER,
                          subtype='answer_chosen',
                          targets=targets_data)
    post_reply.author.unread_notifications += 1
    db.session.add(notify)
    db.session.commit()

    task_selector('choose_answer', user_id=user.id, post_reply_id=post_reply_id)

    if src == SRC_API:
        return user.id, post_reply


def unchoose_answer(post_reply_id, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post_reply = PostReply.query.get(post_reply_id)
    post_reply.answer = False
    db.session.commit()

    task_selector('unchoose_answer', user_id=user.id, post_reply_id=post_reply_id)

    if src == SRC_API:
        return user.id, post_reply