import datetime

from app import celery, db
from app.activitypub.signature import default_context, post_request, send_post_request
from app.models import Community, CommunityMember, User, Instance
from app.utils import gibberish, ap_datetime, get_task_session, patch_db_session

from flask import current_app


""" JSON format
Block:
{
  'id':
  'type':
  'actor':        # person doing the ban
  'object':       # person being banned
  'target':       # community (or site)
  '@context':
  'audience':
  'to': []
  'cc': []
  'endTime':
  'expires':
  'removeData':
  'summary':
}
Announce:
remove @context from inner object
{
  '@context':
  'actor':        # community
  'cc':
  'to':
  'type':
  'object':
}
"""

@celery.task
def ban_from_site(send_async, user_id, mod_id, expiry, reason, remove_data):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                ban_person(session, user_id, mod_id, None, expiry, reason, remove_data)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def unban_from_site(send_async, user_id, mod_id, expiry, reason):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                ban_person(session, user_id, mod_id, None, expiry, reason, False, is_undo=True)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def ban_from_community(send_async, user_id, mod_id, community_id, expiry, reason):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                ban_person(session, user_id, mod_id, community_id, expiry, reason, False)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def unban_from_community(send_async, user_id, mod_id, community_id, expiry, reason):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                ban_person(session, user_id, mod_id, community_id, expiry, reason, False, is_undo=True)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def ban_person(session, user_id, mod_id, community_id, expiry, reason: str, remove_data: bool, is_undo=False):
    if expiry is None:
        expiry = datetime.datetime(year=2100, month=1, day=1)
    user = session.query(User).get(user_id)
    mod = session.query(User).get(mod_id)
    if community_id:    # community ban
        community = session.query(Community).get(community_id)
        communities = [community] if community.is_local() else []
        if community.local_only:
            return
        cc = [community.public_url()]
        target = community.public_url()
    else:               # instance ban
        community = None
        communities = []
        cc = []
        target = f"{current_app.config['SERVER_URL']}/"

    block_id = f"{current_app.config['SERVER_URL']}/activities/block/{gibberish(15)}"
    to = ["https://www.w3.org/ns/activitystreams#Public"]
    block = {
      'id': block_id,
      'type': 'Block',
      'actor': mod.public_url(),
      'object': user.public_url(),
      'target': target,
      '@context': default_context(),
      'to': to,
      'cc': cc,
      'endTime': ap_datetime(expiry),
      'expires': ap_datetime(expiry),
      'removeData': remove_data,
      'summary': reason
    }
    if community_id:
        block['audience'] = community.public_url()
    else:
        block['audience'] = block['target']

    if is_undo:
        del block['@context']
        undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/{gibberish(15)}"
        undo = {
          'id': undo_id,
          'type': 'Undo',
          'actor': mod.public_url(),
          'object': block,
          '@context': default_context(),
          'to': to,
          'cc': cc
        }
        if community_id:
            undo['audience'] = community.public_url()

    if is_undo:
        del undo['@context']
        object=undo
    else:
        del block['@context']
        object=block

    # site ban
    if not community:
        instances = session.query(Instance).filter(Instance.software.in_(['piefed', 'pylova', 'lemmy', 'mbin'])).\
            filter(Instance.gone_forever == False, Instance.dormant == False).all()
        for instance in instances:
            if instance.inbox and instance.online() and instance.id != 1:
                send_post_request(instance.inbox, object, mod.private_key, mod.public_url() + '#main-key')
        return

    # community ban - local mod / remote community
    if community and not community.is_local():
        send_post_request(community.ap_inbox_url, object, mod.private_key, mod.public_url() + '#main-key')
        return

    # community ban - local mod / local communities
    # (just 1 for community ban of a local or remote user, but all joined if site ban of remote user)
    for community in communities:
        announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
        cc = [community.ap_followers_url]
        announce = {
          'id': announce_id,
          'type': 'Announce',
          'actor': community.public_url(),
          'object': object,
          '@context': default_context(),
          'to': to,
          'cc': cc
        }
        sent_to = set()
        for instance in community.following_instances():
            sent_to.add(instance.id)
            if instance.inbox and instance.online():
                send_post_request(instance.inbox, announce, community.private_key, community.public_url() + '#main-key')
        if user.instance_id not in sent_to:     # community.following_instances() excludes instances where banned people are the only follower and they've just been banned so they may be no other followers from that instance.
            send_post_request(user.instance.inbox, announce, community.private_key, community.public_url() + '#main-key')
