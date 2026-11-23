"""Adaptive AI Coaching service with deterministic 3x3 offline fallback (F4.5)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from tasks.models import Reminder

logger = logging.getLogger(__name__)

VALID_TONES: tuple[str, ...] = ("friendly", "strict", "minimal")
DEFAULT_TONE: str = "friendly"
MAX_TEMPLATE_MESSAGE_LENGTH: int = 180
MAX_AI_MESSAGE_LENGTH: int = 220
GEMINI_TIMEOUT_SECONDS: int = 8

# Deterministic 3x3 matrix keyed by (tone, escalation_level).
# All templates stay concise (<= 180 chars) and use {hours}, {checkpoint}, {task}.
COACHING_TEMPLATES: dict[tuple[str, int], str] = {
    ("friendly", 1): (
        "Santai tapi konsisten! Cicil '{checkpoint}' untuk {task} ({hours} jam lagi). "
        "25 menit saja sudah progres hebat."
    ),
    ("friendly", 2): (
        "Yuk semangat! '{checkpoint}' ({task}) tinggal {hours} jam lagi. "
        "Buka Focus Mode 25 menit sekarang biar besok lebih lega."
    ),
    ("friendly", 3): (
        "Kamu pasti bisa! '{checkpoint}' ({task}) tinggal {hours} jam lagi. "
        "Fokus kerjakan bagian paling penting sekarang juga!"
    ),
    ("strict", 1): (
        "Jadwal aktif: '{checkpoint}' untuk {task} jatuh tempo {hours} jam lagi. "
        "Mulai eksekusi hari ini, jangan ditunda."
    ),
    ("strict", 2): (
        "Peringatan H-1: '{checkpoint}' ({task}) tersisa {hours} jam. "
        "Hentikan distraksi dan tuntaskan blok kerja ini sekarang."
    ),
    ("strict", 3): (
        "KRITIS ({hours} jam tersisa): '{checkpoint}' untuk {task} harus beres SEKARANG. "
        "Masuk Focus Mode tanpa penundaan."
    ),
    ("minimal", 1): "[H-3d] {checkpoint} | {task} — sisa {hours} jam.",
    ("minimal", 2): "[H-1d] {checkpoint} | {task} — sisa {hours} jam. Prioritaskan hari ini.",
    ("minimal", 3): "[KRITIS] {checkpoint} | {task} — sisa {hours} jam. Kerjakan sekarang.",
}

# Used when reminder.snooze_count >= 2 to counter chronic postponement.
ANTI_SNOOZE_TEMPLATES: dict[str, str] = {
    "friendly": (
        "Sudah beberapa kali di-snooze nih! Coba mulai 5 menit saja untuk "
        "'{checkpoint}' ({task}), sisa {hours} jam."
    ),
    "strict": (
        "Cukup menunda! '{checkpoint}' ({task}) sudah sering di-snooze dan tinggal "
        "{hours} jam. Mulai 10 menit sekarang."
    ),
    "minimal": (
        "[Anti-Snooze] {checkpoint} | {task} — sisa {hours} jam. Mulai langkah pertama sekarang."
    ),
}


def _normalize_tone(raw_tone: object) -> str:
    """Normalizes any coaching tone value to one of VALID_TONES."""
    if isinstance(raw_tone, str):
        candidate = raw_tone.strip().lower()
        if candidate in VALID_TONES:
            return candidate
    return DEFAULT_TONE


def get_user_coaching_tone(user: object) -> str:
    """Safely inspects user.preference.coaching_tone, defaulting to 'friendly'."""
    if user is None:
        return DEFAULT_TONE
    try:
        preference = getattr(user, "preference", None)
    except Exception:
        preference = None
    if preference is None:
        return DEFAULT_TONE
    raw_tone = getattr(preference, "coaching_tone", None)
    return _normalize_tone(raw_tone)


def _truncate_label(text: str, max_len: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[: max_len - 3].rstrip()}..."


def _format_hours(hours_left: float) -> str:
    if hours_left <= 0:
        return "0"
    if hours_left < 1:
        return f"{hours_left:.1f}"
    if hours_left < 2:
        formatted = f"{hours_left:.1f}"
        return formatted.rstrip("0").rstrip(".")
    return str(int(round(hours_left)))


def _format_fallback_message(
    tone: str,
    escalation_level: int,
    snooze_count: int,
    hours_left: float,
    checkpoint_title: str,
    task_title: str,
) -> str:
    """Formats the deterministic template and guarantees length <= 180 chars."""
    clamped_level = min(3, max(1, int(escalation_level or 1)))
    if snooze_count >= 2:
        template = ANTI_SNOOZE_TEMPLATES[tone]
    else:
        template = COACHING_TEMPLATES[(tone, clamped_level)]

    hours_str = _format_hours(hours_left)
    cp_label = _truncate_label(checkpoint_title, 40)
    task_label = _truncate_label(task_title, 40)

    rendered = template.format(hours=hours_str, checkpoint=cp_label, task=task_label)
    if len(rendered) > MAX_TEMPLATE_MESSAGE_LENGTH:
        # Shrink titles further to stay cleanly within 180 chars without cutting context.
        cp_label = _truncate_label(checkpoint_title, 24)
        task_label = _truncate_label(task_title, 24)
        rendered = template.format(hours=hours_str, checkpoint=cp_label, task=task_label)
    if len(rendered) > MAX_TEMPLATE_MESSAGE_LENGTH:
        rendered = f"{rendered[: MAX_TEMPLATE_MESSAGE_LENGTH - 3].rstrip()}..."
    return rendered


def _request_gemini_coaching(
    api_key: str,
    *,
    task_title: str,
    checkpoint_title: str,
    hours_left: float,
    escalation_level: int,
    snooze_count: int,
    tone: str,
) -> str | None:
    """Calls Gemini REST API with non-PII task metadata only. Returns None on failure."""
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    # Strictly non-PII metadata payload (no username, email, or personal identifiers).
    metadata = {
        "task_title": _truncate_label(task_title, 80),
        "checkpoint_title": _truncate_label(checkpoint_title, 80),
        "hours_left": round(hours_left, 1),
        "escalation_level": escalation_level,
        "snooze_count": snooze_count,
        "tone": tone,
    }
    prompt = (
        "Write exactly ONE concise, motivating academic reminder sentence in Indonesian "
        "(maximum 180 characters, plain text, no quotes or markdown) for a university student "
        f"using tone='{tone}'. If snooze_count >= 2, gently address procrastination. "
        f"Metadata: {json.dumps(metadata, ensure_ascii=False)}"
    )

    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.6,
                "maxOutputTokens": 90,
            },
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=GEMINI_TIMEOUT_SECONDS) as response:
            raw_body = response.read().decode("utf-8")
        parsed = json.loads(raw_body)
        candidates = parsed.get("candidates") or []
        if not candidates:
            return None
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        if not parts:
            return None
        text = " ".join(str(parts[0].get("text") or "").split()).strip("\"' ")
        if not text or len(text) > MAX_AI_MESSAGE_LENGTH:
            return None
        return text
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        OSError,
    ) as exc:
        logger.warning("Gemini coaching request failed; using fallback template: %s", exc)
        return None


def generate_coaching_message(
    reminder: Reminder,
    user_tone: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Generates a coaching message and returns (message, source) where source is 'gemini' or 'fallback'."""
    current_now = now or timezone.now()
    tone = (
        _normalize_tone(user_tone)
        if user_tone is not None
        else get_user_coaching_tone(reminder.user)
    )

    has_checkpoint = bool(reminder.checkpoint_id and reminder.checkpoint)
    target_deadline = (
        reminder.checkpoint.target_deadline if has_checkpoint else reminder.task.deadline
    )
    checkpoint_title = (
        reminder.checkpoint.title if has_checkpoint else "Target utama"
    )
    task_title = reminder.task.title

    delta_seconds = (target_deadline - current_now).total_seconds()
    hours_left = delta_seconds / 3600.0
    escalation_level = min(3, max(1, int(reminder.escalation_level or 1)))
    snooze_count = int(reminder.snooze_count or 0)

    fallback_text = _format_fallback_message(
        tone=tone,
        escalation_level=escalation_level,
        snooze_count=snooze_count,
        hours_left=hours_left,
        checkpoint_title=checkpoint_title,
        task_title=task_title,
    )

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return fallback_text, "fallback"

    ai_text = _request_gemini_coaching(
        api_key,
        task_title=task_title,
        checkpoint_title=checkpoint_title,
        hours_left=hours_left,
        escalation_level=escalation_level,
        snooze_count=snooze_count,
        tone=tone,
    )
    if not ai_text or len(ai_text) > MAX_AI_MESSAGE_LENGTH:
        return fallback_text, "fallback"
    return ai_text, "gemini"
