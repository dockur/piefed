from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional

from app.blocklist.util import apply_blocklist_url_rules


class AddCopyBlocklistForm(FlaskForm):
    title = StringField(_l('Name'), validators=[DataRequired(), Length(max=256)])
    url = StringField(_l('Url'), validators=[DataRequired(), Length(max=50)])
    description = TextAreaField(_l('Description'))
    members = TextAreaField(_l('Accounts to block'), validators=[Optional()], render_kw={'rows': 10})
    submit = SubmitField(_l('Save'))

    def validate(self, extra_validators=None):
        if not super().validate(extra_validators):
            return False
        return apply_blocklist_url_rules(self.url)


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
