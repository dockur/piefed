from flask import request, flash, url_for, redirect, abort
from flask_babel import _, g
from flask_login import current_user
from sqlalchemy import or_, desc, text

from app import limiter, db, current_app
from app.activitypub.util import resolve_remote_post_from_search
from app.community.forms import RetrieveRemotePost
from app.community.util import search_for_community
from app.constants import POST_STATUS_REVIEWING
from app.models import Post, Language, Community, Instance, PostReply
from app.search import bp
from app.utils import render_template, blocked_domains, blocked_or_banned_instances, \
    communities_banned_from, recently_upvoted_posts, recently_downvoted_posts, blocked_users, blocked_communities, \
    show_ban_message, login_required, login_required_if_private_instance, moderating_communities_ids, get_setting, \
    user_pronouns


@bp.route('/search', methods=['GET', 'POST'])
@limiter.limit("100 per day;20 per 5 minutes", exempt_when=lambda: current_user.is_authenticated)
@login_required_if_private_instance
def run_search():
    if 'bingbot' in request.user_agent.string:  # Stop bingbot from running nonsense searches
        abort(404)
    if current_user.is_authenticated:
        banned_from = communities_banned_from(current_user.id)
    else:
        banned_from = []

    page = request.args.get('page', 1, type=int)
    community = request.args.get('community', '').strip()
    language_id = request.args.get('language', 0, type=int)
    type = request.args.get('type', 0, type=int)
    software = request.args.get('software', '')
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    q = (request.args.get('q') or '').strip()
    sort_by = request.args.get('sort_by', '')
    search_for = request.args.get('search_for', 'posts')
    nsfw = request.args.get('nsfw', '')
    minimum_upvote = request.args.get('minimum_upvote', '')

    community_id = request.args.get('community_id', 0, int)
    if community_id == 0 and community:
        if q == '' and search_for == 'communities':     # when trying to find a community, people go to the search and then type stuff into the field labelled 'Community'. This is wrong.
            return redirect(f'/communities?search={community}&language_id={language_id}')
        else:
            if not community.startswith('!'):
                community = f'!{community}'
            if not "@" in community:
                community = community + "@" + current_app.config['SERVER_NAME']
            community_obj = search_for_community(community, allow_fetch=False)
            if community_obj:
                community_id = community_obj.id

    if q != '' or type != 0 or language_id != 0 or community_id != 0 or nsfw != '' or minimum_upvote != '':
        posts = None
        db.session.execute(text("SET work_mem = '100MB';"))
        if search_for == 'posts':
            posts = Post.query.filter(Post.deleted == False, Post.status > POST_STATUS_REVIEWING, Post.private == False)
            if current_user.is_authenticated:
                if current_user.ignore_bots == 1:
                    posts = posts.filter(Post.from_bot == False)
                if current_user.hide_nsfl == 1:
                    posts = posts.filter(Post.nsfl == False)

                if nsfw == '' and current_user.hide_nsfw == 1:
                    posts = posts.filter(Post.nsfw == False)
                else:
                    if nsfw == 'exclude':
                        posts = posts.filter(Post.nsfw == False)
                    elif nsfw == 'only':
                        posts = posts.filter(Post.nsfw == True)
                    elif nsfw == 'include':
                        pass
                domains_ids = blocked_domains(current_user.id)
                if domains_ids:
                    posts = posts.filter(or_(Post.domain_id.not_in(domains_ids), Post.domain_id == None))
                instance_ids = blocked_or_banned_instances(current_user.id)
                if instance_ids:
                    posts = posts.filter(or_(Post.instance_id.not_in(instance_ids), Post.instance_id == None))
                community_ids = blocked_communities(current_user.id)
                if community_ids:
                    posts = posts.filter(Post.community_id.not_in(community_ids))
                # filter blocked users
                blocked_accounts = blocked_users(current_user.id)
                if blocked_accounts:
                    posts = posts.filter(Post.user_id.not_in(blocked_accounts))
                if banned_from:
                    posts = posts.filter(Post.community_id.not_in(banned_from))
            else:
                posts = posts.filter(Post.from_bot == False)
                posts = posts.filter(Post.nsfl == False)
                if nsfw == 'exclude' or nsfw == '':
                    posts = posts.filter(Post.nsfw == False)
                elif nsfw == 'only':
                    posts = posts.filter(Post.nsfw == True)
                elif nsfw == 'include':
                    pass
                posts = posts.filter(Post.nsfw == False)

            if minimum_upvote:
                posts = posts.filter(Post.up_votes - Post.down_votes >= int(minimum_upvote))
            posts = posts.filter(Post.indexable == True)
            if q is not None:
                posts = posts.search(q, sort=True if sort_by == '' else False)
            if type != 0:
                posts = posts.filter(Post.type == type)
            if community_id:
                posts = posts.filter(Post.community_id == community_id)
            if language_id:
                posts = posts.filter(Post.language_id == language_id)
            if software:
                posts = posts.join(Instance, Post.instance_id == Instance.id).filter(Instance.software == software)
            if sort_by == 'date':
                posts = posts.order_by(desc(Post.posted_at))
            elif sort_by == 'top':
                posts = posts.order_by(desc(Post.up_votes - Post.down_votes))

            posts = posts.paginate(page=page,
                                   per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                                   error_out=False)

            next_url = url_for('search.run_search', page=posts.next_num, q=q) if posts.has_next else None
            prev_url = url_for('search.run_search', page=posts.prev_num, q=q) if posts.has_prev and page != 1 else None

        replies = None
        if search_for == 'comments':
            replies = PostReply.query.filter(PostReply.deleted == False, PostReply.private == False)
            if current_user.is_authenticated:
                if current_user.ignore_bots == 1:
                    replies = replies.filter(PostReply.from_bot == False)
                if current_user.hide_nsfw == 1:
                    replies = replies.filter(PostReply.nsfw == False)
                instance_ids = blocked_or_banned_instances(current_user.id)
                if instance_ids:
                    replies = replies.filter(
                        or_(PostReply.instance_id.not_in(instance_ids), PostReply.instance_id == None))
                community_ids = blocked_communities(current_user.id)
                if community_ids:
                    replies = replies.filter(PostReply.community_id.not_in(community_ids))
                # filter blocked users
                blocked_accounts = blocked_users(current_user.id)
                if blocked_accounts:
                    replies = replies.filter(PostReply.user_id.not_in(blocked_accounts))
                if banned_from:
                    replies = replies.filter(PostReply.community_id.not_in(banned_from))
            else:
                replies = replies.filter(PostReply.from_bot == False)
                replies = replies.filter(PostReply.nsfw == False)

            replies = replies.join(Post, PostReply.post_id == Post.id).filter(Post.indexable == True,
                                                                              Post.deleted == False,
                                                                              Post.status > POST_STATUS_REVIEWING)
            replies = replies.filter(PostReply.indexable == True)
            if q is not None:
                replies = replies.search(q, sort=True if sort_by == '' else False)
            if type != 0:
                replies = replies.filter(Post.type == type)
            if community_id:
                replies = replies.filter(PostReply.community_id == community_id)
            if language_id:
                replies = replies.filter(PostReply.language_id == language_id)
            if software:
                replies = replies.join(Instance, PostReply.instance_id == Instance.id).filter(
                    Instance.software == software)
            if sort_by == 'date':
                replies = replies.order_by(desc(PostReply.posted_at))
            elif sort_by == 'top':
                replies = replies.order_by(desc(PostReply.up_votes - PostReply.down_votes))

            replies = replies.paginate(page=page,
                                       per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                                       error_out=False)

            next_url = url_for('search.run_search', page=replies.next_num, q=q,
                               search_for=search_for) if replies.has_next else None
            prev_url = url_for('search.run_search', page=replies.prev_num, q=q,
                               search_for=search_for) if replies.has_prev and page != 1 else None

        communities = None
        if search_for == 'communities':
            return redirect(f'/communities?search={q}&language_id={language_id}')

        # Voting history
        if current_user.is_authenticated:
            recently_upvoted = recently_upvoted_posts(current_user.id)
            recently_downvoted = recently_downvoted_posts(current_user.id)
        else:
            recently_upvoted = []
            recently_downvoted = []

        languages = Language.query.order_by(Language.name).all()
        instance_software = Instance.unique_software_names()
        return render_template('search/results.html', title=_('Search results for %(q)s', q=q), posts=posts,
                               replies=replies,
                               community_results=communities, q=q,
                               community_str=community, community_id=community_id, language_id=language_id,
                               search_for=search_for, sort_by=sort_by, type=type,
                               software=software, nsfw=nsfw, minimum_upvote=minimum_upvote,
                               languages=languages, instance_software=instance_software,
                               next_url=next_url, prev_url=prev_url, show_post_community=True,
                               recently_upvoted=recently_upvoted,
                               recently_downvoted=recently_downvoted,
                               user_pronouns=user_pronouns(),
                               moderated_community_ids=moderating_communities_ids(current_user.get_id()),
                               )

    else:
        languages = Language.query.order_by(Language.name).all()
        communities = Community.query.filter(Community.banned == False).order_by(Community.name)
        instance_software = Instance.unique_software_names()
        if current_user.is_authenticated:
            communities = communities.filter(Community.id.not_in(banned_from))

        return render_template('search/start.html', title=_('Search'), communities=communities.all(),
                               languages=languages, instance_software=instance_software,
                               is_admin=current_user.is_authenticated and current_user.is_admin(),
                               is_staff=current_user.is_authenticated and current_user.is_staff(),
                               default_user_add_remote=get_setting("allow_default_user_add_remote_community", True)
                               )


@bp.route('/retrieve_remote_post', methods=['GET', 'POST'])
@login_required
def retrieve_remote_post():
    if current_user.banned:
        return show_ban_message()
    form = RetrieveRemotePost()
    new_post = None
    if form.validate_on_submit():
        address = form.address.data.strip()
        new_post = resolve_remote_post_from_search(address)
        if new_post is None:
            flash(_('Post not found.'), 'warning')

    return render_template('community/retrieve_remote_post.html',
                           title=_('Retrieve Remote Post'), form=form, new_post=new_post)
