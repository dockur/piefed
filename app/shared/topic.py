from flask import flash
from flask_babel import _
from flask_login import current_user

from app import db
from app.constants import *
from app.models import Topic, NotificationSubscription
from app.utils import authorise_api_user, render_template


def subscribe_topic(topic_id: int, subscribe, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id
    topic = Topic.query.get(topic_id)

    if src == SRC_WEB:
        subscribe = False if topic.notify_new_posts(user_id) else True

    existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == topic.id,
                                                                  NotificationSubscription.user_id == user_id,
                                                                  NotificationSubscription.type == NOTIF_TOPIC).first()

    if subscribe == False:
        if existing_notification:
            db.session.delete(existing_notification)
            db.session.commit()
        else:
            msg = 'A subscription for this topic did not exist.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))
    
    else:
        if existing_notification:
            msg = 'A subscription for this topic already existed.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))
        else:
            new_notification = NotificationSubscription(name=topic.name, user_id=user_id, entity_id=topic.id,
                                                        type=NOTIF_TOPIC)
            db.session.add(new_notification)
            db.session.commit()
    
    if src == SRC_API:
        return user_id
    else:
        return render_template("topic/_notification_toggle.html", topic=topic)
