from collections import namedtuple
from datetime import timedelta, timezone
from random import randint

from feedgen.feed import FeedGenerator
from flask import request, flash, json, url_for, current_app, redirect, abort, make_response, g
from flask_login import login_required, current_user
from flask_babel import _
from sqlalchemy import text, desc, or_

from app.activitypub.signature import post_request
from app.constants import SUBSCRIPTION_NONMEMBER, POST_TYPE_IMAGE, POST_TYPE_LINK
from app.inoculation import inoculation
from app.models import Topic, Community, Post, utcnow, CommunityMember, CommunityJoinRequest, User
from app.topic import bp
from app import db, celery, cache
from app.topic.forms import ChooseTopicsForm
from app.utils import render_template, user_filters_posts, moderating_communities, joined_communities, \
    community_membership, blocked_domains, validation_required, mimetype_from_url


@bp.route('/topic/<path:topic_path>', methods=['GET'])
def show_topic(topic_path):

    page = request.args.get('page', 1, type=int)
    sort = request.args.get('sort', '' if current_user.is_anonymous else current_user.default_sort)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    post_layout = request.args.get('layout', 'list' if not low_bandwidth else None)

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
        # get posts from communities in that topic
        posts = Post.query.join(Community, Post.community_id == Community.id).filter(Community.topic_id == current_topic.id, Community.banned == False)

        # filter out nsfw and nsfl if desired
        if current_user.is_anonymous:
            posts = posts.filter(Post.from_bot == False, Post.nsfw == False, Post.nsfl == False)
            content_filters = {}
        else:
            if current_user.ignore_bots:
                posts = posts.filter(Post.from_bot == False)
            if current_user.show_nsfl is False:
                posts = posts.filter(Post.nsfl == False)
            if current_user.show_nsfw is False:
                posts = posts.filter(Post.nsfw == False)
            content_filters = user_filters_posts(current_user.id)

            domains_ids = blocked_domains(current_user.id)
            if domains_ids:
                posts = posts.filter(or_(Post.domain_id.not_in(domains_ids), Post.domain_id == None))

        # sorting
        if sort == '' or sort == 'hot':
            posts = posts.order_by(desc(Post.ranking)).order_by(desc(Post.posted_at))
        elif sort == 'top':
            posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=7)).order_by(desc(Post.score))
        elif sort == 'new':
            posts = posts.order_by(desc(Post.posted_at))
        elif sort == 'active':
            posts = posts.order_by(desc(Post.last_active))

        # paging
        per_page = 100
        if post_layout == 'masonry':
            per_page = 200
        elif post_layout == 'masonry_wide':
            per_page = 300
        posts = posts.paginate(page=page, per_page=per_page, error_out=False)

        topic_communities = Community.query.filter(Community.topic_id == current_topic.id).order_by(Community.name)

        next_url = url_for('topic.show_topic',
                           topic_path=topic_path,
                           page=posts.next_num, sort=sort, layout=post_layout) if posts.has_next else None
        prev_url = url_for('topic.show_topic',
                           topic_path=topic_path,
                           page=posts.prev_num, sort=sort, layout=post_layout) if posts.has_prev and page != 1 else None

        sub_topics = Topic.query.filter_by(parent_id=current_topic.id).order_by(Topic.name).all()

        return render_template('topic/show_topic.html', title=_(current_topic.name), posts=posts, topic=current_topic, sort=sort,
                               page=page, post_layout=post_layout, next_url=next_url, prev_url=prev_url,
                               topic_communities=topic_communities, content_filters=content_filters,
                               sub_topics=sub_topics, topic_path=topic_path, breadcrumbs=breadcrumbs,
                               rss_feed=f"https://{current_app.config['SERVER_NAME']}/topic/{topic_path}.rss",
                               rss_feed_name=f"{current_topic.name} on {g.site.name}",
                               show_post_community=True, moderating_communities=moderating_communities(current_user.get_id()),
                               joined_communities=joined_communities(current_user.get_id()),
                               inoculation=inoculation[randint(0, len(inoculation) - 1)],
                               POST_TYPE_LINK=POST_TYPE_LINK, POST_TYPE_IMAGE=POST_TYPE_IMAGE)
    else:
        abort(404)


@bp.route('/topic/<path:topic_path>.rss', methods=['GET'])
@cache.cached(timeout=600)
def show_topic_rss(topic_path):
    topic_url_parts = topic_path.split('/')
    last_topic_machine_name = topic_url_parts[-1]
    topic = Topic.query.filter(Topic.machine_name == last_topic_machine_name.strip().lower()).first()

    if topic:
        posts = Post.query.join(Community, Post.community_id == Community.id).filter(Community.topic_id == topic.id,
                                                                                     Community.banned == False)
        posts = posts.filter(Post.from_bot == False, Post.nsfw == False, Post.nsfl == False)
        posts = posts.order_by(desc(Post.created_at)).limit(100).all()

        fg = FeedGenerator()
        fg.id(f"https://{current_app.config['SERVER_NAME']}/topic/{last_topic_machine_name}")
        fg.title(f'{topic.name} on {g.site.name}')
        fg.link(href=f"https://{current_app.config['SERVER_NAME']}/topic/{last_topic_machine_name}", rel='alternate')
        fg.logo(f"https://{current_app.config['SERVER_NAME']}/static/images/apple-touch-icon.png")
        fg.subtitle(' ')
        fg.link(href=f"https://{current_app.config['SERVER_NAME']}/topic/{last_topic_machine_name}.rss", rel='self')
        fg.language('en')

        for post in posts:
            fe = fg.add_entry()
            fe.title(post.title)
            fe.link(href=f"https://{current_app.config['SERVER_NAME']}/post/{post.id}")
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


@bp.route('/choose_topics', methods=['GET', 'POST'])
@login_required
def choose_topics():
    form = ChooseTopicsForm()
    form.chosen_topics.choices = topics_for_form()
    if form.validate_on_submit():
        if form.chosen_topics.data:
            for topic_id in form.chosen_topics.data:
                join_topic(topic_id)
            flash(_('You have joined some communities relating to those interests. Find them on the Topics menu or browse the home page.'))
            cache.delete_memoized(joined_communities, current_user.id)
            return redirect(url_for('main.index'))
        else:
            flash(_('You did not choose any topics. Would you like to choose individual communities instead?'))
            return redirect(url_for('main.list_communities'))
    else:
        return render_template('topic/choose_topics.html', form=form,
                               moderating_communities=moderating_communities(current_user.get_id()),
                               joined_communities=joined_communities(current_user.get_id()),
                               )


@bp.route('/topic/<topic_name>/submit', methods=['GET', 'POST'])
@login_required
@validation_required
def topic_create_post(topic_name):
    topic = Topic.query.filter(Topic.machine_name == topic_name.strip().lower()).first()
    if not topic:
        abort(404)
    communities = Community.query.filter_by(topic_id=topic.id, banned=False).order_by(Community.title).all()
    if request.form.get('community_id', '') != '':
        community = Community.query.get_or_404(int(request.form.get('community_id')))
        return redirect(url_for('community.join_then_add', actor=community.link()))
    return render_template('topic/topic_create_post.html', communities=communities, topic=topic,
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()))


def topics_for_form():
    topics = Topic.query.filter_by(parent_id=None).order_by(Topic.name).all()
    result = []
    for topic in topics:
        result.append((topic.id, topic.name))
        sub_topics = Topic.query.filter_by(parent_id=topic.id).order_by(Topic.name).all()
        for sub_topic in sub_topics:
            result.append((sub_topic.id, ' --- ' + sub_topic.name))
    return result


def join_topic(topic_id):
    communities = Community.query.filter_by(topic_id=topic_id, banned=False).all()
    for community in communities:
        if not community.user_is_banned(current_user) and community_membership(current_user, community) == SUBSCRIPTION_NONMEMBER:
            if not community.is_local():
                join_request = CommunityJoinRequest(user_id=current_user.id, community_id=community.id)
                db.session.add(join_request)
                db.session.commit()
                if current_app.debug:
                    send_community_follow(community.id, join_request, current_user.id)
                else:
                    send_community_follow.delay(community.id, join_request.id, current_user.id)

            member = CommunityMember(user_id=current_user.id, community_id=community.id)
            db.session.add(member)
            db.session.commit()
            cache.delete_memoized(community_membership, current_user, community)


@celery.task
def send_community_follow(community_id, join_request_id, user_id):
    with current_app.app_context():
        user = User.query.get(user_id)
        community = Community.query.get(community_id)
        follow = {
            "actor": f"https://{current_app.config['SERVER_NAME']}/u/{user.user_name}",
            "to": [community.ap_profile_id],
            "object": community.ap_profile_id,
            "type": "Follow",
            "id": f"https://{current_app.config['SERVER_NAME']}/activities/follow/{join_request_id}"
        }
        success = post_request(community.ap_inbox_url, follow, user.private_key,
                               user.profile_id() + '#main-key')
