import requests
import logging
from config import Config

logger = logging.getLogger("hermes.canvas")

BASE = Config.CANVAS_BASE_URL.rstrip("/")
HEADERS = {"Authorization": f"Bearer {Config.CANVAS_TOKEN}"}

EXAM_KEYWORDS = [
    "exam", "midterm", "final", "quiz", "test", "assessment"
]

def _get(url, params=None):
    """GET with auto-pagination via Link header."""
    results = []
    while url:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return data
        # follow next page
        link = resp.headers.get("Link", "")
        url = None
        params = None  # params are encoded in the next URL
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                break
    return results

def get_active_courses():
    """Return list of active enrolled courses."""
    try:
        courses = _get(f"{BASE}/api/v1/courses", params={
            "enrollment_state": "active",
            "per_page": 50,
            "include[]": ["course_image", "term"]
        })
        return [c for c in courses if isinstance(c, dict) and not c.get("access_restricted_by_date")]
    except Exception as e:
        logger.error(f"Failed to fetch courses: {e}")
        return []

def get_assignments(course_id):
    """Return all assignments for a course."""
    try:
        assignments = _get(f"{BASE}/api/v1/courses/{course_id}/assignments", params={
            "per_page": 50,
            "include[]": ["submission", "rubric"],
            "order_by": "due_at"
        })
        return assignments if isinstance(assignments, list) else []
    except Exception as e:
        logger.error(f"Failed to fetch assignments for course {course_id}: {e}")
        return []

def get_course_files(course_id):
    """Return list of files for a course (used to find syllabi)."""
    try:
        files = _get(f"{BASE}/api/v1/courses/{course_id}/files", params={
            "per_page": 50,
        })
        return files if isinstance(files, list) else []
    except Exception as e:
        logger.error(f"Failed to fetch files for course {course_id}: {e}")
        return []

def get_course_pages(course_id):
    """Return list of pages for a course (syllabi sometimes live here)."""
    try:
        pages = _get(f"{BASE}/api/v1/courses/{course_id}/pages", params={"per_page": 50})
        return pages if isinstance(pages, list) else []
    except Exception as e:
        logger.error(f"Failed to fetch pages for course {course_id}: {e}")
        return []

def get_page_content(course_id, page_url):
    """Get body content of a Canvas page."""
    try:
        data = _get(f"{BASE}/api/v1/courses/{course_id}/pages/{page_url}")
        return data.get("body", "") if isinstance(data, dict) else ""
    except Exception as e:
        logger.error(f"Failed to fetch page {page_url}: {e}")
        return ""

def download_file(url) -> bytes:
    """Download raw file bytes (for PDFs)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.error(f"Failed to download file {url}: {e}")
        return b""

def get_calendar_events(course_ids):
    """Return calendar events (exams, important dates) for given course IDs."""
    try:
        context_codes = [f"course_{cid}" for cid in course_ids]
        params = {
            "per_page": 50,
            "type": "event",
            "start_date": "2025-01-01",
        }
        for code in context_codes:
            params[f"context_codes[]"] = code

        # Canvas requires repeated param keys — use a list of tuples
        param_list = [("per_page", 50), ("type", "event"), ("start_date", "2025-01-01")]
        for code in context_codes:
            param_list.append(("context_codes[]", code))

        resp = requests.get(f"{BASE}/api/v1/calendar_events",
                            headers=HEADERS, params=param_list, timeout=30)
        resp.raise_for_status()
        events = resp.json()

        # Filter to exam-like events
        exam_events = []
        for event in events:
            title = (event.get("title") or "").lower()
            desc = (event.get("description") or "").lower()
            if any(kw in title or kw in desc for kw in EXAM_KEYWORDS):
                exam_events.append(event)
        return exam_events
    except Exception as e:
        logger.error(f"Failed to fetch calendar events: {e}")
        return []

def is_syllabus_file(filename: str) -> bool:
    name = filename.lower()
    keywords = ["syllabus", "syllab", "course_info", "course info", "course overview",
                "course_overview", "class overview", "course guide", "course schedule",
                "course outline", "course_schedule", "class info"]
    return any(kw in name for kw in keywords)

def get_course_syllabus_body(course_id) -> str:
    """Fetch the Canvas built-in syllabus HTML for a course."""
    try:
        data = _get(f"{BASE}/api/v1/courses/{course_id}", params={"include[]": "syllabus_body"})
        return data.get("syllabus_body") or "" if isinstance(data, dict) else ""
    except Exception as e:
        logger.error(f"Failed to fetch syllabus body for course {course_id}: {e}")
        return ""

def get_course_submissions(course_id):
    """Get all graded submissions for the current user in a course."""
    try:
        subs = _get(f"{BASE}/api/v1/courses/{course_id}/students/submissions", params={
            "student_ids[]": "self",
            "include[]": ["assignment"],
            "per_page": 100,
        })
        return [s for s in (subs if isinstance(subs, list) else [])
                if s.get("score") is not None and s.get("assignment_id")]
    except Exception as e:
        logger.error(f"Failed to fetch submissions for course {course_id}: {e}")
        return []

def get_course_current_grade(course_id):
    """Get current overall score % for the student in this course."""
    try:
        enrollments = _get(f"{BASE}/api/v1/courses/{course_id}/enrollments", params={
            "user_id": "self",
            "per_page": 5,
        })
        for e in (enrollments if isinstance(enrollments, list) else []):
            if e.get("type") == "StudentEnrollment":
                g = e.get("grades", {})
                return g.get("current_score")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch course grade for {course_id}: {e}")
        return None

def get_announcements(course_id, per_page=10):
    """Return recent announcements for a course."""
    try:
        items = _get(f"{BASE}/api/v1/courses/{course_id}/discussion_topics", params={
            "only_announcements": "true",
            "per_page": per_page,
            "order_by": "recent_activity"
        })
        return items if isinstance(items, list) else []
    except Exception as e:
        logger.error(f"Failed to fetch announcements for course {course_id}: {e}")
        return []

def get_assignment_groups(course_id):
    """Return assignment groups with weights for a course."""
    try:
        groups = _get(f"{BASE}/api/v1/courses/{course_id}/assignment_groups", params={"per_page": 50})
        return groups if isinstance(groups, list) else []
    except Exception as e:
        logger.error(f"Failed to fetch assignment groups for course {course_id}: {e}")
        return []
