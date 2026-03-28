import sys
import os
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, jsonify

EASTERN = ZoneInfo("America/New_York")

# Make sure the parent hermes directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database as db
from modules.analyzer import generate_chat_response
from modules.scheduler_engine import should_send_start_reminder

app = Flask(__name__)
logger = logging.getLogger("hermes.web")


def _from_canvas_time(ts: str) -> datetime:
    """Convert a Canvas UTC timestamp to naive Eastern local time."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(EASTERN).replace(tzinfo=None)

def _fmt_due(due_at_str):
    if not due_at_str:
        return None, None, "due-ok"
    try:
        due = _from_canvas_time(due_at_str)
        now = datetime.now()
        days = (due - now).days
        hours = (due - now).total_seconds() / 3600

        if hours < 0:
            fmt = f"Overdue ({due.strftime('%b %d')})"
            css = "due-soon"
        elif hours < 24:
            fmt = f"Today {due.strftime('%I:%M%p').lstrip('0')}"
            css = "due-today"
        elif days <= 3:
            fmt = f"{due.strftime('%a %b %d %I:%M%p').lstrip('0')}"
            css = "due-soon"
        else:
            fmt = due.strftime("%a %b %d")
            css = "due-ok"
        return due, fmt, css
    except Exception:
        return None, due_at_str[:10], "due-ok"


def _fmt_start_by(start_by_str):
    if not start_by_str:
        return None
    try:
        dt = datetime.fromisoformat(start_by_str)
        now = datetime.now()
        if dt.date() == now.date():
            return "today"
        elif dt.date() == (now + __import__('datetime').timedelta(days=1)).date():
            return "tomorrow"
        return dt.strftime("%a %b %d")
    except Exception:
        return None


def _enrich_assignment(a: dict) -> dict:
    due_dt, due_fmt, due_class = _fmt_due(a.get("due_at"))
    a["due_fmt"] = due_fmt
    a["due_class"] = due_class
    a["due_dt"] = due_dt
    a["start_by_fmt"] = _fmt_start_by(a.get("start_by"))

    # Parse analysis_json for extra fields
    if a.get("analysis_json"):
        try:
            analysis = json.loads(a["analysis_json"])
            a.setdefault("assignment_type", analysis.get("assignment_type"))
            a.setdefault("study_suggestions", analysis.get("study_suggestions", []))
            a.setdefault("watch_outs", analysis.get("watch_outs", []))
        except Exception:
            pass

    return a


def _enrich_exam(e: dict) -> dict:
    _, date_fmt, _ = _fmt_due(e.get("start_at"))
    e["date_fmt"] = date_fmt

    if e.get("start_study_by"):
        e["start_study_fmt"] = _fmt_start_by(e["start_study_by"])
    else:
        e["start_study_fmt"] = None

    if e.get("analysis_json"):
        try:
            analysis = json.loads(e["analysis_json"])
            e["priority"] = analysis.get("priority", "high")
            e["daily_study_hours"] = analysis.get("daily_study_hours")
            e["study_tips"] = analysis.get("study_tips", [])
        except Exception:
            e["study_tips"] = []

    return e


@app.route("/")
def dashboard():
    now = datetime.now()
    hour = now.hour
    if hour < 12:
        greeting = "morning"
    elif hour < 17:
        greeting = "afternoon"
    else:
        greeting = "evening"

    assignments_raw = db.get_upcoming_assignments(days_ahead=60)
    assignments = [_enrich_assignment(a) for a in assignments_raw]
    exams_raw = db.get_upcoming_exams(days_ahead=60)
    exams = [_enrich_exam(e) for e in exams_raw]
    courses = db.get_courses()

    due_today, this_week, coming_up = [], [], []
    for a in assignments:
        due_dt = a.get("due_dt")
        if due_dt:
            days = (due_dt - now).days
            hours = (due_dt - now).total_seconds() / 3600
            if hours < 0:
                due_today.append(a)  # overdue, show at top
            elif hours <= 24:
                due_today.append(a)
            elif days <= 7:
                this_week.append(a)
            else:
                coming_up.append(a)

    upcoming_exams = [e for e in exams if e.get("start_at")]

    # Quick collision check from DB (no new API call)
    collision_alerts = []
    # Check if any high-priority items are within 2 days of each other
    high_items = [a for a in assignments if a.get("priority") in ("high", "critical") and a.get("due_dt")]
    for i, a in enumerate(high_items):
        for b in high_items[i+1:]:
            diff = abs((a["due_dt"] - b["due_dt"]).total_seconds()) / 3600
            if diff <= 48:
                collision_alerts.append({
                    "window": f"{a['due_dt'].strftime('%b %d')}–{b['due_dt'].strftime('%b %d')}",
                    "advice": f"{a['title']} and {b['title']} are both due within 48 hours — plan ahead."
                })
                break

    # Time totals
    today_hours = sum(a.get("estimated_hours") or 0 for a in due_today)
    week_hours = sum(a.get("estimated_hours") or 0 for a in this_week)

    return render_template("dashboard.html",
        greeting=greeting,
        today_str=now.strftime("%A, %B %d"),
        active_courses=len(courses),
        total_upcoming=len(assignments),
        due_today=due_today,
        this_week=this_week,
        coming_up=coming_up,
        upcoming_exams=upcoming_exams[:3],
        collision_alerts=collision_alerts,
        today_hours=round(today_hours, 1),
        week_hours=round(week_hours, 1),
    )


@app.route("/calendar")
def calendar_page():
    now = datetime.now()
    assignments_raw = db.get_upcoming_assignments(days_ahead=28)
    assignments = [_enrich_assignment(a) for a in assignments_raw]
    exams_raw = db.get_upcoming_exams(days_ahead=28)
    exams = [_enrich_exam(e) for e in exams_raw]

    weeks = []
    for week_num in range(4):
        week = []
        for day_offset in range(7):
            offset = week_num * 7 + day_offset
            day_date = (now + timedelta(days=offset)).date()
            if offset == 0:
                day_name = "Today"
            elif offset == 1:
                day_name = "Tomorrow"
            else:
                day_name = day_date.strftime("%A")

            day_assignments = [a for a in assignments if a.get("due_dt") and a["due_dt"].date() == day_date]
            day_exams = []
            for e in exams:
                if e.get("start_at"):
                    try:
                        if _from_canvas_time(e["start_at"]).date() == day_date:
                            day_exams.append(e)
                    except Exception:
                        pass

            total_hours = sum(a.get("estimated_hours") or 0 for a in day_assignments)
            week.append({
                "day_name": day_name,
                "date_str": day_date.strftime("%b %-d"),
                "assignments": day_assignments,
                "exams": day_exams,
                "total_hours": round(total_hours, 1),
                "is_today": offset == 0,
            })
        weeks.append(week)

    return render_template("calendar.html", weeks=weeks)


@app.route("/assignments/<assignment_id>")
def assignment_detail(assignment_id):
    a = db.get_assignment_by_id(assignment_id)
    if not a:
        return "Assignment not found", 404
    a = _enrich_assignment(dict(a))

    study_suggestions = []
    watch_outs = []
    reasoning = ""
    if a.get("analysis_json"):
        try:
            analysis = json.loads(a["analysis_json"])
            study_suggestions = analysis.get("study_suggestions", [])
            watch_outs = analysis.get("watch_outs", [])
            reasoning = analysis.get("reasoning", "")
        except Exception:
            pass

    return render_template(
        "assignment_detail.html",
        a=a,
        study_suggestions=study_suggestions,
        watch_outs=watch_outs,
        reasoning=reasoning,
    )


@app.route("/assignments")
def assignments_page():
    assignments_raw = db.get_upcoming_assignments(days_ahead=90)
    assignments = [_enrich_assignment(a) for a in assignments_raw]
    return render_template("assignments.html", assignments=assignments, total=len(assignments))


@app.route("/exams")
def exams_page():
    exams_raw = db.get_upcoming_exams(days_ahead=90)
    exams = [_enrich_exam(e) for e in exams_raw]
    return render_template("exams.html", exams=exams)


@app.route("/courses")
def courses_page():
    courses_raw = db.get_courses()
    enriched = []
    for c in courses_raw:
        course = dict(c)
        syllabi = db.get_syllabus(c["id"])
        course["has_syllabus"] = len(syllabi) > 0

        # Count assignments
        assignments = db.get_upcoming_assignments(days_ahead=90)
        course["assignment_count"] = sum(1 for a in assignments if a.get("course_id") == c["id"])

        # Get rules from syllabus
        course["early_bonus"] = None
        course["resubmit"] = None
        course["grading_weights"] = None
        for s in syllabi:
            if s.get("rules_json"):
                try:
                    rules = json.loads(s["rules_json"])
                    eb = rules.get("early_submission_bonus", {})
                    if eb.get("exists"):
                        course["early_bonus"] = eb.get("description", "")
                    rp = rules.get("resubmit_policy", {})
                    if rp.get("exists"):
                        course["resubmit"] = rp.get("description", "")
                    gw = rules.get("grading_weights", {})
                    if gw:
                        parts = [f"{k}: {v}" for k, v in gw.items() if v and v != "percentage"]
                        course["grading_weights"] = " | ".join(parts[:4])
                except Exception:
                    pass

        enriched.append(course)

    return render_template("courses.html", courses=enriched)


@app.route("/chat")
def chat_page():
    messages_raw = db.get_recent_messages(limit=50)
    messages = []
    for m in messages_raw:
        messages.append({
            "body": m["body"],
            "direction_class": "user" if m["direction"] == "inbound" else "hermes",
            "label": "You" if m["direction"] == "inbound" else "Hermes",
            "timestamp": m["timestamp"]
        })
    return render_template("chat.html", messages=messages)


@app.route("/chat/send", methods=["POST"])
def chat_send():
    data = request.get_json()
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "empty message"}), 400

    # Store inbound
    db.store_message("inbound", user_message)

    # Check for status updates
    assignments = db.get_upcoming_assignments(days_ahead=21)
    status_note = None
    body_lower = user_message.lower()
    status = None
    if any(w in body_lower for w in ["finished", "done", "submitted", "turned in", "complete"]):
        status = "submitted"
    elif any(w in body_lower for w in ["started", "working on", "beginning", "began"]):
        status = "started"

    if status:
        for a in assignments:
            title_words = set(a["title"].lower().split())
            body_words = set(body_lower.split())
            meaningful = {w for w in (title_words & body_words) if len(w) > 3}
            if meaningful:
                db.update_assignment_status(a["id"], status)
                status_note = f"Student indicated '{a['title']}' status changed to {status}"
                break

    context = {
        "assignments": assignments,
        "exams": db.get_upcoming_exams(days_ahead=30),
        "current_date": datetime.now().strftime("%Y-%m-%d %A"),
        "status_update_note": status_note
    }

    response = generate_chat_response(user_message, context)
    db.store_message("outbound", response)

    return jsonify({"response": response})


@app.route("/api/assignment/<assignment_id>/done", methods=["POST"])
def mark_done(assignment_id):
    db.update_assignment_status(assignment_id, "submitted")
    return jsonify({"status": "ok"})

@app.route("/api/assignment/<assignment_id>/difficulty", methods=["POST"])
def set_difficulty(assignment_id):
    data = request.get_json()
    difficulty = int(data.get("difficulty", 5))
    hours = float(data.get("hours", 0)) if data.get("hours") else None
    conn = db.get_conn()
    if hours:
        conn.execute("UPDATE assignments SET difficulty=?, estimated_hours=? WHERE id=?",
                     (difficulty, hours, assignment_id))
    else:
        conn.execute("UPDATE assignments SET difficulty=? WHERE id=?",
                     (difficulty, assignment_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/sync", methods=["POST"])
def trigger_sync():
    import threading
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    try:
        import hermes as h
        t = threading.Thread(target=h.sync_canvas, daemon=True)
        t.start()
        return jsonify({"status": "sync started"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/reanalyze", methods=["POST"])
def reanalyze_all():
    """Clear all analysis and re-run. Use when Gemini was misbehaving."""
    import sqlite3
    conn = db.get_conn()
    conn.execute("UPDATE assignments SET analysis_json=NULL, difficulty=NULL, estimated_hours=NULL, start_by=NULL, priority='medium'")
    conn.execute("UPDATE exam_events SET analysis_json=NULL, study_hours_estimated=NULL, start_study_by=NULL")
    conn.commit()
    conn.close()
    # Trigger sync in background to re-analyze
    import threading, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    try:
        import hermes as h
        t = threading.Thread(target=h.sync_canvas, daemon=True)
        t.start()
    except Exception:
        pass
    return jsonify({"status": "re-analysis started — refresh in a minute"})


def run(port=5000, debug=False, use_reloader=True):
    app.run(host="127.0.0.1", port=port, debug=debug, use_reloader=use_reloader)
