import os
import re
from datetime import timedelta
from time import sleep
from io import BytesIO
import json as python_json
import shutil

from flask import request, flash, json, url_for, current_app, redirect, g, abort, send_file
from flask_login import current_user
from flask_babel import _
from slugify import slugify
from sqlalchemy import text, desc, or_
from PIL import Image
from urllib.parse import urlparse
from furl import furl

from app import db, celery, cache
from app.activitypub.routes import process_inbox_request, process_delete_request, replay_inbox_request
from app.activitypub.signature import post_request, default_context, RsaKeys
from app.activitypub.util import instance_allowed, extract_domain_and_actor
from app.admin.constants import ReportTypes
from app.admin.forms import FederationForm, SiteMiscForm, SiteProfileForm, EditCommunityForm, EditUserForm, \
    EditTopicForm, SendNewsletterForm, AddUserForm, PreLoadCommunitiesForm, ImportExportBannedListsForm, \
    EditInstanceForm, RemoteInstanceScanForm, MoveCommunityForm, EditBlockedImageForm, AddBlockedImageForm, CmsPageForm
from flask_wtf import FlaskForm
from app.admin.util import unsubscribe_from_everything_then_delete, unsubscribe_from_community, send_newsletter, \
    topics_for_form, move_community_images_to_here
from app.community.util import save_icon_file, save_banner_file, search_for_community
from app.community.routes import do_subscribe
from app.constants import REPORT_STATE_NEW, REPORT_STATE_ESCALATED, POST_STATUS_REVIEWING
from app.email import send_registration_approved_email
from app.models import AllowedInstances, BannedInstances, ActivityPubLog, utcnow, Site, Community, CommunityMember, \
    User, Instance, File, Report, Topic, UserRegistration, Role, Post, PostReply, Language, RolePermission, Domain, \
    Tag, DefederationSubscription, BlockedImage, CmsPage, Notification
from app.shared.tasks import task_selector
from app.utils import render_template, permission_required, set_setting, get_setting, gibberish, markdown_to_html, \
    moderating_communities, joined_communities, finalize_user_setup, theme_list, blocked_phrases, blocked_referrers, \
    topic_tree, languages_for_form, menu_topics, ensure_directory_exists, add_to_modlog, get_request, file_get_contents, \
    download_defeds, instance_banned, login_required, referrer, \
    community_membership, retrieve_image_hash, posts_with_blocked_images, user_access, reported_posts, user_notes, \
    safe_order_by, get_task_session, patch_db_session, low_value_reposters
from app.admin import bp


@bp.route('/', methods=['GET', 'POST'])
@login_required
def admin_home():
    load1, load5, load15 = os.getloadavg()
    if current_app.config["NUM_CPU"] and current_app.config["NUM_CPU"] != 0:
        num_cores = current_app.config["NUM_CPU"]
    else:
        num_cores = os.cpu_count()
    path = os.getcwd()
    usage = shutil.disk_usage(path)

    total = usage.total
    used = usage.used
    percent_used = used / total * 100

    if percent_used > 95:
        disk_usage = f"<span class='blink red'>Storage used: {percent_used:.2f}%</span>"
    else:
        disk_usage = f"Storage used: {percent_used:.2f}%"
    
    # Get plugin information
    from app.plugins import get_loaded_plugins, get_plugin_hooks
    plugins = get_loaded_plugins()
    plugin_hooks = get_plugin_hooks()
    
    return render_template('admin/home.html', title=_('Admin'), load1=load1, load5=load5, load15=load15,
                           num_cores=num_cores,
                           disk_usage=disk_usage,
                           plugins=plugins,
                           plugin_hooks=plugin_hooks)


@bp.route('/site', methods=['GET', 'POST'])
@permission_required('change instance settings')
@login_required
def admin_site():
    form = SiteProfileForm()
    site = Site.query.get(1)
    if site is None:
        site = Site()
    if form.validate_on_submit():
        site.name = form.name.data
        site.description = form.description.data  # tagline
        site.about = form.about.data
        site.sidebar = form.sidebar.data
        site.legal_information = form.legal_information.data
        site.tos_url = form.tos_url.data
        site.updated = utcnow()
        site.contact_email = form.contact_email.data
        if site.id is None:
            db.session.add(site)
        # Save site icon
        uploaded_icon = request.files['icon']
        if uploaded_icon and uploaded_icon.filename != '':
            allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.svg']
            file_ext = os.path.splitext(uploaded_icon.filename)[1]
            if file_ext.lower() not in allowed_extensions:
                abort(400)
            directory = 'app/static/media'
            ensure_directory_exists(directory)

            # Remove existing logo files
            if os.path.isfile(f'app{site.logo}'):
                os.unlink(f'app{site.logo}')
            if os.path.isfile(f'app{site.logo_180}'):
                os.unlink(f'app{site.logo_180}')
            if os.path.isfile(f'app{site.logo_152}'):
                os.unlink(f'app{site.logo_152}')
            if os.path.isfile(f'app{site.logo_32}'):
                os.unlink(f'app{site.logo_32}')
            if os.path.isfile(f'app{site.logo_16}'):
                os.unlink(f'app{site.logo_16}')

            # Save logo file
            base_filename = f'logo_{gibberish(5)}'
            uploaded_icon.save(f'{directory}/{base_filename}{file_ext}')
            if file_ext == '.svg':
                delete_original = False
                site.logo = site.logo_180 = site.logo_152 = site.logo_32 = site.logo_16 = f'/static/media/{base_filename}{file_ext}'
            else:
                img = Image.open(f'{directory}/{base_filename}{file_ext}')
                if img.width > 100:
                    img.thumbnail((100, 100))
                    img.save(f'{directory}/{base_filename}_100{file_ext}')
                    site.logo = f'/static/media/{base_filename}_100{file_ext}'
                    delete_original = True
                else:
                    site.logo = f'/static/media/{base_filename}{file_ext}'
                    delete_original = False

                # Save multiple copies of the logo - different sizes
                img = Image.open(f'{directory}/{base_filename}{file_ext}')
                img.thumbnail((180, 180))
                img.save(f'{directory}/{base_filename}_180{file_ext}')
                site.logo_180 = f'/static/media/{base_filename}_180{file_ext}'

                img = Image.open(f'{directory}/{base_filename}{file_ext}')
                img.thumbnail((152, 152))
                img.save(f'{directory}/{base_filename}_152{file_ext}')
                site.logo_152 = f'/static/media/{base_filename}_152{file_ext}'

                img = Image.open(f'{directory}/{base_filename}{file_ext}')
                img.thumbnail((32, 32))
                img.save(f'{directory}/{base_filename}_32{file_ext}')
                site.logo_32 = f'/static/media/{base_filename}_32{file_ext}'

                img = Image.open(f'{directory}/{base_filename}{file_ext}')
                img.thumbnail((16, 16))
                img.save(f'{directory}/{base_filename}_16{file_ext}')
                site.logo_16 = f'/static/media/{base_filename}_16{file_ext}'

            if delete_original:
                os.unlink(f'app/static/media/{base_filename}{file_ext}')

        db.session.commit()
        set_setting('announcement', form.announcement.data)
        flash(_('Settings saved.'))
    elif request.method == 'GET':
        form.name.data = site.name
        form.description.data = site.description
        form.about.data = site.about
        form.sidebar.data = site.sidebar
        form.legal_information.data = site.legal_information
        form.tos_url.data = site.tos_url
        form.contact_email.data = site.contact_email
        form.announcement.data = get_setting('announcement', '')
    return render_template('admin/site.html', title=_('Site profile'), form=form)


@bp.route('/misc', methods=['GET', 'POST'])
@permission_required('change instance settings')
@login_required
def admin_misc():
    form = SiteMiscForm()
    site = Site.query.get(1)
    if site is None:
        site = Site()
    form.default_theme.choices = theme_list()
    form.language_id.choices = languages_for_form(all=True)
    if form.validate_on_submit():
        site.enable_downvotes = form.enable_downvotes.data
        site.enable_gif_reply_rep_decrease = form.enable_gif_reply_rep_decrease.data
        site.enable_chan_image_filter = form.enable_chan_image_filter.data
        site.enable_this_comment_filter = form.enable_this_comment_filter.data
        site.allow_local_image_posts = form.allow_local_image_posts.data
        site.remote_image_cache_days = form.remote_image_cache_days.data
        site.enable_nsfw = form.enable_nsfw.data
        site.enable_nsfl = form.enable_nsfl.data
        site.community_creation_admin_only = form.community_creation_admin_only.data
        site.reports_email_admins = form.reports_email_admins.data
        site.registration_mode = form.registration_mode.data
        site.application_question = form.application_question.data
        site.auto_decline_referrers = form.auto_decline_referrers.data
        set_setting('auto_decline_countries', form.auto_decline_countries.data.strip())
        site.log_activitypub_json = form.log_activitypub_json.data
        site.show_inoculation_block = form.show_inoculation_block.data
        site.updated = utcnow()
        site.default_theme = form.default_theme.data
        site.additional_css = form.additional_css.data
        site.additional_js = form.additional_js.data
        site.default_filter = form.default_filter.data
        site.private_instance = form.private_instance.data
        site.language_id = form.language_id.data
        if site.id is None:
            db.session.add(site)
        db.session.commit()
        cache.delete_memoized(blocked_referrers)
        set_setting('meme_comms_low_quality', form.meme_comms_low_quality.data)
        set_setting('public_modlog', form.public_modlog.data)
        set_setting('email_verification', form.email_verification.data)
        set_setting('captcha_enabled', form.captcha_enabled.data)
        set_setting('choose_topics', form.choose_topics.data)
        set_setting('filter_selection', form.filter_selection.data)
        set_setting('registration_approved_email', form.registration_approved_email.data)
        set_setting('ban_check_servers', form.ban_check_servers.data)
        flash(_('Settings saved.'))
    elif request.method == 'GET':
        form.enable_downvotes.data = site.enable_downvotes
        form.enable_gif_reply_rep_decrease.data = site.enable_gif_reply_rep_decrease
        form.enable_chan_image_filter.data = site.enable_chan_image_filter
        form.enable_this_comment_filter.data = site.enable_this_comment_filter
        form.meme_comms_low_quality.data = get_setting('meme_comms_low_quality', False)
        form.allow_local_image_posts.data = site.allow_local_image_posts
        form.remote_image_cache_days.data = site.remote_image_cache_days
        form.enable_nsfw.data = site.enable_nsfw
        form.enable_nsfl.data = site.enable_nsfl
        form.community_creation_admin_only.data = site.community_creation_admin_only
        form.reports_email_admins.data = site.reports_email_admins
        form.registration_mode.data = site.registration_mode
        form.application_question.data = site.application_question
        form.auto_decline_referrers.data = site.auto_decline_referrers
        form.auto_decline_countries.data = get_setting('auto_decline_countries', '')
        form.log_activitypub_json.data = site.log_activitypub_json
        form.language_id.data = site.language_id
        form.show_inoculation_block.data = site.show_inoculation_block
        form.default_theme.data = site.default_theme if site.default_theme is not None else ''
        form.additional_css.data = site.additional_css if site.additional_css is not None else ''
        form.additional_js.data = site.additional_js if site.additional_js is not None else ''
        form.default_filter.data = site.default_filter if site.default_filter else 'popular'
        form.public_modlog.data = get_setting('public_modlog', False)
        form.email_verification.data = get_setting('email_verification', True)
        form.captcha_enabled.data = get_setting('captcha_enabled', False)
        form.choose_topics.data = get_setting('choose_topics', True)
        form.filter_selection.data = get_setting('filter_selection', True)
        form.private_instance.data = site.private_instance
        form.registration_approved_email.data = get_setting('registration_approved_email', '')
        form.ban_check_servers.data = get_setting('ban_check_servers', '')
    return render_template('admin/misc.html', title=_('Misc settings'), form=form)


@bp.route('/federation', methods=['GET', 'POST'])
@permission_required('change instance settings')
@login_required
def admin_federation():
    form = FederationForm()
    preload_form = PreLoadCommunitiesForm()
    ban_lists_form = ImportExportBannedListsForm()
    remote_scan_form = RemoteInstanceScanForm()

    # this is the pre-load communities button
    if preload_form.pre_load_submit.data and preload_form.validate():
        # how many communities to add
        if preload_form.communities_num.data:
            communities_to_add = preload_form.communities_num.data
        else:
            communities_to_add = 25

        # pull down the community.full.json
        resp = get_request('https://data.lemmyverse.net/data/community.full.json')
        community_json = resp.json()
        resp.close()

        already_known = list(db.session.execute(text('SELECT ap_public_url FROM "community"')).scalars())
        banned_urls = list(db.session.execute(text('SELECT domain FROM "banned_instances"')).scalars())
        seven_things_plus = [
            'shit', 'piss', 'fuck',
            'cunt', 'cocksucker', 'motherfucker', 'tits',
            'memes', 'piracy', '196', 'greentext', 'usauthoritarianism',
            'enoughmuskspam', 'political_weirdos', '4chan'
        ]

        total_count = already_known_count = nsfw_count = low_content_count = low_active_users_count = banned_count = bad_words_count = 0
        candidate_communities = []

        for community in community_json:
            total_count += 1

            # sort out already known communities
            if community['url'] in already_known:
                already_known_count += 1
                continue

            # sort out the nsfw communities
            elif community['nsfw']:
                nsfw_count += 1
                continue

            # sort out any that have less than 100 posts
            elif community['counts']['posts'] < 100:
                low_content_count += 1
                continue

            # sort out any that do not have greater than 500 active users over the past week
            elif community['counts']['users_active_week'] < 500:
                low_active_users_count += 1
                continue

            # sort out any instances we have already banned
            elif community['baseurl'] in banned_urls:
                banned_count += 1
                continue

            # sort out the 'seven things you can't say on tv' names (cursewords), plus some
            # "low effort" communities
            if any(badword in community['name'].lower() for badword in seven_things_plus):
                bad_words_count += 1
                continue

            else:
                candidate_communities.append(community)

        filtered_count = already_known_count + nsfw_count + low_content_count + low_active_users_count + banned_count + bad_words_count
        flash(_('%d out of %d communities were excluded using current filters' % (filtered_count, total_count)))

        # sort the list based on the users_active_week key
        parsed_communities_sorted = sorted(candidate_communities, key=lambda c: c['counts']['users_active_week'],
                                           reverse=True)

        # get the community urls to join
        community_urls_to_join = []

        # if the admin user wants more added than we have, then just add all of them
        if communities_to_add > len(parsed_communities_sorted):
            communities_to_add = len(parsed_communities_sorted)

        # make the list of urls
        for i in range(communities_to_add):
            community_urls_to_join.append(parsed_communities_sorted[i]['url'].lower())

        # loop through the list and send off the follow requests
        # use User #1, the first instance admin

        # NOTE: Subscribing using the admin's alt_profile causes problems:
        # 1. 'Leave' will use the main user name so unsubscribe won't succeed.
        # 2. De-selecting and re-selecting 'vote privately' generates a new alt_user_name every time,
        #    so the username needed for a successful unsubscribe might get lost
        # 3. If the admin doesn't have 'vote privately' selected, the federation JSON will populate
        #    with a blank space for the name, so the subscription won't succeed.
        # 4. Membership is based on user id, so using the alt_profile doesn't decrease the admin's joined communities
        #
        # Therefore, 'main_user_name=False' has been changed to 'admin_preload=True' below

        user = User.query.get(1)
        pre_load_messages = []
        for community in community_urls_to_join:
            # get the relevant url bits
            server, community = extract_domain_and_actor(community)

            # find the community
            new_community = search_for_community('!' + community + '@' + server)
            if not new_community:
                continue
            # subscribe to the community
            # capture the messages returned by do_subscibe
            # and show to user if instance is in debug mode
            if current_app.debug:
                message = do_subscribe(new_community.ap_id, user.id, admin_preload=True)
                pre_load_messages.append(message)
            else:
                do_subscribe.delay(new_community.ap_id, user.id, admin_preload=True)

        if current_app.debug:
            flash(_('Results: %(results)s', results=str(pre_load_messages)))
        else:
            flash(
                _('Subscription process for %(communities_to_add)d of %(parsed_communities_sorted)d communities launched in background, check admin/activities for details',
                  communities_to_add=communities_to_add, parsed_communities_sorted=len(parsed_communities_sorted)))

        return redirect(url_for('admin.admin_federation'))

    # this is the remote server scan
    elif remote_scan_form.remote_scan_submit.data and remote_scan_form.validate():
        # filters to be used later
        already_known = list(db.session.execute(text('SELECT ap_public_url FROM "community"')).scalars())
        banned_urls = list(db.session.execute(text('SELECT domain FROM "banned_instances"')).scalars())
        seven_things_plus = [
            'shit', 'piss', 'fuck',
            'cunt', 'cocksucker', 'motherfucker', 'tits',
            'memes', 'piracy', '196', 'greentext', 'usauthoritarianism',
            'enoughmuskspam', 'political_weirdos', '4chan'
        ]
        is_lemmy = False
        is_mbin = False
        is_piefed = False

        # get the remote_url data
        remote_url = remote_scan_form.remote_url.data

        # test to make sure its a valid fqdn
        regex_pattern = '^(https:\\/\\/)(?=.{1,255}$)((.{1,63}\\.){1,127}(?![0-9]*$)[a-z0-9-]+\\.?)$'
        result = re.match(regex_pattern, remote_url)
        if result is None:
            flash(_(f'{remote_url} does not appear to be a valid url. Make sure input is in the form "https://server-name.tld" without trailing slashes or paths.'))
            return redirect(url_for('admin.admin_federation'))

        # check if it's a banned instance
        # Parse the URL
        parsed_url = urlparse(remote_url)
        # Extract the server domain name
        server_domain = parsed_url.netloc
        if server_domain in banned_urls:
            flash(_(f'{remote_url} is a banned instance.'))
            return redirect(url_for('admin.admin_federation'))

        # get dry run
        dry_run = remote_scan_form.dry_run.data

        # get the number of follows requested
        communities_requested = remote_scan_form.communities_requested.data

        # get the minimums
        min_posts = remote_scan_form.minimum_posts.data
        min_users = remote_scan_form.minimum_active_users.data

        # get the nodeinfo
        resp = get_request(f'{remote_url}/.well-known/nodeinfo')
        nodeinfo_dict = json.loads(resp.text)

        # check the ['links'] for instanceinfo url
        schema2p0 = "http://nodeinfo.diaspora.software/ns/schema/2.0"
        schema2p1 = "http://nodeinfo.diaspora.software/ns/schema/2.1"
        for e in nodeinfo_dict['links']:
            if e['rel'] == schema2p0 or e['rel'] == schema2p1:
                remote_instanceinfo_url = e["href"]

        # get the instanceinfo
        resp = get_request(remote_instanceinfo_url)
        instanceinfo_dict = json.loads(resp.text)

        # determine the instance software
        instance_software_name = instanceinfo_dict['software']['name']
        # instance_software_version = instanceinfo_dict['software']['version']

        # if the instance is not running lemmy or mbin break for now as 
        # we dont yet support others for scanning
        if instance_software_name == "lemmy":
            is_lemmy = True
        elif instance_software_name == "mbin":
            is_mbin = True
        elif instance_software_name == "piefed":
            is_piefed = True
        else:
            flash(_(f"{remote_url} does not appear to be a lemmy, mbin, or piefed instance."))
            return redirect(url_for('admin.admin_federation'))

        if is_lemmy:
            # lemmy has a hard-coded upper limit of 50 commnities
            # in their api response
            # loop through and send off requests to the remote endpoint for 50 communities at a time
            comms_list = []
            page = 1
            get_more_communities = True
            while get_more_communities:
                params = {"sort": "Active", "type_": "Local", "limit": "50", "page": f"{page}", "show_nsfw": "false"}
                resp = get_request(f"{remote_url}/api/v3/community/list", params=params)
                page_dict = json.loads(resp.text)
                # get the individual communities out of the communities[] list in the response and 
                # add them to a holding list[] of our own
                for c in page_dict["communities"]:
                    comms_list.append(c)
                # check the amount of items in the page_dict['communities'] list
                # if it's lesss than 50 then we know its the last page of communities
                # so we break the loop
                if len(page_dict['communities']) < 50:
                    get_more_communities = False
                else:
                    page += 1

            # filter out the communities
            already_known_count = nsfw_count = low_content_count = low_active_users_count = bad_words_count = 0
            candidate_communities = []
            for community in comms_list:
                # sort out already known communities
                if community['community']['actor_id'] in already_known:
                    already_known_count += 1
                    continue
                # sort out any that have less than minimum posts
                elif community['counts']['posts'] < min_posts:
                    low_content_count += 1
                    continue
                # sort out any that do not have greater than the requested active users over the past week
                elif community['counts']['users_active_week'] < min_users:
                    low_active_users_count += 1
                    continue
                # sort out the 'seven things you can't say on tv' names (cursewords), plus some
                # "low effort" communities
                if any(badword in community['community']['name'].lower() for badword in seven_things_plus):
                    bad_words_count += 1
                    continue
                else:
                    candidate_communities.append(community)

            # get the community urls to join
            community_urls_to_join = []

            # if the admin user wants more added than we have, then just add all of them
            if communities_requested > len(candidate_communities):
                communities_to_add = len(candidate_communities)
            else:
                communities_to_add = communities_requested

            # make the list of urls
            for i in range(communities_to_add):
                community_urls_to_join.append(candidate_communities[i]['community']['actor_id'].lower())

            # if its a dry run, just return the stats
            if dry_run:
                message = f"Dry-Run for {remote_url}: \
                            Local Communities on the server: {len(comms_list)}, \
                            Communities we already have: {already_known_count}, \
                            Communities below minimum posts: {low_content_count}, \
                            Communities below minimum users: {low_active_users_count}, \
                            Candidate Communities based on filters: {len(candidate_communities)}, \
                            Communities to join request: {communities_requested}, \
                            Communities to join based on current filters: {len(community_urls_to_join)}."
                flash(_(message))
                return redirect(url_for('admin.admin_federation'))

        if is_piefed:
            # loop through and send off requests to the remote endpoint for 50 communities at a time
            comms_list = []
            page = 1
            get_more_communities = True
            while get_more_communities:
                params = {"sort": "Active", "type_": "Local", "limit": "50", "page": f"{page}", "show_nsfw": "false"}
                resp = get_request(f"{remote_url}/api/alpha/community/list", params=params)
                page_dict = json.loads(resp.text)
                # get the individual communities out of the communities[] list in the response and 
                # add them to a holding list[] of our own
                for c in page_dict["communities"]:
                    comms_list.append(c)
                # check the amount of items in the page_dict['communities'] list
                # if it's less than 50 then we know its the last page of communities
                # so we break the loop
                if len(page_dict['communities']) < 50:
                    get_more_communities = False
                else:
                    page += 1

            # filter out the communities
            already_known_count = nsfw_count = low_content_count = low_active_users_count = bad_words_count = 0
            candidate_communities = []
            for community in comms_list:
                # sort out already known communities
                if community['community']['actor_id'] in already_known:
                    already_known_count += 1
                    continue
                # sort out any that have less than minimum posts
                elif community['counts']['post_count'] < min_posts:
                    low_content_count += 1
                    continue

                # sort out any that do not have greater than the requested active users over the past week
                elif community['counts']['active_weekly'] < min_users:
                    low_active_users_count += 1
                    continue

                # sort out the 'seven things you can't say on tv' names (cursewords), plus some
                # "low effort" communities
                if any(badword in community['community']['name'].lower() for badword in seven_things_plus):
                    bad_words_count += 1
                    continue
                else:
                    candidate_communities.append(community)

            # get the community urls to join
            community_urls_to_join = []

            # if the admin user wants more added than we have, then just add all of them
            if communities_requested > len(candidate_communities):
                communities_to_add = len(candidate_communities)
            else:
                communities_to_add = communities_requested

            # make the list of urls
            for i in range(communities_to_add):
                community_urls_to_join.append(candidate_communities[i]['community']['actor_id'].lower())

            # if its a dry run, just return the stats
            if dry_run:
                message = f"Dry-Run for {remote_url}: \
                            Local Communities on the server: {len(comms_list)}, \
                            Communities we already have: {already_known_count}, \
                            Communities below minimum posts: {low_content_count}, \
                            Communities below minimum users: {low_active_users_count}, \
                            Candidate Communities based on filters: {len(candidate_communities)}, \
                            Communities to join request: {communities_requested}, \
                            Communities to join based on current filters: {len(community_urls_to_join)}."
                flash(_(message))
                return redirect(url_for('admin.admin_federation'))

        if is_mbin:
            # loop through and send the right number of requests to the remote endpoint for mbin
            # mbin does not have the hard-coded limit, but lets stick with 50 to match lemmy 
            mags_list = []
            page = 1
            get_more_magazines = True
            while get_more_magazines:
                params = {"p": f"{page}", "perPage": "50", "sort": "active", "federation": "local",
                          "hide_adult": "hide"}
                resp = get_request(f"{remote_url}/api/magazines", params=params)
                page_dict = json.loads(resp.text)
                # get the individual magazines out of the items[] list in the response and 
                # add them to a holding list[] of our own
                for m in page_dict['items']:
                    mags_list.append(m)
                # check the amount of items in the page_dict['items'] list
                # if it's lesss than 50 then we know its the last page of magazines
                # so we break the loop
                if len(page_dict['items']) < 50:
                    get_more_magazines = False
                else:
                    page += 1

            # filter out the magazines
            already_known_count = low_content_count = low_subscribed_users_count = bad_words_count = 0
            candidate_communities = []
            for magazine in mags_list:
                # sort out already known communities
                if magazine['apProfileId'] in already_known:
                    already_known_count += 1
                    continue
                # sort out any that have less than minimum posts
                elif magazine['entryCount'] < min_posts:
                    low_content_count += 1
                    continue
                # sort out any that do not have greater than the requested users over the past week
                # mbin does not show active users here, so its based on subscriber count
                elif magazine['subscriptionsCount'] < min_users:
                    low_subscribed_users_count += 1
                    continue
                # sort out the 'seven things you can't say on tv' names (cursewords), plus some
                # "low effort" communities
                if any(badword in magazine['name'].lower() for badword in seven_things_plus):
                    bad_words_count += 1
                    continue
                else:
                    candidate_communities.append(magazine)

            # get the community urls to join
            community_urls_to_join = []

            # if the admin user wants more added than we have, then just add all of them
            if communities_requested > len(candidate_communities):
                magazines_to_add = len(candidate_communities)
            else:
                magazines_to_add = communities_requested

            # make the list of urls
            for i in range(magazines_to_add):
                community_urls_to_join.append(candidate_communities[i]['apProfileId'].lower())

            # if its a dry run, just return the stats
            if dry_run:
                message = f"Dry-Run for {remote_url}: \
                            Local Magazines on the server: {len(mags_list)}, \
                            Magazines we already have: {already_known_count}, \
                            Magazines below minimum posts: {low_content_count}, \
                            Magazines below minimum users: {low_subscribed_users_count}, \
                            Candidate Magazines based on filters: {len(candidate_communities)}, \
                            Magazines to join request: {communities_requested}, \
                            Magazines to join based on current filters: {len(community_urls_to_join)}."
                flash(_(message))
                return redirect(url_for('admin.admin_federation'))

        user = User.query.get(1)
        remote_scan_messages = []
        for community in community_urls_to_join:
            # get the relevant url bits
            server, community = extract_domain_and_actor(community)
            # find the community
            new_community = search_for_community('!' + community + '@' + server)
            if not new_community:
                continue
            # subscribe to the community
            # capture the messages returned by do_subscribe
            # and show to user if instance is in debug mode
            if current_app.debug:
                message = do_subscribe(new_community.ap_id, user.id, admin_preload=True)
                remote_scan_messages.append(message)
            else:
                do_subscribe.delay(new_community.ap_id, user.id, admin_preload=True)

        if current_app.debug:
            flash(_('Results: %(results)s', results=str(remote_scan_messages)))
        else:
            flash(
                _('Based on current filters, the subscription process for %(communities_to_join)d of %(candidate_communities)d communities launched in background, check admin/activities for details',
                  communities_to_join=len(community_urls_to_join), candidate_communities=len(candidate_communities)))

        return redirect(url_for('admin.admin_federation'))

    # this is the import bans button
    elif ban_lists_form.import_submit.data and ban_lists_form.validate():
        import_file = request.files['import_file']
        if import_file and import_file.filename != '':
            file_ext = os.path.splitext(import_file.filename)[1]
            if file_ext.lower() != '.json':
                abort(400)
            new_filename = gibberish(15) + '.json'

            directory = 'app/static/media/'

            # save the file
            final_place = os.path.join(directory, new_filename + file_ext)
            import_file.save(final_place)

            # import bans in background task
            if current_app.debug:
                import_bans_task(final_place)
                return redirect(url_for('admin.admin_federation'))
            else:
                import_bans_task.delay(final_place)
                flash(_('Ban imports started in a background process.'))
                return redirect(url_for('admin.admin_federation'))
        else:
            flash(_('Ban imports requested, but no json provided.'))
            return redirect(url_for('admin.admin_federation'))

    # this is the export bans button
    elif ban_lists_form.export_submit.data and ban_lists_form.validate():

        ban_lists_dict = {}

        if get_setting('use_allowlist'):
            # get the allowed_instances info
            allowed_instances = []
            already_allowed = AllowedInstances.query.all()
            if len(already_allowed) > 0:
                for aa in already_allowed:
                    allowed_instances.append(aa.domain)
            ban_lists_dict['allowed_instances'] = allowed_instances
        else:
            # get banned_instances info
            banned_instances = []
            instance_bans = BannedInstances.query.all()
            if len(instance_bans) > 0:
                for bi in instance_bans:
                    banned_instances.append(bi.domain)
            ban_lists_dict['banned_instances'] = banned_instances

        # get banned_domains info
        banned_domains = []
        domain_bans = Domain.query.filter_by(banned=True).all()
        if len(domain_bans) > 0:
            for domain in domain_bans:
                banned_domains.append(domain.name)
        ban_lists_dict['banned_domains'] = banned_domains

        # get banned_tags info
        banned_tags = []
        tag_bans = Tag.query.filter_by(banned=True).all()
        if len(tag_bans) > 0:
            for tag_ban in tag_bans:
                tag_dict = {}
                tag_dict['name'] = tag_ban.name
                tag_dict['display_as'] = tag_ban.display_as
                banned_tags.append(tag_dict)
        ban_lists_dict['banned_tags'] = banned_tags

        # get banned_users info
        banned_users = []
        user_bans = User.query.filter_by(banned=True).all()
        if len(user_bans) > 0:
            for user_ban in user_bans:
                banned_users.append(user_ban.ap_id)
        ban_lists_dict['banned_users'] = banned_users

        # setup the BytesIO buffer
        buffer = BytesIO()
        buffer.write(str(python_json.dumps(ban_lists_dict)).encode('utf-8'))
        buffer.seek(0)

        # send the file to the user as a download
        # the as_attachment=True results in flask
        # redirecting to the current page, so no
        # url_for needed here
        return send_file(buffer, download_name=f'{current_app.config["SERVER_NAME"]}_bans.json', as_attachment=True,
                         mimetype='application/json')

    # this is the main settings form
    elif form.validate_on_submit():
        if form.federation_mode.data == 'allowlist':
            set_setting('use_allowlist', True)
            db.session.execute(text('DELETE FROM allowed_instances'))
            for allow in form.allowlist.data.split('\n'):
                if allow.strip():
                    db.session.add(AllowedInstances(domain=allow.strip()))
                    cache.delete_memoized(instance_allowed, allow.strip())
        else:  # blocklist mode
            set_setting('use_allowlist', False)
            db.session.execute(text('DELETE FROM banned_instances WHERE subscription_id is null'))
            for banned in form.blocklist.data.split('\n'):
                if banned.strip():
                    db.session.add(BannedInstances(domain=banned.strip()))
                    cache.delete_memoized(instance_banned, banned.strip())

        # update and sync defederation subscriptions
        db.session.execute(text('DELETE FROM banned_instances WHERE subscription_id is not null'))
        db.session.query(DefederationSubscription).delete()
        db.session.commit()
        for defed_subscription in form.defederation_subscription.data.split('\n'):
            if defed_subscription.strip():
                db.session.add(DefederationSubscription(domain=defed_subscription.strip().lower()))
        db.session.commit()
        for defederation_sub in DefederationSubscription.query.all():
            download_defeds(defederation_sub.id, defederation_sub.domain)

        g.site.blocked_phrases = form.blocked_phrases.data
        set_setting('actor_blocked_words', form.blocked_actors.data)
        set_setting('actor_bio_blocked_words', form.blocked_bio.data)
        cache.delete_memoized(blocked_phrases)
        cache.delete_memoized(get_setting, 'actor_blocked_words')
        db.session.commit()

        flash(_('Admin settings saved'))

    # this is just the regular page load
    elif request.method == 'GET':
        use_allowlist = get_setting('use_allowlist', False)
        form.federation_mode.data = 'allowlist' if use_allowlist else 'blocklist'
        instances = BannedInstances.query.filter(BannedInstances.subscription_id == None).all()
        form.blocklist.data = '\n'.join([instance.domain for instance in instances])
        instances = AllowedInstances.query.all()
        form.allowlist.data = '\n'.join([instance.domain for instance in instances])
        form.defederation_subscription.data = '\n'.join([instance.domain for instance in DefederationSubscription.query.all()])
        form.blocked_phrases.data = g.site.blocked_phrases
        form.blocked_actors.data = get_setting('actor_blocked_words', '88')
        form.blocked_bio.data = get_setting('actor_bio_blocked_words', '')

    return render_template('admin/federation.html', title=_('Federation settings'),
                           form=form, preload_form=preload_form, ban_lists_form=ban_lists_form,
                           remote_scan_form=remote_scan_form, current_app_debug=current_app.debug)


@celery.task
def import_bans_task(filename):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                contents = file_get_contents(filename)
                contents_json = json.loads(contents)

                # import allowed_instances
                if get_setting('use_allowlist'):
                    # check for allowed_instances existing and being more than 0 entries
                    instances_allowed = contents_json['allowed_instances']
                    if isinstance(instance_allowed, list) and len(instance_allowed) > 0:
                        # get the existing allows and their domains
                        already_allowed_instances = []
                        already_allowed = AllowedInstances.query.all()
                        if len(already_allowed) > 0:
                            for already_allowed in already_allowed:
                                already_allowed_instances.append(already_allowed.domain)

                        # loop through the instances_allowed
                        for allowed_instance in instances_allowed:
                            # check if we have already allowed this instance
                            if allowed_instance in already_allowed_instances:
                                continue
                            else:
                                # allow the instance
                                db.session.add(AllowedInstances(domain=allowed_instance))
                    # commit to the db
                    db.session.commit()

                # import banned_instances
                else:
                    # check for banned_instances existing and being more than 0 entries
                    instance_bans = contents_json['banned_instances']
                    if isinstance(instance_bans, list) and len(instance_bans) > 0:
                        # get the existing bans and their domains
                        already_banned_instances = []
                        already_banned = BannedInstances.query.all()
                        if len(already_banned) > 0:
                            for ab in already_banned:
                                already_banned_instances.append(ab.domain)

                        # loop through the instance_bans
                        for instance_ban in instance_bans:
                            # check if we have already banned this instance
                            if instance_ban in already_banned_instances:
                                continue
                            else:
                                # ban the domain
                                db.session.add(BannedInstances(domain=instance_ban))
                    # commit to the db
                    db.session.commit()

                # import banned_domains
                # check for banned_domains existing and being more than 0 entries
                domain_bans = contents_json['banned_domains']
                if isinstance(domain_bans, list) and len(domain_bans) > 0:
                    # get the existing bans and their domains
                    already_banned_domains = []
                    already_banned = Domain.query.filter_by(banned=True).all()
                    if len(already_banned) > 0:
                        for ab in already_banned:
                            already_banned_domains.append(ab.name)

                    # loop through the domain_bans
                    for domain_ban in domain_bans:
                        # check if we have already banned this domain
                        if domain_ban in already_banned_domains:
                            continue
                        else:
                            # ban the domain
                            db.session.add(Domain(name=domain_ban, banned=True))
                    # commit to the db
                    db.session.commit()

                # import banned_tags
                # check for banned_tags existing and being more than 0 entries
                tag_bans = contents_json['banned_tags']
                if isinstance(tag_bans, list) and len(tag_bans) > 0:
                    # get the existing bans and their domains
                    already_banned_tags = []
                    already_banned = Tag.query.filter_by(banned=True).all()
                    if len(already_banned) > 0:
                        for ab in already_banned:
                            already_banned_tags.append(ab.name)

                    # loop through the tag_bans
                    for tag_ban in tag_bans:
                        # check if we have already banned this tag
                        if tag_ban['name'] in already_banned_tags:
                            continue
                        else:
                            # ban the domain
                            db.session.add(Tag(name=tag_ban['name'], display_as=tag_ban['display_as'], banned=True))
                    # commit to the db
                    db.session.commit()

                # import banned_users
                # check for banned_users existing and being more than 0 entries
                user_bans = contents_json['banned_users']
                if isinstance(user_bans, list) and len(user_bans) > 0:
                    # get the existing bans and their domains
                    already_banned_users = []
                    already_banned = User.query.filter_by(banned=True).all()
                    if len(already_banned) > 0:
                        for ab in already_banned:
                            already_banned_users.append(ab.ap_id)

                    # loop through the user_bans
                    for user_ban in user_bans:
                        # check if we have already banned this user
                        if user_ban in already_banned_users:
                            continue
                        else:
                            # ban the user
                            db.session.add(User(user_name=user_ban.split('@')[0], ap_id=user_ban, banned=True))
                    # commit to the db
                    db.session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@bp.route('/activities', methods=['GET'])
@permission_required('change instance settings')
@login_required
def admin_activities():
    if current_app.config['LOG_ACTIVITYPUB_TO_DB'] is False:
        flash(_('LOG_ACTIVITYPUB_TO_DB is off so no incoming activities are being logged to the database.'), 'warning')

    db.session.query(ActivityPubLog).filter(
        ActivityPubLog.created_at < utcnow() - timedelta(days=3)).delete()
    db.session.commit()

    page = request.args.get('page', 1, type=int)
    result_filter = request.args.get('result', type=str)
    direction_filter = request.args.get('direction', type=str)

    activities = ActivityPubLog.query.order_by(desc(ActivityPubLog.created_at))
    if result_filter:
        activities = activities.filter(ActivityPubLog.result == result_filter)
    if direction_filter:
        activities = activities.filter(ActivityPubLog.direction == direction_filter)

    activities = activities.paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_activities', page=activities.next_num, result=result_filter,
                       direction=direction_filter) if activities.has_next else None
    prev_url = url_for('admin.admin_activities', page=activities.prev_num, result=result_filter,
                       direction=direction_filter) if activities.has_prev and page != 1 else None

    return render_template('admin/activities.html', title=_('ActivityPub Log'), next_url=next_url, prev_url=prev_url,
                           activities=activities)


@bp.route('/activity_json/<int:activity_id>')
@permission_required('change instance settings')
@login_required
def activity_json(activity_id):
    activity = ActivityPubLog.query.get_or_404(activity_id)
    return render_template('admin/activity_json.html', title=_('Activity JSON'),
                           activity_json_data=activity.activity_json, activity=activity,
                           current_app=current_app)


@bp.route('/activity_json/<int:activity_id>/replay')
@permission_required('change instance settings')
@login_required
def activity_replay(activity_id):
    activity = ActivityPubLog.query.get_or_404(activity_id)
    request_json = json.loads(activity.activity_json)
    replay_inbox_request(request_json)

    return 'Ok'


@bp.route('/communities', methods=['GET'])
@permission_required('administer all communities')
@login_required
def admin_communities():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    sort_by = request.args.get('sort_by', 'title ASC')

    communities = Community.query
    if search:
        communities = communities.filter(or_(Community.title.ilike(f"%{search}%"), Community.ap_id.ilike(f"%{search}%")))
    communities = communities.order_by(safe_order_by(sort_by, Community, {'title', 'topic_id', 'subscriptions_count',
                                                                          'show_popular', 'show_all', 'post_count',
                                                                          'content_retention', 'nsfw', 'post_reply_count', 'last_active'}))
    communities = communities.paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_communities', page=communities.next_num, search=search,
                       sort_by=sort_by) if communities.has_next else None
    prev_url = url_for('admin.admin_communities', page=communities.prev_num, search=search,
                       sort_by=sort_by) if communities.has_prev and page != 1 else None

    return render_template('admin/communities.html', title=_('Communities'), next_url=next_url, prev_url=prev_url,
                           communities=communities,
                           search=search, sort_by=sort_by,
                           )


@bp.route('/communities/no-topic', methods=['GET'])
@permission_required('administer all communities')
@login_required
def admin_communities_no_topic():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')

    communities = Community.query.filter(Community.topic_id == None)
    if search:
        communities = communities.filter(Community.title.ilike(f"%{search}%"))
    communities = communities.order_by(-Community.post_count).paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_communities_no_topic', page=communities.next_num) if communities.has_next else None
    prev_url = url_for('admin.admin_communities_no_topic', page=communities.prev_num) if communities.has_prev and page != 1 else None

    return render_template('admin/communities.html', title=_('Communities with no topic'), next_url=next_url,
                           prev_url=prev_url,
                           communities=communities)


@bp.route('/communities/low-quality', methods=['GET'])
@permission_required('administer all communities')
@login_required
def admin_communities_low_quality():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')

    communities = Community.query.filter(Community.low_quality == True)
    if search:
        communities = communities.filter(Community.title.ilike(f"%{search}%"))
    communities = communities.order_by(-Community.post_count).paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_communities_low_quality',
                       page=communities.next_num) if communities.has_next else None
    prev_url = url_for('admin.admin_communities_low_quality',
                       page=communities.prev_num) if communities.has_prev and page != 1 else None

    return render_template('admin/communities.html', title=_('Communities with low_quality == True'), next_url=next_url,
                           prev_url=prev_url,
                           communities=communities)


@bp.route('/community/<int:community_id>/edit', methods=['GET', 'POST'])
@permission_required('administer all communities')
@login_required
def admin_community_edit(community_id):
    form = EditCommunityForm()
    community = Community.query.get_or_404(community_id)
    old_topic_id = community.topic_id if community.topic_id else None
    form.topic.choices = topics_for_form(0)
    form.languages.choices = languages_for_form(all=True)
    if form.validate_on_submit():
        community.name = form.url.data
        community.title = form.title.data
        community.description = form.description.data
        community.description_html = markdown_to_html(form.description.data)
        community.rules = form.rules.data
        community.nsfw = form.nsfw.data
        community.banned = form.banned.data
        community.local_only = form.local_only.data
        community.restricted_to_mods = form.restricted_to_mods.data
        community.new_mods_wanted = form.new_mods_wanted.data
        community.show_popular = form.show_popular.data
        community.show_all = form.show_all.data
        community.low_quality = form.low_quality.data
        community.content_retention = form.content_retention.data
        community.topic_id = form.topic.data if form.topic.data != 0 else None
        community.default_layout = form.default_layout.data
        community.posting_warning = form.posting_warning.data
        community.ignore_remote_language = form.ignore_remote_language.data

        icon_file = request.files['icon_file']
        if icon_file and icon_file.filename != '':
            if community.icon_id:
                community.icon.delete_from_disk()
            file = save_icon_file(icon_file)
            if file:
                community.icon = file
        banner_file = request.files['banner_file']
        if banner_file and banner_file.filename != '':
            if community.image_id:
                community.image.delete_from_disk()
            file = save_banner_file(banner_file)
            if file:
                community.image = file

        # Languages of the community
        db.session.execute(text('DELETE FROM "community_language" WHERE community_id = :community_id'),
                           {'community_id': community_id})
        for language_choice in form.languages.data:
            community.languages.append(Language.query.get(language_choice))
        # Always include the undetermined language, so posts with no language will be accepted
        community.languages.append(Language.query.filter(Language.code == 'und').first())
        db.session.commit()

        if community.topic_id != old_topic_id:
            if community.topic_id:
                community.topic.num_communities = community.topic.communities.count()
            if old_topic_id:
                topic = Topic.query.get(old_topic_id)
                if topic:
                    topic.num_communities = topic.communities.count()
            db.session.commit()

        flash(_('Saved'))
        return redirect(url_for('admin.admin_communities'))
    else:
        if not community.is_local():
            flash(_('This is a remote community - most settings here will be regularly overwritten with data from the original server.'), 'warning')
        form.url.data = community.name
        form.title.data = community.title
        form.description.data = community.description
        form.rules.data = community.rules
        form.nsfw.data = community.nsfw
        form.banned.data = community.banned
        form.local_only.data = community.local_only
        form.new_mods_wanted.data = community.new_mods_wanted
        form.restricted_to_mods.data = community.restricted_to_mods
        form.show_popular.data = community.show_popular
        form.show_all.data = community.show_all
        form.low_quality.data = community.low_quality
        form.content_retention.data = community.content_retention
        form.topic.data = community.topic_id if community.topic_id else None
        form.default_layout.data = community.default_layout
        form.posting_warning.data = community.posting_warning
        form.languages.data = community.language_ids()
        form.ignore_remote_language.data = community.ignore_remote_language
    return render_template('admin/edit_community.html', title=_('Edit community'), form=form, community=community)


@bp.route('/community/<int:community_id>/delete', methods=['POST'])
@permission_required('administer all communities')
@login_required
def admin_community_delete(community_id):
    community = Community.query.get_or_404(community_id)

    community.banned = True  # Unsubscribing everyone could take a long time so until that is completed hide this community from the UI by banning it.
    community.last_active = utcnow()
    db.session.commit()

    unsubscribe_everyone_then_delete(community.id)

    reason = f"Community {community.name} deleted by {current_user.user_name}"
    add_to_modlog('delete_community', actor=current_user, reason=reason, community=community)

    flash(_('Community deleted'))
    return redirect(url_for('admin.admin_communities'))


def unsubscribe_everyone_then_delete(community_id):
    if current_app.debug:
        unsubscribe_everyone_then_delete_task(community_id)
    else:
        unsubscribe_everyone_then_delete_task.delay(community_id)


@celery.task
def unsubscribe_everyone_then_delete_task(community_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                community = Community.query.get_or_404(community_id)
                if not community.is_local():
                    members = CommunityMember.query.filter_by(community_id=community_id).all()
                    for member in members:
                        user = User.query.get(member.user_id)
                        unsubscribe_from_community(community, user)
                else:
                    # todo: federate delete of local community out to all following instances
                    ...

                sleep(5)
                community.delete_dependencies()
                session.delete(community)  # todo: when a remote community is deleted it will be able to be re-created by using the 'Add remote' function. Not ideal. Consider soft-delete.
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@bp.route('/topics', methods=['GET'])
@permission_required('administer all communities')
@login_required
def admin_topics():
    topics = topic_tree()
    return render_template('admin/topics.html', title=_('Topics'), topics=topics)


@bp.route('/topic/add', methods=['GET', 'POST'])
@permission_required('administer all communities')
@login_required
def admin_topic_add():
    form = EditTopicForm()
    form.parent_id.choices = topics_for_form(0)
    if form.validate_on_submit():
        topic = Topic(name=form.name.data, machine_name=slugify(form.machine_name.data.strip()), num_communities=0,
                      show_posts_in_children=form.show_posts_in_children.data)
        if form.parent_id.data:
            topic.parent_id = form.parent_id.data
        else:
            topic.parent_id = None
        db.session.add(topic)
        db.session.commit()
        cache.delete_memoized(menu_topics)

        flash(_('Saved'))
        return redirect(url_for('admin.admin_topics'))

    return render_template('admin/edit_topic.html', title=_('Add topic'), form=form)


@bp.route('/topic/<int:topic_id>/edit', methods=['GET', 'POST'])
@permission_required('administer all communities')
@login_required
def admin_topic_edit(topic_id):
    form = EditTopicForm()
    topic = Topic.query.get_or_404(topic_id)
    form.parent_id.choices = topics_for_form(topic_id)
    if form.validate_on_submit():
        topic.name = form.name.data
        topic.num_communities = topic.communities.count()
        topic.machine_name = slugify(form.machine_name.data.strip())
        topic.show_posts_in_children = form.show_posts_in_children.data
        if form.parent_id.data:
            topic.parent_id = form.parent_id.data
        else:
            topic.parent_id = None
        db.session.commit()
        cache.delete_memoized(menu_topics)
        flash(_('Saved'))
        return redirect(url_for('admin.admin_topics'))
    else:
        form.name.data = topic.name
        form.machine_name.data = topic.machine_name
        form.parent_id.data = topic.parent_id
        form.show_posts_in_children.data = topic.show_posts_in_children
    return render_template('admin/edit_topic.html', title=_('Edit topic'), form=form, topic=topic)


@bp.route('/topic/<int:topic_id>/delete', methods=['POST'])
@permission_required('administer all communities')
@login_required
def admin_topic_delete(topic_id):
    topic = Topic.query.get_or_404(topic_id)
    topic.num_communities = topic.communities.count()
    if topic.num_communities == 0:
        db.session.delete(topic)
        flash(_('Topic deleted'))
    else:
        flash(_('Cannot delete topic with communities assigned to it.', 'error'))
    db.session.commit()

    cache.delete_memoized(menu_topics)

    return redirect(url_for('admin.admin_topics'))


@bp.route('/users', methods=['GET'])
@permission_required('administer all users')
@login_required
def admin_users():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    local_remote = request.args.get('local_remote', '')
    sort_by = request.args.get('sort_by', 'last_seen DESC')
    last_seen = request.args.get('last_seen', 0, type=int)

    sort_by_btn = request.args.get('sort_by_btn', '')
    if sort_by_btn:
        return redirect(url_for('admin.admin_users', page=page, search=search, local_remote=local_remote, sort_by=sort_by_btn, last_seen=last_seen))

    users = User.query.filter_by(deleted=False)
    if local_remote == 'local':
        users = users.filter_by(ap_id=None)
    elif local_remote == 'remote':
        users = users.filter(User.ap_id != None)
    if search:
        users = users.filter(or_(User.email.ilike(f"%{search}%"), User.user_name.ilike(f"%{search}%")))
    if last_seen > 0:
        users = users.filter(User.last_seen > utcnow() - timedelta(days=last_seen))
    if 'attitude' in sort_by:
        users = users.filter(User.attitude != None)
    users = users.order_by(safe_order_by(sort_by, User, {'user_name', 'banned', 'reports', 'attitude', 'reputation', 'created', 'last_seen'}))
    users = users.paginate(page=page, per_page=500, error_out=False)

    next_url = url_for('admin.admin_users', page=users.next_num, search=search, local_remote=local_remote,
                       sort_by=sort_by, last_seen=last_seen) if users.has_next else None
    prev_url = url_for('admin.admin_users', page=users.prev_num, search=search, local_remote=local_remote,
                       sort_by=sort_by, last_seen=last_seen) if users.has_prev and page != 1 else None

    return render_template('admin/users.html', title=_('Users'), next_url=next_url, prev_url=prev_url, users=users,
                           local_remote=local_remote, search=search, sort_by=sort_by, last_seen=last_seen,
                           user_notes=user_notes(current_user.get_id()),
                           )


@bp.route('/content', methods=['GET'])
@permission_required('administer all communities')
@login_required
def admin_content():
    page = request.args.get('page', 1, type=int)
    replies_page = request.args.get('replies_page', 1, type=int)
    posts_replies = request.args.get('posts_replies', '')
    show = request.args.get('show', 'trash')
    days = request.args.get('days', 3, type=int)

    posts = Post.query.join(User, User.id == Post.user_id).filter(Post.deleted == False,
                                                                  Post.status > POST_STATUS_REVIEWING)
    post_replies = PostReply.query.join(User, User.id == PostReply.user_id).filter(PostReply.deleted == False)
    if show == 'trash':
        title = _('Bad / Most downvoted')
        posts = posts.filter(Post.down_votes > 1, Post.score < 10)
        if days > 0:
            posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=days))
        posts = posts.order_by(Post.score)
        post_replies = post_replies.filter(PostReply.down_votes > 1, PostReply.score < 10)
        if days > 0:
            post_replies = post_replies.filter(PostReply.posted_at > utcnow() - timedelta(days=days))
        post_replies = post_replies.order_by(PostReply.score)
    elif show == 'spammy':
        title = _('Likely spam')
        posts = posts.filter(Post.score <= 0)
        if days > 0:
            posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=days),
                                 User.created > utcnow() - timedelta(days=days))
        posts = posts.order_by(Post.score)
        post_replies = post_replies.filter(PostReply.score <= 0)
        if days > 0:
            post_replies = post_replies.filter(PostReply.posted_at > utcnow() - timedelta(days=days),
                                               User.created > utcnow() - timedelta(days=days))
        post_replies = post_replies.order_by(PostReply.score)
    elif show == 'deleted':
        title = _('Deleted content')
        posts = Post.query.filter(Post.deleted == True)
        if days > 0:
            posts = posts.filter(Post.posted_at > utcnow() - timedelta(days=days))
        posts = posts.order_by(desc(Post.posted_at))
        post_replies = PostReply.query.filter(PostReply.deleted == True)
        if days > 0:
            post_replies = post_replies.filter(PostReply.posted_at > utcnow() - timedelta(days=days))
        post_replies = post_replies.order_by(desc(PostReply.posted_at))

    if posts_replies == 'posts':
        post_replies = post_replies.filter(False)
    elif posts_replies == 'replies':
        posts = posts.filter(False)

    posts = posts.paginate(page=page, per_page=100, error_out=False)
    post_replies = post_replies.paginate(page=replies_page, per_page=100, error_out=False)

    next_url = url_for('admin.admin_content', page=posts.next_num, replies_page=replies_page,
                       posts_replies=posts_replies, show=show, days=days) if posts.has_next else None
    prev_url = url_for('admin.admin_content', page=posts.prev_num, replies_page=replies_page,
                       posts_replies=posts_replies, show=show, days=days) if posts.has_prev and page != 1 else None
    next_url_replies = url_for('admin.admin_content', replies_page=post_replies.next_num, page=page,
                               posts_replies=posts_replies, show=show, days=days) if post_replies.has_next else None
    prev_url_replies = url_for('admin.admin_content', replies_page=post_replies.prev_num, page=page,
                               posts_replies=posts_replies, show=show,
                               days=days) if post_replies.has_prev and replies_page != 1 else None

    return render_template('admin/content.html', title=title,
                           next_url=next_url, prev_url=prev_url,
                           next_url_replies=next_url_replies, prev_url_replies=prev_url_replies,
                           posts=posts, post_replies=post_replies,
                           user_notes=user_notes(current_user.get_id()),
                           posts_replies=posts_replies, show=show, days=days,
                           reported_posts=reported_posts(current_user.get_id(), g.admin_ids),
                           )


@bp.route('/approve_registrations', methods=['GET'])
@permission_required('approve registrations')
@login_required
def admin_approve_registrations():
    if current_app.config['FLAG_THROWAWAY_EMAILS'] and os.path.isfile('app/static/tmp/disposable_domains.txt'):
        with open('app/static/tmp/disposable_domains.txt', 'r', encoding='utf-8') as f:
            disposable_domains = [line.rstrip('\n') for line in f]
    else:
        disposable_domains = []

    registrations = UserRegistration.query.filter_by(status=0).order_by(UserRegistration.created_at).all()
    recently_approved = UserRegistration.query.filter_by(status=1).order_by(desc(UserRegistration.approved_at)).limit(
        30)
    return render_template('admin/approve_registrations.html',
                           registrations=registrations, disposable_domains=disposable_domains,
                           recently_approved=recently_approved,
                           )


@bp.route('/approve_registrations/<int:user_id>/approve', methods=['POST'])
@permission_required('approve registrations')
@login_required
def admin_approve_registrations_approve(user_id):
    user = User.query.get_or_404(user_id)
    registration = UserRegistration.query.filter_by(status=0, user_id=user_id).first()
    if registration:
        registration.status = 1
        registration.approved_at = utcnow()
        registration.approved_by = current_user.id
        db.session.commit()
        if user.verified:
            finalize_user_setup(user)
            send_registration_approved_email(user)

        flash(_('Registration approved.'))

    return redirect(url_for('admin.admin_approve_registrations'))


@bp.route('/approve_registrations/<int:user_id>/deny', methods=['POST'])
@permission_required('approve registrations')
@login_required
def admin_approve_registrations_denied(user_id):
    user = User.query.get_or_404(user_id)
    registration = UserRegistration.query.filter_by(status=0, user_id=user_id).first()
    if registration:
        # remove the registration attempt
        db.session.delete(registration)

        # remove notifications caused by the registration attempt
        reg_notifs = Notification.query.filter_by(author_id=user.id)
        for n in reg_notifs:
            db.session.delete(n)

        # remove the user from the db so the username is available again
        user.deleted = True
        user.delete_dependencies()
        db.session.delete(user)

        # save that to the db
        db.session.commit()

        flash(_('Registration denied. User removed from the database.'))

    return redirect(url_for('admin.admin_approve_registrations'))


@bp.route('/user/<int:user_id>/edit', methods=['GET', 'POST'])
@permission_required('administer all users')
@login_required
def admin_user_edit(user_id):
    form = EditUserForm()
    user = User.query.get_or_404(user_id)
    if form.validate_on_submit():
        user.bot = form.bot.data
        user.bot_override = form.bot_override.data
        user.suppress_crossposts = form.suppress_crossposts.data
        user.banned = form.banned.data
        user.ban_posts = form.ban_posts.data
        user.ban_comments = form.ban_comments.data
        user.hide_nsfw = form.hide_nsfw.data
        user.hide_nsfl = form.hide_nsfl.data
        if form.verified.data and not user.verified:
            finalize_user_setup(user)
        user.verified = form.verified.data
        if form.remove_avatar.data and user.avatar_id:
            file = File.query.get(user.avatar_id)
            file.delete_from_disk()
            user.avatar_id = None
            db.session.delete(file)

        if form.remove_banner.data and user.cover_id:
            file = File.query.get(user.cover_id)
            file.delete_from_disk()
            user.cover_id = None
            db.session.delete(file)

        # Update user roles. The UI only lets the user choose 1 role but the DB structure allows for multiple roles per user.
        db.session.execute(text('DELETE FROM user_role WHERE user_id = :user_id'), {'user_id': user.id})
        user.roles.append(Role.query.get(form.role.data))
        if form.role.data == 4:
            flash(_("Permissions are cached for 50 seconds so new admin roles won't take effect immediately."))

        db.session.commit()
        cache.delete_memoized(low_value_reposters)

        flash(_('Saved'))
        return redirect(url_for('admin.admin_users', local_remote='local' if user.is_local() else 'remote'))
    else:
        if not user.is_local():
            flash(_('This is a remote user - most settings here will be regularly overwritten with data from the original server.'), 'warning')
        form.bot.data = user.bot
        form.bot_override.data = user.bot_override
        form.suppress_crossposts.data = user.suppress_crossposts
        form.verified.data = user.verified
        form.banned.data = user.banned
        form.ban_posts.data = user.ban_posts
        form.ban_comments.data = user.ban_comments
        form.hide_nsfw.data = user.hide_nsfw
        form.hide_nsfl.data = user.hide_nsfl
        if user.roles and user.roles.count() > 0:
            form.role.data = user.roles[0].id

    return render_template('admin/edit_user.html', title=_('Edit user'), form=form, user=user)


@bp.route('/users/add', methods=['GET', 'POST'])
@permission_required('administer all users')
@login_required
def admin_users_add():
    form = AddUserForm()
    user = User()
    if form.validate_on_submit():
        user.user_name = form.user_name.data
        user.title = form.user_name.data
        user.set_password(form.password.data)
        user.about = form.about.data
        user.email = form.email.data
        user.about_html = markdown_to_html(form.about.data)
        user.matrix_user_id = form.matrix_user_id.data
        user.bot = form.bot.data
        profile_file = request.files['profile_file']
        if profile_file and profile_file.filename != '':
            # remove old avatar
            if user.avatar_id:
                file = File.query.get(user.avatar_id)
                file.delete_from_disk()
                user.avatar_id = None
                db.session.delete(file)

            # add new avatar
            file = save_icon_file(profile_file, 'users')
            if file:
                user.avatar = file
        banner_file = request.files['banner_file']
        if banner_file and banner_file.filename != '':
            # remove old cover
            if user.cover_id:
                file = File.query.get(user.cover_id)
                file.delete_from_disk()
                user.cover_id = None
                db.session.delete(file)

            # add new cover
            file = save_banner_file(banner_file, 'users')
            if file:
                user.cover = file
        user.newsletter = form.newsletter.data
        user.ignore_bots = form.ignore_bots.data
        user.hide_nsfw = form.hide_nsfw.data
        user.hide_nsfl = form.hide_nsfl.data

        user.instance_id = 1
        user.roles.append(Role.query.get(form.role.data))
        db.session.add(user)
        db.session.commit()
        finalize_user_setup(user)

        flash(_('User added'))
        return redirect(url_for('admin.admin_users', local_remote='local'))

    return render_template('admin/add_user.html', title=_('Add user'), form=form, user=user)


@bp.route('/user/<int:user_id>/delete', methods=['POST'])
@permission_required('administer all users')
@login_required
def admin_user_delete(user_id):
    if user_id == 1:
        flash(_('This user cannot be deleted.'))
        return redirect(referrer())
    user = User.query.get_or_404(user_id)

    user.banned = True  # Unsubscribing everyone could take a long time so until that is completed hide this user from the UI by banning it.
    user.last_active = utcnow()
    user.deleted_by = current_user.id
    db.session.commit()

    if user.is_local():
        if user.private_key is not None:  # They have a private key once the registration is fully completed
            unsubscribe_from_everything_then_delete(user.id)
        else:  # Non-finalized users can just be deleted as they will not have been federated anywhere.
            user.deleted = True
            user.delete_dependencies()
            db.session.commit()
    else:
        user.deleted = True
        user.delete_dependencies()
        db.session.commit()

        add_to_modlog('delete_user', actor=current_user, target_user=user, link_text=user.display_name(), link=user.link())

    flash(_('User deleted'))
    return redirect(referrer())



@bp.route('/reports', methods=['GET'])
@permission_required('administer all users')
@login_required
def admin_reports():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    local_remote = request.args.get('local_remote', '')
    report_types = request.args.getlist('report_types',  type=int)  # Extract multiple values
    
    if len(report_types) == 0:
        report_types = [-1]
    
    reports = Report.query.filter(or_(Report.status == REPORT_STATE_NEW, Report.status == REPORT_STATE_ESCALATED))
    if local_remote == 'local':
        reports = reports.filter_by(source_instance_id=1)
    if local_remote == 'remote':
        reports = reports.filter(Report.source_instance_id != 1)
    if len(report_types) > 0 and -1 not in report_types:
        reports = reports.filter(Report.type.in_(report_types))
    reports = reports.order_by(desc(Report.created_at)).paginate(page=page, per_page=1000, error_out=False)

    next_url = url_for('admin.admin_reports', page=reports.next_num) if reports.has_next else None
    prev_url = url_for('admin.admin_reports', page=reports.prev_num) if reports.has_prev and page != 1 else None

    return render_template('admin/reports.html', title=_('Reports'), next_url=next_url, prev_url=prev_url,
                           reports=reports, local_remote=local_remote, search=search, report_types=report_types, report_types_list=ReportTypes.get_choices())


@bp.route('/newsletter', methods=['GET', 'POST'])
@permission_required('change instance settings')
@login_required
def newsletter():
    form = SendNewsletterForm()
    if form.validate_on_submit():
        send_newsletter(form)
        flash(_('Newsletter sent'))
        return redirect(url_for('admin.newsletter'))

    return render_template("admin/newsletter.html", form=form, title=_('Send newsletter'))


@bp.route('/permissions', methods=['GET', 'POST'])
@permission_required('change user roles')
@login_required
def admin_permissions():
    form = FlaskForm()
    if request.method == 'POST':
        permissions = db.session.execute(text('SELECT DISTINCT permission FROM "role_permission"')).fetchall()
        db.session.execute(text('DELETE FROM "role_permission"'))
        roles = [3, 4]  # 3 = Staff, 4 = Admin
        staff_user_ids = list(db.session.execute(text('SELECT user_id FROM "user_role" WHERE role_id = 3')).scalars())
        for permission in permissions:
            for role in roles:
                if request.form.get(f'role_{role}_{permission[0]}'):
                    db.session.add(RolePermission(role_id=role, permission=permission[0]))
            for staff_user_id in staff_user_ids:
                cache.delete_memoized(user_access, permission, staff_user_id)
        db.session.commit()

        flash(_('Settings saved'))

    roles = Role.query.filter(Role.id > 2).order_by(Role.weight).all()
    permissions = db.session.execute(text('SELECT DISTINCT permission FROM "role_permission"')).fetchall()

    return render_template('admin/permissions.html', title=_('Role permissions'), roles=roles,
                           form=form, permissions=permissions)


@bp.route('/instances', methods=['GET', 'POST'])
@permission_required('change instance settings')
@login_required
def admin_instances():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    filter = request.args.get('filter', '')
    sort_by = request.args.get('sort_by', 'domain ASC')
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'

    instances = Instance.query

    if search:
        instances = instances.filter(Instance.domain.ilike(f"%{search}%"))
    title = 'Instances'
    if filter:
        if filter == 'trusted':
            instances = instances.filter(Instance.trusted == True)
            title = 'Trusted instances'
        elif filter == 'online':
            instances = instances.filter(Instance.dormant == False, Instance.gone_forever == False)
            title = 'Online instances'
        elif filter == 'dormant':
            instances = instances.filter(Instance.dormant == True, Instance.gone_forever == False)
            title = 'Dormant instances'
        elif filter == 'gone_forever':
            instances = instances.filter(Instance.gone_forever == True)
            title = 'Gone forever instances'
        elif filter == 'blocked':
            instances = instances.join(BannedInstances, BannedInstances.domain == Instance.domain)

    instances = instances.order_by(safe_order_by(sort_by, Instance, {'domain', 'software', 'version', 'vote_weight',
                                                                     'trusted', 'last_seen', 'last_successful_send',
                                                                     'failures', 'gone_forever', 'dormant'}))
    instances = instances.paginate(page=page, per_page=50, error_out=False)
    next_url = url_for('admin.admin_instances', page=instances.next_num, search=search, filter=filter,
                       sort_by=sort_by) if instances.has_next else None
    prev_url = url_for('admin.admin_instances', page=instances.prev_num, search=search, filter=filter,
                       sort_by=sort_by) if instances.has_prev and page != 1 else None

    return render_template('admin/instances.html', instances=instances,
                           title=_(title), search=search, filter=filter, sort_by=sort_by,
                           next_url=next_url, prev_url=prev_url,
                           low_bandwidth=low_bandwidth)


@bp.route('/instance/<int:instance_id>/edit', methods=['GET', 'POST'])
@permission_required('administer all communities')
@login_required
def admin_instance_edit(instance_id):
    form = EditInstanceForm()
    instance = Instance.query.get_or_404(instance_id)
    if form.validate_on_submit():
        instance.vote_weight = form.vote_weight.data
        instance.dormant = form.dormant.data
        instance.gone_forever = form.gone_forever.data
        instance.trusted = form.trusted.data
        instance.posting_warning = form.posting_warning.data
        instance.inbox = form.inbox.data

        db.session.commit()

        flash(_('Saved'))
        return redirect(url_for('admin.admin_instances'))
    else:
        form.vote_weight.data = instance.vote_weight
        form.dormant.data = instance.dormant
        form.gone_forever.data = instance.gone_forever
        form.trusted.data = instance.trusted
        form.posting_warning.data = instance.posting_warning
        form.inbox.data = instance.inbox

    return render_template('admin/edit_instance.html', title=_('Edit instance'), form=form, instance=instance)


@bp.route('/community/<int:community_id>/move/<int:new_owner>', methods=['GET', 'POST'])
@permission_required('change instance settings')
@login_required
def admin_community_move(community_id, new_owner):
    community = Community.query.get_or_404(community_id)
    new_owner_user = User.query.get_or_404(new_owner)
    form = MoveCommunityForm()

    form.new_owner.label.text = _('Set community owner to %(user_name)s', user_name=new_owner_user.link())

    if form.validate_on_submit():
        form.new_url.data = slugify(form.new_url.data, separator='_').lower()
        old_name = community.link()
        community.ap_id = None
        private_key, public_key = RsaKeys.generate_keypair()
        community.name = form.new_url.data.lower()
        community.private_key = private_key
        community.public_key = public_key
        community.ap_profile_id = 'https://' + current_app.config['SERVER_NAME'] + '/c/' + form.new_url.data
        community.ap_public_url = 'https://' + current_app.config['SERVER_NAME'] + '/c/' + form.new_url.data
        community.ap_followers_url = 'https://' + current_app.config['SERVER_NAME'] + '/c/' + form.new_url.data + '/followers'
        community.ap_featured_url = 'https://' + current_app.config['SERVER_NAME'] + '/c/' + form.new_url.data + '/featured'
        community.ap_moderators_url = 'https://' + current_app.config['SERVER_NAME'] + '/c/' + form.new_url.data + '/moderators'
        community.ap_domain = current_app.config['SERVER_NAME']
        community.instance_id = 1

        if form.new_owner.data:
            community.user_id = new_owner_user.id
        db.session.commit()
        try:
            membership = CommunityMember(user_id=new_owner_user.id, community_id=community.id,
                                         is_owner=new_owner_user.id)
            db.session.add(membership)
            db.session.commit()
        except:
            db.session.rollback()

        cache.delete_memoized(community_membership, new_owner_user, community)
        cache.delete_memoized(joined_communities, new_owner_user.id)
        cache.delete_memoized(moderating_communities, new_owner_user.id)

        if current_app.debug:
            move_community_images_to_here(community.id)
        else:
            move_community_images_to_here.delay(community.id)

        new_url = f'https://{current_app.config["SERVER_NAME"]}/c/{community.link()}'
        flash(_('%(community_name)s is now %(new_url)s. Contact the initiator of this request to let them know.',
                community_name=old_name, new_url=new_url))

        flash(_('Ensure this community has the right moderators.'))
        return redirect(url_for('community.community_mod_list', community_id=community.id))

    form.new_url.data = community.name

    return render_template('admin/community_move.html', title=_('Move community'), form=form, community=community)


@bp.route('/blocked_images', methods=['GET'])
@permission_required('administer all communities')
@login_required
def admin_blocked_images():
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    blocked_images = BlockedImage.query.order_by(desc(BlockedImage.id)).all()
    return render_template('admin/blocked_images.html', blocked_images=blocked_images,
                           title=_('Blocked images'),
                           low_bandwidth=low_bandwidth)


@bp.route('/blocked_image/<int:image_id>/edit', methods=['GET', 'POST'])
@permission_required('administer all communities')
@login_required
def admin_blocked_image_edit(image_id):
    form = EditBlockedImageForm()
    image = BlockedImage.query.get_or_404(image_id)
    if form.validate_on_submit():
        image.hash = form.hash.data
        image.file_name = form.file_name.data
        image.note = form.note.data
        db.session.commit()

        flash(_('Saved'))
        return redirect(url_for('admin.admin_blocked_images'))
    else:
        form.hash.data = image.hash
        form.file_name.data = image.file_name
        form.note.data = image.note

    return render_template('admin/edit_blocked_image.html', title=_('Edit blocked image'), form=form,
                           blocked_image=image)


@bp.route('/blocked_image/add', methods=['GET', 'POST'])
@permission_required('administer all communities')
@login_required
def admin_blocked_image_add():
    form = AddBlockedImageForm()
    if form.validate_on_submit():
        if form.url.data:
            hash = retrieve_image_hash(form.url.data)
            file_name = str(furl(form.url.data).path).split('/')
            file_name = file_name[-1]
        else:
            hash = form.hash.data
            file_name = form.file_name.data
        image = BlockedImage(hash=hash, file_name=file_name, note=form.note.data)
        db.session.add(image)
        db.session.commit()

        flash(_('Saved'))
        return redirect(url_for('admin.admin_blocked_image_purge_posts'))

    flash(_('Provide the url of an image or the hash (and file name) of it, but not both.'))

    return render_template('admin/edit_blocked_image.html', title=_('Add blocked image'), form=form)


@bp.route('/block_image_purge_posts', methods=['GET', 'POST'])
@permission_required('administer all communities')
@login_required
def admin_blocked_image_purge_posts():
    form = FlaskForm()
    if request.method == 'POST':
        post_ids = request.form.getlist('post_ids')

        task_selector('delete_posts_with_blocked_images', post_ids=post_ids, user_id=current_user.id,
                      send_async=not current_app.debug)

        flash(_('%(count)s posts deleted.', count=len(post_ids)))

        return redirect(url_for('admin.admin_blocked_images'))

    posts = Post.query.filter(Post.id.in_(posts_with_blocked_images()), Post.deleted == False).order_by(desc(Post.posted_at)).all()
    return render_template('post/post_block_image_purge_posts.html', posts=posts,
                           title=_('Posts containing blocked images'),
                           form=form, referrer=request.args.get('referrer'))


@bp.route('/blocked_image/<int:image_id>/delete', methods=['POST'])
@permission_required('administer all communities')
@login_required
def admin_blocked_image_delete(image_id):
    image = BlockedImage.query.get_or_404(image_id)

    db.session.delete(image)
    db.session.commit()

    flash(_('Blocked image deleted'))

    return redirect(url_for('admin.admin_blocked_images'))


# CMS pages
@bp.route('/pages', methods=['GET'])
@permission_required('edit cms pages')
@login_required
def admin_cms_pages():
    pages = CmsPage.query.order_by(CmsPage.created_at.desc()).all()
    return render_template('admin/cms_pages.html', pages=pages, title=_('CMS Pages'))


@bp.route('/pages/add', methods=['GET', 'POST'])
@permission_required('edit cms pages')
@login_required
def admin_cms_page_add():
    form = CmsPageForm()
    if form.validate_on_submit():
        page = CmsPage(url=form.url.data, title=form.title.data, body=form.body.data,
                       body_html=markdown_to_html(form.body.data), last_edited_by=current_user.display_name())
        db.session.add(page)
        db.session.commit()
        flash(_('Page saved.'))
        return redirect(url_for('admin.admin_cms_pages'))

    return render_template('admin/cms_page_edit.html', form=form, title=_('Add CMS Page'))


@bp.route('/pages/<int:page_id>/edit', methods=['GET', 'POST'])
@permission_required('edit cms pages')
@login_required
def admin_cms_page_edit(page_id):
    page = CmsPage.query.get_or_404(page_id)
    form = CmsPageForm(original_page=page, obj=page)

    if form.validate_on_submit():
        page.url = form.url.data
        page.title = form.title.data
        page.body = form.body.data
        page.body_html = markdown_to_html(form.body.data)
        page.last_edited_by = current_user.display_name()
        page.edited_at = utcnow()
        db.session.commit()
        flash(_('Page saved.'))
        return redirect(url_for('admin.admin_cms_pages'))

    return render_template('admin/cms_page_edit.html', form=form, page=page, title=_('Edit page'))


@bp.route('/pages/<int:page_id>/delete', methods=['POST'])
@permission_required('edit cms pages')
@login_required
def admin_cms_page_delete(page_id):
    page = CmsPage.query.get_or_404(page_id)
    db.session.delete(page)
    db.session.commit()
    flash(_('Page deleted.'))
    return redirect(url_for('admin.admin_cms_pages'))
