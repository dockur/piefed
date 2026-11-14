import re

from flask_babel import _, lazy_gettext as _l
from flask_wtf import FlaskForm
from sqlalchemy import func
from wtforms import StringField, PasswordField, SubmitField, HiddenField, BooleanField, RadioField, EmailField, \
    TextAreaField, SelectField
from wtforms.validators import ValidationError, DataRequired, Email, EqualTo, Length

from app.models import User, Community, Feed
from app.utils import MultiCheckboxField, CaptchaField, get_setting


class LoginForm(FlaskForm):
    user_name = StringField(_l('User name'), validators=[DataRequired()], render_kw={'autofocus': True,
                                                                                     'autocomplete': 'username',
                                                                                     'placeholder': _l('or email')})
    password = PasswordField(_l('Password'), validators=[DataRequired(), Length(min=8, max=129)],
                             render_kw={'title': _l('Minimum length 8, maximum 128')})
    low_bandwidth_mode = BooleanField(_l('Low bandwidth mode'))
    timezone = HiddenField(render_kw={'id': 'timezone'})
    submit = SubmitField(_l('Log In'))


class TotpLoginForm(FlaskForm):
    code = StringField(_l('Authentication code'), validators=[DataRequired(), Length(min=6, max=6)],
                       render_kw={'autofocus': True, 'autocomplete': 'one-time-code', 'inputmode': 'numeric',
                                  'placeholder': _l('6-digit code')})
    trust_device = BooleanField(_l('Trust this device'))
    submit = SubmitField(_l('Log In'))


class RegistrationForm(FlaskForm):
    user_name = StringField(_l('User name'), validators=[DataRequired(), Length(min=3, max=50)],
                            render_kw={'autofocus': True, 'autocomplete': 'username'})
    email = HiddenField(_l('Email'))
    real_email = EmailField(_l('Email'), validators=[DataRequired(), Email(), Length(min=5, max=255)], render_kw={'autocomplete': 'email'})
    password = PasswordField(_l('Password'), validators=[DataRequired(), Length(min=8, max=129)],
                             render_kw={'autocomplete': 'new-password', 'title': _l('Minimum length 8, maximum 128')})
    password2 = PasswordField(_l('Repeat password'), validators=[DataRequired(), EqualTo('password'), Length(min=8, max=129)],
                              render_kw={'autocomplete': 'new-password', 'title': _l('Minimum length 8, maximum 128')})
    question = TextAreaField(_l('Why would you like to join this site?'), validators=[DataRequired(), Length(min=1, max=512)])
    terms = BooleanField(_l('I agree to the terms of service & privacy policy (see links in footer)'), validators=[DataRequired()])
    captcha = CaptchaField(_l('Enter captcha code below'), validators=[DataRequired()])
    timezone = HiddenField(render_kw={'id': 'timezone'})

    submit = SubmitField(_l('Register'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not get_setting('captcha_enabled', True):
            delattr(self, 'captcha')

    def validate_real_email(self, email):
        user = User.query.filter(func.lower(User.email) == func.lower(email.data.strip())).first()
        if user is not None:
            raise ValidationError(_l('An account with this email address already exists.'))

    def validate_user_name(self, user_name):
        user_name.data = user_name.data.strip()
        if ' ' in user_name.data:
            raise ValidationError(_l('User names cannot contain spaces.'))
        if '@' in user_name.data:
            raise ValidationError(_l('User names cannot contain @.'))

        # Allow alphanumeric characters and underscores (a-z, A-Z, 0-9, _)
        if not re.match(r'^[a-zA-Z0-9_]+$', user_name.data):
            raise ValidationError(_l('User names can only contain letters, numbers, and underscores.'))

        user = User.query.filter(func.lower(User.user_name) == func.lower(user_name.data.strip())).filter_by(ap_id=None).first()
        if user is not None:
            if user.deleted:
                raise ValidationError(_l('This username was used in the past and cannot be reused.'))
            else:
                raise ValidationError(_l('An account with this user name already exists.'))

        community = Community.query.filter(func.lower(Community.name) == func.lower(user_name.data.strip()),
                                           Community.ap_id == None).first()
        if community is not None:
            raise ValidationError(_l('This name is in use already.'))

        feed = Feed.query.filter(Feed.name == user_name.data.lower(), Feed.ap_id == None).first()
        if feed is not None:
            raise ValidationError(_('This name is in use already.'))

    def validate_password(self, password):
        if not password.data:
            return
        password.data = password.data.strip()
        if password.data == 'password' or password.data == '12345678' or password.data == '1234567890':
            raise ValidationError(_l('This password is too common.'))

        if len(password.data) == 128:
            raise ValidationError(_l('Maximum password length is 128 characters.'))

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

    def filter_user_name(self, user_name):
        if isinstance(user_name, str):
            user_name = user_name.strip()

        return user_name


class ResetPasswordRequestForm(FlaskForm):
    email = EmailField(_l('Email'), validators=[DataRequired(), Email()], render_kw={'autofocus': True})
    submit = SubmitField(_l('Request password reset'))


class ResetPasswordForm(FlaskForm):
    password = PasswordField(_l('Password'), validators=[DataRequired()], render_kw={'autofocus': True})
    password2 = PasswordField(_l('Repeat password'), validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField(_l('Set password'))


class ResendEmailForm(FlaskForm):
    email = EmailField(_l('Email'), validators=[DataRequired(), Email()], render_kw={'autofocus': True})
    submit = SubmitField(_l('Resend verification email'))


class FilterSetupForm(FlaskForm):
    options = [(1, _l('Please make it stop')),
               (0, _l('A little is ok')),
               (-1, _l('Bring it on')),
               ]
    trump_musk_level = RadioField(_l('How tired of Trump and Musk news are you?'), choices=options, default=1, coerce=int,
                                  render_kw={'class': 'form-select'})
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
    ignore_reposters = SelectField(_l('Posts by reposters'), choices=hide_type_choices,
                                   default=3, coerce=int, render_kw={'class': 'form-select'})
    hide_nsfw = SelectField(_l('NSFW posts'), choices=hide_type_choices,
                            default=0, coerce=int, render_kw={'class': 'form-select'})
    hide_nsfl = SelectField(_l('NSFL posts'), choices=hide_type_choices,
                            default=1, coerce=int, render_kw={'class': 'form-select'})
    hide_gen_ai = SelectField(_l('AI-generated posts'), choices=ai_hide_type_choices,
                            default=1, coerce=int, render_kw={'class': 'form-select'})
    submit = SubmitField(_l('Save'))


class ChooseTopicsForm(FlaskForm):
    chosen_topics = MultiCheckboxField(_l('Choose some topics you are interested in'), coerce=int)
    submit = SubmitField(_l('Choose'))


class RegisterByMastodonForm(FlaskForm):
    email = EmailField(_l('Email'), validators=[DataRequired(), Email()], render_kw={'autofocus': True})
    submit = SubmitField(_l("Set email"))
