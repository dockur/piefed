from flask import Blueprint

bp = Blueprint('blocklist', __name__)

from app.blocklist import routes