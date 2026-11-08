from flask import g, current_app, request, redirect, url_for, flash, abort, make_response
from markupsafe import Markup
from flask_babel import _
from flask_login import current_user
from slugify import slugify
from sqlalchemy import desc, or_

from app import db, cache, celery
from app.activitypub.signature import RsaKeys, send_post_request
from app.activitypub.util import extract_domain_and_actor
from app.blocklist.forms import AddCopyBlocklistForm, SearchRemoteBlocklist, EditBlocklistForm
from app.blocklist.util import search_for_blocklist, blocklist_members_for_edit
from app.community.util import save_icon_file, save_banner_file, hashtags_used_in_communities
from app.constants import SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR, POST_TYPE_IMAGE, \
    POST_TYPE_LINK, POST_TYPE_VIDEO, NOTIF_FEED, SUBSCRIPTION_MEMBER, SUBSCRIPTION_NONMEMBER, SRC_WEB
from app.feed import bp
from app.feed.forms import AddCopyFeedForm, EditFeedForm, SearchRemoteFeed
from app.feed.util import feeds_for_form, search_for_feed, actor_to_feed, feed_communities_for_edit
from app.inoculation import inoculation
from app.models import Feed, FeedMember, FeedItem, Community, NotificationSubscription, \
    CommunityMember, User, FeedJoinRequest, Instance, Topic, CommunityJoinRequest, Blocklist, BlocklistItem, \
    BlocklistMember
from app.shared.blocklist import join_blocklist, _blocklist_add_member, edit_blocklist, \
    make_blocklist, delete_blocklist
from app.utils import show_ban_message, piefed_markdown_to_lemmy_markdown, markdown_to_html, render_template, \
    user_filters_posts, joined_communities, menu_instance_feeds, validation_required, feed_membership, \
    gibberish, get_task_session, instance_banned, menu_subscribed_feeds, referrer, community_membership, \
    login_required, moderating_communities_ids, approval_required, \
    blocklist_membership


@bp.route('/blocklist/new', methods=['GET', 'POST'])
@login_required
def blocklist_new():
    if current_user.banned:
        return show_ban_message()

    form = AddCopyBlocklistForm()

    if form.validate_on_submit():
        if form.url.data.strip().lower().startswith('/f/'):
            form.url.data = form.url.data[3:]
        form.url.data = slugify(form.url.data.strip().split('/')[0], separator='_').lower()

        make_blocklist(form, SRC_WEB, None)

        flash(_('Your new blocklist has been created.'))
        return redirect(url_for('user.user_myblocklists', actor=current_user.link()))

    return render_template('feed/feed_new.html', title=_('Create a Feed'), form=form,
                           current_app=current_app, )


@bp.route('/blocklist/add_remote', methods=['GET', 'POST'])
@login_required
def blocklist_add_remote():
    if current_user.banned:
        return show_ban_message()
    form = SearchRemoteBlocklist()
    new_feed = None
    if form.validate_on_submit():
        address = form.address.data.strip().lower()

        if address.startswith('~') and '@' in address:
            try:
                new_feed = search_for_blocklist(address)
            except Exception as e:
                if 'is blocked.' in str(e):
                    flash(_('Sorry, that instance is blocked, check https://gui.fediseer.com/ for reasons.'), 'warning')
        elif address.startswith('@') and '@' in address[1:]:
            # todo: the user is searching for a person instead
            ...
        elif '@' in address:
            new_feed = search_for_blocklist('%' + address)
        elif address.startswith('https://') or address.startswith('http://'):
            server, feed = extract_domain_and_actor(address)
            new_feed = search_for_blocklist('%' + feed + '@' + server)
        else:
            message = Markup(
                _('Accepted address formats: %feedname@server.name or https://server.name/b/feedname.'))
            flash(message, 'error')
        if new_feed is None:
            flash(_('Feed not found.'), 'warning')
        else:
            cache.delete_memoized(blocklist_membership, current_user, new_feed)

    return render_template('feed/add_remote.html',
                           title=_('Add remote feed'), form=form, new_feed=new_feed,
                           subscribed=blocklist_membership(current_user, new_feed) >= SUBSCRIPTION_MEMBER,
                           )


@bp.route('/blocklist/<int:blocklist_id>/edit', methods=['GET', 'POST'])
@login_required
def blocklist_edit(blocklist_id: int):
    url_changed = False
    old_url = None
    if current_user.banned:
        return show_ban_message()
    # load the feed
    feed_to_edit: Blocklist = Blocklist.query.get_or_404(blocklist_id)
    # make sure the user owns this feed
    if feed_to_edit.user_id != current_user.id:
        abort(404)
    edit_feed_form = EditBlocklistForm()
    edit_feed_form.feed_id = blocklist_id

    if feed_to_edit.subscriptions_count > 1:
        edit_feed_form.url.render_kw = {'disabled': True}

    if edit_feed_form.validate_on_submit():
        if edit_feed_form.url.data:
            edit_feed_form.url.data = slugify(edit_feed_form.url.data.strip().split('/')[0], separator='_').lower()
            old_url = feed_to_edit.name
            url_changed = feed_to_edit.name != edit_feed_form.url.data
            feed_to_edit.name = edit_feed_form.url.data
            feed_to_edit.machine_name = edit_feed_form.url.data

        edit_blocklist(edit_feed_form, feed_to_edit, SRC_WEB, None, from_scratch=False)

        flash(_('Settings saved.'))
        if url_changed and old_url is not None:
            if referrer().endswith(old_url):
                return redirect('/f/' + feed_to_edit.name)
            else:
                return redirect(referrer())
        else:
            return redirect(referrer())

    # add the current data to the form
    edit_feed_form.title.data = feed_to_edit.title
    edit_feed_form.url.data = feed_to_edit.name
    edit_feed_form.description.data = feed_to_edit.description
    edit_feed_form.members.data = blocklist_members_for_edit(feed_to_edit.id)

    return render_template('blocklist/blocklist_edit.html', form=edit_feed_form, )


@bp.route('/blocklist/<int:blocklist_id>/delete', methods=['POST'])
@login_required
def blocklist_delete(blocklist_id: int):

    blocklist = Blocklist.query.get_or_404(blocklist_id)

    delete_blocklist(blocklist_id, SRC_WEB)

    flash(_('Blocklist deleted'))

    # send the user back to the page they came from or main
    # Get the referrer from the request headers
    referrer = request.referrer

    # If the referrer exists and is not the same as the current request URL, redirect to the referrer
    if referrer and referrer != request.url:
        return redirect(referrer)

    # If referrer is not available or is the same as the current request URL, redirect to the default URL
    return redirect(url_for('main.index'))


@bp.route('/blocklist/<int:blocklist_id>/copy', methods=['GET', 'POST'])
@login_required
def blocklist_copy(blocklist_id: int):
    if current_user.banned:
        return show_ban_message()
    # load the feed
    blocklist_to_copy = Feed.query.get_or_404(blocklist_id)
    copy_feed_form = AddCopyBlocklistForm()

    if copy_feed_form.validate_on_submit():
        if copy_feed_form.url.data.strip().lower().startswith('/f/'):
            copy_feed_form.url.data = copy_feed_form.url.data[3:]
        copy_feed_form.url.data = slugify(copy_feed_form.url.data, separator='_').lower()

        private_key, public_key = RsaKeys.generate_keypair()
        blocklist = Blocklist(user_id=current_user.id, title=copy_feed_form.title.data, name=copy_feed_form.url.data,
                    machine_name=copy_feed_form.url.data,
                    description=piefed_markdown_to_lemmy_markdown(copy_feed_form.description.data),
                    description_html=markdown_to_html(copy_feed_form.description.data),
                    private_key=private_key,
                    public_key=public_key,
                    ap_profile_id='https://' + current_app.config[
                        'SERVER_NAME'] + '/f/' + copy_feed_form.url.data.lower(),
                    ap_public_url='https://' + current_app.config['SERVER_NAME'] + '/f/' + copy_feed_form.url.data,
                    ap_followers_url='https://' + current_app.config[
                        'SERVER_NAME'] + '/f/' + copy_feed_form.url.data + '/followers',
                    ap_following_url='https://' + current_app.config[
                        'SERVER_NAME'] + '/f/' + copy_feed_form.url.data + '/following',
                    ap_domain=current_app.config['SERVER_NAME'],
                    subscriptions_count=1, instance_id=1)
        db.session.add(blocklist)
        db.session.commit()

        # get the FeedItems from the feed being copied and
        # make sure they all come over to the new Feed
        old_blocklist_items = BlocklistItem.query.join(Blocklist, BlocklistItem.blocklist_id == blocklist_to_copy.id).all()
        for item in old_blocklist_items:
            fi = BlocklistItem(blocklist_id=blocklist.id, user_id=item.user_id)
            db.session.add(fi)
            db.session.commit()

        # also subscribe the user to any community they are not already subscribed to
        member_of_ids = []
        member_of = BlocklistMember.query.filter_by(user_id=current_user.id).all()
        for cm in member_of:
            member_of_ids.append(cm.user_id)
        for item in old_blocklist_items:
            if item.community_id not in member_of_ids and current_user.feed_auto_follow:
                u = User.query.get(item.user_id)
                actor = u.ap_id if u.ap_id else u.name
                join_blocklist(actor, current_user.id, joined_via_blocklist=True)

        blocklist.num_blocked = len(old_blocklist_items)
        db.session.add(blocklist)
        db.session.commit()

        membership = BlocklistMember(user_id=current_user.id, blocklist_id=blocklist.id, is_owner=True)
        db.session.add(membership)
        db.session.commit()

        flash(_('Your new Feed has been created.'))
        return redirect(url_for('main.index'))

        # add the current data to the form
    copy_feed_form.title.data = blocklist_to_copy.title
    copy_feed_form.url.data = blocklist_to_copy.name
    copy_feed_form.description.data = blocklist_to_copy.description
    copy_feed_form.members.data = blocklist_members_for_edit(blocklist_to_copy.id)

    return render_template('feed/feed_copy.html', form=copy_feed_form)


@bp.route('/blocklist/<actor>/subscribe', methods=['GET'])
@login_required
@validation_required
@approval_required
def subscribe(actor):
    join_blocklist(actor, current_user.id)
    referrer = request.headers.get('Referer', None)
    if referrer is not None:
        return redirect(referrer)
    else:
        return redirect('/f/' + actor)


@bp.route('/blocklist/<actor>/unsubscribe', methods=['GET'])
@login_required
def blocklist_unsubscribe(actor):
    feed = actor_to_feed(actor)

    if feed is not None:
        subscription = feed_membership(current_user, feed)
        if subscription:
            if subscription != SUBSCRIPTION_OWNER:
                proceed = True
                # Undo the Follow
                if '@' in actor:  # this is a remote feed, so activitypub is needed
                    if not feed.instance.gone_forever:
                        follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{gibberish(15)}"
                        if feed.instance.domain == 'ovo.st':
                            join_request = FeedJoinRequest.query.filter_by(user_id=current_user.id,
                                                                           feed_id=feed.id).first()
                            if join_request:
                                follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
                        undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15)
                        follow = {
                            "actor": current_user.public_url(),
                            "to": [feed.public_url()],
                            "object": feed.public_url(),
                            "type": "Follow",
                            "id": follow_id
                        }
                        undo = {
                            'actor': current_user.public_url(),
                            'to': [feed.public_url()],
                            'type': 'Undo',
                            'id': undo_id,
                            'object': follow
                        }
                        send_post_request(feed.ap_inbox_url, undo, current_user.private_key,
                                          current_user.public_url() + '#main-key', timeout=10)

                if proceed:
                    db.session.query(FeedMember).filter_by(user_id=current_user.id, feed_id=feed.id).delete()
                    db.session.query(FeedJoinRequest).filter_by(user_id=current_user.id, feed_id=feed.id).delete()
                    feed.subscriptions_count -= 1
                    db.session.commit()

                    # Remove the account from each community in the feed
                    if current_user.feed_auto_leave:
                        for feed_item in FeedItem.query.filter_by(feed_id=feed.id).all():
                            membership = CommunityMember.query.filter_by(user_id=current_user.id,
                                                                         community_id=feed_item.community_id).first()
                            if membership and membership.joined_via_feed:
                                db.session.query(CommunityMember).filter_by(user_id=current_user.id,
                                                                            community_id=feed_item.community_id).delete()
                            db.session.query(CommunityJoinRequest).filter_by(user_id=current_user.id,
                                                                             community_id=feed_item.community_id).delete()
                            cache.delete_memoized(community_membership, current_user,
                                                  Community.query.get(feed_item.community_id))
                        db.session.commit()

                    flash(_('You have left %(feed_title)s', feed_title=feed.title))
                cache.delete_memoized(feed_membership, current_user, feed)
                cache.delete_memoized(menu_subscribed_feeds, current_user.id)
                cache.delete_memoized(joined_communities, current_user.id)
            else:
                # todo: community deletion
                flash(_('You need to make someone else the owner before unsubscribing.'), 'warning')

        # send them back where they came from
        referrer = request.headers.get('Referer', None)
        if referrer is not None:
            return redirect(referrer)
        else:
            return redirect('/f/' + actor)
    else:
        abort(404)
