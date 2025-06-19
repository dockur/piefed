from __future__ import annotations

from collections import namedtuple, defaultdict
from datetime import datetime, timedelta
from random import randint

from flask import redirect, url_for, flash, current_app, abort, request, g, make_response, jsonify
from flask_login import current_user
from flask_babel import _
from sqlalchemy import text, desc
from sqlalchemy.orm.exc import NoResultFound
from furl import furl

from app import db, constants, cache, limiter, celery
from app.activitypub.signature import default_context, send_post_request
from app.activitypub.util import update_post_from_activity
from app.community.util import send_to_remote_instance, flair_from_form
from app.inoculation import inoculation
from app.post.forms import NewReplyForm, ReportPostForm, MeaCulpaForm, CrossPostForm, ConfirmationForm, \
    ConfirmationMultiDeleteForm, EditReplyForm, FlairPostForm, DeleteConfirmationForm
from app.community.forms import CreateLinkForm, CreateDiscussionForm, CreateVideoForm, CreatePollForm, EditImageForm
from app.constants import NOTIF_REPORT, POST_STATUS_SCHEDULED, POST_STATUS_PUBLISHED
from app.post.util import post_replies, get_comment_branch, tags_to_string, url_needs_archive, \
    generate_archive_link, body_has_no_archive_link
from app.constants import SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR, POST_TYPE_LINK, \
    POST_TYPE_IMAGE, \
    POST_TYPE_ARTICLE, POST_TYPE_VIDEO, NOTIF_REPLY, NOTIF_POST, POST_TYPE_POLL, SRC_WEB, SRC_API
from app.models import Post, PostReply, \
    PostReplyVote, PostVote, Notification, utcnow, UserBlock, DomainBlock, Report, Site, Community, \
    Topic, User, Instance, UserFollower, Poll, PollChoice, PollChoiceVote, PostBookmark, \
    PostReplyBookmark, CommunityBlock, File, CommunityFlair, UserFlair, BlockedImage, CommunityBan
from app.post import bp
from app.shared.tasks import task_selector
from app.utils import render_template, markdown_to_html, validation_required, \
    shorten_string, markdown_to_text, gibberish, ap_datetime, return_304, \
    request_etag_matches, ip_address, instance_banned, \
    moderating_communities, joined_communities, \
    blocked_instances, blocked_domains, community_moderators, show_ban_message, recently_upvoted_posts, \
    recently_downvoted_posts, recently_upvoted_post_replies, recently_downvoted_post_replies, \
    languages_for_form, add_to_modlog, blocked_communities, piefed_markdown_to_lemmy_markdown, \
    permission_required, blocked_users, get_request, is_local_image_url, is_video_url, can_upvote, can_downvote, \
    referrer, can_create_post_reply, communities_banned_from, \
    block_bots, flair_for_form, login_required_if_private_instance, retrieve_image_hash, posts_with_blocked_images, \
    possible_communities, user_notes, login_required
from app.post.util import post_type_to_form_url_type
from app.shared.reply import make_reply, edit_reply, bookmark_reply, remove_bookmark_reply, subscribe_reply, \
    delete_reply, mod_remove_reply, vote_for_reply
from app.shared.post import edit_post, sticky_post, lock_post, bookmark_post, remove_bookmark_post, subscribe_post, \
    vote_for_post
from app.shared.site import block_remote_instance


@login_required_if_private_instance
def show_post(post_id: int):
    with limiter.limit('30/minute'):
        post = Post.query.get_or_404(post_id)
        community: Community = post.community

        if community.banned or post.deleted:
            if current_user.is_anonymous or not (current_user.is_authenticated and (current_user.is_admin() or current_user.is_staff())):
                abort(404)
            else:
                if post.deleted_by == post.user_id:
                    flash(_('This post has been deleted by the author and is only visible to staff and admins.'), 'warning')
                else:
                    flash(_('This post has been deleted and is only visible to staff and admins.'), 'warning')

        sort = request.args.get('sort', 'hot')

        # If nothing has changed since their last visit, return HTTP 304
        current_etag = f"{post.id}{sort}_{hash(post.last_active)}"
        if current_user.is_anonymous and request_etag_matches(current_etag):
            return return_304(current_etag)

        if post.mea_culpa:
            flash(_('%(name)s has indicated they made a mistake in this post.', name=post.author.user_name), 'warning')

        mods = community_moderators(community.id)
        is_moderator = community.is_moderator()

        if community.private_mods:
            mod_list = []
        else:
            mod_user_ids = [mod.user_id for mod in mods]
            mod_list = User.query.filter(User.id.in_(mod_user_ids)).all()

        banned_from_community = False
        if current_user.is_authenticated and community.id in communities_banned_from(current_user.id):
            ban_details = CommunityBan.query.filter(CommunityBan.user_id == current_user.id,
                                                    CommunityBan.community_id == community.id).first()
            banned_from_community = True
            if ban_details:
                if ban_details.ban_until:
                    flash(_('You have been banned from this community until %(when)s.',
                            when=ban_details.ban_until.date()))
                else:
                    flash(_('You have been banned from this community.'))

        # handle top-level comments/replies
        form = NewReplyForm()
        form.language_id.choices = languages_for_form() if current_user.is_authenticated else []

        if post.status == POST_STATUS_SCHEDULED:
            flash(_('This post is scheduled to be published at %(when)s UTC', when=str(post.scheduled_for)))    # todo: convert into current_user.timezone

        if current_user.is_authenticated:
            if not post.community.is_moderator() and not post.community.is_owner() and not current_user.is_staff() and not current_user.is_admin():
                form.distinguished.render_kw = {'disabled': True}

        if current_user.is_authenticated and current_user.verified and form.validate_on_submit():
            try:
                reply = make_reply(form, post, None, SRC_WEB)
            except Exception as ex:
                flash(_('Your reply was not accepted because %(reason)s', reason=str(ex)), 'error')
                return redirect(url_for('activitypub.post_ap', post_id=post_id))

            return redirect(url_for('activitypub.post_ap', post_id=post_id, _anchor=f'comment_{reply.id}'))
        else:
            replies = post_replies(community, post.id, sort, current_user)
            more_replies = defaultdict(list)
            if post.cross_posts:
                cbf = communities_banned_from(current_user.get_id())
                bc = blocked_communities(current_user.get_id())
                bi = blocked_instances(current_user.get_id())
                for cross_posted_post in Post.query.filter(Post.id.in_(post.cross_posts)):
                    if cross_posted_post.community_id not in cbf \
                            and cross_posted_post.community_id not in bc \
                            and cross_posted_post.community.instance_id not in bi:
                        cross_posted_replies = post_replies(cross_posted_post.community, cross_posted_post.id, sort, current_user)
                        if len(cross_posted_replies):
                            more_replies[cross_posted_post.community].extend(cross_posted_replies)
            form.notify_author.data = True

            # user flair
            user_flair = {}
            for u_flair in UserFlair.query.filter(UserFlair.community_id == community.id):
                user_flair[u_flair.user_id] = u_flair.flair

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

        # for logged in users who have the 'hide read posts' function enabled
        # mark this post as read
        if current_user.is_authenticated:
            user = current_user
            if current_user.hide_read_posts:
                current_user.mark_post_as_read(post)
                db.session.commit()
        else:
            user = None

        community_flair = CommunityFlair.query.filter(CommunityFlair.community_id == post.community_id).order_by(CommunityFlair.flair).all()

        response = render_template('post/post.html', title=post.title, post=post, is_moderator=is_moderator, is_owner=community.is_owner(),
                               community=post.community, community_flair=community_flair,
                               breadcrumbs=breadcrumbs, related_communities=related_communities, mods=mod_list,
                               poll_form=poll_form, poll_results=poll_results, poll_data=poll_data, poll_choices=poll_choices, poll_total_votes=poll_total_votes,
                               canonical=post.ap_id, form=form, replies=replies, more_replies=more_replies, user_flair=user_flair,
                               THREAD_CUTOFF_DEPTH=constants.THREAD_CUTOFF_DEPTH,
                               description=description, og_image=og_image, show_deleted=current_user.is_authenticated and current_user.is_admin_or_staff(),
                               autoplay=request.args.get('autoplay', False), archive_link=archive_link,
                               noindex=not post.author.indexable, preconnect=post.url if post.url else None,
                               recently_upvoted=recently_upvoted, recently_downvoted=recently_downvoted,
                               recently_upvoted_replies=recently_upvoted_replies, recently_downvoted_replies=recently_downvoted_replies,
                               reply_collapse_threshold=reply_collapse_threshold,
                               etag=f"{post.id}{sort}_{hash(post.last_active)}", markdown_editor=current_user.is_authenticated and current_user.markdown_editor,
                               can_upvote_here=can_upvote(user, community),
                               can_downvote_here=can_downvote(user, community),
                               user_notes=user_notes(current_user.get_id()),
                               banned_from_community=banned_from_community,
                               low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1',
                               inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                               )
        response.headers.set('Vary', 'Accept, Cookie, Accept-Language')
        response.headers.set('Link', f'<https://{current_app.config["SERVER_NAME"]}/post/{post.id}>; rel="alternate"; type="application/activity+json"')
        oembed_url = url_for('post.post_oembed', post_id=post.id, _external=True)
        response.headers.set('Link', f'<{oembed_url}>; rel="alternate"; type="application/json+oembed"')
        return response


@bp.route('/post/<int:post_id>/embed', methods=['GET', 'HEAD'])
@block_bots
def post_embed(post_id):
    with limiter.limit('30/minute'):
        post = Post.query.get_or_404(post_id)
        community: Community = post.community

        if community.banned or post.deleted:
            if current_user.is_anonymous or not (
                    current_user.is_authenticated and (current_user.is_admin() or current_user.is_staff())):
                abort(404)
            else:
                if post.deleted_by == post.user_id:
                    flash(_('This post has been deleted by the author and is only visible to staff and admins.'),
                          'warning')
                else:
                    flash(_('This post has been deleted and is only visible to staff and admins.'), 'warning')

        # If nothing has changed since their last visit, return HTTP 304
        current_etag = f"{post.id}_{hash(post.last_active)}"
        if current_user.is_anonymous and request_etag_matches(current_etag):
            return return_304(current_etag)

        if post.mea_culpa:
            flash(_('%(name)s has indicated they made a mistake in this post.', name=post.author.user_name), 'warning')

        response = render_template('post/post_embed.html', title=post.title, post=post, show_post_community=True,
                                   etag=f"{post.id}_{hash(post.last_active)}", embed=True)
        response.headers.set('Vary', 'Accept, Cookie, Accept-Language')
        return response


@bp.route('/post/<int:post_id>/embed_code', methods=['GET', 'HEAD'])
@block_bots
def post_embed_code(post_id):
    post = Post.query.get_or_404(post_id)
    community = post.community

    # Breadcrumbs
    breadcrumbs = []
    breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
    breadcrumb.text = _('Home')
    breadcrumb.url = '/'
    breadcrumbs.append(breadcrumb)

    if community.topic_id:
        related_communities = Community.query.filter_by(topic_id=community.topic_id). \
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

        breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
        breadcrumb.text = post.title
        breadcrumb.url = f'/post/{post.id}'
        breadcrumbs.append(breadcrumb)
    else:
        related_communities = []

        breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
        breadcrumb.text = _('Communities')
        breadcrumb.url = '/communities'
        breadcrumbs.append(breadcrumb)

        breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
        breadcrumb.text = post.community.display_name()
        breadcrumb.url = '/c/' + post.community.link()
        breadcrumbs.append(breadcrumb)

        breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
        breadcrumb.text = post.title
        breadcrumb.url = f'/post/{post.id}'
        breadcrumbs.append(breadcrumb)

    return render_template('post/post_embed_code.html', title=_('Embed code for %(post_title)s', post_title=post.title),
                           post=post, breadcrumbs=breadcrumbs)


@bp.route('/post/<int:post_id>/oembed', methods=['GET', 'HEAD'])
def post_oembed(post_id):
    post = Post.query.get_or_404(post_id)
    iframe_url = url_for('post.post_embed', post_id=post.id, _external=True)
    oembed = {
      "version": "1.0",
      "type": "rich",
      "provider_name": g.site.name,
      "provider_url": f"https://{current_app.config['SERVER_NAME']}",
      "title": post.title,
      "html": f"<p><iframe src='{iframe_url}' class='piefed-embed' style='max-width: 100%; border: 0' width='400' allowfullscreen='allowfullscreen'></iframe><script src='https://{current_app.config['SERVER_NAME']}/static/js/embed.js' async='async'></script></p>",
      "width": 400,
      "height": 300,
      "author_name": post.author.display_name(),
    }
    return jsonify(oembed)


@bp.route('/post/<int:post_id>/<vote_direction>', methods=['GET', 'POST'])
@login_required
@validation_required
def post_vote(post_id: int, vote_direction):
    return vote_for_post(post_id, vote_direction, SRC_WEB)


@bp.route('/comment/<int:comment_id>/<vote_direction>', methods=['POST'])
@login_required
@validation_required
def comment_vote(comment_id, vote_direction):
    return vote_for_reply(comment_id, vote_direction, SRC_WEB)


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
                task_selector('edit_post', post_id=post.id)
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
                send_post_request(post.author.ap_inbox_url, pollvote_json, current_user.private_key, current_user.public_url() + '#main-key')

    return redirect(url_for('activitypub.post_ap', post_id=post_id))


@bp.route('/post/<int:post_id>/comment/<int:comment_id>')
@login_required_if_private_instance
def continue_discussion(post_id, comment_id):
    post = Post.query.get_or_404(post_id)
    comment = PostReply.query.get_or_404(comment_id)

    if post.community.banned or post.deleted or comment.deleted:
        if current_user.is_anonymous or not (current_user.is_authenticated and (current_user.is_admin() or current_user.is_staff())):
            abort(404)
        else:
            if post.deleted_by == post.user_id:
                flash(_('This post has been deleted by the author and is only visible to staff and admins.'), 'warning')
            else:
                flash(_('This post has been deleted and is only visible to staff and admins.'), 'warning')

    mods = post.community.moderators()
    is_moderator = current_user.is_authenticated and any(mod.user_id == current_user.id for mod in mods)
    if post.community.private_mods:
        mod_list = []
    else:
        mod_user_ids = [mod.user_id for mod in mods]
        mod_list = User.query.filter(User.id.in_(mod_user_ids)).all()
    replies = get_comment_branch(post.id, comment.id, 'top')

    # Voting history
    if current_user.is_authenticated:
        recently_upvoted_replies = recently_upvoted_post_replies(current_user.id)
        recently_downvoted_replies = recently_downvoted_post_replies(current_user.id)
    else:
        recently_upvoted_replies = []
        recently_downvoted_replies = []

    response = render_template('post/continue_discussion.html', title=_('Discussing %(title)s', title=post.title), post=post, mods=mod_list,
                           is_moderator=is_moderator, comment=comment, replies=replies, markdown_editor=current_user.is_authenticated and current_user.markdown_editor,
                           recently_upvoted_replies=recently_upvoted_replies, recently_downvoted_replies=recently_downvoted_replies,
                           community=post.community,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None)
    response.headers.set('Vary', 'Accept, Cookie, Accept-Language')
    return response


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/reply', methods=['GET', 'POST'])
@login_required
def add_reply(post_id: int, comment_id: int):
    # this route is used when JS is disabled
    if current_user.banned or current_user.ban_comments:
        return show_ban_message()
    post = Post.query.get_or_404(post_id)

    if not post.comments_enabled:
        flash(_('Comments have been disabled.'), 'warning')
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
        current_user.last_seen = utcnow()
        current_user.ip_address = ip_address()

        try:
            reply = make_reply(form, post, in_reply_to.id, SRC_WEB)
        except Exception as ex:
            flash(_('Your reply was not accepted because %(reason)s', reason=str(ex)), 'error')
            if in_reply_to.depth <= constants.THREAD_CUTOFF_DEPTH:
                return redirect(url_for('activitypub.post_ap', post_id=post_id, _anchor=f'comment_{in_reply_to.id}'))
            else:
                return redirect(url_for('post.continue_discussion', post_id=post_id, comment_id=in_reply_to.parent_id))

        if reply.depth <= constants.THREAD_CUTOFF_DEPTH:
            return redirect(url_for('activitypub.post_ap', post_id=post_id, _anchor=f'comment_{reply.id}'))
        else:
            return redirect(url_for('post.continue_discussion', post_id=post_id, comment_id=reply.parent_id))
    else:
        form.notify_author.data = True

        return render_template('post/add_reply.html', title=_('Discussing %(title)s', title=post.title), post=post,
                               is_moderator=is_moderator, form=form, comment=in_reply_to, markdown_editor=current_user.is_authenticated and current_user.markdown_editor,
                               mods=mod_list,
                               community=post.community,
                               SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                               inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                               )


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/reply_inline', methods=['GET', 'POST'])
@login_required
def add_reply_inline(post_id: int, comment_id: int):
    # this route is called by htmx and returns a html fragment representing a form that can be submitted to make a new reply
    # it also accepts the POST from that form and makes the reply
    if current_user.banned or current_user.ban_comments:
        return _('You have been banned.')
    post = Post.query.get_or_404(post_id)
    if not can_create_post_reply(current_user, post.community):
        return _('You are not permitted to comment in this community')

    if not post.comments_enabled:
        return _('Comments have been disabled.')

    in_reply_to = PostReply.query.get_or_404(comment_id)

    if in_reply_to.author.has_blocked_user(current_user.id):
        return _('You cannot reply to %(name)s', name=in_reply_to.author.display_name())

    if request.method == 'GET':
        return render_template('post/add_reply_inline.html', post_id=post_id, comment_id=comment_id, languages=languages_for_form())
    else:
        content = request.form.get('body', '').strip()
        language_id = int(request.form.get('language_id'))

        if content == '':
            return f'<div id="reply_to_{comment_id}" class="hidable"></div>' # do nothing, just hide the form
        reply = PostReply.new(current_user, post, in_reply_to=in_reply_to, body=piefed_markdown_to_lemmy_markdown(content),
                              body_html=markdown_to_html(content), notify_author=True,
                              language_id=language_id, distinguished=False)

        current_user.language_id = language_id
        reply.ap_id = reply.profile_id()
        db.session.commit()

        # Federate the reply
        task_selector('make_reply', reply_id=reply.id, parent_id=in_reply_to.id)

        user_flair = {}
        if current_user.is_authenticated:
            for u_flair in UserFlair.query.filter(UserFlair.community_id == post.community_id,
                                                  UserFlair.user_id == current_user.id):
                user_flair[u_flair.user_id] = u_flair.flair

        return render_template('post/add_reply_inline_result.html', post_reply=reply, user_flair=user_flair)


@bp.route('/post/<int:post_id>/options_menu', methods=['GET'])
@block_bots
def post_options(post_id: int):
    post = Post.query.get_or_404(post_id)
    if post.deleted:
        if current_user.is_anonymous:
            abort(404)
        if (not post.community.is_moderator() and
            not current_user.is_admin() and
            (post.deleted_by is not None and post.deleted_by != current_user.id)):
            abort(401)

    existing_bookmark = []
    if current_user.is_authenticated:
        existing_bookmark = PostBookmark.query.filter(PostBookmark.post_id == post_id, PostBookmark.user_id == current_user.id).first()

    return render_template('post/post_options.html', post=post, existing_bookmark=existing_bookmark)


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/options_menu', methods=['GET'])
@block_bots
def post_reply_options(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    if post.deleted or post_reply.deleted:
        if current_user.is_anonymous:
            abort(404)

    existing_bookmark = []
    if current_user.is_authenticated:
        existing_bookmark = PostReplyBookmark.query.filter(PostReplyBookmark.post_reply_id == comment_id,
                                                      PostReplyBookmark.user_id == current_user.id).first()

    return render_template('post/post_reply_options.html', post=post, post_reply=post_reply,
                           existing_bookmark=existing_bookmark)


@bp.route('/post/<int:post_id>/edit', methods=['GET', 'POST'])
@login_required
def post_edit(post_id: int):
    post = Post.query.get_or_404(post_id)
    post_type = post.type
    if post.type == POST_TYPE_ARTICLE:
        form = CreateDiscussionForm()
    elif post.type == POST_TYPE_LINK:
        form = CreateLinkForm()
    elif post.type == POST_TYPE_IMAGE:
        if post.image and post.image.source_url and is_local_image_url(post.image.source_url):
            form = EditImageForm()
        else:
            form = CreateLinkForm()
            post_type = POST_TYPE_LINK
    elif post.type == POST_TYPE_VIDEO:
        if is_video_url(post.url):
            form = CreateVideoForm()
        else:
            form = CreateLinkForm()
            post_type = POST_TYPE_LINK
    elif post.type == POST_TYPE_POLL:
        form = CreatePollForm()
        poll = Poll.query.filter_by(post_id=post_id).first()
        del form.finish_in
    else:
        abort(404)

    del form.communities

    flair_choices = flair_for_form(post.community_id)
    if len(flair_choices):
        form.flair.choices = flair_choices
    else:
        del form.flair

    mods = post.community.moderators()
    if post.community.private_mods:
        mod_list = []
    else:
        mod_user_ids = [mod.user_id for mod in mods]
        mod_list = User.query.filter(User.id.in_(mod_user_ids)).all()

    if post.user_id == current_user.id or post.community.is_moderator() or current_user.is_admin():

        if post.community.id in communities_banned_from(current_user.id):
            abort(403)

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
            try:
                uploaded_file = request.files['image_file'] if post_type == POST_TYPE_IMAGE else None
                edit_post(form, post, post_type, SRC_WEB, uploaded_file=uploaded_file)
                flash(_('Your changes have been saved.'), 'success')
            except Exception as ex:
                flash(_('Your edit was not accepted because %(reason)s', reason=str(ex)), 'error')
                abort(401)

            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.title.data = post.title
            form.body.data = post.body
            form.notify_author.data = post.notify_author
            form.nsfw.data = post.nsfw
            form.nsfl.data = post.nsfl
            form.sticky.data = post.sticky
            form.language_id.data = post.language_id
            form.tags.data = tags_to_string(post)
            form.repeat.data = post.repeat
            form.scheduled_for.data = post.scheduled_for
            if form.flair:
                form.flair.data = [flair.id for flair in post.flair]
            if post_type == POST_TYPE_LINK:
                form.link_url.data = post.url
            elif post_type == POST_TYPE_IMAGE:
                # existing_image = True
                form.image_alt_text.data = post.image.alt_text
                path = post.image.file_path
                # This is fallback for existing entries
                if not path:
                    path = "app/" + post.image.source_url.replace(
                        f"https://{current_app.config['SERVER_NAME']}/", ""
                    )
                if not path.startswith('http'):
                    with open(path, "rb")as file:
                        form.image_file.data = file.read()

            elif post_type == POST_TYPE_VIDEO:
                form.video_url.data = post.url
            elif post_type == POST_TYPE_POLL:
                poll = Poll.query.filter_by(post_id=post.id).first()
                form.mode.data = poll.mode
                form.local_only.data = poll.local_only
                i = 1
                for choice in PollChoice.query.filter_by(post_id=post.id).order_by(PollChoice.sort_order).all():
                    form_field = getattr(form, f"choice_{i}")
                    form_field.data = choice.choice_text
                    i += 1

            if not (post.community.is_moderator() or post.community.is_owner() or current_user.is_admin()):
                form.sticky.render_kw = {'disabled': True}
            return render_template('post/post_edit.html', title=_('Edit post'), form=form,
                                   post_type=post_type, community=post.community, post=post,
                                   markdown_editor=current_user.markdown_editor, mods=mod_list,
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                                   )
    else:
        abort(401)


@bp.route('/post/<int:post_id>/delete', methods=['GET', 'POST'])
@login_required
def post_delete(post_id: int):
    post = Post.query.get_or_404(post_id)
    community = post.community
    if post.user_id == current_user.id or community.is_moderator() or current_user.is_admin():
        if post.community.id in communities_banned_from(current_user.id):
            abort(403)
        form = DeleteConfirmationForm()
        if form.validate_on_submit():
            post_delete_post(community, post, current_user.id, form.reason.data)
            ref = request.form.get('referrer')
            if '/post/' not in ref:
                return redirect(ref)
            else:
                return redirect(url_for('activitypub.community_profile', actor=community.ap_id if community.ap_id is not None else community.name))
        else:
            form.referrer.data = referrer(url_for('activitypub.community_profile', actor=community.ap_id if community.ap_id is not None else community.name))
            return render_template('generic_form.html', title=_('Are you sure you want to delete the post "%(post_title)s"?',
                                                                post_title=post.title),
                                   form=form)


def post_delete_post(community: Community, post: Post, user_id: int, reason: str | None, federate_all_communities=True):
    user: User = User.query.get(user_id)
    if post.url:
        post.calculate_cross_posts(delete_only=True)
    post.deleted = True
    post.deleted_by = user_id
    post.author.post_count -= 1
    community.post_count -= 1
    if hasattr(g, 'site'):  # g.site is invalid when running from cli
        flash(_('Post deleted.'))
    db.session.commit()

    delete_json = {
        'id': f"https://{current_app.config['SERVER_NAME']}/activities/delete/{gibberish(15)}",
        'type': 'Delete',
        'actor': user.public_url(),
        'audience': post.community.public_url(),
        'to': [post.community.public_url(), 'https://www.w3.org/ns/activitystreams#Public'],
        'published': ap_datetime(utcnow()),
        'cc': [
            user.followers_url()
        ],
        'object': post.ap_id,
        'uri': post.ap_id
    }
    if post.user_id != user.id:
        delete_json['summary'] = 'Deleted by mod'

    # Federation
    if not community.local_only:    # local_only communities do not federate
        # if this is a remote community and we are a mod of that community
        if not post.community.is_local() and user.is_local() and (post.user_id == user.id or community.is_moderator(user) or community.is_owner(user)):
            send_post_request(post.community.ap_inbox_url, delete_json, user.private_key, user.public_url() + '#main-key')
        elif post.community.is_local():  # if this is a local community - Announce it to followers on remote instances
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
                if instance.inbox and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                    send_to_remote_instance(instance.id, post.community.id, announce)

    # Federate to microblog followers
    followers = UserFollower.query.filter_by(local_user_id=post.user_id)
    if followers:
        instances = Instance.query.join(User, User.instance_id == Instance.id).join(UserFollower,
                                                                                    UserFollower.remote_user_id == User.id)
        instances = instances.filter(UserFollower.local_user_id == post.user_id)
        for instance in instances:
            if instance.inbox and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain) and instance.online():
                send_post_request(instance.inbox, delete_json, user.private_key, user.public_url() + '#main-key')

    if post.user_id != user.id and reason is not None:
        add_to_modlog('delete_post', reason=reason, community_id=community.id, link_text=shorten_string(post.title),
                      link=f'post/{post.id}')


@bp.route('/post/<int:post_id>/restore', methods=['POST'])
@login_required
def post_restore(post_id: int):
    post = Post.query.get_or_404(post_id)
    if post.user_id == current_user.id or post.community.is_moderator() or post.community.is_owner() or current_user.is_admin():
        if post.deleted_by == post.user_id:
            was_mod_deletion = False
        else:
            was_mod_deletion = True
        post.deleted = False
        post.deleted_by = None
        post.author.post_count += 1
        post.community.post_count += 1
        db.session.commit()

        # Federate un-delete
        if not post.community.local_only:
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
              },
              "cc": [post.community.public_url()],
              "audience": post.community.public_url(),
              "type": "Undo",
              "id": f"https://{current_app.config['SERVER_NAME']}/activities/undo/{gibberish(15)}"
            }
            if was_mod_deletion:
                delete_json['object']['summary'] = "Deleted by mod"

            if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
                if not was_mod_deletion or (was_mod_deletion and post.community.is_moderator(current_user)):
                    send_post_request(post.community.ap_inbox_url, delete_json, current_user.private_key,
                                      current_user.public_url() + '#main-key')

            else:  # local community - send it to followers on remote instances
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

        if post.user_id != current_user.id:
            add_to_modlog('restore_post', community_id=post.community.id, link_text=shorten_string(post.title),
                          link=f'post/{post.id}')

        flash(_('Post has been restored.'))
    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/purge', methods=['POST'])
@login_required
def post_purge(post_id: int):
    post = Post.query.get_or_404(post_id)
    if not post.deleted:
        abort(404)
    if post.deleted_by == current_user.id or post.community.is_moderator() or current_user.is_admin():
        post.delete_dependencies()
        db.session.delete(post)
        db.session.commit()
        flash(_('Post purged.'))
    else:
        abort(401)

    return redirect(url_for('user.show_profile_by_id', user_id=post.user_id))


@bp.route('/post/<int:post_id>/bookmark', methods=['POST'])
@login_required
def post_bookmark(post_id: int):
    try:
        bookmark_post(post_id, SRC_WEB)
    except NoResultFound:
        abort(404)

    return render_template('post/_add_remove_bookmark.html', post_id=post_id, action_type="add", item_type="post")


@bp.route('/post/<int:post_id>/remove_bookmark', methods=['POST'])
@login_required
def post_remove_bookmark(post_id: int):
    try:
        remove_bookmark_post(post_id, SRC_WEB)
    except NoResultFound:
        abort(404)

    return render_template('post/_add_remove_bookmark.html', post_id=post_id, action_type="remove", item_type="post")


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/bookmark', methods=['POST'])
@login_required
def post_reply_bookmark(post_id: int, comment_id: int):
    try:
        bookmark_reply(comment_id, SRC_WEB)
    except NoResultFound:
        abort(404)

    return render_template('post/_add_remove_bookmark.html', post_id=post_id, reply_id=comment_id,
                           action_type="add", item_type="reply")


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/remove_bookmark', methods=['POST'])
@login_required
def post_reply_remove_bookmark(post_id: int, comment_id: int):
    try:
        remove_bookmark_reply(comment_id, SRC_WEB)
    except NoResultFound:
        abort(404)

    return render_template('post/_add_remove_bookmark.html', post_id=post_id, reply_id=comment_id,
                           action_type="remove", item_type="reply")


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

        targets_data = {'suspect_post_id': post.id, 'suspect_user_id': post.author.id, 'reporter_id': current_user.id}
        for mod in post.community.moderators():
            notification = Notification(user_id=mod.user_id, title=_('A post has been reported'),
                                        url=f"https://{current_app.config['SERVER_NAME']}/post/{post.id}",
                                        author_id=current_user.id, notif_type=NOTIF_REPORT,
                                        subtype='post_reported',
                                        targets=targets_data)
            db.session.add(notification)
            already_notified.add(mod.user_id)

        # only notify admins for certain types of report
        if '5' in form.reasons.data or '6' in form.reasons.data:
            for admin in Site.admins():
                if admin.id not in already_notified:
                    notify = Notification(title=_('Reported content'), url='/admin/reports', user_id=admin.id,
                                          author_id=current_user.id, notif_type=NOTIF_REPORT,
                                          subtype='post_reported',
                                          targets=targets_data)
                    db.session.add(notify)
                    admin.unread_notifications += 1

        post.reports += 1
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
                send_post_request(post.community.ap_inbox_url, report_json, current_user.private_key,
                                  current_user.public_url() + '#main-key')

        flash(_('Post has been reported, thank you!'))
        return redirect(post.community.local_url())
    elif request.method == 'GET':
        form.report_remote.data = True

    return render_template('post/post_report.html', title=_('Report post'), form=form, post=post)


@bp.route('/post/<int:post_id>/block_user', methods=['POST'])
@login_required
def post_block_user(post_id: int):
    post = Post.query.get_or_404(post_id)
    existing = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=post.author.id).first()
    if not existing:
        db.session.add(UserBlock(blocker_id=current_user.id, blocked_id=post.author.id))
        db.session.commit()
    flash(_('%(name)s has been blocked.', name=post.author.user_name))
    cache.delete_memoized(blocked_users, current_user.id)

    # todo: federate block to post author instance

    return redirect(post.community.local_url())


@bp.route('/post/<int:post_id>/block_domain', methods=['POST'])
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


@bp.route('/post/<int:post_id>/block_community', methods=['POST'])
@login_required
def post_block_community(post_id: int):
    post = Post.query.get_or_404(post_id)
    existing = CommunityBlock.query.filter_by(user_id=current_user.id, community_id=post.community_id).first()
    if not existing:
        db.session.add(CommunityBlock(user_id=current_user.id, community_id=post.community_id))
        db.session.commit()
        cache.delete_memoized(blocked_communities, current_user.id)
    flash(_('Posts in %(name)s will be hidden.', name=post.community.display_name()))
    return redirect(post.community.local_url())


@bp.route('/post/<int:post_id>/block_instance', methods=['POST'])
@login_required
def post_block_instance(post_id: int):
    post = Post.query.get_or_404(post_id)
    block_remote_instance(post.instance_id, SRC_WEB)
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

    return render_template('post/post_mea_culpa.html', title=_('I changed my mind'), form=form, post=post)


@bp.route('/post/<int:post_id>/sticky/<mode>', methods=['GET', 'POST'])
@login_required
def post_sticky(post_id: int, mode):
    post = Post.query.get_or_404(post_id)
    if post.community.is_moderator(current_user) or current_user.is_admin():
        sticky_post(post.id, mode == 'yes', SRC_WEB)
    if mode == 'yes':
        flash(_('%(name)s has been stickied.', name=post.title))
    else:
        flash(_('%(name)s has been un-stickied.', name=post.title))
    return redirect(referrer(url_for('activitypub.post_ap', post_id=post.id)))


@bp.route('/post/<int:post_id>/set_flair', methods=['GET', 'POST'])
@login_required
def post_set_flair(post_id):
    post = Post.query.get_or_404(post_id)
    if post.user_id == current_user.id or post.community.is_moderator(current_user) or current_user.is_staff() or current_user.is_admin():
        form = FlairPostForm()
        flair_choices = flair_for_form(post.community.id)
        if len(flair_choices):
            form.flair.choices = flair_choices

        if form.validate_on_submit():
            post.flair.clear()
            post.flair = flair_from_form(form.flair.data)
            db.session.commit()
            if post.status == POST_STATUS_PUBLISHED and post.author.is_local():
                task_selector('edit_post', post_id=post.id)
            return redirect(url_for('activitypub.community_profile', actor=post.community.link()))
        form.referrer.data = referrer()
        form.flair.data = [flair.id for flair in post.flair]
        return render_template('generic_form.html', form=form, title=_('Set flair for %(post_title)s', post_title=post.title))
    else:
        abort(401)


@bp.route('/post/<int:post_id>/lock/<mode>', methods=['GET', 'POST'])
@login_required
def post_lock(post_id: int, mode):
    try:
        lock_post(post_id, mode == 'yes', SRC_WEB)
    except:
        abort(404)
    return redirect(referrer(url_for('activitypub.post_ap', post_id=post_id)))


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
        targets_data = {'suspect_comment_id':post_reply.id,'suspect_user_id':post_reply.author.id,'reporter_id':current_user.id}
        for mod in post.community.moderators():
            notification = Notification(user_id=mod.user_id, title=_('A comment has been reported'),
                                        url=f"https://{current_app.config['SERVER_NAME']}/comment/{post_reply.id}",
                                        author_id=current_user.id, notif_type=NOTIF_REPORT,
                                        subtype='comment_reported',
                                        targets=targets_data)
            db.session.add(notification)
            already_notified.add(mod.user_id)

        if '5' in form.reasons.data or '6' in form.reasons.data:
            for admin in Site.admins():
                if admin.id not in already_notified:
                    notify = Notification(title='Suspicious content', url='/admin/reports', user_id=admin.id,
                                          author_id=current_user.id, notif_type=NOTIF_REPORT,
                                            subtype='comment_reported',
                                          targets=targets_data)
                    db.session.add(notify)
                    admin.unread_notifications += 1
        post_reply.reports += 1
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
                send_post_request(post.community.ap_inbox_url, report_json, current_user.private_key, current_user.public_url() + '#main-key')

        flash(_('Comment has been reported, thank you!'))
        return redirect(url_for('activitypub.post_ap', post_id=post.id))
    elif request.method == 'GET':
        form.report_remote.data = True

    return render_template('post/post_reply_report.html', title=_('Report comment'), form=form, post=post, post_reply=post_reply)


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/block_user', methods=['POST'])
@login_required
def post_reply_block_user(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    existing = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=post_reply.author.id).first()
    if not existing:
        db.session.add(UserBlock(blocker_id=current_user.id, blocked_id=post_reply.author.id))
        db.session.commit()
    flash(_('%(name)s has been blocked.', name=post_reply.author.user_name))
    cache.delete_memoized(blocked_users, current_user.id)

    # todo: federate block to post_reply author instance

    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/block_instance', methods=['POST'])
@login_required
def post_reply_block_instance(post_id: int, comment_id: int):
    post_reply = PostReply.query.get_or_404(comment_id)
    block_remote_instance(post_reply.instance_id, SRC_WEB)
    flash(_('Content from %(name)s will be hidden.', name=post_reply.instance.domain))
    return redirect(url_for('activitypub.post_ap', post_id=post_id))


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/edit', methods=['GET', 'POST'])
@login_required
def post_reply_edit(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    if post_reply.parent_id:
        comment = PostReply.query.get_or_404(post_reply.parent_id)
    else:
        comment = None
    form = EditReplyForm()
    form.language_id.choices = languages_for_form()
    if post_reply.user_id == current_user.id or post.community.is_moderator():
        if form.validate_on_submit():
            edit_reply(form, post_reply, post, SRC_WEB)
            return redirect(url_for('activitypub.post_ap', post_id=post.id))
        else:
            form.body.data = post_reply.body
            form.notify_author.data = post_reply.notify_author
            form.language_id.data = post_reply.language_id
            form.distinguished.data = post_reply.distinguished
            if not post.community.is_moderator() and not post.community.is_owner() and not current_user.is_staff() and not current_user.is_admin():
                form.distinguished.render_kw = {'disabled': True}
            return render_template('post/post_reply_edit.html', title=_('Edit comment'), form=form, post=post, post_reply=post_reply,
                                   comment=comment, markdown_editor=current_user.markdown_editor,
                                   community=post.community, 
                                   SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                                   inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                                   )
    else:
        abort(401)


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/delete', methods=['POST'])
@login_required
def post_reply_delete(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    community = post.community

    form = ConfirmationMultiDeleteForm()

    if form.validate_on_submit():
        if form.also_delete_replies.data:
            num_deleted = 0
            # Find all the post_replys that have the same IDs in the path. NB the @>
            child_post_ids = db.session.execute(text('select id from "post_reply" where path @> ARRAY[:parent_path]'),
                                                {'parent_path': post_reply.path}).scalars()
            for child_post_id in child_post_ids:
                if child_post_id != 0:
                    reply = PostReply.query.get_or_404(child_post_id)

                    if reply.user_id == current_user.id:
                        # User is deleting their own reply
                        delete_reply(reply.id, SRC_WEB, None)
                    elif post.community.is_moderator() or current_user.is_admin():
                        # Moderator or admin is deleting the reply
                        if form.reason.data:
                            reason = 'Deleted by mod: ' + form.reason.data
                        else:
                            reason = 'Deleted by mod'
                        mod_remove_reply(reply.id, reason, SRC_WEB, None)
                    num_deleted += 1
        else:
            num_deleted = 0
            if post_reply.user_id == current_user.id:
                # User is deleting their own reply
                delete_reply(post_reply.id, SRC_WEB, None)
                num_deleted = 1
            elif community.is_moderator() or current_user.is_admin():
                # Moderator or admin is deleting the reply
                if form.reason.data:
                    reason = 'Deleted by mod: ' + form.reason.data
                else:
                    reason = 'Deleted by mod'
                mod_remove_reply(post_reply.id, reason, SRC_WEB, None)
                num_deleted = 1
        if num_deleted > 0:
            flash(_('Deleted %(num_deleted)s comments.', num_deleted=num_deleted))
        return redirect(url_for('activitypub.post_ap', post_id=post.id, _anchor=f'comment_{comment_id}'))
    else:
        return render_template('generic_form.html', title=_('Are you sure you want to delete this comment?'), form=form)


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/restore', methods=['POST'])
@login_required
def post_reply_restore(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)

    if post_reply.user_id == current_user.id or post.community.is_moderator() or current_user.is_admin():
        if post_reply.deleted_by == post_reply.user_id:
            was_mod_deletion = False
        else:
            was_mod_deletion = True
        post_reply.deleted = False
        post_reply.deleted_by = None
        if not post_reply.author.bot:
            post.reply_count += 1
        post_reply.author.post_reply_count += 1
        if post_reply.path:
            db.session.execute(text('update post_reply set child_count = child_count + 1 where id in :parents'),
                               {'parents': tuple(post_reply.path[:-1])})
        db.session.commit()
        flash(_('Comment restored.'))

        # Federate un-delete
        if not post.community.local_only:
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
                  'object': post_reply.ap_id,
                  'uri': post_reply.ap_id,
              },
              "cc": [post.community.public_url()],
              "audience": post.community.public_url(),
              "type": "Undo",
              "id": f"https://{current_app.config['SERVER_NAME']}/activities/undo/{gibberish(15)}"
            }
            if was_mod_deletion:
                delete_json['object']['summary'] = "Deleted by mod"

            if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
                if not was_mod_deletion or (was_mod_deletion and post.community.is_moderator(current_user)):
                    send_post_request(post.community.ap_inbox_url, delete_json, current_user.private_key,
                                      current_user.public_url() + '#main-key')


            else:  # local community - send it to followers on remote instances
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

        if post_reply.user_id != current_user.id:
            add_to_modlog('restore_post_reply', community_id=post.community.id, link_text=f'comment on {shorten_string(post.title)}',
                          link=f'post/{post.id}#comment_{post_reply.id}')

    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/comment/<int:comment_id>/purge', methods=['POST'])
@login_required
def post_reply_purge(post_id: int, comment_id: int):
    post = Post.query.get_or_404(post_id)
    post_reply = PostReply.query.get_or_404(comment_id)
    if not post_reply.deleted:
        abort(404)
    if post_reply.deleted_by == current_user.id or post.community.is_moderator() or current_user.is_admin():
        if not post_reply.has_replies():
            post_reply.delete_dependencies()
            db.session.delete(post_reply)
            db.session.commit()
            flash(_('Comment purged.'))
        else:
            flash(_('Comments that have been replied to cannot be purged.'))
    else:
        abort(401)

    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/notification', methods=['GET', 'POST'])
@login_required
def post_notification(post_id: int):
    try:
        return subscribe_post(post_id, None, SRC_WEB)
    except NoResultFound:
        abort(404)


@bp.route('/post_reply/<int:post_reply_id>/notification', methods=['GET', 'POST'])
@login_required
def post_reply_notification(post_reply_id: int):
    try:
        return subscribe_reply(post_reply_id, None, SRC_WEB)
    except NoResultFound:
        abort(404)


@bp.route('/post/<int:post_id>/cross_posts', methods=['GET'])
def post_cross_posts(post_id: int):
    post = Post.query.get_or_404(post_id)
    cross_posts = Post.query.filter(Post.id.in_(post.cross_posts))
    return render_template('post/post_cross_posts.html', cross_posts=cross_posts)


@bp.route('/post/<int:post_id>/block_image', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def post_block_image(post_id: int):
    post = Post.query.get_or_404(post_id)
    if post.type == POST_TYPE_IMAGE:
        form = ConfirmationForm()
        if form.validate_on_submit():
            hash = retrieve_image_hash(post.url)
            if hash:
                file_name = str(furl(post.url).path).split('/')
                file_name = file_name[-1]
                blocked_image = BlockedImage(hash=hash, file_name=file_name, note=shorten_string(post.title))
                db.session.add(blocked_image)
                db.session.commit()

                flash(_('Image blocked. Now delete matching posts.'))
                return redirect(url_for('post.post_block_image_purge_posts', post_id=post_id, referrer=request.form.get('referrer')))

        else:
            form.referrer.data = referrer()
            return render_template('generic_form.html',
                                   title=_('Are you sure you want to block this image?'),
                                   message=_('All posts that use this image will be deleted and future posts of the image will be rejected.'),
                                   form=form)

    return redirect(referrer())


@bp.route('/post/<int:post_id>/block_image_purge_posts', methods=['POST'])
@login_required
@permission_required('change instance settings')
def post_block_image_purge_posts(post_id: int):
    post = Post.query.get_or_404(post_id)
    if request.method == 'POST':
        post_ids = request.form.getlist('post_ids')

        task_selector('delete_posts_with_blocked_images', post_ids=post_ids, user_id=current_user.id, send_async=not current_app.debug)

        flash(_('%(count)s posts deleted.', count=len(post_ids)))

        ref = request.args.get('referrer')
        if '/post/' not in ref:
            return redirect(ref)
        else:
            return redirect(url_for('activitypub.community_profile',
                                    actor=post.community.ap_id if post.community.ap_id is not None else post.community.name))

    posts = Post.query.filter(Post.id.in_(posts_with_blocked_images()), Post.deleted == False).order_by(desc(Post.posted_at)).all()
    return render_template('post/post_block_image_purge_posts.html', post=post, posts=posts, title=_('Posts containing blocked images'),
                           referrer=request.args.get('referrer'))


@bp.route('/post/<int:post_id>/voting_activity', methods=['GET'])
@login_required
@permission_required('change instance settings')
def post_view_voting_activity(post_id: int):
    post = Post.query.get_or_404(post_id)

    post_title=post.title
    upvoters = User.query.join(PostVote, PostVote.user_id == User.id).filter_by(post_id=post_id, effect=1.0).order_by(User.ap_domain, User.user_name)
    downvoters = User.query.join(PostVote, PostVote.user_id == User.id).filter_by(post_id=post_id, effect=-1.0).order_by(User.ap_domain, User.user_name)

    # local users will be at the bottom of each list as ap_domain is empty for those.

    return render_template('post/post_voting_activity.html', title=_('Voting Activity'),
                           post_title=post_title, upvoters=upvoters, downvoters=downvoters)


@bp.route('/comment/<int:comment_id>/voting_activity', methods=['GET'])
@login_required
@permission_required('change instance settings')
def post_reply_view_voting_activity(comment_id: int):
    post_reply = PostReply.query.get_or_404(comment_id)

    reply_text=post_reply.body
    upvoters = User.query.join(PostReplyVote, PostReplyVote.user_id == User.id).filter_by(post_reply_id=comment_id, effect=1.0).order_by(User.ap_domain, User.user_name)
    downvoters = User.query.join(PostReplyVote, PostReplyVote.user_id == User.id).filter_by(post_reply_id=comment_id, effect=-1.0).order_by(User.ap_domain, User.user_name)

    # local users will be at the bottom of each list as ap_domain is empty for those.

    return render_template('post/post_reply_voting_activity.html', title=_('Voting Activity'),
                           reply_text=reply_text, upvoters=upvoters, downvoters=downvoters)


@bp.route('/post/<int:post_id>/fixup_from_remote', methods=['POST'])
@login_required
@permission_required('change instance settings')
def post_fixup_from_remote(post_id: int):
    post = Post.query.get_or_404(post_id)

    # will fail for some MBIN objects for same reason that 'View original on ...' does
    # (ap_id is lowercase, but original URL was mixed-case and remote instance software is case-sensitive)
    remote_post_request = get_request(post.ap_id, headers={'Accept': 'application/activity+json'})
    if remote_post_request.status_code == 200:
        remote_post_json = remote_post_request.json()
        remote_post_request.close()
        if 'type' in remote_post_json and remote_post_json['type'] == 'Page':
            post.domain_id = None
            file_entry_to_delete = None
            if post.image_id:
                file_entry_to_delete = post.image_id
            post.image_id = None
            post.url = None
            db.session.commit()
            if file_entry_to_delete:
                File.query.filter_by(id=file_entry_to_delete).delete()
                db.session.commit()
            update_json = {'type': 'Update', 'object': remote_post_json}
            update_post_from_activity(post, update_json)

    return redirect(url_for('activitypub.post_ap', post_id=post.id))


@bp.route('/post/<int:post_id>/cross-post', methods=['GET', 'POST'])
@login_required
def post_cross_post(post_id: int):
    post = Post.query.get_or_404(post_id)
    form = CrossPostForm()

    form.which_community.choices = possible_communities()

    if form.validate_on_submit():
        community = Community.query.get_or_404(form.which_community.data)
        post_type = post_type_to_form_url_type(post.type, post.url)
        response = make_response(redirect(url_for('community.add_post', actor=community.link(), type=post_type, source=str(post.id))))
        response.set_cookie('cross_post_community_id', str(community.id), max_age=timedelta(days=28))
        response.delete_cookie('post_title')
        response.delete_cookie('post_description')
        response.delete_cookie('post_tags')
        return response
    else:
        breadcrumbs = []
        breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
        breadcrumb.text = _('Home')
        breadcrumb.url = '/'
        breadcrumbs.append(breadcrumb)
        breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
        breadcrumb.text = _('Communities')
        breadcrumb.url = '/communities'
        breadcrumbs.append(breadcrumb)

        if request.cookies.get('cross_post_community_id'):
            form.which_community.data = int(request.cookies.get('cross_post_community_id'))

        return render_template('post/post_cross_post.html', title=_('Cross post'), form=form, post=post,
                               breadcrumbs=breadcrumbs)


@bp.route('/post_preview', methods=['POST'])
@login_required
def preview():
    return markdown_to_html(request.form.get('body'))
