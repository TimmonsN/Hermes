import logging
from datetime import datetime, timedelta
from config import Config
import database as db

logger = logging.getLogger("hermes.scheduler")

def should_send_start_reminder(assignment: dict) -> tuple[bool, str]:
    """
    Determine if it's time to send a start reminder for an assignment.
    Returns (should_send, reason).
    """
    if assignment.get("notified_start"):
        return False, "already notified"

    if assignment.get("status") in ("started", "submitted", "complete"):
        return False, "already in progress or done"

    if not assignment.get("due_at"):
        return False, "no due date"

    now = datetime.now()

    try:
        due = datetime.fromisoformat(assignment["due_at"].replace("Z", "+00:00")).replace(tzinfo=None)
        days_until_due = (due - now).days
        hours_until_due = (due - now).total_seconds() / 3600
    except Exception:
        return False, "could not parse due date"

    # Always alert if due within 24 hours and not notified
    if hours_until_due <= 24:
        return True, "due within 24 hours"

    # If we have a calculated start_by date, check if we've passed it
    if assignment.get("start_by"):
        try:
            start_by = datetime.fromisoformat(assignment["start_by"])
            if now >= start_by:
                return True, f"past recommended start date ({start_by.strftime('%b %d')})"
        except Exception:
            pass

    # Fallback: use priority-based rules
    priority = assignment.get("priority", "medium")
    if priority == "critical" and days_until_due <= 5:
        return True, "critical priority item coming up"
    if priority == "high" and days_until_due <= 3:
        return True, "high priority item coming up"
    if priority == "medium" and days_until_due <= 2:
        return True, "medium priority item due soon"
    if priority == "low" and days_until_due <= 1:
        return True, "low priority item due tomorrow"

    return False, "not yet time"


def should_send_check_in(assignment: dict) -> bool:
    """Send a check-in if started but not yet submitted and due is approaching."""
    if assignment.get("status") != "started":
        return False
    if not assignment.get("due_at"):
        return False

    try:
        due = datetime.fromisoformat(assignment["due_at"].replace("Z", "+00:00")).replace(tzinfo=None)
        hours_until_due = (due - datetime.now()).total_seconds() / 3600
    except Exception:
        return False

    # Check in when about halfway to deadline
    last_check = db.get_last_notification_time(assignment["id"], "check_in")
    if last_check:
        try:
            last_dt = datetime.fromisoformat(str(last_check))
            if (datetime.now() - last_dt).total_seconds() < 3600 * 6:
                return False  # checked in within 6 hours
        except Exception:
            pass

    return hours_until_due <= 12


def is_within_active_hours() -> bool:
    """Check if current time is within allowed working hours."""
    hour = datetime.now().hour
    return Config.WAKE_HOUR <= hour <= Config.NO_WORK_AFTER_HOUR


def get_early_bonus_window(assignment: dict):
    """
    If an assignment has an early submission bonus, return the window to submit early.
    Returns (in_window, deadline_for_bonus).
    """
    if not assignment.get("has_early_bonus"):
        return False, None
    if not assignment.get("due_at"):
        return False, None

    try:
        due = datetime.fromisoformat(assignment["due_at"].replace("Z", "+00:00")).replace(tzinfo=None)
        # Default: early bonus if submitted 24h before due date
        early_deadline = due - timedelta(hours=24)
        now = datetime.now()

        if now < early_deadline:
            hours_left = (early_deadline - now).total_seconds() / 3600
            if hours_left <= 48:  # alert when within 48h of early deadline
                return True, early_deadline
    except Exception:
        pass

    return False, None
