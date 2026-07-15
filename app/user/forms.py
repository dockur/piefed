from flask_login import current_user
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, PasswordField, BooleanField, EmailField, TextAreaField, FileField, \
    RadioField, DateField, SelectField, IntegerField, SelectMultipleField, HiddenField
from wtforms.validators import ValidationError, DataRequired, Email, EqualTo, Length, Optional
from flask_babel import _, lazy_gettext as _l

from app.utils import MultiCheckboxField, get_timezones


class ProfileForm(FlaskForm):
    title = StringField(_l('Display name'), validators=[Optional(), Length(max=255)])
    email = EmailField(_l('Email address'), validators=[Email(), DataRequired(), Length(min=5, max=255)])
    password = PasswordField(_l('Set new password'), validators=[Optional(), Length(min=8, max=129)],
                             render_kw={'autocomplete': 'new-password', 'title': _l('Minimum length 8, maximum 128')})
    about = TextAreaField(_l('Bio'), validators=[Optional(), Length(min=3, max=1000)], render_kw={'rows': 5, 'class': 'autoresize'})
    extra_label_1 = StringField(_l('Extra field 1 - label'), validators=[Optional(), Length(max=50)],
                                render_kw={"placeholder": _l('e.g. Pronouns')})
    extra_text_1 = StringField(_l('Extra field 1 - text'), validators=[Optional(), Length(max=256)],
                               render_kw={"placeholder": _l("he/him, she/her, they/them, etc")})
    extra_label_2 = StringField(_l('Extra field 2 - label'), validators=[Optional(), Length(max=50)],
                                render_kw={"placeholder": _l('What I care about')})
    extra_text_2 = StringField(_l('Extra field 2 - text'), validators=[Optional(), Length(max=256)],
                               render_kw={"placeholder": _l('Cats, Peace, Family')})
    extra_label_3 = StringField(_l('Extra field 3 - label'), validators=[Optional(), Length(max=50)],
                                render_kw={"placeholder": _l('People who influence me')})
    extra_text_3 = StringField(_l('Extra field 3 - text'), validators=[Optional(), Length(max=256)])
    extra_label_4 = StringField(_l('Extra field 4 - label'), validators=[Optional(), Length(max=50)],
                                render_kw={"placeholder": _l("Things I've changed my mind about")})
    extra_text_4 = StringField(_l('Extra field 4 - text'), validators=[Optional(), Length(max=256)])
    matrixuserid = StringField(_l('Matrix User ID'), validators=[Optional(), Length(max=255)],
                               render_kw={'autocomplete': 'off'})
    profile_file = FileField(_l('Avatar image'), render_kw={'accept': 'image/*'})
    banner_file = FileField(_l('Top banner image'), render_kw={'accept': 'image/*'})
    bot = BooleanField(_l('This profile is a bot'))
    timezone = SelectField(_l('Timezone'), validators=[DataRequired()], render_kw={'id': 'timezone', 'class': 'form-control tom-select'})
    submit = SubmitField(_l('Save profile'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timezone.choices = get_timezones()

    def validate_email(self, field):
        if current_user.another_account_using_email(field.data):
            raise ValidationError(_l('That email address is already in use by another account'))

    def validate_matrix_user_id(self, matrix_user_id):
        if not matrix_user_id.data.strip().startswith('@'):
            raise ValidationError(_l('Matrix user ids start with @'))

    def validate_title(self, field):
        if field.data.strip().lower() == '[deleted]':
            raise ValidationError(_l('Invalid display name'))


class SettingsForm(FlaskForm):
    interface_language = SelectField(_l('Interface language'), coerce=str, validators=[Optional()],
                                     render_kw={'class': 'form-select'})
    read_languages = MultiCheckboxField(_l('Content language'), coerce=int, validators=[Optional()],
                                        render_kw={'class':'form-multicheck-columns'})
    newsletter = BooleanField(_l('Subscribe to email newsletter'))
    email_unread = BooleanField(_l('Receive email about missed notifications'))
    ignore_bots = BooleanField(_l('Posts by bots'))
    nsfw = BooleanField(_l('NSFW posts'))
    nsfl = BooleanField(_l('NSFL posts'))
    reply_collapse_threshold = IntegerField(_l('Reply collapse threshold'), validators=[Optional()])
    reply_hide_threshold = IntegerField(_l('Reply hide threshold'), validators=[Optional()])
    page_length = IntegerField(_l('Posts per page'), validators=[Optional()])
    markdown_editor = BooleanField(_l('Use markdown editor GUI when writing'))
    low_bandwidth_mode = BooleanField(_l('Low bandwidth mode'))
    searchable = BooleanField(_l('Show profile in user list'))
    indexable = BooleanField(_l('My posts appear in search results'))
    hide_read_posts = BooleanField(_l('Do not display posts with which I have already interacted (opened/upvoted/downvoted)'))
    show_subscribed_communities = BooleanField(_l("Show communities I've joined on my profile"))
    manually_approves_followers = BooleanField(_l('Manually approve followers'))
    federate_votes = BooleanField(_l('Federate votes'))
    feed_auto_follow = BooleanField(_l('Enable Automatic Follow of Feed Communities.'), default=True)
    feed_auto_leave = BooleanField(_l('Enable Automatic Leave of Feed Communities.'), default=False)
    sorts = [('hot', _l('Hot')),
             ('top', _l('Top')),
             ('new', _l('New')),
             ('old', _l('Old')),
             ('active', _l('Active')),
             ('scaled', _l('Scaled')),
             ]
    default_sort = SelectField(_l('Default post sort'), choices=sorts, validators=[DataRequired()], coerce=str,
                               render_kw={'class': 'form-select'})
    comment_sorts = [('hot', _l('Hot')),
                     ('top', _l('Top')),
                     ('new', _l('New')),
                     ('old', _l('Old')),
                     ]
    default_comment_sort = SelectField(_l('Default comment sort'), choices=comment_sorts, validators=[DataRequired()], coerce=str,
                               render_kw={'class': 'form-select'})
    filters = [('subscribed', _l('Subscribed')),
               ('moderating', _l('Moderating')),
               ('local', _l('Local')),
               ('popular', _l('Popular')),
               ('all', _l('All')),
               ]
    default_filter = SelectField(_l('Default home filter'), choices=filters, validators=[DataRequired()], coerce=str,
                                 render_kw={'class': 'form-select'})
    theme = SelectField(_l('Theme'), coerce=str, render_kw={'class': 'form-select'})
    compact_levels = [
        ('', _l('Not compact - expand images')),
        ('compact-min', _l('More compact - only thumbnails for images')),
        ('compact-min compact-max', _l('Most compact - no images, only text')),  # this will apply both classes to the body tag
    ]
    compaction = SelectField(_l('Compact UI'), choices=compact_levels, coerce=str, render_kw={'class': 'form-select'})
    fonts = [('', _l('Theme default - fastest')),
             ('atkinson', _l('Atkinson Hyperlegible - low vision')),
             ('dyslexic', _l('OpenDyslexic')),
             ('inter', _l('Inter - pretty')),
             ('roboto', _l('Roboto - pretty')),
             ]
    font = SelectField(_l('Font'), choices=fonts, validators=[Optional()], coerce=str,
                       render_kw={'class': 'form-select'})
    allow_community_themes = BooleanField(_l('Allow community themes'))
    code_styles = ['autumn', 'borland', 'bw', 'colorful', 'default', 'emacs', 'friendly', 'fruity', 'manni', 'monokai',
                   'murphy', 'native', 'pastie', 'perldoc', 'tango', 'trac', 'vim', 'vs']
    code_style = SelectField(_l('Code syntax highlighting color scheme'), choices=code_styles, coerce=str,
                             render_kw={'class': 'form-select'})
    accept_from = [
        ('0', _l('None')),
        ('1', _l('This instance')),
        ('2', _l('Trusted instances')),
        ('3', _l('All instances')),
    ]
    accept_private_messages = SelectField(_l('Accept private messages from'), choices=accept_from, coerce=int, render_kw={'class': 'form-select'})
    max_hours_per_day = IntegerField(_l('Stop me from using PieFed after this many hours per day'), validators=[Optional()])
    max_hours_change_restriction = SelectField(_l('When changes to this limit will be possible'),
                                               choices=[
                                                  ('anytime', _l('Anytime')),
                                                  ('1day', _l('In 1 day')),
                                                  ('1month', _l('In 1 month')),
                                                  ('2months', _l('In 2 months'))
                                               ],
                                               coerce=str,
                                               render_kw={'class': 'form-select'})
    additional_css = TextAreaField(_l('Additional CSS'))
    submit = SubmitField(_l('Save settings'))


class ImportExportForm(FlaskForm):
    import_file = FileField(_l('Import community subscriptions and user blocks from Lemmy'))
    export_settings = SubmitField(_l('Export'))
    submit = SubmitField(_l('Import'))


class DeleteAccountForm(FlaskForm):
    submit = SubmitField(_l('Yes, delete my account'))


class UnsubAllForm(FlaskForm):
    submit = SubmitField(_l('Yes, unsubscribe from all communities I am not moderating'))


class BanUserForm(FlaskForm):
    reason = StringField(_l('Reason'), render_kw={'list': 'reasons'})
    ip_address = BooleanField(_l('Ban IP address'))
    purge = BooleanField(_l('Delete all content by this account'))
    flush = BooleanField(_l('Flush CDN cache when deleting (slow)'))
    submit = SubmitField(_l('Ban'), render_kw={'autofocus': True})


class ReportUserForm(FlaskForm):
    reason_choices = [('1', _l('Breaks community rules')),
                      ('7', _l('Spam')),
                      ('2', _l('Harassment')),
                      ('3', _l('Threatening violence')),
                      ('4', _l('Promoting hate / genocide')),
                      ('15', _l('Misinformation / disinformation')),
                      ('16', _l('Racism, sexism, transphobia')),
                      ('17', _l('Malicious reporting')),
                      ('6', _l('Sharing personal info - doxing')),
                      ('5', _l('Minor abuse or sexualization')),
                      ('8', _l('Non-consensual intimate media')),
                      ('9', _l('Prohibited transaction')), ('10', _l('Impersonation')),
                      ('11', _l('Copyright violation')), ('12', _l('Trademark violation')),
                      ('13', _l('Self-harm or suicide')),
                      ('14', _l('Other'))]
    reasons = MultiCheckboxField(_l('Reason'), choices=reason_choices)
    description = StringField(_l('More info'), validators=[Length(max=256)])
    report_remote = BooleanField('Also send report to originating instance')
    submit = SubmitField(_l('Report'))

    def reasons_to_string(self, reason_data) -> str:
        result = []
        for reason_id in reason_data:
            for choice in self.reason_choices:
                if choice[0] == reason_id:
                    result.append(str(choice[1]))
        return ', '.join(result)[:255]


class FilterForm(FlaskForm):
    community_keyword_filter = StringField(_l('Hide posts in communities with these words in their name'))
    hide_type_choices = [(0, _l('Show')),
                         (1, _l('Hide completely')),
                         (2, _l('Blur thumbnail')),
                         (3, _l('Make post semi-transparent'))]
    ai_hide_type_choices = [(0, _l('Show')),
                         (1, _l('Hide completely')),
                         (2, _l('Label as AI')),
                         (3, _l('Make post semi-transparent'))]
    ignore_bots = SelectField(_l('Posts by bots'), choices=hide_type_choices,
                              default=0, coerce=int, render_kw={'class': 'form-select'})
    hide_nsfw = SelectField(_l('NSFW posts'), choices=hide_type_choices,
                            default=1, coerce=int, render_kw={'class': 'form-select'})
    hide_nsfl = SelectField(_l('NSFL posts'), choices=hide_type_choices,
                            default=1, coerce=int, render_kw={'class': 'form-select'})
    hide_gen_ai = SelectField(_l('AI-generated posts'), choices=ai_hide_type_choices,
                            default=0, coerce=int, render_kw={'class': 'form-select'})
    reply_collapse_threshold = IntegerField(_l('Reply collapse threshold'))
    reply_hide_threshold = IntegerField(_l('Reply hide threshold'))
    hide_low_quality = BooleanField(_l('Hide posts in low quality communities'))
    submit = SubmitField(_l('Save settings'))


class KeywordFilterEditForm(FlaskForm):
    title = StringField(_l('Name'), validators=[DataRequired(), Length(min=3, max=50)])
    filter_home = BooleanField(_l('Home feed'), default=True)
    filter_posts = BooleanField(_l('Posts in communities'))
    filter_replies = BooleanField(_l('Comments on posts'))
    hide_type_choices = [(0, _l('Make semi-transparent')), (1, _l('Hide completely'))]
    hide_type = RadioField(_l('Action to take'), choices=hide_type_choices, default=1, coerce=int)
    keywords = TextAreaField(_l('Keywords that trigger this filter'),
                             render_kw={'placeholder': 'One keyword or phrase per line', 'rows': 3},
                             validators=[DataRequired(), Length(min=3, max=500)])
    expire_after = DateField(_l('Expire after'), validators=[Optional()])
    submit = SubmitField(_l('Save'))


class RemoteFollowForm(FlaskForm):
    instance_url = StringField(_l('Your remote instance:'), validators=[DataRequired(), Length(min=3, max=50)],
                               render_kw={'placeholder': 'e.g. mastodon.social'})
    type_choices = [
        ('mastodon', _l('Mastodon, Misskey, Akkoma, Iceshrimp and friends')),
        ('friendica', _l('Friendica')),
        ('hubzilla', _l('Hubzilla')),
        ('lemmy', _l('Lemmy')),
        ('pixelfed', _l('Pixelfed')),
    ]

    instance_type = SelectField(_l('Instance type'), choices=type_choices, render_kw={'class': 'form-select'})
    submit = SubmitField(_l('View profile on remote instance'))


class UserNoteForm(FlaskForm):
    note = StringField(_l('Note'), validators=[Optional(), Length(max=50)], render_kw={'class': 'hide_label'})
    apply_all = BooleanField(_l('Apply note to everyone with the same user name'))
    submit = SubmitField(_l('Save note'))


class DeleteFileForm(FlaskForm):
    referrer = HiddenField()
    submit = SubmitField(_l('Delete'), render_kw={'autofocus': True})


class UploadFileForm(FlaskForm):
    referrer = HiddenField()
    urls = TextAreaField(_l('URLs to files (one per line)'), validators=[Length(max=10000)])
    file1 = FileField(_l('File 1'), render_kw={'accept': 'image/*'})
    file2 = FileField(_l('File 2'), render_kw={'accept': 'image/*'})
    file3 = FileField(_l('File 3'), render_kw={'accept': 'image/*'})
    file4 = FileField(_l('File 4'), render_kw={'accept': 'image/*'})
    file5 = FileField(_l('File 5'), render_kw={'accept': 'image/*'})
    file6 = FileField(_l('File 6'), render_kw={'accept': 'image/*'})
    file7 = FileField(_l('File 7'), render_kw={'accept': 'image/*'})
    file8 = FileField(_l('File 8'), render_kw={'accept': 'image/*'})
    file9 = FileField(_l('File 9'), render_kw={'accept': 'image/*'})
    file10 = FileField(_l('File 10'), render_kw={'accept': 'image/*'})

    submit = SubmitField(_l('Upload'))


class BlockUserForm(FlaskForm):
    username = StringField(_l('Username or ActivityPub ID'), validators=[DataRequired(), Length(min=1, max=255)],
                          render_kw={'placeholder': _l('e.g. @user@example.com or user')})
    submit = SubmitField(_l('Block user'))


class BlockCommunityForm(FlaskForm):
    community_name = StringField(_l('Community name or ActivityPub ID'), validators=[DataRequired(), Length(min=1, max=255)],
                                render_kw={'placeholder': _l('e.g. !community@example.com or community')})
    submit = SubmitField(_l('Block community'))


class BlockDomainForm(FlaskForm):
    domain_name = StringField(_l('Domain name'), validators=[DataRequired(), Length(min=1, max=255)],
                             render_kw={'placeholder': _l('e.g. example.com')})
    submit = SubmitField(_l('Block domain'))


class BlockInstanceForm(FlaskForm):
    instance_domain = StringField(_l('Instance domain'), validators=[DataRequired(), Length(min=1, max=255)],
                                 render_kw={'placeholder': _l('e.g. lemmy.example.com')})
    submit = SubmitField(_l('Block instance'))
