import sys
import os
import json
import logging
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, jsonify, redirect, url_for

EASTERN = ZoneInfo("America/New_York")

# Make sure the parent hermes directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database as db
from modules.analyzer import generate_chat_response, _is_boilerplate_description
from modules.scheduler_engine import should_send_start_reminder

app = Flask(__name__)
logger = logging.getLogger("hermes.web")


@app.context_processor
def inject_globals():
    unread = db.get_unread_announcement_count()
    return dict(unread_announcements=unread)


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
        elif dt.date() == (now + timedelta(days=1)).date():
            return "tomorrow"
        return dt.strftime("%a %b %d")
    except Exception:
        return None


_late_policy_cache = {}  # course_id -> (accepts: bool|None, reason: str)

_NO_LATE_PHRASES = [
    "no late", "not accepted", "will not be accepted", "not be accepted",
    "0 credit", "zero credit", "no credit after", "no credit for late",
    "cannot be submitted late", "no makeup", "no make-up",
    "submissions will not", "will receive a 0", "receive a zero",
    "no extensions", "not eligible for late", "strictly no late",
    "late work will not", "late submissions will not", "no late work",
    "past due assignments", "not graded after", "not accepted after",
]
_YES_LATE_PHRASES = [
    "% per day", "percent per day", "per day late", "late penalty",
    "late submission", "accepted late", "accept late",
    "within 24", "within 48", "within 72", "within one week",
    "deduction per day", "deducted per day", "points off per day",
    "late work accepted", "late work will be accepted",
    "late with penalty", "submitted late for partial",
    "up to one week late", "up to 1 week", "by end of week",
    "through carmen", "by the end of the semester",
    "10% per", "20% per", "25% per", "50% per",
]

def _scan_text_for_late_policy(text: str):
    """Scan raw text for late policy signals. Returns (True/False/None, reason)."""
    t = text.lower()
    if any(p in t for p in _NO_LATE_PHRASES):
        return False, "Syllabus: late work not accepted."
    if any(p in t for p in _YES_LATE_PHRASES):
        return True, "Syllabus allows late submission (likely with a grade penalty)."
    return None, ""

def _late_policy_for_course(course_id: str):
    """Determine late submission policy for a course.

    Checks in order:
    1. The AI-summarized late_policy field from rules_json
    2. The raw syllabus content (full text search) if summary is inconclusive
    3. Course notes stored by Hermes

    Returns (True/False/None, reason):
        True  — late submissions accepted (may have penalty)
        False — late submissions not accepted
        None  — genuinely no information found in any source
    """
    if course_id in _late_policy_cache:
        return _late_policy_cache[course_id]

    syllabi = []
    rules = {}
    try:
        syllabi = db.get_syllabus(str(course_id))
        for s in syllabi:
            try:
                r = json.loads(s["rules_json"]) if s.get("rules_json") else {}
                rules.update(r)
            except Exception:
                pass
    except Exception:
        pass

    # Step 1: check the AI-summarized late_policy field
    policy_text = (rules.get("late_policy") or "").strip()
    is_vague = (not policy_text
                or policy_text.lower() in ("none", "n/a", "unknown", "not specified")
                or "not specified" in policy_text.lower()
                or len(policy_text) < 10)

    if not is_vague:
        result, reason = _scan_text_for_late_policy(policy_text)
        if result is not None:
            _late_policy_cache[course_id] = (result, reason)
            return result, reason

    # Step 2: scan raw syllabus content directly — the AI summary may have missed it
    for s in syllabi:
        raw_content = (s.get("content") or "").strip()
        if not raw_content:
            continue
        result, reason = _scan_text_for_late_policy(raw_content)
        if result is not None:
            _late_policy_cache[course_id] = (result, reason)
            return result, reason

    # Step 3: check course notes (Hermes's synthesized understanding of the course)
    try:
        course_notes = db.get_course_notes(str(course_id)) or ""
        if course_notes:
            result, reason = _scan_text_for_late_policy(course_notes)
            if result is not None:
                _late_policy_cache[course_id] = (result, reason)
                return result, reason
    except Exception:
        pass

    # No policy found in any source
    _late_policy_cache[course_id] = (None, "")
    return None, ""


def _assignment_still_submittable(a: dict, now: datetime) -> tuple:
    """Determine if an overdue assignment can still earn points.

    Returns (submittable: bool|None, reason: str).
    True  = confirmed open, submit now
    False = confirmed closed, skip
    None  = no information to go on — show with minimal note
    """
    # 1. Non-submission types are never submittable
    sub_types = (a.get("submission_types") or "").strip()
    if sub_types in ("none", "not_graded", "on_paper"):
        return False, ""

    # 2. Canvas lock_at is the hardest signal available
    lock_at = a.get("lock_at")
    if lock_at:
        try:
            lock_dt = _from_canvas_time(lock_at)
            if lock_dt < now:
                return False, ""
        except Exception:
            pass

    # 3. Exams can never be retaken
    title_lower = (a.get("title") or "").lower()
    if any(kw in title_lower for kw in ("midterm", "final exam", "exam", "quiz")):
        return False, ""

    # 4. Syllabus + raw content + course notes check
    cid = str(a.get("course_id", ""))
    accepts_late, policy_reason = _late_policy_for_course(cid)
    if accepts_late is False:
        return False, ""
    if accepts_late is True:
        return True, policy_reason

    # 5. >14 days overdue with no lock_at and no late policy = almost certainly closed
    due_at = a.get("due_at")
    if due_at:
        try:
            due_dt = _from_canvas_time(due_at)
            if (now - due_dt).days > 14:
                return False, ""
        except Exception:
            pass

    # Still here: lock_at not set, policy not found, recent enough to possibly be open
    # Show it but be honest that we couldn't confirm the policy
    return None, "Late policy not found in syllabus — submission window may still be open."


def _calc_priority_score(a: dict, now: datetime = None) -> float:
    """Calculate smarter priority score for sorting.
    priority_score = (urgency_weight * days_factor) + (impact_weight * grade_impact) + (effort_penalty * hours_factor)
    """
    if now is None:
        now = datetime.now()
    # days_factor: peaks at due date (1.0), drops to 0 at 14+ days out
    due_at = a.get("due_at")
    days_factor = 0.5
    if due_at:
        try:
            due_dt = _from_canvas_time(due_at)
            days_until = (due_dt - now).total_seconds() / 86400
            days_factor = max(0.0, 1.0 - days_until / 14.0)
        except Exception:
            pass

    # grade_impact: points_possible relative to typical (100pts) * group_weight
    pts = a.get("points_possible") or 10
    grade_impact = min(1.0, pts / 100.0)  # normalize to 0-1

    # hours_factor: more work = higher priority signal
    hours = a.get("estimated_hours") or 2.0
    hours_factor = min(1.0, hours / 10.0)

    urgency_weight = 0.5
    impact_weight = 0.35
    effort_weight = 0.15

    return (urgency_weight * days_factor) + (impact_weight * grade_impact) + (effort_weight * hours_factor)


def _enrich_assignment(a: dict) -> dict:
    due_dt, due_fmt, due_class = _fmt_due(a.get("due_at"))
    a["due_fmt"] = due_fmt
    a["due_class"] = due_class
    a["due_dt"] = due_dt
    a["start_by_fmt"] = _fmt_start_by(a.get("start_by"))
    a["priority_score"] = _calc_priority_score(a)

    # Parse analysis_json for extra fields
    if a.get("analysis_json"):
        try:
            analysis = json.loads(a["analysis_json"])
            a.setdefault("assignment_type", analysis.get("assignment_type"))
            a.setdefault("study_suggestions", analysis.get("study_suggestions", []))
            a.setdefault("watch_outs", analysis.get("watch_outs", []))
        except Exception:
            pass

    wl = _workload_label(a.get("estimated_hours"))
    a["workload_label"] = wl[0] if wl else None
    a["workload_color"] = wl[1] if wl else "var(--muted)"

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

    # Collision alerts: only flag genuinely brutal stacking.
    # Require 3+ high-priority items OR combined hours ≥ 8h within a 36-hour window.
    collision_alerts = []
    high_items = [a for a in assignments if a.get("priority") in ("high", "critical") and a.get("due_dt")]
    for i, a in enumerate(high_items):
        window_items = [a]
        window_hours = a.get("estimated_hours") or 2.0
        for b in high_items[i+1:]:
            diff = abs((a["due_dt"] - b["due_dt"]).total_seconds()) / 3600
            if diff <= 36:
                window_items.append(b)
                window_hours += b.get("estimated_hours") or 2.0
        # Only flag if it's a genuinely big crunch
        if len(window_items) >= 3 or (len(window_items) >= 2 and window_hours >= 8):
            titles = " + ".join(x["title"][:30] for x in window_items[:3])
            collision_alerts.append({
                "window": a["due_dt"].strftime("%b %d"),
                "advice": f"~{round(window_hours, 1)}h of work stacked around {a['due_dt'].strftime('%a %b %d')}: {titles}. Start early."
            })
            break  # only show one collision alert on dashboard

    # Time totals
    today_hours = sum(a.get("estimated_hours") or 0 for a in due_today)
    week_hours = sum(a.get("estimated_hours") or 0 for a in this_week)

    # --- Proactive Intelligence Warnings ---
    proactive_warnings = []

    # Warning: heavy workload in next 48h
    next_48h = [a for a in assignments if a.get("due_dt") and
                0 <= (a["due_dt"] - now).total_seconds() / 3600 <= 48]
    total_48h_hours = sum(a.get("estimated_hours") or 0 for a in next_48h)
    if total_48h_hours >= 4:
        hardest = max(next_48h, key=lambda x: x.get("difficulty") or 0, default=None)
        hardest_name = hardest["title"] if hardest else "your hardest assignment"
        proactive_warnings.append({
            "level": "danger",
            "icon": "fire",
            "message": f"You have ~{round(total_48h_hours, 1)}h of work due in the next 48h — start {hardest_name} NOW."
        })

    # Warning: exams in less than 5 days
    soon_exams = [e for e in exams if e.get("start_at")]
    for e in soon_exams:
        try:
            exam_dt = _from_canvas_time(e["start_at"])
            days_away = (exam_dt - now).days
            if 0 < days_away <= 5:
                proactive_warnings.append({
                    "level": "warning",
                    "icon": "exam",
                    "message": f"Exam in {days_away} day{'s' if days_away != 1 else ''}: {e['title']} ({e.get('course_name','')}) — you should already be studying."
                })
        except Exception:
            pass

    # Warning: 3+ assignments due same day
    from collections import defaultdict
    day_counts = defaultdict(list)
    for a in assignments:
        if a.get("due_dt"):
            day_counts[a["due_dt"].date()].append(a)
    for day_date, day_items in day_counts.items():
        if len(day_items) >= 3:
            days_away = (datetime(day_date.year, day_date.month, day_date.day) - now).days
            if 1 <= days_away <= 7:
                proactive_warnings.append({
                    "level": "warning",
                    "icon": "stack",
                    "message": f"{len(day_items)} assignments due on {day_date.strftime('%A %b %-d')} — plan your weekend around this."
                })

    # Warning: unstarted assignment due tomorrow
    for a in assignments:
        if a.get("due_dt") and a.get("status", "pending") == "pending":
            hours_away = (a["due_dt"] - now).total_seconds() / 3600
            if 0 < hours_away <= 30:
                proactive_warnings.append({
                    "level": "danger",
                    "icon": "clock",
                    "message": f"You haven't started '{a['title']}' and it's due in ~{int(hours_away)}h."
                })

    # Warning: early submission bonus expiring
    for a in assignments:
        if a.get("has_early_bonus") and a.get("due_dt"):
            hours_away = (a["due_dt"] - now).total_seconds() / 3600
            if 24 <= hours_away <= 36:
                proactive_warnings.append({
                    "level": "info",
                    "icon": "bonus",
                    "message": f"Early submission bonus window closing soon for '{a['title']}' — submit in the next ~{int(hours_away - 24)}h for bonus points."
                })

    # --- Quick Stats ---
    all_grades = db.get_all_grades()
    avg_grade = None
    if all_grades:
        valid = [g["grade_pct"] for g in all_grades if g.get("grade_pct") is not None]
        avg_grade = round(sum(valid) / len(valid), 1) if valid else None

    week_minutes = db.get_total_time_this_week()
    week_hours_logged = round(week_minutes / 60, 1) if week_minutes else 0
    completed_count = db.get_semester_completed_count()

    # --- Today's Focus: top 3 by priority score (descending) ---
    all_upcoming = sorted(
        [a for a in assignments if a.get("due_dt")],
        key=lambda x: x.get("priority_score", 0),
        reverse=True
    )
    top_priority = all_upcoming[:3]

    # --- Grade Snapshot ---
    grade_snapshot = []
    for c in courses:
        cid = c["id"]
        course_grades = [g for g in all_grades if str(g.get("course_id")) == str(cid)]
        canvas_grade = c.get("canvas_grade_pct")
        valid_pcts = [g["grade_pct"] for g in course_grades if g.get("grade_pct") is not None]
        current_avg = round(sum(valid_pcts) / len(valid_pcts), 1) if valid_pcts else None
        display_avg = canvas_grade if canvas_grade is not None else current_avg
        target = db.get_grade_goal(cid)
        grade_snapshot.append({
            "name": c.get("code") or c["name"][:12],
            "current_avg": display_avg,
            "letter": _letter_grade(display_avg),
            "on_track": (display_avg >= target) if display_avg is not None else None,
            "target": target,
        })

    # --- This Week at a Glance (next 7 days) ---
    week_glance = []
    max_h = 0
    for i in range(7):
        day_date = (now + timedelta(days=i)).date()
        day_assignments = [a for a in assignments if a.get("due_dt") and a["due_dt"].date() == day_date]
        hours = round(sum(a.get("estimated_hours") or 0 for a in day_assignments), 1)
        max_h = max(max_h, hours)
        label = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][day_date.weekday() % 7 if True else 0]
        label = day_date.strftime("%a")[:3]
        week_glance.append({
            "label": label,
            "date": day_date.strftime("%-d"),
            "hours": hours,
            "count": len(day_assignments),
            "is_today": i == 0,
            "max_h": 0,  # will be set below
        })
    for wd in week_glance:
        wd["max_h"] = max_h if max_h > 0 else 1

    # --- Upcoming exams with days_until ---
    for e in upcoming_exams:
        if e.get("start_at"):
            try:
                exam_dt = _from_canvas_time(e["start_at"])
                e["days_until"] = max(0, (exam_dt.date() - now.date()).days)
            except Exception:
                e["days_until"] = 0
        else:
            e["days_until"] = 0

    week_synthesis = db.get_pref("week_synthesis") or ""

    # --- Tonight's Study Session Plan ---
    TONIGHT_HOURS = 3.0
    session_plan = []
    remaining_hrs = TONIGHT_HOURS
    pending_for_session = [
        a for a in assignments
        if a.get("status") not in ("submitted", "complete") and a.get("due_dt")
    ]
    pending_for_session.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    for a in pending_for_session[:5]:
        hrs = min(a.get("estimated_hours") or 1.0, remaining_hrs)
        if hrs < 0.25:
            break
        session_plan.append({**a, "session_hours": round(hrs, 1)})
        remaining_hrs -= hrs
        if remaining_hrs <= 0:
            break

    # --- Grade Recovery: resubmittable assignments that move the grade meaningfully ---
    grade_recovery = []
    try:
        resub = db.get_resubmittable_assignments()
        for r in resub[:3]:
            groups = db.get_assignment_groups(str(r["course_id"]))
            grp = next((g for g in groups if g["canvas_group_id"] == r.get("canvas_group_id")), None)
            if grp and grp["weight"] > 0:
                total_pts = db.get_group_total_points(str(r["course_id"]), r["canvas_group_id"])
                if total_pts > 0 and r.get("points_possible"):
                    current_pct = r.get("grade_pct", 0) or 0
                    potential_gain = ((100 - current_pct) / 100) * r["points_possible"]
                    grade_impact = round((potential_gain / total_pts) * grp["weight"], 2)
                    if grade_impact >= 0.3:
                        grade_recovery.append({**r, "grade_impact": grade_impact})
    except Exception:
        pass

    # --- Single best action: highest priority_score assignment not submitted ---
    top_action = None
    for a in sorted(assignments, key=lambda x: x.get("priority_score", 0), reverse=True):
        if a.get("status") not in ("submitted", "complete"):
            top_action = a
            break

    # Attach grade_impact_pct to top_action if available
    if top_action:
        try:
            if top_action.get("canvas_group_id") and top_action.get("points_possible") and top_action.get("course_id"):
                groups_for_top = db.get_assignment_groups(str(top_action["course_id"]))
                group_for_top = next(
                    (g for g in groups_for_top if g["canvas_group_id"] == top_action["canvas_group_id"]),
                    None
                )
                if group_for_top and group_for_top["weight"] > 0:
                    group_total = db.get_group_total_points(str(top_action["course_id"]), top_action["canvas_group_id"])
                    if group_total > 0:
                        top_action["grade_impact_pct"] = round(
                            (top_action["points_possible"] / group_total) * (group_for_top["weight"] / 100) * 100, 2
                        )
        except Exception:
            pass

    # --- Daily Digest: real-time actionable data ---
    today_date = now.date()
    tomorrow_date = (now + timedelta(days=1)).date()

    # Overdue assignments: Hermes determines if each can still earn points.
    # Uses Canvas lock_at, syllabus late policy, and assignment type heuristics.
    with db._connect() as _conn:
        overdue_rows = _conn.execute("""
            SELECT a.* FROM assignments a
            LEFT JOIN courses c ON c.id = a.course_id
            WHERE a.due_at < datetime('now')
              AND a.status NOT IN ('submitted', 'complete')
              AND (c.is_ignored IS NULL OR c.is_ignored = 0)
            ORDER BY a.due_at ASC
        """).fetchall()

    overdue_asgns = []
    for row in overdue_rows:
        r = dict(row)
        # Filter past-term courses: if term_name is stored and doesn't match current term
        cid = str(r.get("course_id", ""))
        course_row = db.get_course_by_id(cid) if cid else None
        if course_row:
            term = (course_row.get("term_name") or "").strip()
            if term and "2026" not in term:
                continue  # past term — skip entirely

        submittable, reason = _assignment_still_submittable(r, now)
        if submittable is False:
            continue  # Hermes has determined this cannot earn points

        enriched = _enrich_assignment(r)
        enriched["late_reason"] = reason  # explain to user what Hermes determined
        enriched["late_certain"] = submittable is True  # True=confirmed open, None=uncertain
        overdue_asgns.append(enriched)

    dd_due_today = [a for a in assignments if a.get("due_dt") and a["due_dt"].date() == today_date]
    dd_due_tomorrow = [a for a in assignments if a.get("due_dt") and a["due_dt"].date() == tomorrow_date]

    # Should start today: assignments where start_by date is today
    import math as _math
    dd_start_today = []
    for a in assignments:
        if not a.get("due_dt"):
            continue
        if a["due_dt"].date() == today_date:
            continue  # already in due_today
        est_hours = a.get("estimated_hours") or 2.0
        days_to_start = _math.ceil(est_hours / 3)
        ideal_start = a["due_dt"].date() - timedelta(days=days_to_start)
        if ideal_start <= today_date < a["due_dt"].date():
            dd_start_today.append(a)

    # Exam countdown (within 7 days)
    dd_exam_countdown = []
    for e in upcoming_exams:
        days_until = e.get("days_until", 99)
        if days_until is not None and days_until <= 7:
            dd_exam_countdown.append(e)

    # Grade warnings (courses below target)
    dd_grade_warnings = []
    for snap in grade_snapshot:
        if snap.get("on_track") is False and snap.get("current_avg") is not None:
            dd_grade_warnings.append(snap)

    daily_digest = {
        "due_today": dd_due_today,
        "due_tomorrow": dd_due_tomorrow,
        "overdue": overdue_asgns,
        "should_start_today": dd_start_today,
        "exam_countdown": dd_exam_countdown,
        "grade_warnings": dd_grade_warnings,
    }

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
        proactive_warnings=proactive_warnings,
        avg_grade=avg_grade,
        week_hours_logged=week_hours_logged,
        completed_count=completed_count,
        top_priority=top_priority,
        grade_snapshot=grade_snapshot,
        week_glance=week_glance,
        week_synthesis=week_synthesis,
        daily_digest=daily_digest,
        top_action=top_action,
        grade_recovery=grade_recovery,
        session_plan=session_plan,
    )


@app.route("/calendar")
def calendar_page():
    now = datetime.now()
    assignments_raw = db.get_upcoming_assignments(days_ahead=28)
    assignments = [_enrich_assignment(a) for a in assignments_raw]
    exams_raw = db.get_upcoming_exams(days_ahead=28)
    exams = [_enrich_exam(e) for e in exams_raw]

    today = now.date()

    def _exam_date(e):
        try:
            return _from_canvas_time(e["start_at"]).date() if e.get("start_at") else None
        except Exception:
            return None

    def _heatmap_pressure(target_date):
        """Returns (intensity_score, contributors_list).
        Score = pressure from upcoming assignments + exams within the next several days.
        Contributors = items causing pressure that aren't due on target_date itself.
        """
        score = 0.0
        contributors = []

        for a in assignments:
            if not a.get("due_dt"):
                continue
            due_date = a["due_dt"].date()
            if due_date < target_date:
                continue
            days_until = (due_date - target_date).days
            if days_until == 0:
                # Items due today contribute directly (shown as cells)
                hours = a.get("estimated_hours") or 1.5
                score += hours * 2.0  # due-today items count double
                continue
            if days_until <= 5:
                hours = a.get("estimated_hours") or 1.5
                priority_mult = 1.5 if a.get("priority") in ("high", "critical") else 1.0
                score += (hours / days_until) * priority_mult
                # Only show as contributor if it's high-stakes and worth starting early
                if days_until <= 3 and a.get("priority") in ("high", "critical"):
                    contributors.append({
                        "title": a["title"], "days_until": days_until, "type": "assignment"
                    })

        for e in exams:
            ed = _exam_date(e)
            if not ed or ed < target_date:
                continue
            days_until = (ed - target_date).days
            if days_until == 0:
                score += 4.0
                continue
            if days_until <= 7:
                score += 3.0 / max(days_until, 1)
                if days_until <= 5:
                    contributors.append({
                        "title": e["title"], "days_until": days_until, "type": "exam"
                    })

        # Deduplicate and cap contributors
        seen = set()
        unique = []
        for c in contributors:
            if c["title"] not in seen:
                seen.add(c["title"])
                unique.append(c)

        return round(score, 2), unique[:3]

    def _heatmap_level(intensity):
        if intensity <= 0:
            return "heat-none", 4
        elif intensity < 2.0:
            return "heat-light", 6
        elif intensity < 5.0:
            return "heat-moderate", 10
        elif intensity < 9.0:
            return "heat-heavy", 16
        else:
            return "heat-overwhelming", 22

    def _hours_heat_class(total_hours):
        """Color heat bar based on total estimated hours due on the day."""
        if total_hours == 0:
            return "heat-none", 3
        elif total_hours < 2:
            return "heat-light", 5
        elif total_hours < 4:
            return "heat-moderate", 9
        elif total_hours < 6:
            return "heat-heavy", 14
        else:
            return "heat-overwhelming", 20

    days_since_sunday = today.isoweekday() % 7
    week_start = today - timedelta(days=days_since_sunday)

    weeks = []
    for week_num in range(4):
        week = []
        week_total_hours = 0.0
        for day_offset in range(7):
            day_date = week_start + timedelta(weeks=week_num, days=day_offset)
            is_today = (day_date == today)
            is_past = (day_date < today)

            if is_today:
                day_name = "Today"
            elif day_date == today + timedelta(days=1):
                day_name = "Tomorrow"
            else:
                day_name = day_date.strftime("%A")

            day_assignments = [a for a in assignments if a.get("due_dt") and a["due_dt"].date() == day_date]
            day_exams = [e for e in exams if _exam_date(e) == day_date]

            total_hours = sum(a.get("estimated_hours") or 0 for a in day_assignments)
            week_total_hours += total_hours
            intensity, contributors = _heatmap_pressure(day_date)
            heat_class, heat_height = _hours_heat_class(total_hours)
            week.append({
                "day_name": day_name,
                "date_str": day_date.strftime("%b %-d"),
                "assignments": day_assignments,
                "exams": day_exams,
                "contributors": contributors,  # upcoming items causing pressure
                "total_hours": round(total_hours, 1),
                "is_today": is_today,
                "is_past": is_past,
                "heat_intensity": intensity,
                "heat_class": heat_class,
                "heat_height": heat_height,
            })
        weeks.append({"days": week, "week_total_hours": round(week_total_hours, 1)})

    week_synthesis = db.get_pref("week_synthesis") or ""
    return render_template("calendar.html", weeks=weeks, week_synthesis=week_synthesis)


_STOP_WORDS = {'the','a','an','and','or','for','to','in','of','with','on','at','by','from',
               'this','is','are','was','be','as','it','its','into','use','using','your'}

def _relevant_course_materials(assignment_title: str, syllabi: list) -> list:
    """Return course materials relevant to this assignment, with content snippets.

    Always includes the syllabus. Other files are scored by keyword overlap with
    the assignment title — only files with at least one matching keyword are shown.
    """
    import re as _re
    title_words = {w.lower() for w in _re.split(r'\W+', assignment_title)
                   if len(w) >= 3 and w.lower() not in _STOP_WORDS}

    non_syl = []  # (score, entry) for non-syllabus files
    syllabus_entries = []

    for s in syllabi:
        fname = s.get("file_name", "")
        if not fname:
            continue
        content = s.get("content") or ""
        snippet = content[:300].strip()

        if fname == "__canvas_syllabus_page__":
            syllabus_entries.append({"label": "Canvas Syllabus Page", "kind": "syllabus",
                                     "score": 0, "snippet": snippet, "chars": len(content)})
        elif fname.startswith("__page__"):
            page_id = fname[8:]
            text = (page_id + " " + content[:500]).lower()
            score = sum(1 for w in title_words if w in text)
            if score > 0:
                non_syl.append((score, {"label": page_id, "kind": "page",
                                        "score": score, "snippet": snippet, "chars": len(content)}))
        elif fname.startswith("__piazza__"):
            # Piazza posts: match subject against title words
            text = content[:500].lower()
            score = sum(1 for w in title_words if w in text)
            if score > 0:
                non_syl.append((score, {"label": f"Piazza: {content[:60].strip()}", "kind": "page",
                                        "score": score, "snippet": snippet, "chars": len(content)}))
        else:
            from modules.canvas_client import is_syllabus_file as _is_syl
            if _is_syl(fname):
                syllabus_entries.append({"label": fname, "kind": "syllabus",
                                         "score": 0, "snippet": snippet, "chars": len(content)})
            else:
                fname_lower = fname.lower()
                score = sum(1 for w in title_words if w in fname_lower)
                if score > 0:
                    non_syl.append((score, {"label": fname, "kind": "file",
                                            "score": score, "snippet": snippet, "chars": len(content)}))

    # Only show files whose score equals the maximum score found
    # (i.e. the best-matching files only — no partial matches when better ones exist)
    if non_syl:
        max_score = max(s for s, _ in non_syl)
        # Require score == max_score, capped at 5 files
        best = [e for s, e in sorted(non_syl, key=lambda x: -x[0]) if s == max_score][:5]
    else:
        best = []

    return syllabus_entries + best


@app.route("/assignments/<assignment_id>")
def assignment_detail(assignment_id):
    a = db.get_assignment_by_id(assignment_id)
    if not a:
        return "Assignment not found", 404
    a = _enrich_assignment(dict(a))

    study_suggestions = []
    watch_outs = []
    reasoning = ""
    time_breakdown = {}
    course_strategy_note = ""
    if a.get("analysis_json"):
        try:
            analysis = json.loads(a["analysis_json"])
            study_suggestions = analysis.get("study_suggestions", [])
            watch_outs = analysis.get("watch_outs", [])
            reasoning = analysis.get("reasoning", "")
            time_breakdown = analysis.get("time_breakdown", {})
            course_strategy_note = analysis.get("course_strategy_note", "")
        except Exception:
            pass

    # Notes & checklist
    note_obj = db.get_assignment_note(assignment_id)
    note_text = note_obj["note"] if note_obj else ""
    checklist = db.get_checklist(assignment_id)
    checklist_stats = db.get_checklist_stats(assignment_id)
    time_spent = db.get_time_spent(assignment_id)

    # Grade entry (read-only display)
    grade_obj = db.get_grade_for_assignment(assignment_id)

    # Ingested course materials — show only ones relevant to this assignment
    syllabi = db.get_syllabus(str(a.get("course_id", "")))
    course_materials = _relevant_course_materials(a.get("title", ""), syllabi)

    # Classify the description situation so the template can be honest
    # description_status: "real" | "from_materials" | "blind_guess"
    raw_desc = a.get("description", "")
    if not _is_boilerplate_description(raw_desc):
        description_status = "real"
    elif course_materials:
        description_status = "from_materials"
    else:
        description_status = "blind_guess"
    description_inferred = description_status != "real"  # keep backward compat for template

    # task_sections, course_weight_context, and study_strategy from analysis
    task_sections = []
    course_weight_context = ""
    study_strategy = ""
    if a.get("analysis_json"):
        try:
            analysis_extra = json.loads(a["analysis_json"])
            task_sections = analysis_extra.get("task_sections") or []
            course_weight_context = analysis_extra.get("course_weight_context") or ""
            study_strategy = analysis_extra.get("study_strategy") or ""
        except Exception:
            pass

    # Grade Impact: how much this single assignment is worth toward the final grade
    grade_impact_pct = None
    try:
        if a.get("canvas_group_id") and a.get("points_possible") and a.get("course_id"):
            groups_for_impact = db.get_assignment_groups(str(a["course_id"]))
            group_for_impact = next(
                (g for g in groups_for_impact if g["canvas_group_id"] == a["canvas_group_id"]),
                None
            )
            if group_for_impact and group_for_impact["weight"] > 0:
                group_total = db.get_group_total_points(str(a["course_id"]), a["canvas_group_id"])
                if group_total > 0:
                    grade_impact_pct = round(
                        (a["points_possible"] / group_total) * (group_for_impact["weight"] / 100) * 100,
                        2
                    )
    except Exception:
        pass

    # Find Canvas announcements mentioning this assignment title
    relevant_announcements = []
    try:
        all_announcements = db.get_announcements_for_course(str(a.get("course_id", "")), limit=50)
        title_words = set((a.get("title") or "").lower().split()) - {"the", "a", "an", "for", "and", "or", "in", "of", "to", "with"}
        for ann in all_announcements:
            ann_text = ((ann.get("title") or "") + " " + (ann.get("message") or "")).lower()
            if sum(1 for w in title_words if w in ann_text and len(w) > 3) >= 2:
                relevant_announcements.append(ann)
    except Exception:
        pass

    # Office hours from syllabus
    office_hours = ""
    try:
        import re as _re_oh
        syllabi_oh = db.get_syllabus(str(a.get("course_id", "")))
        for s in syllabi_oh:
            rules_oh = json.loads(s["rules_json"]) if s.get("rules_json") else {}
            oh = rules_oh.get("office_hours") or ""
            if oh and len(oh) > 5:
                office_hours = oh
                break
        if not office_hours:
            for s in syllabi_oh:
                content_oh = s.get("content", "") or ""
                match_oh = _re_oh.search(r'office hours?[:\s]+([^\n\.]{10,80})', content_oh, _re_oh.IGNORECASE)
                if match_oh:
                    office_hours = match_oh.group(1).strip()
                    break
    except Exception:
        pass

    # days_until for due date check
    days_until = None
    try:
        if a.get("due_at"):
            due_dt_check = _from_canvas_time(a["due_at"])
            days_until = (due_dt_check - datetime.now()).days
    except Exception:
        pass

    # Procrastination scenarios
    schedule_scenarios = None
    est_hours = a.get("estimated_hours")
    due_at_str = a.get("due_at")
    if est_hours and est_hours >= 1.0 and due_at_str and a.get("status") not in ("submitted", "complete"):
        try:
            due_dt = _from_canvas_time(due_at_str)
            now = datetime.now()
            days_left = max((due_dt - now).total_seconds() / 86400, 0)
            if 0.1 < days_left <= 14:
                # Scenario A: start tonight
                nights_available = max(int(days_left), 1)
                hrs_per_night_a = round(est_hours / nights_available, 1)
                # Scenario B: start the day before
                if days_left > 1.5:
                    hrs_one_night = round(est_hours, 1)
                    schedule_scenarios = {
                        "start_now": {"days": nights_available, "hrs_per_day": hrs_per_night_a},
                        "wait": {"hrs_total": hrs_one_night, "days_away": int(days_left)},
                        "feasible_now": hrs_per_night_a <= 4,
                        "feasible_wait": hrs_one_night <= 5,
                    }
        except Exception:
            pass

    return render_template(
        "assignment_detail.html",
        a=a,
        study_suggestions=study_suggestions,
        watch_outs=watch_outs,
        reasoning=reasoning,
        time_breakdown=time_breakdown,
        course_strategy_note=course_strategy_note,
        note_text=note_text,
        checklist=checklist,
        checklist_stats=checklist_stats,
        time_spent=time_spent,
        grade_obj=grade_obj,
        description_inferred=description_inferred,
        course_materials=course_materials,
        task_sections=task_sections,
        course_weight_context=course_weight_context,
        schedule_scenarios=schedule_scenarios,
        grade_impact_pct=grade_impact_pct,
        study_strategy=study_strategy,
        description_status=description_status,
        relevant_announcements=relevant_announcements,
        office_hours=office_hours,
        days_until=days_until,
    )


@app.route("/assignments")
def assignments_page():
    # Fetch all assignments (including submitted) for accurate completion stats
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT a.* FROM assignments a
            LEFT JOIN courses c ON c.id = a.course_id
            WHERE a.due_at IS NOT NULL
              AND (c.is_ignored IS NULL OR c.is_ignored = 0)
            ORDER BY a.due_at ASC
        """).fetchall()
    all_assignments_raw = [dict(r) for r in rows]
    all_enriched = [_enrich_assignment(a) for a in all_assignments_raw]

    # Only show upcoming (not submitted) by default
    now = datetime.now()
    assignments = [a for a in all_enriched if a.get("status") not in ("submitted", "complete")
                   and a.get("due_dt") and a["due_dt"] > now - timedelta(hours=1)]

    # Attach checklist stats for completion % display
    for a in assignments:
        stats = db.get_checklist_stats(a["id"])
        a["checklist_stats"] = stats

    courses = db.get_courses()
    course_names = sorted(set(a["course_name"] for a in all_enriched if a.get("course_name")))
    total_hours = round(sum(a.get("estimated_hours") or 0 for a in assignments), 1)

    # Completion stats per course (homework submitted count vs total)
    course_completion = {}
    for cname in course_names:
        course_asgns = [a for a in all_enriched if a.get("course_name") == cname]
        total = len(course_asgns)
        submitted = sum(1 for a in course_asgns if a.get("status") in ("submitted", "complete"))
        course_completion[cname] = {
            "total": total,
            "submitted": submitted,
            "pct": round(submitted / total * 100) if total > 0 else 0,
        }

    # Overdue and not submitted
    overdue = [a for a in all_enriched
               if a.get("status") not in ("submitted", "complete")
               and a.get("due_dt") and a["due_dt"] < now]

    return render_template("assignments.html",
                           assignments=assignments,
                           all_assignments=all_enriched,
                           overdue=overdue,
                           total=len(assignments),
                           courses=courses,
                           course_names=course_names,
                           total_hours=total_hours,
                           course_completion=course_completion)


@app.route("/exams")
def exams_page():
    exams_raw = db.get_upcoming_exams(days_ahead=90)
    exams = [_enrich_exam(e) for e in exams_raw]
    return render_template("exams.html", exams=exams)


@app.route("/exams/<exam_id>")
def exam_detail(exam_id):
    e = db.get_exam_by_id(exam_id)
    if not e:
        return "Exam not found", 404
    e = _enrich_exam(dict(e))

    study_tips = e.get("study_tips", [])
    reasoning = ""
    daily_study_plan = []
    if e.get("analysis_json"):
        try:
            analysis = json.loads(e["analysis_json"])
            reasoning = analysis.get("reasoning", "")
            study_tips = analysis.get("study_tips", study_tips)
            daily_study_plan = analysis.get("daily_study_plan", [])
        except Exception:
            pass

    course_id = str(e.get("course_id", ""))
    course_notes = db.get_course_notes(course_id)

    # Weight of this exam in the course
    exam_group_weight = None
    try:
        groups = db.get_assignment_groups(course_id)
        exam_keywords = ["exam", "midterm", "final"]
        for g in groups:
            gname_lower = g["name"].lower()
            if any(kw in gname_lower for kw in exam_keywords) and g["weight"] > 0:
                exam_group_weight = g["weight"]
                break
    except Exception:
        pass

    # Current category grade for this exam's group
    exam_category_current = None
    exam_category_name = None
    all_grades_for_exam = db.get_grades_for_course(course_id)
    target_grade = db.get_grade_goal(course_id)
    try:
        groups = db.get_assignment_groups(course_id)
        all_course_assignments = db.get_all_assignments_for_course(course_id)
        for g in groups:
            gname_lower = g["name"].lower()
            if any(kw in gname_lower for kw in ["exam", "midterm", "final"]) and g["weight"] > 0:
                exam_category_name = g["name"]
                group_asgn_ids = {a["id"] for a in all_course_assignments
                                  if a.get("canvas_group_id") == g["canvas_group_id"]}
                graded = [gr for gr in all_grades_for_exam
                          if gr.get("assignment_id") in group_asgn_ids and gr.get("grade_pct") is not None]
                if graded:
                    exam_category_current = round(sum(gr["grade_pct"] for gr in graded) / len(graded), 1)
                break
    except Exception:
        pass

    # Needed score on this exam to hit target
    needed_on_exam = None
    if exam_group_weight and exam_category_current is not None and target_grade:
        try:
            # Simplified: (target - current_category * (1 - exam_fraction)) / exam_fraction
            # We don't know the exact fraction this exam is of the category,
            # so just show needed vs target comparison
            needed_on_exam = None  # complex, skip for now
        except Exception:
            pass

    # Related graded assignments (what the exam likely covers)
    recent_graded = []
    try:
        all_course_asgns = db.get_all_assignments_for_course(course_id)
        graded_ids = {g["assignment_id"] for g in all_grades_for_exam}
        graded_assignments = [a for a in all_course_asgns if a["id"] in graded_ids and a.get("due_at")]
        graded_assignments.sort(key=lambda x: x["due_at"], reverse=True)
        recent_graded = graded_assignments[:5]
        # Attach grade info
        grade_lookup = {g["assignment_id"]: g for g in all_grades_for_exam}
        for ra in recent_graded:
            gr = grade_lookup.get(ra["id"])
            if gr:
                ra["grade_pct"] = gr.get("grade_pct")
                ra["points_earned"] = gr.get("points_earned")
    except Exception:
        pass

    # Exam countdown
    days_until_exam = None
    if e.get("start_at"):
        try:
            exam_dt = _from_canvas_time(e["start_at"])
            days_until_exam = max(0, (exam_dt.date() - date.today()).days)
        except Exception:
            pass

    # Pre-exam topic map: weak areas and ungraded (likely exam content)
    exam_topics = {}
    try:
        course_assignments_full = db.get_all_assignments_for_course(course_id)
        course_grades_full = db.get_grades_for_course(course_id)
        grade_map_full = {g["assignment_id"]: g for g in course_grades_full}

        weak = []
        for asgn in course_assignments_full:
            g = grade_map_full.get(asgn["id"])
            if g and g.get("grade_pct") is not None and g["grade_pct"] < 80:
                weak.append({"title": asgn["title"], "grade_pct": g["grade_pct"], "id": asgn["id"]})

        ungraded_upcoming = [
            asgn for asgn in course_assignments_full
            if asgn["id"] not in grade_map_full and asgn.get("due_at")
        ]

        exam_topics = {
            "weak_areas": sorted(weak, key=lambda x: x["grade_pct"])[:5],
            "upcoming": [{"title": asgn["title"], "id": asgn["id"]} for asgn in ungraded_upcoming if asgn.get("due_at")][:8],
        }
    except Exception:
        exam_topics = {}

    return render_template("exam_detail.html",
        e=e,
        reasoning=reasoning,
        study_tips=study_tips,
        course_notes=course_notes,
        daily_study_plan=daily_study_plan,
        exam_group_weight=exam_group_weight,
        exam_category_current=exam_category_current,
        exam_category_name=exam_category_name,
        target_grade=target_grade,
        recent_graded=recent_graded,
        days_until_exam=days_until_exam,
        exam_topics=exam_topics,
    )


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

        # Attach recent announcements for this course
        anns = db.get_announcements_for_course(str(c["id"]), limit=5)
        course["announcements"] = _enrich_announcements(list(anns))
        # Mark all as read when courses page is viewed
        for ann in course["announcements"]:
            db.mark_announcement_read(ann["canvas_id"])

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


@app.route("/chat/assignment/<assignment_id>")
def chat_assignment(assignment_id):
    a = db.get_assignment_by_id(assignment_id)
    if not a:
        return redirect("/chat")
    a = dict(a)
    a = _enrich_assignment(a)

    # Build Hermes's opening message with full context
    analysis = {}
    if a.get("analysis_json"):
        try:
            analysis = json.loads(a["analysis_json"])
        except Exception:
            pass

    syllabi = db.get_syllabus(str(a.get("course_id", "")))
    course_notes = ""
    for s in syllabi[:2]:
        if s.get("content"):
            course_notes += s["content"][:600]

    opening = (
        f"I've got full context on **{a['title']}** ({a.get('course_name', '')}). "
        f"Due {a.get('due_fmt', 'unknown')}. "
    )
    if analysis.get("estimated_hours"):
        opening += f"I'm estimating ~{analysis['estimated_hours']}h of work. "
    if analysis.get("reasoning"):
        opening += f"{analysis['reasoning']} "
    opening += "What do you want to know or work through?"

    # Pass assignment context for /chat/send to use
    return render_template("chat.html",
        messages=[{"body": opening, "direction_class": "hermes", "label": "Hermes", "timestamp": ""}],
        assignment_context=a,
        assignment_analysis=analysis,
    )


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

    # Build rich context including grades and syllabus notes
    courses_raw = db.get_courses()
    course_grades_list = [dict(c) for c in courses_raw if c.get("canvas_grade_pct") is not None]

    syllabus_notes = []
    for c in courses_raw[:5]:
        syllabi = db.get_syllabus(c["id"])
        for s in syllabi[:1]:
            try:
                import json as _json
                rules = _json.loads(s["rules_json"]) if s.get("rules_json") else {}
                gw = rules.get("grading_weights", {})
                ep = rules.get("late_policy", "")
                eb = rules.get("early_submission_bonus", {})
                if gw or ep or eb.get("exists"):
                    note = f"{c['name']}: weights={gw}" + (f" late={ep}" if ep else "") + (f" early_bonus={eb.get('description','')}" if eb.get("exists") else "")
                    syllabus_notes.append(note)
            except Exception:
                pass

    # If this is an assignment-specific chat, load full assignment context
    focused_assignment = None
    assignment_id = data.get("assignment_id")
    if assignment_id:
        fa = db.get_assignment_by_id(str(assignment_id))
        if fa:
            fa = dict(fa)
            if fa.get("analysis_json"):
                try:
                    fa["analysis"] = json.loads(fa["analysis_json"])
                except Exception:
                    pass
            # Include relevant course material content
            syllabi = db.get_syllabus(str(fa.get("course_id", "")))
            fa["course_content"] = "\n".join(
                s.get("content", "")[:800] for s in syllabi[:2] if s.get("content")
            )
            focused_assignment = fa

    context = {
        "assignments": assignments,
        "exams": db.get_upcoming_exams(days_ahead=30),
        "current_date": datetime.now().strftime("%Y-%m-%d %A"),
        "status_update_note": status_note,
        "grades": db.get_all_grades()[:10],
        "course_grades": course_grades_list,
        "syllabus_notes": " | ".join(syllabus_notes[:3]),
        "focused_assignment": focused_assignment,
    }

    response = generate_chat_response(user_message, context)
    db.store_message("outbound", response)

    return jsonify({"response": response})


@app.route("/api/assignment/<assignment_id>/done", methods=["POST"])
def mark_done(assignment_id):
    db.update_assignment_status(assignment_id, "submitted")
    return jsonify({"status": "ok"})

@app.route("/api/assignment/<assignment_id>/mark-done", methods=["POST"])
def mark_done_alias(assignment_id):
    db.update_assignment_status(assignment_id, "submitted")
    return jsonify({"status": "ok"})

@app.route("/api/assignment/<assignment_id>/toggle-complete", methods=["POST"])
def toggle_complete(assignment_id):
    """Toggle assignment status between submitted and not_submitted."""
    a = db.get_assignment_by_id(assignment_id)
    if not a:
        return jsonify({"error": "not found"}), 404
    current_status = a.get("status", "pending")
    if current_status in ("submitted", "complete"):
        new_status = "not_submitted"
    else:
        new_status = "submitted"
    db.update_assignment_status(assignment_id, new_status)
    return jsonify({"status": "ok", "new_status": new_status})

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


@app.route("/api/assignment/<assignment_id>/reanalyze", methods=["POST"])
def reanalyze_assignment(assignment_id):
    """Force re-analyze a single assignment using current course materials. Does NOT full sync."""
    conn = db.get_conn()
    conn.execute(
        "UPDATE assignments SET analysis_json=NULL, difficulty=NULL, estimated_hours=NULL, start_by=NULL, priority=NULL WHERE id=?",
        (assignment_id,)
    )
    conn.commit()
    conn.close()

    def _targeted_reanalyze():
        a = db.get_assignment_by_id(assignment_id)
        if not a:
            return
        a = dict(a)
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        try:
            import hermes as h
            from modules import analyzer
            cid = str(a.get("course_id", ""))
            rules_map = {cid: h._get_syllabus_rules(cid)}
            materials_map = {cid: h._get_course_materials(cid)}
            notes_map = {cid: db.get_course_notes(cid)}
            results = analyzer.analyze_assignments_batch([a], rules_map, materials_map, notes_map)
            if results and not results[0].get("_rate_limited"):
                db.store_analysis(a["id"], results[0])
                logger.info(f"Single re-analysis complete: {a['title']}")
        except Exception as e:
            logger.error(f"Single re-analysis failed for {assignment_id}: {e}")

    import threading
    threading.Thread(target=_targeted_reanalyze, daemon=True).start()
    return jsonify({"status": "re-analysis queued"})


@app.route("/api/reanalyze", methods=["POST"])
def reanalyze_all():
    """Clear all analysis and re-run. Use when Gemini was misbehaving."""
    import sqlite3
    conn = db.get_conn()
    conn.execute("UPDATE assignments SET analysis_json=NULL, difficulty=NULL, estimated_hours=NULL, start_by=NULL, priority=NULL")
    conn.execute("UPDATE exam_events SET analysis_json=NULL, study_hours_estimated=NULL, start_study_by=NULL")
    conn.commit()
    conn.close()
    # Track sync status
    db.set_pref("reanalyze_status", "running")
    db.set_pref("reanalyze_started", datetime.now().isoformat())
    db.set_pref("reanalyze_finished", "")
    # Trigger sync in background to re-analyze
    import threading, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    def _run_sync_and_finish():
        try:
            import hermes as h
            h.sync_canvas()
        except Exception:
            pass
        db.set_pref("reanalyze_status", "done")
        db.set_pref("reanalyze_finished", datetime.now().isoformat())
    try:
        t = threading.Thread(target=_run_sync_and_finish, daemon=True)
        t.start()
    except Exception:
        db.set_pref("reanalyze_status", "idle")
    return jsonify({"status": "re-analysis started — refresh in a minute"})


@app.route("/api/sync-status")
def sync_status():
    """Return current re-analysis/sync status for dashboard polling."""
    status = db.get_pref("reanalyze_status") or "idle"
    started = db.get_pref("reanalyze_started") or ""
    finished = db.get_pref("reanalyze_finished") or ""
    return jsonify({"status": status, "started": started, "finished": finished})


# ─── Grades ───────────────────────────────────────────────────────────────────

def _workload_label(hours):
    """Derive a workload category label from estimated hours."""
    if hours is None:
        return None
    if hours < 1:
        return ("quick", "var(--green)")
    elif hours < 3:
        return ("moderate", "var(--text)")
    elif hours < 6:
        return ("heavy", "var(--yellow)")
    elif hours < 12:
        return ("major", "var(--orange)")
    else:
        return ("week-eater", "var(--red)")


def _letter_grade(pct):
    if pct is None:
        return "?"
    if pct >= 93: return "A"
    if pct >= 90: return "A-"
    if pct >= 87: return "B+"
    if pct >= 83: return "B"
    if pct >= 80: return "B-"
    if pct >= 77: return "C+"
    if pct >= 73: return "C"
    if pct >= 70: return "C-"
    if pct >= 60: return "D"
    return "F"

def _grade_color(pct):
    if pct is None: return "muted"
    if pct >= 90: return "green"
    if pct >= 80: return "yellow"
    return "red"


@app.route("/grades")
def grades_page():
    import threading
    courses = db.get_courses()
    all_grades = db.get_all_grades()

    # If there are no grades at all, auto-trigger a background sync instead of
    # asking the user to do it manually.
    auto_syncing = False
    if not all_grades and courses:
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            import hermes as _h
            threading.Thread(target=_h.sync_canvas, daemon=True).start()
            auto_syncing = True
        except Exception:
            pass

    course_summaries = []
    for c in courses:
        cid = c["id"]
        course_grades = [g for g in all_grades if str(g.get("course_id")) == str(cid)]
        if not course_grades:
            course_summaries.append({
                "id": cid, "name": c["name"], "code": c.get("code", ""),
                "graded_count": 0, "current_avg": None, "letter": "?",
                "color": "muted", "target": db.get_grade_goal(cid),
                "grades": [], "on_track": None, "canvas_grade": None,
                "needed_on_final": None, "final_weight_pct": 0,
                "category_grades": [], "projected_optimistic": None,
                "projected_realistic": None, "trend": "stable",
                "trend_color": "muted", "trend_icon": "→",
                "ai_suggested_target": None, "ai_target_reasoning": None,
            })
            continue

        valid_pcts = [g["grade_pct"] for g in course_grades if g.get("grade_pct") is not None]
        current_avg = round(sum(valid_pcts) / len(valid_pcts), 1) if valid_pcts else None
        target = db.get_grade_goal(cid)

        # Prefer Canvas overall grade if available
        canvas_grade = c.get("canvas_grade_pct")
        display_avg = canvas_grade if canvas_grade is not None else current_avg

        # Get assignment groups for this course
        groups = db.get_assignment_groups(cid)

        # Calculate per-group grades
        category_grades = []
        final_group = None
        final_weight = None
        projected_optimistic = None
        projected_realistic = None
        trend = "stable"
        trend_color = "muted"
        trend_icon = "→"

        all_course_assignments = db.get_all_assignments_for_course(cid)

        for g in groups:
            if g["weight"] <= 0:
                continue
            # Get assignments in this group
            group_assignments = [a for a in all_course_assignments
                                if a.get("canvas_group_id") == g["canvas_group_id"]]
            group_assignment_ids = {a["id"] for a in group_assignments}
            # Get grades for those assignments
            graded = [gr for gr in course_grades if gr.get("assignment_id") in group_assignment_ids and gr.get("grade_pct") is not None]
            graded_ids = {gr["assignment_id"] for gr in graded}

            group_avg = round(sum(g2["grade_pct"] for g2 in graded) / len(graded), 1) if graded else None

            gname_lower = g["name"].lower()
            is_final = "final exam" in gname_lower or (gname_lower == "final")
            has_ungraded = len(group_assignments) > len(graded)

            # Points-based group calculations for projections
            total_pts = sum((a.get("points_possible") or 0) for a in group_assignments if (a.get("points_possible") or 0) > 0)
            graded_earned = sum(
                (gr.get("points_earned") or 0) if (gr.get("points_earned") or 0) > 0
                else ((gr.get("grade_pct") or 0) / 100.0 * (gr.get("a_points_possible") or 0))
                for gr in graded
            )
            graded_possible = sum(gr.get("points_possible", 0) or gr.get("a_points_possible", 0) or 0 for gr in graded)
            ungraded_assignments = [a for a in group_assignments if a["id"] not in graded_ids]
            ungraded_pts = sum((a.get("points_possible") or 0) for a in ungraded_assignments)

            category_grades.append({
                "name": g["name"],
                "weight": g["weight"],
                "current_pct": group_avg,
                "graded_count": len(graded),
                "total_count": len(group_assignments),
                "is_final": is_final,
                "total_pts": total_pts,
                "graded_earned": graded_earned,
                "graded_possible": graded_possible,
                "ungraded_pts": ungraded_pts,
            })

            if is_final and has_ungraded:
                final_group = g["name"]
                final_weight = g["weight"] / 100.0

        if final_weight and display_avg is not None and final_weight > 0:
            current_weight = 1.0 - final_weight
            needed_on_final = round((target - display_avg * current_weight) / final_weight, 1)
            needed_on_final = min(needed_on_final, 200)  # cap at 200% to avoid absurd numbers
        else:
            needed_on_final = None
            final_weight = 0

        # --- Grade Impact Projections ---
        # For each group, project optimistic (ace everything) and realistic (maintain current pace)
        optimistic_total = 0.0
        realistic_total = 0.0
        projections_possible = False

        for cat in category_grades:
            w = cat["weight"] / 100.0
            if w <= 0:
                continue
            total_pts = cat["total_pts"]
            graded_earned = cat["graded_earned"]
            graded_possible = cat["graded_possible"]
            ungraded_pts = cat["ungraded_pts"]

            if total_pts <= 0:
                # Group exists but no assignments linked (e.g. Final Exam before posted)
                if cat["current_pct"] is not None:
                    optimistic_total += cat["current_pct"] * w
                    realistic_total += cat["current_pct"] * w
                else:
                    # Optimistic: assume 100%; Realistic: assume current overall course average
                    optimistic_total += 100.0 * w
                    if display_avg is not None:
                        realistic_total += display_avg * w
                    else:
                        realistic_total += 75.0 * w  # reasonable default
                    projections_possible = True
                continue

            projections_possible = True
            # Optimistic: assume 100% on all remaining
            opt_earned = graded_earned + ungraded_pts
            opt_pct = (opt_earned / total_pts * 100) if total_pts > 0 else 0
            optimistic_total += opt_pct * w

            # Realistic: assume current average on remaining
            current_rate = (graded_earned / graded_possible) if graded_possible > 0 else 0.85
            real_earned = graded_earned + current_rate * ungraded_pts
            real_pct = (real_earned / total_pts * 100) if total_pts > 0 else 0
            realistic_total += real_pct * w

        projected_optimistic = round(optimistic_total, 1) if projections_possible else None
        projected_realistic = round(realistic_total, 1) if projections_possible else None

        # Compute grade trajectory: trend from graded assignments over time
        graded_with_dates = []
        for g_entry in course_grades:
            aid = g_entry.get("assignment_id")
            a_match = next((a for a in all_course_assignments if a["id"] == aid), None)
            if a_match and a_match.get("due_at") and g_entry.get("grade_pct") is not None:
                graded_with_dates.append((a_match["due_at"], g_entry["grade_pct"]))
        graded_with_dates.sort(key=lambda x: x[0])

        trend = "stable"
        trend_color = "muted"
        trend_icon = "→"
        if len(graded_with_dates) >= 4:
            half = len(graded_with_dates) // 2
            first_half = [p for _, p in graded_with_dates[:half]]
            second_half = [p for _, p in graded_with_dates[half:]]
            first_avg = sum(first_half) / len(first_half)
            second_avg = sum(second_half) / len(second_half)
            diff = second_avg - first_avg
            if diff > 3:
                trend = "up"
                trend_color = "green"
                trend_icon = "↑"
            elif diff < -3:
                trend = "down"
                trend_color = "red"
                trend_icon = "↓"

        # AI-suggested target
        ai_suggestion = db.get_grade_target_suggestion(cid)

        # Grade pattern recognition
        patterns = []
        for cat in category_grades:
            if cat["current_pct"] is not None and cat["graded_count"] >= 2:
                if display_avg and cat["current_pct"] < display_avg - 8:
                    patterns.append(
                        f"Your {cat['name']} average ({cat['current_pct']:.0f}%) is dragging down your overall grade — "
                        f"this category is worth {cat['weight']:.0f}% of your final grade."
                    )
                elif display_avg and cat["current_pct"] > display_avg + 8:
                    patterns.append(
                        f"You're strong in {cat['name']} ({cat['current_pct']:.0f}%) — keep it up."
                    )

        course_summaries.append({
            "id": cid, "name": c["name"], "code": c.get("code", ""),
            "graded_count": len(course_grades),
            "current_avg": display_avg,
            "canvas_grade": canvas_grade,
            "letter": _letter_grade(display_avg),
            "color": _grade_color(display_avg),
            "target": target,
            "on_track": (display_avg >= target) if display_avg is not None else None,
            "needed_on_final": needed_on_final,
            "final_weight_pct": round(final_weight * 100) if final_weight else 0,
            "grades": course_grades[:5],
            "ai_suggested_target": ai_suggestion.get("target") if ai_suggestion else None,
            "ai_target_reasoning": ai_suggestion.get("reasoning") if ai_suggestion else None,
            "category_grades": category_grades,
            "projected_optimistic": projected_optimistic,
            "projected_realistic": projected_realistic,
            "trend": trend,
            "trend_color": trend_color,
            "trend_icon": trend_icon,
            "patterns": patterns,
        })

    return render_template("grades.html",
        course_summaries=course_summaries,
        letter_grade=_letter_grade,
        grade_color=_grade_color,
        auto_syncing=auto_syncing,
    )




@app.route("/api/grade-goal/<course_id>", methods=["POST"])
def set_grade_goal(course_id):
    data = request.get_json()
    target = float(data.get("target", 90))
    db.set_grade_goal(course_id, target)
    return jsonify({"status": "ok"})


@app.route("/api/course/<course_id>/ignore", methods=["POST"])
def toggle_course_ignore(course_id):
    data = request.get_json(force=True, silent=True) or {}
    ignored = bool(data.get("ignored", True))
    db.set_course_ignored(course_id, ignored)
    return jsonify({"status": "ok", "ignored": ignored})


# ─── Study Plan ───────────────────────────────────────────────────────────────

@app.route("/study-plan")
def study_plan_page():
    import math
    from collections import defaultdict
    plan = db.get_study_plan()

    # Group saved plan entries by date
    by_date = defaultdict(list)
    for entry in plan:
        by_date[entry["date"]].append(entry)

    now = datetime.now()
    assignments_raw = db.get_upcoming_assignments(days_ahead=14)
    assignments_sp = [_enrich_assignment(a) for a in assignments_raw]
    exams_sp = db.get_upcoming_exams(days_ahead=14)

    days_list = []
    for i in range(7):
        day_date = (now + timedelta(days=i)).date()
        date_str = day_date.isoformat()
        entries = by_date.get(date_str, [])

        # Assignments due that day
        due_this_day = [a for a in assignments_sp if a.get("due_dt") and a["due_dt"].date() == day_date]

        # Assignments to START working on today (due_date minus ceil(hours/3) days)
        start_today = []
        for a in assignments_sp:
            if not a.get("due_dt"):
                continue
            if a["due_dt"].date() == day_date:
                continue  # already in due_this_day
            est_hours = a.get("estimated_hours") or 2.0
            days_to_start_before = math.ceil(est_hours / 3)
            ideal_start = a["due_dt"].date() - timedelta(days=days_to_start_before)
            if ideal_start == day_date:
                start_today.append(a)

        # Exams due that day
        exams_today = []
        for e in exams_sp:
            if e.get("start_at"):
                try:
                    exam_dt = _from_canvas_time(e["start_at"])
                    if exam_dt.date() == day_date:
                        exams_today.append(e)
                except Exception:
                    pass

        # Exams within 7 days — note when to start studying
        exam_study_reminders = []
        for e in exams_sp:
            if e.get("start_at"):
                try:
                    exam_dt = _from_canvas_time(e["start_at"])
                    days_away = (exam_dt.date() - day_date).days
                    if 0 < days_away <= 7:
                        study_hours = 6  # default
                        if e.get("analysis_json"):
                            try:
                                ea = json.loads(e["analysis_json"])
                                study_hours = ea.get("study_hours", 6)
                            except Exception:
                                pass
                        days_needed = math.ceil(study_hours / 3)
                        start_day = exam_dt.date() - timedelta(days=days_needed)
                        if start_day == day_date:
                            exam_study_reminders.append({
                                "title": e["title"],
                                "course_name": e.get("course_name", ""),
                                "days_away": days_away,
                                "days_needed": days_needed,
                            })
                except Exception:
                    pass

        total_hours = round(sum(e.get("hours_planned", 0) for e in entries), 1)
        # Urgency color based on hours due that day
        due_hours = round(sum(a.get("estimated_hours") or 0 for a in due_this_day), 1)
        if due_hours == 0:
            urgency_color = "var(--green)"
            urgency_label = "free"
        elif due_hours < 3:
            urgency_color = "var(--yellow)"
            urgency_label = f"{due_hours}h due"
        elif due_hours < 6:
            urgency_color = "var(--orange)"
            urgency_label = f"{due_hours}h due"
        else:
            urgency_color = "var(--red)"
            urgency_label = f"{due_hours}h due"

        days_list.append({
            "date_str": date_str,
            "date_fmt": day_date.strftime("%A, %b %-d"),
            "is_today": i == 0,
            "entries": entries,
            "total_hours": total_hours,
            "due_this_day": due_this_day,
            "start_today": start_today,
            "exams_today": exams_today,
            "exam_study_reminders": exam_study_reminders,
            "urgency_color": urgency_color,
            "urgency_label": urgency_label,
        })

    has_plan = bool(plan)
    return render_template("study_plan.html", days_list=days_list, has_plan=has_plan)


@app.route("/api/study-plan/generate", methods=["POST"])
def generate_study_plan():
    from config import Config
    from modules.analyzer import generate_study_plan as _gen_plan

    assignments = db.get_upcoming_assignments(days_ahead=14)
    exams = db.get_upcoming_exams(days_ahead=14)
    entries = _gen_plan(assignments, exams, wake_hour=Config.WAKE_HOUR,
                        stop_hour=Config.NO_WORK_AFTER_HOUR, days=14)
    db.save_study_plan(entries)
    return jsonify({"status": "ok", "entries": len(entries)})


# ─── Alerts (Hermes-generated) ────────────────────────────────────────────────

@app.route("/alerts")
def alerts_page():
    """Hermes-generated intelligence: proactive warnings, urgent items, what Hermes would text."""
    from config import Config as _Cfg
    now = datetime.now()
    assignments_raw = db.get_upcoming_assignments(days_ahead=21)
    assignments = [_enrich_assignment(a) for a in assignments_raw]
    exams_raw = db.get_upcoming_exams(days_ahead=21)
    exams = [_enrich_exam(e) for e in exams_raw]

    # --- API quota status ---
    usage = db.get_api_usage_summary()
    gemini_calls = usage.get("gemini", 0)
    groq_calls = usage.get("groq", 0)
    gemini_limit = _Cfg.GEMINI_DAILY_LIMIT
    groq_limit = _Cfg.GROQ_DAILY_LIMIT

    quota_status = [
        {
            "provider": "Gemini",
            "calls": gemini_calls,
            "limit": gemini_limit,
            "pct": round(gemini_calls / gemini_limit * 100) if gemini_limit else 0,
            "exhausted": gemini_calls >= gemini_limit,
            "warning": gemini_calls >= gemini_limit * 0.8,
        },
        {
            "provider": "Groq",
            "calls": groq_calls,
            "limit": groq_limit,
            "pct": round(groq_calls / groq_limit * 100) if groq_limit else 0,
            "exhausted": groq_calls >= groq_limit,
            "warning": groq_calls >= groq_limit * 0.8,
        },
    ]

    alerts = []

    # Overdue items — Hermes determines if each can still earn points
    for a in assignments:
        if not (a.get("due_dt") and (a["due_dt"] - now).total_seconds() < 0):
            continue
        if a.get("status") in ("submitted", "complete"):
            continue
        submittable, late_reason = _assignment_still_submittable(a, now)
        if submittable is False:
            continue  # can't earn points — don't show it
        title = "Overdue — Submit Now" if submittable is True else "Overdue — Policy Unclear"
        alerts.append({
            "level": "danger", "icon": "overdue",
            "title": title,
            "message": f"{a['title']} ({a.get('course_name','')}) was due {a['due_fmt']}. {late_reason}",
            "assignment_id": a["id"],
        })

    # Heavy workload next 48h
    next_48h = [a for a in assignments if a.get("due_dt") and 0 <= (a["due_dt"] - now).total_seconds() / 3600 <= 48]
    total_48h = sum(a.get("estimated_hours") or 0 for a in next_48h)
    if total_48h >= 4:
        hardest = max(next_48h, key=lambda x: x.get("difficulty") or 0, default=None)
        alerts.append({
            "level": "danger", "icon": "fire",
            "title": f"~{round(total_48h,1)}h due in 48h",
            "message": f"You have {len(next_48h)} assignment(s) totaling ~{round(total_48h,1)}h due in the next 48 hours. Start with {hardest['title']} first." if hardest else f"{len(next_48h)} assignments due in 48h.",
            "assignment_id": hardest["id"] if hardest else None,
        })

    # Unstarted assignments due within 30h
    for a in assignments:
        if a.get("due_dt") and a.get("status", "pending") == "pending":
            hours = (a["due_dt"] - now).total_seconds() / 3600
            if 0 < hours <= 30:
                alerts.append({
                    "level": "danger", "icon": "clock",
                    "title": f"Due in ~{int(hours)}h",
                    "message": f"'{a['title']}' ({a.get('course_name','')}) hasn't been started and is due soon.",
                    "assignment_id": a["id"],
                })

    # Exams within 5 days
    for e in exams:
        if e.get("start_at"):
            try:
                exam_dt = _from_canvas_time(e["start_at"])
                days = (exam_dt - now).days
                if 0 < days <= 5:
                    alerts.append({
                        "level": "warning", "icon": "exam",
                        "title": f"Exam in {days} day{'s' if days != 1 else ''}",
                        "message": f"{e['title']} ({e.get('course_name','')}) — you should already be studying.",
                        "assignment_id": None,
                    })
            except Exception:
                pass

    # 3+ assignments same day
    from collections import defaultdict
    day_map = defaultdict(list)
    for a in assignments:
        if a.get("due_dt"):
            day_map[a["due_dt"].date()].append(a)
    for day_date, items in sorted(day_map.items()):
        if len(items) >= 3:
            days_away = (datetime(day_date.year, day_date.month, day_date.day) - now).days
            if 1 <= days_away <= 10:
                alerts.append({
                    "level": "warning", "icon": "stack",
                    "title": f"{len(items)} due {day_date.strftime('%a %b %-d')}",
                    "message": f"{', '.join(a['title'] for a in items[:3])}{'...' if len(items) > 3 else ''} all due the same day.",
                    "assignment_id": None,
                })

    # Early bonus windows closing
    for a in assignments:
        if a.get("has_early_bonus") and a.get("due_dt"):
            hours = (a["due_dt"] - now).total_seconds() / 3600
            if 24 <= hours <= 36:
                alerts.append({
                    "level": "info", "icon": "bonus",
                    "title": "Early bonus closing",
                    "message": f"Submit '{a['title']}' in the next ~{int(hours - 24)}h to earn bonus points.",
                    "assignment_id": a["id"],
                })

    # Due in <24h and not submitted
    for a in assignments:
        if a.get("due_dt") and a.get("status", "pending") not in ("submitted", "complete"):
            hours = (a["due_dt"] - now).total_seconds() / 3600
            if 0 < hours < 24:
                # Avoid duplicate with "Unstarted assignments due within 30h"
                already = any(al.get("assignment_id") == a["id"] for al in alerts)
                if not already:
                    alerts.append({
                        "level": "danger", "icon": "clock",
                        "title": f"Due in {int(hours)}h",
                        "message": f"'{a['title']}' ({a.get('course_name','')}) is due in less than 24 hours and is not marked as submitted.",
                        "assignment_id": a["id"],
                    })

    # Recently graded assignments with low scores
    recent_grades_for_alert = sorted(
        [g for g in all_grades_for_alert if g.get("grade_pct") is not None],
        key=lambda x: x.get("entered_at", ""),
        reverse=True
    )
    for g in recent_grades_for_alert[:10]:
        if g.get("grade_pct", 100) < 75:
            already = any(al.get("assignment_id") == g.get("assignment_id") for al in alerts)
            if not already:
                alerts.append({
                    "level": "warning",
                    "icon": "grade",
                    "title": f"Low Grade: {g.get('title', 'Assignment')}",
                    "message": f"You received {g['grade_pct']:.1f}% on {g.get('title','')} ({g.get('course_name','')}).",
                    "assignment_id": g.get("assignment_id"),
                })

    # Courses below target grade
    courses_all = db.get_courses()
    all_grades_for_alert = db.get_all_grades()
    for c in courses_all:
        cid = c["id"]
        canvas_grade = c.get("canvas_grade_pct")
        if canvas_grade is None:
            course_grades = [g for g in all_grades_for_alert if str(g.get("course_id")) == str(cid)]
            valid = [g["grade_pct"] for g in course_grades if g.get("grade_pct") is not None]
            display_avg = round(sum(valid) / len(valid), 1) if valid else None
        else:
            display_avg = canvas_grade
        target = db.get_grade_goal(cid)
        if display_avg is not None and display_avg < target:
            gap = round(target - display_avg, 1)
            alerts.append({
                "level": "warning", "icon": "grades",
                "title": f"{c.get('code') or c['name']} below target",
                "message": f"Current grade is {display_avg}% — {gap}% below your {target}% target. Check the Grades page for what score you need on the final.",
                "assignment_id": None,
            })

    # Upcoming exams in <3 days
    for e in exams:
        if e.get("start_at"):
            try:
                exam_dt = _from_canvas_time(e["start_at"])
                days = (exam_dt - now).days
                if 0 <= days < 3:
                    already = any(al.get("icon") == "exam" and e["title"] in al.get("message", "") for al in alerts)
                    if not already:
                        alerts.append({
                            "level": "danger", "icon": "exam",
                            "title": f"Exam {'tomorrow' if days == 1 else 'today' if days == 0 else 'in 2 days'}",
                            "message": f"{e['title']} ({e.get('course_name','')}) is {'today' if days == 0 else 'in ' + str(days) + ' day' + ('s' if days != 1 else '')}. Stop reading alerts and start reviewing.",
                            "assignment_id": None,
                        })
            except Exception:
                pass

    if not alerts:
        alerts = []  # empty state handled in template

    return render_template("alerts.html", alerts=alerts, quota_status=quota_status)


# ─── Announcements (Canvas professor posts — lives in Courses tab) ─────────────

HIGHLIGHT_KEYWORDS = [
    "due date", "extended", "extra credit", "exam", "cancelled", "canceled",
    "moved", "postponed", "bonus", "important", "reminder", "grade"
]

def _enrich_announcements(announcements):
    for ann in announcements:
        text = ((ann.get("title") or "") + " " + (ann.get("message") or "")).lower()
        ann["highlighted"] = any(kw in text for kw in HIGHLIGHT_KEYWORDS)
        try:
            dt = datetime.fromisoformat(ann["posted_at"].replace("Z", "+00:00")).replace(tzinfo=None)
            ann["posted_fmt"] = dt.strftime("%b %-d")
        except Exception:
            ann["posted_fmt"] = (ann.get("posted_at") or "")[:10]
    return announcements


@app.route("/api/announcement/<canvas_id>/read", methods=["POST"])
def mark_announcement_read(canvas_id):
    db.mark_announcement_read(canvas_id)
    return jsonify({"status": "ok"})


# ─── Assignment Notes & Checklist ─────────────────────────────────────────────

@app.route("/api/assignment/<assignment_id>/note", methods=["POST"])
def save_note(assignment_id):
    data = request.get_json()
    note = data.get("note", "")
    db.upsert_assignment_note(assignment_id, note)
    return jsonify({"status": "ok"})


@app.route("/api/assignment/<assignment_id>/checklist", methods=["POST"])
def add_checklist_item(assignment_id):
    data = request.get_json()
    item_text = (data.get("item_text") or "").strip()
    if not item_text:
        return jsonify({"error": "empty"}), 400
    item_id = db.add_checklist_item(assignment_id, item_text)
    return jsonify({"status": "ok", "id": item_id})


@app.route("/api/checklist/<int:item_id>/toggle", methods=["POST"])
def toggle_checklist(item_id):
    db.toggle_checklist_item(item_id)
    return jsonify({"status": "ok"})


@app.route("/api/checklist/<int:item_id>", methods=["DELETE"])
def delete_checklist(item_id):
    db.delete_checklist_item(item_id)
    return jsonify({"status": "ok"})


@app.route("/api/assignment/<assignment_id>/time", methods=["POST"])
def log_time(assignment_id):
    data = request.get_json()
    minutes = int(data.get("minutes", 0))
    if minutes > 0:
        db.log_time_spent(assignment_id, minutes)
    total = db.get_time_spent(assignment_id)
    return jsonify({"status": "ok", "total_minutes": total})




@app.route("/roi")
def roi_page():
    """Return on Investment — grade recovery and extra credit opportunities."""
    # Build group weight lookup: {(course_id, canvas_group_id): weight}
    courses = db.get_courses()
    group_weights = {}  # (course_id, canvas_group_id) -> weight
    for c in courses:
        for g in db.get_assignment_groups(c["id"]):
            group_weights[(str(c["id"]), g["canvas_group_id"])] = g["weight"]

    # --- Resubmission opportunities ---
    resubs = db.get_resubmittable_assignments()
    resub_ops = []
    for r in resubs:
        cid = str(r["course_id"])
        gid = r["canvas_group_id"]
        group_weight = group_weights.get((cid, gid), 0.0)
        total_pts = db.get_group_total_points(cid, gid) if gid else 0
        points_possible = r.get("points_possible") or 0
        current_pct = r.get("grade_pct") or 0
        points_earned = r.get("points_earned") or 0
        potential_gain_pts = points_possible - points_earned  # max possible gain
        if total_pts > 0 and group_weight > 0:
            grade_impact = round((potential_gain_pts / total_pts) * group_weight, 2)
        else:
            grade_impact = None

        analysis = {}
        try:
            analysis = json.loads(r["analysis_json"]) if r.get("analysis_json") else {}
        except Exception:
            pass

        resub_ops.append({
            "id": r["id"],
            "title": r["title"],
            "course_name": r["course_name"],
            "current_pct": round(current_pct, 1),
            "points_earned": round(points_earned, 1),
            "points_possible": points_possible,
            "grade_impact": grade_impact,
            "html_url": r.get("html_url"),
            "resubmit_details": analysis.get("resubmit_details", ""),
        })
    resub_ops.sort(key=lambda x: x.get("grade_impact") or 0, reverse=True)

    # --- Extra credit opportunities ---
    ec_raw = db.get_extra_credit_assignments()
    ec_ops = []
    for r in ec_raw:
        cid = str(r["course_id"])
        gid = r["canvas_group_id"]
        group_weight = group_weights.get((cid, gid), 0.0)
        total_pts = db.get_group_total_points(cid, gid) if gid else 0
        points_possible = r.get("points_possible") or 0
        if total_pts > 0 and group_weight > 0:
            grade_impact = round((points_possible / total_pts) * group_weight, 2)
        else:
            grade_impact = None

        due_fmt, _, due_class = _fmt_due(r.get("due_at"))
        est_hours = r.get("estimated_hours")
        ec_ops.append({
            "id": r["id"],
            "title": r["title"],
            "course_name": r["course_name"],
            "points_possible": points_possible,
            "grade_impact": grade_impact,
            "due_fmt": due_fmt,
            "due_class": due_class,
            "est_hours": est_hours,
            "html_url": r.get("html_url"),
        })
    ec_ops.sort(key=lambda x: x.get("grade_impact") or 0, reverse=True)

    # Split EC into independent (can do now) vs activity-based
    _EC_ACTIVITY_KEYWORDS = ["kudos", "outreach", "extension", "lecture", "lab extension",
                             "in-class", "attendance", "participation", "k-12"]
    ec_independent = []
    ec_activity = []
    for ec in ec_ops:
        title_lower = (ec.get("title") or "").lower()
        if any(kw in title_lower for kw in _EC_ACTIVITY_KEYWORDS):
            ec_activity.append(ec)
        else:
            ec_independent.append(ec)

    # --- Low-weight assignments to consider skipping ---
    # Assignments worth <1% of final grade, unsubmitted, due >3 days away
    skip_candidates = []
    all_upcoming = db.get_upcoming_assignments(days_ahead=30)
    for a in all_upcoming:
        if a.get("status") in ("submitted", "complete"):
            continue
        cid = str(a.get("course_id", ""))
        gid = a.get("canvas_group_id")
        group_weight = group_weights.get((cid, gid), 0.0)
        total_pts = db.get_group_total_points(cid, gid) if gid else 0
        pts = a.get("points_possible") or 0
        if total_pts > 0 and group_weight > 0 and pts > 0:
            grade_impact = round((pts / total_pts) * group_weight, 2)
            est_hours = a.get("estimated_hours") or 0
            # Flag if impact < 1% of final grade but would take >1h
            if grade_impact < 1.0 and est_hours > 1.0:
                due_fmt, _, due_class = _fmt_due(a.get("due_at"))
                skip_candidates.append({
                    "id": a["id"],
                    "title": a["title"],
                    "course_name": a.get("course_name", ""),
                    "grade_impact": grade_impact,
                    "est_hours": est_hours,
                    "due_fmt": due_fmt,
                    "due_class": due_class,
                    "ratio": round(grade_impact / est_hours, 3) if est_hours > 0 else 0,
                })
    # Sort by worst ratio (most hours for least grade impact)
    skip_candidates.sort(key=lambda x: x["ratio"])
    skip_candidates = skip_candidates[:8]

    return render_template("roi.html",
        resub_ops=resub_ops,
        ec_ops=ec_ops,
        ec_independent=ec_independent,
        ec_activity=ec_activity,
        skip_candidates=skip_candidates,
    )


@app.errorhandler(404)
def not_found(e):
    try:
        return render_template("404.html"), 404
    except Exception:
        return "<h1>404 — Page not found</h1>", 404


@app.errorhandler(500)
def server_error(e):
    logger.error(f"500 error: {e}")
    try:
        return render_template("500.html"), 500
    except Exception:
        return "<h1>500 — Server error</h1>", 500


@app.route("/settings")
def settings_page():
    from config import Config as _Cfg
    last_sync = db.get_pref("last_sync_time")
    token_display = None
    if _Cfg.CANVAS_TOKEN:
        t = _Cfg.CANVAS_TOKEN
        token_display = t[:6] + "..." + t[-4:] if len(t) > 10 else "set"
    courses = db.get_courses(include_ignored=True)
    sync_hour_1 = int(db.get_pref("sync_hour_1") or 14)
    sync_hour_2 = int(db.get_pref("sync_hour_2") or 20)
    return render_template("settings.html",
        last_sync=last_sync,
        token_display=token_display,
        canvas_url=getattr(_Cfg, "CANVAS_BASE_URL", ""),
        courses=courses,
        digest_hour=getattr(_Cfg, "DIGEST_HOUR", 8),
        sync_hour_1=sync_hour_1,
        sync_hour_2=sync_hour_2,
    )


@app.route("/api/settings/sync-schedule", methods=["POST"])
def save_sync_schedule():
    data = request.get_json(force=True, silent=True) or {}
    h1 = data.get("sync_hour_1")
    h2 = data.get("sync_hour_2")
    if h1 is not None:
        db.set_pref("sync_hour_1", str(int(h1)))
    if h2 is not None:
        db.set_pref("sync_hour_2", str(int(h2)))
    return jsonify({"status": "ok"})


def run(port=5000, debug=False, use_reloader=True):
    app.run(host="127.0.0.1", port=port, debug=debug, use_reloader=use_reloader)
