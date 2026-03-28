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
    """)

    conn.commit()
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

def get_courses():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM courses WHERE is_active=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def set_course_piazza(course_id, nid):
    conn = get_conn()
    conn.execute("UPDATE courses SET piazza_nid=? WHERE id=?", (nid, str(course_id)))
    conn.commit()
    conn.close()

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
                 points_possible, submission_types, html_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (assignment_id, data["canvas_id"], data["course_id"], data.get("course_name",""),
              data["title"], data.get("description",""), data.get("due_at"),
              data.get("points_possible"), data.get("submission_types",""), data.get("html_url","")))

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
        SELECT * FROM assignments
        WHERE due_at IS NOT NULL
          AND due_at >= datetime('now')
          AND due_at <= datetime('now', ? || ' days')
          AND status NOT IN ('complete', 'submitted')
        ORDER BY due_at ASC
    """, (str(days_ahead),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_active_assignments():
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM assignments
        WHERE due_at IS NOT NULL AND due_at >= datetime('now')
          AND status NOT IN ('complete', 'submitted')
        ORDER BY due_at ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

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

def message_already_processed(twilio_sid):
    conn = get_conn()
    row = conn.execute("SELECT id FROM messages WHERE twilio_sid=?", (twilio_sid,)).fetchone()
    conn.close()
    return row is not None

# --- Time Logs ---

def log_time(assignment_id, started_at, ended_at, actual_hours):
    conn = get_conn()
    conn.execute("""
        INSERT INTO time_logs (assignment_id, started_at, ended_at, actual_hours)
        VALUES (?, ?, ?, ?)
    """, (assignment_id, started_at, ended_at, actual_hours))
    conn.commit()
    conn.close()

def get_time_logs_for_assignment(assignment_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM time_logs WHERE assignment_id=?", (assignment_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

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
