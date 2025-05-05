from collections import namedtuple

from flask import request, url_for, g, abort, flash, redirect
from flask_login import current_user, login_required
from flask_babel import _
from sqlalchemy import or_, desc

from app import db, cache
from app.constants import SRC_WEB, POST_STATUS_REVIEWING
from app.instance import bp
from app.models import Instance, User, Post, read_posts
from app.utils import render_template, moderating_communities, joined_communities, menu_topics, blocked_domains, \
    blocked_instances, blocked_communities, blocked_users, user_filters_home, recently_upvoted_posts, \
    recently_downvoted_posts, menu_instance_feeds, menu_my_feeds, menu_subscribed_feeds
from app.shared.site import block_remote_instance, unblock_remote_instance


@bp.route('/instances', methods=['GET'])
def list_instances():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    filter = request.args.get('filter', '')
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    instances = Instance.query.order_by(Instance.domain)
    if search:
        instances = instances.filter(Instance.domain.ilike(f"%{search}%"))
    title = _('Instances')
    if filter:
        if filter == 'trusted':
            instances = instances.filter(Instance.trusted == True)
            title = _('Trusted instances')
        elif filter == 'online':
            instances = instances.filter(Instance.dormant == False, Instance.gone_forever == False)
            title = _('Online instances')
        elif filter == 'dormant':
            instances = instances.filter(Instance.dormant == True, Instance.gone_forever == False)
            title = _('Dormant instances')
        elif filter == 'gone_forever':
            instances = instances.filter(Instance.gone_forever == True)
            title = _('Gone forever instances')

    # Pagination
    instances = instances.paginate(page=page, per_page=50, error_out=False)
    next_url = url_for('instance.list_instances', page=instances.next_num) if instances.has_next else None
    prev_url = url_for('instance.list_instances', page=instances.prev_num) if instances.has_prev and page != 1 else None

    return render_template('instance/list_instances.html', instances=instances,
                           title=title, search=search, filter=filter,
                           next_url=next_url, prev_url=prev_url,
                           low_bandwidth=low_bandwidth,
                           site=g.site, )


@bp.route('/instance/<instance_domain>', methods=['GET'])
def instance_overview(instance_domain):
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    instance = Instance.query.filter(Instance.domain == instance_domain).first()
    if instance is None:
        abort(404)

    return render_template('instance/overview.html', instance=instance,
                           site=g.site,
                           title=_('%(instance)s overview', instance=instance.domain),
                           )


@bp.route('/instance/<instance_domain>/people', methods=['GET'])
def instance_people(instance_domain):
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    filter = request.args.get('filter', '')
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    instance = Instance.query.filter(Instance.domain == instance_domain).first()
    if instance is None:
        abort(404)

    if current_user.is_authenticated and current_user.is_admin():
        people = User.query.filter_by(instance_id=instance.id, deleted=False, banned=False).order_by(desc(User.last_seen))
    else:
        people = User.query.filter_by(instance_id=instance.id, deleted=False, banned=False, searchable=True).order_by(desc(User.last_seen))

    # Pagination
    people = people.paginate(page=page, per_page=100 if current_user.is_authenticated and not low_bandwidth else 50, error_out=False)
    next_url = url_for('instance.instance_people', page=people.next_num, instance_domain=instance_domain) if people.has_next else None
    prev_url = url_for('instance.instance_people', page=people.prev_num, instance_domain=instance_domain) if people.has_prev and page != 1 else None

    return render_template('instance/people.html', people=people, instance=instance, next_url=next_url, prev_url=prev_url,
                           site=g.site,
                           title=_('People from %(instance)s', instance=instance.domain),
                           )


@bp.route('/instance/<instance_domain>/posts', methods=['GET'])
def instance_posts(instance_domain):
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    instance = Instance.query.filter(Instance.domain == instance_domain).first()
    if instance is None:
        abort(404)

    if current_user.is_anonymous:
        posts = Post.query.filter(Post.instance_id == instance.id, Post.from_bot == False, Post.nsfw == False, Post.nsfl == False, Post.deleted == False, Post.status > POST_STATUS_REVIEWING)
        content_filters = {}
    else:
        posts = Post.query.filter(Post.instance_id == instance.id, Post.deleted == False, Post.status > POST_STATUS_REVIEWING)

        if current_user.ignore_bots == 1:
            posts = posts.filter(Post.from_bot == False)
        if current_user.hide_nsfl == 1:
            posts = posts.filter(Post.nsfl == False)
        if current_user.hide_nsfw == 1:
            posts = posts.filter(Post.nsfw == False)
        if current_user.hide_read_posts:
            posts = posts.outerjoin(read_posts, (Post.id == read_posts.c.read_post_id) & (read_posts.c.user_id == current_user.id))
            posts = posts.filter(read_posts.c.read_post_id.is_(None))  # Filter where there is no corresponding read post for the current user

        domains_ids = blocked_domains(current_user.id)
        if domains_ids:
            posts = posts.filter(or_(Post.domain_id.not_in(domains_ids), Post.domain_id == None))
        instance_ids = blocked_instances(current_user.id)
        if instance_ids:
            posts = posts.filter(or_(Post.instance_id.not_in(instance_ids), Post.instance_id == None))
        community_ids = blocked_communities(current_user.id)
        if community_ids:
            posts = posts.filter(Post.community_id.not_in(community_ids))
        # filter blocked users
        blocked_accounts = blocked_users(current_user.id)
        if blocked_accounts:
            posts = posts.filter(Post.user_id.not_in(blocked_accounts))
        content_filters = user_filters_home(current_user.id)

    # Sorting
    posts = posts.order_by(desc(Post.posted_at))

    # Pagination
    posts = posts.paginate(page=page, per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                           error_out=False)
    next_url = url_for('instance.instance_posts', page=posts.next_num, instance_domain=instance_domain) if posts.has_next else None
    prev_url = url_for('instance.instance_posts', page=posts.prev_num, instance_domain=instance_domain) if posts.has_prev and page != 1 else None

    # Voting history
    if current_user.is_authenticated:
        recently_upvoted = recently_upvoted_posts(current_user.id)
        recently_downvoted = recently_downvoted_posts(current_user.id)
    else:
        recently_upvoted = []
        recently_downvoted = []

    breadcrumbs = []
    breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
    breadcrumb.text = _('Home')
    breadcrumb.url = '/'
    breadcrumbs.append(breadcrumb)
    breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
    breadcrumb.text = _('Instances')
    breadcrumb.url = '/instances'
    breadcrumbs.append(breadcrumb)
    breadcrumb = namedtuple("Breadcrumb", ['text', 'url'])
    breadcrumb.text = instance.domain
    breadcrumb.url = '/instance/' + instance.domain
    breadcrumbs.append(breadcrumb)

    return render_template('instance/posts.html', posts=posts, show_post_community=True, instance=instance,
                           low_bandwidth=low_bandwidth, recently_upvoted=recently_upvoted, breadcrumbs=breadcrumbs,
                           recently_downvoted=recently_downvoted,
                           next_url=next_url, prev_url=prev_url,
                           #rss_feed=f"https://{current_app.config['SERVER_NAME']}/feed",
                           #rss_feed_name=f"Posts on " + g.site.name,
                           title=_("Posts from %(instance)s", instance=instance.domain),
                           content_filters=content_filters,
                           site=g.site,
                           )


@bp.route('/instance/<int:instance_id>/block', methods=['GET'])
@login_required
def instance_block(instance_id):
    instance = Instance.query.get_or_404(instance_id)
    block_remote_instance(instance_id, SRC_WEB)
    flash(_('Content from %(instance_domain)s will be hidden.', instance_domain=instance.domain))
    goto = request.args.get('redirect') if 'redirect' in request.args else url_for('user.user_settings_filters')
    return redirect(goto)


@bp.route('/instance/<int:instance_id>/unblock', methods=['GET'])
@login_required
def instance_unblock(instance_id):
    instance = Instance.query.get_or_404(instance_id)
    unblock_remote_instance(instance_id, SRC_WEB)
    flash(_('%(instance_domain)s has been unblocked.', instance_domain=instance.domain))
    goto = request.args.get('redirect') if 'redirect' in request.args else url_for('user.user_settings_filters')
    return redirect(goto)
