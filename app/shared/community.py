from flask import current_app, flash, render_template
from flask_babel import _, force_locale, gettext
from flask_login import current_user
from slugify import slugify
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db, cache
from app.activitypub.signature import RsaKeys
from app.activitypub.util import make_image_sizes
from app.chat.util import send_message
from app.constants import *
from app.email import send_email
from app.models import CommunityBlock, CommunityMember, Notification, NotificationSubscription, User, Conversation, \
    Community, Language, File
from app.shared.tasks import task_selector
from app.shared.upload import process_upload
from app.user.utils import search_for_user
from app.utils import authorise_api_user, blocked_communities, shorten_string, markdown_to_html, \
    instance_banned, community_membership, joined_communities, moderating_communities, is_image_url, \
    communities_banned_from, piefed_markdown_to_lemmy_markdown, community_moderators, add_to_modlog, \
    get_recipient_language, moderating_communities_ids


# function can be shared between WEB and API (only API calls it for now)
# call from admin.federation not tested
def join_community(community_id: int, src, auth=None, user_id=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)

    send_async = not (current_app.debug or src == SRC_WEB)  # False if using a browser

    sync_retval = task_selector('join_community', send_async, user_id=user_id, community_id=community_id, src=src)

    if send_async or sync_retval is True:
        existing_member = CommunityMember.query.filter_by(user_id=user_id,
                                                          community_id=community_id).first()
        if not existing_member:
            member = CommunityMember(user_id=user_id, community_id=community_id)
            db.session.add(member)
            db.session.commit()

    if src == SRC_API:
        return user_id
    elif src == SRC_PLD:
        return sync_retval
    else:
        return


# function can be shared between WEB and API (only API calls it for now)
def leave_community(community_id: int, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id
    cm = CommunityMember.query.filter_by(user_id=user_id, community_id=community_id).one()
    if not cm.is_owner:
        task_selector('leave_community', user_id=user_id, community_id=community_id)

        db.session.query(CommunityMember).filter_by(user_id=user_id, community_id=community_id).delete()
        db.session.commit()

        if src == SRC_WEB:
            flash(_('You have left the community'))
    else:
        # todo: community deletion
        if src == SRC_API:
            raise Exception('need_to_make_someone_else_owner')
        else:
            flash(_('You need to make someone else the owner before unsubscribing.'), 'warning')
            return

    if src == SRC_API:
        return user_id
    else:
        # let calling function handle redirect
        return


def block_community(community_id: int, src, auth=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    existing = CommunityBlock.query.filter_by(user_id=user_id, community_id=community_id).first()
    if not existing:
        db.session.add(CommunityBlock(user_id=user_id, community_id=community_id))
        db.session.commit()
        cache.delete_memoized(blocked_communities, user_id)

    if src == SRC_API:
        return user_id
    else:
        return  # let calling function handle confirmation flash message and redirect


def unblock_community(community_id: int, src, auth=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    existing_block = CommunityBlock.query.filter_by(user_id=user_id, community_id=community_id).first()
    if existing_block:
        db.session.delete(existing_block)
        db.session.commit()
        cache.delete_memoized(blocked_communities, user_id)

    if src == SRC_API:
        return user_id
    else:
        return  # let calling function handle confirmation flash message and redirect


def invite_with_chat(community_id: int, handle: str, src, auth=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
        user = User.query.get(user_id)
    else:
        user = current_user

    recipient = search_for_user(handle)
    if recipient and not recipient.banned and not instance_banned(recipient.instance.domain):
        community = Community.query.get(community_id)
        if community.banned:
            return 0

        conversation = Conversation(user_id=user.id)
        conversation.members.append(recipient)
        conversation.members.append(user)
        db.session.add(conversation)
        db.session.commit()

        message = f"Hi there,\n\nI think you might appreciate this community, check it out: https://{current_app.config['SERVER_NAME']}/c/{community.link()}.\n\n"
        if recipient.is_local():
            message += f"If you'd like to join it use this link: https://{current_app.config['SERVER_NAME']}/c/{community.link()}/subscribe."
        else:
            if recipient.instance.software.lower() == 'piefed':
                message += f"Join the community by going to https://{recipient.instance.domain}/c/{community.link()}@{community.ap_domain}/subscribe or if that doesn't work try pasting {community.lemmy_link()} into this form: https://{recipient.instance.domain}/community/add_remote."
            elif recipient.instance.software.lower() == 'lemmy' or recipient.instance.software.lower() == 'mbin':
                message += f"Join the community by clicking 'Join' at https://{recipient.instance.domain}/c/{community.link()}@{community.ap_domain} or if that doesn't work try pasting {community.lemmy_link()} into your search function."
            else:
                message = render_template('email/invite_to_community.txt', user=user, community=community,
                                          host=current_app.config['SERVER_NAME'])

        reply = send_message(message, conversation.id)

        return 1 if reply else 0
    return 0


def invite_with_email(community_id: int, to: str, src, auth=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
        user = User.query.get(user_id)
    else:
        user = current_user

    community = Community.query.get(community_id)
    if community.banned:
        return 0

    message = render_template('email/invite_to_community.txt', user=user, community=community,
                              host=current_app.config['SERVER_NAME'])

    send_email(f"{community.display_name()} on {current_app.config['SERVER_NAME']}",
               f"{user.display_name()} <{current_app.config['MAIL_FROM']}>",
               [to], message, markdown_to_html(message))
    return 1


def make_community(input, src, auth=None, uploaded_icon_file=None, uploaded_banner_file=None):
    if src == SRC_API:
        input['name'] = slugify(input['name'], separator='_').lower()

        name = input['name']
        title = input['title']
        nsfw = input['nsfw']
        restricted_to_mods = input['restricted_to_mods']
        local_only = input['local_only']
        discussion_languages = input['discussion_languages']
        user = authorise_api_user(auth, return_type='model')
    else:
        if input.url.data.strip().lower().startswith('/c/'):
            input.url.data = input.url.data[3:]
        input.url.data = slugify(input.url.data.strip(), separator='_').lower()

        name = input.url.data
        title = input.community_name.data
        nsfw = input.nsfw.data
        restricted_to_mods = input.restricted_to_mods.data
        local_only = input.local_only.data
        discussion_languages = input.languages.data
        user = current_user

    if user.verified is False or user.private_key is None:
        raise Exception("You can't create a community until your account is verified.")

    # test user with this name doesn't already exist
    ap_profile_id = 'https://' + current_app.config['SERVER_NAME'] + '/u/' + name.lower()
    existing_user = User.query.filter_by(ap_profile_id=ap_profile_id).first()
    if existing_user:
        raise Exception('A User with that name already exists, so it cannot be used for a Community')
    # test community with this name doesn't already exist (it'll be reinforced by the DB, but check first anyway)
    ap_profile_id = 'https://' + current_app.config['SERVER_NAME'] + '/c/' + name.lower()
    existing_community = Community.query.filter_by(ap_profile_id=ap_profile_id).first()
    if existing_community:
        raise Exception('community with that name already exists')

    private_key, public_key = RsaKeys.generate_keypair()
    community = Community(title=title, name=name, nsfw=nsfw, restricted_to_mods=restricted_to_mods,
                          local_only=local_only,
                          private_key=private_key, public_key=public_key,
                          ap_profile_id=ap_profile_id,
                          ap_public_url='https://' + current_app.config['SERVER_NAME'] + '/c/' + name,
                          ap_followers_url='https://' + current_app.config['SERVER_NAME'] + '/c/' + name + '/followers',
                          ap_domain=current_app.config['SERVER_NAME'],
                          subscriptions_count=1, instance_id=1, low_quality='memes' in name)
    try:
        db.session.add(community)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise Exception('Community with that name already exists')

    membership = CommunityMember(user_id=user.id, community_id=community.id, is_moderator=True, is_owner=True)
    db.session.add(membership)
    for language_choice in discussion_languages:
        language = Language.query.get(language_choice)
        if language:
            community.languages.append(language)
    # Always include the undetermined language, so posts with no language will be accepted
    undetermined = Language.query.filter(Language.code == 'und').first()
    if undetermined.id not in discussion_languages:
        community.languages.append(undetermined)
    db.session.commit()

    community = edit_community(input, community, src, auth, uploaded_icon_file, uploaded_banner_file, from_scratch=True)

    if src == SRC_API:
        return user.id, community.id
    else:
        return community.name


def edit_community(input, community, src, auth=None, uploaded_icon_file=None, uploaded_banner_file=None, from_scratch=False):
    if src == SRC_API:
        title = input['title']
        description = input['description']
        rules = input['rules']
        icon_url = input['icon_url']
        banner_url = input['banner_url']
        nsfw = input['nsfw']
        restricted_to_mods = input['restricted_to_mods']
        local_only = input['local_only']
        discussion_languages = input['discussion_languages']
        user = authorise_api_user(auth, return_type='model')
    else:
        title = input.community_name.data
        description = piefed_markdown_to_lemmy_markdown(input.description.data)
        rules = input.rules.data
        icon_url = process_upload(uploaded_icon_file, destination='communities') if uploaded_icon_file else None
        banner_url = process_upload(uploaded_banner_file, destination='communities') if uploaded_banner_file else None
        nsfw = input.nsfw.data
        restricted_to_mods = input.restricted_to_mods.data
        local_only = input.local_only.data
        discussion_languages = input.languages.data
        user = current_user

    icon_url_changed = banner_url_changed = False

    if not from_scratch:
        if not (community.is_owner(user) or community.is_moderator(user) or community.is_instance_admin(user)):
            raise Exception('incorrect_login')

        if community.icon_id and icon_url != community.icon.source_url:
            if icon_url != community.icon.medium_url():
                icon_url_changed = True
                remove_file = File.query.get(community.icon_id)
                if remove_file:
                    remove_file.delete_from_disk()
                community.icon_id = None
        if not community.icon_id:
            icon_url_changed = True
        if community.image_id and banner_url != community.image.source_url:
            if banner_url != community.image.medium_url():
                banner_url_changed = True
                remove_file = File.query.get(community.image_id)
                if remove_file:
                    remove_file.delete_from_disk()
                community.image_id = None
                cache.delete_memoized(Community.header_image, community)
        if not community.image_id:
            cache.delete_memoized(Community.header_image, community)
            banner_url_changed = True
        db.session.execute(text('DELETE FROM "community_language" WHERE community_id = :community_id'),
                           {'community_id': community.id})

    if icon_url and (from_scratch or icon_url_changed) and is_image_url(icon_url):
        file = File(source_url=icon_url)
        db.session.add(file)
        db.session.commit()
        community.icon_id = file.id
        make_image_sizes(community.icon_id, 40, 250, 'communities', community.low_quality)
    if banner_url and (from_scratch or banner_url_changed) and is_image_url(banner_url):
        file = File(source_url=banner_url)
        db.session.add(file)
        db.session.commit()
        community.image_id = file.id
        make_image_sizes(community.image_id, 878, 1600, 'communities', community.low_quality)

    community.title = title
    community.description = description
    community.rules = rules
    community.nsfw = nsfw
    community.description_html = markdown_to_html(description)
    community.restricted_to_mods = restricted_to_mods
    community.local_only = local_only
    db.session.commit()

    if not from_scratch:
        for language_choice in discussion_languages:
            language = Language.query.get(language_choice)
            if language:
                community.languages.append(language)
        # Always include the undetermined language, so posts with no language will be accepted
        undetermined = Language.query.filter(Language.code == 'und').first()
        if undetermined.id not in discussion_languages:
            community.languages.append(undetermined)
        db.session.commit()

        task_selector('edit_community', user_id=user.id, community_id=community.id)

    cache.delete_memoized(community_membership, user, community)
    cache.delete_memoized(joined_communities, user.id)
    cache.delete_memoized(moderating_communities, user.id)

    if from_scratch:
        return community
    else:
        return user.id


def subscribe_community(community_id: int, subscribe, src, auth=None):
    community = Community.query.filter_by(id=community_id, banned=False).one()
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    if src == SRC_WEB:
        subscribe = False if community.notify_new_posts(user_id) else True

    existing_notification = NotificationSubscription.query.filter_by(entity_id=community_id, user_id=user_id,
                                                                     type=NOTIF_COMMUNITY).first()
    if subscribe == False:
        if existing_notification:
            db.session.delete(existing_notification)
            db.session.commit()
        else:
            msg = 'A subscription for this community did not exist.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))

    else:
        if existing_notification:
            msg = 'A subscription for this community already existed.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))
        else:
            if community_id in communities_banned_from(user_id):
                msg = 'You are banned from this community.'
                if src == SRC_API:
                    raise Exception(msg)
                else:
                    flash(_(msg))
            else:
                new_notification = NotificationSubscription(
                    name=shorten_string(_('New posts in %(community_name)s', community_name=community.title)),
                    user_id=user_id, entity_id=community_id, type=NOTIF_COMMUNITY)
                db.session.add(new_notification)
                db.session.commit()

    if src == SRC_API:
        return user_id
    else:
        return render_template('community/_notification_toggle.html', community=community)


def delete_community(community_id: int, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user.id

    community = Community.query.filter_by(id=community_id).one()
    if not (community.is_owner(user) or community.is_moderator(user) or user.is_admin_or_staff()):
        raise Exception('incorrect_login')
    if not community.is_local():
        raise Exception('Only local communities can be deleted')

    """
    Soft-deleting communities needs some attention.
    There isn't a 'deleted' column in the table - I'm just going to use 'banned' for now
    This will fed out the delete, but incoming activity for community deletion isn't understood
    Posts and replies in a deleted community shouldn't be soft-deleted, because without knowing why they were deleted, it's not possible to know if they should be restored if the community is restored
    For 7 days, the API should just return 'error' if the post is part of a deleted community (it doesn't atm)
    After 7 days, the community can be hard-deleted (and it's dependencies with it). This suggests there needs to be a 'deleted' column and a 'deleted_at' column (and maybe a 'deleted_by' column, so a mod can't restore a community deleted by an admin)
    """

    community.banned = True
    db.session.commit()
    task_selector('delete_community', user_id=user.id, community_id=community.id)

    if src == SRC_API:
        return user.id


def restore_community(community_id: int, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    community = Community.query.filter_by(id=community_id).one()
    if not (community.is_owner(user) or community.is_moderator(user) or user.is_admin_or_staff()):
        raise Exception('incorrect_login')
    if not community.is_local():
        raise Exception('Only local communities can be restored')

    """
    same comments as in delete_community
    """

    community.banned = False
    db.session.commit()
    task_selector('restore_community', user_id=user.id, community_id=community.id)

    if src == SRC_API:
        return user.id


def add_mod_to_community(community_id: int, person_id: int, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    community = Community.query.filter_by(id=community_id).one()
    new_moderator = User.query.filter_by(id=person_id, banned=False).one()
    if not community.is_owner(user) and not user.is_admin_or_staff():
        raise Exception('incorrect_login')

    existing_member = CommunityMember.query.filter(CommunityMember.user_id == new_moderator.id,
                                                   CommunityMember.community_id == community_id).first()
    if existing_member:
        existing_member.is_moderator = True
    else:
        new_member = CommunityMember(community_id=community_id, user_id=new_moderator.id, is_moderator=True)
        db.session.add(new_member)
    db.session.commit()
    if src == SRC_WEB:
        flash(_('Moderator added'))

    # Notify new mod
    if new_moderator.is_local():
        targets_data = {'gen': '0', 'community_id': community.id}
        with force_locale(get_recipient_language(new_moderator.id)):
            notify = Notification(title=gettext('You are now a moderator of %(name)s', name=community.display_name()),
                                  url='/c/' + community.name, user_id=new_moderator.id,
                                  author_id=user.id, notif_type=NOTIF_NEW_MOD,
                                  subtype='new_moderator',
                                  targets=targets_data)
            new_moderator.unread_notifications += 1
            db.session.add(notify)
            db.session.commit()
    else:
        # for remote users, send a chat message to let them know
        existing_conversation = Conversation.find_existing_conversation(recipient=new_moderator,
                                                                        sender=user)
        if not existing_conversation:
            existing_conversation = Conversation(user_id=user.id)
            existing_conversation.members.append(new_moderator)
            existing_conversation.members.append(user)
            db.session.add(existing_conversation)
            db.session.commit()
        server = current_app.config['SERVER_NAME']
        send_message(f"Hi there. I've added you as a moderator to the community !{community.name}@{server}.",
                     existing_conversation.id, user=user)

    add_to_modlog('add_mod', actor=user, target_user=new_moderator, community=community, link_text=new_moderator.display_name(),
                    link=new_moderator.link())

    # Flush cache
    cache.delete_memoized(moderating_communities, new_moderator.id)
    cache.delete_memoized(joined_communities, new_moderator.id)
    cache.delete_memoized(community_moderators, community_id)
    cache.delete_memoized(moderating_communities_ids, new_moderator.id)
    cache.delete_memoized(Community.moderators, community)

    task_selector('add_mod', user_id=user.id, mod_id=person_id, community_id=community_id)

    if src == SRC_API:
        return user.id


def remove_mod_from_community(community_id: int, person_id: int, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    community = Community.query.filter_by(id=community_id).one()
    old_moderator = User.query.filter_by(id=person_id).one()
    if not community.is_owner(user) and not user.is_admin_or_staff():
        raise Exception('incorrect_login')

    existing_member = CommunityMember.query.filter(CommunityMember.user_id == old_moderator.id,
                                                   CommunityMember.community_id == community_id).first()
    if existing_member:
        existing_member.is_moderator = False
        existing_member.is_owner = False
        db.session.commit()
    if src == SRC_WEB:
        flash(_('Moderator removed'))

    add_to_modlog('remove_mod', actor=user, target_user=old_moderator, community=community, link_text=old_moderator.display_name(),
                    link=old_moderator.link())

    # Flush cache
    cache.delete_memoized(moderating_communities, old_moderator.id)
    cache.delete_memoized(joined_communities, old_moderator.id)
    cache.delete_memoized(community_moderators, community_id)
    cache.delete_memoized(moderating_communities_ids, old_moderator.id)
    cache.delete_memoized(Community.moderators, community)

    task_selector('remove_mod', user_id=user.id, mod_id=person_id, community_id=community_id)

    if src == SRC_API:
        return user.id
