import json
import logging
import re
import time
from datetime import datetime, timedelta
from google import genai
from google.genai.errors import ClientError
from config import Config
import database as db

logger = logging.getLogger("hermes.analyzer")

_gemini_client = genai.Client(api_key=Config.GEMINI_API_KEY)
_groq_client = None

# Phrases that indicate a Canvas submission placeholder, not a real description
_BOILERPLATE_PHRASES = [
    "please use this to submit",
    "use this assignment to submit",
    "use this to submit",
    "submit your completed",
    "submit your assignment",
    "assignment submission",
    "submit here",
    "upload your",
    "submit via",
    "this is where you",
]

_MATERIAL_STOP = {'with','this','that','from','have','will','assignment','homework',
                  'worksheet','reflection','discussion','quiz','exam','project','lab',
                  'and','the','for','not','are','but','you','your'}

def _find_relevant_materials(title: str, materials_dict: dict, max_chars: int = 3000) -> str:
    """Return content from files most relevant to this assignment title."""
    if not materials_dict:
        return ""
    keywords = [w for w in re.split(r'\W+', title.lower()) if len(w) > 3 and w not in _MATERIAL_STOP]
    if not keywords:
        return ""

    scored = []
    for fname, content in materials_dict.items():
        fname_lower = fname.lower()
        score = sum(1 for kw in keywords if kw in fname_lower)
        if score > 0:
            scored.append((score, fname, content))

    if not scored:
        return ""

    max_score = max(s for s, _, _ in scored)
    best = [(fname, content) for s, fname, content in scored if s == max_score]

    parts = []
    total = 0
    for fname, content in best[:2]:
        if total >= max_chars:
            break
        snippet = content[:max_chars - total]
        parts.append(f"[{fname}]\n{snippet.strip()}")
        total += len(snippet)
    return "\n\n".join(parts)


def _is_boilerplate_description(description: str) -> bool:
    """Return True if the description is a Canvas submission placeholder, not real content."""
    if not description:
        return True
    clean = re.sub(r'<[^>]+>', ' ', description)
    clean = re.sub(r'&\w+;', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip().lower()
    if len(clean) < 40:
        return True
    return any(phrase in clean for phrase in _BOILERPLATE_PHRASES)


def _get_groq_client():
    global _groq_client
    if _groq_client is None and Config.GROQ_API_KEY:
        from groq import Groq
        _groq_client = Groq(api_key=Config.GROQ_API_KEY)
    return _groq_client


def _default_analysis():
    """Placeholder returned when all AI is unavailable. NOT stored to DB — triggers retry next sync."""
    return {
        "difficulty": None, "estimated_hours": None, "priority": "medium",
        "has_early_bonus": False, "early_bonus_details": "",
        "can_resubmit": False, "resubmit_details": "",
        "recommended_days_before_due": Config.BUFFER_DAYS,
        "start_by": None, "reasoning": None,
        "_rate_limited": True,
    }


HERMES_PERSONA = """You are Hermes, an AI academic assistant for a college student at Ohio State University.
Your job is to analyze assignments, understand class rules, and help the student get the best grades possible.
Be direct, practical, and fight for the student's success. The student tends to procrastinate (currently doing things day-of deadlines), so factor that in.
Always respond with valid JSON when asked for structured analysis."""


HERMES_CHAT_PERSONA = """You are Hermes, Niko's AI school buddy at Ohio State. You have full context on his assignments, exams, and grades.

Hard rules:
- Answer the exact question asked. Lead with the answer — no preamble, no greeting.
- Keep responses to 2-4 sentences unless he explicitly asks for detail.
- Never repeat what you just said. Never re-summarize his situation back to him.
- If you don't know something specific (like the exact assignment rubric), say so plainly and tell him where to find it. Don't make things up or pivot to something else.
- Don't add unsolicited advice after answering — if he wants more, he'll ask.
- Be warm but not sycophantic. No "Great question!" or "Absolutely!"."""

def _ask_groq(prompt: str, model: str = None, system_prompt: str = None,
              call_type: str = "analysis") -> str:
    """Send a prompt to Groq and return the text response.

    call_type: "analysis" (batch/single assignment AI) or "chat" (conversational).
    Used for per-type usage tracking on the Alerts page.
    """
    client = _get_groq_client()
    if not client:
        raise RuntimeError("Groq not configured — set GROQ_API_KEY")
    use_model = model or Config.GROQ_MODEL_ANALYSIS
    sys_msg = system_prompt or HERMES_PERSONA
    response = client.chat.completions.create(
        model=use_model,
        messages=[
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    db.track_api_call("groq", call_type=call_type)
    return response.choices[0].message.content


def _ask(prompt: str, retries: int = 1, call_type: str = "analysis") -> str:
    """Send a prompt to Gemini. Falls back to Groq quickly if Gemini is rate-limited.

    call_type: passed through to tracking so analysis vs chat calls are counted separately.
    """
    full_prompt = f"{HERMES_PERSONA}\n\n{prompt}"
    last_exc = None
    for attempt in range(retries + 1):
        try:
            response = _gemini_client.models.generate_content(
                model=Config.GEMINI_MODEL,
                contents=full_prompt,
            )
            db.track_api_call("gemini", call_type=call_type)
            return response.text
        except ClientError as e:
            if e.code == 429:
                last_exc = e
                if attempt < retries:
                    wait = 15 * (2 ** attempt)  # 15s, then fall to Groq
                    logger.warning(f"Gemini rate limited (attempt {attempt+1}/{retries+1}), waiting {wait}s...")
                    time.sleep(wait)
            else:
                raise  # non-429 Gemini error

    # Gemini exhausted — fall back to Groq
    if Config.GROQ_API_KEY:
        logger.warning("Gemini retries exhausted (429) — falling back to Groq")
        return _ask_groq(prompt, call_type=call_type)
    raise last_exc  # no Groq, re-raise


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


def generate_course_notes(course_name: str, syllabus_content: str, rules: dict, grade_context: str = "") -> str:
    """Generate strategic course notes that Hermes uses as persistent memory for this course."""
    rules_summary = json.dumps(rules, indent=2) if rules else "No structured rules extracted."
    prompt = f"""You are Hermes analyzing a course to build persistent strategic notes.

Course: {course_name}
{grade_context}

Syllabus/materials content:
{syllabus_content[:3000]}

Extracted rules:
{rules_summary[:1000]}

Write 3-5 sentences of dense, strategic notes that will help you give better advice on assignments for this course.
Focus on: grading weights (what matters most), assignment patterns, late/resubmit policies, any known professor quirks, and what typically causes students to lose points.
Be specific and factual — these notes are your memory of this course. Write in first person as Hermes.
Return plain text, no JSON, no headers."""
    try:
        return _ask(prompt)
    except Exception as e:
        logger.error(f"generate_course_notes failed for {course_name}: {e}")
        return ""


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
        if "429" in str(e) or "rate_limit" in str(e).lower():
            logger.warning(f"Assignment analysis rate-limited for '{assignment.get('title')}' — will retry next sync.")
            return _default_analysis()
        logger.error(f"Assignment analysis failed for '{assignment.get('title')}': {e}")
        return {
            "difficulty": 5, "estimated_hours": 2.0, "priority": "medium",
            "has_early_bonus": False, "early_bonus_details": "",
            "can_resubmit": False, "resubmit_details": "",
            "recommended_days_before_due": Config.BUFFER_DAYS,
            "start_by": None, "reasoning": "Analysis failed — using defaults.",
            "_rate_limited": True,
        }


def analyze_assignments_batch(assignments: list, syllabus_rules_map: dict, course_materials_map: dict = None, course_notes_map: dict = None, course_groups_map: dict = None) -> list:
    """Analyze up to 15 assignments in a single API call (Gemini primary, Groq fallback).

    Returns list of analysis dicts in the same order as the input assignments.
    course_groups_map: {course_id: [{canvas_group_id, name, weight}, ...]}
    """
    if not assignments:
        return []

    now = datetime.now()

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
        raw_desc = a.get("description") or ""
        materials_dict = (course_materials_map or {}).get(str(a.get("course_id", "")), {})
        if isinstance(materials_dict, dict):
            materials = _find_relevant_materials(a.get("title", ""), materials_dict)
        else:
            materials = materials_dict  # backward compat if string passed
        rubric = a.get("rubric_text", "")
        course_notes = (course_notes_map or {}).get(str(a.get("course_id", "")), "")

        # Resolve actual assignment group name and weight from Canvas data
        groups = (course_groups_map or {}).get(str(a.get("course_id", "")), [])
        group_info = next(
            (g for g in groups if g.get("canvas_group_id") == a.get("canvas_group_id")),
            None
        )
        if group_info and group_info.get("weight", 0) > 0:
            grade_weight_line = (
                f"Grade category: {group_info['name']} — worth {group_info['weight']:.0f}% of final grade"
            )
        else:
            grade_weight_line = "Grade category: Unknown (no group weight data)"

        if _is_boilerplate_description(raw_desc):
            if materials:
                desc = f"[Canvas description is a submission link only. Assignment content inferred from course materials below:]\n{materials[:2500]}"
            else:
                desc = f"[No Canvas description and no matching course files found. Infer from course '{a.get('course_name', '')}' and title '{a.get('title', '')}' using your academic knowledge.]"
        else:
            desc = raw_desc[:800]
            if materials:
                desc += f"\n\nRelevant course materials:\n{materials[:1500]}"

        lines.append(
            f"--- Assignment {idx + 1} ---\n"
            f"Course: {a.get('course_name', 'Unknown')}\n"
            f"Title: {a.get('title', 'Unknown')}\n"
            f"Points: {a.get('points_possible', 'unknown')}\n"
            f"Due: {due_at} ({days_until_due} days from now)\n"
            f"Submission types: {a.get('submission_types', 'unknown')}\n"
            f"{grade_weight_line}\n"
            f"Description: {desc}\n"
            f"Rubric (grading criteria): {rubric if rubric else 'Not available'}\n"
            f"Course strategy notes: {course_notes[:400] if course_notes else 'None yet'}\n"
            f"Syllabus rules: {rules_summary}"
        )

    batch_text = "\n\n".join(lines)

    prompt = f"""You are Hermes, analyzing assignments for Niko, a college student at Ohio State University. He tends to procrastinate, but he's capable — your job is to give accurate, actionable analysis so he can get A grades.

Analyze each of the following {len(assignments)} assignments and return a JSON array.

{batch_text}

Return a JSON array with exactly {len(assignments)} objects in the same order as the assignments above.
Each object must have these fields:
{{
  "difficulty": 1-10 (honest — a 3000-word essay is 7+, a short worksheet is 2-3),
  "estimated_hours": float (realistic wall-clock time including research, drafting, editing/debugging),
  "time_breakdown": {{"research": float, "writing_or_coding": float, "review": float}},
  "assignment_type": "essay|coding|problem_set|reading|quiz|project|discussion|lab|other",
  "priority": "low|medium|high|critical",
  "has_early_bonus": true/false,
  "early_bonus_details": "description or empty string",
  "can_resubmit": true/false,
  "resubmit_details": "description or empty string",
  "recommended_days_before_due": integer (days ahead Niko should START — be realistic, not alarmist),
  "study_suggestions": ["3-5 specific, actionable strategies for THIS assignment type"],
  "watch_outs": ["2-4 specific traps or failure modes for this assignment"],
  "course_strategy_note": "one sentence on how this fits into the course grade",
  "task_sections": ["specific subtask 1 (est time)", "subtask 2 (est time)"],
  "course_weight_context": "one sentence: what % of final grade this is worth and whether it's high/low stakes relative to the course",
  "study_strategy": "3-5 sentence specific action plan: what to review first, which course materials or lecture topics to focus on, how to approach the problem type, and the single most common mistake to avoid on this specific assignment type",
  "reasoning": "2-3 calm, factual sentences explaining difficulty and time estimate"
}}

TONE RULES — strictly enforced:
- reasoning must be factual and calm. Never use ALL CAPS, never say "drop everything", never be melodramatic.
- Match urgency to actual timeline: 3+ days and < 3h of work = low urgency, matter-of-fact tone.
- "critical" priority is reserved for things due within 24h OR exams/finals. Use "high" for due-in-3-days.
- If description says "[No real description]", infer from course name + title using your academic knowledge.
- study_suggestions must be SPECIFIC to this assignment type, not generic ("read the rubric" is not useful).
- watch_outs must be things that commonly cause point deductions on THIS specific type of work.
- task_sections: list 3-5 concrete sub-tasks with estimated time each ONLY if there's enough info from description/PDF to be specific; otherwise use an empty array [].
- course_weight_context: one sentence stating the grade % impact and whether it's high or low stakes.

PRIORITY & DIFFICULTY CALIBRATION — hard rules, no exceptions:
- difficulty 8/10 → priority must be "high" (never "medium")
- difficulty 9-10/10 → priority must be "critical"
- estimated_hours ≥ 7h → priority must be at least "high"
- estimated_hours ≥ 10h → priority must be "critical"
- "medium" is for difficulty 5-7, estimated_hours 2-6h ONLY
- "low" is only for trivial tasks: difficulty 1-4, < 2h of real work
- A coding project worth 100+ points is NEVER "medium" or below
- recommended_days_before_due must be at least ceil(estimated_hours / 3) — don't tell Niko to do 10h of work in one sitting

Return ONLY a valid JSON array, no markdown, no extra text."""

    def _parse_batch_result(raw, count):
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
        if len(results) != count:
            raise ValueError(f"Expected {count} results, got {len(results)}")
        return results

    def _apply_start_by(results):
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
                except Exception:
                    analysis["start_by"] = None
            else:
                analysis["start_by"] = None
        return results

    def _enforce_calibration(results):
        """Hard post-processing rules — Python enforces what the AI prompt says."""
        import math
        for a in results:
            diff = a.get("difficulty") or 0
            hrs = a.get("estimated_hours") or 0
            p = a.get("priority", "medium")
            if hrs >= 10:
                a["priority"] = "critical"
                if a.get("recommended_days_before_due") is not None:
                    a["recommended_days_before_due"] = max(a["recommended_days_before_due"], math.ceil(hrs / 3))
            elif hrs >= 7 or diff >= 9:
                if p not in ("critical",):
                    a["priority"] = "high"
            elif diff >= 8:
                if p == "medium" or p == "low":
                    a["priority"] = "high"
        return results

    # Try Gemini first (with Groq fallback inside _ask). On rate limit: wait 20s and
    # retry once before giving up. A single 429 shouldn't permanently skip the batch —
    # that was the root cause of 98 unanalyzed assignments in the original bug.
    for _batch_attempt in range(2):
        try:
            raw = _ask(prompt)  # _ask already handles Gemini→Groq fallback internally
            results = _parse_batch_result(raw, len(assignments))
            return _enforce_calibration(_apply_start_by(results))

        except ClientError as e:
            if e.code == 429:
                if _batch_attempt == 0:
                    logger.warning(f"Batch rate-limited (Gemini 429) — waiting 20s before retry...")
                    time.sleep(20)
                    continue  # retry
                logger.warning(f"Batch still rate-limited after retry — returning defaults for {len(assignments)} assignments.")
                return [_default_analysis() for _ in assignments]
            logger.error(f"Batch API error ({e}), falling back to individual analysis")
            break  # non-429 error → fall through to individual analysis
        except Exception as e:
            # Rate limit from Groq or any provider — wait and retry rather than immediately giving up
            if "429" in str(e) or "rate_limit" in str(e).lower():
                if _batch_attempt == 0:
                    logger.warning(f"Batch rate-limited (provider 429) — waiting 20s before retry...")
                    time.sleep(20)
                    continue  # retry
                logger.warning(f"Batch still rate-limited after retry — returning defaults for {len(assignments)} assignments.")
                return [_default_analysis() for _ in assignments]
            logger.error(f"Batch parse/logic error ({e}), falling back to individual analysis")
            break  # non-rate-limit error → fall through to individual analysis

    # Parse/logic failure (not rate limit) — try individual analysis
    fallback = []
    for a in assignments:
        rules = syllabus_rules_map.get(str(a.get("course_id", "")), {})
        result = analyze_assignment(a, rules, a.get("course_name", ""))
        if result.get("_rate_limited"):
            # Individual hit rate limit too — bail out of the loop
            logger.warning(f"Individual fallback also rate-limited — stopping fallback loop.")
            fallback.extend([_default_analysis() for _ in assignments[len(fallback):]])
            break
        fallback.append(result)
    return _enforce_calibration(fallback)


def generate_grade_targets(courses_data: list) -> list:
    """Batch-generate realistic target grade suggestions for multiple courses.

    courses_data: list of dicts with keys: name, current_grade, remaining_count,
                  remaining_hours, course_notes, grading_weights

    Returns list of dicts: {course_name, suggested_target, reasoning}
    """
    if not courses_data:
        return []

    lines = []
    for i, c in enumerate(courses_data):
        lines.append(
            f"Course {i+1}: {c.get('name', 'Unknown')}\n"
            f"  Current grade: {c.get('current_grade', 'unknown')}\n"
            f"  Remaining assignments: {c.get('remaining_count', '?')} (~{c.get('remaining_hours', '?')}h of work)\n"
            f"  Grading weights: {c.get('grading_weights', 'unknown')}\n"
            f"  Course notes: {(c.get('course_notes') or 'None')[:200]}"
        )

    prompt = f"""Niko is a college student at Ohio State. Suggest a realistic but ambitious target grade for each course.

{chr(10).join(lines)}

For each course, suggest a target percentage that is:
- Realistic given the current grade and remaining work
- Ambitious but achievable with solid effort (not guaranteed, requires actual work)
- Higher if the current grade is already strong and remaining work is manageable
- Lower if the current grade is struggling or remaining work is very heavy
- Never above 100%, never below current grade (can't go back in time)

Return a JSON array with exactly {len(courses_data)} objects:
[{{"course_name": "...", "suggested_target": 92.5, "reasoning": "One sentence explaining why this target makes sense."}}]

Return ONLY valid JSON, no markdown."""

    try:
        raw = _ask(prompt)
        text = raw.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
        results = json.loads(text.strip())
        if isinstance(results, list) and len(results) == len(courses_data):
            return results
    except Exception as e:
        logger.error(f"generate_grade_targets failed: {e}")
    return []


def analyze_course_strategy(course_name: str, syllabus_text: str, current_grade: float = None) -> dict:
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
    """Generate a day-by-day study schedule for the next N days."""
    from datetime import date, timedelta

    if not assignments and not exams:
        return []

    now = datetime.now()
    available_hours_per_day = stop_hour - wake_hour

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
            urgency = hours_needed / days_left * 2.0
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

    work_items.sort(key=lambda x: -x["urgency"])

    plan_entries = []
    daily_budget = {}

    for item in work_items:
        hours_left = item["hours_needed"]
        due_date = item["due_dt"].date()

        for day_offset in range(days):
            plan_date = (now + timedelta(days=day_offset)).date()
            if plan_date >= due_date:
                break
            if hours_left <= 0:
                break

            date_str = plan_date.isoformat()
            used = daily_budget.get(date_str, 0)
            max_hours = min(available_hours_per_day * 0.6, 6.0)
            slot = min(hours_left, max_hours - used, 3.0)

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
  "study_tips": ["tip1", "tip2", "tip3"],
  "daily_study_plan": [
    {{"day": 1, "focus": "Review [specific topic], skim lecture notes on [topic]", "hours": 1.5}},
    {{"day": 2, "focus": "Practice problems from [specific topic], review weak areas", "hours": 2.0}}
  ],
  "reasoning": "brief explanation"
}}

For daily_study_plan: generate one entry per study day (up to days_to_start_studying days). Each focus should be specific to this course/exam — reference actual topics if known from the description or course. If description gives no topics, reference general exam prep strategies for the subject.

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
    """Respond conversationally. Uses Groq (fast) with Gemini fallback."""
    assignments_summary = []
    for a in context.get("assignments", [])[:12]:
        due = a.get("due_at", "")[:10] if a.get("due_at") else "no due date"
        assignments_summary.append(
            f"- {a.get('title')} ({a.get('course_name','')}) due {due} | "
            f"status:{a.get('status','pending')} priority:{a.get('priority','?')} "
            f"diff:{a.get('difficulty','?')}/10 ~{a.get('estimated_hours','?')}h"
        )

    exams_summary = []
    for e in context.get("exams", [])[:5]:
        date = e.get("start_at", "")[:10] if e.get("start_at") else "unknown"
        exams_summary.append(f"- {e.get('title')} ({e.get('course_name','')}) on {date}")

    grades_summary = []
    for g in context.get("grades", [])[:8]:
        if g.get("grade_pct") is not None:
            grades_summary.append(f"- {g.get('title','?')} ({g.get('course_name','?')}): {g['grade_pct']:.1f}%")

    course_grades_summary = []
    for c in context.get("course_grades", []):
        if c.get("canvas_grade_pct") is not None:
            course_grades_summary.append(f"- {c['name']}: {c['canvas_grade_pct']:.1f}% overall")

    syllabus_summary = context.get("syllabus_notes", "")

    course_notes_list = []
    for c in context.get("course_grades", []):
        notes = db.get_course_notes(str(c.get("id", c.get("canvas_id", ""))))
        if notes:
            course_notes_list.append(f"[{c['name']}] {notes[:300]}")
    course_notes_block = ("Course strategy notes:\n" + "\n".join(course_notes_list)) if course_notes_list else ""

    # Build focused assignment block if this is an assignment-specific chat
    focused_block = ""
    fa = context.get("focused_assignment")
    if fa:
        fa_analysis = fa.get("analysis") or {}
        focused_block = f"""
FOCUSED ASSIGNMENT CONTEXT:
Title: {fa.get('title')}
Course: {fa.get('course_name')}
Due: {fa.get('due_at', '')[:10]}
Points: {fa.get('points_possible')}
Description: {(fa.get('description') or 'None')[:500]}
Hermes analysis: difficulty={fa_analysis.get('difficulty')}/10, estimated_hours={fa_analysis.get('estimated_hours')}, priority={fa_analysis.get('priority')}
Reasoning: {fa_analysis.get('reasoning', '')}
Study tips: {'; '.join((fa_analysis.get('study_suggestions') or [])[:3])}
Watch-outs: {'; '.join((fa_analysis.get('watch_outs') or [])[:3])}
Course materials: {fa.get('course_content', '')[:600]}

Niko is asking specifically about this assignment. Answer directly and specifically — not generic advice.
"""

    prompt = f"""Today: {context.get('current_date', datetime.now().strftime('%Y-%m-%d %A'))}
{focused_block}
Assignments:
{chr(10).join(assignments_summary) if assignments_summary else 'None.'}

Exams:
{chr(10).join(exams_summary) if exams_summary else 'None.'}

Course grades (from Canvas):
{chr(10).join(course_grades_summary) if course_grades_summary else 'Not available.'}

Recent graded assignments:
{chr(10).join(grades_summary) if grades_summary else 'None entered.'}

{('Syllabus notes: ' + syllabus_summary) if syllabus_summary else ''}
{course_notes_block}
{('Context: ' + context.get('status_update_note','')) if context.get('status_update_note') else ''}

Niko: {user_message}"""

    try:
        # Groq is primary for chat — faster, more conversational.
        # call_type="chat" ensures chat calls are tracked separately from analysis
        # calls, so the Alerts page can show a meaningful breakdown.
        if Config.GROQ_API_KEY:
            return _ask_groq(prompt, model=Config.GROQ_MODEL_CHAT,
                             system_prompt=HERMES_CHAT_PERSONA, call_type="chat")
        return _ask(prompt, call_type="chat")
    except Exception as e:
        logger.error(f"Chat response failed: {e}")
        return "Hit an error — try again in a moment."


def generate_week_synthesis(upcoming_assignments: list, upcoming_exams: list) -> str:
    """Generate a 2-3 sentence week overview. Stored and shown at top of calendar."""
    if not upcoming_assignments and not upcoming_exams:
        return ""

    now = datetime.now()

    # Build a compact summary of what's coming up
    lines = []
    for a in upcoming_assignments[:15]:
        due = a.get("due_at", "")[:10] if a.get("due_at") else "?"
        lines.append(f"- {a.get('title')} ({a.get('course_name','')}) due {due} | {a.get('priority','?')} priority | ~{a.get('estimated_hours','?')}h")

    for e in upcoming_exams[:5]:
        date = e.get("start_at", "")[:10] if e.get("start_at") else "?"
        lines.append(f"- EXAM: {e.get('title')} ({e.get('course_name','')}) on {date}")

    prompt = f"""Today is {now.strftime('%A %B %d, %Y')}.

Here's what Niko has coming up in the next 2 weeks:
{chr(10).join(lines)}

Write 2-3 sentences MAX summarizing:
1. The roughest upcoming period (if any cluster of heavy work exists)
2. The single most important thing to start NOW (if anything is urgent or high-effort)
3. Any clear day(s) to use wisely

Keep it direct, specific, and actionable. No preamble like "Looking at your schedule..." — just the insight. If there's nothing pressing, say so briefly."""

    try:
        return _ask(prompt)
    except Exception as e:
        logger.error(f"generate_week_synthesis failed: {e}")
        return ""


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
