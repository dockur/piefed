import json
from zoneinfo import ZoneInfo
from app import celery, db
from app.activitypub.signature import default_context, send_post_request
from app.constants import POST_TYPE_LINK, POST_TYPE_ARTICLE, POST_TYPE_IMAGE, POST_TYPE_VIDEO, \
    POST_TYPE_POLL, MICROBLOG_APPS, NOTIF_MENTION, POST_TYPE_EVENT
from app.models import CommunityBan, Instance, Notification, Poll, PollChoice, Post, User, UserFollower, utcnow, Event, \
    Community
from app.user.utils import search_for_user
from app.utils import gibberish, instance_banned, ap_datetime, get_recipient_language, get_task_session, \
    patch_db_session

from flask import current_app
from flask_babel import _, force_locale, gettext

import re


""" Post JSON format
{
  'id':
  'type':
  'attributedTo':
  'to': []
  'cc': []
  'tag': []
  'audience':
  'content':
  'mediaType':
  'source': {}
  'inReplyTo':      (only added for Mentions and user followers, as Note with inReplyTo: None)
  'published':
  'updated':        (inner oject of Update only)
  'language': {}
  'name':           (not included in Polls, which are sent out as microblogs)
  'attachment': []
  'commentsEnabled':
  'sensitive':
  'nsfl':
  'stickied':
  'image':          (for posts with thumbnails only)
  'endTime':        (last 3 are for polls)
  'votersCount':
  'oneOf' / 'anyOf':
}
"""
""" Create / Update / Announce JSON format
{
  'id':
  'type':
  'actor':
  'object':
  'to': []
  'cc': []
  '@context':       (outer object only)
  'audience':       (not in Announce)
}
"""



@celery.task
def make_post(send_async, post_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            send_post(post_id, session=session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def edit_post(send_async, post_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            send_post(post_id, edit=True, session=session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def send_post(post_id, edit=False, session=None):
    post = session.query(Post).get(post_id)
    user = post.author
    community = post.community

    post_body_html = post.body_html if post.body_html else ''

    # Find any users Mentioned in post body with @user@instance syntax
    recipients = []
    if post.body:
        pattern = r"@([a-zA-Z0-9_.-]*)@([a-zA-Z0-9_.-]*)\b"
        matches = re.finditer(pattern, post.body)
        for match in matches:
            recipient = None
            if match.group(2) == current_app.config['SERVER_NAME']:
                user_name = match.group(1)
                if user_name != user.user_name:
                    try:
                        recipient = search_for_user(user_name)
                    except:
                        pass
            else:
                ap_id = f"{match.group(1)}@{match.group(2)}"
                try:
                    recipient = search_for_user(ap_id)
                except:
                    pass
            if recipient:
                add_recipient = True
                for existing_recipient in recipients:
                    if ((not recipient.ap_id and recipient.user_name == existing_recipient.user_name) or
                        (recipient.ap_id and recipient.ap_id == existing_recipient.ap_id)):
                        add_recipient = False
                        break
                if add_recipient:
                    recipients.append(recipient)

    # Notify any local users that have been Mentioned
    for recipient in recipients:
        if recipient.is_local():
            if edit:
                existing_notification = session.query(Notification).filter(Notification.user_id == recipient.id, Notification.url == f"{current_app.config['SERVER_URL']}/post/{post.id}").first()
            else:
                existing_notification = None
            if not existing_notification:
                targets_data = {'gen':'0',
                                'post_id':post.id,
                                'post_body':post.body,
                                'post_title': post.title,
                                'author_user_name': user.ap_id if user.ap_id else user.user_name
                                }
                with force_locale(get_recipient_language(recipient.id)):
                    notification = Notification(user_id=recipient.id, title=gettext(f"You have been mentioned in post {post.id}"),
                                                url=f"{current_app.config['SERVER_URL']}/post/{post.id}",
                                                author_id=user.id, notif_type=NOTIF_MENTION,
                                                subtype='post_mention',
                                                targets=targets_data)
                    recipient.unread_notifications += 1
                    session.add(notification)
                    session.commit()

    if not community.instance.online():
        return

    # local_only communities can also be used to send activity to User Followers
    # return now though, if there aren't any
    followers = session.query(UserFollower).filter_by(local_user_id=post.user_id, is_inward=True).all()
    if not followers and community.local_only:
        return

    banned = session.query(CommunityBan).filter_by(user_id=user.id, community_id=community.id).first()
    if banned:
        return
    if not community.is_local():
        if user.has_blocked_instance(community.instance.id) or instance_banned(community.instance.domain):
            return

    if post.type == POST_TYPE_POLL:
        type = 'Question'
    elif post.type == POST_TYPE_EVENT:
        type = 'Event'
    else:
        type = 'Page'
    to = [community.public_url(), "https://www.w3.org/ns/activitystreams#Public"]
    cc = []
    tag = post.tags_for_activitypub()
    for recipient in recipients:
        tag.append({'href': recipient.public_url(), 'name': recipient.mention_tag(), 'type': 'Mention'})
        cc.append(recipient.public_url())
    language = {'identifier': post.language_code(), 'name': post.language_name()}
    source = {'content': post.body, 'mediaType': 'text/markdown'}
    attachment = []
    if post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO:
        attachment.append({'href': post.url, 'type': 'Link'})
    elif post.type == POST_TYPE_IMAGE:
        attachment.append({'type': 'Image', 'url': post.image.source_url, 'name': post.image.alt_text})

    page = {
      'id': post.public_url(),
      'type': type,
      'attributedTo': user.public_url(),
      'to': to,
      'cc': cc,
      'tag': tag,
      'audience': community.public_url(),
      'content': post_body_html if post.type != POST_TYPE_POLL else '<p>' + post.title + '</p>' + post_body_html,
      'mediaType': 'text/html',
      'source': source,
      'published': ap_datetime(post.posted_at),
      'language': language,
      'name': post.title,
      'attachment': attachment,
      'commentsEnabled': post.comments_enabled,
      'sensitive': post.nsfw or post.nsfl,
      'nsfl': post.nsfl,
      'genAI': post.ai_generated,
      'stickied': post.sticky,
      'interactionPolicy': {
        'canQuote': {
          'automaticApproval': ['https://www.w3.org/ns/activitystreams#Public']
        }
      },
    }
    if post.type != POST_TYPE_POLL:
        page['name'] = post.title
    if edit:
        page['updated'] = ap_datetime(utcnow())
    if post.image_id:
        image_url = ''
        if post.image.source_url:
            image_url = post.image.source_url
        elif post.image.file_path:
            image_url = post.image.file_path.replace('app/static/', f"{current_app.config['SERVER_URL']}/static/")
        elif post.image.thumbnail_path:
            image_url = post.image.thumbnail_path.replace('app/static/', f"{current_app.config['SERVER_URL']}/static/")
        page['image'] = {'type': 'Image', 'url': image_url}
    if post.type == POST_TYPE_POLL:
        poll = Poll.query.filter_by(post_id=post.id).first()
        page['endTime'] = ap_datetime(poll.end_poll)
        page['votersCount'] = poll.total_votes() if edit else 0
        choices = []
        for choice in PollChoice.query.filter_by(post_id=post.id).order_by(PollChoice.sort_order).all():
            choices.append({'type': 'Note', 'name': choice.choice_text, 'replies': {'type': 'Collection', 'totalItems': choice.num_votes if edit else 0}})
        page['oneOf' if poll.mode == 'single' else 'anyOf'] = choices
    elif post.type == POST_TYPE_EVENT:
        event = Event.query.filter_by(post_id=post.id).first()
        page['startTime'] = ap_datetime(event.start)
        page['endTime'] = ap_datetime(event.end)
        page['timezone'] = event.timezone
        page['maximumAttendeeCapacity'] = event.max_attendees
        page['participantCount'] = event.participant_count
        page['onlineLink'] = event.online_link
        page['joinMode'] = event.join_mode
        page['externalParticipationUrl'] = event.external_participation_url
        page['anonymousParticipation'] = event.anonymous_participation
        page['isOnline'] = event.online
        page['buyTicketsLink'] = event.buy_tickets_link
        page['feeCurrency'] = event.event_fee_currency
        page['feeAmount'] = event.event_fee_amount
        page['location'] = event.location

    activity = 'create' if not edit else 'update'
    create_id = f"{current_app.config['SERVER_URL']}/activities/{activity}/{gibberish(15)}"
    type = 'Create' if not edit else 'Update'
    create = {
      'id': create_id,
      'type': type,
      'actor': user.public_url(),
      'object': page,
      'to': to,
      'cc': cc,
      '@context': default_context(),
      'audience': community.public_url()
    }

    domains_sent_to = [current_app.config['SERVER_NAME']]

    # if the community is local, and remote instance is something like Lemmy, Announce the activity
    # if the community is local, and remote instance is something like Mastodon, Announce creates (so the community Boosts it), but send updates directly and from the user
    # Announce of Poll doesn't work for Mastodon, so don't add domain to domains_sent_to, so they receive it if they're also following the User or they get Mentioned
    # if the community is remote, send activity directly
    if not community.local_only:
        if community.is_local():
            del create['@context']

            announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
            cc = [community.ap_followers_url]
            group_announce = {
              'id': announce_id,
              'type': 'Announce',
              'actor': community.public_url(),
              'object': create,
              'to': to,
              'cc': cc,
              '@context': default_context()
            }
            microblog_announce = {
              'id': announce_id,
              'type': 'Announce',
              'actor': community.public_url(),
              'object': post.ap_id,
              'to': to,
              'cc': cc,
              '@context': default_context()
            }
            for instance in set(community.following_instances() + user.following_instances(software='piefed')):
                if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                    if instance.software in MICROBLOG_APPS:
                        if activity == 'create':
                            send_post_request(instance.inbox, microblog_announce, community.private_key, community.public_url() + '#main-key')
                        else:
                            send_post_request(instance.inbox, create, user.private_key, user.public_url() + '#main-key')
                    else:
                        send_post_request(instance.inbox, group_announce, community.private_key, community.public_url() + '#main-key')
                    if post.type != POST_TYPE_POLL:
                          domains_sent_to.append(instance.domain)
        else:
            send_post_request(community.ap_inbox_url, create, user.private_key, user.public_url() + '#main-key')
            domains_sent_to.append(community.instance.domain)

    # amend copy of the Create, for anyone Mentioned in post body or who is following the user, to a format more likely to be understood by Mastodon
    if '@context' not in create:
        create['@context'] = default_context()
    if 'name' in page:
        del page['name']
    note = page
    note['content'] = ''
    if note['type'] == 'Page' or note['type'] == 'Event':
        note['type'] = 'Note'
    if post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO:
        note['content'] += '<p><a href=' + post.url + '>' + post.title + '</a></p>'
    elif post.type != POST_TYPE_POLL:
        note['content'] = '<p>' + post.title + '</p>'
    if post.type == POST_TYPE_EVENT:
        # Convert UTC time to event timezone
        event_tz = ZoneInfo(post.event.timezone)
        local_start = post.event.start.replace(tzinfo=ZoneInfo('UTC')).astimezone(event_tz)
        note['content'] += '<p>' + local_start.strftime('%Y-%m-%dT%H:%M:%S') + f' ({post.event.timezone})</p>'
    if post_body_html:
        note['content'] = note['content'] + post_body_html
    if post.language_id:
        note['contentMap'] = {post.language_code(): note['content']}
    note['inReplyTo'] = None
    create['object'] = note
    if not community.local_only:
        for recipient in recipients:
            if recipient.instance.domain not in domains_sent_to:
                send_post_request(recipient.instance.inbox, create, user.private_key, user.public_url() + '#main-key')
                domains_sent_to.append(recipient.instance.domain)

    if not followers:
        return

    # send the amended copy of the Create to anyone who is following the User, but hasn't already received something
    for follower in followers:
        user_details = session.query(User).get(follower.remote_user_id)
        if user_details:
            create['cc'].append(user_details.public_url())

    for instance in user.following_instances():
        if instance.domain not in domains_sent_to and instance.id != 1 and instance.software != 'piefed':
            send_post_request(instance.inbox, create, user.private_key, user.public_url() + '#main-key')



""" JSON format
Move:
{
  'id':
  'type':
  'actor':
  'object':
  '@context':
  'audience':
  'to': []
  'cc': []
}
For Announce, remove @context from inner object, and use same fields except audience
"""


@celery.task
def move_post(send_async, user_id, old_community_id, new_community_id, post_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                post = session.query(Post).get(post_id)
                if post and not post.deleted:
                    new_community = session.query(Community).get(new_community_id)
                    old_community = session.query(Community).get(old_community_id)
                    move_object(session, user_id, post, origin=old_community, target=new_community)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def move_object(session, user_id, object, origin, target):
    user = session.query(User).get(user_id)

    if isinstance(origin, Community) and isinstance(target, Community):
        community = origin
    else:
        raise Exception('Unsupported origin or target')

    if community.local_only or not community.instance.online():
        return

    move_id = f"{current_app.config['SERVER_URL']}/activities/move/{gibberish(15)}"
    to = ["https://www.w3.org/ns/activitystreams#Public"]
    cc = [community.public_url()]
    move = {
      'id': move_id,
      'type': 'Move',
      'actor': user.public_url(),
      'object': f'{object.public_url()}',
      '@context': default_context(),
      'origin': origin.public_url(),
      'target': target.public_url(),
      'to': to,
      'cc': cc
    }

    if community.is_local():
        del move['@context']

        announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
        actor = community.public_url()
        cc = [community.ap_followers_url]
        announce = {
          'id': announce_id,
          'type': 'Announce',
          'actor': actor,
          'object': move,
          '@context': default_context(),
          'to': to,
          'cc': cc
        }
        for instance in community.following_instances():
            if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                send_post_request(instance.inbox, announce, community.private_key, community.public_url() + '#main-key')
    else:
        send_post_request(community.ap_inbox_url, move, user.private_key, user.public_url() + '#main-key')
