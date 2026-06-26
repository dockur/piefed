from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed
from sqlalchemy import func
from wtforms import StringField, PasswordField, SubmitField, EmailField, BooleanField, TextAreaField, \
    SelectField, FileField, IntegerField, RadioField
from wtforms.fields.choices import SelectMultipleField
from wtforms.validators import ValidationError, DataRequired, EqualTo, Length, Optional
from flask_babel import _, lazy_gettext as _l

from app.constants import DOWNVOTE_ACCEPT_ALL, DOWNVOTE_ACCEPT_MEMBERS, DOWNVOTE_ACCEPT_INSTANCE, \
    DOWNVOTE_ACCEPT_TRUSTED, DOWNVOTE_ACCEPT_NONE
from app.models import Community, User, CmsPage


class SiteProfileForm(FlaskForm):
    name = StringField(_l('Site Name'))
    description = StringField(_l('Tagline'))
    icon = FileField(_l('Icon'), validators=[FileAllowed(['jpg', 'jpeg', 'png', 'webp', 'svg'], 'Images only!')],
                     render_kw={'accept': 'image/*'})
    sidebar = TextAreaField(_l('Sidebar'))
    about = TextAreaField(_l('About'))
    announcement = TextAreaField(_l('Announcement at top of home page'))
    legal_information = TextAreaField(_l('Legal information'))
    tos_url = StringField(_l('Terms of service url'), validators=[Length(max=255)])
    contact_email = EmailField(_l('General instance contact email address'), validators=[DataRequired(), Length(min=5, max=255)])
    submit = SubmitField(_l('Save'))


class SiteMiscForm(FlaskForm):
    enable_downvotes = BooleanField(_l('Enable downvotes'))
    enable_gif_reply_rep_decrease = BooleanField(_l('Decrease reputation when posting only a gif as a comment'))
    enable_chan_image_filter = BooleanField(_l('Decrease reputation when an image post matches the 4chan filter'))
    enable_this_comment_filter = BooleanField(_l('Filter out comments that are simply a form of "this"'))
    meme_comms_low_quality = BooleanField(_l('Meme communities = low-quality'))
    allow_local_image_posts = BooleanField(_l('Allow local image posts'))
    video_upload_options = [('no', _l('No')),
               ('user 1', _l('User ID 1')),
               ('admins', _l('Admins and staff')),
               ('users', _l('Any user')),
               ]
    allow_video_file_uploads = SelectField(_l('Allow video file uploads'), choices=video_upload_options,
                                           validators=[DataRequired()], render_kw={'class': 'form-select'})
    enable_nsfw = BooleanField(_l('Allow NSFW communities'))
    enable_nsfl = BooleanField(_l('Allow NSFL communities and posts'))
    nsfw_country_restriction = TextAreaField(_l('Bar people from these countries from accessing NSFW and NSFL content'))
    community_creation_admin_only = BooleanField(_l('Only admins can create new local communities'))
    reports_email_admins = BooleanField(_l('Notify admins about reports, not just moderators'))
    email_verification = BooleanField(_l('Require new accounts to verify their email address'))
    captcha_enabled = BooleanField(_l('Require CAPTCHA for new account registration'))
    types = [('Open', _l('Open')), ('RequireApplication', _l('Require application')), ('Closed', _l('Closed'))]
    registration_mode = SelectField(_l('Registration mode'), choices=types, default=1, coerce=str, render_kw={'class': 'form-select'})
    application_question = TextAreaField(_l('Question to ask people applying for an account'))
    registration_approved_email = TextAreaField(_l('Registration approved email'), render_kw={'rows': '5'})

    choose_topics = BooleanField(_l('Provide a list of topics to subscribe to'))
    filter_selection = BooleanField(_l('Filter setup during onboarding'))
    auto_decline_countries = TextAreaField(_l('Ignore registrations from these countries'))
    auto_decline_referrers = TextAreaField(_l('Block registrations from these referrers (one per line)'))
    ban_check_servers = TextAreaField(_l('Warn if new account banned from these instances'))
    language_id = SelectField(_l('Primary language'), validators=[DataRequired()], coerce=int, render_kw={'class': 'form-select'})
    default_theme = SelectField(_l('Default theme'), coerce=str, render_kw={'class': 'form-select'})
    additional_css = TextAreaField(_l('Additional CSS'))
    additional_js = TextAreaField(_l('Additional JS'))
    read_posts_cutoff = IntegerField(_l('Trim read posts table after this many days'))
    filters = [('local', _l('Local')),
               ('popular', _l('Popular')),
               ('all', _l('All')),
               ]
    default_filter = SelectField(_l('Default home filter'), choices=filters, validators=[DataRequired()], coerce=str,
                                 render_kw={'class': 'form-select'})
    cache_remote_images_locally = BooleanField(_l('Cache remote images locally'))
    enable_report_em_dash_replies = BooleanField(_l('Report comments containing an em dash (—) from newly created users - likely spam / AI.'))
    limit_one_em_report_per_user = BooleanField(_l('Limit em dash reports to just the first occurence per user'))
    log_activitypub_json = BooleanField(_l('Log ActivityPub JSON for debugging'))
    public_modlog = BooleanField(_l('Show moderation actions publicly'))
    private_instance = BooleanField(_l('Private instance - require login to browse'))
    show_inoculation_block = BooleanField(_l('Show Rational Discourse Toolkit in sidebar'))
    allow_default_user_add_remote_community = BooleanField(_l('Allow non-admins to add remote communities'))
    honeypot = BooleanField(_l('Honeypot to block bad crawlers'))

    submit = SubmitField(_l('Save'))


class InstanceChooserForm(FlaskForm):
    enable_instance_chooser = BooleanField(_l('Enable instance chooser'))
    elevator_pitch = StringField(_l('One-sentence elevator pitch'), validators=[Length(max=90)])
    number_of_admins = IntegerField(_l('Number of admins with emergency access'))
    financial_stability = BooleanField(_l('Non-admins donate enough to pay for hosting'))
    daily_backups = BooleanField(_l('This instance has automated daily backups'))
    submit = SubmitField(_l('Save'))


class FederationForm(FlaskForm):
    federation_mode = RadioField(_l('Federation mode'), choices=[
        ('blocklist', _l('Blocklist - deny federation with specified instances')),
        ('allowlist', _l('Allowlist - only allow federation with specified instances'))
    ], default='blocklist')
    allowlist = TextAreaField(_l('Allow federation with these instances'))
    allowlist_mode = RadioField(_l('Allowlist mode'), choices=[
        (0, _l('Weak')),
        (1, _l('Strong')),
        (2, _l('Intense'))
    ], default=0)
    blocklist = TextAreaField(_l('Deny federation with these instances'))
    defederation_subscription = TextAreaField(_l('Auto-defederate from any instance defederated by'))
    blocked_phrases = TextAreaField(_l('Discard all posts, comments and PMs with these phrases (one per line)'))
    blocked_actors = TextAreaField(_l('Discard all posts and comments by users with these words in their name (one per line)'))
    blocked_bio = TextAreaField(_l('Discard all posts and comments by users with these phrases in their bio (one per line)'))
    auto_add_remote_communities = BooleanField(_l('Automatically add new remote communities'))
    submit = SubmitField(_l('Save'))


class CloseInstanceForm(FlaskForm):
    announcement = TextAreaField(_l('Closing down announcement for home page'), validators=[DataRequired()])
    submit = SubmitField(_l('Yes, close my instance'))


class PreLoadCommunitiesForm(FlaskForm):
    communities_num = IntegerField(_l('Number of Communities to add'), default=25)
    pre_load_submit = SubmitField(_l('Add Communities'))


class RemoteInstanceScanForm(FlaskForm):
    remote_url = StringField(_l('Remote Server'), validators=[DataRequired()])
    communities_requested = IntegerField(_l('Number of Communities to add'), default=25)
    minimum_posts = IntegerField(_l('Communities must have at least this many posts'), default=100)
    minimum_active_users = IntegerField(_l('Communities must have at least this many active users in the past week.'), default=100)
    dry_run = BooleanField(_l('Dry Run'))
    remote_scan_submit = SubmitField(_l('Scan'))


class ImportExportBannedListsForm(FlaskForm):
    import_file = FileField(_l('Import Bans List Json File'))
    import_submit = SubmitField(_l('Import'))
    export_submit = SubmitField(_l('Export'))


class EditCommunityForm(FlaskForm):
    title = StringField(_l('Title'), validators=[DataRequired()])
    url = StringField(_l('Url'), validators=[DataRequired()])
    description = TextAreaField(_l('Description'))
    icon_file = FileField(_l('Icon image'))
    banner_file = FileField(_l('Banner image'))
    rules = TextAreaField(_l('Rules'))
    nsfw = BooleanField(_l('NSFW community'))
    ai_generated = BooleanField(_l('Only AI-generated content'))
    banned = BooleanField(_l('Banned - no new posts accepted'))
    local_only = BooleanField(_l('Only accept posts from current instance'))
    restricted_to_mods = BooleanField(_l('Only moderators can post'))
    new_mods_wanted = BooleanField(_l('New moderators wanted'))
    downvote_accept_modes = [(DOWNVOTE_ACCEPT_ALL, _l('Everyone')),
                             (DOWNVOTE_ACCEPT_NONE, _l('Nobody')),
                             (DOWNVOTE_ACCEPT_MEMBERS, _l('Community members')),
                             (DOWNVOTE_ACCEPT_INSTANCE, _l('This instance')),
                             (DOWNVOTE_ACCEPT_TRUSTED, _l('Trusted instances')),

                             ]
    downvote_accept_mode = SelectField(_l('Accept downvotes from'), coerce=int, choices=downvote_accept_modes,
                                       validators=[Optional()], render_kw={'class': 'form-select'})
    show_popular = BooleanField(_l('Posts can be popular'))
    show_all = BooleanField(_l('Posts show in All list'))
    low_quality = BooleanField(_l("Low quality / toxic - upvotes in here don't add to reputation"))
    options = [(-1, _l('Forever')),
               (7, _l('1 week')),
               (14, _l('2 weeks')),
               (28, _l('1 month')),
               (56, _l('2 months')),
               (84, _l('3 months')),
               (168, _l('6 months')),
               (365, _l('1 year')),
               (730, _l('2 years')),
               (1825, _l('5 years')),
               (3650, _l('10 years')),
             ]
    content_retention = SelectField(_l('Retain content'), choices=options, default=1, coerce=int, render_kw={'class': 'form-select'})
    topic = SelectField(_l('Topic'), coerce=int, validators=[Optional()], render_kw={'class': 'form-select'})
    layouts = [('', _l('List')),
               ('masonry', _l('Masonry')),
               ('masonry_wide', _l('Wide masonry'))]
    default_layout = SelectField(_l('Layout'), coerce=str, choices=layouts, validators=[Optional()], render_kw={'class': 'form-select'})
    posting_warning = StringField(_l('Posting warning'), validators=[Optional(), Length(min=3, max=512)])
    languages = SelectMultipleField(_l('Languages'), coerce=int, validators=[Optional()], render_kw={'class': 'form-select'})
    ignore_remote_language = BooleanField(_l('Override remote language setting'))
    ignore_remote_gen_ai = BooleanField(_l('Override remote AI content setting'))
    always_translate = BooleanField(_l('Always show translation icon on posts'))
    can_be_archived = BooleanField(_l('Old posts can be archived'))
    submit = SubmitField(_l('Save'))

    def validate(self, extra_validators=None):
        if not super().validate():
            return False
        if self.url.data.strip() == '':
            self.url.errors.append(_l('Url is required.'))
            return False
        # commented out as PeerTube and NodeBB can both use dashes in their URLs
        #else:
        #    if '-' in self.url.data.strip():
        #        self.url.errors.append(_l('- cannot be in Url. Use _ instead?'))
        #        return False
        return True


class EditTopicForm(FlaskForm):
    name = StringField(_l('Name'), validators=[DataRequired()], render_kw={'title': _l('Human readable name for the topic.')})
    machine_name = StringField(_l('Slug'), validators=[DataRequired()], render_kw={'title': _l('A short and unique identifier that becomes part of the URL.')})
    parent_id = SelectField(_l('Parent topic'), coerce=int, validators=[Optional()], render_kw={'class': 'form-select'})
    show_posts_in_children = BooleanField(_l('Show posts from child topics'), validators=[Optional()])
    countries = TextAreaField(_l('Countries'), validators=[Optional()], render_kw={'title': _l('Use two-letter country codes, one per line.')})
    submit = SubmitField(_l('Save'))


class EditInstanceForm(FlaskForm):
    dormant = BooleanField(_l('Dormant'))
    gone_forever = BooleanField(_l('Gone forever'))
    trusted = BooleanField(_l('Trusted'))
    silenced = BooleanField(_l('Silenced'))
    hide = BooleanField(_l('Hide from instance chooser'))
    popular = BooleanField(_l('Communities become popular when added'))
    posting_warning = TextAreaField(_l('Posting warning'))
    admin_note = TextAreaField(_l('Notes about this instance (only visible to other admins)'))
    inbox = StringField(_l('Inbox'))
    submit = SubmitField(_l('Save'))


class CreateOfflineInstanceForm(FlaskForm):
    domain = StringField(_l('Domain (not including https://)'))
    submit = SubmitField(_l('Save'))


class EditBlockedImageForm(FlaskForm):
    hash = TextAreaField(_l('Hash'), validators=[DataRequired(), Length(min=256, max=256)])
    file_name = StringField(_l('Filename'), validators=[Optional(), Length(max=256)])
    note = StringField(_l('Note'), validators=[Optional(), Length(max=256)])
    submit = SubmitField(_l('Save'))


class AddBlockedImageForm(FlaskForm):
    url = StringField(_l('Url'), validators=[Optional()])
    hash = TextAreaField(_l('Hash'), validators=[Optional(), Length(min=256, max=256)])
    file_name = StringField(_l('Filename'), validators=[Optional(), Length(max=256)])
    note = StringField(_l('Note'), validators=[Optional(), Length(max=256)])
    submit = SubmitField(_l('Save'))


class AddUserForm(FlaskForm):
    user_name = StringField(_l('User name'), validators=[DataRequired(), Length(max=50)],
                            render_kw={'autofocus': True, 'autocomplete': 'off'})
    email = StringField(_l('Email address'), validators=[Optional(), Length(max=255)])
    password = PasswordField(_l('Password'), validators=[DataRequired(), Length(min=8, max=50)],
                             render_kw={'autocomplete': 'new-password'})
    password2 = PasswordField(_l('Repeat password'), validators=[DataRequired(), EqualTo('password')])
    about = TextAreaField(_l('Bio'), validators=[Optional(), Length(min=3, max=5000)])
    matrix_user_id = StringField(_l('Matrix User ID'), validators=[Optional(), Length(max=255)])
    profile_file = FileField(_l('Avatar image'))
    banner_file = FileField(_l('Top banner image'))
    bot = BooleanField(_l('This profile is a bot'))
    verified = BooleanField(_l('Email address is verified'))
    banned = BooleanField(_l('Banned'))
    newsletter = BooleanField(_l('Subscribe to email newsletter'))
    hide_type_choices = [(0, _l('Show')),
                         (1, _l('Hide completely')),
                         (2, _l('Blur')),
                         (3, _l('Make semi-transparent'))]
    ignore_bots = SelectField(_l('Posts by bots'), choices=hide_type_choices,
                                 default=0, coerce=int, render_kw={'class': 'form-select'})
    hide_nsfw = SelectField(_l('NSFW posts'), choices=hide_type_choices, default=1,
                            coerce=int, render_kw={'class': 'form-select'})
    hide_nsfl = SelectField(_l('NSFL posts'), choices=hide_type_choices, default=1,
                            coerce=int, render_kw={'class': 'form-select'})

    role_options = [(2, _l('User')),
               (3, _l('Staff')),
               (4, _l('Admin')),
               ]
    role = SelectField(_l('Role'), choices=role_options, default=2, coerce=int, render_kw={'class': 'form-select'})
    submit = SubmitField(_l('Save'))

    def validate_email(self, email):
        user = User.query.filter(func.lower(User.email) == func.lower(email.data.strip())).first()
        if user is not None:
            raise ValidationError(_l('An account with this email address already exists.'))

    def validate_user_name(self, user_name):
        if '@' in user_name.data:
            raise ValidationError(_l('User names cannot contain @.'))
        user = User.query.filter(func.lower(User.user_name) == func.lower(user_name.data.strip())).filter_by(ap_id=None).first()
        if user is not None:
            if user.deleted:
                raise ValidationError(_l('This username was used in the past and cannot be reused.'))
            else:
                raise ValidationError(_l('An account with this user name already exists.'))
        community = Community.query.filter(func.lower(Community.name) == func.lower(user_name.data.strip())).first()
        if community is not None:
            raise ValidationError(_l('A community with this name exists so it cannot be used for a user.'))

    def validate_password(self, password):
        if not password.data:
            return
        password.data = password.data.strip()
        if password.data == 'password' or password.data == '12345678' or password.data == '1234567890':
            raise ValidationError(_l('This password is too common.'))

        first_char = password.data[0]  # the first character in the string

        all_the_same = True
        # Compare all characters to the first character
        for char in password.data:
            if char != first_char:
                all_the_same = False
        if all_the_same:
            raise ValidationError(_l('This password is not secure.'))

        if password.data == 'password' or password.data == '12345678' or password.data == '1234567890':
            raise ValidationError(_l('This password is too common.'))


class EditUserForm(FlaskForm):
    bot = BooleanField(_l('This profile is a bot'))
    bot_override = BooleanField(_l('Flag their posts as from a bot'))
    suppress_crossposts = BooleanField(_l('Suppress cross-posts'))
    verified = BooleanField(_l('Email address is verified'))
    banned = BooleanField(_l('Banned'))
    ban_posts = BooleanField(_l('Ban posts'))
    ban_comments = BooleanField(_l('Ban comments'))
    can_send_pm = BooleanField(_l('Can send PMs'))
    hide_type_choices = [(0, _l('Show')),
                         (1, _l('Hide completely')),
                         (2, _l('Blur')),
                         (3, _l('Make semi-transparent'))]
    hide_nsfw = SelectField(_l('NSFW posts'), choices=hide_type_choices, default=1,
                            coerce=int, render_kw={'class': 'form-select'})
    hide_nsfl = SelectField(_l('NSFL posts'), choices=hide_type_choices, default=1,
                            coerce=int, render_kw={'class': 'form-select'})
    role_options = [(2, _l('User')),
               (3, _l('Staff')),
               (4, _l('Admin')),
               ]
    role = SelectField(_l('Role'), choices=role_options, default=2, coerce=int, render_kw={'class': 'form-select'})
    remove_avatar = BooleanField(_l('Remove avatar'))
    remove_banner = BooleanField(_l('Remove banner'))
    admin_note = TextAreaField(_l('Notes about this user (only visible to other admins)'))
    submit = SubmitField(_l('Save'))


class SendNewsletterForm(FlaskForm):
    subject = StringField(_l('Subject'), validators=[DataRequired()])
    body_text = TextAreaField(_l('Body (text)'), render_kw={"rows": 10}, validators=[DataRequired()])
    body_html = TextAreaField(_l('Body (html)'), render_kw={"rows": 20}, validators=[DataRequired()])
    test = BooleanField(_l('Test mode'), render_kw={'checked': True})
    submit = SubmitField(_l('Send newsletter'))


class MoveCommunityForm(FlaskForm):
    new_url = StringField(_l('New url'), validators=[DataRequired()])
    new_owner = BooleanField(_l('Set new owner'))
    submit = SubmitField(_l('Submit'))

    def validate_new_url(self, new_url):
        existing_community = Community.query.filter(Community.ap_id == None, Community.name == new_url.data.lower()).first()
        if existing_community:
            raise ValidationError(_l('A local community at that url already exists'))


class CmsPageForm(FlaskForm):
    url = StringField(_l('URL path'), validators=[DataRequired(), Length(max=100)], 
                      render_kw={'placeholder': _l('e.g., /about-us')})
    title = StringField(_l('Page title'), validators=[DataRequired(), Length(max=255)])
    body = TextAreaField(_l('Content (Markdown)'), validators=[DataRequired()], 
                        render_kw={'rows': 15, 'placeholder': _l('Write your content in Markdown format...')})
    submit = SubmitField(_l('Save'))

    def __init__(self, original_page=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_page = original_page

    def validate_url(self, url):
        if not url.data.startswith('/'):
            url.data = '/' + url.data
        
        # Check if another page already uses this URL (excluding the current page if editing)
        existing_page = CmsPage.query.filter_by(url=url.data).first()
        if existing_page and (not self.original_page or existing_page.id != self.original_page.id):
            raise ValidationError(_l('A page with this URL already exists.'))


class EmojiForm(FlaskForm):
    token = StringField(_l('Token or character'), validators=[DataRequired(), Length(max=20)],
                        render_kw={'placeholder': _l('e.g., :happy: or a single character like 👍')})
    url = StringField(_l('URL'), validators=[Optional(), Length(max=1024)],
                      render_kw={'placeholder': _l('e.g. https://...')})
    aliases = StringField(_l('Keywords'), validators=[Optional(), Length(max=100)])
    category = StringField(_l('Category'), validators=[DataRequired(), Length(max=20)])
    submit = SubmitField(_l('Save'))
