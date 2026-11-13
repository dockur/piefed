from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, TextAreaField, BooleanField, HiddenField, SelectField, FileField
from wtforms.validators import ValidationError, DataRequired, Email, EqualTo, Length, Optional
from flask_babel import _, lazy_gettext as _l

class AddTestCommunities(FlaskForm):
    communities_submit = SubmitField(_l('Populate Communities'))


class AddTestTopics(FlaskForm):
    topics_submit = SubmitField(_l('Populate Topics'))


class DeleteTestCommunities(FlaskForm):
    delete_communities_submit = SubmitField(_l('Delete Communities'))


class DeleteTestTopics(FlaskForm):
    delete_topics_submit = SubmitField(_l('Delete Topics'))


class ActivityPubForm(FlaskForm):
    json = TextAreaField(_l('JSON'), validators=[DataRequired()], render_kw={'rows': 10})
    submit = SubmitField(_l('Submit'))


class SendActivityForm(FlaskForm):
    activity = TextAreaField(_l('Activity to send'), validators=[Length(max=10000), DataRequired()], render_kw={'placeholder': _l('Any JSON'), 'rows': 20})
    inbox = StringField(_l('Inbox'), validators=[DataRequired()])
    submit = SubmitField(_l('Send'))
