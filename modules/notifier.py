"""
Outbound SMS alerts only — one-way notifications to phone/Apple Watch.
Conversation happens in the web dashboard (http://localhost:5000/chat).
"""

import logging
from twilio.rest import Client
from config import Config
import database as db

logger = logging.getLogger("hermes.notifier")

_twilio = None

def _get_client():
    global _twilio
    if _twilio is None:
        _twilio = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
    return _twilio

def send(message: str, log_item_id=None, log_item_type="system", log_notif_type="general"):
    if not Config.sms_enabled():
        logger.debug(f"SMS disabled, skipping: {message[:60]}")
        return False
    try:
        client = _get_client()
        msg = client.messages.create(
            body=message,
            from_=Config.TWILIO_PHONE_NUMBER,
            to=Config.YOUR_PHONE_NUMBER
        )
        logger.info(f"SMS sent [{msg.sid}]: {message[:60]}")
        db.store_message("outbound", message, msg.sid)
        if log_item_id:
            db.log_notification(log_item_id, log_item_type, log_notif_type, message)
        return True
    except Exception as e:
        logger.error(f"SMS failed: {e}")
        return False

def send_digest(message: str):
    return send(message, log_notif_type="digest")

def send_urgent(message: str, item_id=None):
    return send(message, log_item_id=item_id, log_notif_type="urgent")

def send_start_reminder(assignment: dict):
    title = assignment.get("title", "assignment")
    course = assignment.get("course_name", "")
    due = assignment.get("due_at", "")[:10] if assignment.get("due_at") else "soon"
    hours = assignment.get("estimated_hours")
    early_bonus = assignment.get("has_early_bonus")
    can_resub = assignment.get("can_resubmit")

    msg = f"Start this: {course} - {title}\nDue: {due}"
    if hours:
        msg += f"\nEst. time: {hours}h"
    if early_bonus:
        msg += f"\nBonus points for early submission!"
    if can_resub:
        msg += f"\nSubmit early — you can resubmit before the deadline."
    msg += "\nDetails: http://localhost:5000"

    success = send(msg, log_item_id=assignment["id"], log_item_type="assignment",
                   log_notif_type="start_reminder")
    if success:
        db.mark_notified(assignment["id"], "notified_start")
    return success

def send_check_in(assignment: dict):
    title = assignment.get("title", "assignment")
    course = assignment.get("course_name", "")
    due = assignment.get("due_at", "")[:10] if assignment.get("due_at") else "soon"
    msg = f"Check in: {course} - {title} (due {due})\nUpdate your status: http://localhost:5000"
    return send(msg, log_item_id=assignment["id"], log_item_type="assignment", log_notif_type="check_in")

def send_collision_alert(collision: dict):
    window = collision.get("window", "upcoming")
    items = collision.get("items", [])
    advice = collision.get("advice", "plan ahead")
    severity = collision.get("severity", "high")
    msg = f"WORKLOAD ALERT [{severity.upper()}] — {window}\n"
    for item in items[:3]:
        msg += f"  {item}\n"
    msg += f"{advice}\nDetails: http://localhost:5000"
    return send_urgent(msg)
