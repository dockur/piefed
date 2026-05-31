from flask import redirect, url_for, flash, current_app, abort, g
from flask_babel import _
from flask_login import current_user, login_required

from app import db, cache
from app.activitypub.signature import send_post_request
from app.auth import bp
from app.auth.forms import ChooseTopicsForm, FilterSetupForm
from app.auth.util import get_country
from app.constants import SUBSCRIPTION_NONMEMBER
from app.models import User, Topic, Community, CommunityJoinRequest, CommunityMember, Filter, InstanceChooser, Language
from app.utils import render_template, joined_communities, community_membership, get_setting, num_topics, ip_address


@bp.route('/instance_chooser')
def onboarding_instance_chooser():
    if get_setting('enable_instance_chooser', False):
        instances = InstanceChooser.query.all()
        language_ids = set()
        for instance in instances:
            language_ids.add(instance.language_id)
        languages = Language.query.filter(Language.id.in_(language_ids)).all()
        return render_template('auth/instance_chooser.html', title=_('Which server do you want to join?'),
                               instances=instances, languages=languages, closed=g.site.registration_mode == 'Closed')
    else:
        return redirect(url_for('auth.register'))


@bp.route('/filter_selection', methods=['GET', 'POST'])
@login_required
def filter_selection():
    if get_setting('filter_selection', True):
        form = FilterSetupForm()
        if form.validate_on_submit():
            if form.trump_musk_level.data >= 0:
                existing_filters = Filter.query.filter(Filter.user_id == current_user.id, Filter.title == 'Trump & Musk').first()
                if existing_filters is not None:
                    content_filter = Filter(title='Trump & Musk', filter_home=True, filter_posts=True, filter_replies=False, hide_type=form.trump_musk_level.data, keywords='trump\nmusk', expire_after=None, user_id=current_user.id)
                    db.session.add(content_filter)
            current_user.ignore_bots = form.ignore_bots.data
            current_user.hide_nsfw = form.hide_nsfw.data
            current_user.hide_nsfl = form.hide_nsfl.data
            current_user.hide_gen_ai = form.hide_gen_ai.data
            db.session.commit()
            return redirect(url_for('auth.choose_topics'))
        else:
            form.hide_nsfw.data = 0 if current_app.config['CONTENT_WARNING'] or g.site.enable_nsfw else 1
            return render_template('auth/filter_selection.html', form=form)
    else:
        return redirect(url_for('auth.choose_topics'))


@bp.route('/choose_topics', methods=['GET', 'POST'])
@login_required
def choose_topics():
    mark_onboarding_as_finished()
    if get_setting('choose_topics', True) and num_topics() > 0:
        form = ChooseTopicsForm()
        choices, selections = topics_for_form()
        form.chosen_topics.choices = choices
        if form.validate_on_submit():
            if form.chosen_topics.data:
                for topic_id in form.chosen_topics.data:
                    join_topic(topic_id)
                flash(_('You have joined some communities relating to those interests. Find more on the Explore menu or browse the home page.'))
                cache.delete_memoized(joined_communities, current_user.id)
                return redirect(url_for('main.index'))
            else:
                flash(_('You did not choose any topics. Would you like to choose individual communities instead?'))
                return redirect(url_for('main.list_communities'))
        else:
            form.chosen_topics.data = selections    # topics that match the user's country are ticked by default
            return render_template('auth/choose_topics.html', form=form,
                                   )
    else:
        flash(_('Please join some communities you\'re interested in and then go to the home page by clicking on the logo above.'))
        return redirect(url_for('main.list_communities'))

def mark_onboarding_as_finished():
    current_user.finished_onboarding = True
    db.session.commit()

def join_topic(topic_id):
    communities = Community.query.filter_by(topic_id=topic_id, banned=False).all()
    for community in communities:
        if not community.user_is_banned(current_user) and community_membership(current_user, community) == SUBSCRIPTION_NONMEMBER:
            if not community.is_local():
                join_request = CommunityJoinRequest(user_id=current_user.id, community_id=community.id)
                db.session.add(join_request)
                db.session.commit()
                send_community_follow(community.id, join_request.uuid, current_user.id)

            existing_member = CommunityMember.query.filter(CommunityMember.community_id == community.id, CommunityMember.user_id == current_user.id).first()
            if not existing_member:
                member = CommunityMember(user_id=current_user.id, community_id=community.id)
                db.session.add(member)
                db.session.commit()
            cache.delete_memoized(community_membership, current_user, community)


def topics_for_form():
    topics = Topic.query.filter_by(parent_id=None).order_by(Topic.name).all()
    result = []         # topics, arranged into a hierarchy
    selections = []     # topic ids that match the user's country
    user_country = get_country(ip_address())
    for topic in topics:
        result.append((topic.id, topic.name))
        if user_country and user_country in topic.countries:
            selections.append(topic.id)
        sub_topics = Topic.query.filter_by(parent_id=topic.id).order_by(Topic.name).all()
        for sub_topic in sub_topics:
            result.append((sub_topic.id, ' --- ' + sub_topic.name))
            if user_country and user_country in sub_topic.countries:
                selections.append(sub_topic.id)
    return result, selections


def send_community_follow(community_id: int, join_request_id: int, user_id: int):
    with current_app.app_context():
        user = User.query.get(user_id)
        community = Community.query.get(community_id)
        if not community.instance.gone_forever:
            follow = {
                "actor": user.public_url(),
                "to": [community.public_url()],
                "object": community.public_url(),
                "type": "Follow",
                "id": f"{current_app.config['SERVER_URL']}/activities/follow/{join_request_id}"
            }
            send_post_request(community.ap_inbox_url, follow, user.private_key, user.public_url() + '#main-key')
