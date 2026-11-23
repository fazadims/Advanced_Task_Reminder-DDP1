"""Anti-spam Daily Digest builder with priority capping & quiet-hours awareness (F4.3)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from django.utils import timezone

from tasks.models import Checkpoint, Task

DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(7, 0)
DEFAULT_MAX_ALERTS_PER_DAY = 5
MAX_DIGEST_ITEMS = 5
UPCOMING_WINDOW = timedelta(hours=48)


def _parse_time_value(value: Any, fallback: time) -> time:
    """Safely parses a datetime.time or 'HH:MM' string into a time object."""
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        parts = value.strip().split(":")
        if len(parts) >= 2:
            try:
                hour = int(parts[0])
                minute = int(parts[1])
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return time(hour, minute)
            except ValueError:
                pass
    return fallback


def _is_within_quiet_hours(local_time: time, start: time, end: time) -> bool:
    """Returns True if local_time falls within [start, end), supporting overnight windows."""
    if start == end:
        return False
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def get_user_digest_preferences(user: Any, now: datetime | None = None) -> dict[str, Any]:
    """Safely inspects optional user.preference for quiet hours and max_alerts_per_day."""
    current_now = now or timezone.now()
    local_now = timezone.localtime(current_now) if timezone.is_aware(current_now) else current_now

    try:
        pref = getattr(user, "preference", None)
    except Exception:
        pref = None

    raw_start = None
    raw_end = None
    raw_max = None
    if pref is not None:
        raw_start = getattr(pref, "quiet_hours_start", None)
        if raw_start is None:
            raw_start = getattr(pref, "quiet_start", None)
        raw_end = getattr(pref, "quiet_hours_end", None)
        if raw_end is None:
            raw_end = getattr(pref, "quiet_end", None)
        raw_max = getattr(pref, "max_alerts_per_day", None)

    quiet_start = _parse_time_value(raw_start, DEFAULT_QUIET_START)
    quiet_end = _parse_time_value(raw_end, DEFAULT_QUIET_END)

    try:
        max_alerts = int(raw_max) if raw_max is not None else DEFAULT_MAX_ALERTS_PER_DAY
        if max_alerts < 1:
            max_alerts = DEFAULT_MAX_ALERTS_PER_DAY
    except (TypeError, ValueError):
        max_alerts = DEFAULT_MAX_ALERTS_PER_DAY

    return {
        "quiet_hours_start": quiet_start,
        "quiet_hours_end": quiet_end,
        "quiet_hours_label": f"{quiet_start.strftime('%H:%M')}-{quiet_end.strftime('%H:%M')}",
        "is_quiet_hours": _is_within_quiet_hours(local_now.time(), quiet_start, quiet_end),
        "max_alerts_per_day": max_alerts,
    }


def _format_local_deadline(dt: datetime) -> str:
    local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return local_dt.strftime("%d %b %H:%M")


def build_daily_digest(
    user: Any,
    slot: str = "MORNING",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Builds a prioritized, anti-spam Morning or Evening digest capped at top 5 items."""
    current_now = now or timezone.now()
    local_now = timezone.localtime(current_now) if timezone.is_aware(current_now) else current_now
    digest_date = local_now.date()

    normalized_slot = (slot or "MORNING").strip().upper()
    if normalized_slot not in ("MORNING", "EVENING"):
        normalized_slot = "MORNING"

    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    horizon_48h = current_now + UPCOMING_WINDOW

    prefs = get_user_digest_preferences(user, now=current_now)

    overdue_tasks = list(
        Task.objects.filter(user=user, deadline__lt=current_now)
        .exclude(status__in=("DONE", "ARCHIVED"))
        .select_related("course")
        .order_by("deadline", "-priority_score")
    )

    today_checkpoints = list(
        Checkpoint.objects.filter(
            task__user=user,
            is_done=False,
            target_deadline__gte=day_start,
            target_deadline__lt=day_end,
        )
        .exclude(task__status__in=("DONE", "ARCHIVED"))
        .select_related("task", "task__course")
        .order_by("target_deadline", "order")
    )

    upcoming_48h_tasks = list(
        Task.objects.filter(
            user=user,
            deadline__gte=current_now,
            deadline__lte=horizon_48h,
        )
        .exclude(status__in=("DONE", "ARCHIVED"))
        .select_related("course")
        .order_by("deadline", "-priority_score")
    )

    unified: list[dict[str, Any]] = []

    for task in overdue_tasks:
        unified.append(
            {
                "kind": "overdue_task",
                "title": task.title,
                "task_id": task.pk,
                "task_title": task.title,
                "checkpoint_id": None,
                "course_code": task.course.code if task.course_id else "",
                "deadline": task.deadline,
                "is_overdue": True,
                "priority_level": task.priority_level,
                "task": task,
                "checkpoint": None,
            }
        )

    for cp in today_checkpoints:
        unified.append(
            {
                "kind": "today_checkpoint",
                "title": cp.title,
                "task_id": cp.task_id,
                "task_title": cp.task.title,
                "checkpoint_id": cp.pk,
                "course_code": cp.task.course.code if cp.task.course_id else "",
                "deadline": cp.target_deadline,
                "is_overdue": cp.target_deadline < current_now,
                "priority_level": cp.task.priority_level,
                "task": cp.task,
                "checkpoint": cp,
            }
        )

    for task in upcoming_48h_tasks:
        unified.append(
            {
                "kind": "upcoming_48h_task",
                "title": task.title,
                "task_id": task.pk,
                "task_title": task.title,
                "checkpoint_id": None,
                "course_code": task.course.code if task.course_id else "",
                "deadline": task.deadline,
                "is_overdue": False,
                "priority_level": task.priority_level,
                "task": task,
                "checkpoint": None,
            }
        )

    # Sort by (is_overdue desc, deadline asc)
    unified.sort(key=lambda item: (not item["is_overdue"], item["deadline"]))

    total_items = len(unified)
    cap = min(MAX_DIGEST_ITEMS, prefs["max_alerts_per_day"])
    items = unified[:cap]
    overflow_count = max(0, total_items - len(items))

    slot_label = "Morning Briefing" if normalized_slot == "MORNING" else "Evening Recap"
    header = f"ATR {slot_label} ({digest_date.strftime('%d %b %Y')})"

    if total_items == 0:
        summary_text = (
            f"{header}\n"
            "Tidak ada tugas overdue, checkpoint hari ini, atau deadline 48 jam ke depan. Mantap!"
        )
    else:
        lines = [header]
        for idx, item in enumerate(items, start=1):
            if item["kind"] == "overdue_task":
                tag = "[OVERDUE]"
                label = item["title"]
            elif item["kind"] == "today_checkpoint":
                tag = "[OVERDUE CP]" if item["is_overdue"] else "[CHECKPOINT]"
                label = f"{item['title']} ({item['task_title']})"
            else:
                tag = "[48H]"
                label = item["title"]

            course_prefix = f"[{item['course_code']}] " if item["course_code"] else ""
            dl_text = _format_local_deadline(item["deadline"])
            lines.append(f"{idx}. {tag} {course_prefix}{label} — {dl_text}")

        if overflow_count > 0:
            lines.append(
                f"...dan {overflow_count} item lainnya. Buka ATR untuk melihat daftar lengkap."
            )
        summary_text = "\n".join(lines)

    return {
        "items": items,
        "overflow_count": overflow_count,
        "overdue_tasks": overdue_tasks,
        "today_checkpoints": today_checkpoints,
        "upcoming_48h_tasks": upcoming_48h_tasks,
        "total_items": total_items,
        "slot": normalized_slot,
        "digest_date": digest_date,
        "summary_text": summary_text,
        "quiet_hours_start": prefs["quiet_hours_start"],
        "quiet_hours_end": prefs["quiet_hours_end"],
        "quiet_hours_label": prefs["quiet_hours_label"],
        "is_quiet_hours": prefs["is_quiet_hours"],
        "max_alerts_per_day": prefs["max_alerts_per_day"],
    }
