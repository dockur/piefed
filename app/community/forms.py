import re
from io import BytesIO

import pytesseract
from PIL import Image, UnidentifiedImageError
from flask import request, g
from flask_babel import _, lazy_gettext as _l
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import func
from wtforms import StringField, SubmitField, TextAreaField, BooleanField, HiddenField, SelectField, FileField, \
    DateField
from wtforms.fields import DateTimeLocalField
from wtforms.validators import ValidationError, DataRequired, Length, Regexp, Optional

from app import db
from app.constants import DOWNVOTE_ACCEPT_ALL, DOWNVOTE_ACCEPT_MEMBERS, DOWNVOTE_ACCEPT_INSTANCE, \
    DOWNVOTE_ACCEPT_TRUSTED
from app.models import Community, Site, utcnow, User, Feed
from app.utils import domain_from_url, MultiCheckboxField


class AddCommunityForm(FlaskForm):
    community_name = StringField(_l('Name'), validators=[DataRequired()])
    url = StringField(_l('Url'), validators=[Length(max=50)])
    description = TextAreaField(_l('Description'))
    posting_warning = StringField(_l('Posting warning'), validators=[Length(max=512)])
    icon_file = FileField(_l('Icon image'), render_kw={'accept': 'image/*'})
    banner_file = FileField(_l('Banner image'), render_kw={'accept': 'image/*'})
    nsfw = BooleanField('NSFW')
    local_only = BooleanField('Local only')
    languages = MultiCheckboxField(_l('Languages'), coerce=int, validators=[Optional()],
                                   render_kw={'class': 'form-multicheck-columns'})
    submit = SubmitField(_l('Create'))

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

            # Allow alphanumeric characters and underscores (a-z, A-Z, 0-9, _)
            if not re.match(r'^[a-zA-Z0-9_]+$', self.url.data):
                self.url.errors.append(_l('Community urls can only contain letters, numbers, and underscores.'))
                return False

            community = Community.query.filter(Community.name == self.url.data.strip().lower(),
                                               Community.ap_id == None).first()
            if community is not None:
                self.url.errors.append(_l('A community with this url already exists.'))
                return False
            user = User.query.filter(func.lower(User.user_name) == func.lower(self.url.data.strip())).filter_by(
                ap_id=None).first()
            if user is not None:
                if user.deleted:
                    self.url.errors.append(_l('This name was used in the past and cannot be reused.'))
                else:
                    self.url.errors.append(_l('This name is in use already.'))
                return False
            feed = Feed.query.filter(Feed.name == self.url.data.strip().lower(), Feed.ap_id == None).first()
            if feed is not None:
                self.url.errors.append(_('This name is in use already.'))
                return False
        return True


class EditCommunityForm(FlaskForm):
    title = StringField(_l('Title'), validators=[DataRequired()])
    description = TextAreaField(_l('Description'))
    posting_warning = StringField(_l('Posting warning'), validators=[Length(max=512)])
    icon_file = FileField(_l('Icon image'), render_kw={'accept': 'image/*'})
    banner_file = FileField(_l('Banner image'), render_kw={'accept': 'image/*'})
    nsfw = BooleanField(_l('NSFW community'))
    local_only = BooleanField(_l('Only accept posts from current instance'))
    restricted_to_mods = BooleanField(_l('Only moderators can post'))
    new_mods_wanted = BooleanField(_l('New moderators wanted'))
    downvote_accept_modes = [(DOWNVOTE_ACCEPT_ALL, _l('Everyone')),
                             (DOWNVOTE_ACCEPT_MEMBERS, _l('Community members')),
                             (DOWNVOTE_ACCEPT_INSTANCE, _l('This instance')),
                             (DOWNVOTE_ACCEPT_TRUSTED, _l('Trusted instances')),

                             ]
    downvote_accept_mode = SelectField(_l('Accept downvotes from'), coerce=int, choices=downvote_accept_modes,
                                       validators=[Optional()], render_kw={'class': 'form-select'})
    topic = SelectField(_l('Topic'), coerce=int, validators=[Optional()], render_kw={'class': 'form-select'})
    languages = MultiCheckboxField(_l('Languages'), coerce=int, validators=[Optional()],
                                   render_kw={'class': 'form-multicheck-columns'})
    layouts = [('', _l('List')),
               ('masonry', _l('Masonry')),
               ('masonry_wide', _l('Wide masonry'))]
    default_layout = SelectField(_l('Layout'), coerce=str, choices=layouts, validators=[Optional()],
                                 render_kw={'class': 'form-select'})
    submit = SubmitField(_l('Save'))


class EditCommunityWikiPageForm(FlaskForm):
    title = StringField(_l('Title'), validators=[DataRequired()])
    slug = StringField(_l('Slug'), validators=[DataRequired()])
    body = TextAreaField(_l('Body'), render_kw={'rows': '10'})
    edit_options = [(0, _l('Mods and admins')),
                    (1, _l('Trusted accounts')),
                    (2, _l('Community members')),
                    (3, _l('Any account'))
                    ]
    who_can_edit = SelectField(_l('Who can edit'), coerce=int, choices=edit_options, validators=[Optional()],
                               render_kw={'class': 'form-select'})
    submit = SubmitField(_l('Save'))


class AddModeratorForm(FlaskForm):
    user_name = StringField(_l('User name'), validators=[DataRequired()])
    submit = SubmitField(_l('Find'))


class EscalateReportForm(FlaskForm):
    reason = StringField(_l('Amend the report description if necessary'), validators=[DataRequired()])
    submit = SubmitField(_l('Escalate report'))


class ResolveReportForm(FlaskForm):
    note = StringField(_l('Note for mod log'), validators=[Optional()])
    also_resolve_others = BooleanField(_l('Also resolve all other reports about the same thing.'), default=True)
    submit = SubmitField(_l('Resolve report'))


class SearchRemoteCommunity(FlaskForm):
    address = StringField(_l('Community address'),
                          render_kw={'placeholder': 'e.g. !name@server', 'autofocus': True, 'autocomplete': 'off'},
                          validators=[DataRequired()])
    submit = SubmitField(_l('Search'))


class BanUserCommunityForm(FlaskForm):
    reason = StringField(_l('Reason'), render_kw={'autofocus': True}, validators=[DataRequired()])
    ban_until = DateField(_l('Ban until'), validators=[Optional()])
    delete_posts = BooleanField(_l('Also delete all their posts'))
    delete_post_replies = BooleanField(_l('Also delete all their comments'))
    submit = SubmitField(_l('Ban'))


class FindAndBanUserCommunityForm(FlaskForm):
    user_name = StringField(_l('User name'), validators=[DataRequired()])
    submit = SubmitField(_l('Find'))


class CreatePostForm(FlaskForm):
    communities = SelectField(_l('Community'), validators=[DataRequired()], coerce=int,
                              render_kw={'class': 'form-select',
                                         'hx-get': '/community/community_changed',
                                         'hx-params': '*',
                                         'hx-target': '#communityFlair'})
    title = StringField(_l('Title'), validators=[DataRequired(), Length(min=3, max=255)])
    body = TextAreaField(_l('Body'), validators=[Optional(), Length(min=3, max=50000)], render_kw={'rows': 5})
    tags = StringField(_l('Tags'), validators=[Optional(), Length(min=2, max=5000)])
    flair = MultiCheckboxField(_l('Flair'), coerce=int, validators=[Optional()],
                               render_kw={'class': 'form-multicheck-columns'})
    sticky = BooleanField(_l('Sticky'))
    nsfw = BooleanField(_l('NSFW'))
    nsfl = BooleanField(_l('Gore/gross'))
    notify_author = BooleanField(_l('Notify about replies'))
    language_id = SelectField(_l('Language'), validators=[DataRequired()], coerce=int,
                              render_kw={'class': 'form-select'})
    scheduled_for = DateTimeLocalField(_l('Publish at'), validators=[Optional()], format="%Y-%m-%dT%H:%M")
    repeat = SelectField(_l('Repeat'), validators=[Optional()],
                         choices=[('none', _l('None')), ('once', _l('Only once')), ('daily', _l('Daily')),
                                  ('weekly', _l('Weekly')), ('monthly', _l('Monthly'))],
                         render_kw={'class': 'form-select'})
    timezone = HiddenField(render_kw={'id': 'timezone'})
    submit = SubmitField(_l('Publish'))

    def validate_nsfw(self, field):
        if g.site.enable_nsfw is False:
            if field.data:
                self.nsfw.errors.append(_l('NSFW posts are not allowed.'))
                return False
        return True

    def validate_nsfl(self, field):
        if g.site.enable_nsfl is False:
            if field.data:
                self.nsfl.errors.append(_l('NSFL posts are not allowed.'))
                return False
        return True

    def validate_scheduled_for(self, field):
        if field.data and field.data < utcnow():
            self.scheduled_for.errors.append(_l('Choose a time in the future.'))
            return False
        return True

    def validate_repeat(self, field):
        if self.scheduled_for.data and field.data == 'none':
            self.repeat.errors.append(_l('A scheduled post must have a frequency set.'))
            return False
        return True


class CreateDiscussionForm(CreatePostForm):
    pass


class CreateLinkForm(CreatePostForm):
    link_url = StringField(_l('URL'), validators=[DataRequired(), Regexp(r'^https?://', message='Submitted links need to start with "http://"" or "https://"')],
                           render_kw={'placeholder': 'https://...',
                                      'hx-get': '/community/check_url_already_posted',
                                      'hx-params': '*',
                                      'hx-target': '#urlUsed'})

    def validate_link_url(self, field):
        if 'blogspot.com' in field.data:
            self.link_url.errors.append(_l("Links to %(domain)s are not allowed.", domain='blogspot.com'))
            return False
        domain = domain_from_url(field.data, create=False)
        if domain and domain.banned:
            self.link_url.errors.append(_l("Links to %(domain)s are not allowed.", domain=domain.name))
            return False
        return True


class CreateVideoForm(CreatePostForm):
    video_url = StringField(_l('URL'), validators=[DataRequired(), Regexp(r'^https?://', message='Submitted links need to start with "http://"" or "https://"')],
                            render_kw={'placeholder': 'https://...'})

    def validate(self, extra_validators=None) -> bool:
        super().validate(extra_validators)

        domain = domain_from_url(self.video_url.data, create=False)
        if domain and domain.banned:
            self.video_url.errors.append(_l("Videos from %(domain)s are not allowed.", domain=domain.name))
            return False
        return True


class CreateImageForm(CreatePostForm):
    image_alt_text = StringField(_l('Alt text'), validators=[Optional(), Length(min=3, max=1500)])
    image_file = FileField(_l('Image'), validators=[DataRequired()], render_kw={'accept': 'image/*'})

    def validate(self, extra_validators=None) -> bool:
        super().validate(extra_validators)

        uploaded_file = request.files['image_file']
        if uploaded_file and uploaded_file.filename != '' and not uploaded_file.filename.endswith(
                '.svg') and not uploaded_file.filename.endswith('.gif'):
            Image.MAX_IMAGE_PIXELS = 89478485

            site = Site.query.get(1)
            if site is None:
                site = Site()

            if site.enable_chan_image_filter:
                # Do not allow fascist meme content
                try:
                    if '.avif' in uploaded_file.filename:
                        import pillow_avif  # NOQA
                    image_text = pytesseract.image_to_string(Image.open(BytesIO(uploaded_file.read())).convert('L'))
                except FileNotFoundError:
                    image_text = ''
                except UnidentifiedImageError:
                    image_text = ''

                if 'Anonymous' in image_text and (
                        'No.' in image_text or ' N0' in image_text):  # chan posts usually contain the text 'Anonymous' and ' No.12345'
                    self.image_file.errors.append(
                        "This image is an invalid file type.")  # deliberately misleading error message
                    current_user.reputation -= 1
                    db.session.commit()
                    return False
        if uploaded_file.filename.endswith('.gif'):
            max_size_in_mb = 10 * 1024 * 1024  # 10 MB
            if len(uploaded_file.read()) > max_size_in_mb:
                error_message = "This image filesize is too large."
                if not isinstance(self.image_file.errors, list):
                    self.image_file.errors = [error_message]
                else:
                    self.image_file.errors.append(error_message)
                return False
            uploaded_file.seek(0)
        if self.communities:
            community = Community.query.get(self.communities.data)
            if community.is_local() and g.site.allow_local_image_posts is False:
                self.communities.errors.append(_l('Images cannot be posted to local communities.'))

        return True


class EditImageForm(CreateImageForm):
    image_file = FileField(_l('Replace Image'), validators=[DataRequired()], render_kw={'accept': 'image/*'})
    image_file = FileField(_l('Image'), validators=[Optional()], render_kw={'accept': 'image/*'})

    def validate(self, extra_validators=None) -> bool:
        super().validate(extra_validators)

        if self.communities:
            community = Community.query.get(self.communities.data)
            if community.is_local() and g.site.allow_local_image_posts is False:
                self.communities.errors.append(_l('Images cannot be posted to local communities.'))

        return True


class CreatePollForm(CreatePostForm):
    mode = SelectField(_('Mode'), validators=[DataRequired()], choices=[('single', _l('Voters choose one option')),
                                                                        ('multiple', _l('Voters choose many options'))],
                       render_kw={'class': 'form-select'})
    finish_choices = [
        ('30m', _l('30 minutes')),
        ('1h', _l('1 hour')),
        ('6h', _l('6 hours')),
        ('12h', _l('12 hours')),
        ('1d', _l('1 day')),
        ('3d', _l('3 days')),
        ('7d', _l('7 days')),
    ]
    finish_in = SelectField(_('End voting in'), validators=[DataRequired()], choices=finish_choices,
                            render_kw={'class': 'form-select'})
    local_only = BooleanField(_l('Accept votes from this instance only'))
    choice_1 = StringField(
        'Choice')  # intentionally left out of internationalization (no _l()) as this label is not used
    choice_2 = StringField('Choice')
    choice_3 = StringField('Choice')
    choice_4 = StringField('Choice')
    choice_5 = StringField('Choice')
    choice_6 = StringField('Choice')
    choice_7 = StringField('Choice')
    choice_8 = StringField('Choice')
    choice_9 = StringField('Choice')
    choice_10 = StringField('Choice')

    def validate(self, extra_validators=None) -> bool:
        super().validate(extra_validators)

        # Polls shouldn't be scheduled more than once
        if self.repeat.data in ['daily', 'weekly', 'monthly']:
            self.repeat.errors.append(_l("Polls can't be scheduled more than once"))
            return False

        choices_made = 0
        for i in range(1, 10):
            choice_data = getattr(self, f"choice_{i}").data.strip()
            if choice_data != '':
                choices_made += 1
        if choices_made == 0:
            self.choice_1.errors.append(_l('Polls need options for people to choose from'))
            return False
        elif choices_made <= 1:
            self.choice_2.errors.append(_l('Provide at least two choices'))
            return False
        return True


class ReportCommunityForm(FlaskForm):
    reason_choices = [('1', _l('Breaks instance rules')),
                      ('2', _l('Abandoned by moderators')),
                      ('3', _l('Cult')),
                      ('4', _l('Scam')),
                      ('5', _l('Alt-right pipeline')),
                      ('6', _l('Hate / genocide')),
                      ('7', _l('Other')),
                      ]
    reasons = MultiCheckboxField(_l('Reason'), choices=reason_choices)
    description = StringField(_l('More info'))
    report_remote = BooleanField('Also send report to originating instance')
    submit = SubmitField(_l('Report'))

    def reasons_to_string(self, reason_data) -> str:
        result = []
        for reason_id in reason_data:
            for choice in self.reason_choices:
                if choice[0] == reason_id:
                    result.append(str(choice[1]))
        return ', '.join(result)


class SetMyFlairForm(FlaskForm):
    my_flair = StringField(_l('Flair'), validators=[Optional(), Length(min=0, max=50)])
    submit = SubmitField(_l('Save'))


class DeleteCommunityForm(FlaskForm):
    submit = SubmitField(_l('Delete community'))


class RetrieveRemotePost(FlaskForm):
    address = StringField(_l('Full URL'),
                          render_kw={'placeholder': 'e.g. https://lemmy.world/post/123', 'autofocus': True},
                          validators=[DataRequired()])
    submit = SubmitField(_l('Retrieve'))


class InviteCommunityForm(FlaskForm):
    to = TextAreaField(_l('To'), validators=[DataRequired()],
                       render_kw={'placeholder': _l('Email addresses or fediverse handles, one per line'),
                                  'autofocus': True})
    submit = SubmitField(_l('Invite'))

    def validate_to(self, field):
        if ',' in field.data:
            raise ValidationError(_l('Use new lines instead of commas.'))
        lines = field.data.split('\n')
        if len(lines) > 50:
            raise ValidationError(_l('Maximum of 50 at a time.'))


class MoveCommunityForm(FlaskForm):
    old_community_locked = BooleanField(_l('The old community is locked'), validators=[DataRequired()])
    post_link = StringField(_l('Move notification post in old community'), validators=[DataRequired()])
    submit = SubmitField(_l('Request move'))


class EditCommunityFlairForm(FlaskForm):
    flair = StringField(_l('Flair'), validators=[DataRequired()])
    text_color = StringField(_l('Text color'), render_kw={"type": "color"})
    background_color = StringField(_l('Background color'), render_kw={"type": "color"})
    blur_images = BooleanField(_l('Blur images and thumbnails for posts with this flair'))
    submit = SubmitField(_l('Save'))
