from collections import namedtuple
from datetime import timedelta, timezone
from random import randint
from typing import List

from feedgen.feed import FeedGenerator
from flask import request, flash, url_for, current_app, redirect, abort, make_response, g
from flask_babel import _
from flask_login import current_user
from sqlalchemy import text, desc, asc, or_

from app import db, cache
from app.community.util import hashtags_used_in_communities
from app.constants import SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR, POST_TYPE_IMAGE, \
    POST_TYPE_LINK, POST_TYPE_VIDEO, NOTIF_TOPIC
from app.email import send_topic_suggestion
from app.inoculation import inoculation
from app.models import Topic, Community, NotificationSubscription, PostReply, utcnow
from app.topic import bp
from app.topic.forms import SuggestTopicsForm
from app.utils import render_template, user_filters_posts, validation_required, mimetype_from_url, login_required, \
    gibberish, get_deduped_post_ids, paginate_post_ids, post_ids_to_models, blocked_communities, \
    recently_upvoted_posts, recently_downvoted_posts, blocked_or_banned_instances, blocked_users, \
    joined_or_modding_communities, \
    login_required_if_private_instance, communities_banned_from, reported_posts, user_notes, moderating_communities_ids, \
    approval_required, block_honey_pot, user_pronouns, community_membership_private


@bp.route('/topic/<path:topic_path>', methods=['GET'])
@login_required_if_private_instance
def show_topic(topic_path):
    block_honey_pot()
    page = request.args.get('page', 0, type=int)
    sort = request.args.get('sort', '' if current_user.is_anonymous else current_user.default_sort)
    if sort == 'scaled':
        sort = ''
    result_id = request.args.get('result_id', gibberish(15)) if current_user.is_authenticated else None
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    post_layout = request.args.get('layout', 'list' if not low_bandwidth else None)
    content_type = request.args.get('content_type', 'posts')
    tag = request.args.get('tag', '')
    page_length = 20 if low_bandwidth else current_app.config['PAGE_LENGTH']
    if current_user.is_authenticated and current_user.page_length and current_user.page_length < page_length:
        page_length = current_user.page_length
    if post_layout == 'masonry':
        page_length = 200
    elif post_layout == 'masonry_wide':
        page_length = 300

    # translate topic_name from /topic/fediverse to topic_id
    topic_url_parts = topic_path.split('/')
    last_topic_machine_name = topic_url_parts[-1]
    breadcrumbs = []
    existing_url = '/topic'
    topic = None
    for url_part in topic_url_parts:
        topic = Topic.query.filter(Topic.machine_name == url_part.strip().lower()).first()
        if topic:
            breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
            breadcrumb.text = topic.name
            breadcrumb.url = f"{existing_url}/{topic.machine_name}" if topic.machine_name != last_topic_machine_name else ''
            breadcrumbs.append(breadcrumb)
            existing_url = breadcrumb.url
        else:
            abort(404)
    current_topic = topic

    if current_topic:
        if current_user.is_authenticated:
            pc = community_membership_private(current_user.id)
            if len(pc) == 0:  # tuples must have at least 2 elements, I think?
                private_communities = [0, 0]
            else:
                private_communities = pc + [0]
        else:
            private_communities = ()
        if len(private_communities) == 0:
            private_communities = [0, 0]

        # get posts from communities in that topic
        if current_topic.show_posts_in_children:  # include posts from child topics
            topic_ids = get_all_child_topic_ids(current_topic)
        else:
            topic_ids = [current_topic.id]
        community_ids = db.session.execute(
            text('SELECT id FROM community WHERE (private is false OR id IN :private_communities) AND banned is false AND topic_id IN :topic_ids'),
            {'topic_ids': tuple(topic_ids), 'private_communities': tuple(private_communities)}).scalars()

        community_ids = list(community_ids)

        topic_communities = Community.query.filter(
            Community.topic_id == current_topic.id, Community.banned == False, Community.total_subscriptions_count > 0).\
            filter(Community.instance_id.not_in(blocked_or_banned_instances(current_user.get_id()))).\
            filter(Community.id.not_in(blocked_communities(current_user.get_id()))).\
            filter(or_(Community.private == False, Community.id.in_(community_membership_private(current_user.get_id())))).\
            order_by(desc(Community.total_subscriptions_count))

        posts = None
        comments = None
        if content_type == 'posts':
            post_ids = get_deduped_post_ids(result_id, community_ids, sort, tag)
            has_next_page = len(post_ids) > page + 1 * page_length
            post_ids = paginate_post_ids(post_ids, page, page_length=page_length)
            posts = post_ids_to_models(post_ids, sort)

            next_url = url_for('topic.show_topic', topic_path=topic_path, result_id=result_id,
                               page=page + 1, sort=sort, layout=post_layout) if has_next_page else None
            prev_url = url_for('topic.show_topic', topic_path=topic_path, result_id=result_id,
                               page=page - 1, sort=sort, layout=post_layout) if page > 0 else None
        elif content_type == 'comments':
            comments = PostReply.query.filter(PostReply.community_id.in_(community_ids))

            # filter out nsfw and nsfl if desired
            if current_user.is_anonymous:
                if current_app.config['CONTENT_WARNING']:
                    comments = comments.filter(PostReply.from_bot == False, PostReply.deleted == False)
                else:
                    comments = comments.filter(PostReply.from_bot == False, PostReply.nsfw == False,
                                               PostReply.deleted == False)
            else:
                if current_user.ignore_bots == 1:
                    comments = comments.filter(PostReply.from_bot == False)
                if current_user.hide_nsfw == 1:
                    comments = comments.filter(PostReply.nsfw == False)

                comments = comments.filter(PostReply.deleted == False)

                # filter instances
                instance_ids = blocked_or_banned_instances(current_user.id)
                if instance_ids:
                    comments = comments.filter(
                        or_(PostReply.instance_id.not_in(instance_ids), PostReply.instance_id == None))

                # filter blocked users
                blocked_accounts = blocked_users(current_user.id)
                if blocked_accounts:
                    comments = comments.filter(PostReply.user_id.not_in(blocked_accounts))

            if sort == '' or sort == 'hot':
                comments = comments.order_by(desc(PostReply.posted_at))
            elif sort == 'top_12h':
                comments = comments.filter(PostReply.posted_at > utcnow() - timedelta(hours=12)).order_by(
                    desc(PostReply.up_votes - PostReply.down_votes))
            elif sort == 'top':
                comments = comments.filter(PostReply.posted_at > utcnow() - timedelta(hours=24)).order_by(
                    desc(PostReply.up_votes - PostReply.down_votes))
            elif sort == 'top_1w':
                comments = comments.filter(PostReply.posted_at > utcnow() - timedelta(days=7)).order_by(
                    desc(PostReply.up_votes - PostReply.down_votes))
            elif sort == 'top_1m':
                comments = comments.filter(PostReply.posted_at > utcnow() - timedelta(days=28)).order_by(
                    desc(PostReply.up_votes - PostReply.down_votes))
            elif sort == 'top_1y':
                comments = comments.filter(PostReply.posted_at > utcnow() - timedelta(days=365)).order_by(
                    desc(PostReply.up_votes - PostReply.down_votes))
            elif sort == 'top_all':
                comments = comments.order_by(desc(PostReply.up_votes - PostReply.down_votes))
            elif sort == 'new' or sort == 'active':
                comments = comments.order_by(desc(PostReply.posted_at))
            elif sort == 'old':
                comments = comments.order_by(asc(PostReply.posted_at))
            per_page = 100
            comments = comments.paginate(page=page, per_page=per_page, error_out=False)
            next_url = url_for('topic.show_topic', topic_path=topic_path,
                               page=comments.next_num, sort=sort, layout=post_layout,
                               content_type=content_type) if comments.has_next else None
            prev_url = url_for('topic.show_topic', topic_path=topic_path,
                               page=comments.prev_num, sort=sort, layout=post_layout,
                               content_type=content_type) if comments.has_prev and page != 1 else None
        else:
            abort(400)

        sub_topics = Topic.query.filter_by(parent_id=current_topic.id).order_by(Topic.name).all()

        # Voting history
        if current_user.is_authenticated:
            recently_upvoted = recently_upvoted_posts(current_user.id)
            recently_downvoted = recently_downvoted_posts(current_user.id)
            communities_banned_from_list = communities_banned_from(current_user.id)
            content_filters = user_filters_posts(current_user.id)
        else:
            recently_upvoted = []
            recently_downvoted = []
            communities_banned_from_list = []
            content_filters = {}

        return render_template('topic/show_topic.html', title=_(current_topic.name), posts=posts, topic=current_topic,
                               sort=sort,
                               page=page, post_layout=post_layout, next_url=next_url, prev_url=prev_url,
                               comments=comments,
                               topic_communities=topic_communities, content_filters=user_filters_posts(current_user.id) if current_user.is_authenticated else {},
                               sub_topics=sub_topics, topic_path=topic_path, breadcrumbs=breadcrumbs,
                               tags=hashtags_used_in_communities(community_ids, content_filters),
                               joined_communities=joined_or_modding_communities(current_user.get_id()),
                               rss_feed=f"{current_app.config['SERVER_URL']}/topic/{topic_path}.rss",
                               rss_feed_name=f"{current_topic.name} on {g.site.name}", content_type=content_type,
                               reported_posts=reported_posts(current_user.get_id(), current_user.get_id() in g.admin_ids),
                               user_notes=user_notes(current_user.get_id()),
                               moderated_community_ids=moderating_communities_ids(current_user.get_id()),
                               show_post_community=True, recently_upvoted=recently_upvoted,
                               recently_downvoted=recently_downvoted,
                               user_pronouns=user_pronouns(),
                               inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                               POST_TYPE_LINK=POST_TYPE_LINK, POST_TYPE_IMAGE=POST_TYPE_IMAGE,
                               POST_TYPE_VIDEO=POST_TYPE_VIDEO,
                               SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                               communities_banned_from_list=communities_banned_from_list
                               )
    else:
        abort(404)


@bp.route('/topic/<path:topic_path>.rss', methods=['GET'])
@cache.cached(timeout=600)
def show_topic_rss(topic_path):
    topic_url_parts = topic_path.split('/')
    last_topic_machine_name = topic_url_parts[-1]
    topic = Topic.query.filter(Topic.machine_name == last_topic_machine_name.strip().lower()).first()

    if topic:
        if topic.show_posts_in_children:  # include posts from child topics
            topic_ids = get_all_child_topic_ids(topic)
        else:
            topic_ids = [topic.id]

        community_ids = db.session.execute(
            text('SELECT id FROM community WHERE banned is false AND private is false AND topic_id IN :topic_ids'),
            {'topic_ids': tuple(topic_ids)}).scalars()
        post_ids = get_deduped_post_ids('', list(community_ids), 'new')
        post_ids = paginate_post_ids(post_ids, 0, page_length=100)
        posts = post_ids_to_models(post_ids, 'new')

        fg = FeedGenerator()
        fg.id(f"{current_app.config['SERVER_URL']}/topic/{last_topic_machine_name}")
        fg.title(f'{topic.name} on {g.site.name}')
        fg.link(href=f"{current_app.config['SERVER_URL']}/topic/{last_topic_machine_name}", rel='alternate')
        fg.logo(f"{current_app.config['SERVER_URL']}/static/images/apple-touch-icon.png")
        fg.subtitle(' ')
        fg.link(href=f"{current_app.config['SERVER_URL']}/topic/{last_topic_machine_name}.rss", rel='self')
        fg.language('en')

        for post in posts:
            fe = fg.add_entry()
            fe.title(post.title)
            if post.slug:
                fe.link(href=f"{current_app.config['SERVER_URL']}{post.slug}")
            else:
                fe.link(href=f"{current_app.config['SERVER_URL']}/post/{post.id}")
            if post.url:
                type = mimetype_from_url(post.url)
                if type and not type.startswith('text/'):
                    fe.enclosure(post.url, type=type)
            fe.description(post.body_html)
            fe.guid(post.profile_id(), permalink=True)
            fe.author(name=post.author.user_name)
            fe.pubDate(post.created_at.replace(tzinfo=timezone.utc))

        response = make_response(fg.rss_str())
        response.headers.set('Content-Type', 'application/rss+xml')
        response.headers.add_header('ETag', f"{topic.id}_{hash(g.site.last_active)}")
        response.headers.add_header('Cache-Control', 'no-cache, max-age=600, must-revalidate')
        return response
    else:
        abort(404)


@bp.route('/topic/<topic_name>/submit', methods=['GET', 'POST'])
@login_required
@validation_required
@approval_required
def topic_create_post(topic_name):
    topic = Topic.query.filter(Topic.machine_name == topic_name.strip().lower()).first()
    if not topic:
        abort(404)
    communities = Community.query.filter_by(topic_id=topic.id, banned=False).order_by(Community.title).all()
    child_topics = [topic.id for topic in Topic.query.filter(Topic.parent_id == topic.id).all()]
    sub_communities = Community.query.filter_by(banned=False).filter(Community.topic_id.in_(child_topics)).order_by(
        Community.title).all()
    if request.form.get('community_id', '') != '':
        community = Community.query.get_or_404(int(request.form.get('community_id')))
        return redirect(url_for('community.join_then_add', actor=community.link()))
    return render_template('topic/topic_create_post.html', communities=communities, sub_communities=sub_communities,
                           topic=topic,
                           SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR,
                           )


@bp.route('/topic/<int:topic_id>/notification', methods=['GET', 'POST'])
@login_required
def topic_notification(topic_id: int):
    # Toggle whether the current user is subscribed to notifications about this community's posts or not
    topic = Topic.query.get_or_404(topic_id)
    existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == topic.id,
                                                                  NotificationSubscription.user_id == current_user.id,
                                                                  NotificationSubscription.type == NOTIF_TOPIC).first()
    if existing_notification:
        db.session.delete(existing_notification)
        db.session.commit()
    else:  # no subscription yet, so make one
        new_notification = NotificationSubscription(name=topic.name, user_id=current_user.id, entity_id=topic.id,
                                                    type=NOTIF_TOPIC)
        db.session.add(new_notification)
        db.session.commit()

    return render_template('topic/_notification_toggle.html', topic=topic)


@bp.route('/topics/new', methods=['GET', 'POST'])
@login_required
def suggest_topics():
    form = SuggestTopicsForm()
    if not current_user.trustworthy():
        return redirect(url_for('topic.suggestion_denied'))
    if form.validate_on_submit():
        subject = _('New topic suggestion from %(site_name)s', site_name=g.site.name)
        recipients = g.site.contact_email
        topic_name = form.topic_name.data
        communities_for_topic = form.communities_for_topic.data
        send_topic_suggestion(communities_for_topic, current_user, recipients, subject, topic_name)
        flash(_('Thank you for the topic suggestion, it has been sent to the site administrator(s).'))
        return redirect(url_for('main.list_topics'))
    else:
        return render_template('topic/suggest_topics.html', form=form, title=_('Suggest a topic'),
                               )


@bp.route('/topic/suggestion-denied', methods=['GET'])
@login_required
def suggestion_denied():
    return render_template('topic/suggestion_denied.html')


def get_all_child_topic_ids(topic: Topic) -> List[int]:
    # recurse down the topic tree, gathering all the topic IDs found
    topic_ids = [topic.id]
    for child_topic in Topic.query.filter(Topic.parent_id == topic.id):
        topic_ids.extend(get_all_child_topic_ids(child_topic))
    return topic_ids
