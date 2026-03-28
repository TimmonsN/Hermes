import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "hermes.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS courses (
            id TEXT PRIMARY KEY,
            name TEXT,
            code TEXT,
            canvas_id INTEGER UNIQUE,
            piazza_nid TEXT,
            is_active INTEGER DEFAULT 1,
            grading_weights TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS syllabi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id TEXT,
            file_name TEXT,
            file_hash TEXT,
            content TEXT,
            rules_json TEXT,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(course_id, file_name)
        );

        CREATE TABLE IF NOT EXISTS assignments (
            id TEXT PRIMARY KEY,
            canvas_id INTEGER UNIQUE,
            course_id TEXT,
            course_name TEXT,
            title TEXT,
            description TEXT,
            due_at TIMESTAMP,
            points_possible REAL,
            submission_types TEXT,
            html_url TEXT,
            difficulty INTEGER,
            estimated_hours REAL,
            start_by TIMESTAMP,
            priority TEXT DEFAULT 'medium',
            has_early_bonus INTEGER DEFAULT 0,
            early_bonus_details TEXT,
            can_resubmit INTEGER DEFAULT 0,
            resubmit_details TEXT,
            analysis_json TEXT,
            status TEXT DEFAULT 'pending',
            notified_start INTEGER DEFAULT 0,
            notified_urgent INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS exam_events (
            id TEXT PRIMARY KEY,
            canvas_id TEXT UNIQUE,
            course_id TEXT,
            course_name TEXT,
            title TEXT,
            start_at TIMESTAMP,
            description TEXT,
            study_hours_estimated REAL,
            start_study_by TIMESTAMP,
            analysis_json TEXT,
            notified INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT,
            item_type TEXT,
            notif_type TEXT,
            message TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT,
            body TEXT,
            twilio_sid TEXT UNIQUE,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS time_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id TEXT,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            actual_hours REAL
        );

        CREATE TABLE IF NOT EXISTS preferences (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id TEXT,
            course_id TEXT,
            points_earned REAL,
            points_possible REAL,
            grade_pct REAL,
            entered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS course_grade_goals (
            course_id TEXT PRIMARY KEY,
            target_grade_pct REAL DEFAULT 90.0
        );

        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canvas_id TEXT UNIQUE,
            course_id TEXT,
            course_name TEXT,
            title TEXT,
            message TEXT,
            posted_at TIMESTAMP,
            is_read INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS assignment_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id TEXT UNIQUE,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS assignment_checklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id TEXT,
            item_text TEXT,
            is_done INTEGER DEFAULT 0,
            position INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS study_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            assignment_id TEXT,
            hours_planned REAL,
            note TEXT,
            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS time_spent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id TEXT,
            minutes INTEGER,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT,
            date TEXT,
            calls INTEGER DEFAULT 0,
            UNIQUE(provider, date)
        );
    """)

    conn.commit()

    # Add columns that may not exist in older DBs
    for migration in [
        "ALTER TABLE courses ADD COLUMN is_ignored INTEGER DEFAULT 0",
        "ALTER TABLE courses ADD COLUMN canvas_grade_pct REAL",
        "ALTER TABLE courses ADD COLUMN course_notes TEXT",
        "ALTER TABLE assignments ADD COLUMN rubric_text TEXT",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass  # column already exists

    conn.close()

# --- Courses ---

def upsert_course(canvas_id, name, code):
    conn = get_conn()
    c = conn.cursor()
    course_id = str(canvas_id)
    c.execute("""
        INSERT INTO courses (id, name, code, canvas_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(canvas_id) DO UPDATE SET name=excluded.name, code=excluded.code
    """, (course_id, name, code, canvas_id))
    conn.commit()
    conn.close()
    return course_id

def get_courses(include_ignored=False):
    conn = get_conn()
    if include_ignored:
        rows = conn.execute("SELECT * FROM courses WHERE is_active=1").fetchall()
    else:
        rows = conn.execute("SELECT * FROM courses WHERE is_active=1 AND (is_ignored IS NULL OR is_ignored=0)").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def set_course_ignored(course_id, ignored: bool):
    conn = get_conn()
    conn.execute("UPDATE courses SET is_ignored=? WHERE id=?", (1 if ignored else 0, str(course_id)))
    conn.commit()
    conn.close()

def set_canvas_course_grade(course_id, grade_pct):
    conn = get_conn()
    conn.execute("UPDATE courses SET canvas_grade_pct=? WHERE id=?", (grade_pct, str(course_id)))
    conn.commit()
    conn.close()

def set_course_notes(course_id: str, notes: str):
    conn = get_conn()
    conn.execute("UPDATE courses SET course_notes=? WHERE id=?", (notes, str(course_id)))
    conn.commit()
    conn.close()

def get_course_notes(course_id: str) -> str:
    conn = get_conn()
    row = conn.execute("SELECT course_notes FROM courses WHERE id=?", (str(course_id),)).fetchone()
    conn.close()
    return row["course_notes"] if row and row["course_notes"] else ""

# --- Syllabi ---

def upsert_syllabus(course_id, file_name, file_hash, content, rules_json):
    conn = get_conn()
    conn.execute("""
        INSERT INTO syllabi (course_id, file_name, file_hash, content, rules_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(course_id, file_name) DO UPDATE SET
            file_hash=excluded.file_hash,
            content=excluded.content,
            rules_json=excluded.rules_json,
            ingested_at=CURRENT_TIMESTAMP
    """, (str(course_id), file_name, file_hash, content, json.dumps(rules_json)))
    conn.commit()
    conn.close()

def get_syllabus(course_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM syllabi WHERE course_id=?", (str(course_id),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_syllabus_hash(course_id, file_name):
    conn = get_conn()
    row = conn.execute("SELECT file_hash FROM syllabi WHERE course_id=? AND file_name=?",
                       (str(course_id), file_name)).fetchone()
    conn.close()
    return row["file_hash"] if row else None

# --- Assignments ---

def upsert_assignment(data: dict):
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute("SELECT id FROM assignments WHERE canvas_id=?", (data["canvas_id"],)).fetchone()

    if existing:
        rubric_text = data.get("rubric_text")
        if rubric_text is not None:
            c.execute("""
                UPDATE assignments SET
                    title=?, description=?, due_at=?, points_possible=?,
                    submission_types=?, html_url=?, course_name=?, rubric_text=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE canvas_id=?
            """, (data["title"], data.get("description",""), data.get("due_at"),
                  data.get("points_possible"), data.get("submission_types",""),
                  data.get("html_url",""), data.get("course_name",""), rubric_text,
                  data["canvas_id"]))
        else:
            c.execute("""
                UPDATE assignments SET
                    title=?, description=?, due_at=?, points_possible=?,
                    submission_types=?, html_url=?, course_name=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE canvas_id=?
            """, (data["title"], data.get("description",""), data.get("due_at"),
                  data.get("points_possible"), data.get("submission_types",""),
                  data.get("html_url",""), data.get("course_name",""), data["canvas_id"]))
        assignment_id = existing["id"]
    else:
        assignment_id = f"{data['course_id']}_{data['canvas_id']}"
        c.execute("""
            INSERT INTO assignments
                (id, canvas_id, course_id, course_name, title, description, due_at,
                 points_possible, submission_types, html_url, rubric_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (assignment_id, data["canvas_id"], data["course_id"], data.get("course_name",""),
              data["title"], data.get("description",""), data.get("due_at"),
              data.get("points_possible"), data.get("submission_types",""), data.get("html_url",""),
              data.get("rubric_text")))

    conn.commit()
    conn.close()
    return assignment_id

def store_analysis(assignment_id, analysis: dict):
    conn = get_conn()
    conn.execute("""
        UPDATE assignments SET
            difficulty=?, estimated_hours=?, start_by=?, priority=?,
            has_early_bonus=?, early_bonus_details=?,
            can_resubmit=?, resubmit_details=?,
            analysis_json=?, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (
        analysis.get("difficulty"), analysis.get("estimated_hours"),
        analysis.get("start_by"), analysis.get("priority","medium"),
        int(analysis.get("has_early_bonus", False)), analysis.get("early_bonus_details",""),
        int(analysis.get("can_resubmit", False)), analysis.get("resubmit_details",""),
        json.dumps(analysis), assignment_id
    ))
    conn.commit()
    conn.close()

def get_upcoming_assignments(days_ahead=14):
    conn = get_conn()
    rows = conn.execute("""
        SELECT a.* FROM assignments a
        LEFT JOIN courses c ON c.id = a.course_id
        WHERE a.due_at IS NOT NULL
          AND a.due_at >= datetime('now')
          AND a.due_at <= datetime('now', ? || ' days')
          AND a.status NOT IN ('complete', 'submitted')
          AND (c.is_ignored IS NULL OR c.is_ignored = 0)
        ORDER BY a.due_at ASC
    """, (str(days_ahead),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_active_assignments():
    conn = get_conn()
    rows = conn.execute("""
        SELECT a.* FROM assignments a
        LEFT JOIN courses c ON c.id = a.course_id
        WHERE a.due_at IS NOT NULL AND a.due_at >= datetime('now')
          AND a.status NOT IN ('complete', 'submitted')
          AND (c.is_ignored IS NULL OR c.is_ignored = 0)
        ORDER BY a.due_at ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_assignment_id_by_canvas_id(canvas_id):
    conn = get_conn()
    row = conn.execute("SELECT id FROM assignments WHERE canvas_id=?", (canvas_id,)).fetchone()
    conn.close()
    return row["id"] if row else None

def get_unanalyzed_assignments():
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM assignments
        WHERE analysis_json IS NULL
          AND due_at IS NOT NULL
          AND due_at >= datetime('now')
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_assignment_by_id(assignment_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def update_assignment_status(assignment_id, status):
    conn = get_conn()
    conn.execute("UPDATE assignments SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (status, assignment_id))
    conn.commit()
    conn.close()

def mark_notified(assignment_id, notif_field):
    conn = get_conn()
    conn.execute(f"UPDATE assignments SET {notif_field}=1 WHERE id=?", (assignment_id,))
    conn.commit()
    conn.close()

# --- Exam Events ---

def upsert_exam(data: dict):
    conn = get_conn()
    existing = conn.execute("SELECT id FROM exam_events WHERE canvas_id=?", (data["canvas_id"],)).fetchone()
    if existing:
        conn.execute("""
            UPDATE exam_events SET title=?, start_at=?, description=?, course_name=?
            WHERE canvas_id=?
        """, (data["title"], data.get("start_at"), data.get("description",""),
              data.get("course_name",""), data["canvas_id"]))
        exam_id = existing["id"]
    else:
        exam_id = f"exam_{data['course_id']}_{data['canvas_id']}"
        conn.execute("""
            INSERT INTO exam_events (id, canvas_id, course_id, course_name, title, start_at, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (exam_id, data["canvas_id"], data["course_id"], data.get("course_name",""),
              data["title"], data.get("start_at"), data.get("description","")))
    conn.commit()
    conn.close()
    return exam_id

def get_upcoming_exams(days_ahead=30):
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM exam_events
        WHERE start_at >= datetime('now')
          AND start_at <= datetime('now', ? || ' days')
        ORDER BY start_at ASC
    """, (str(days_ahead),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def store_exam_analysis(exam_id, analysis: dict):
    conn = get_conn()
    conn.execute("""
        UPDATE exam_events SET
            study_hours_estimated=?, start_study_by=?, analysis_json=?
        WHERE id=?
    """, (analysis.get("study_hours"), analysis.get("start_study_by"),
          json.dumps(analysis), exam_id))
    conn.commit()
    conn.close()

# --- Notifications ---

def log_notification(item_id, item_type, notif_type, message):
    conn = get_conn()
    conn.execute("""
        INSERT INTO notifications (item_id, item_type, notif_type, message)
        VALUES (?, ?, ?, ?)
    """, (item_id, item_type, notif_type, message))
    conn.commit()
    conn.close()

def get_last_notification_time(item_id, notif_type):
    conn = get_conn()
    row = conn.execute("""
        SELECT sent_at FROM notifications
        WHERE item_id=? AND notif_type=?
        ORDER BY sent_at DESC LIMIT 1
    """, (item_id, notif_type)).fetchone()
    conn.close()
    return row["sent_at"] if row else None

# --- Messages ---

def store_message(direction, body, twilio_sid=None):
    conn = get_conn()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO messages (direction, body, twilio_sid)
            VALUES (?, ?, ?)
        """, (direction, body, twilio_sid))
        conn.commit()
    except Exception:
        pass
    conn.close()

def get_last_inbound_time():
    conn = get_conn()
    row = conn.execute("""
        SELECT timestamp FROM messages WHERE direction='inbound'
        ORDER BY timestamp DESC LIMIT 1
    """).fetchone()
    conn.close()
    return row["timestamp"] if row else None

def get_recent_messages(limit=20):
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM messages ORDER BY timestamp DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return list(reversed([dict(r) for r in rows]))

# --- Preferences ---

def get_pref(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_pref(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO preferences (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


# --- Grades ---

def upsert_grade(assignment_id, course_id, points_earned, points_possible):
    conn = get_conn()
    grade_pct = (points_earned / points_possible * 100) if points_possible else None
    # Check if grade exists
    existing = conn.execute("SELECT id FROM grades WHERE assignment_id=?", (assignment_id,)).fetchone()
    if existing:
        conn.execute("""
            UPDATE grades SET points_earned=?, points_possible=?, grade_pct=?, entered_at=CURRENT_TIMESTAMP
            WHERE assignment_id=?
        """, (points_earned, points_possible, grade_pct, assignment_id))
    else:
        conn.execute("""
            INSERT INTO grades (assignment_id, course_id, points_earned, points_possible, grade_pct)
            VALUES (?, ?, ?, ?, ?)
        """, (assignment_id, course_id, points_earned, points_possible, grade_pct))
    conn.commit()
    conn.close()
    return grade_pct

def get_grade_for_assignment(assignment_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM grades WHERE assignment_id=?", (assignment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_grades_for_course(course_id):
    conn = get_conn()
    rows = conn.execute("""
        SELECT g.*, a.title, a.points_possible as a_points_possible
        FROM grades g
        LEFT JOIN assignments a ON a.id = g.assignment_id
        WHERE g.course_id=?
        ORDER BY g.entered_at DESC
    """, (str(course_id),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_grades():
    conn = get_conn()
    rows = conn.execute("""
        SELECT g.*, a.title, a.course_name
        FROM grades g
        LEFT JOIN assignments a ON a.id = g.assignment_id
        ORDER BY g.entered_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def set_grade_goal(course_id, target_pct):
    conn = get_conn()
    conn.execute("""
        INSERT INTO course_grade_goals (course_id, target_grade_pct)
        VALUES (?, ?)
        ON CONFLICT(course_id) DO UPDATE SET target_grade_pct=excluded.target_grade_pct
    """, (str(course_id), target_pct))
    conn.commit()
    conn.close()

def get_grade_goal(course_id):
    conn = get_conn()
    row = conn.execute("SELECT target_grade_pct FROM course_grade_goals WHERE course_id=?", (str(course_id),)).fetchone()
    conn.close()
    return row["target_grade_pct"] if row else 90.0

def delete_grade(assignment_id):
    conn = get_conn()
    conn.execute("DELETE FROM grades WHERE assignment_id=?", (assignment_id,))
    conn.commit()
    conn.close()


# --- Announcements ---

def upsert_announcement(canvas_id, course_id, course_name, title, message, posted_at):
    conn = get_conn()
    conn.execute("""
        INSERT INTO announcements (canvas_id, course_id, course_name, title, message, posted_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(canvas_id) DO UPDATE SET
            title=excluded.title,
            message=excluded.message,
            course_name=excluded.course_name
    """, (str(canvas_id), str(course_id), course_name, title, message, posted_at))
    conn.commit()
    conn.close()

def get_announcements(limit=50):
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM announcements ORDER BY posted_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_announcements_for_course(course_id, limit=5):
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM announcements WHERE course_id=? ORDER BY posted_at DESC LIMIT ?
    """, (str(course_id), limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_unread_announcement_count():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) as cnt FROM announcements WHERE is_read=0").fetchone()
    conn.close()
    return row["cnt"] if row else 0

def mark_announcement_read(canvas_id):
    conn = get_conn()
    conn.execute("UPDATE announcements SET is_read=1 WHERE canvas_id=?", (str(canvas_id),))
    conn.commit()
    conn.close()

def mark_all_announcements_read():
    conn = get_conn()
    conn.execute("UPDATE announcements SET is_read=1")
    conn.commit()
    conn.close()


# --- Assignment Notes ---

def upsert_assignment_note(assignment_id, note):
    conn = get_conn()
    conn.execute("""
        INSERT INTO assignment_notes (assignment_id, note)
        VALUES (?, ?)
        ON CONFLICT(assignment_id) DO UPDATE SET note=excluded.note, updated_at=CURRENT_TIMESTAMP
    """, (assignment_id, note))
    conn.commit()
    conn.close()

def get_assignment_note(assignment_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM assignment_notes WHERE assignment_id=?", (assignment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# --- Assignment Checklist ---

def get_checklist(assignment_id):
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM assignment_checklist WHERE assignment_id=? ORDER BY position ASC, id ASC
    """, (assignment_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_checklist_item(assignment_id, item_text):
    conn = get_conn()
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), 0) as mp FROM assignment_checklist WHERE assignment_id=?",
        (assignment_id,)
    ).fetchone()["mp"]
    c = conn.execute("""
        INSERT INTO assignment_checklist (assignment_id, item_text, position)
        VALUES (?, ?, ?)
    """, (assignment_id, item_text, max_pos + 1))
    item_id = c.lastrowid
    conn.commit()
    conn.close()
    return item_id

def toggle_checklist_item(item_id):
    conn = get_conn()
    conn.execute("UPDATE assignment_checklist SET is_done = 1 - is_done WHERE id=?", (item_id,))
    conn.commit()
    conn.close()

def delete_checklist_item(item_id):
    conn = get_conn()
    conn.execute("DELETE FROM assignment_checklist WHERE id=?", (item_id,))
    conn.commit()
    conn.close()

def get_checklist_stats(assignment_id):
    conn = get_conn()
    row = conn.execute("""
        SELECT COUNT(*) as total, SUM(is_done) as done
        FROM assignment_checklist WHERE assignment_id=?
    """, (assignment_id,)).fetchone()
    conn.close()
    total = row["total"] or 0
    done = row["done"] or 0
    return {"total": total, "done": done, "pct": round(done / total * 100) if total > 0 else 0}


# --- Time Spent ---

def log_time_spent(assignment_id, minutes):
    conn = get_conn()
    conn.execute("INSERT INTO time_spent (assignment_id, minutes) VALUES (?, ?)", (assignment_id, minutes))
    conn.commit()
    conn.close()

def get_time_spent(assignment_id):
    conn = get_conn()
    row = conn.execute("SELECT COALESCE(SUM(minutes), 0) as total FROM time_spent WHERE assignment_id=?",
                       (assignment_id,)).fetchone()
    conn.close()
    return row["total"] if row else 0

def get_total_time_this_week():
    conn = get_conn()
    row = conn.execute("""
        SELECT COALESCE(SUM(minutes), 0) as total FROM time_spent
        WHERE logged_at >= datetime('now', '-7 days')
    """).fetchone()
    conn.close()
    return row["total"] if row else 0


# --- Study Plan ---

def save_study_plan(entries):
    """entries: list of dicts with date, assignment_id, hours_planned, note"""
    conn = get_conn()
    conn.execute("DELETE FROM study_plan")
    for e in entries:
        conn.execute("""
            INSERT INTO study_plan (date, assignment_id, hours_planned, note)
            VALUES (?, ?, ?, ?)
        """, (e["date"], e["assignment_id"], e["hours_planned"], e.get("note", "")))
    conn.commit()
    conn.close()

def get_study_plan():
    conn = get_conn()
    rows = conn.execute("""
        SELECT sp.*, a.title, a.course_name, a.due_at, a.priority, a.difficulty
        FROM study_plan sp
        LEFT JOIN assignments a ON a.id = sp.assignment_id
        ORDER BY sp.date ASC, sp.id ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# --- API Usage Tracking ---

def track_api_call(provider: str):
    """Increment today's call count for a given provider ('gemini' or 'groq')."""
    conn = get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("""
        INSERT INTO api_usage (provider, date, calls) VALUES (?, ?, 1)
        ON CONFLICT(provider, date) DO UPDATE SET calls = calls + 1
    """, (provider, today))
    conn.commit()
    conn.close()

def get_api_usage_today(provider: str) -> int:
    conn = get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT calls FROM api_usage WHERE provider=? AND date=?", (provider, today)
    ).fetchone()
    conn.close()
    return row["calls"] if row else 0

def get_api_usage_summary() -> dict:
    """Returns {provider: calls_today} for all tracked providers."""
    conn = get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT provider, calls FROM api_usage WHERE date=?", (today,)
    ).fetchall()
    conn.close()
    return {r["provider"]: r["calls"] for r in rows}


def get_semester_completed_count():
    conn = get_conn()
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM assignments
        WHERE status IN ('submitted', 'complete')
    """).fetchone()
    conn.close()
    return row["cnt"] if row else 0
