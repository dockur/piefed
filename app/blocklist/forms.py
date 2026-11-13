from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from sqlalchemy import func
from wtforms import StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional

from app.blocklist.util import apply_blocklist_url_rules
from app.models import Community, Blocklist, User, Feed


class AddCopyBlocklistForm(FlaskForm):
    title = StringField(_l('Name'), validators=[DataRequired(), Length(max=256)])
    url = StringField(_l('Url'), validators=[DataRequired(), Length(max=50)])
    description = TextAreaField(_l('Description'))
    members = TextAreaField(_l('Accounts to block'), validators=[Optional()], render_kw={'rows': 10})
    submit = SubmitField(_l('Save'))

    def validate(self, extra_validators=None):
        if not super().validate(extra_validators):
            return False
        if not apply_blocklist_url_rules(self.url):
            return False
        community = Community.query.filter(Community.name == self.url.data.strip().lower(),
                                           Community.ap_id == None).first()
        if community is not None:
            self.url.errors.append(_l('A community with this url already exists.'))
            return False
        blocklist = Blocklist.query.filter(Blocklist.machine_name == self.url.data.strip().lower(),
                                           Blocklist.ap_id == None).first()
        if blocklist is not None:
            self.url.errors.append(_l('A blocklist with this url already exists.'))
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
            self.url.errors.append(_l('This name is in use already.'))
            return False
        return True


class EditBlocklistForm(FlaskForm):
    title = StringField(_l('Name'), validators=[DataRequired(), Length(max=256)])
    description = TextAreaField(_l('Description'))
    members = TextAreaField(_l('Accounts to block'), validators=[Optional()], render_kw={'rows': 10})
    submit = SubmitField(_l('Save'))


class SearchRemoteBlocklist(FlaskForm):
    address = StringField(_l('Blocklist address'),
                          render_kw={'placeholder': 'e.g. %blocklistname@server.name or https://server.name/b/blocklistname',
                                     'autofocus': True},
                          validators=[DataRequired()])
    submit = SubmitField(_l('Search'))
