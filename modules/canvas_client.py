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
            "include[]": ["submission"],
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
            "content_types[]": ["application/pdf", "text/html"]
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
    return "syllabus" in name or "syllab" in name or "course_info" in name or "course info" in name
