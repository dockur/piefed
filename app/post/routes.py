from collections import namedtuple
from datetime import datetime, timedelta
from random import randint

from flask import redirect, url_for, flash, current_app, abort, request, g, make_response
from flask_login import login_user, logout_user, current_user, login_required
from flask_babel import _
from sqlalchemy import or_, desc

from app import db, constants, cache
from app.activitypub.signature import HttpSignature, post_request
from app.activitypub.util import default_context
from app.community.util import save_post, send_to_remote_instance
from app.inoculation import inoculation
from app.post.forms import NewReplyForm, ReportPostForm, MeaCulpaForm
from app.community.forms import CreateLinkForm, CreateImageForm, CreateDiscussionForm
from app.post.util import post_replies, get_comment_branch, post_reply_count
from app.constants import SUBSCRIPTION_MEMBER, POST_TYPE_LINK, POST_TYPE_IMAGE, POST_TYPE_ARTICLE
from app.models import Post, PostReply, \
    PostReplyVote, PostVote, Notification, utcnow, UserBlock, DomainBlock, InstanceBlock, Report, Site, Community, \
    Topic, User, Instance
from app.post import bp
from app.utils import get_setting, render_template, allowlist_html, markdown_to_html, validation_required, \
    shorten_string, markdown_to_text, gibberish, ap_datetime, return_304, \
    request_etag_matches, ip_address, user_ip_banned, instance_banned, can_downvote, can_upvote, post_ranking, \
    reply_already_exists, reply_is_just_link_to_gif_reaction, confidence, moderating_communities, joined_communities, \
    blocked_instances, blocked_domains, community_moderators, blocked_phrases, show_ban_message


def show_post(post_id: int):
    post = Post.query.get_or_404(post_id)
    community: Community = post.community

    if community.banned:
        abort(404)

    sort = request.args.get('sort', 'hot')

    # If nothing has changed since their last visit, return HTTP 304
    current_etag = f"{post.id}{sort}_{hash(post.last_active)}"
    if current_user.is_anonymous and request_etag_matches(current_etag):
        return return_304(current_etag)

    if post.mea_culpa:
        flash(_('%(name)s has indicated they made a mistake in this post.', name=post.author.user_name), 'warning')

    mods = community_moderators(community.id)
    is_moderator = current_user.is_authenticated and any(mod.user_id == current_user.id for mod in mods)

    if community.private_mods:
        mod_list = []
    else:
        mod_user_ids = [mod.user_id for mod in mods]
        mod_list = User.query.filter(User.id.in_(mod_user_ids)).all()

    # handle top-level comments/replies
    form = NewReplyForm()
    if current_user.is_authenticated and current_user.verified and form.validate_on_submit():

        if not post.comments_enabled:
            flash('Comments have been disabled.', 'warning')
            return redirect(url_for('activitypub.post_ap', post_id=post_id))

        if current_user.banned:
            flash('You have been banned.', 'error')
            logout_user()
            resp = make_response(redirect(url_for('main.index')))
            resp.set_cookie('sesion', '17489047567495', expires=datetime(year=2099, month=12, day=30))
            return resp

        if post.author.has_blocked_user(current_user.id):
            flash(_('You cannot reply to %(name)s', name=post.author.display_name()))
            return redirect(url_for('activitypub.post_ap', post_id=post_id))

        # avoid duplicate replies
        if reply_already_exists(user_id=current_user.id, post_id=post.id, parent_id=None, body=form.body.data):
            return redirect(url_for('activitypub.post_ap', post_id=post_id))

        # disallow low-effort gif reaction posts
        if reply_is_just_link_to_gif_reaction(form.body.data):
            current_user.reputation -= 1
            flash(_('This type of comment is not accepted, sorry.'), 'error')
            return redirect(url_for('activitypub.post_ap', post_id=post_id))

        reply = PostReply(user_id=current_user.id, post_id=post.id, community_id=community.id, body=form.body.data,
                          body_html=markdown_to_html(form.body.data), body_html_safe=True,
                          from_bot=current_user.bot, nsfw=post.nsfw, nsfl=post.nsfl,
                          notify_author=form.notify_author.data, instance_id=1)
        if post.notify_author and current_user.id != post.user_id:
            notification = Notification(title=shorten_string(_('Reply from %(name)s on %(post_title)s',
                                                name=current_user.display_name(),
                                                post_title=post.title), 50),
                                        user_id=post.user_id,
                                        author_id=current_user.id, url=url_for('activitypub.post_ap', post_id=post.id))
            db.session.add(notification)
            post.author.unread_notifications += 1
        post.last_active = community.last_active = utcnow()
        post.reply_count += 1
        community.post_reply_count += 1

        db.session.add(reply)
        db.session.commit()
        reply.ap_id = reply.profile_id()
        if current_user.reputation > 100:
            reply.up_votes += 1
            reply.score += 1
            reply.ranking += 1
        elif current_user.reputation < -100:
            reply.score -= 1
            reply.ranking -= 1
        db.session.commit()
        form.body.data = ''
        flash('Your comment has been added.')

        post.flush_cache()

        # federation
        reply_json = {
            'type': 'Note',
            'id': reply.profile_id(),
            'attributedTo': current_user.public_url(),
            'to': [
                'https://www.w3.org/ns/activitystreams#Public'
            ],
            'cc': [
                community.public_url(), post.author.public_url()
            ],
            'content': reply.body_html,
            'inReplyTo': post.profile_id(),
            'mediaType': 'text/html',
            'source': {
                'content': reply.body,
                'mediaType': 'text/markdown'
            },
            'published': ap_datetime(utcnow()),
            'distinguished': False,
            'audience': community.public_url(),
            'tag': [{
                'href': post.author.public_url(),
                'name': post.author.mention_tag(),
                'type': 'Mention'
            }]
        }
        create_json = {
            'type': 'Create',
            'actor': current_user.public_url(),
            'audience': community.public_url(),
            'to': [
                'https://www.w3.org/ns/activitystreams#Public'
            ],
            'cc': [
                community.public_url(), post.author.public_url()
            ],
            'object': reply_json,
            'id': f"https://{current_app.config['SERVER_NAME']}/activities/create/{gibberish(15)}",
            'tag': [{
                'href': post.author.public_url(),
                'name': post.author.mention_tag(),
                'type': 'Mention'
            }]
        }
        if not community.is_local():    # this is a remote community, send it to the instance that hosts it
            success = post_request(community.ap_inbox_url, create_json, current_user.private_key,
                                                       current_user.ap_profile_id + '#main-key')
            if not success:
                flash('Failed to send to remote instance', 'error')
        else:                       # local community - send it to followers on remote instances
            announce = {
                "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}",
                "type": 'Announce',
                "to": [
                    "https://www.w3.org/ns/activitystreams#Public"
                ],
                "actor": community.public_url(),
                "cc": [
                    community.ap_followers_url
                ],
                '@context': default_context(),
                'object': create_json
            }

            for instance in community.following_instances():
                if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                    send_to_remote_instance(instance.id, community.id, announce)

        # send copy of Note to post author (who won't otherwise get it if no-one else on their instance is subscribed to the community)
        if not post.author.is_local() and post.author.ap_domain != community.ap_domain:
            if not community.is_local() or (community.is_local and not community.has_followers_from_domain(post.author.ap_domain)):
                success = post_request(post.author.ap_inbox_url, create_json, current_user.private_key,
                                                       current_user.ap_profile_id + '#main-key')
                if not success:
                    # sending to shared inbox is good enough for Mastodon, but Lemmy will reject it the local community has no followers
                    personal_inbox = post.author.public_url() + '/inbox'
                    post_request(personal_inbox, create_json, current_user.private_key,
                                                       current_user.ap_profile_id + '#main-key')

        return redirect(url_for('activitypub.post_ap', post_id=post_id))  # redirect to current page to avoid refresh resubmitting the form
    else:
        replies = post_replies(post.id, sort)
        form.notify_author.data = True

    og_image = post.image.source_url if post.image_id else None
    description = shorten_string(markdown_to_text(post.body), 150) if post.body else None

    breadcrumbs = []
    breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
    breadcrumb.text = _('Home')
    breadcrumb.url = '/'
    breadcrumbs.append(breadcrumb)

    if community.topic_id:
        related_communities = Community.query.filter_by(topic_id=community.topic_id).\
            filter(Community.id != community.id, Community.banned == False).order_by(Community.name)
        topics = []
        previous_topic = Topic.query.get(community.topic_id)
        topics.append(previous_topic)
        while previous_topic.parent_id:
            topic = Topic.query.get(previous_topic.parent_id)
            topics.append(topic)
            previous_topic = topic
        topics = list(reversed(topics))

        breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
        breadcrumb.text = _('Topics')
        breadcrumb.url = '/topics'
        breadcrumbs.append(breadcrumb)

        existing_url = '/topic'
        for topic in topics:
            breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
            breadcrumb.text = topic.name
            breadcrumb.url = f"{existing_url}/{topic.machine_name}"
            breadcrumbs.append(breadcrumb)
            existing_url = breadcrumb.url
    else:
        related_communities = []
        breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
        breadcrumb.text = _('Communities')
        breadcrumb.url = '/communities'
        breadcrumbs.append(breadcrumb)

    response = render_template('post/post.html', title=post.title, post=post, is_moderator=is_moderator, community=post.community,
                           breadcrumbs=breadcrumbs, related_communities=related_communities, mods=mod_list,
                           canonical=post.ap_id, form=form, replies=replies, THREAD_CUTOFF_DEPTH=constants.THREAD_CUTOFF_DEPTH,
                           description=description, og_image=og_image, POST_TYPE_IMAGE=constants.POST_TYPE_IMAGE,
                           POST_TYPE_LINK=constants.POST_TYPE_LINK, POST_TYPE_ARTICLE=constants.POST_TYPE_ARTICLE,
                           noindex=not post.author.indexable,
                           etag=f"{post.id}{sort}_{hash(post.last_active)}", markdown_editor=current_user.is_authenticated and current_user.markdown_editor,
                           low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1', SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           inoculation=inoculation[randint(0, len(inoculation) - 1)]
                           )
    response.headers.set('Vary', 'Accept, Cookie, Accept-Language')
    return response


@bp.route('/post/<int:post_id>/<vote_direction>', methods=['GET', 'POST'])
@login_required
@validation_required
def post_vote(post_id: int, vote_direction):
    upvoted_class = downvoted_class = ''
    post = Post.query.get_or_404(post_id)
    existing_vote = PostVote.query.filter_by(user_id=current_user.id, post_id=post.id).first()
    if existing_vote:
        if not post.community.low_quality:
            post.author.reputation -= existing_vote.effect
        if existing_vote.effect > 0:  # previous vote was up
            if vote_direction == 'upvote':  # new vote is also up, so remove it
                db.session.delete(existing_vote)
                post.up_votes -= 1
                post.score -= 1
            else:  # new vote is down while previous vote was up, so reverse their previous vote
                existing_vote.effect = -1
                post.up_votes -= 1
                post.down_votes += 1
                post.score -= 2
                downvoted_class = 'voted_down'
        else:  # previous vote was down
            if vote_direction == 'downvote':  # new vote is also down, so remove it
                db.session.delete(existing_vote)
                post.down_votes -= 1
                post.score += 1
            else:  # new vote is up while previous vote was down, so reverse their previous vote
                existing_vote.effect = 1
                post.up_votes += 1
                post.down_votes -= 1
                post.score += 2
                upvoted_class = 'voted_up'
    else:
        if vote_direction == 'upvote':
            effect = 1
            post.up_votes += 1
            post.score += 1
            upvoted_class = 'voted_up'
        else:
            effect = -1
            post.down_votes += 1
            post.score -= 1
            downvoted_class = 'voted_down'
        vote = PostVote(user_id=current_user.id, post_id=post.id, author_id=post.author.id,
                             effect=effect)
        # upvotes do not increase reputation in low quality communities
        if post.community.low_quality and effect > 0:
            effect = 0
        post.author.reputation += effect
        db.session.add(vote)

        if not post.community.local_only:
            action_type = 'Like' if vote_direction == 'upvote' else 'Dislike'
            action_json = {
                'actor': current_user.profile_id(),
                'object': post.profile_id(),
                'type': action_type,
                'id': f"https://{current_app.config['SERVER_NAME']}/activities/{action_type.lower()}/{gibberish(15)}",
                'audience': post.community.profile_id()
            }
            if post.community.is_local():
                announce = {
                    "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}",
                    "type": 'Announce',
                    "to": [
                        "https://www.w3.org/ns/activitystreams#Public"
                    ],
                    "actor": post.community.ap_profile_id,
                    "cc": [
                        post.community.ap_followers_url
                    ],
                    '@context': default_context(),
                    'object': action_json
                }
                for instance in post.community.following_instances():
                    if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                        send_to_remote_instance(instance.id, post.community.id, announce)
            else:
                success = post_request(post.community.ap_inbox_url, action_json, current_user.private_key,
                                                           current_user.ap_profile_id + '#main-key')
                if not success:
                    flash('Failed to send vote', 'warning')

    current_user.last_seen = utcnow()
    current_user.ip_address = ip_address()
    if not current_user.banned:
        post.ranking = post_ranking(post.score, post.created_at)
        db.session.commit()
        current_user.recalculate_attitude()
        db.session.commit()
    post.flush_cache()
    template = 'post/_post_voting_buttons.html' if request.args.get('style', '') == '' else 'post/_post_voting_buttons_masonry.html'
    return render_template(template, post=post, community=post.community,
                           upvoted_class=upvoted_class,
                           downvoted_class=downvoted_class)


@bp.route('/comment/<int:comment_id>/<vote_direction>', methods=['POST'])
@login_required
@validation_required
def comment_vote(comment_id, vote_direction):
    upvoted_class = downvoted_class = ''
    comment = PostReply.query.get_or_404(comment_id)
    existing_vote = PostReplyVote.query.filter_by(user_id=current_user.id, post_reply_id=comment.id).first()
    if existing_vote:
        if existing_vote.effect > 0:  # previous vote was up
            if vote_direction == 'upvote':  # new vote is also up, so remove it
                db.session.delete(existing_vote)
                comment.up_votes -= 1
                comment.score -= 1
            else:  # new vote is down while previous vote was up, so reverse their previous vote
                existing_vote.effect = -1
                comment.up_votes -= 1
                comment.down_votes += 1
                comment.score -= 2
                downvoted_class = 'voted_down'
        else:  # previous vote was down
            if vote_direction == 'downvote':  # new vote is also down, so remove it
                db.session.delete(existing_vote)
                comment.down_votes -= 1
                comment.score += 1
            else:  # new vote is up while previous vote was down, so reverse their previous vote
                existing_vote.effect = 1
                comment.up_votes += 1
                comment.down_votes -= 1
                comment.score += 2
                upvoted_class = 'voted_up'
    else:
        if vote_direction == 'upvote':
            effect = 1
            comment.up_votes += 1
            comment.score += 1
            upvoted_class = 'voted_up'
        else:
            effect = -1
            comment.down_votes += 1
            comment.score -= 1
            downvoted_class = 'voted_down'
        vote = PostReplyVote(user_id=current_user.id, post_reply_id=comment_id, author_id=comment.author.id, effect=effect)
        comment.author.reputation += effect
        db.session.add(vote)

        if comment.community.is_local():
            ...
            # todo: federate vote
        else:
            if not comment.community.local_only:
                action_type = 'Like' if vote_direction == 'upvote' else 'Dislike'
                action_json = {
                    'actor': current_user.profile_id(),
                    'object': comment.profile_id(),
                    'type': action_type,
                    'id': f"https://{current_app.config['SERVER_NAME']}/activities/{action_type.lower()}/{gibberish(15)}",
                    'audience': comment.community.profile_id()
                }
                success = post_request(comment.community.ap_inbox_url, action_json, current_user.private_key,
                                                           current_user.ap_profile_id + '#main-key')
                if not success:
                    flash('Failed to send vote', 'error')

    current_user.last_seen = utcnow()
    current_user.ip_address = ip_address()
    comment.ranking = confidence(comment.up_votes, comment.down_votes)
    db.session.commit()
    current_user.recalculate_attitude()
    db.session.commit()

    comment.post.flush_cache()
    return render_template('post/_comment_voting_buttons.html', comment=comment,
                           upvoted_class=upvoted_class,
                           downvoted_class=downvoted_class, community=comment.community)


@bp.route('/post/<int:post_id>/comment/<int:comment_id>')
def continue_discussion(post_id, comment_id):
    post = Post.query.get_or_404(post_id)
    comment = PostReply.query.get_or_404(comment_id)
    if post.community.banned:
        abort(404)
    mods = post.community.moderators()
    is_moderator = current_user.is_authenticated and any(mod.user_id == current_user.id for mod in mods)
    if post.community.private_mods:
        mod_list = []
    else:
        mod_user_ids = [mod.user_id for mod in mods]
        mod_list = User.query.filter(User.id.in_(mod_user_ids)).all()
    replies = get_comment_branch(post.id, comment.id, 'top')

    response = render_template('post/continue_discussion.html', title=_('Discussing %(title)s', title=post.title), post=post, mods=mod_list,
                           is_moderator=is_moderator, comment=comment, replies=replies, markdown_editor=current_user.is_authenticated and current_user.markdown_editor,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()), community=post.community,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)])
    response.headers.set('Vary', 'Accept, Cookie, Accept-Language')
    return response


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/reply', methods=['GET', 'POST'])
@login_required
def add_reply(post_id: int, comment_id: int):
    if current_user.banned:
        return show_ban_message()
    post = Post.query.get_or_404(post_id)

    if not post.comments_enabled:
        flash('Comments have been disabled.', 'warning')
        return redirect(url_for('activitypub.post_ap', post_id=post_id))

    in_reply_to = PostReply.query.get_or_404(comment_id)
    mods = post.community.moderators()
    is_moderator = current_user.is_authenticated and any(mod.user_id == current_user.id for mod in mods)
    if post.community.private_mods:
        mod_list = []
    else:
        mod_user_ids = [mod.user_id for mod in mods]
        mod_list = User.query.filter(User.id.in_(mod_user_ids)).all()

    if in_reply_to.author.has_blocked_user(current_user.id):
        flash(_('You cannot reply to %(name)s', name=in_reply_to.author.display_name()))
        return redirect(url_for('activitypub.post_ap', post_id=post_id))

    form = NewReplyForm()
    if form.validate_on_submit():
        if reply_already_exists(user_id=current_user.id, post_id=post.id, parent_id=in_reply_to.id, body=form.body.data):
            if in_reply_to.depth <= constants.THREAD_CUTOFF_DEPTH:
                return redirect(url_for('activitypub.post_ap', post_id=post_id, _anchor=f'comment_{in_reply_to.id}'))
            else:
                return redirect(url_for('post.continue_discussion', post_id=post_id, comment_id=in_reply_to.parent_id))

        if reply_is_just_link_to_gif_reaction(form.body.data):
            current_user.reputation -= 1
            flash(_('This type of comment is not accepted, sorry.'), 'error')
            if in_reply_to.depth <= constants.THREAD_CUTOFF_DEPTH:
                return redirect(url_for('activitypub.post_ap', post_id=post_id, _anchor=f'comment_{in_reply_to.id}'))
            else:
                return redirect(url_for('post.continue_discussion', post_id=post_id, comment_id=in_reply_to.parent_id))

        current_user.last_seen = utcnow()
        current_user.ip_address = ip_address()
        reply = PostReply(user_id=current_user.id, post_id=post.id, parent_id=in_reply_to.id, depth=in_reply_to.depth + 1,
                          community_id=post.community.id, body=form.body.data,
                          body_html=markdown_to_html(form.body.data), body_html_safe=True,
                          from_bot=current_user.bot, nsfw=post.nsfw, nsfl=post.nsfl,
                          notify_author=form.notify_author.data, instance_id=1)
        if reply.body:
            for blocked_phrase in blocked_phrases():
                if blocked_phrase in reply.body:
                    abort(401)
        db.session.add(reply)
        if in_reply_to.notify_author and current_user.id != in_reply_to.user_id and in_reply_to.author.ap_id is None:    # todo: check if replier is blocked
            notification = Notification(title=shorten_string(_('Reply from %(name)s on %(post_title)s',
                                                name=current_user.display_name(),
                                                post_title=post.title), 50),
                                        user_id=in_reply_to.user_id,
                                        author_id=current_user.id, url=url_for('activitypub.post_ap', post_id=post.id))
            db.session.add(notification)
            in_reply_to.author.unread_notifications += 1
        db.session.commit()
        reply.ap_id = reply.profile_id()
        db.session.commit()
        if current_user.reputation > 100:
            reply.up_votes += 1
            reply.score += 1
            reply.ranking += 1
        elif current_user.reputation < -100:
            reply.score -= 1
            reply.ranking -= 1
        post.reply_count = post_reply_count(post.id)
        post.last_active = post.community.last_active = utcnow()
        db.session.commit()
        form.body.data = ''
        flash('Your comment has been added.')

        post.flush_cache()

        # federation
        if not post.community.local_only:
            reply_json = {
                'type': 'Note',
                'id': reply.profile_id(),
                'attributedTo': current_user.public_url(),
                'to': [
                    'https://www.w3.org/ns/activitystreams#Public'
                ],
                'cc': [
                    post.community.public_url(),
                    in_reply_to.author.public_url()
                ],
                'content': reply.body_html,
                'inReplyTo': in_reply_to.profile_id(),
                'url': reply.profile_id(),
                'mediaType': 'text/html',
                'source': {
                    'content': reply.body,
                    'mediaType': 'text/markdown'
                },
                'published': ap_datetime(utcnow()),
                'distinguished': False,
                'audience': post.community.public_url(),
                'contentMap': {
                    'en': reply.body_html
                }
            }
            create_json = {
                '@context': default_context(),
                'type': 'Create',
                'actor': current_user.public_url(),
                'audience': post.community.public_url(),
                'to': [
                    'https://www.w3.org/ns/activitystreams#Public'
                ],
                'cc': [
                    post.community.public_url(),
                    in_reply_to.author.public_url()
                ],
                'object': reply_json,
                'id': f"https://{current_app.config['SERVER_NAME']}/activities/create/{gibberish(15)}"
            }
            if in_reply_to.notify_author and in_reply_to.author.ap_id is not None:
                reply_json['tag'] = [
                    {
                        'href': in_reply_to.author.public_url(),
                        'name': in_reply_to.author.mention_tag(),
                        'type': 'Mention'
                    }
                ]
                create_json['tag'] = [
                    {
                        'href': in_reply_to.author.public_url(),
                        'name': in_reply_to.author.mention_tag(),
                        'type': 'Mention'
                    }
                ]
            if not post.community.is_local():    # this is a remote community, send it to the instance that hosts it
                success = post_request(post.community.ap_inbox_url, create_json, current_user.private_key,
                                                           current_user.ap_profile_id + '#main-key')
                if not success:
                    flash('Failed to send reply', 'error')
            else:                       # local community - send it to followers on remote instances
                announce = {
                    "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}",
                    "type": 'Announce',
                    "to": [
                        "https://www.w3.org/ns/activitystreams#Public"
                    ],
                    "actor": post.community.public_url(),
                    "cc": [
                        post.community.ap_followers_url
                    ],
                    '@context': default_context(),
                    'object': create_json
                }

                for instance in post.community.following_instances():
                    if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                        send_to_remote_instance(instance.id, post.community.id, announce)

            # send copy of Note to comment author (who won't otherwise get it if no-one else on their instance is subscribed to the community)
            if not in_reply_to.author.is_local() and in_reply_to.author.ap_domain != reply.community.ap_domain:
                if not post.community.is_local() or (post.community.is_local and not post.community.has_followers_from_domain(in_reply_to.author.ap_domain)):
                    success = post_request(in_reply_to.author.ap_inbox_url, create_json, current_user.private_key,
                                                           current_user.ap_profile_id + '#main-key')
                    if not success:
                        # sending to shared inbox is good enough for Mastodon, but Lemmy will reject it the local community has no followers
                        personal_inbox = in_reply_to.author.public_url() + '/inbox'
                        post_request(personal_inbox, create_json, current_user.private_key,
                                                       current_user.ap_profile_id + '#main-key')

        if reply.depth <= constants.THREAD_CUTOFF_DEPTH:
            return redirect(url_for('activitypub.post_ap', post_id=post_id, _anchor=f'comment_{reply.id}'))
        else:
            return redirect(url_for('post.continue_discussion', post_id=post_id, comment_id=reply.parent_id))
    else:
        form.notify_author.data = True
        return render_template('post/add_reply.html', title=_('Discussing %(title)s', title=post.title), post=post,
                               is_moderator=is_moderator, form=form, comment=in_reply_to, markdown_editor=current_user.is_authenticated and current_user.markdown_editor,
                               moderating_communities=moderating_communities(current_user.get_id()), mods=mod_list,
                               joined_communities = joined_communities(current_user.id),
                               inoculation=inoculation[randint(0, len(inoculation) - 1)])


@bp.route('/post/<int:post_id>/options', methods=['GET'])
def post_options(post_id: int):
    post = Post.query.get_or_404(post_id)
    return render_template('post/post_options.html', post=post,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()))


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/options', methods=['GET'])
def post_reply_options(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    return render_template('post/post_reply_options.html', post=post, post_reply=post_reply,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id())
                           )


@bp.route('/post/<int:post_id>/edit', methods=['GET'])
@login_required
def post_edit(post_id: int):
    post = Post.query.get_or_404(post_id)
    if post.type == POST_TYPE_ARTICLE:
        return redirect(url_for('post.post_edit_discussion_post', post_id=post_id))
    elif post.type == POST_TYPE_LINK:
        return redirect(url_for('post.post_edit_link_post', post_id=post_id))
    elif post.type == POST_TYPE_IMAGE:
        return redirect(url_for('post.post_edit_image_post', post_id=post_id))
    else:
        abort(404)


@bp.route('/post/<int:post_id>/edit_discussion', methods=['GET', 'POST'])
@login_required
def post_edit_discussion_post(post_id: int):
    post = Post.query.get_or_404(post_id)
    form = CreateDiscussionForm()
    del form.communities

    mods = post.community.moderators()
    if post.community.private_mods:
        mod_list = []
    else:
        mod_user_ids = [mod.user_id for mod in mods]
        mod_list = User.query.filter(User.id.in_(mod_user_ids)).all()

    if post.user_id == current_user.id or post.community.is_moderator() or current_user.is_admin():
        if g.site.enable_nsfl is False:
            form.nsfl.render_kw = {'disabled': True}
        if post.community.nsfw:
            form.nsfw.data = True
            form.nsfw.render_kw = {'disabled': True}
        if post.community.nsfl:
            form.nsfl.data = True
            form.nsfw.render_kw = {'disabled': True}

        if form.validate_on_submit():
            save_post(form, post, 'discussion')
            post.community.last_active = utcnow()
            post.edited_at = utcnow()
            db.session.commit()

            post.flush_cache()
            flash(_('Your changes have been saved.'), 'success')

            # federate edit
            if not post.community.local_only:
                federate_post_update(post)

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.discussion_title.data = post.title
            form.discussion_body.data = post.body
            form.notify_author.data = post.notify_author
            form.nsfw.data = post.nsfw
            form.nsfl.data = post.nsfl
            form.sticky.data = post.sticky
            if not (post.community.is_moderator() or post.community.is_owner() or current_user.is_admin()):
                form.sticky.render_kw = {'disabled': True}
            return render_template('post/post_edit_discussion.html', title=_('Edit post'), form=form, post=post,
                                   markdown_editor=current_user.markdown_editor, mods=mod_list,
                                   moderating_communities=moderating_communities(current_user.get_id()),
                                   joined_communities=joined_communities(current_user.get_id()),
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)]
                                   )
    else:
        abort(401)


@bp.route('/post/<int:post_id>/edit_image', methods=['GET', 'POST'])
@login_required
def post_edit_image_post(post_id: int):
    post = Post.query.get_or_404(post_id)
    form = CreateImageForm()
    del form.communities

    mods = post.community.moderators()
    if post.community.private_mods:
        mod_list = []
    else:
        mod_user_ids = [mod.user_id for mod in mods]
        mod_list = User.query.filter(User.id.in_(mod_user_ids)).all()

    if post.user_id == current_user.id or post.community.is_moderator() or current_user.is_admin():
        if g.site.enable_nsfl is False:
            form.nsfl.render_kw = {'disabled': True}
        if post.community.nsfw:
            form.nsfw.data = True
            form.nsfw.render_kw = {'disabled': True}
        if post.community.nsfl:
            form.nsfl.data = True
            form.nsfw.render_kw = {'disabled': True}

        old_url = post.url

        if form.validate_on_submit():
            save_post(form, post, 'image')
            post.community.last_active = utcnow()
            post.edited_at = utcnow()
            db.session.commit()

            if post.url != old_url:
                if post.cross_posts is not None:
                    old_cross_posts = Post.query.filter(Post.id.in_(post.cross_posts)).all()
                    post.cross_posts.clear()
                    for ocp in old_cross_posts:
                        if ocp.cross_posts is not None:
                            ocp.cross_posts.remove(post.id)

                new_cross_posts = Post.query.filter(Post.id != post.id, Post.url == post.url,
                                                Post.posted_at > post.edited_at - timedelta(days=6)).all()
                for ncp in new_cross_posts:
                    if ncp.cross_posts is None:
                        ncp.cross_posts = [post.id]
                    else:
                        ncp.cross_posts.append(post.id)
                    if post.cross_posts is None:
                        post.cross_posts = [ncp.id]
                    else:
                        post.cross_posts.append(ncp.id)

                db.session.commit()

            post.flush_cache()
            flash(_('Your changes have been saved.'), 'success')
            # federate edit

            if not post.community.local_only:
                federate_post_update(post)

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.image_title.data = post.title
            form.image_body.data = post.body
            form.image_alt_text.data = post.image.alt_text
            form.notify_author.data = post.notify_author
            form.nsfw.data = post.nsfw
            form.nsfl.data = post.nsfl
            form.sticky.data = post.sticky
            if not (post.community.is_moderator() or post.community.is_owner() or current_user.is_admin()):
                form.sticky.render_kw = {'disabled': True}
            return render_template('post/post_edit_image.html', title=_('Edit post'), form=form, post=post,
                                   markdown_editor=current_user.markdown_editor, mods=mod_list,
                                   moderating_communities=moderating_communities(current_user.get_id()),
                                   joined_communities=joined_communities(current_user.get_id()),
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)]
                                   )
    else:
        abort(401)


@bp.route('/post/<int:post_id>/edit_link', methods=['GET', 'POST'])
@login_required
def post_edit_link_post(post_id: int):
    post = Post.query.get_or_404(post_id)
    form = CreateLinkForm()
    del form.communities

    mods = post.community.moderators()
    if post.community.private_mods:
        mod_list = []
    else:
        mod_user_ids = [mod.user_id for mod in mods]
        mod_list = User.query.filter(User.id.in_(mod_user_ids)).all()

    if post.user_id == current_user.id or post.community.is_moderator() or current_user.is_admin():
        if g.site.enable_nsfl is False:
            form.nsfl.render_kw = {'disabled': True}
        if post.community.nsfw:
            form.nsfw.data = True
            form.nsfw.render_kw = {'disabled': True}
        if post.community.nsfl:
            form.nsfl.data = True
            form.nsfw.render_kw = {'disabled': True}

        old_url = post.url

        if form.validate_on_submit():
            save_post(form, post, 'link')
            post.community.last_active = utcnow()
            post.edited_at = utcnow()
            db.session.commit()

            if post.url != old_url:
                if post.cross_posts is not None:
                    old_cross_posts = Post.query.filter(Post.id.in_(post.cross_posts)).all()
                    post.cross_posts.clear()
                    for ocp in old_cross_posts:
                        if ocp.cross_posts is not None:
                            ocp.cross_posts.remove(post.id)

                new_cross_posts = Post.query.filter(Post.id != post.id, Post.url == post.url,
                                                Post.posted_at > post.edited_at - timedelta(days=6)).all()
                for ncp in new_cross_posts:
                    if ncp.cross_posts is None:
                        ncp.cross_posts = [post.id]
                    else:
                        ncp.cross_posts.append(post.id)
                    if post.cross_posts is None:
                        post.cross_posts = [ncp.id]
                    else:
                        post.cross_posts.append(ncp.id)

                db.session.commit()

            post.flush_cache()
            flash(_('Your changes have been saved.'), 'success')
            # federate edit

            if not post.community.local_only:
                federate_post_update(post)

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.link_title.data = post.title
            form.link_body.data = post.body
            form.link_url.data = post.url
            form.notify_author.data = post.notify_author
            form.nsfw.data = post.nsfw
            form.nsfl.data = post.nsfl
            form.sticky.data = post.sticky
            if not (post.community.is_moderator() or post.community.is_owner() or current_user.is_admin()):
                form.sticky.render_kw = {'disabled': True}
            return render_template('post/post_edit_link.html', title=_('Edit post'), form=form, post=post,
                                   markdown_editor=current_user.markdown_editor, mods=mod_list,
                                   moderating_communities=moderating_communities(current_user.get_id()),
                                   joined_communities=joined_communities(current_user.get_id()),
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)]
                                   )
    else:
        abort(401)


def federate_post_update(post):
    page_json = {
        'type': 'Page',
        'id': post.ap_id,
        'attributedTo': current_user.ap_profile_id,
        'to': [
            post.community.ap_profile_id,
            'https://www.w3.org/ns/activitystreams#Public'
        ],
        'name': post.title,
        'cc': [],
        'content': post.body_html if post.body_html else '',
        'mediaType': 'text/html',
        'source': {
            'content': post.body if post.body else '',
            'mediaType': 'text/markdown'
        },
        'attachment': [],
        'commentsEnabled': post.comments_enabled,
        'sensitive': post.nsfw,
        'nsfl': post.nsfl,
        'stickied': post.sticky,
        'published': ap_datetime(post.posted_at),
        'updated': ap_datetime(post.edited_at),
        'audience': post.community.ap_profile_id
    }
    update_json = {
        'id': f"https://{current_app.config['SERVER_NAME']}/activities/update/{gibberish(15)}",
        'type': 'Update',
        'actor': current_user.profile_id(),
        'audience': post.community.profile_id(),
        'to': [post.community.profile_id(), 'https://www.w3.org/ns/activitystreams#Public'],
        'published': ap_datetime(utcnow()),
        'cc': [
            current_user.followers_url()
        ],
        'object': page_json,
    }
    if post.type == POST_TYPE_LINK:
        page_json['attachment'] = [{'href': post.url, 'type': 'Link'}]
    elif post.image_id:
        if post.image.file_path:
            image_url = post.image.file_path.replace('app/static/',
                                                     f"https://{current_app.config['SERVER_NAME']}/static/")
        elif post.image.thumbnail_path:
            image_url = post.image.thumbnail_path.replace('app/static/',
                                                          f"https://{current_app.config['SERVER_NAME']}/static/")
        else:
            image_url = post.image.source_url
        # NB image is a dict while attachment is a list of dicts (usually just one dict in the list)
        page_json['image'] = {'type': 'Image', 'url': image_url}
        if post.type == POST_TYPE_IMAGE:
            page_json['attachment'] = [{'type': 'Link',
                                        'href': post.image.source_url}]  # source_url is always a https link, no need for .replace() as done above
    if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
        success = post_request(post.community.ap_inbox_url, update_json, current_user.private_key,
                               current_user.ap_profile_id + '#main-key')
        if not success:
            flash('Failed to send edit to remote server', 'error')
    else:  # local community - send it to followers on remote instances
        announce = {
            "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}",
            "type": 'Announce',
            "to": [
                "https://www.w3.org/ns/activitystreams#Public"
            ],
            "actor": post.community.ap_profile_id,
            "cc": [
                post.community.ap_followers_url
            ],
            '@context': default_context(),
            'object': update_json
        }

        for instance in post.community.following_instances():
            if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(
                    instance.domain):
                send_to_remote_instance(instance.id, post.community.id, announce)


@bp.route('/post/<int:post_id>/delete', methods=['GET', 'POST'])
@login_required
def post_delete(post_id: int):
    post = Post.query.get_or_404(post_id)
    community = post.community
    if post.user_id == current_user.id or community.is_moderator() or current_user.is_admin():
        if post.url:
            if post.cross_posts is not None:
                old_cross_posts = Post.query.filter(Post.id.in_(post.cross_posts)).all()
                post.cross_posts.clear()
                for ocp in old_cross_posts:
                    if ocp.cross_posts is not None:
                        ocp.cross_posts.remove(post.id)
        post.delete_dependencies()
        post.flush_cache()
        db.session.delete(post)
        g.site.last_active = community.last_active = utcnow()
        db.session.commit()
        flash(_('Post deleted.'))

        if not community.local_only:
            delete_json = {
                'id': f"https://{current_app.config['SERVER_NAME']}/activities/delete/{gibberish(15)}",
                'type': 'Delete',
                'actor': current_user.profile_id(),
                'audience': post.community.profile_id(),
                'to': [post.community.profile_id(), 'https://www.w3.org/ns/activitystreams#Public'],
                'published': ap_datetime(utcnow()),
                'cc': [
                    current_user.followers_url()
                ],
                'object': post.ap_id,
            }

            if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
                success = post_request(post.community.ap_inbox_url, delete_json, current_user.private_key,
                                       current_user.ap_profile_id + '#main-key')
                if not success:
                    flash('Failed to send delete to remote server', 'error')
            else:  # local community - send it to followers on remote instances
                announce = {
                    "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}",
                    "type": 'Announce',
                    "to": [
                        "https://www.w3.org/ns/activitystreams#Public"
                    ],
                    "actor": post.community.ap_profile_id,
                    "cc": [
                        post.community.ap_followers_url
                    ],
                    '@context': default_context(),
                    'object': delete_json
                }

                for instance in post.community.following_instances():
                    if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(
                            instance.domain):
                        send_to_remote_instance(instance.id, post.community.id, announce)

    return redirect(url_for('activitypub.community_profile', actor=community.ap_id if community.ap_id is not None else community.name))


@bp.route('/post/<int:post_id>/report', methods=['GET', 'POST'])
@login_required
def post_report(post_id: int):
    post = Post.query.get_or_404(post_id)
    form = ReportPostForm()
    if post.reports == -1:  # When a mod decides to ignore future reports, post.reports is set to -1
        flash(_('Moderators have already assessed reports regarding this post, no further reports are necessary.'), 'warning')
    if form.validate_on_submit():
        if post.reports == -1:
            flash(_('Post has already been reported, thank you!'))
            return redirect(post.community.local_url())
        report = Report(reasons=form.reasons_to_string(form.reasons.data), description=form.description.data,
                        type=1, reporter_id=current_user.id, suspect_user_id=post.author.id, suspect_post_id=post.id,
                        suspect_community_id=post.community.id, in_community_id=post.community.id, source_instance_id=1)
        db.session.add(report)

        # Notify moderators
        already_notified = set()
        for mod in post.community.moderators():
            notification = Notification(user_id=mod.user_id, title=_('A post has been reported'),
                                        url=f"https://{current_app.config['SERVER_NAME']}/post/{post.id}",
                                        author_id=current_user.id)
            db.session.add(notification)
            already_notified.add(mod.user_id)
        post.reports += 1
        # todo: only notify admins for certain types of report
        for admin in Site.admins():
            if admin.id not in already_notified:
                notify = Notification(title='Suspicious content', url='/admin/reports', user_id=admin.id, author_id=current_user.id)
                db.session.add(notify)
                admin.unread_notifications += 1
        db.session.commit()

        # federate report to community instance
        if not post.community.is_local() and form.report_remote.data:
            summary = form.reasons_to_string(form.reasons.data)
            if form.description.data:
                summary += ' - ' + form.description.data
            report_json = {
              "actor": current_user.profile_id(),
              "audience": post.community.profile_id(),
              "content": None,
              "id": f"https://{current_app.config['SERVER_NAME']}/activities/flag/{gibberish(15)}",
              "object": post.ap_id,
              "summary": summary,
              "to": [
                post.community.profile_id()
              ],
              "type": "Flag"
            }
            instance = Instance.query.get(post.community.instance_id)
            if post.community.ap_inbox_url and not current_user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                success = post_request(post.community.ap_inbox_url, report_json, current_user.private_key,
                                       current_user.ap_profile_id + '#main-key')
                if not success:
                    flash('Failed to send report to remote server', 'error')

        flash(_('Post has been reported, thank you!'))
        return redirect(post.community.local_url())
    elif request.method == 'GET':
        form.report_remote.data = True

    return render_template('post/post_report.html', title=_('Report post'), form=form, post=post,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id())
                           )


@bp.route('/post/<int:post_id>/block_user', methods=['GET', 'POST'])
@login_required
def post_block_user(post_id: int):
    post = Post.query.get_or_404(post_id)
    existing = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=post.author.id).first()
    if not existing:
        db.session.add(UserBlock(blocker_id=current_user.id, blocked_id=post.author.id))
        db.session.commit()
    flash(_('%(name)s has been blocked.', name=post.author.user_name))

    # todo: federate block to post author instance

    return redirect(post.community.local_url())


@bp.route('/post/<int:post_id>/block_domain', methods=['GET', 'POST'])
@login_required
def post_block_domain(post_id: int):
    post = Post.query.get_or_404(post_id)
    existing = DomainBlock.query.filter_by(user_id=current_user.id, domain_id=post.domain_id).first()
    if not existing:
        db.session.add(DomainBlock(user_id=current_user.id, domain_id=post.domain_id))
        db.session.commit()
        cache.delete_memoized(blocked_domains, current_user.id)
    flash(_('Posts linking to %(name)s will be hidden.', name=post.domain.name))
    return redirect(post.community.local_url())


@bp.route('/post/<int:post_id>/block_instance', methods=['GET', 'POST'])
@login_required
def post_block_instance(post_id: int):
    post = Post.query.get_or_404(post_id)
    existing = InstanceBlock.query.filter_by(user_id=current_user.id, instance_id=post.instance_id).first()
    if not existing:
        db.session.add(InstanceBlock(user_id=current_user.id, instance_id=post.instance_id))
        db.session.commit()
        cache.delete_memoized(blocked_instances, current_user.id)
    flash(_('Content from %(name)s will be hidden.', name=post.instance.domain))
    return redirect(post.community.local_url())


@bp.route('/post/<int:post_id>/mea_culpa', methods=['GET', 'POST'])
@login_required
def post_mea_culpa(post_id: int):
    post = Post.query.get_or_404(post_id)
    form = MeaCulpaForm()
    if form.validate_on_submit():
        post.comments_enabled = False
        post.mea_culpa = True
        post.community.last_active = utcnow()
        post.last_active = utcnow()
        db.session.commit()
        return redirect(url_for('activitypub.post_ap', post_id=post.id))

    return render_template('post/post_mea_culpa.html', title=_('I changed my mind'), form=form, post=post,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id())
                           )


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/report', methods=['GET', 'POST'])
@login_required
def post_reply_report(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    form = ReportPostForm()

    if post_reply.reports == -1:  # When a mod decides to ignore future reports, post_reply.reports is set to -1
        flash(_('Moderators have already assessed reports regarding this comment, no further reports are necessary.'), 'warning')

    if form.validate_on_submit():

        if post_reply.reports == -1:
            flash(_('Comment has already been reported, thank you!'))
            return redirect(post.community.local_url())

        report = Report(reasons=form.reasons_to_string(form.reasons.data), description=form.description.data,
                        type=2, reporter_id=current_user.id, suspect_post_id=post.id, suspect_community_id=post.community.id,
                        suspect_user_id=post_reply.author.id, suspect_post_reply_id=post_reply.id, in_community_id=post.community.id,
                        source_instance_id=1)
        db.session.add(report)

        # Notify moderators
        already_notified = set()
        for mod in post.community.moderators():
            notification = Notification(user_id=mod.user_id, title=_('A comment has been reported'),
                                        url=f"https://{current_app.config['SERVER_NAME']}/comment/{post_reply.id}",
                                        author_id=current_user.id)
            db.session.add(notification)
            already_notified.add(mod.user_id)
        post_reply.reports += 1
        # todo: only notify admins for certain types of report
        for admin in Site.admins():
            if admin.id not in already_notified:
                notify = Notification(title='Suspicious content', url='/admin/reports', user_id=admin.id, author_id=current_user.id)
                db.session.add(notify)
                admin.unread_notifications += 1
        db.session.commit()

        # federate report to originating instance
        if not post.community.is_local() and form.report_remote.data:
            summary = form.reasons_to_string(form.reasons.data)
            if form.description.data:
                summary += ' - ' + form.description.data
            report_json = {
                "actor": current_user.profile_id(),
                "audience": post.community.profile_id(),
                "content": None,
                "id": f"https://{current_app.config['SERVER_NAME']}/activities/flag/{gibberish(15)}",
                "object": post_reply.ap_id,
                "summary": summary,
                "to": [
                    post.community.profile_id()
                ],
                "type": "Flag"
            }
            instance = Instance.query.get(post.community.instance_id)
            if post.community.ap_inbox_url and not current_user.has_blocked_instance(
                    instance.id) and not instance_banned(instance.domain):
                success = post_request(post.community.ap_inbox_url, report_json, current_user.private_key,
                                       current_user.ap_profile_id + '#main-key')
                if not success:
                    flash('Failed to send report to remote server', 'error')

        flash(_('Comment has been reported, thank you!'))
        return redirect(post.community.local_url())
    elif request.method == 'GET':
        form.report_remote.data = True

    return render_template('post/post_reply_report.html', title=_('Report comment'), form=form, post=post, post_reply=post_reply,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id())
                           )


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/block_user', methods=['GET', 'POST'])
@login_required
def post_reply_block_user(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    existing = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=post_reply.author.id).first()
    if not existing:
        db.session.add(UserBlock(blocker_id=current_user.id, blocked_id=post_reply.author.id))
        db.session.commit()
    flash(_('%(name)s has been blocked.', name=post_reply.author.user_name))

    # todo: federate block to post_reply author instance

    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/block_instance', methods=['GET', 'POST'])
@login_required
def post_reply_block_instance(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    existing = InstanceBlock.query.filter_by(user_id=current_user.id, instance_id=post_reply.instance_id).first()
    if not existing:
        db.session.add(InstanceBlock(user_id=current_user.id, instance_id=post_reply.instance_id))
        db.session.commit()
    flash(_('Content from %(name)s will be hidden.', name=post_reply.instance.domain))
    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/edit', methods=['GET', 'POST'])
@login_required
def post_reply_edit(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    if post_reply.parent_id:
        comment = PostReply.query.get_or_404(post_reply.parent_id)
    else:
        comment = None
    form = NewReplyForm()
    if post_reply.user_id == current_user.id or post.community.is_moderator():
        if form.validate_on_submit():
            post_reply.body = form.body.data
            post_reply.body_html = markdown_to_html(form.body.data)
            post_reply.notify_author = form.notify_author.data
            post.community.last_active = utcnow()
            post_reply.edited_at = utcnow()
            db.session.commit()
            post.flush_cache()
            flash(_('Your changes have been saved.'), 'success')

            if post_reply.parent_id:
                in_reply_to = PostReply.query.get(post_reply.parent_id)
            else:
                in_reply_to = post
            # federate edit
            if not post.community.local_only:
                reply_json = {
                    'type': 'Note',
                    'id': post_reply.profile_id(),
                    'attributedTo': current_user.public_url(),
                    'to': [
                        'https://www.w3.org/ns/activitystreams#Public'
                    ],
                    'cc': [
                        post.community.public_url(),
                        in_reply_to.author.public_url()
                    ],
                    'content': post_reply.body_html,
                    'inReplyTo': in_reply_to.profile_id(),
                    'url': post_reply.profile_id(),
                    'mediaType': 'text/html',
                    'source': {
                        'content': post_reply.body,
                        'mediaType': 'text/markdown'
                    },
                    'published': ap_datetime(post_reply.posted_at),
                    'updated': ap_datetime(post_reply.edited_at),
                    'distinguished': False,
                    'audience': post.community.public_url(),
                    'contentMap': {
                        'en': post_reply.body_html
                    }
                }
                update_json = {
                    '@context': default_context(),
                    'type': 'Update',
                    'actor': current_user.public_url(),
                    'audience': post.community.public_url(),
                    'to': [
                        'https://www.w3.org/ns/activitystreams#Public'
                    ],
                    'cc': [
                        post.community.public_url(),
                        in_reply_to.author.public_url()
                    ],
                    'object': reply_json,
                    'id': f"https://{current_app.config['SERVER_NAME']}/activities/update/{gibberish(15)}"
                }
                if in_reply_to.notify_author and in_reply_to.author.ap_id is not None:
                    reply_json['tag'] = [
                        {
                            'href': in_reply_to.author.public_url(),
                            'name': in_reply_to.author.mention_tag(),
                            'type': 'Mention'
                        }
                    ]
                    update_json['tag'] = [
                        {
                            'href': in_reply_to.author.public_url(),
                            'name': in_reply_to.author.mention_tag(),
                            'type': 'Mention'
                        }
                    ]
                if not post.community.is_local():    # this is a remote community, send it to the instance that hosts it
                    success = post_request(post.community.ap_inbox_url, update_json, current_user.private_key,
                                                               current_user.ap_profile_id + '#main-key')
                    if not success:
                        flash('Failed to send send edit to remote server', 'error')
                else:                       # local community - send it to followers on remote instances
                    announce = {
                        "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}",
                        "type": 'Announce',
                        "to": [
                            "https://www.w3.org/ns/activitystreams#Public"
                        ],
                        "actor": post.community.public_url(),
                        "cc": [
                            post.community.ap_followers_url
                        ],
                        '@context': default_context(),
                        'object': update_json
                    }

                    for instance in post.community.following_instances():
                        if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                            send_to_remote_instance(instance.id, post.community.id, announce)

                # send copy of Note to post author (who won't otherwise get it if no-one else on their instance is subscribed to the community)
                if not in_reply_to.author.is_local() and in_reply_to.author.ap_domain != post_reply.community.ap_domain:
                    if not post.community.is_local() or (post.community.is_local and not post.community.has_followers_from_domain(in_reply_to.author.ap_domain)):
                        success = post_request(in_reply_to.author.ap_inbox_url, update_json, current_user.private_key,
                                                               current_user.ap_profile_id + '#main-key')
                        if not success:
                            # sending to shared inbox is good enough for Mastodon, but Lemmy will reject it the local community has no followers
                            personal_inbox = in_reply_to.author.public_url() + '/inbox'
                            post_request(personal_inbox, update_json, current_user.private_key,
                                                           current_user.ap_profile_id + '#main-key')

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.body.data = post_reply.body
            form.notify_author.data = post_reply.notify_author
            return render_template('post/post_reply_edit.html', title=_('Edit comment'), form=form, post=post, post_reply=post_reply,
                                   comment=comment, markdown_editor=current_user.markdown_editor, moderating_communities=moderating_communities(current_user.get_id()),
                                   joined_communities=joined_communities(current_user.get_id()),
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)])
    else:
        abort(401)


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/delete', methods=['GET', 'POST'])
@login_required
def post_reply_delete(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    community = post.community
    if post_reply.user_id == current_user.id or community.is_moderator():
        if post_reply.has_replies():
            post_reply.body = 'Deleted by author' if post_reply.author.id == current_user.id else 'Deleted by moderator'
            post_reply.body_html = markdown_to_html(post_reply.body)
        else:
            post_reply.delete_dependencies()
            db.session.delete(post_reply)
        g.site.last_active = community.last_active = utcnow()
        db.session.commit()
        post.flush_cache()
        flash(_('Comment deleted.'))
        # federate delete
        if not post.community.local_only:
            delete_json = {
                'id': f"https://{current_app.config['SERVER_NAME']}/activities/delete/{gibberish(15)}",
                'type': 'Delete',
                'actor': current_user.profile_id(),
                'audience': post.community.profile_id(),
                'to': [post.community.profile_id(), 'https://www.w3.org/ns/activitystreams#Public'],
                'published': ap_datetime(utcnow()),
                'cc': [
                    current_user.followers_url()
                ],
                'object': post_reply.ap_id,
            }

            if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
                success = post_request(post.community.ap_inbox_url, delete_json, current_user.private_key,
                                       current_user.ap_profile_id + '#main-key')
                if not success:
                    flash('Failed to send delete to remote server', 'error')
            else:  # local community - send it to followers on remote instances
                announce = {
                    "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}",
                    "type": 'Announce',
                    "to": [
                        "https://www.w3.org/ns/activitystreams#Public"
                    ],
                    "actor": post.community.ap_profile_id,
                    "cc": [
                        post.community.ap_followers_url
                    ],
                    '@context': default_context(),
                    'object': delete_json
                }

                for instance in post.community.following_instances():
                    if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                        send_to_remote_instance(instance.id, post.community.id, announce)

    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/notification', methods=['GET', 'POST'])
@login_required
def post_notification(post_id: int):
    post = Post.query.get_or_404(post_id)
    if post.user_id == current_user.id:
        post.notify_author = not post.notify_author
        db.session.commit()
    return render_template('post/_post_notification_toggle.html', post=post)


@bp.route('/post_reply/<int:post_reply_id>/notification', methods=['GET', 'POST'])
@login_required
def post_reply_notification(post_reply_id: int):
    post_reply = PostReply.query.get_or_404(post_reply_id)
    if post_reply.user_id == current_user.id:
        post_reply.notify_author = not post_reply.notify_author
        db.session.commit()
    return render_template('post/_reply_notification_toggle.html', comment={'comment': post_reply})


@bp.route('/post/<int:post_id>/cross_posts', methods=['GET'])
def post_cross_posts(post_id: int):
    post = Post.query.get_or_404(post_id)
    cross_posts = Post.query.filter(Post.id.in_(post.cross_posts)).all()
    return render_template('post/post_cross_posts.html', post=post, cross_posts=cross_posts)
