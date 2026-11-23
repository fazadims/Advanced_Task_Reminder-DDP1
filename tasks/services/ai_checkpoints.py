"""Optional Gemini-powered checkpoint suggestions with deterministic fallback (F3.4)."""

import json
import os
import urllib.request
from urllib.error import URLError

from tasks.models import Task
from tasks.services.checkpoint_gen import PHASE_TEMPLATES

VALID_PHASES = {"RESEARCH", "DRAFT", "REVIEW", "SUBMIT", "CUSTOM"}
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"


def _fallback_checkpoints(task: Task) -> list[dict]:
    """Return deterministic checkpoint suggestions derived from PHASE_TEMPLATES."""
    steps = PHASE_TEMPLATES.get(task.category, PHASE_TEMPLATES["OTHER"])
    per_step = max(15, round((task.estimated_minutes or 0) / len(steps)))
    per_step = max(10, min(240, per_step))
    return [
        {
            "title": label,
            "phase": phase,
            "estimated_minutes": per_step,
        }
        for phase, label, _ratio in steps
    ]


def _build_prompt(task: Task, extra_context: str = "") -> str:
    course_label = str(task.course) if task.course_id else "General"
    lines = [
        "Break down this university assignment into 3 to 5 concise, actionable checkpoints.",
        f"Title: {task.title}",
        f"Course: {course_label}",
        f"Category: {task.category}",
        f"Deadline: {task.deadline.isoformat()}",
        f"Estimated total minutes: {task.estimated_minutes}",
        f"Difficulty (1-5): {task.difficulty}",
    ]
    if task.description:
        lines.append(f"Description: {task.description.strip()}")
    if extra_context and extra_context.strip():
        lines.append(f"Extra context: {extra_context.strip()}")
    lines.append(
        "Return ONLY a JSON array of objects with keys: "
        '"title" (non-empty string), '
        '"phase" (one of RESEARCH, DRAFT, REVIEW, SUBMIT, CUSTOM), and '
        '"estimated_minutes" (integer between 10 and 240).'
    )
    return "\n".join(lines)


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _normalize_suggestions(raw_items) -> list[dict]:
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("checkpoints") or raw_items.get("suggestions")
    if not isinstance(raw_items, list):
        raise ValueError("AI response must be a JSON list of checkpoints.")

    normalized: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:200]
        if not title:
            continue
        raw_phase = str(item.get("phase") or "CUSTOM").strip().upper()
        phase = raw_phase if raw_phase in VALID_PHASES else "CUSTOM"
        raw_minutes = item.get("estimated_minutes", 30)
        try:
            minutes = int(round(float(raw_minutes)))
        except (TypeError, ValueError):
            minutes = 30
        minutes = max(10, min(240, minutes))
        normalized.append(
            {
                "title": title,
                "phase": phase,
                "estimated_minutes": minutes,
            }
        )
    return normalized


def suggest_checkpoints(task: Task, extra_context: str = "") -> tuple[list[dict], str]:
    """Suggest checkpoints via Gemini REST API with automatic zero-error fallback.

    Returns ``(suggestions, source)`` where ``source`` is ``"ai"`` or ``"fallback"``.
    Makes zero network requests when ``GEMINI_API_KEY`` is unset or blank.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return _fallback_checkpoints(task), "fallback"

    model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": _build_prompt(task, extra_context)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.4,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8")
        data = json.loads(body)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(_strip_code_fences(text))
        suggestions = _normalize_suggestions(parsed)
        if not suggestions:
            return _fallback_checkpoints(task), "fallback"
        return suggestions, "ai"
    except (URLError, TimeoutError, KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError, OSError):
        return _fallback_checkpoints(task), "fallback"
