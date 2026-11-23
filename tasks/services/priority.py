"""Deterministic priority scoring engine for academic tasks."""

from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.utils import timezone

from tasks.models import Task

DIFFICULTY_LABELS: dict[int, str] = {
    1: "Sangat Mudah",
    2: "Mudah",
    3: "Sedang",
    4: "Sulit",
    5: "Sangat Sulit",
}


def _normalize_dt(dt: datetime, reference: datetime) -> datetime:
    """Aligns timezone awareness between two datetime instances."""
    if settings.USE_TZ:
        if timezone.is_aware(reference) and timezone.is_naive(dt):
            return timezone.make_aware(dt, timezone.get_current_timezone())
        if timezone.is_naive(reference) and timezone.is_aware(dt):
            return timezone.make_naive(dt, timezone.get_current_timezone())
    return dt


def _format_deadline_reason(hours_left: float) -> str:
    if hours_left <= 0:
        overdue_hours = abs(hours_left)
        if overdue_hours < 1:
            return "Sudah melewati deadline"
        if overdue_hours < 24:
            return f"Terlambat {max(1, int(round(overdue_hours)))} jam"
        return f"Terlambat {max(1, int(round(overdue_hours / 24.0)))} hari"
    if hours_left < 1:
        mins_left = max(1, int(round(hours_left * 60.0)))
        return f"Deadline {mins_left} mnt lagi"
    if hours_left <= 48:
        return f"Deadline {max(1, int(round(hours_left)))} jam lagi"
    return f"Deadline {max(1, int(round(hours_left / 24.0)))} hari lagi"


def _format_weight_value(weight: float) -> str:
    if abs(weight - round(weight)) < 1e-6:
        return str(int(round(weight)))
    return f"{weight:.2f}".rstrip("0").rstrip(".")


def recalculate_task_priority(
    task: Task,
    now: datetime | None = None,
) -> tuple[float, str, str]:
    """Computes priority_score (0–100), priority_level, and Indonesian priority_reason.

    Mutates ``task.priority_score``, ``task.priority_level``, and ``task.priority_reason``
    in memory without calling ``task.save()`` so the caller controls persistence.
    """
    if task.status in ("DONE", "ARCHIVED"):
        score = 0.0
        level = "LOW"
        reason = "Tugas sudah selesai atau diarsipkan."
        task.priority_score = score
        task.priority_level = level
        task.priority_reason = reason
        return score, level, reason

    if task.deadline is None:
        score = 0.0
        level = "LOW"
        reason = "Tenggat waktu belum ditentukan."
        task.priority_score = score
        task.priority_level = level
        task.priority_reason = reason
        return score, level, reason

    current_now = now if now is not None else timezone.now()
    deadline = _normalize_dt(task.deadline, current_now)
    current_now = _normalize_dt(current_now, deadline)
    hours_left = (deadline - current_now).total_seconds() / 3600.0

    # 1. Deadline urgency (0–50 pts)
    if hours_left <= 0:
        deadline_pts = 50.0
    elif hours_left <= 24:
        deadline_pts = 45.0
    elif hours_left <= 48:
        deadline_pts = 36.0
    elif hours_left <= 120:
        deadline_pts = 24.0
    elif hours_left <= 240:
        deadline_pts = 14.0
    else:
        deadline_pts = 6.0

    # 2. BRP weight (0–25 pts)
    weight_val = max(0.0, float(task.weight_percentage or 0))
    weight_pts = min(25.0, weight_val * 0.85)

    # 3. Difficulty (0–15 pts)
    difficulty_val = max(1, min(5, int(task.difficulty or 3)))
    difficulty_pts = difficulty_val * 3.0

    # 4. Group project & effort bonus (0–10 pts)
    bonus_pts = 0.0
    if task.is_group_project:
        bonus_pts += 5.0
    estimated_minutes = int(task.estimated_minutes or 0)
    if estimated_minutes >= 180:
        bonus_pts += 5.0
    elif estimated_minutes >= 90:
        bonus_pts += 2.0

    raw_score = deadline_pts + weight_pts + difficulty_pts + bonus_pts
    score = round(min(100.0, max(0.0, raw_score)), 1)

    if score >= 75:
        level = "CRITICAL"
    elif score >= 55:
        level = "HIGH"
    elif score >= 35:
        level = "MEDIUM"
    else:
        level = "LOW"

    reason_parts = [_format_deadline_reason(hours_left)]
    if weight_val > 0:
        reason_parts.append(f"Bobot BRP {_format_weight_value(weight_val)}%")
    reason_parts.append(
        f"Kesulitan {DIFFICULTY_LABELS[difficulty_val]} ({difficulty_val}/5)"
    )
    if task.is_group_project:
        reason_parts.append("Proyek kelompok")

    reason = " · ".join(reason_parts)[:255]

    task.priority_score = score
    task.priority_level = level
    task.priority_reason = reason
    return score, level, reason
