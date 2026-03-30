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
            heat_class, heat_height = _heatmap_level(intensity)
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

    # Flag if Canvas gave us a useless description so the UI can show a note
    description_inferred = _is_boilerplate_description(a.get("description", ""))

    # Ingested course materials — show only ones relevant to this assignment
    course_materials = []
    syllabi = db.get_syllabus(str(a.get("course_id", "")))
    course_materials = _relevant_course_materials(a.get("title", ""), syllabi)

    # task_sections and course_weight_context from analysis
    task_sections = []
    course_weight_context = ""
    if a.get("analysis_json"):
        try:
            analysis_extra = json.loads(a["analysis_json"])
            task_sections = analysis_extra.get("task_sections") or []
            course_weight_context = analysis_extra.get("course_weight_context") or ""
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
    )


@app.route("/assignments")
def assignments_page():
    assignments_raw = db.get_upcoming_assignments(days_ahead=90)
    assignments = [_enrich_assignment(a) for a in assignments_raw]

    # Attach checklist stats for completion % display
    for a in assignments:
        stats = db.get_checklist_stats(a["id"])
        a["checklist_stats"] = stats

    courses = db.get_courses()
    course_names = sorted(set(a["course_name"] for a in assignments if a.get("course_name")))
    total_hours = round(sum(a.get("estimated_hours") or 0 for a in assignments), 1)
    return render_template("assignments.html", assignments=assignments, total=len(assignments),
                           courses=courses, course_names=course_names, total_hours=total_hours)


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
    if e.get("analysis_json"):
        try:
            analysis = json.loads(e["analysis_json"])
            reasoning = analysis.get("reasoning", "")
            study_tips = analysis.get("study_tips", study_tips)
        except Exception:
            pass

    course_notes = db.get_course_notes(str(e.get("course_id", "")))

    return render_template("exam_detail.html",
        e=e,
        reasoning=reasoning,
        study_tips=study_tips,
        course_notes=course_notes,
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
    courses = db.get_courses()
    all_grades = db.get_all_grades()

    course_summaries = []
    for c in courses:
        cid = c["id"]
        course_grades = [g for g in all_grades if str(g.get("course_id")) == str(cid)]
        if not course_grades:
            course_summaries.append({
                "id": cid, "name": c["name"], "code": c.get("code", ""),
                "graded_count": 0, "current_avg": None, "letter": "?",
                "color": "muted", "target": db.get_grade_goal(cid),
                "grades": [], "on_track": None,
                "needed_on_final": None, "final_weight_pct": 0,
                "category_grades": [],
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
        group_map = {g["canvas_group_id"]: g for g in groups}

        # Calculate per-group grades
        category_grades = []
        final_group = None
        final_weight = None

        for g in groups:
            if g["weight"] <= 0:
                continue
            # Get assignments in this group
            group_assignments = [a for a in db.get_all_assignments_for_course(cid)
                                if a.get("canvas_group_id") == g["canvas_group_id"]]
            group_assignment_ids = {a["id"] for a in group_assignments}
            # Get grades for those assignments
            graded = [gr for gr in course_grades if gr.get("assignment_id") in group_assignment_ids and gr.get("grade_pct") is not None]

            group_avg = round(sum(g2["grade_pct"] for g2 in graded) / len(graded), 1) if graded else None

            gname_lower = g["name"].lower()
            is_final = "final exam" in gname_lower or (gname_lower == "final")
            has_ungraded = len(group_assignments) > len(graded)

            category_grades.append({
                "name": g["name"],
                "weight": g["weight"],
                "current_pct": group_avg,
                "graded_count": len(graded),
                "total_count": len(group_assignments),
                "is_final": is_final,
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

        # AI-suggested target
        ai_suggestion = db.get_grade_target_suggestion(cid)

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
        })

    return render_template("grades.html",
        course_summaries=course_summaries,
        letter_grade=_letter_grade,
        grade_color=_grade_color,
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
    from config import Config
    plan = db.get_study_plan()

    # Group by date
    from collections import defaultdict
    by_date = defaultdict(list)
    for entry in plan:
        by_date[entry["date"]].append(entry)

    now = datetime.now()
    days_list = []
    for i in range(14):
        day_date = (now + timedelta(days=i)).date()
        date_str = day_date.isoformat()
        entries = by_date.get(date_str, [])
        total = round(sum(e.get("hours_planned", 0) for e in entries), 1)
        days_list.append({
            "date_str": date_str,
            "date_fmt": day_date.strftime("%A, %b %-d"),
            "is_today": i == 0,
            "entries": entries,
            "total_hours": total,
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

    # Overdue items
    for a in assignments:
        if a.get("due_dt") and (a["due_dt"] - now).total_seconds() < 0 and a.get("status") != "submitted":
            alerts.append({
                "level": "danger", "icon": "overdue",
                "title": "Overdue",
                "message": f"{a['title']} ({a.get('course_name','')}) was due {a['due_fmt']}.",
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
        skip_candidates=skip_candidates,
    )


def run(port=5000, debug=False, use_reloader=True):
    app.run(host="127.0.0.1", port=port, debug=debug, use_reloader=use_reloader)
