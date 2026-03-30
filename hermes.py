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
from modules import canvas_client, analyzer, syllabus, piazza_client
from modules.scheduler_engine import (
    should_send_start_reminder, should_send_check_in,
    is_within_active_hours, get_early_bonus_window
)

# Assignment title keywords for exam detection
EXAM_KEYWORDS = ["exam", "midterm", "final"]
EXAM_EXCLUSIONS = ["practice", "review", "prep", "sample", "example", "study guide"]


def _looks_like_exam(title: str) -> bool:
    """True if an assignment title looks like an actual exam (not a practice/review)."""
    t = title.lower()
    if any(ex in t for ex in EXAM_EXCLUSIONS):
        return False
    return any(kw in t for kw in EXAM_KEYWORDS)


def _is_default_analysis(analysis_json: str) -> bool:
    """True if stored analysis is just the error fallback defaults (difficulty=5, hours=2.0).
    Used to identify assignments that need to be re-analyzed with real AI output."""
    try:
        a = json.loads(analysis_json)
        return a.get("difficulty") == 5 and a.get("estimated_hours") == 2.0
    except Exception:
        return False

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

def _extract_rubric(rubric_data: list) -> str:
    """Convert Canvas rubric criteria list to readable text for AI context."""
    if not rubric_data:
        return ""
    lines = []
    for criterion in rubric_data:
        desc = criterion.get("description", "")
        pts = criterion.get("points", "")
        long_desc = criterion.get("long_description", "")
        line = f"- {desc} ({pts} pts)"
        if long_desc:
            line += f": {long_desc[:200]}"
        lines.append(line)
    return "\n".join(lines)[:1500]


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
        # Store term name so we can filter out past-term courses in the UI
        term = course.get("term") or {}
        term_name = term.get("name", "")
        if term_name:
            try:
                conn = db.get_conn()
                conn.execute("UPDATE courses SET term_name=? WHERE id=?", (term_name, str(cid)))
                conn.commit()
                conn.close()
            except Exception:
                pass
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
                "lock_at": a.get("lock_at"),  # when Canvas closes submissions
                "points_possible": a.get("points_possible"),
                "submission_types": ",".join(a.get("submission_types", [])),
                "html_url": a.get("html_url", ""),
                "rubric_text": _extract_rubric(a.get("rubric") or []),
            }
            assignment_id = db.upsert_assignment(data)
            db.set_assignment_canvas_group(assignment_id, a.get("assignment_group_id"))

            # Auto-mark submitted/graded items from Canvas submission data
            submission = a.get("submission") or {}
            canvas_state = submission.get("workflow_state", "")
            if canvas_state in ("submitted", "graded", "complete"):
                db.update_assignment_status(assignment_id, "submitted")

            existing = db.get_assignment_by_id(assignment_id)
            if existing:
                if not existing.get("analysis_json"):
                    # Never analyzed — queue regardless of submission status
                    new_for_analysis.append(existing)
                elif existing.get("status") not in ("submitted", "complete") and _is_default_analysis(existing["analysis_json"]):
                    # Has fallback/default analysis and still pending — re-analyze
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

    # Syllabi (PDFs + HTML from Canvas files)
    _sync_syllabi(courses)

    # Assignment groups (grade weights from Canvas)
    _sync_assignment_groups(courses)

    # Canvas pages/modules (often contain assignment details not in files)
    _sync_course_pages(courses)

    # Announcements
    _sync_announcements(courses)

    # Grades — auto-sync from Canvas submissions
    _sync_grades(courses)

    # Piazza posts → course materials + announcements
    _sync_piazza(courses)

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
            # Build syllabus_rules_map, course_materials_map, course_groups_map for this chunk
            rules_map = {}
            materials_map = {}
            course_notes_map = {}
            groups_map = {}
            for a in chunk:
                cid = str(a["course_id"])
                if cid not in rules_map:
                    rules_map[cid] = _get_syllabus_rules(cid)
                if cid not in materials_map:
                    materials_map[cid] = _get_course_materials_dict(cid)
                if cid not in course_notes_map:
                    course_notes_map[cid] = db.get_course_notes(cid)
                if cid not in groups_map:
                    groups_map[cid] = db.get_assignment_groups(cid)
            try:
                analyses = analyzer.analyze_assignments_batch(chunk, rules_map, materials_map, course_notes_map, groups_map)
                stored = 0
                all_rate_limited = True
                for a, analysis in zip(chunk, analyses):
                    if analysis.get("_rate_limited"):
                        continue  # don't store placeholder — will retry on next sync
                    all_rate_limited = False
                    db.store_analysis(a["id"], analysis)
                    stored += 1
                    logger.info(f"    OK: {a['title']} | diff={analysis.get('difficulty')} "
                                f"hrs={analysis.get('estimated_hours')} priority={analysis.get('priority')}")
                if stored < len(chunk):
                    logger.warning(f"  Batch {batch_num}: {len(chunk)-stored} skipped (rate limited)")
                if all_rate_limited:
                    logger.warning("  Both providers rate-limited — aborting analysis queue until next sync.")
                    break
            except Exception as e:
                logger.warning(f"  Batch {batch_num} failed entirely: {e}")
            if batch_num < len(chunks):
                time.sleep(15)  # pause between batches to respect rate limits

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

    db.set_pref("last_sync_time", datetime.now().isoformat())
    logger.info("Canvas sync complete.")

    # Course notes run after analysis so they don't block the analysis queue
    _sync_course_notes(courses)


def _sync_syllabi(courses):
    import re as _re
    for course in courses:
        cid = course["id"]
        cname = course.get("name", "")
        logger.info(f"Checking files for {cname} (course {cid})...")
        files = canvas_client.get_course_files(cid)
        logger.info(f"  {cname}: found {len(files)} files total")
        for f in files:
            fname = f.get("display_name", "")
            is_pdf = fname.lower().endswith(".pdf")
            is_html_text = f.get("content-type", "").startswith("text/")
            if not (is_pdf or is_html_text):
                continue  # Only ingest readable files

            url = f.get("url") or f.get("download_url", "")
            if not url:
                continue

            raw = canvas_client.download_file(url)
            if not raw:
                continue

            new_hash = syllabus.hash_content(raw)
            if new_hash == db.get_syllabus_hash(str(cid), fname):
                continue  # unchanged

            content = syllabus.parse_pdf(raw) if is_pdf else raw.decode("utf-8", errors="replace")
            content = syllabus.truncate_for_llm(content)
            if not content.strip():
                continue

            is_syl = canvas_client.is_syllabus_file(fname)
            if is_syl:
                logger.info(f"Ingesting syllabus: {cname} — {fname}")
                rules = analyzer.extract_syllabus_rules(content, cname)
            else:
                logger.info(f"Ingesting course material: {cname} — {fname}")
                rules = {}  # Don't run LLM rule extraction on every file — just store content

            db.upsert_syllabus(str(cid), fname, new_hash, content, rules)

        # Canvas built-in syllabus page
        html_body = canvas_client.get_course_syllabus_body(cid)
        if html_body and html_body.strip():
            fname = "__canvas_syllabus_page__"
            new_hash = syllabus.hash_content(html_body.encode("utf-8"))
            if new_hash != db.get_syllabus_hash(str(cid), fname):
                content = _re.sub(r'<[^>]+>', ' ', html_body)
                content = syllabus.truncate_for_llm(content)
                if content.strip():
                    logger.info(f"Ingesting Canvas syllabus page: {cname}")
                    rules = analyzer.extract_syllabus_rules(content, cname)
                    db.upsert_syllabus(str(cid), fname, new_hash, content, rules)


def _sync_course_pages(courses):
    """Ingest Canvas course pages (wiki pages / module pages).

    Many professors post assignment details, rubrics, and course info as Canvas
    pages rather than file uploads — especially important for courses with 0 files.
    Page content is stored in syllabi table so it flows into all future analyses.
    """
    import re as _re
    for course in courses:
        cid = course["id"]
        cname = course.get("name", "")
        try:
            pages = canvas_client.get_course_pages(cid)
            if not pages:
                continue
            logger.info(f"  {cname}: found {len(pages)} Canvas pages")
            for page in pages:
                page_url = page.get("url") or page.get("page_id", "")
                page_title = page.get("title", page_url)
                if not page_url:
                    continue
                fname = f"__page__{page_url}"
                # Fetch the page body
                body_html = canvas_client.get_page_content(cid, page_url)
                if not body_html or not body_html.strip():
                    continue
                new_hash = syllabus.hash_content(body_html.encode("utf-8"))
                if new_hash == db.get_syllabus_hash(str(cid), fname):
                    continue  # unchanged since last sync
                # Strip HTML tags for plain text storage
                content = _re.sub(r'<[^>]+>', ' ', body_html)
                content = _re.sub(r'&\w+;', ' ', content)
                content = _re.sub(r'\s+', ' ', content).strip()
                content = syllabus.truncate_for_llm(content)
                if not content or len(content) < 50:
                    continue
                logger.info(f"  Ingesting page: {cname} — {page_title}")
                db.upsert_syllabus(str(cid), fname, new_hash, content, {})
        except Exception as e:
            logger.warning(f"Page sync failed for {cname}: {e}")


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


def _get_course_materials(course_id, max_chars=1200):
    """Return content snippets from non-syllabus course files (e.g. homework PDFs)."""
    syllabi = db.get_syllabus(str(course_id))
    snippets = []
    for s in syllabi:
        fname = s.get("file_name", "")
        if fname.startswith("__"):
            continue  # Skip internal keys
        if canvas_client.is_syllabus_file(fname):
            continue  # Syllabus already included via rules
        content = s.get("content", "")
        if content and len(content.strip()) > 100:
            snippets.append(f"[{fname}]\n{content[:500].strip()}")
    return "\n\n".join(snippets)[:max_chars]


def _get_course_materials_dict(course_id: str) -> dict:
    """Return {filename: full_content} for non-syllabus course materials."""
    syllabi = db.get_syllabus(str(course_id))
    result = {}
    for s in syllabi:
        fname = s.get("file_name", "")
        if fname.startswith("__"):
            continue
        if canvas_client.is_syllabus_file(fname):
            continue
        content = s.get("content", "")
        if content and len(content.strip()) > 100:
            result[fname] = content
    return result


def _sync_assignment_groups(courses):
    """Fetch Canvas assignment groups (grade weights) for each course."""
    for c in courses:
        cid = str(c.get("id", ""))
        canvas_id = c.get("id")
        if not canvas_id:
            continue
        try:
            groups = canvas_client.get_assignment_groups(canvas_id)
            for g in groups:
                gid = g.get("id")
                name = g.get("name", "")
                weight = g.get("group_weight") or 0.0
                if gid:
                    db.upsert_assignment_group(cid, gid, name, weight)
            if groups:
                logger.info(f"  {c['name']}: synced {len(groups)} assignment groups")
        except Exception as e:
            logger.warning(f"  Assignment group sync failed for {c['name']}: {e}")


def _sync_piazza(courses):
    """Sync Piazza posts for any course that has a piazza_nid configured.

    Instructor posts → stored as announcements.
    All posts (especially assignment-detail notes) → stored as course materials
    in the syllabi table so they feed into analysis prompts.

    Auto-maps the env PIAZZA_NETWORK_ID to the matching course if piazza_nid
    isn't set yet.
    """
    # Config.PIAZZA_NETWORK_ID from .env, fall back to db pref set via Settings UI
    nid = Config.PIAZZA_NETWORK_ID or db.get_pref("piazza_network_id") or ""
    # Need email+password configured, and at least one nid source
    if not (Config.PIAZZA_EMAIL and Config.PIAZZA_PASSWORD and nid):
        return

    # Find which course this network belongs to
    target_course = None
    for c in courses:
        if c.get("piazza_nid") == nid:
            target_course = c
            break

    # Auto-detect: match by course code hint or pick the one with fewest files
    if not target_course:
        piazza_code = os.getenv("PIAZZA_COURSE_CODE", "2421")
        for c in courses:
            if piazza_code in c.get("name", "") or piazza_code in c.get("code", ""):
                target_course = c
                db.set_course_piazza_nid(str(c["id"]), nid)
                logger.info(f"Auto-mapped Piazza network {nid} → {c['name']}")
                break

    if not target_course:
        logger.warning("Could not map PIAZZA_NETWORK_ID to any active course. "
                       "Set PIAZZA_COURSE_CODE in .env (e.g. PIAZZA_COURSE_CODE=2421)")
        return

    cid = str(target_course["id"])
    cname = target_course.get("name", "")
    logger.info(f"Syncing Piazza for {cname}...")

    posts = piazza_client.get_posts(nid, limit=100)
    if not posts:
        return

    instructors_notes = 0
    materials_stored = 0

    for post in posts:
        subject = post.get("subject", "") or ""
        content = post.get("content", "") or ""
        instructor_answer = post.get("instructor_answer", "") or ""
        post_id = post.get("id", "")
        created = post.get("created", "")

        # Instructor notes → announcements feed
        if post.get("post_type") == "note" or "instructor" in " ".join(post.get("tags", [])).lower():
            canvas_id = f"piazza_{post_id}"
            if subject or content:
                db.upsert_announcement(canvas_id, cid, cname, subject, content, created)
                instructors_notes += 1

        # Every post with real content → course material so Hermes can reference it
        full_text = subject
        if content:
            full_text += f"\n{content}"
        if instructor_answer:
            full_text += f"\nInstructor answer: {instructor_answer}"

        if len(full_text.strip()) > 50:
            fname = f"__piazza__{post_id}"
            new_hash = syllabus.hash_content(full_text.encode("utf-8"))
            if new_hash != db.get_syllabus_hash(cid, fname):
                truncated = syllabus.truncate_for_llm(full_text)
                db.upsert_syllabus(cid, fname, new_hash, truncated, {})
                materials_stored += 1

    db.set_pref("last_piazza_sync", datetime.now().isoformat())
    logger.info(f"Piazza sync for {cname}: {instructors_notes} announcements, {materials_stored} new/updated posts")


def _sync_course_notes(courses):
    """Hermes's learn-and-grow mechanism.

    After each sync, generate a dense strategic summary per course (grading weights,
    assignment patterns, what causes point deductions, etc.) and store it in the DB.
    These notes are then injected into every future batch analysis and chat prompt,
    so Hermes gets smarter about each course over time as more data arrives.

    Also generates AI-suggested target grades for each course (shown on Grades page).
    """
    courses_for_targets = []

    for course in courses:
        cid = str(course["id"])
        cname = course.get("name", "")

        # Skip if notes already exist (regenerate only if we have new data)
        existing_notes = db.get_course_notes(cid)
        syllabi = db.get_syllabus(cid)
        if existing_notes and not syllabi:
            continue  # No syllabus to learn from, keep existing notes

        # Build context for note generation
        rules_map = _get_syllabus_rules(cid)
        materials = _get_course_materials(cid)
        grades = db.get_grades_for_course(cid)
        canvas_grade = course.get("canvas_grade_pct")

        syllabus_content = ""
        for s in syllabi[:2]:
            if s.get("content"):
                syllabus_content += s["content"][:800]

        if not syllabus_content and not materials and not grades:
            continue  # Nothing to generate notes from

        grade_context = ""
        if canvas_grade is not None:
            grade_context = f"Current grade: {canvas_grade:.1f}%"
        elif grades:
            valid = [g["grade_pct"] for g in grades if g.get("grade_pct") is not None]
            if valid:
                avg = sum(valid) / len(valid)
                grade_context = f"Current avg from {len(valid)} graded assignments: {avg:.1f}%"

        try:
            notes = analyzer.generate_course_notes(
                cname, syllabus_content or materials, rules_map, grade_context
            )
            if notes:
                db.set_course_notes(cid, notes)
                logger.info(f"Course notes generated for {cname}")
        except Exception as e:
            logger.warning(f"Course notes generation failed for {cname}: {e}")

        # Collect data for batch grade target suggestions
        remaining = [a for a in db.get_upcoming_assignments(days_ahead=90)
                     if str(a.get("course_id")) == cid and a.get("status") not in ("submitted", "complete")]
        remaining_hours = sum(a.get("estimated_hours") or 2.0 for a in remaining)
        gw = rules_map.get("grading_weights", {})
        courses_for_targets.append({
            "id": cid,
            "name": cname,
            "current_grade": canvas_grade,
            "remaining_count": len(remaining),
            "remaining_hours": round(remaining_hours, 1),
            "course_notes": db.get_course_notes(cid),
            "grading_weights": json.dumps(gw) if gw else "unknown",
        })

    # Batch-generate grade target suggestions
    if courses_for_targets:
        try:
            suggestions = analyzer.generate_grade_targets(courses_for_targets)
            for course_data, suggestion in zip(courses_for_targets, suggestions):
                target = suggestion.get("suggested_target")
                reasoning = suggestion.get("reasoning", "")
                if target is not None:
                    db.set_grade_target_suggestion(course_data["id"], float(target), reasoning)
                    logger.info(f"Grade target suggestion: {course_data['name']} → {target}%")
        except Exception as e:
            logger.warning(f"Grade target generation failed: {e}")

    # Generate week synthesis
    try:
        upcoming = db.get_upcoming_assignments(days_ahead=14)
        exams_soon = db.get_upcoming_exams(days_ahead=14)
        synthesis = analyzer.generate_week_synthesis(upcoming, exams_soon)
        if synthesis:
            db.set_pref("week_synthesis", synthesis)
            logger.info("Week synthesis updated.")
    except Exception as e:
        logger.warning(f"Week synthesis failed: {e}")


def _sync_grades(courses):
    """Auto-sync graded submission scores and overall course grade from Canvas."""
    total_grades = 0
    for course in courses:
        cid = course["id"]
        try:
            # Per-assignment grades
            submissions = canvas_client.get_course_submissions(cid)
            for sub in submissions:
                canvas_asgn_id = sub.get("assignment_id")
                score = sub.get("score")
                asgn = sub.get("assignment") or {}
                pts_possible = asgn.get("points_possible")
                if canvas_asgn_id and score is not None and pts_possible:
                    local_id = db.get_assignment_id_by_canvas_id(canvas_asgn_id)
                    if local_id:
                        db.upsert_grade(local_id, str(cid), score, pts_possible)
                        total_grades += 1
            # Overall course grade
            current_grade = canvas_client.get_course_current_grade(cid)
            if current_grade is not None:
                db.set_canvas_course_grade(str(cid), current_grade)
        except Exception as e:
            logger.warning(f"Grade sync failed for course {cid}: {e}")
    logger.info(f"Grade sync complete: {total_grades} assignment grades updated.")


def _sync_announcements(courses):
    """Sync recent announcements from Canvas for all active courses."""
    total = 0
    for course in courses:
        cid = course["id"]
        cname = course.get("name", "")
        try:
            items = canvas_client.get_announcements(cid)
            for item in items:
                canvas_id = str(item.get("id", ""))
                title = item.get("title", "")
                message = item.get("message", "") or ""
                posted_at = item.get("posted_at") or item.get("created_at", "")
                if canvas_id and title:
                    db.upsert_announcement(canvas_id, str(cid), cname, title, message, posted_at)
                    total += 1
        except Exception as e:
            logger.warning(f"Announcement sync failed for {cname}: {e}")
    logger.info(f"Announcements synced: {total} items across {len(courses)} courses.")


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

    # Disable the werkzeug file-watcher reloader in production.
    # The reloader spawns a MONITOR process + a WORKER process — both would
    # independently start syncs and schedulers, doubling every AI API call and
    # burning through Gemini quota. launchd handles process management instead.
    dev_mode = os.environ.get("HERMES_DEV", "").lower() in ("1", "true", "yes")

    # Only the single production process (or the werkzeug worker) runs background jobs.
    werkzeug_worker = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    run_bg = not dev_mode or werkzeug_worker

    if run_bg:
        last_sync = db.get_pref("last_sync_time")
        sync_age_hours = None
        if last_sync:
            try:
                sync_age_hours = (datetime.now() - datetime.fromisoformat(last_sync)).total_seconds() / 3600
            except Exception:
                pass

        if sync_age_hours is None or sync_age_hours >= 2.0:
            sync_thread = threading.Thread(target=sync_canvas, daemon=True)
            sync_thread.start()
        else:
            logger.info(f"Skipping startup sync — last sync was {sync_age_hours:.1f}h ago (< 2h cooldown).")

        # Scheduler — sync hours configurable via DB prefs (set from Settings page)
        sync_hour_1 = int(db.get_pref("sync_hour_1") or 14)
        sync_hour_2 = int(db.get_pref("sync_hour_2") or 20)
        scheduler = BackgroundScheduler(timezone="America/New_York")
        scheduler.add_job(sync_canvas, CronTrigger(hour=sync_hour_1, minute=0), id="sync_afternoon")
        scheduler.add_job(sync_canvas, CronTrigger(hour=sync_hour_2, minute=0), id="sync_evening")
        scheduler.add_job(send_daily_digest, CronTrigger(hour=Config.DIGEST_HOUR, minute=5), id="digest")
        scheduler.add_job(check_and_notify, "interval", minutes=30, id="notifications")
        scheduler.start()
        logger.info(f"Scheduler running. Canvas sync at 2pm + 8pm. Digest at {Config.DIGEST_HOUR}:05.")

    logger.info(f"Starting web dashboard at http://localhost:{Config.WEB_PORT}")
    from web.app import run as run_web
    run_web(port=Config.WEB_PORT, use_reloader=dev_mode)


if __name__ == "__main__":
    main()
