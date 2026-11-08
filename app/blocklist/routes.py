from flask import request, redirect, url_for, flash, abort
from flask_babel import _
from flask_login import current_user
from sqlalchemy import desc

from app import db
from app.activitypub.util import extract_domain_and_actor
from app.blocklist import bp
from app.blocklist.forms import AddCopyBlocklistForm, SearchRemoteBlocklist, EditBlocklistForm
from app.blocklist.util import search_for_blocklist, blocklist_members_for_edit, actor_to_blocklist
from app.constants import SUBSCRIPTION_OWNER, SUBSCRIPTION_MEMBER, SUBSCRIPTION_NONMEMBER, SUBSCRIPTION_PENDING, \
    SRC_WEB
from app.models import User, Blocklist, BlocklistItem, BlocklistMember
from app.shared.blocklist import join_blocklist, leave_blocklist, make_blocklist, edit_blocklist, delete_blocklist
from app.utils import show_ban_message, render_template, login_required, validation_required, approval_required, \
    blocklist_membership


def _flash_unresolved(unresolved):
    if unresolved:
        flash(_('These accounts could not be found and were not added: %(accounts)s',
                accounts=', '.join(unresolved)), 'warning')


def _owned_blocklist_or_404(blocklist_id: int) -> Blocklist:
    blocklist = db.session.query(Blocklist).get(blocklist_id)
    if blocklist is None or not blocklist.is_local():
        abort(404)
    if blocklist.user_id != current_user.id and not current_user.is_admin():
        abort(404)
    return blocklist


@bp.route('/blocklists', methods=['GET'])
def blocklists():
    my_blocklists = []
    subscribed_blocklists = []
    if current_user.is_authenticated:
        my_blocklists = db.session.query(Blocklist).filter(Blocklist.user_id == current_user.id).\
            order_by(Blocklist.title).all()
        subscribed_blocklists = db.session.query(Blocklist).join(BlocklistMember, BlocklistMember.blocklist_id == Blocklist.id).\
            filter(BlocklistMember.user_id == current_user.id, BlocklistMember.is_owner == False).\
            order_by(Blocklist.title).all()

    search = request.args.get('search', '').strip()
    other_blocklists = db.session.query(Blocklist)
    if search:
        other_blocklists = other_blocklists.filter(Blocklist.title.ilike(f'%{search}%'))
    other_blocklists = other_blocklists.order_by(desc(Blocklist.subscriptions_count)).limit(100).all()

    return render_template('blocklist/blocklists.html', title=_('Blocklists'), my_blocklists=my_blocklists,
                           subscribed_blocklists=subscribed_blocklists, other_blocklists=other_blocklists,
                           search=search)


# @bp.route('/b/<actor>', methods=['GET']) - defined in activitypub/routes.py, which calls this function for browser requests
def show_blocklist(blocklist: Blocklist):
    page = request.args.get('page', 1, type=int)
    blocked_accounts = User.query.join(BlocklistItem, BlocklistItem.user_id == User.id).\
        filter(BlocklistItem.blocklist_id == blocklist.id).order_by(User.user_name).\
        paginate(page=page, per_page=100, error_out=False)
    next_url = url_for('activitypub.blocklist_profile', actor=blocklist.link(), page=blocked_accounts.next_num) \
        if blocked_accounts.has_next else None
    prev_url = url_for('activitypub.blocklist_profile', actor=blocklist.link(), page=blocked_accounts.prev_num) \
        if blocked_accounts.has_prev and page != 1 else None

    membership = blocklist_membership(current_user, blocklist) if current_user.is_authenticated else SUBSCRIPTION_NONMEMBER
    owner = db.session.query(User).get(blocklist.user_id)

    return render_template('blocklist/show_blocklist.html', title=blocklist.title, blocklist=blocklist, owner=owner,
                           blocked_accounts=blocked_accounts, next_url=next_url, prev_url=prev_url,
                           membership=membership, SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER,
                           SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER, SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING)


@bp.route('/blocklist/new', methods=['GET', 'POST'])
@login_required
def blocklist_new():
    if current_user.banned:
        return show_ban_message()

    form = AddCopyBlocklistForm()
    if form.validate_on_submit():
        blocklist, unresolved = make_blocklist(form, SRC_WEB)
        flash(_('Your new blocklist has been created.'))
        _flash_unresolved(unresolved)
        return redirect(url_for('activitypub.blocklist_profile', actor=blocklist.link()))

    return render_template('blocklist/blocklist_new.html', title=_('Create a blocklist'), form=form)


@bp.route('/blocklist/<int:blocklist_id>/copy', methods=['GET', 'POST'])
@login_required
def blocklist_copy(blocklist_id: int):
    if current_user.banned:
        return show_ban_message()
    blocklist_to_copy = db.session.query(Blocklist).get(blocklist_id)
    if blocklist_to_copy is None:
        abort(404)

    form = AddCopyBlocklistForm()
    if form.validate_on_submit():
        blocklist, unresolved = make_blocklist(form, SRC_WEB)
        flash(_('Your new blocklist has been created.'))
        _flash_unresolved(unresolved)
        return redirect(url_for('activitypub.blocklist_profile', actor=blocklist.link()))

    if request.method == 'GET':
        form.title.data = blocklist_to_copy.title
        form.url.data = blocklist_to_copy.machine_name
        form.description.data = blocklist_to_copy.description
        form.members.data = blocklist_members_for_edit(blocklist_to_copy.id)

    return render_template('blocklist/blocklist_new.html', title=_('Copy a blocklist'), form=form,
                           copying=blocklist_to_copy)


@bp.route('/blocklist/<int:blocklist_id>/edit', methods=['GET', 'POST'])
@login_required
def blocklist_edit(blocklist_id: int):
    if current_user.banned:
        return show_ban_message()
    blocklist = _owned_blocklist_or_404(blocklist_id)

    form = EditBlocklistForm()
    if form.validate_on_submit():
        unresolved = edit_blocklist(form, blocklist, SRC_WEB)
        flash(_('Settings saved.'))
        _flash_unresolved(unresolved)
        return redirect(url_for('activitypub.blocklist_profile', actor=blocklist.link()))

    if request.method == 'GET':
        form.title.data = blocklist.title
        form.description.data = blocklist.description
        form.members.data = blocklist_members_for_edit(blocklist.id)

    return render_template('blocklist/blocklist_edit.html', title=_('Edit %(title)s', title=blocklist.title),
                           form=form, blocklist=blocklist)


@bp.route('/blocklist/<int:blocklist_id>/delete', methods=['POST'])
@login_required
def blocklist_delete(blocklist_id: int):
    blocklist = _owned_blocklist_or_404(blocklist_id)
    delete_blocklist(blocklist, SRC_WEB)
    flash(_('Blocklist deleted'))
    return redirect(url_for('blocklist.blocklists'))


@bp.route('/blocklist/add_remote', methods=['GET', 'POST'])
@login_required
def blocklist_add_remote():
    if current_user.banned:
        return show_ban_message()
    form = SearchRemoteBlocklist()
    new_blocklist = None
    if form.validate_on_submit():
        address = form.address.data.strip().lower()
        try:
            if address.startswith('https://') or address.startswith('http://'):
                server, name = extract_domain_and_actor(address)
                new_blocklist = search_for_blocklist(f'%{name}@{server}')
            elif '@' in address:
                new_blocklist = search_for_blocklist(address)
            else:
                flash(_('Accepted address formats: %(example1)s or %(example2)s',
                        example1='%blocklistname@server.name', example2='https://server.name/b/blocklistname'),
                      'error')
        except Exception as e:
            if 'is blocked.' in str(e):
                flash(_('Sorry, that instance is blocked, check https://gui.fediseer.com/ for reasons.'), 'warning')
            else:
                raise
        if new_blocklist is None:
            flash(_('Blocklist not found.'), 'warning')
        else:
            return redirect(url_for('activitypub.blocklist_profile', actor=new_blocklist.link()))

    return render_template('blocklist/add_remote.html', title=_('Find a blocklist'), form=form)


@bp.route('/blocklist/<actor>/subscribe', methods=['POST'])
@login_required
@validation_required
@approval_required
def blocklist_subscribe(actor):
    if current_user.banned:
        return show_ban_message()
    blocklist = actor_to_blocklist(actor)
    if blocklist is None:
        abort(404)
    join_blocklist(blocklist, SRC_WEB)
    return redirect(url_for('activitypub.blocklist_profile', actor=blocklist.link()))


@bp.route('/blocklist/<actor>/unsubscribe', methods=['POST'])
@login_required
def blocklist_unsubscribe(actor):
    blocklist = actor_to_blocklist(actor)
    if blocklist is None:
        abort(404)
    leave_blocklist(blocklist, SRC_WEB)
    return redirect(url_for('activitypub.blocklist_profile', actor=blocklist.link()))
