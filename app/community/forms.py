from flask import request, g
from flask_login import current_user
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, TextAreaField, BooleanField, HiddenField, SelectField, FileField, \
    DateField
from wtforms.validators import ValidationError, DataRequired, Email, EqualTo, Length, Regexp, Optional
from flask_babel import _, lazy_gettext as _l

from app import db
from app.models import Community, utcnow
from app.utils import domain_from_url, MultiCheckboxField
from PIL import Image, ImageOps
from io import BytesIO
import pytesseract


class AddCommunityForm(FlaskForm):
    community_name = StringField(_l('Name'), validators=[DataRequired()])
    url = StringField(_l('Url'))
    description = TextAreaField(_l('Description'))
    icon_file = FileField(_('Icon image'))
    banner_file = FileField(_('Banner image'))
    rules = TextAreaField(_l('Rules'))
    nsfw = BooleanField('NSFW')
    local_only = BooleanField('Local only')
    submit = SubmitField(_l('Create'))

    def validate(self, extra_validators=None):
        if not super().validate():
            return False
        if self.url.data.strip() == '':
            self.url.errors.append(_('Url is required.'))
            return False
        else:
            if '-' in self.url.data.strip():
                self.url.errors.append(_('- cannot be in Url. Use _ instead?'))
                return False
        return True


class EditCommunityForm(FlaskForm):
    title = StringField(_l('Title'), validators=[DataRequired()])
    description = TextAreaField(_l('Description'))
    icon_file = FileField(_('Icon image'))
    banner_file = FileField(_('Banner image'))
    rules = TextAreaField(_l('Rules'))
    nsfw = BooleanField(_l('Porn community'))
    local_only = BooleanField(_l('Only accept posts from current instance'))
    restricted_to_mods = BooleanField(_l('Only moderators can post'))
    new_mods_wanted = BooleanField(_l('New moderators wanted'))
    topic = SelectField(_l('Topic'), coerce=int, validators=[Optional()])
    layouts = [('', _l('List')),
               ('masonry', _l('Masonry')),
               ('masonry_wide', _l('Wide masonry'))]
    default_layout = SelectField(_l('Layout'), coerce=str, choices=layouts, validators=[Optional()])
    submit = SubmitField(_l('Save'))


class AddModeratorForm(FlaskForm):
    user_name = StringField(_l('User name'), validators=[DataRequired()])
    submit = SubmitField(_l('Add'))


class EscalateReportForm(FlaskForm):
    reason = StringField(_l('Amend the report description if necessary'), validators=[DataRequired()])
    submit = SubmitField(_l('Escalate report'))


class ResolveReportForm(FlaskForm):
    note = StringField(_l('Note for mod log'), validators=[Optional()])
    also_resolve_others = BooleanField(_l('Also resolve all other reports about the same thing.'), default=True)
    submit = SubmitField(_l('Resolve report'))


class SearchRemoteCommunity(FlaskForm):
    address = StringField(_l('Community address'), render_kw={'placeholder': 'e.g. !name@server', 'autofocus': True}, validators=[DataRequired()])
    submit = SubmitField(_l('Search'))


class BanUserCommunityForm(FlaskForm):
    reason = StringField(_l('Reason'), render_kw={'autofocus': True}, validators=[DataRequired()])
    ban_until = DateField(_l('Ban until'), validators=[Optional()])
    delete_posts = BooleanField(_l('Also delete all their posts'))
    delete_post_replies = BooleanField(_l('Also delete all their comments'))
    submit = SubmitField(_l('Ban'))


class CreateDiscussionForm(FlaskForm):
    communities = SelectField(_l('Community'), validators=[DataRequired()], coerce=int)
    discussion_title = StringField(_l('Title'), validators=[DataRequired(), Length(min=3, max=255)])
    discussion_body = TextAreaField(_l('Body'), validators=[Optional(), Length(min=3, max=5000)])
    sticky = BooleanField(_l('Sticky'))
    nsfw = BooleanField(_l('NSFW'))
    nsfl = BooleanField(_l('Gore/gross'))
    notify_author = BooleanField(_l('Notify about replies'))
    submit = SubmitField(_l('Save'))


class CreateLinkForm(FlaskForm):
    communities = SelectField(_l('Community'), validators=[DataRequired()], coerce=int)
    link_title = StringField(_l('Title'), validators=[DataRequired(), Length(min=3, max=255)])
    link_body = TextAreaField(_l('Body'), validators=[Optional(), Length(min=3, max=5000)])
    link_url = StringField(_l('URL'), validators=[DataRequired(), Regexp(r'^https?://', message='Submitted links need to start with "http://"" or "https://"')],
                           render_kw={'placeholder': 'https://...'})
    sticky = BooleanField(_l('Sticky'))
    nsfw = BooleanField(_l('NSFW'))
    nsfl = BooleanField(_l('Gore/gross'))
    notify_author = BooleanField(_l('Notify about replies'))
    submit = SubmitField(_l('Save'))

    def validate(self, extra_validators=None) -> bool:
        domain = domain_from_url(self.link_url.data, create=False)
        if domain and domain.banned:
            self.link_url.errors.append(_("Links to %(domain)s are not allowed.", domain=domain.name))
            return False
        return True


class CreateImageForm(FlaskForm):
    communities = SelectField(_l('Community'), validators=[DataRequired()], coerce=int)
    image_title = StringField(_l('Title'), validators=[DataRequired(), Length(min=3, max=255)])
    image_alt_text = StringField(_l('Alt text'), validators=[Optional(), Length(min=3, max=255)])
    image_body = TextAreaField(_l('Body'), validators=[Optional(), Length(min=3, max=5000)])
    image_file = FileField(_('Image'), validators=[DataRequired()])
    sticky = BooleanField(_l('Sticky'))
    nsfw = BooleanField(_l('NSFW'))
    nsfl = BooleanField(_l('Gore/gross'))
    notify_author = BooleanField(_l('Notify about replies'))
    submit = SubmitField(_l('Save'))

    def validate(self, extra_validators=None) -> bool:
        uploaded_file = request.files['image_file']
        if uploaded_file and uploaded_file.filename != '':
            Image.MAX_IMAGE_PIXELS = 89478485
            # Do not allow fascist meme content
            try:
                image_text = pytesseract.image_to_string(Image.open(BytesIO(uploaded_file.read())).convert('L'))
            except FileNotFoundError as e:
                image_text = ''
            if 'Anonymous' in image_text and (
                    'No.' in image_text or ' N0' in image_text):  # chan posts usually contain the text 'Anonymous' and ' No.12345'
                self.image_file.errors.append(
                    f"This image is an invalid file type.")  # deliberately misleading error message
                current_user.reputation -= 1
                db.session.commit()
                return False
        if self.communities:
            community = Community.query.get(self.communities.data)
            if community.is_local() and g.site.allow_local_image_posts is False:
                self.communities.errors.append(_('Images cannot be posted to local communities.'))

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


class DeleteCommunityForm(FlaskForm):
    submit = SubmitField(_l('Delete community'))
