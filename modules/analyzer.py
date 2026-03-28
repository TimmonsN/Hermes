import json
import logging
import time
from datetime import datetime, timedelta
from google import genai
from google.genai.errors import ClientError
from config import Config

logger = logging.getLogger("hermes.analyzer")

_client = genai.Client(api_key=Config.GEMINI_API_KEY)

HERMES_PERSONA = """You are Hermes, an AI academic assistant for a college student at Ohio State University.
Your job is to analyze assignments, understand class rules, and help the student get the best grades possible.
Be direct, practical, and fight for the student's success. The student tends to procrastinate (currently doing things day-of deadlines), so factor that in.
Always respond with valid JSON when asked for structured analysis."""


def _ask(prompt: str, retries: int = 3) -> str:
    """Send a prompt to Gemini and return the text response.
    Retries up to `retries` times on 429 rate-limit errors with exponential backoff.
    """
    full_prompt = f"{HERMES_PERSONA}\n\n{prompt}"
    for attempt in range(retries + 1):
        try:
            response = _client.models.generate_content(
                model=Config.GEMINI_MODEL,
                contents=full_prompt
            )
            return response.text
        except ClientError as e:
            if e.status_code == 429 and attempt < retries:
                wait = 30 * (2 ** attempt)  # 30s, 60s, 120s
                logger.warning(f"Gemini rate limited (attempt {attempt+1}/{retries+1}), waiting {wait}s...")
                time.sleep(wait)
            else:
                raise


def _parse_json(text: str) -> dict:
    """Strip markdown fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def extract_syllabus_rules(syllabus_text: str, course_name: str) -> dict:
    prompt = f"""Analyze this syllabus for {course_name} and extract the following as JSON:

{{
  "early_submission_bonus": {{
    "exists": true/false,
    "description": "e.g. 10 bonus points for submitting 24 hours early",
    "hours_early": 24
  }},
  "resubmit_policy": {{
    "exists": true/false,
    "description": "e.g. can resubmit until due date for better grade",
    "deadline": "same as due date or specific"
  }},
  "grading_weights": {{
    "assignments": "percentage or description",
    "exams": "percentage",
    "quizzes": "percentage",
    "participation": "percentage",
    "other": "any other categories"
  }},
  "late_policy": "description of late work policy",
  "attendance_policy": "description",
  "key_rules": ["any other important rules that affect when/how to submit work"]
}}

Syllabus text:
{syllabus_text}

Return ONLY valid JSON, no markdown."""

    try:
        return _parse_json(_ask(prompt))
    except Exception as e:
        logger.error(f"Syllabus rules extraction failed: {e}")
        return {}


def analyze_assignment(assignment: dict, syllabus_rules: dict, course_name: str) -> dict:
    due_at = assignment.get("due_at", "")
    now = datetime.now()

    if due_at:
        try:
            due_dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
            days_until_due = (due_dt.replace(tzinfo=None) - now).days
        except Exception:
            days_until_due = 7
    else:
        days_until_due = 999

    rules_summary = json.dumps(syllabus_rules, indent=2) if syllabus_rules else "No syllabus rules available."

    prompt = f"""Analyze this assignment for a college student and return structured JSON.

Course: {course_name}
Assignment: {assignment.get('title', 'Unknown')}
Points: {assignment.get('points_possible', 'unknown')}
Due: {due_at} ({days_until_due} days from now)
Submission types: {assignment.get('submission_types', 'unknown')}

Assignment description:
{(assignment.get('description') or 'No description provided.')[:3000]}

Course rules from syllabus:
{rules_summary}

Return this JSON:
{{
  "difficulty": 1-10,
  "estimated_hours": float,
  "assignment_type": "essay|coding|problem_set|reading|quiz|project|discussion|other",
  "priority": "low|medium|high|critical",
  "has_early_bonus": true/false,
  "early_bonus_details": "description or empty string",
  "can_resubmit": true/false,
  "resubmit_details": "description or empty string",
  "recommended_days_before_due": integer,
  "study_suggestions": ["tip1", "tip2"],
  "watch_outs": ["important notes"],
  "reasoning": "brief explanation"
}}

Consider: student procrastinates and currently does things day-of. Be realistic about difficulty.
Return ONLY valid JSON, no markdown."""

    try:
        analysis = _parse_json(_ask(prompt))

        if due_at and analysis.get("recommended_days_before_due") is not None:
            try:
                due_dt = datetime.fromisoformat(due_at.replace("Z", "+00:00")).replace(tzinfo=None)
                buffer = max(analysis["recommended_days_before_due"], Config.BUFFER_DAYS)
                start_dt = due_dt - timedelta(days=buffer)
                if start_dt.hour < Config.WAKE_HOUR:
                    start_dt = start_dt.replace(hour=Config.WAKE_HOUR, minute=0)
                analysis["start_by"] = start_dt.isoformat()
            except Exception as e:
                logger.warning(f"Could not calculate start_by: {e}")
                analysis["start_by"] = None
        else:
            analysis["start_by"] = None

        return analysis
    except Exception as e:
        logger.error(f"Assignment analysis failed for '{assignment.get('title')}': {e}")
        return {
            "difficulty": 5, "estimated_hours": 2.0, "priority": "medium",
            "has_early_bonus": False, "early_bonus_details": "",
            "can_resubmit": False, "resubmit_details": "",
            "recommended_days_before_due": Config.BUFFER_DAYS,
            "start_by": None, "reasoning": "Analysis failed — using defaults."
        }


def analyze_assignments_batch(assignments: list, syllabus_rules_map: dict) -> list:
    """Analyze up to 15 assignments in a single Gemini API call.

    Args:
        assignments: list of assignment dicts (same shape as used by analyze_assignment)
        syllabus_rules_map: dict mapping course_id -> syllabus rules dict

    Returns:
        list of analysis dicts in the same order as the input assignments.
        Falls back to individual analysis per assignment if batch parsing fails.
    """
    if not assignments:
        return []

    now = datetime.now()

    # Build the batch prompt
    lines = []
    for idx, a in enumerate(assignments):
        due_at = a.get("due_at", "")
        if due_at:
            try:
                due_dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
                days_until_due = (due_dt.replace(tzinfo=None) - now).days
            except Exception:
                days_until_due = 7
        else:
            days_until_due = 999

        rules = syllabus_rules_map.get(str(a.get("course_id", "")), {})
        rules_summary = json.dumps(rules, indent=2) if rules else "No syllabus rules available."
        desc = (a.get("description") or "No description provided.")[:600]

        lines.append(
            f"--- Assignment {idx + 1} ---\n"
            f"Course: {a.get('course_name', 'Unknown')}\n"
            f"Title: {a.get('title', 'Unknown')}\n"
            f"Points: {a.get('points_possible', 'unknown')}\n"
            f"Due: {due_at} ({days_until_due} days from now)\n"
            f"Submission types: {a.get('submission_types', 'unknown')}\n"
            f"Description: {desc}\n"
            f"Syllabus rules: {rules_summary}"
        )

    batch_text = "\n\n".join(lines)

    prompt = f"""You are analyzing assignments for Niko, a college student at Ohio State University who tends to procrastinate and often starts things the day they are due. Your job is to give brutally honest, actionable analysis so he can get A grades.

Analyze each of the following {len(assignments)} assignments and return a JSON array.

{batch_text}

Return a JSON array with exactly {len(assignments)} objects in the same order as the assignments above.
Each object must have these fields:
{{
  "difficulty": 1-10 (be honest — a 3000-word essay is at least a 7),
  "estimated_hours": float (realistic total including research, drafting, editing/debugging),
  "time_breakdown": {{"research": float, "writing_or_coding": float, "review": float}},
  "assignment_type": "essay|coding|problem_set|reading|quiz|project|discussion|lab|other",
  "priority": "low|medium|high|critical",
  "has_early_bonus": true/false,
  "early_bonus_details": "description or empty string",
  "can_resubmit": true/false,
  "resubmit_details": "description or empty string",
  "recommended_days_before_due": integer (minimum days ahead Niko should START, given his procrastination habit),
  "study_suggestions": ["3-5 specific, actionable strategies for THIS assignment type", "e.g. for essays: outline first before writing", "for coding: test edge cases early"],
  "watch_outs": ["2-4 specific traps or failure modes for this assignment", "e.g. don't forget to cite sources", "the rubric penalizes off-topic responses"],
  "course_strategy_note": "one sentence on how this fits into the course grade",
  "reasoning": "2-3 sentences on difficulty rating and time estimate"
}}

Critical rules:
- study_suggestions must be SPECIFIC to the assignment type, not generic advice
- watch_outs must be things that commonly cause students to lose points on THIS type of work
- If the assignment involves Canvas submission or a specific format, call that out in watch_outs
- Consider the syllabus grading weights when setting priority
- Niko procrastinates — recommended_days_before_due should account for his tendency to start late

Return ONLY a valid JSON array, no markdown, no extra text."""

    try:
        raw = _ask(prompt)

        # _parse_json handles a single object; for an array we do it manually
        text = raw.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        results = json.loads(text)
        if not isinstance(results, list):
            raise ValueError("Response was not a JSON array")
        if len(results) != len(assignments):
            raise ValueError(f"Expected {len(assignments)} results, got {len(results)}")

        # Calculate start_by for each result
        for i, analysis in enumerate(results):
            due_at = assignments[i].get("due_at", "")
            if due_at and analysis.get("recommended_days_before_due") is not None:
                try:
                    due_dt = datetime.fromisoformat(due_at.replace("Z", "+00:00")).replace(tzinfo=None)
                    buffer = max(analysis["recommended_days_before_due"], Config.BUFFER_DAYS)
                    start_dt = due_dt - timedelta(days=buffer)
                    if start_dt.hour < Config.WAKE_HOUR:
                        start_dt = start_dt.replace(hour=Config.WAKE_HOUR, minute=0)
                    analysis["start_by"] = start_dt.isoformat()
                except Exception as e:
                    logger.warning(f"Could not calculate start_by for batch item {i}: {e}")
                    analysis["start_by"] = None
            else:
                analysis["start_by"] = None

        return results

    except Exception as e:
        logger.error(f"Batch analysis failed ({e}), falling back to individual analysis")
        fallback = []
        for a in assignments:
            rules = syllabus_rules_map.get(str(a.get("course_id", "")), {})
            fallback.append(analyze_assignment(a, rules, a.get("course_name", "")))
        return fallback


def analyze_course_strategy(course_name: str, syllabus_text: str, current_grade: float = None) -> dict:
    """Return an overall course strategy based on syllabus and current grade."""
    grade_note = f"Current grade: {current_grade:.1f}%" if current_grade is not None else "No grades entered yet."

    prompt = f"""Analyze this course and give a strategic plan to maximize the student's grade.

Course: {course_name}
{grade_note}

Syllabus content:
{syllabus_text[:4000]}

Return JSON:
{{
  "grade_breakdown": {{"category": "weight_pct", ...}},
  "highest_impact_categories": ["which categories most affect the final grade"],
  "strategy": "2-3 sentence overall strategy to maximize grade",
  "assignments_to_prioritize": "which types of assignments to focus on and why",
  "assignments_to_not_sweat": "which assignments are low-stakes",
  "gpa_advice": "specific advice given current grade trajectory",
  "key_rules": ["important rules from the syllabus that affect strategy"]
}}

Return ONLY valid JSON, no markdown."""

    try:
        return _parse_json(_ask(prompt))
    except Exception as e:
        logger.error(f"Course strategy analysis failed for {course_name}: {e}")
        return {"strategy": "Analysis unavailable.", "grade_breakdown": {}, "key_rules": []}


def generate_study_plan(assignments: list, exams: list, wake_hour: int = 12, stop_hour: int = 22, days: int = 14) -> list:
    """Generate a day-by-day study schedule for the next N days.

    Returns list of dicts: {date: str, assignment_id: str, hours_planned: float, note: str}
    """
    from datetime import date, timedelta

    if not assignments and not exams:
        return []

    now = datetime.now()
    available_hours_per_day = stop_hour - wake_hour  # hours available daily

    # Build work items with urgency scores
    work_items = []
    for a in assignments:
        if not a.get("due_at"):
            continue
        try:
            due_dt = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00")).replace(tzinfo=None)
            days_left = max((due_dt - now).days, 0.5)
            hours_needed = float(a.get("estimated_hours") or 2.0)
            difficulty = int(a.get("difficulty") or 5)
            urgency = hours_needed / days_left * (difficulty / 5.0)
            work_items.append({
                "assignment_id": a["id"],
                "title": a.get("title", ""),
                "course_name": a.get("course_name", ""),
                "due_dt": due_dt,
                "hours_needed": hours_needed,
                "urgency": urgency,
                "days_left": days_left,
            })
        except Exception:
            continue

    for e in exams:
        if not e.get("start_at"):
            continue
        try:
            due_dt = datetime.fromisoformat(e["start_at"].replace("Z", "+00:00")).replace(tzinfo=None)
            days_left = max((due_dt - now).days, 0.5)
            hours_needed = float(e.get("study_hours_estimated") or 6.0)
            urgency = hours_needed / days_left * 2.0  # exams are higher urgency
            work_items.append({
                "assignment_id": e["id"],
                "title": f"STUDY: {e.get('title', 'Exam')}",
                "course_name": e.get("course_name", ""),
                "due_dt": due_dt,
                "hours_needed": hours_needed,
                "urgency": urgency,
                "days_left": days_left,
            })
        except Exception:
            continue

    if not work_items:
        return []

    # Sort by urgency descending
    work_items.sort(key=lambda x: -x["urgency"])

    # Build daily schedule
    plan_entries = []
    daily_budget = {}  # date_str -> hours_scheduled

    for item in work_items:
        hours_left = item["hours_needed"]
        due_date = item["due_dt"].date()

        # Spread work across days before due date
        for day_offset in range(days):
            plan_date = (now + timedelta(days=day_offset)).date()
            if plan_date >= due_date:
                break
            if hours_left <= 0:
                break

            date_str = plan_date.isoformat()
            used = daily_budget.get(date_str, 0)
            max_hours = min(available_hours_per_day * 0.6, 6.0)  # cap at 6h/day per item
            slot = min(hours_left, max_hours - used, 3.0)  # max 3h per item per day

            if slot > 0.25:
                plan_entries.append({
                    "date": date_str,
                    "assignment_id": item["assignment_id"],
                    "hours_planned": round(slot, 1),
                    "note": f"{item['title']} ({item['course_name']}) — due {item['due_dt'].strftime('%b %-d')}"
                })
                daily_budget[date_str] = used + slot
                hours_left -= slot

    return plan_entries


def analyze_exam(exam: dict, syllabus_rules: dict, course_name: str) -> dict:
    start_at = exam.get("start_at", "")
    now = datetime.now()
    days_until = 14

    if start_at:
        try:
            exam_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
            days_until = (exam_dt.replace(tzinfo=None) - now).days
        except Exception:
            pass

    grading_weights = json.dumps(syllabus_rules.get("grading_weights", {})) if syllabus_rules else ""

    prompt = f"""A student has an upcoming exam. Return JSON.

Course: {course_name}
Event: {exam.get('title', 'Exam')}
Date: {start_at} ({days_until} days away)
Description: {(exam.get('description') or 'No description')[:1000]}
Grading weights: {grading_weights}

Return:
{{
  "study_hours": float,
  "days_to_start_studying": integer,
  "daily_study_hours": float,
  "priority": "medium|high|critical",
  "study_tips": ["tip1", "tip2"],
  "reasoning": "brief explanation"
}}

Return ONLY valid JSON, no markdown."""

    try:
        analysis = _parse_json(_ask(prompt))

        if start_at and analysis.get("days_to_start_studying"):
            try:
                exam_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00")).replace(tzinfo=None)
                start_study = exam_dt - timedelta(days=analysis["days_to_start_studying"])
                if start_study.hour < Config.WAKE_HOUR:
                    start_study = start_study.replace(hour=Config.WAKE_HOUR, minute=0)
                analysis["start_study_by"] = start_study.isoformat()
            except Exception:
                analysis["start_study_by"] = None
        else:
            analysis["start_study_by"] = None

        return analysis
    except Exception as e:
        logger.error(f"Exam analysis failed: {e}")
        return {"study_hours": 6, "days_to_start_studying": 3, "priority": "high",
                "start_study_by": None, "reasoning": "Analysis failed."}


def detect_workload_collisions(assignments: list, exams: list) -> dict:
    if not assignments and not exams:
        return {"collisions": [], "overall_stress": "low"}

    items = []
    for a in assignments:
        if a.get("due_at") and a.get("priority") in ("high", "critical"):
            items.append(f"Assignment: {a['title']} ({a.get('course_name','')}) due {a['due_at'][:10]}, "
                         f"difficulty {a.get('difficulty','?')}/10, ~{a.get('estimated_hours','?')}h")
    for e in exams:
        if e.get("start_at"):
            items.append(f"Exam: {e['title']} ({e.get('course_name','')}) on {e['start_at'][:10]}")

    if not items:
        return {"collisions": [], "overall_stress": "low"}

    prompt = f"""A student has these upcoming high-priority items:

{chr(10).join(items)}

Identify dangerous deadline stacking (multiple big things within 2-3 days of each other).
Return JSON:
{{
  "collisions": [
    {{
      "items": ["item1", "item2"],
      "window": "e.g. March 28-30",
      "severity": "high|critical",
      "advice": "how to handle this"
    }}
  ],
  "overall_stress": "low|medium|high|critical",
  "recommendations": ["suggestion1", "suggestion2"]
}}

Return ONLY valid JSON, no markdown."""

    try:
        return _parse_json(_ask(prompt))
    except Exception as e:
        logger.error(f"Collision detection failed: {e}")
        return {"collisions": [], "overall_stress": "medium"}


def generate_chat_response(user_message: str, context: dict) -> str:
    """Respond conversationally to the student via the web dashboard."""
    assignments_summary = []
    for a in context.get("assignments", [])[:10]:
        due = a.get("due_at", "")[:10] if a.get("due_at") else "no due date"
        assignments_summary.append(
            f"- {a.get('title')} ({a.get('course_name','')}) due {due}, "
            f"status: {a.get('status','pending')}, priority: {a.get('priority','?')}, "
            f"difficulty: {a.get('difficulty','?')}/10, ~{a.get('estimated_hours','?')}h"
        )

    exams_summary = []
    for e in context.get("exams", [])[:5]:
        date = e.get("start_at", "")[:10] if e.get("start_at") else "unknown"
        exams_summary.append(f"- {e.get('title')} ({e.get('course_name','')}) on {date}")

    prompt = f"""You are Hermes, an AI school buddy for a college student at Ohio State University named Niko.
You know everything about his upcoming assignments and exams. Be supportive, direct, and fight for his success.
Be warm but no-nonsense. Today is {context.get('current_date', datetime.now().strftime('%Y-%m-%d'))}.

Upcoming assignments:
{chr(10).join(assignments_summary) if assignments_summary else 'None upcoming.'}

Upcoming exams:
{chr(10).join(exams_summary) if exams_summary else 'None upcoming.'}

{('Note: ' + context.get('status_update_note','')) if context.get('status_update_note') else ''}

Niko says: {user_message}

Respond helpfully and directly. If he tells you he finished or started something, acknowledge it positively.
If he's asking about an assignment, give genuinely useful academic guidance.
If he seems stressed, be encouraging. If he's too relaxed about something urgent, be honest."""

    try:
        return _ask(prompt)
    except Exception as e:
        logger.error(f"Chat response failed: {e}")
        return "Hit an error — try again in a moment."


def generate_weekly_digest(assignments: list, exams: list, collision_report: dict) -> str:
    now = datetime.now()
    today_str = now.strftime("%A, %b %d")

    today_items, this_week, upcoming = [], [], []

    for a in assignments:
        if not a.get("due_at"):
            continue
        try:
            due = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00")).replace(tzinfo=None)
            days_away = (due - now).days
            line = f"{a.get('course_name','')}: {a['title']} (due {due.strftime('%a %b %d %I:%M%p').lstrip('0')})"
            if a.get("has_early_bonus"):
                line += " [early bonus!]"
            if a.get("can_resubmit"):
                line += " [resubmittable]"
            if days_away <= 0:
                today_items.append(f"DUE TODAY: {line}")
            elif days_away <= 7:
                start_note = ""
                if a.get("start_by"):
                    sb = datetime.fromisoformat(a["start_by"])
                    start_note = f" - start {sb.strftime('%a')}"
                this_week.append(f"{line}{start_note}")
            else:
                upcoming.append(line)
        except Exception:
            continue

    for e in exams:
        if not e.get("start_at"):
            continue
        try:
            exam_dt = datetime.fromisoformat(e["start_at"].replace("Z", "+00:00")).replace(tzinfo=None)
            days_away = (exam_dt - now).days
            line = f"EXAM: {e.get('course_name','')}: {e['title']} ({exam_dt.strftime('%a %b %d')})"
            if days_away <= 7:
                this_week.append(line)
            else:
                upcoming.append(line)
        except Exception:
            continue

    parts = [f"Hermes — {today_str}"]
    if today_items:
        parts.append("\nTODAY:")
        parts.extend(today_items)
    if this_week:
        parts.append("\nTHIS WEEK:")
        for item in this_week:
            parts.append(f"  {item}")
    if upcoming:
        parts.append("\nCOMING UP:")
        for item in upcoming[:3]:
            parts.append(f"  {item}")
    collisions = collision_report.get("collisions", [])
    if collisions:
        parts.append("\nHEADS UP — deadline collision:")
        for col in collisions[:2]:
            parts.append(f"  {col.get('window','')}: {col.get('advice','spread your work')}")
    if not today_items and not this_week:
        parts.append("\nNothing pressing. Good time to get ahead.")
    parts.append("\nFull details: http://localhost:5000")
    return "\n".join(parts)
