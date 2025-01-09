from flask_wtf import FlaskForm
from flask_wtf.file import FileRequired, FileAllowed
from sqlalchemy import func
from wtforms import StringField, PasswordField, SubmitField, EmailField, HiddenField, BooleanField, TextAreaField, \
    SelectField, FileField, IntegerField, FloatField
from wtforms.fields.choices import SelectMultipleField
from wtforms.validators import ValidationError, DataRequired, Email, EqualTo, Length, Optional
from flask_babel import _, lazy_gettext as _l

from app.models import Community, User


class SiteProfileForm(FlaskForm):
    name = StringField(_l('Site Name'))
    description = StringField(_l('Tagline'))
    icon = FileField(_l('Icon'), validators=[FileAllowed(['jpg', 'jpeg', 'png', 'webp', 'svg'], 'Images only!')],
                     render_kw={'accept': 'image/*'})
    sidebar = TextAreaField(_l('Sidebar'))
    about = TextAreaField(_l('About'))
    announcement = TextAreaField(_l('Announcement at top of home page'))
    legal_information = TextAreaField(_l('Legal information'))
    contact_email = EmailField(_l('General instance contact email address'), validators=[DataRequired(), Length(min=5, max=255)])
    submit = SubmitField(_l('Save'))


class SiteMiscForm(FlaskForm):
    enable_downvotes = BooleanField(_l('Enable downvotes'))
    allow_local_image_posts = BooleanField(_l('Allow local image posts'))
    remote_image_cache_days = IntegerField(_l('Days to cache images from remote instances for'), render_kw={'placeholder': _l('0 means cache forever')})
    enable_nsfw = BooleanField(_l('Allow NSFW communities'))
    enable_nsfl = BooleanField(_l('Allow NSFL communities and posts'))
    community_creation_admin_only = BooleanField(_l('Only admins can create new local communities'))
    reports_email_admins = BooleanField(_l('Notify admins about reports, not just moderators'))
    types = [('Open', _l('Open')), ('RequireApplication', _l('Require application')), ('Closed', _l('Closed'))]
    registration_mode = SelectField(_l('Registration mode'), choices=types, default=1, coerce=str, render_kw={'class': 'form-select'})
    application_question = TextAreaField(_l('Question to ask people applying for an account'))
    auto_decline_referrers = TextAreaField(_l('Block registrations from these referrers (one per line)'))
    default_theme = SelectField(_l('Default theme'), coerce=str, render_kw={'class': 'form-select'})
    log_activitypub_json = BooleanField(_l('Log ActivityPub JSON for debugging'))
    public_modlog = BooleanField(_l('Show moderation actions publicly'))
    show_inoculation_block = BooleanField(_l('Show Rational Discourse Toolkit in sidebar'))
    submit = SubmitField(_l('Save'))


class FederationForm(FlaskForm):
    use_allowlist = BooleanField(_l('Allowlist instead of blocklist'))
    allowlist = TextAreaField(_l('Allow federation with these instances'))
    use_blocklist = BooleanField(_l('Blocklist instead of allowlist'))
    blocklist = TextAreaField(_l('Deny federation with these instances'))
    defederation_subscription = TextAreaField(_l('Auto-defederate from any instance defederated by'))
    blocked_phrases = TextAreaField(_l('Discard all posts and comments with these phrases (one per line)'))
    blocked_actors = TextAreaField(_l('Discard all posts and comments by users with these words in their name (one per line)'))
    submit = SubmitField(_l('Save'))


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
    nsfw = BooleanField(_l('Porn community'))
    banned = BooleanField(_l('Banned - no new posts accepted'))
    local_only = BooleanField(_l('Only accept posts from current instance'))
    restricted_to_mods = BooleanField(_l('Only moderators can post'))
    new_mods_wanted = BooleanField(_l('New moderators wanted'))
    show_popular = BooleanField(_l('Posts can be popular'))
    show_all = BooleanField(_l('Posts show in All list'))
    low_quality = BooleanField(_l("Low quality / toxic - votes in here don't affect reputation"))
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
    submit = SubmitField(_l('Save'))

    def validate(self, extra_validators=None):
        if not super().validate():
            return False
        if self.url.data.strip() == '':
            self.url.errors.append(_l('Url is required.'))
            return False
        else:
            if '-' in self.url.data.strip():
                self.url.errors.append(_l('- cannot be in Url. Use _ instead?'))
                return False
        return True


class EditTopicForm(FlaskForm):
    name = StringField(_l('Name'), validators=[DataRequired()], render_kw={'title': _l('Human readable name for the topic.')})
    machine_name = StringField(_l('Slug'), validators=[DataRequired()], render_kw={'title': _l('A short and unique identifier that becomes part of the URL.')})
    parent_id = SelectField(_l('Parent topic'), coerce=int, validators=[Optional()], render_kw={'class': 'form-select'})
    show_posts_in_children = BooleanField(_l('Show posts from child topics'), validators=[Optional()])
    submit = SubmitField(_l('Save'))


class EditInstanceForm(FlaskForm):
    vote_weight = FloatField(_l('Vote weight'))
    dormant = BooleanField(_l('Dormant'))
    gone_forever = BooleanField(_l('Gone forever'))
    trusted = BooleanField(_l('Trusted'))
    posting_warning = TextAreaField(_l('Posting warning'))
    submit = SubmitField(_l('Save'))


class AddUserForm(FlaskForm):
    user_name = StringField(_l('User name'), validators=[DataRequired()],
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
    ignore_bots = SelectField(_l('Hide posts by bots'), choices=hide_type_choices,
                                 default=0, coerce=int, render_kw={'class': 'form-select'})
    hide_nsfw = SelectField(_l('Show NSFW posts'), choices=hide_type_choices, default=1,
                            coerce=int, render_kw={'class': 'form-select'})
    hide_nsfl = SelectField(_l('Show NSFL posts'), choices=hide_type_choices, default=1,
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
    verified = BooleanField(_l('Email address is verified'))
    banned = BooleanField(_l('Banned'))
    ban_posts = BooleanField(_l('Ban posts'))
    ban_comments = BooleanField(_l('Ban comments'))
    hide_type_choices = [(0, _l('Show')),
                         (1, _l('Hide completely')),
                         (2, _l('Blur')),
                         (3, _l('Make semi-transparent'))]
    hide_nsfw = SelectField(_l('Show NSFW posts'), choices=hide_type_choices, default=1,
                            coerce=int, render_kw={'class': 'form-select'})
    hide_nsfl = SelectField(_l('Show NSFL posts'), choices=hide_type_choices, default=1,
                            coerce=int, render_kw={'class': 'form-select'})
    role_options = [(2, _l('User')),
               (3, _l('Staff')),
               (4, _l('Admin')),
               ]
    role = SelectField(_l('Role'), choices=role_options, default=2, coerce=int, render_kw={'class': 'form-select'})
    remove_avatar = BooleanField(_l('Remove avatar'))
    remove_banner = BooleanField(_l('Remove banner'))
    submit = SubmitField(_l('Save'))


class SendNewsletterForm(FlaskForm):
    subject = StringField(_l('Subject'), validators=[DataRequired()])
    body_text = TextAreaField(_l('Body (text)'), render_kw={"rows": 10}, validators=[DataRequired()])
    body_html = TextAreaField(_l('Body (html)'), render_kw={"rows": 20}, validators=[DataRequired()])
    test = BooleanField(_l('Test mode'), render_kw={'checked': True})
    submit = SubmitField(_l('Send newsletter'))
