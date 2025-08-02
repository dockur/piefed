from flask_wtf import FlaskForm
from wtforms import (
    IntegerField,
    SelectField,
    TextAreaField,
    BooleanField,
    SubmitField,
)
from wtforms.validators import DataRequired


class AdminReportActionForm(FlaskForm):
    report_id = IntegerField("Report ID", validators=[DataRequired()])
    action = SelectField(
        "Action",
        choices=[
            ("resolve", "Resolve"),
            ("dismiss", "Dismiss"),
            ("ban_user", "Ban User"),
            ("remove_content", "Remove Content"),
        ],
        validators=[DataRequired()],
    )
    reason = TextAreaField("Reason")
    notify_reporter = BooleanField("Notify Reporter", default=True)
    resolve_similar = BooleanField("Resolve Similar Reports", default=True)
    ban_duration = SelectField(
        "Ban Duration",
        choices=[
            ("0", "Permanent"),
            ("24", "1 day"),
            ("168", "1 week"),
            ("720", "1 month"),
            ("8760", "1 year"),
        ],
        default="0",
    )
    ban_scope = SelectField(
        "Ban Scope",
        choices=[("community", "Community"), ("site", "Site-wide")],
        default="community",
    )
    delete_content = BooleanField("Delete Content", default=True)
    submit = SubmitField("Submit")
