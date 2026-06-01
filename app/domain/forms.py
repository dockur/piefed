from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, RadioField


class PostWarningForm(FlaskForm):
    post_warning = StringField(_l('Warning on posts'))
    warning_type = RadioField(_l('Warning type'), choices=[
        (0, _l('Warning')),
        (1, _l('Helpful context')),
        (2, _l('Recommendation'))
    ], default=0)
    submit = SubmitField(_l('Save'))
