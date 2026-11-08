from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from sqlalchemy import func
from wtforms import StringField, SubmitField, TextAreaField, BooleanField, SelectField, FileField
from wtforms.validators import DataRequired, Length, Optional

from app.blocklist.util import apply_blocklist_url_rules
from app.models import Community, User, Feed


class AddCopyBlocklistForm(FlaskForm):
    title = StringField(_l('Name'), validators=[DataRequired()])
    url = StringField(_l('Url'), validators=[Length(max=30)])
    description = TextAreaField(_l('Description'))
    members = TextAreaField(_l('Members'), validators=[DataRequired()], render_kw={'rows': 5})

    submit = SubmitField(_l('Save'))

    def validate(self, extra_validators=None):
        if not super().validate():
            return False
        if self.url.data.strip() == '':
            self.url.errors.append(_l('Url is required.'))
            return False
        else:
            if not apply_blocklist_url_rules(self):
                return False
            community = Community.query.filter(Community.name == self.url.data.strip().lower(),
                                               Community.ap_id == None).first()
            if community is not None:
                self.url.errors.append(_l('A community with this url already exists.'))
                return False
            feed = Feed.query.filter(Feed.name == self.url.data.strip().lower(),
                                               Feed.ap_id == None).first()
            if feed is not None:
                self.url.errors.append(_l('A feed with this url already exists.'))
                return False
            user = User.query.filter(func.lower(User.user_name) == func.lower(self.url.data.strip())).filter_by(
                ap_id=None).first()
            if user is not None:
                if user.deleted:
                    self.url.errors.append(_l('This name was used in the past and cannot be reused.'))
                else:
                    self.url.errors.append(_l('This name is in use already.'))
                return False

        input_members = self.members.data.strip().split('\n')
        for user_ap_id in input_members:
            if not user_ap_id.strip():
                continue

            if not '@' in user_ap_id:
                self.members.errors.append(_l('Please make sure each member is formatted as "community_name@instance.tld"'))
                return False
        return True


class EditBlocklistForm(FlaskForm):
    blocklist_id = 0
    title = StringField(_l('Name'), validators=[DataRequired()])
    url = StringField(_l('Url'), validators=[Length(max=50)])
    description = TextAreaField(_l('Description'))
    members = TextAreaField(_l('Members'), validators=[DataRequired()], render_kw={'rows': 5})
    submit = SubmitField(_l('Save'))

    def validate(self, extra_validators=None):
        if not super().validate():
            return False
        if self.url.data is not None:  # when editing a blocklist with members this field is disabled
            if self.url.data.strip() == '':
                self.url.errors.append(_l('This field is required.'))
                return False
            else:
                if not apply_blocklist_url_rules(self):
                    return False

        input_members = self.members.data.strip().split('\n')
        for member_ap_id in input_members:
            if not member_ap_id.strip():
                continue

            if not '@' in member_ap_id:
                self.members.errors.append(_l('Please make sure each member is formatted as "user_name@instance.tld"'))
                return False
        return True


class SearchRemoteBlocklist(FlaskForm):
    address = StringField(_l('Blocklist address'),
                          render_kw={'placeholder': 'e.g. https://server.name/b/blocklistname', 'autofocus': True},
                          validators=[DataRequired()])
    submit = SubmitField(_l('Search'))
