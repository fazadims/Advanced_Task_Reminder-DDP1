"""Template filters and simple tags for Task UX badges, durations, and urgency indicators."""

from __future__ import annotations

from datetime import timedelta

from django import template
from django.utils import timezone

from tasks.models import Checkpoint

register = template.Library()

DIFFICULTY_META: dict[int, dict[str, object]] = {
    1: {
        "level": 1,
        "label": "Sangat Mudah",
        "short_label": "Sangat Mudah",
        "description": "Bisa selesai cepat dalam < 30 menit tanpa hambatan.",
        "badge_class": "bg-emerald-50 text-emerald-700 border-emerald-200",
        "bar_color": "bg-emerald-500",
    },
    2: {
        "level": 2,
        "label": "Mudah",
        "short_label": "Mudah",
        "description": "Materi sudah cukup dipahami, butuh 1 sesi fokus.",
        "badge_class": "bg-teal-50 text-teal-700 border-teal-200",
        "bar_color": "bg-teal-500",
    },
    3: {
        "level": 3,
        "label": "Sedang",
        "short_label": "Sedang",
        "description": "Butuh 1–2 sesi fokus dan pemahaman konsep yang rapi.",
        "badge_class": "bg-amber-50 text-amber-800 border-amber-200",
        "bar_color": "bg-amber-500",
    },
    4: {
        "level": 4,
        "label": "Sulit",
        "short_label": "Sulit",
        "description": "Kompleks — pecah jadi beberapa checkpoint & mulai lebih awal.",
        "badge_class": "bg-orange-50 text-orange-800 border-orange-200",
        "bar_color": "bg-orange-500",
    },
    5: {
        "level": 5,
        "label": "Sangat Sulit",
        "short_label": "Sangat Sulit",
        "description": "Tantangan tinggi / proyek besar — cicil jauh-jauh hari.",
        "badge_class": "bg-rose-50 text-rose-800 border-rose-200",
        "bar_color": "bg-rose-500",
    },
}

STATUS_BADGE_CLASSES: dict[str, str] = {
    "DONE": "bg-emerald-50 text-emerald-700 border-emerald-200",
    "IN_PROGRESS": "bg-sky-50 text-sky-700 border-sky-200",
    "ARCHIVED": "bg-slate-100 text-slate-600 border-slate-200",
    "TODO": "bg-amber-50 text-amber-800 border-amber-200",
}

PRIORITY_META_MAP: dict[str, dict[str, str]] = {
    "CRITICAL": {
        "label": "Kritis",
        "badge_class": "bg-rose-50 text-rose-700 border-rose-200",
        "stripe_class": "border-l-rose-500",
    },
    "HIGH": {
        "label": "Tinggi",
        "badge_class": "bg-amber-50 text-amber-800 border-amber-200",
        "stripe_class": "border-l-amber-500",
    },
    "MEDIUM": {
        "label": "Sedang",
        "badge_class": "bg-indigo-50 text-indigo-700 border-indigo-200",
        "stripe_class": "border-l-indigo-500",
    },
    "LOW": {
        "label": "Rendah",
        "badge_class": "bg-slate-100 text-slate-600 border-slate-200",
        "stripe_class": "border-l-slate-300",
    },
}

COURSE_PALETTE: tuple[str, ...] = (
    "bg-indigo-50 text-indigo-700 border-indigo-200",
    "bg-teal-50 text-teal-700 border-teal-200",
    "bg-sky-50 text-sky-700 border-sky-200",
    "bg-violet-50 text-violet-700 border-violet-200",
    "bg-amber-50 text-amber-800 border-amber-200",
    "bg-rose-50 text-rose-700 border-rose-200",
)


@register.filter
def human_duration(minutes) -> str:
    """Formats a duration in minutes into concise Indonesian text."""
    try:
        total = int(minutes or 0)
    except (TypeError, ValueError):
        total = 0

    if total <= 0:
        return "0 mnt"
    if total < 60:
        return f"{total} mnt"
    hours, rem = divmod(total, 60)
    if rem == 0:
        return f"{hours} jam"
    return f"{hours} jam {rem} mnt"


@register.filter
def relative_deadline(dt) -> dict[str, object]:
    """Returns relative deadline copy, tone, Tailwind badge classes, and overdue flag."""
    if dt is None:
        return {
            "text": "—",
            "tone": "normal",
            "badge_class": "bg-slate-100 text-slate-600 border-slate-200",
            "is_overdue": False,
        }

    now = timezone.now()
    target = dt
    if timezone.is_aware(now) and timezone.is_naive(target):
        target = timezone.make_aware(target, timezone.get_current_timezone())
    elif timezone.is_naive(now) and timezone.is_aware(target):
        now = timezone.make_aware(now, timezone.get_current_timezone())

    diff_seconds = (target - now).total_seconds()
    if diff_seconds < 0:
        abs_seconds = abs(diff_seconds)
        if abs_seconds < 3600:
            mins = max(1, int(abs_seconds // 60))
            text = f"Terlambat {mins} mnt"
        elif abs_seconds < 86400:
            hrs = max(1, int(abs_seconds // 3600))
            text = f"Terlambat {hrs} jam"
        else:
            days = max(1, int(abs_seconds // 86400))
            text = f"Terlambat {days} hari"
        return {
            "text": text,
            "tone": "overdue",
            "badge_class": "bg-rose-50 text-rose-700 border-rose-200",
            "is_overdue": True,
        }

    if diff_seconds < 3600:
        if diff_seconds < 60:
            text = "< 1 mnt lagi"
        elif diff_seconds < 120:
            text = "1 mnt lagi"
        else:
            mins = int(diff_seconds // 60)
            text = f"{mins} mnt lagi"
        return {
            "text": text,
            "tone": "urgent",
            "badge_class": "bg-rose-50 text-rose-700 border-rose-200",
            "is_overdue": False,
        }

    if diff_seconds <= 86400:
        hrs = max(1, int(round(diff_seconds / 3600.0)))
        return {
            "text": f"{hrs} jam lagi",
            "tone": "urgent",
            "badge_class": "bg-amber-50 text-amber-800 border-amber-200",
            "is_overdue": False,
        }

    if diff_seconds <= 172800:
        local_now = timezone.localtime(now) if timezone.is_aware(now) else now
        local_target = timezone.localtime(target) if timezone.is_aware(target) else target
        if local_target.date() == (local_now + timedelta(days=1)).date():
            text = f"Besok, {local_target.strftime('%H:%M')}"
        else:
            hrs = max(1, int(round(diff_seconds / 3600.0)))
            text = f"{hrs} jam lagi"
        return {
            "text": text,
            "tone": "soon",
            "badge_class": "bg-amber-50 text-amber-800 border-amber-200",
            "is_overdue": False,
        }

    days = max(2, int(round(diff_seconds / 86400.0)))
    return {
        "text": f"{days} hari lagi",
        "tone": "normal",
        "badge_class": "bg-slate-100 text-slate-600 border-slate-200",
        "is_overdue": False,
    }


@register.filter
def difficulty_info(level) -> dict[str, object]:
    """Returns DIFFICULTY_META dictionary for a clamped difficulty level (1..5)."""
    try:
        parsed = int(level)
    except (TypeError, ValueError):
        parsed = 3
    clamped = max(1, min(5, parsed))
    return DIFFICULTY_META[clamped]


@register.simple_tag
def difficulty_levels() -> list[dict[str, object]]:
    """Returns ordered list of all 5 difficulty metadata dictionaries."""
    return [DIFFICULTY_META[i] for i in range(1, 6)]


@register.simple_tag
def phase_choices():
    """Returns Checkpoint.PHASE_CHOICES for template selectors."""
    return Checkpoint.PHASE_CHOICES


@register.filter
def status_badge_class(status) -> str:
    """Returns canonical Tailwind badge classes for a Task status."""
    key = str(status or "TODO").upper()
    return STATUS_BADGE_CLASSES.get(key, STATUS_BADGE_CLASSES["TODO"])


@register.filter
def priority_meta(level) -> dict[str, str]:
    """Returns Indonesian label and Tailwind classes for a Task priority level."""
    key = str(level or "LOW").upper()
    return PRIORITY_META_MAP.get(key, PRIORITY_META_MAP["LOW"])


@register.filter
def course_badge_class(course) -> str:
    """Returns a deterministic harmonious Tailwind badge class for a Course."""
    if course is None:
        return "bg-slate-100 text-slate-600 border-slate-200"
    pk = getattr(course, "pk", None)
    if pk is not None:
        idx = int(pk) % len(COURSE_PALETTE)
    else:
        code = str(getattr(course, "code", "") or "")
        idx = sum(ord(ch) for ch in code) % len(COURSE_PALETTE)
    return COURSE_PALETTE[idx]


@register.filter
def urgency_stripe_class(task) -> str:
    """Returns a left-border Tailwind color class reflecting task status and urgency."""
    if task is None:
        return "border-l-indigo-500"
    status = getattr(task, "status", "")
    if status == "DONE":
        return "border-l-emerald-500"
    if status == "ARCHIVED":
        return "border-l-slate-300"

    now = timezone.now()
    deadline = getattr(task, "deadline", None)
    if deadline is not None:
        if timezone.is_aware(now) and timezone.is_naive(deadline):
            deadline = timezone.make_aware(deadline, timezone.get_current_timezone())
        elif timezone.is_naive(now) and timezone.is_aware(deadline):
            now = timezone.make_aware(now, timezone.get_current_timezone())

    priority_level = str(getattr(task, "priority_level", "") or "").upper()
    if (deadline is not None and deadline < now) or priority_level == "CRITICAL":
        return "border-l-rose-500"
    if priority_level == "HIGH" or (
        deadline is not None and deadline <= now + timedelta(hours=48)
    ):
        return "border-l-amber-500"
    return "border-l-indigo-500"


@register.filter
def checkpoint_progress(task) -> dict[str, int]:
    """Prefetch-safe checkpoint completion counts and percentage for a Task."""
    if task is None:
        return {"done": 0, "total": 0, "percent": 0}
    checkpoints = list(task.checkpoints.all())
    total = len(checkpoints)
    if total == 0:
        pct = 100 if getattr(task, "status", "") == "DONE" else 0
        return {"done": 0, "total": 0, "percent": pct}
    done = sum(1 for cp in checkpoints if cp.is_done)
    return {"done": done, "total": total, "percent": round((done / total) * 100)}


@register.filter
def abs_int(value) -> int:
    """Returns the non-negative integer absolute value."""
    try:
        return abs(int(value or 0))
    except (TypeError, ValueError):
        return 0


@register.filter
def format_weight(value) -> str:
    """Formats a BRP weight percentage concisely (e.g. 15.00 -> '15%', 12.50 -> '12.5%')."""
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        num = 0.0
    if abs(num - round(num)) < 1e-6:
        return f"{int(round(num))}%"
    formatted = f"{num:.2f}".rstrip("0").rstrip(".")
    return f"{formatted}%"


@register.filter
def reminder_escalation_label(level) -> str:
    """Returns Indonesian label for a Reminder escalation tier (1..3)."""
    try:
        parsed = int(level or 1)
    except (TypeError, ValueError):
        parsed = 1
    return {
        1: "Ringan (H-3 hari)",
        2: "Mendesak (H-1 hari)",
        3: "Kritis (H-3 jam)",
    }.get(parsed, "Pengingat")


@register.filter
def reminder_status_label(status) -> str:
    """Returns Indonesian label for a Reminder status."""
    key = str(status or "")
    return {
        "PENDING": "Menunggu",
        "SENT": "Terkirim",
        "SNOOZED": "Ditunda",
        "DISMISSED": "Ditutup",
        "DONE": "Selesai",
    }.get(key, key)


@register.filter
def focus_mode_label(mode) -> str:
    """Returns display label for a FocusSession mode."""
    key = str(mode or "")
    return {
        "POMODORO_25_5": "Pomodoro (25/5)",
        "DEEP_50_10": "Deep Work (50/10)",
        "CUSTOM": "Kustom",
    }.get(key, key)
