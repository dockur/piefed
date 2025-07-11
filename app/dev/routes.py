import random
from flask import request, flash, url_for, current_app, redirect, g, abort
from flask_login import current_user
from flask_babel import _

from app import db, cache
from app.activitypub.signature import RsaKeys
from app.admin.routes import unsubscribe_everyone_then_delete
from app.dev import bp
from app.dev.forms import AddTestCommunities, AddTestTopics, DeleteTestCommunities, DeleteTestTopics
from app.inoculation import inoculation
from app.models import Site, User, Community, CommunityMember, Language, Topic, utcnow
from app.utils import render_template, community_membership, moderating_communities, joined_communities, menu_topics, \
    markdown_to_html, permission_required, login_required


# a page for handy dev tools
@bp.route('/dev/tools', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def tools():
    if not current_app.debug:
        abort(404)
    communities_form = AddTestCommunities()
    topics_form = AddTestTopics()
    delete_communities_form = DeleteTestCommunities()
    delete_topics_form = DeleteTestTopics()

    # create 30 dev_ communities
    if communities_form.communities_submit.data and communities_form.validate():
        for n in range(30):
            # generate a keypair
            private_key, public_key = RsaKeys.generate_keypair()
            # generate a Title, name, description, rules, as strings, all the same with num from loop
            # add local_only, ap_profile_id, ap_public_url. ap_followers_url, ap_domain, subscriptions_count
            # instance_id, and low_quality='memes' in form.url.data  bits as in the community thing
            loop_num = "{:02d}".format(n)
            title = "dev_Community_" + loop_num
            name = "dev_Community_" + loop_num
            description = "dev_Community_" + loop_num
            rules = "dev_Community_" + loop_num + "Rules"
            community = Community(title=title, name=name, description=description,
                                  rules=rules, nsfw=False, private_key=private_key,
                                  public_key=public_key, description_html=markdown_to_html(description), local_only=True,
                                  ap_profile_id='https://' + current_app.config['SERVER_NAME'] + '/c/' + name.lower(),
                                  ap_public_url='https://' + current_app.config['SERVER_NAME'] + '/c/' + name,
                                  ap_followers_url='https://' + current_app.config['SERVER_NAME'] + '/c/' + name.lower() + '/followers',
                                  ap_domain=current_app.config['SERVER_NAME'],
                                  subscriptions_count=1, instance_id=1, low_quality='memes' in name)            

            # add and commit to db
            db.session.add(community)
            db.session.commit()

            # add community membership for current user
            # add to db
            membership = CommunityMember(user_id=current_user.id, community_id=community.id, is_moderator=True,
                                         is_owner=True)
            db.session.add(membership)

            # do the cache clearing bits
            cache.delete_memoized(community_membership, current_user, community)
            cache.delete_memoized(joined_communities, current_user.id)
            cache.delete_memoized(moderating_communities, current_user.id)

        # redirect browser to communities list page
        return redirect(url_for('main.list_communities'))

    # create 10 dev_ topics
    elif topics_form.topics_submit.data and topics_form.validate():
        # get the list of communities in the db
        communities = Community.query.filter_by(banned=False)

        # pick 10 random communities from the communities list
        rand_communities = []
        for _ in range(10):
            rand_communities.append(random.choice(communities.all()))

        # generate new topics
        for n in range(10):
            # generate strings for name, machine_name, and default to 0 communities
            loop_num = "{:02d}".format(n)
            name = "dev_Topic_" + loop_num
            machine_name = "dev-topic-" + loop_num
            num_communities = 0
            # make the topic
            topic = Topic(name=name, machine_name=machine_name, num_communities=num_communities)
            # add the new topic to the db
            db.session.add(topic)
            db.session.commit()
            # refresh the topic menu
            cache.delete_memoized(menu_topics)

        # get the list of topics in the db
        topics = Topic.query.filter_by()

        # pick 10 random topics from the topics list
        rand_topics = []
        for _ in range(10):
            rand_topics.append(random.choice(topics.all()))

        # get those topic_ids
        rand_topic_ids = []
        for t in rand_topics:
            rand_topic_ids.append(t.id)

        # loop 10 times
        # get the community
        # add the topic_id to the community
        # save the db
        # update the num_communities for the topic
        # save the db again
        for i in range(10): 
            community = rand_communities[i]
            community.topic_id = rand_topic_ids[i]
            db.session.commit()
            community.topic.num_communities = community.topic.communities.count()
            db.session.commit()

        # redirect browser to topics list page
        return redirect(url_for('main.list_topics'))

    # delete dev_ communities
    elif delete_communities_form.delete_communities_submit.data and delete_communities_form.validate():
        # get the list of local communities
        communities = Community.query.filter_by(banned=False, local_only=True)

        # sort for ones whose name field starts with "dev_"
        dev_communities = []
        for c in communities.all():
            if c.name.startswith("dev_"):
                dev_communities.append(c)

        # loop through the list and:
        # - ban the community
        # - set its last active state
        # - commit to db
        # - call the unsubscribe and delete from admin.routes
        for c in dev_communities:
            c.banned = True
            c.last_active = utcnow()
            db.session.commit()
            unsubscribe_everyone_then_delete(c.id)

        # redirect browser to communities list page
        flash(_('%(num_communities)d dev communities deleted', len(dev_communities)))
        return redirect(url_for('main.list_communities'))

    # delete dev_ topics
    elif delete_topics_form.delete_topics_submit.data and delete_topics_form.validate():
        # get the list of topics in the db
        topics = Topic.query.filter_by()

        # sort for the ones whose name field starts with "dev_"
        dev_topics = []
        for t in topics.all():
            if t.name.startswith("dev_"):
                dev_topics.append(t)

        # loop through the topics
        # if the topic has communities in it, set it aside and tell the dev
        # else delete it
        topics_with_communities = 0
        deleted_topics = 0

        for t in dev_topics:
            topic = Topic.query.filter_by(id=t.id).first()
            topic.num_communities = topic.communities.count()
            if topic.num_communities == 0:
                db.session.delete(topic)
                db.session.commit()
            else:
                topics_with_communities += 1

        if topics_with_communities > 0:
            flash(_(f'{deleted_topics} Dev Topics Deleted. {topics_with_communities} Dev Topics remain as they still have communities'))
            return redirect(url_for('main.list_topics')) 
        else:
            flash(_(f'{deleted_topics} Dev Topics Deleted.'))
            return redirect(url_for('main.list_topics')) 

    else:
        return render_template('dev/tools.html', 
                               communities_form=communities_form,
                               topics_form=topics_form,
                               delete_communities_form=delete_communities_form,
                               delete_topics_form=delete_topics_form,

                               inoculation=inoculation[random.randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                               )
