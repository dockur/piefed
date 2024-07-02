from collections import namedtuple
from datetime import datetime, timedelta
from random import randint

from flask import redirect, url_for, flash, current_app, abort, request, g, make_response
from flask_login import login_user, logout_user, current_user, login_required
from flask_babel import _
from sqlalchemy import or_, desc
from wtforms import SelectField, RadioField

from app import db, constants, cache
from app.activitypub.signature import HttpSignature, post_request, default_context, post_request_in_background
from app.activitypub.util import notify_about_post_reply, inform_followers_of_post_update
from app.community.util import save_post, send_to_remote_instance
from app.inoculation import inoculation
from app.post.forms import NewReplyForm, ReportPostForm, MeaCulpaForm
from app.community.forms import CreateLinkForm, CreateImageForm, CreateDiscussionForm, CreateVideoForm, CreatePollForm
from app.post.util import post_replies, get_comment_branch, post_reply_count, tags_to_string, url_needs_archive, \
    generate_archive_link, body_has_no_archive_link
from app.constants import SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR, POST_TYPE_LINK, \
    POST_TYPE_IMAGE, \
    POST_TYPE_ARTICLE, POST_TYPE_VIDEO, NOTIF_REPLY, NOTIF_POST, POST_TYPE_POLL
from app.models import Post, PostReply, \
    PostReplyVote, PostVote, Notification, utcnow, UserBlock, DomainBlock, InstanceBlock, Report, Site, Community, \
    Topic, User, Instance, NotificationSubscription, UserFollower, Poll, PollChoice, PollChoiceVote, PostBookmark, \
    PostReplyBookmark
from app.post import bp
from app.utils import get_setting, render_template, allowlist_html, markdown_to_html, validation_required, \
    shorten_string, markdown_to_text, gibberish, ap_datetime, return_304, \
    request_etag_matches, ip_address, user_ip_banned, instance_banned, can_downvote, can_upvote, post_ranking, \
    reply_already_exists, reply_is_just_link_to_gif_reaction, confidence, moderating_communities, joined_communities, \
    blocked_instances, blocked_domains, community_moderators, blocked_phrases, show_ban_message, recently_upvoted_posts, \
    recently_downvoted_posts, recently_upvoted_post_replies, recently_downvoted_post_replies, reply_is_stupid, \
    languages_for_form, english_language_id, MultiCheckboxField, menu_topics


def show_post(post_id: int):
    post = Post.query.get_or_404(post_id)
    community: Community = post.community

    if community.banned or post.deleted:
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
    form.language_id.choices = languages_for_form()
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

        # respond to comments that are just variants of 'this'
        if reply_is_stupid(form.body.data):
            existing_vote = PostVote.query.filter_by(user_id=current_user.id, post_id=post.id).first()
            if existing_vote is None:
                flash(_('We have upvoted the post for you.'), 'warning')
                post_vote(post.id, 'upvote')
            else:
                flash(_('You have already upvoted the post, you do not need to say "this" also.'), 'error')
            return redirect(url_for('activitypub.post_ap', post_id=post_id))

        reply = PostReply(user_id=current_user.id, post_id=post.id, community_id=community.id, body=form.body.data,
                          body_html=markdown_to_html(form.body.data), body_html_safe=True,
                          from_bot=current_user.bot, nsfw=post.nsfw, nsfl=post.nsfl,
                          notify_author=form.notify_author.data, language_id=form.language_id.data, instance_id=1)

        post.last_active = community.last_active = utcnow()
        post.reply_count += 1
        community.post_reply_count += 1
        current_user.language_id = form.language_id.data

        db.session.add(reply)
        db.session.commit()

        notify_about_post_reply(None, reply)

        # Subscribe to own comment
        if form.notify_author.data:
            new_notification = NotificationSubscription(name=shorten_string(_('Replies to my comment on %(post_title)s',
                                                                              post_title=post.title), 50),
                                                        user_id=current_user.id, entity_id=reply.id,
                                                        type=NOTIF_REPLY)
            db.session.add(new_notification)
            db.session.commit()

        # upvote own reply
        reply.score = 1
        reply.up_votes = 1
        reply.ranking = confidence(1, 0)
        vote = PostReplyVote(user_id=current_user.id, post_reply_id=reply.id, author_id=current_user.id, effect=1)
        db.session.add(vote)
        cache.delete_memoized(recently_upvoted_post_replies, current_user.id)

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

        # federation
        reply_json = {
            'type': 'Note',
            'id': reply.public_url(),
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
            'published': ap_datetime(utcnow()),
            'distinguished': False,
            'audience': community.public_url(),
            'tag': [{
                'href': post.author.public_url(),
                'name': post.author.mention_tag(),
                'type': 'Mention'
            }],
            'language': {
                'identifier': reply.language_code(),
                'name': reply.language_name()
            }
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
                                                       current_user.public_url() + '#main-key')
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
                                                       current_user.public_url() + '#main-key')
                if not success:
                    # sending to shared inbox is good enough for Mastodon, but Lemmy will reject it the local community has no followers
                    personal_inbox = post.author.public_url() + '/inbox'
                    post_request(personal_inbox, create_json, current_user.private_key,
                                                       current_user.public_url() + '#main-key')

        return redirect(url_for('activitypub.post_ap', post_id=post_id))  # redirect to current page to avoid refresh resubmitting the form
    else:
        replies = post_replies(post.id, sort)
        form.notify_author.data = True

    og_image = post.image.source_url if post.image_id else None
    description = shorten_string(markdown_to_text(post.body), 150) if post.body else None

    # Breadcrumbs
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

    # Voting history
    if current_user.is_authenticated:
        recently_upvoted = recently_upvoted_posts(current_user.id)
        recently_downvoted = recently_downvoted_posts(current_user.id)
        recently_upvoted_replies = recently_upvoted_post_replies(current_user.id)
        recently_downvoted_replies = recently_downvoted_post_replies(current_user.id)
        reply_collapse_threshold = current_user.reply_collapse_threshold if current_user.reply_collapse_threshold else -1000
    else:
        recently_upvoted = []
        recently_downvoted = []
        recently_upvoted_replies = []
        recently_downvoted_replies = []
        reply_collapse_threshold = -10

    # Polls
    poll_form = False
    poll_results = False
    poll_choices = []
    poll_data = None
    poll_total_votes = 0
    if post.type == POST_TYPE_POLL:
        poll_data = Poll.query.get(post.id)
        if poll_data:
            poll_choices = PollChoice.query.filter_by(post_id=post.id).order_by(PollChoice.sort_order).all()
            poll_total_votes = poll_data.total_votes()
            # Show poll results to everyone after the poll finishes, to the poll creator and to those who have voted
            if (current_user.is_authenticated and (poll_data.has_voted(current_user.id))) \
                    or poll_data.end_poll < datetime.utcnow():
                poll_results = True
            else:
                poll_form = True

    # Archive.ph link
    archive_link = None
    if post.type == POST_TYPE_LINK and body_has_no_archive_link(post.body_html) and url_needs_archive(post.url):
        archive_link = generate_archive_link(post.url)

    response = render_template('post/post.html', title=post.title, post=post, is_moderator=is_moderator, community=post.community,
                           breadcrumbs=breadcrumbs, related_communities=related_communities, mods=mod_list,
                           poll_form=poll_form, poll_results=poll_results, poll_data=poll_data, poll_choices=poll_choices, poll_total_votes=poll_total_votes,
                           canonical=post.ap_id, form=form, replies=replies, THREAD_CUTOFF_DEPTH=constants.THREAD_CUTOFF_DEPTH,
                           description=description, og_image=og_image, POST_TYPE_IMAGE=constants.POST_TYPE_IMAGE,
                           POST_TYPE_LINK=constants.POST_TYPE_LINK, POST_TYPE_ARTICLE=constants.POST_TYPE_ARTICLE,
                           POST_TYPE_VIDEO=constants.POST_TYPE_VIDEO, POST_TYPE_POLL=constants.POST_TYPE_POLL,
                           autoplay=request.args.get('autoplay', False), archive_link=archive_link,
                           noindex=not post.author.indexable, preconnect=post.url if post.url else None,
                           recently_upvoted=recently_upvoted, recently_downvoted=recently_downvoted,
                           recently_upvoted_replies=recently_upvoted_replies, recently_downvoted_replies=recently_downvoted_replies,
                           reply_collapse_threshold=reply_collapse_threshold,
                           etag=f"{post.id}{sort}_{hash(post.last_active)}", markdown_editor=current_user.is_authenticated and current_user.markdown_editor,
                           low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1', SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)]
                           )
    response.headers.set('Vary', 'Accept, Cookie, Accept-Language')
    return response


@bp.route('/post/<int:post_id>/<vote_direction>', methods=['GET', 'POST'])
@login_required
@validation_required
def post_vote(post_id: int, vote_direction):
    post = Post.query.get_or_404(post_id)
    response_style = request.args.get('style', '')
    existing_vote = PostVote.query.filter_by(user_id=current_user.id, post_id=post.id).first()
    undo = None
    if existing_vote:
        if not post.community.low_quality:
            post.author.reputation -= existing_vote.effect
        if existing_vote.effect > 0:  # previous vote was up
            if vote_direction == 'upvote':  # new vote is also up, so remove it
                db.session.delete(existing_vote)
                post.up_votes -= 1
                post.score -= 1
                undo = 'Like'
            else:  # new vote is down while previous vote was up, so reverse their previous vote
                existing_vote.effect = -1
                post.up_votes -= 1
                post.down_votes += 1
                post.score -= 2
        else:  # previous vote was down
            if vote_direction == 'downvote':  # new vote is also down, so remove it
                db.session.delete(existing_vote)
                post.down_votes -= 1
                post.score += 1
                undo = 'Dislike'
            else:  # new vote is up while previous vote was down, so reverse their previous vote
                existing_vote.effect = 1
                post.up_votes += 1
                post.down_votes -= 1
                post.score += 2
    else:
        if vote_direction == 'upvote':
            effect = 1
            post.up_votes += 1
            # Make 'hot' sort more spicy by amplifying the effect of early upvotes
            if post.up_votes + post.down_votes <= 10:
                post.score += current_app.config['SPICY_UNDER_10']
            elif post.up_votes + post.down_votes <= 30:
                post.score += current_app.config['SPICY_UNDER_30']
            elif post.up_votes + post.down_votes <= 60:
                post.score += current_app.config['SPICY_UNDER_60']
            else:
                post.score += 1
        else:
            effect = -1
            post.down_votes += 1
            if post.up_votes + post.down_votes <= 30:
                post.score -= current_app.config['SPICY_UNDER_30']
            elif post.up_votes + post.down_votes <= 60:
                post.score -= current_app.config['SPICY_UNDER_60']
            else:
                post.score -= 1
        vote = PostVote(user_id=current_user.id, post_id=post.id, author_id=post.author.id,
                             effect=effect)
        # upvotes do not increase reputation in low quality communities
        if post.community.low_quality and effect > 0:
            effect = 0
        post.author.reputation += effect
        db.session.add(vote)

    if not post.community.local_only:
        if undo:
            action_json = {
                'actor': current_user.public_url(),
                'type': 'Undo',
                'id': f"https://{current_app.config['SERVER_NAME']}/activities/undo/{gibberish(15)}",
                'audience': post.community.public_url(),
                'object': {
                    'actor': current_user.public_url(),
                    'object': post.public_url(),
                    'type': undo,
                    'id': f"https://{current_app.config['SERVER_NAME']}/activities/{undo.lower()}/{gibberish(15)}",
                    'audience': post.community.public_url()
                }
            }
        else:
            action_type = 'Like' if vote_direction == 'upvote' else 'Dislike'
            action_json = {
                'actor': current_user.public_url(),
                'object': post.profile_id(),
                'type': action_type,
                'id': f"https://{current_app.config['SERVER_NAME']}/activities/{action_type.lower()}/{gibberish(15)}",
                'audience': post.community.public_url()
            }
        if post.community.is_local():
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
                    'object': action_json
            }
            for instance in post.community.following_instances():
                if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                    send_to_remote_instance(instance.id, post.community.id, announce)
        else:
            success = post_request_in_background(post.community.ap_inbox_url, action_json, current_user.private_key,
                                                            current_user.public_url() + '#main-key')
            if not success:
                flash('Failed to send vote', 'warning')

    current_user.last_seen = utcnow()
    current_user.ip_address = ip_address()
    if not current_user.banned:
        post.ranking = post_ranking(post.score, post.created_at)
        db.session.commit()
        current_user.recalculate_attitude()
        db.session.commit()

    recently_upvoted = []
    recently_downvoted = []
    if vote_direction == 'upvote' and undo is None:
        recently_upvoted = [post_id]
    elif vote_direction == 'downvote' and undo is None:
        recently_downvoted = [post_id]
    cache.delete_memoized(recently_upvoted_posts, current_user.id)
    cache.delete_memoized(recently_downvoted_posts, current_user.id)

    if response_style == 'button_big':
        return render_template('post/_post_voting_buttons_big.html', post=post, community=post.community,
                               recently_upvoted=recently_upvoted, recently_downvoted=recently_downvoted)
    elif response_style == 'masonry':
        return render_template('post/_post_voting_buttons_masonry.html', post=post, community=post.community,
                               recently_upvoted=recently_upvoted, recently_downvoted=recently_downvoted)
    else:
        if vote_direction == 'downvote':
            return str(post.down_votes)
        else:
            return str(post.up_votes)


@bp.route('/comment/<int:comment_id>/<vote_direction>', methods=['POST'])
@login_required
@validation_required
def comment_vote(comment_id, vote_direction):
    comment = PostReply.query.get_or_404(comment_id)
    response_style = request.args.get('style', '')
    existing_vote = PostReplyVote.query.filter_by(user_id=current_user.id, post_reply_id=comment.id).first()
    undo = None
    if existing_vote:
        if existing_vote.effect > 0:  # previous vote was up
            if vote_direction == 'upvote':  # new vote is also up, so remove it
                db.session.delete(existing_vote)
                comment.up_votes -= 1
                comment.score -= 1
                undo = 'Like'
            else:  # new vote is down while previous vote was up, so reverse their previous vote
                existing_vote.effect = -1
                comment.up_votes -= 1
                comment.down_votes += 1
                comment.score -= 2
        else:  # previous vote was down
            if vote_direction == 'downvote':  # new vote is also down, so remove it
                db.session.delete(existing_vote)
                comment.down_votes -= 1
                comment.score += 1
                undo = 'Dislike'
            else:  # new vote is up while previous vote was down, so reverse their previous vote
                existing_vote.effect = 1
                comment.up_votes += 1
                comment.down_votes -= 1
                comment.score += 2
    else:
        if vote_direction == 'upvote':
            effect = 1
            comment.up_votes += 1
            comment.score += 1
        else:
            effect = -1
            comment.down_votes += 1
            comment.score -= 1
        vote = PostReplyVote(user_id=current_user.id, post_reply_id=comment_id, author_id=comment.author.id, effect=effect)
        comment.author.reputation += effect
        db.session.add(vote)

    if not comment.community.local_only:
        if undo:
            action_json = {
                'actor': current_user.public_url(),
                'type': 'Undo',
                'id': f"https://{current_app.config['SERVER_NAME']}/activities/undo/{gibberish(15)}",
                'audience': comment.community.public_url(),
                'object': {
                    'actor': current_user.public_url(),
                    'object': comment.public_url(),
                    'type': undo,
                    'id': f"https://{current_app.config['SERVER_NAME']}/activities/{undo.lower()}/{gibberish(15)}",
                    'audience': comment.community.public_url()
                }
            }
        else:
            action_type = 'Like' if vote_direction == 'upvote' else 'Dislike'
            action_json = {
                'actor': current_user.public_url(),
                'object': comment.public_url(),
                'type': action_type,
                'id': f"https://{current_app.config['SERVER_NAME']}/activities/{action_type.lower()}/{gibberish(15)}",
                'audience': comment.community.public_url()
            }
        if comment.community.is_local():
            announce = {
                    "id": f"https://{current_app.config['SERVER_NAME']}/activities/announce/{gibberish(15)}",
                    "type": 'Announce',
                    "to": [
                        "https://www.w3.org/ns/activitystreams#Public"
                    ],
                    "actor": comment.community.ap_profile_id,
                    "cc": [
                        comment.community.ap_followers_url
                    ],
                    '@context': default_context(),
                    'object': action_json
            }
            for instance in comment.community.following_instances():
                if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                    send_to_remote_instance(instance.id, comment.community.id, announce)
        else:
            success = post_request_in_background(comment.community.ap_inbox_url, action_json, current_user.private_key,
                                                            current_user.public_url() + '#main-key')
            if not success:
                flash('Failed to send vote', 'warning')

    current_user.last_seen = utcnow()
    current_user.ip_address = ip_address()
    comment.ranking = confidence(comment.up_votes, comment.down_votes)
    db.session.commit()
    current_user.recalculate_attitude()
    db.session.commit()

    recently_upvoted = []
    recently_downvoted = []
    if vote_direction == 'upvote' and undo is None:
        recently_upvoted = [comment_id]
    elif vote_direction == 'downvote' and undo is None:
        recently_downvoted = [comment_id]
    cache.delete_memoized(recently_upvoted_post_replies, current_user.id)
    cache.delete_memoized(recently_downvoted_post_replies, current_user.id)

    if response_style == 'htmx':
        return render_template('post/_comment_voting_buttons.html', comment=comment, community=comment.community,
                               recently_upvoted_replies=recently_upvoted, recently_downvoted_replies=recently_downvoted)
    else:
        if vote_direction == 'downvote':
            return str(comment.down_votes)
        else:
            return str(comment.up_votes)


@bp.route('/poll/<int:post_id>/vote', methods=['POST'])
@login_required
@validation_required
def poll_vote(post_id):
    poll_data = Poll.query.get_or_404(post_id)
    if poll_data.mode == 'single':
        choice_id = int(request.form.get('poll_choice'))
        poll_data.vote_for_choice(choice_id, current_user.id)
    else:
        for choice_id in request.form.getlist('poll_choice[]'):
            poll_data.vote_for_choice(int(choice_id), current_user.id)
    flash(_('Vote has been cast.'))

    post = Post.query.get(post_id)
    if post:
        poll_votes = PollChoice.query.join(PollChoiceVote, PollChoiceVote.choice_id == PollChoice.id).filter(PollChoiceVote.post_id == post.id, PollChoiceVote.user_id == current_user.id).all()
        for pv in poll_votes:
            if post.author.is_local():
                inform_followers_of_post_update(post.id, 1)
            else:
                pollvote_json = {
                  '@context': default_context(),
                  'actor': current_user.public_url(),
                  'id': f"https://{current_app.config['SERVER_NAME']}/activities/create/{gibberish(15)}",
                  'object': {
                    'attributedTo': current_user.public_url(),
                    'id': f"https://{current_app.config['SERVER_NAME']}/activities/vote/{gibberish(15)}",
                    'inReplyTo': post.profile_id(),
                    'name': pv.choice_text,
                    'to': post.author.public_url(),
                    'type': 'Note'
                  },
                  'to': post.author.public_url(),
                  'type': 'Create'
                }
                try:
                    post_request(post.author.ap_inbox_url, pollvote_json, current_user.private_key,
                                                          current_user.public_url() + '#main-key')
                except Exception:
                    pass

    return redirect(url_for('activitypub.post_ap', post_id=post_id))


@bp.route('/post/<int:post_id>/comment/<int:comment_id>')
def continue_discussion(post_id, comment_id):
    post = Post.query.get_or_404(post_id)
    comment = PostReply.query.get_or_404(comment_id)
    if post.community.banned or post.deleted or comment.deleted:
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
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site,
                           community=post.community,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
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
    form.language_id.choices = languages_for_form()
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

        if reply_is_stupid(form.body.data):
            existing_vote = PostReplyVote.query.filter_by(user_id=current_user.id, post_reply_id=in_reply_to.id).first()
            if existing_vote is None:
                flash(_('We have upvoted the comment for you.'), 'warning')
                comment_vote(in_reply_to.id, 'upvote')
            else:
                flash(_('You have already upvoted the comment, you do not need to say "this" also.'), 'error')
            if in_reply_to.depth <= constants.THREAD_CUTOFF_DEPTH:
                return redirect(url_for('activitypub.post_ap', post_id=post_id))
            else:
                return redirect(url_for('post.continue_discussion', post_id=post_id, comment_id=in_reply_to.parent_id))

        current_user.last_seen = utcnow()
        current_user.ip_address = ip_address()
        current_user.language_id = form.language_id.data
        reply = PostReply(user_id=current_user.id, post_id=post.id, parent_id=in_reply_to.id, depth=in_reply_to.depth + 1,
                          community_id=post.community.id, body=form.body.data,
                          body_html=markdown_to_html(form.body.data), body_html_safe=True,
                          from_bot=current_user.bot, nsfw=post.nsfw, nsfl=post.nsfl,
                          notify_author=form.notify_author.data, instance_id=1, language_id=form.language_id.data)
        if reply.body:
            for blocked_phrase in blocked_phrases():
                if blocked_phrase in reply.body:
                    abort(401)
        db.session.add(reply)
        db.session.commit()

        # Notify subscribers
        notify_about_post_reply(in_reply_to, reply)

        # Subscribe to own comment
        if form.notify_author.data:
            new_notification = NotificationSubscription(name=shorten_string(_('Replies to my comment on %(post_title)s',
                                                                              post_title=post.title), 50),
                                                        user_id=current_user.id, entity_id=reply.id,
                                                        type=NOTIF_REPLY)
            db.session.add(new_notification)

        # upvote own reply
        reply.score = 1
        reply.up_votes = 1
        reply.ranking = confidence(1, 0)
        vote = PostReplyVote(user_id=current_user.id, post_reply_id=reply.id, author_id=current_user.id, effect=1)
        db.session.add(vote)
        cache.delete_memoized(recently_upvoted_post_replies, current_user.id)

        reply.ap_id = reply.profile_id()
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

        # federation
        if not post.community.local_only:
            reply_json = {
                'type': 'Note',
                'id': reply.public_url(),
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
                                                           current_user.public_url() + '#main-key')
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
                                                           current_user.public_url() + '#main-key')
                    if not success:
                        # sending to shared inbox is good enough for Mastodon, but Lemmy will reject it the local community has no followers
                        personal_inbox = in_reply_to.author.public_url() + '/inbox'
                        post_request(personal_inbox, create_json, current_user.private_key,
                                                       current_user.public_url() + '#main-key')

        if reply.depth <= constants.THREAD_CUTOFF_DEPTH:
            return redirect(url_for('activitypub.post_ap', post_id=post_id, _anchor=f'comment_{reply.id}'))
        else:
            return redirect(url_for('post.continue_discussion', post_id=post_id, comment_id=reply.parent_id))
    else:
        form.notify_author.data = True

        return render_template('post/add_reply.html', title=_('Discussing %(title)s', title=post.title), post=post,
                               is_moderator=is_moderator, form=form, comment=in_reply_to, markdown_editor=current_user.is_authenticated and current_user.markdown_editor,
                               moderating_communities=moderating_communities(current_user.get_id()), mods=mod_list,
                               joined_communities = joined_communities(current_user.id), community=post.community,
                               SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                               inoculation=inoculation[randint(0, len(inoculation) - 1)])


@bp.route('/post/<int:post_id>/options', methods=['GET'])
def post_options(post_id: int):
    post = Post.query.get_or_404(post_id)
    if current_user.is_anonymous or not current_user.is_admin():
        if post.deleted:
            abort(404)

    existing_bookmark = []
    if current_user.is_authenticated:
        existing_bookmark = PostBookmark.query.filter(PostBookmark.post_id == post_id, PostBookmark.user_id == current_user.id).first()
    
    return render_template('post/post_options.html', post=post, existing_bookmark=existing_bookmark,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site)


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/options', methods=['GET'])
def post_reply_options(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    if current_user.is_anonymous or not current_user.is_admin():
        if post.deleted or post_reply.deleted:
            abort(404)

    existing_bookmark = []
    if current_user.is_authenticated:
        existing_bookmark = PostReplyBookmark.query.filter(PostReplyBookmark.post_reply_id == comment_id,
                                                      PostReplyBookmark.user_id == current_user.id).first()

    return render_template('post/post_reply_options.html', post=post, post_reply=post_reply,
                           existing_bookmark=existing_bookmark,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
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
    elif post.type == POST_TYPE_VIDEO:
        return redirect(url_for('post.post_edit_video_post', post_id=post_id))
    elif post.type == POST_TYPE_POLL:
        return redirect(url_for('post.post_edit_poll_post', post_id=post_id))
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

        form.language_id.choices = languages_for_form()

        if form.validate_on_submit():
            save_post(form, post, 'discussion')
            post.community.last_active = utcnow()
            post.edited_at = utcnow()
            db.session.commit()

            flash(_('Your changes have been saved.'), 'success')

            # federate edit
            if not post.community.local_only:
                federate_post_update(post)
            federate_post_edit_to_user_followers(post)

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.discussion_title.data = post.title
            form.discussion_body.data = post.body
            form.notify_author.data = post.notify_author
            form.nsfw.data = post.nsfw
            form.nsfl.data = post.nsfl
            form.sticky.data = post.sticky
            form.language_id.data = post.language_id
            form.tags.data = tags_to_string(post)
            if not (post.community.is_moderator() or post.community.is_owner() or current_user.is_admin()):
                form.sticky.render_kw = {'disabled': True}
            return render_template('post/post_edit_discussion.html', title=_('Edit post'), form=form, post=post,
                                   markdown_editor=current_user.markdown_editor, mods=mod_list,
                                   moderating_communities=moderating_communities(current_user.get_id()),
                                   joined_communities=joined_communities(current_user.get_id()),
                                   menu_topics=menu_topics(), site=g.site,
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

        form.language_id.choices = languages_for_form()

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

            flash(_('Your changes have been saved.'), 'success')
            # federate edit

            if not post.community.local_only:
                federate_post_update(post)
            federate_post_edit_to_user_followers(post)

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.image_title.data = post.title
            form.image_body.data = post.body
            form.image_alt_text.data = post.image.alt_text
            form.notify_author.data = post.notify_author
            form.nsfw.data = post.nsfw
            form.nsfl.data = post.nsfl
            form.sticky.data = post.sticky
            form.language_id.data = post.language_id
            form.tags.data = tags_to_string(post)
            if not (post.community.is_moderator() or post.community.is_owner() or current_user.is_admin()):
                form.sticky.render_kw = {'disabled': True}
            return render_template('post/post_edit_image.html', title=_('Edit post'), form=form, post=post,
                                   markdown_editor=current_user.markdown_editor, mods=mod_list,
                                   moderating_communities=moderating_communities(current_user.get_id()),
                                   joined_communities=joined_communities(current_user.get_id()),
                                   menu_topics=menu_topics(), site=g.site,
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

        form.language_id.choices = languages_for_form()

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

            flash(_('Your changes have been saved.'), 'success')
            # federate edit

            if not post.community.local_only:
                federate_post_update(post)
            federate_post_edit_to_user_followers(post)

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.link_title.data = post.title
            form.link_body.data = post.body
            form.link_url.data = post.url
            form.notify_author.data = post.notify_author
            form.nsfw.data = post.nsfw
            form.nsfl.data = post.nsfl
            form.sticky.data = post.sticky
            form.language_id.data = post.language_id
            form.tags.data = tags_to_string(post)
            if not (post.community.is_moderator() or post.community.is_owner() or current_user.is_admin()):
                form.sticky.render_kw = {'disabled': True}
            return render_template('post/post_edit_link.html', title=_('Edit post'), form=form, post=post,
                                   markdown_editor=current_user.markdown_editor, mods=mod_list,
                                   moderating_communities=moderating_communities(current_user.get_id()),
                                   joined_communities=joined_communities(current_user.get_id()),
                                   menu_topics=menu_topics(), site=g.site,
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)]
                                   )
    else:
        abort(401)


@bp.route('/post/<int:post_id>/edit_video', methods=['GET', 'POST'])
@login_required
def post_edit_video_post(post_id: int):
    post = Post.query.get_or_404(post_id)
    form = CreateVideoForm()
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

        form.language_id.choices = languages_for_form()

        if form.validate_on_submit():
            save_post(form, post, 'video')
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

            flash(_('Your changes have been saved.'), 'success')
            # federate edit

            if not post.community.local_only:
                federate_post_update(post)
            federate_post_edit_to_user_followers(post)

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.video_title.data = post.title
            form.video_body.data = post.body
            form.video_url.data = post.url
            form.notify_author.data = post.notify_author
            form.nsfw.data = post.nsfw
            form.nsfl.data = post.nsfl
            form.sticky.data = post.sticky
            form.language_id.data = post.language_id
            form.tags.data = tags_to_string(post)
            if not (post.community.is_moderator() or post.community.is_owner() or current_user.is_admin()):
                form.sticky.render_kw = {'disabled': True}
            return render_template('post/post_edit_video.html', title=_('Edit post'), form=form, post=post,
                                   markdown_editor=current_user.markdown_editor, mods=mod_list,
                                   moderating_communities=moderating_communities(current_user.get_id()),
                                   joined_communities=joined_communities(current_user.get_id()),
                                   menu_topics=menu_topics(), site=g.site,
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)]
                                   )
    else:
        abort(401)


@bp.route('/post/<int:post_id>/edit_poll', methods=['GET', 'POST'])
@login_required
def post_edit_poll_post(post_id: int):
    post = Post.query.get_or_404(post_id)
    poll = Poll.query.filter_by(post_id=post_id).first()
    form = CreatePollForm()
    del form.communities
    del form.finish_in

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

        form.language_id.choices = languages_for_form()

        if form.validate_on_submit():
            save_post(form, post, 'poll')
            post.community.last_active = utcnow()
            post.edited_at = utcnow()
            db.session.commit()

            flash(_('Your changes have been saved.'), 'success')

            # federate edit
            if not post.community.local_only and not poll.local_only:
                federate_post_update(post)
                federate_post_edit_to_user_followers(post)

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.poll_title.data = post.title
            form.poll_body.data = post.body
            form.notify_author.data = post.notify_author
            form.nsfw.data = post.nsfw
            form.nsfl.data = post.nsfl
            form.sticky.data = post.sticky
            form.language_id.data = post.language_id
            poll = Poll.query.filter_by(post_id=post.id).first()
            form.mode.data = poll.mode
            form.local_only.data = poll.local_only
            i = 1
            for choice in PollChoice.query.filter_by(post_id=post.id).order_by(PollChoice.sort_order).all():
                form_field = getattr(form, f"choice_{i}")
                form_field.data = choice.choice_text
                i += 1
            form.tags.data = tags_to_string(post)
            if not (post.community.is_moderator() or post.community.is_owner() or current_user.is_admin()):
                form.sticky.render_kw = {'disabled': True}
            return render_template('post/post_edit_poll.html', title=_('Edit post'), form=form, post=post,
                                   markdown_editor=current_user.markdown_editor, mods=mod_list,
                                   moderating_communities=moderating_communities(current_user.get_id()),
                                   joined_communities=joined_communities(current_user.get_id()),
                                   menu_topics=menu_topics(), site=g.site,
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
        'attachment': [],
        'commentsEnabled': post.comments_enabled,
        'sensitive': post.nsfw,
        'nsfl': post.nsfl,
        'stickied': post.sticky,
        'published': ap_datetime(post.posted_at),
        'updated': ap_datetime(post.edited_at),
        'audience': post.community.ap_profile_id,
        'language': {
            'identifier': post.language_code(),
            'name': post.language_name()
        },
        'tag': post.tags_for_activitypub()
    }
    update_json = {
        'id': f"https://{current_app.config['SERVER_NAME']}/activities/update/{gibberish(15)}",
        'type': 'Update',
        'actor': current_user.public_url(),
        'audience': post.community.public_url(),
        'to': [post.community.public_url(), 'https://www.w3.org/ns/activitystreams#Public'],
        'published': ap_datetime(utcnow()),
        'cc': [
            current_user.followers_url()
        ],
        'object': page_json,
    }
    if post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO:
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
    if post.type == POST_TYPE_POLL:
        poll = Poll.query.filter_by(post_id=post.id).first()
        page_json['type'] = 'Question'
        page_json['endTime'] = ap_datetime(poll.end_poll)
        page_json['votersCount'] = 0
        choices = []
        for choice in PollChoice.query.filter_by(post_id=post.id).all():
            choices.append({
                "type": "Note",
                "name": choice.choice_text,
                "replies": {
                  "type": "Collection",
                  "totalItems": 0
                }
            })
        page_json['oneOf' if poll.mode == 'single' else 'anyOf'] = choices

    if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
        success = post_request(post.community.ap_inbox_url, update_json, current_user.private_key,
                               current_user.public_url() + '#main-key')
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


def federate_post_edit_to_user_followers(post):
    followers = UserFollower.query.filter_by(local_user_id=post.user_id)
    if not followers:
        return

    note = {
        'type': 'Note',
        'id': post.ap_id,
        'inReplyTo': None,
        'attributedTo': current_user.ap_profile_id,
        'to': [
            'https://www.w3.org/ns/activitystreams#Public'
        ],
        'cc': [
            current_user.ap_followers_url
        ],
        'content': '',
        'mediaType': 'text/html',
        'attachment': [],
        'commentsEnabled': post.comments_enabled,
        'sensitive': post.nsfw,
        'nsfl': post.nsfl,
        'stickied': post.sticky,
        'published': ap_datetime(utcnow()),
        'updated': ap_datetime(post.edited_at),
        'language': {
            'identifier': post.language_code(),
            'name': post.language_name()
        },
        'tag': post.tags_for_activitypub()
    }
    update = {
        "id": f"https://{current_app.config['SERVER_NAME']}/activities/create/{gibberish(15)}",
        "actor": current_user.ap_profile_id,
        "to": [
            "https://www.w3.org/ns/activitystreams#Public"
        ],
        "cc": [
            current_user.ap_followers_url
        ],
        "type": "Update",
        "object": note,
        '@context': default_context()
    }
    if post.type == POST_TYPE_ARTICLE:
        note['content'] = '<p>' + post.title + '</p>'
    elif post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO:
        note['content'] = '<p><a href=' + post.url + '>' + post.title + '</a></p>'
    elif post.type == POST_TYPE_IMAGE:
        note['content'] = '<p>' + post.title + '</p>'
        if post.image_id and post.image.source_url:
            if post.image.alt_text:
                note['attachment'] = [{'type': 'Document', 'url': post.image.source_url, 'name': post.image.alt_text}]
            else:
                note['attachment'] = [{'type': 'Document', 'url': post.image.source_url}]
    elif post.type == POST_TYPE_POLL:
        poll = Poll.query.filter_by(post_id=post.id).first()
        note['type'] = 'Question'
        note['endTime'] = ap_datetime(poll.end_poll)
        note['votersCount'] = 0
        choices = []
        for choice in PollChoice.query.filter_by(post_id=post.id).all():
            choices.append({
                "type": "Note",
                "name": choice.choice_text,
                "replies": {
                    "type": "Collection",
                    "totalItems": 0
                }
            })
        note['oneOf' if poll.mode == 'single' else 'anyOf'] = choices

    if post.body_html:
        note['content'] = note['content'] + '<p>' + post.body_html + '</p>'

    instances = Instance.query.join(User, User.instance_id == Instance.id).join(UserFollower, UserFollower.remote_user_id == User.id)
    instances = instances.filter(UserFollower.local_user_id == post.user_id)
    for i in instances:
        post_request(i.inbox, update, current_user.private_key, current_user.public_url() + '#main-key')


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
        post.deleted = True
        g.site.last_active = community.last_active = utcnow()
        db.session.commit()
        flash(_('Post deleted.'))

        delete_json = {
            'id': f"https://{current_app.config['SERVER_NAME']}/activities/delete/{gibberish(15)}",
            'type': 'Delete',
            'actor': current_user.public_url(),
            'audience': post.community.public_url(),
            'to': [post.community.public_url(), 'https://www.w3.org/ns/activitystreams#Public'],
            'published': ap_datetime(utcnow()),
            'cc': [
                current_user.followers_url()
            ],
            'object': post.ap_id,
            'uri': post.ap_id
        }
        if post.user_id != current_user.id:
            delete_json['summary'] = 'Deleted by mod'

        if not community.local_only:
            if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
                success = post_request(post.community.ap_inbox_url, delete_json, current_user.private_key,
                                       current_user.public_url() + '#main-key')
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

        followers = UserFollower.query.filter_by(local_user_id=post.user_id)
        if followers:
            instances = Instance.query.join(User, User.instance_id == Instance.id).join(UserFollower, UserFollower.remote_user_id == User.id)
            instances = instances.filter(UserFollower.local_user_id == post.user_id)
            for i in instances:
                post_request(i.inbox, delete_json, current_user.private_key, current_user.public_url() + '#main-key')

    return redirect(url_for('activitypub.community_profile', actor=community.ap_id if community.ap_id is not None else community.name))


@bp.route('/post/<int:post_id>/restore', methods=['GET', 'POST'])
@login_required
def post_restore(post_id: int):
    post = Post.query.get_or_404(post_id)
    if post.community.is_moderator() or post.community.is_owner() or current_user.is_admin():
        post.deleted = False
        db.session.commit()

        # Federate un-delete
        if post.is_local():
            delete_json = {
              "actor": current_user.public_url(),
              "to": ["https://www.w3.org/ns/activitystreams#Public"],
              "object": {
                  'id': f"https://{current_app.config['SERVER_NAME']}/activities/delete/{gibberish(15)}",
                  'type': 'Delete',
                  'actor': current_user.public_url(),
                  'audience': post.community.public_url(),
                  'to': [post.community.public_url(), 'https://www.w3.org/ns/activitystreams#Public'],
                  'published': ap_datetime(utcnow()),
                  'cc': [
                      current_user.followers_url()
                  ],
                  'object': post.ap_id,
                  'uri': post.ap_id,
                  "summary": "bad post",
              },
              "cc": [post.community.public_url()],
              "audience": post.author.public_url(),
              "type": "Undo",
              "id": f"https://{current_app.config['SERVER_NAME']}/activities/undo/{gibberish(15)}"
            }

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
                'object': delete_json
            }

            for instance in post.community.following_instances():
                if instance.inbox and not current_user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                    send_to_remote_instance(instance.id, post.community.id, announce)

        flash(_('Post has been restored.'))
    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/bookmark', methods=['GET', 'POST'])
@login_required
def post_bookmark(post_id: int):
    post = Post.query.get_or_404(post_id)
    if post.deleted:
        abort(404)
    existing_bookmark = PostBookmark.query.filter(PostBookmark.post_id == post_id, PostBookmark.user_id == current_user.id).first()
    if not existing_bookmark:
        db.session.add(PostBookmark(post_id=post_id, user_id=current_user.id))
        db.session.commit()
        flash(_('Bookmark added.'))
    else:
        flash(_('This post has already been bookmarked.'))
    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/remove_bookmark', methods=['GET', 'POST'])
@login_required
def post_remove_bookmark(post_id: int):
    post = Post.query.get_or_404(post_id)
    if post.deleted:
        abort(404)
    existing_bookmark = PostBookmark.query.filter(PostBookmark.post_id == post_id, PostBookmark.user_id == current_user.id).first()
    if existing_bookmark:
        db.session.delete(existing_bookmark)
        db.session.commit()
        flash(_('Bookmark has been removed.'))
    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/remove_bookmark', methods=['GET', 'POST'])
@login_required
def post_reply_remove_bookmark(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)

    if post.deleted or post_reply.deleted:
        abort(404)
    existing_bookmark = PostReplyBookmark.query.filter(PostReplyBookmark.post_reply_id == comment_id, PostReplyBookmark.user_id == current_user.id).first()
    if existing_bookmark:
        db.session.delete(existing_bookmark)
        db.session.commit()
        flash(_('Bookmark has been removed.'))
    return redirect(url_for('activitypub.post_ap', post_id=post.id))


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
              "actor": current_user.public_url(),
              "audience": post.community.public_url(),
              "content": None,
              "id": f"https://{current_app.config['SERVER_NAME']}/activities/flag/{gibberish(15)}",
              "object": post.ap_id,
              "summary": summary,
              "to": [
                post.community.public_url()
              ],
              "type": "Flag"
            }
            instance = Instance.query.get(post.community.instance_id)
            if post.community.ap_inbox_url and not current_user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                success = post_request(post.community.ap_inbox_url, report_json, current_user.private_key,
                                       current_user.public_url() + '#main-key')
                if not success:
                    flash('Failed to send report to remote server', 'error')

        flash(_('Post has been reported, thank you!'))
        return redirect(post.community.local_url())
    elif request.method == 'GET':
        form.report_remote.data = True

    return render_template('post/post_report.html', title=_('Report post'), form=form, post=post,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
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
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
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
                "actor": current_user.public_url(),
                "audience": post.community.public_url(),
                "content": None,
                "id": f"https://{current_app.config['SERVER_NAME']}/activities/flag/{gibberish(15)}",
                "object": post_reply.ap_id,
                "summary": summary,
                "to": [
                    post.community.public_url()
                ],
                "type": "Flag"
            }
            instance = Instance.query.get(post.community.instance_id)
            if post.community.ap_inbox_url and not current_user.has_blocked_instance(
                    instance.id) and not instance_banned(instance.domain):
                success = post_request(post.community.ap_inbox_url, report_json, current_user.private_key,
                                       current_user.public_url() + '#main-key')
                if not success:
                    flash('Failed to send report to remote server', 'error')

        flash(_('Comment has been reported, thank you!'))
        return redirect(url_for('activitypub.post_ap', post_id=post.id))
    elif request.method == 'GET':
        form.report_remote.data = True

    return render_template('post/post_reply_report.html', title=_('Report comment'), form=form, post=post, post_reply=post_reply,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           menu_topics=menu_topics(), site=g.site
                           )


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/bookmark', methods=['GET'])
@login_required
def post_reply_bookmark(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)

    if post.deleted or post_reply.deleted:
        abort(404)
    existing_bookmark = PostReplyBookmark.query.filter(PostReplyBookmark.post_reply_id == comment_id,
                                                       PostReplyBookmark.user_id == current_user.id).first()
    if not existing_bookmark:
        db.session.add(PostReplyBookmark(post_reply_id=comment_id, user_id=current_user.id))
        db.session.commit()
        flash(_('Bookmark added.'))
    else:
        flash(_('This comment has already been bookmarked.'))
    return redirect(url_for('activitypub.post_ap', post_id=post.id, _anchor=f'comment_{comment_id}'))


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
    form.language_id.choices = languages_for_form()
    if post_reply.user_id == current_user.id or post.community.is_moderator():
        if form.validate_on_submit():
            post_reply.body = form.body.data
            post_reply.body_html = markdown_to_html(form.body.data)
            post_reply.notify_author = form.notify_author.data
            post.community.last_active = utcnow()
            post_reply.edited_at = utcnow()
            post_reply.language_id = form.language_id.data
            db.session.commit()
            flash(_('Your changes have been saved.'), 'success')

            if post_reply.parent_id:
                in_reply_to = PostReply.query.get(post_reply.parent_id)
            else:
                in_reply_to = post
            # federate edit
            if not post.community.local_only:
                reply_json = {
                    'type': 'Note',
                    'id': post_reply.public_url(),
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
                    'url': post_reply.public_url(),
                    'mediaType': 'text/html',
                    'published': ap_datetime(post_reply.posted_at),
                    'updated': ap_datetime(post_reply.edited_at),
                    'distinguished': False,
                    'audience': post.community.public_url(),
                    'contentMap': {
                        'en': post_reply.body_html
                    },
                    'language': {
                        'identifier': post_reply.language_code(),
                        'name': post_reply.language_name()
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
                                                               current_user.public_url() + '#main-key')
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
                                                               current_user.public_url() + '#main-key')
                        if not success:
                            # sending to shared inbox is good enough for Mastodon, but Lemmy will reject it the local community has no followers
                            personal_inbox = in_reply_to.author.public_url() + '/inbox'
                            post_request(personal_inbox, update_json, current_user.private_key,
                                                           current_user.public_url() + '#main-key')

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.body.data = post_reply.body
            form.notify_author.data = post_reply.notify_author
            form.language_id.data = post_reply.language_id
            return render_template('post/post_reply_edit.html', title=_('Edit comment'), form=form, post=post, post_reply=post_reply,
                                   comment=comment, markdown_editor=current_user.markdown_editor, moderating_communities=moderating_communities(current_user.get_id()),
                                   joined_communities=joined_communities(current_user.get_id()), menu_topics=menu_topics(),
                                   community=post.community, site=g.site,
                                   SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)])
    else:
        abort(401)


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/delete', methods=['GET', 'POST'])
@login_required
def post_reply_delete(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    community = post.community
    if post_reply.user_id == current_user.id or community.is_moderator() or current_user.is_admin():
        if post_reply.has_replies():
            post_reply.body = 'Deleted by author' if post_reply.author.id == current_user.id else 'Deleted by moderator'
            post_reply.body_html = markdown_to_html(post_reply.body)
        else:
            post_reply.delete_dependencies()
            post_reply.deleted = True
        g.site.last_active = community.last_active = utcnow()
        db.session.commit()
        flash(_('Comment deleted.'))
        # federate delete
        if not post.community.local_only:
            delete_json = {
                'id': f"https://{current_app.config['SERVER_NAME']}/activities/delete/{gibberish(15)}",
                'type': 'Delete',
                'actor': current_user.public_url(),
                'audience': post.community.public_url(),
                'to': [post.community.public_url(), 'https://www.w3.org/ns/activitystreams#Public'],
                'published': ap_datetime(utcnow()),
                'cc': [
                    current_user.followers_url()
                ],
                'object': post_reply.ap_id,
            }
            if post_reply.user_id != current_user.id:
                delete_json['summary'] = 'Deleted by mod'

            if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
                success = post_request(post.community.ap_inbox_url, delete_json, current_user.private_key,
                                       current_user.public_url() + '#main-key')
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
    # Toggle whether the current user is subscribed to notifications about top-level replies to this post or not
    post = Post.query.get_or_404(post_id)
    existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == post.id,
                                                                  NotificationSubscription.user_id == current_user.id,
                                                                  NotificationSubscription.type == NOTIF_POST).first()
    if existing_notification:
        db.session.delete(existing_notification)
        db.session.commit()
    else:  # no subscription yet, so make one
        new_notification = NotificationSubscription(name=shorten_string(_('Replies to my post %(post_title)s',
                                                                              post_title=post.title)),
                                                    user_id=current_user.id, entity_id=post.id,
                                                    type=NOTIF_POST)
        db.session.add(new_notification)
        db.session.commit()

    return render_template('post/_post_notification_toggle.html', post=post)


@bp.route('/post_reply/<int:post_reply_id>/notification', methods=['GET', 'POST'])
@login_required
def post_reply_notification(post_reply_id: int):
    # Toggle whether the current user is subscribed to notifications about replies to this reply or not
    post_reply = PostReply.query.get_or_404(post_reply_id)
    existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == post_reply.id,
                                                                  NotificationSubscription.user_id == current_user.id,
                                                                  NotificationSubscription.type == NOTIF_REPLY).first()
    if existing_notification:
        db.session.delete(existing_notification)
        db.session.commit()
    else:  # no subscription yet, so make one
        new_notification = NotificationSubscription(name=shorten_string(_('Replies to my comment on %(post_title)s',
                                                                              post_title=post_reply.post.title)), user_id=current_user.id, entity_id=post_reply.id,
                                                    type=NOTIF_REPLY)
        db.session.add(new_notification)
        db.session.commit()

    return render_template('post/_reply_notification_toggle.html', comment={'comment': post_reply})


@bp.route('/post/<int:post_id>/cross_posts', methods=['GET'])
def post_cross_posts(post_id: int):
    post = Post.query.get_or_404(post_id)
    cross_posts = Post.query.filter(Post.id.in_(post.cross_posts)).all()
    return render_template('post/post_cross_posts.html', post=post, cross_posts=cross_posts)
