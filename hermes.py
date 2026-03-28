#!/usr/bin/env python3
"""
Hermes — AI School Buddy for Ohio State University
Monitors Carmen/Canvas, analyzes assignments, keeps you on track.

Primary interface: http://localhost:5000
SMS alerts: optional, one-way outbound only
"""

import logging
import os
import sys
import json
import threading
import time
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import Config
import database as db
from modules import canvas_client, analyzer, syllabus

EXAM_KEYWORDS = ["exam", "midterm", "final"]
EXAM_EXCLUSIONS = ["practice", "review", "prep", "sample", "example", "study guide"]

def _looks_like_exam(title: str) -> bool:
    t = title.lower()
    if any(ex in t for ex in EXAM_EXCLUSIONS):
        return False
    return any(kw in t for kw in EXAM_KEYWORDS)

def _is_default_analysis(analysis_json: str) -> bool:
    """Return True if stored analysis looks like it used fallback defaults."""
    try:
        a = json.loads(analysis_json)
        return a.get("difficulty") == 5 and a.get("estimated_hours") == 2.0
    except Exception:
        return False


from modules.scheduler_engine import (
    should_send_start_reminder, should_send_check_in,
    is_within_active_hours, get_early_bonus_window
)

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/hermes.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("hermes")

# Suppress noisy libs
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)


# --- Canvas Sync ---

def sync_canvas():
    logger.info("Canvas sync starting...")
    courses = canvas_client.get_active_courses()
    if not courses:
        logger.warning("No active courses returned. Check CANVAS_TOKEN.")
        return

    course_ids = []
    for course in courses:
        cid = course.get("id")
        name = course.get("name", "Unknown")
        code = course.get("course_code", "")
        db.upsert_course(cid, name, code)
        course_ids.append(cid)

    new_for_analysis = []
    needs_reanalysis = []

    for course in courses:
        cid = course["id"]
        cname = course.get("name", "")
        assignments = canvas_client.get_assignments(cid)

        for a in assignments:
            if not a.get("due_at"):
                continue
            title = a.get("name", "Untitled")
            data = {
                "canvas_id": a["id"],
                "course_id": str(cid),
                "course_name": cname,
                "title": title,
                "description": a.get("description", ""),
                "due_at": a.get("due_at"),
                "points_possible": a.get("points_possible"),
                "submission_types": ",".join(a.get("submission_types", [])),
                "html_url": a.get("html_url", ""),
            }
            assignment_id = db.upsert_assignment(data)

            # Auto-mark submitted/graded items from Canvas submission data
            submission = a.get("submission") or {}
            canvas_state = submission.get("workflow_state", "")
            if canvas_state in ("submitted", "graded", "complete"):
                db.update_assignment_status(assignment_id, "submitted")

            existing = db.get_assignment_by_id(assignment_id)
            if existing and existing.get("status") not in ("submitted", "complete"):
                if not existing.get("analysis_json"):
                    new_for_analysis.append(existing)
                elif _is_default_analysis(existing["analysis_json"]):
                    needs_reanalysis.append(existing)

            # Also register exam-like assignments as exam events
            if _looks_like_exam(title) and a.get("due_at"):
                db.upsert_exam({
                    "canvas_id": f"asgn_{a['id']}",
                    "course_id": str(cid),
                    "course_name": cname,
                    "title": title,
                    "start_at": a.get("due_at"),
                    "description": a.get("description", ""),
                })

    # Also check Canvas calendar events
    exam_events = canvas_client.get_calendar_events(course_ids)
    logger.info(f"Calendar events found: {len(exam_events)}")
    for event in exam_events:
        course_id_raw = event.get("context_code", "").replace("course_", "")
        course_name = next((c["name"] for c in courses if str(c["id"]) == str(course_id_raw)), "")
        db.upsert_exam({
            "canvas_id": str(event.get("id", "")),
            "course_id": course_id_raw,
            "course_name": course_name,
            "title": event.get("title", "Exam"),
            "start_at": event.get("start_at"),
            "description": event.get("description", ""),
        })

    # Syllabi
    _sync_syllabi(courses)

    # Clean up exam entries that slipped through old/looser keyword matching
    _clean_bad_exams()

    # Analyze using batch calls (up to 15 per API request)
    all_to_analyze = new_for_analysis + needs_reanalysis
    if all_to_analyze:
        BATCH_SIZE = 15
        chunks = [all_to_analyze[i:i + BATCH_SIZE] for i in range(0, len(all_to_analyze), BATCH_SIZE)]
        logger.info(f"Analyzing {len(all_to_analyze)} assignments in {len(chunks)} batch(es) of up to {BATCH_SIZE}...")
        for batch_num, chunk in enumerate(chunks, start=1):
            logger.info(f"  Batch {batch_num}/{len(chunks)}: {len(chunk)} assignments")
            # Build syllabus_rules_map for this chunk (course_id -> rules)
            rules_map = {}
            for a in chunk:
                cid = str(a["course_id"])
                if cid not in rules_map:
                    rules_map[cid] = _get_syllabus_rules(cid)
            try:
                analyses = analyzer.analyze_assignments_batch(chunk, rules_map)
                for a, analysis in zip(chunk, analyses):
                    db.store_analysis(a["id"], analysis)
                    logger.info(f"    OK: {a['title']} | diff={analysis.get('difficulty')} "
                                f"hrs={analysis.get('estimated_hours')} priority={analysis.get('priority')}")
            except Exception as e:
                logger.warning(f"  Batch {batch_num} failed entirely: {e}")
            if batch_num < len(chunks):
                time.sleep(5)  # brief pause between batches to respect rate limits

    # Analyze new exams (rate limited)
    for exam in db.get_upcoming_exams(days_ahead=60):
        if not exam.get("analysis_json"):
            try:
                rules = _get_syllabus_rules(exam["course_id"])
                analysis = analyzer.analyze_exam(exam, rules, exam.get("course_name", ""))
                db.store_exam_analysis(exam["id"], analysis)
                logger.info(f"  Exam OK: {exam['title']}")
            except Exception as e:
                logger.warning(f"  Exam skipped {exam['title']}: {e}")
            time.sleep(5)

    logger.info("Canvas sync complete.")


def _sync_syllabi(courses):
    for course in courses:
        cid = course["id"]
        cname = course.get("name", "")
        files = canvas_client.get_course_files(cid)

        for f in files:
            fname = f.get("display_name", "")
            if not canvas_client.is_syllabus_file(fname):
                continue
            url = f.get("url") or f.get("download_url", "")
            if not url:
                continue

            raw = canvas_client.download_file(url)
            if not raw:
                continue

            new_hash = syllabus.hash_content(raw)
            if new_hash == db.get_syllabus_hash(str(cid), fname):
                continue  # unchanged

            content = syllabus.parse_pdf(raw) if fname.lower().endswith(".pdf") else raw.decode("utf-8", errors="replace")
            content = syllabus.truncate_for_llm(content)
            if not content.strip():
                continue

            logger.info(f"Ingesting syllabus: {cname} — {fname}")
            rules = analyzer.extract_syllabus_rules(content, cname)
            db.upsert_syllabus(str(cid), fname, new_hash, content, rules)


def _get_syllabus_rules(course_id):
    syllabi = db.get_syllabus(str(course_id))
    rules = {}
    for s in syllabi:
        try:
            r = json.loads(s["rules_json"]) if s.get("rules_json") else {}
            rules.update(r)
        except Exception:
            pass
    return rules


def _clean_bad_exams():
    """Remove exam_events that don't match current keyword rules (e.g. old quiz/practice entries)."""
    BAD_PATTERNS = ["quiz", "practice", "review", "prep", "sample", "example"]
    conn = db.get_conn()
    exams = conn.execute("SELECT id, title FROM exam_events").fetchall()
    removed = 0
    for e in exams:
        title = (e["title"] or "").lower()
        if any(p in title for p in BAD_PATTERNS):
            conn.execute("DELETE FROM exam_events WHERE id=?", (e["id"],))
            removed += 1
    conn.commit()
    conn.close()
    if removed:
        logger.info(f"Cleaned {removed} non-exam entries from exam_events table.")


# --- Notifications ---

def check_and_notify():
    if not is_within_active_hours():
        return

    if not Config.sms_enabled():
        return  # SMS not configured, skip

    from modules import notifier

    assignments = db.get_all_active_assignments()
    exams = db.get_upcoming_exams(days_ahead=21)

    collision_report = analyzer.detect_workload_collisions(assignments, exams)
    for collision in collision_report.get("collisions", []):
        if collision.get("severity") == "critical":
            col_key = f"collision_{collision.get('window','')}"
            if not db.get_last_notification_time(col_key, "collision"):
                notifier.send_collision_alert(collision)
                db.log_notification(col_key, "system", "collision", str(collision))

    for a in assignments:
        should_notify, reason = should_send_start_reminder(a)
        if should_notify:
            logger.info(f"SMS reminder: {a['title']} — {reason}")
            notifier.send_start_reminder(a)

        in_window, bonus_deadline = get_early_bonus_window(a)
        if in_window and bonus_deadline:
            if not db.get_last_notification_time(a["id"], "early_bonus"):
                hours_left = (bonus_deadline - datetime.now()).total_seconds() / 3600
                msg = (f"Early bonus window: {a.get('course_name','')} - {a['title']}\n"
                       f"Submit in the next {int(hours_left)}h for bonus points.")
                notifier.send(msg, log_item_id=a["id"], log_item_type="assignment",
                              log_notif_type="early_bonus")

        if should_send_check_in(a):
            notifier.send_check_in(a)


def send_daily_digest():
    if not Config.sms_enabled():
        logger.info("SMS not configured, skipping digest.")
        return

    from modules import notifier

    assignments = db.get_upcoming_assignments(days_ahead=14)
    exams = db.get_upcoming_exams(days_ahead=30)
    collision_report = analyzer.detect_workload_collisions(assignments, exams)
    message = analyzer.generate_weekly_digest(assignments, exams, collision_report)
    notifier.send_digest(message)
    logger.info("Daily digest sent.")


# --- Main ---

def main():
    missing = Config.validate()
    if missing:
        print("\nHermes needs these credentials in .env before starting:")
        for m in missing:
            print(f"  - {m}")
        print("\nSee .env.example for instructions.")
        sys.exit(1)

    db.init_db()
    logger.info("Hermes initializing...")

    if Config.sms_enabled():
        from modules import notifier
        notifier.send("Hermes is online. Dashboard: http://localhost:5000")
        logger.info("SMS alerts enabled.")
    else:
        logger.info("SMS not configured — running in dashboard-only mode.")

    # Flask's reloader spawns a monitor process + a worker process.
    # Only start background jobs in the worker (WERKZEUG_RUN_MAIN=true) or
    # when running without the reloader at all (env var absent).
    is_reloader_worker = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    reloader_active = "WERKZEUG_RUN_MAIN" in os.environ
    if is_reloader_worker or not reloader_active:
        # Initial Canvas sync in background thread so web UI starts immediately
        sync_thread = threading.Thread(target=sync_canvas, daemon=True)
        sync_thread.start()

        # Scheduler
        scheduler = BackgroundScheduler(timezone="America/New_York")
        scheduler.add_job(sync_canvas, CronTrigger(hour=14, minute=0), id="sync_afternoon")
        scheduler.add_job(sync_canvas, CronTrigger(hour=20, minute=0), id="sync_evening")
        scheduler.add_job(send_daily_digest, CronTrigger(hour=Config.DIGEST_HOUR, minute=5), id="digest")
        scheduler.add_job(check_and_notify, "interval", minutes=30, id="notifications")
        scheduler.start()
        logger.info(f"Scheduler running. Canvas sync at 2pm + 8pm. Digest at {Config.DIGEST_HOUR}:05.")

    logger.info(f"Starting web dashboard at http://localhost:{Config.WEB_PORT}")

    # Web UI runs in main thread (blocking); use_reloader watches .py files
    # and auto-restarts the server whenever you save a change.
    from web.app import run as run_web
    run_web(port=Config.WEB_PORT, use_reloader=True)


if __name__ == "__main__":
    main()
