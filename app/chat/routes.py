from flask import request, flash, url_for, redirect, abort, make_response
from flask_babel import _
from flask_login import current_user
from sqlalchemy import desc, or_, text

from app import db
from app.chat import bp
from app.chat.forms import AddReply, ReportConversationForm
from app.chat.util import send_message
from app.constants import NOTIF_REPORT, SRC_WEB, REPORT_TYPE_MESSAGE
from app.models import Site, User, Report, ChatMessage, Notification, Conversation, conversation_member, CommunityBan, \
    ModLog
from app.shared.site import block_remote_instance
from app.utils import render_template, login_required, trustworthy_account_required


@bp.route('/chat', methods=['GET', 'POST'])
@bp.route('/chat/<int:conversation_id>', methods=['GET', 'POST'])
@login_required
def chat_home(conversation_id=None):
    form = AddReply()
    if form.validate_on_submit():
        send_message(form.message.data, conversation_id)
        return redirect(url_for('chat.chat_home', conversation_id=conversation_id, _anchor='message'))
    else:
        conversations = Conversation.query.join(conversation_member,
                                                conversation_member.c.conversation_id == Conversation.id). \
            filter(conversation_member.c.user_id == current_user.id). \
            filter(conversation_member.c.joined == True). \
            order_by(desc(Conversation.updated_at)).limit(50).all()
        if conversation_id is None:
            if conversations:
                return redirect(url_for('chat.chat_home', conversation_id=conversations[0].id))
            else:
                return redirect(url_for('chat.empty'))
        else:
            conversation = Conversation.query.get_or_404(conversation_id)
            conversation.read = True
            if not current_user.is_admin() and not conversation.is_member(current_user):
                abort(400)
            if conversations:
                messages = conversation.messages.order_by(ChatMessage.created_at).all()
                for message in messages:
                    if message.recipient_id == current_user.id:
                        message.read = True
            else:
                messages = []

            members = db.session.execute(text("SELECT user_id FROM conversation_member WHERE joined = :state AND conversation_id = :conversation_id"),
                                         {"state": True, "conversation_id": conversation_id}).all()
            
            if len(members) == 1:
                alone = True
            else:
                alone = False

            sql = f"UPDATE notification SET read = true WHERE url LIKE '/chat/{conversation_id}%' AND user_id = {current_user.id}"
            db.session.execute(text(sql))
            db.session.commit()
            current_user.unread_notifications = Notification.query.filter_by(user_id=current_user.id, read=False).count()
            db.session.commit()

            return render_template('chat/conversation.html',
                                   title=_('Chat with %(name)s', name=conversation.member_names(current_user.id)),
                                   conversations=conversations, messages=messages, form=form, alone=alone,
                                   current_conversation=conversation_id, conversation=conversation)


@bp.route('/chat/<int:to>/new', methods=['GET', 'POST'])
@login_required
@trustworthy_account_required
def new_message(to):
    recipient = User.query.get_or_404(to)

    if not current_user.can_send_pm(recipient):
        return redirect(url_for('chat.denied'))
    
    if recipient.has_blocked_user(current_user.id) or current_user.has_blocked_user(recipient.id):
        return redirect(url_for('chat.blocked'))
    existing_conversation = Conversation.find_existing_conversation(recipient=recipient, sender=current_user)
    if existing_conversation:
        members = db.session.execute(text("SELECT user_id FROM conversation_member WHERE joined = :state AND conversation_id = :conversation_id"),
                                         {"state": True, "conversation_id": existing_conversation.id}).all()
        if current_user.id in members and recipient.id in members:
            return redirect(url_for('chat.chat_home', conversation_id=existing_conversation.id, _anchor='message'))
    form = AddReply()
    form.submit.label.text = _('Send')
    if form.validate_on_submit():
        conversation = Conversation(user_id=current_user.id)
        conversation.members.append(recipient)
        conversation.members.append(current_user)
        db.session.add(conversation)
        db.session.commit()
        send_message(form.message.data, conversation.id)
        return redirect(url_for('chat.chat_home', conversation_id=conversation.id, _anchor='message'))
    else:
        return render_template('chat/new_message.html', form=form,
                               title=_('New message to "%(recipient_name)s"', recipient_name=recipient.link()),
                               recipient=recipient,
                               )


@bp.route('/chat/denied', methods=['GET'])
@login_required
def denied():
    return render_template('chat/denied.html')


@bp.route('/chat/blocked', methods=['GET'])
@login_required
def blocked():
    return render_template('chat/blocked.html')


@bp.route('/chat/empty', methods=['GET'])
@login_required
def empty():
    return render_template('chat/empty.html')


@bp.route('/chat/ban_from_mod/<int:user_id>/<int:community_id>', methods=['GET'])
@login_required
def ban_from_mod(user_id, community_id):
    active_ban = CommunityBan.query.filter_by(user_id=user_id, community_id=community_id).order_by(
        desc(CommunityBan.created_at)).first()
    user_link = 'u/' + current_user.user_name
    past_bans = ModLog.query.filter(ModLog.community_id == community_id, ModLog.link == user_link,
                                    or_(ModLog.action == 'ban_user', ModLog.action == 'unban_user')).order_by(
        desc(ModLog.created_at))
    if active_ban:
        past_bans = past_bans.offset(1)
    # if active_ban and len(past_bans) > 1:
    # past_bans = past_bans
    return render_template('chat/ban_from_mod.html', active_ban=active_ban, past_bans=past_bans)


@bp.route('/chat/<int:conversation_id>/options', methods=['GET', 'POST'])
@login_required
def chat_options(conversation_id):
    conversation = Conversation.query.get_or_404(conversation_id)
    if current_user.is_admin() or conversation.is_member(current_user):
        return render_template('chat/chat_options.html', conversation=conversation)


@bp.route('/chat/<int:conversation_id>/delete', methods=['POST'])
@login_required
def chat_delete(conversation_id):
    conversation = Conversation.query.get_or_404(conversation_id)
    if current_user.is_admin() or conversation.is_member(current_user):
        Report.query.filter(Report.suspect_conversation_id == conversation.id).delete()
        db.session.delete(conversation)
        db.session.commit()
        flash(_('Conversation deleted'))
    return redirect(url_for('chat.chat_home'))


@bp.route('/chat/<int:conversation_id>/leave', methods=['POST'])
@login_required
def chat_leave(conversation_id):
    conversation = Conversation.query.get_or_404(conversation_id)
    if conversation.is_member(current_user):
        # Leave conversation
        db.session.execute(text("UPDATE conversation_member SET joined = :state WHERE user_id = :person_id AND conversation_id = :conversation_id"),
                           {"state": False, "person_id": current_user.id, "conversation_id": conversation_id})
        db.session.commit()

        conversation.delete_if_abandoned()

    return redirect(url_for('chat.chat_home'))


@bp.route('/chat/<int:instance_id>/block_instance', methods=['POST'])
@login_required
def block_instance(instance_id):
    block_remote_instance(instance_id, SRC_WEB)
    flash(_('Instance blocked.'))

    if request.headers.get('HX-Request'):
        resp = make_response()
        curr_url = request.headers.get('HX-Current-Url')

        if "/chat/" in curr_url:
            resp.headers["HX-Redirect"] = url_for("main.index")
        else:
            resp.headers["HX-Redirect"] = curr_url

        return resp

    return redirect(url_for('chat.chat_home'))


@bp.route('/chat/<int:conversation_id>/report', methods=['GET', 'POST'])
@login_required
def chat_report(conversation_id):
    conversation = Conversation.query.get_or_404(conversation_id)
    if current_user.is_admin() or conversation.is_member(current_user):
        form = ReportConversationForm()

        if form.validate_on_submit():
            targets_data = {'gen': '0', 'suspect_conversation_id': conversation.id, 'reporter_id': current_user.id}
            report = Report(reasons=form.reasons_to_string(form.reasons.data),
                            description=form.description.data,
                            type=REPORT_TYPE_MESSAGE,
                            reporter_id=current_user.id,
                            suspect_conversation_id=conversation_id,
                            source_instance_id=1,
                            targets=targets_data)
            db.session.add(report)

            # Notify site admin
            already_notified = set()
            for admin in Site.admins():
                if admin.id not in already_notified:
                    notify = Notification(title='Reported conversation with user', url='/admin/reports',
                                          user_id=admin.id,
                                          author_id=current_user.id, notif_type=NOTIF_REPORT,
                                          subtype='chat_conversation_reported',
                                          targets=targets_data)
                    db.session.add(notify)
                    admin.unread_notifications += 1
            db.session.commit()

            # todo: federate report to originating instance
            if form.report_remote.data:
                ...

            flash(_('This conversation has been reported, thank you!'))
            return redirect(url_for('chat.chat_home', conversation_id=conversation_id))
        elif request.method == 'GET':
            form.report_remote.data = True

        return render_template('chat/report.html', title=_('Report conversation'), form=form, conversation=conversation,

                               )


@bp.route('/chat/refresh-conversation/<int:conversation_id>')
@login_required
def chat_conversation(conversation_id):
    # reload a conversation when an SSE message arrives. See notifs.js
    conversation = Conversation.query.get_or_404(conversation_id)
    if current_user.is_admin() or conversation.is_member(current_user):
        messages = conversation.messages.order_by(ChatMessage.created_at).all()
        for message in messages:
            if message.recipient_id == current_user.id:
                message.read = True

        return render_template('chat/_messages.html', messages=messages, current_user=current_user)
    else:
        return ''
