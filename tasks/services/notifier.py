"""Progressive checkpoint reminder scheduler & due-reminder evaluator (F4.1)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from tasks.models import Checkpoint, Reminder, Task
from tasks.services.ai_coaching import generate_coaching_message
from tasks.services.priority import recalculate_task_priority

ESCALATION_TIERS: tuple[tuple[int, timedelta], ...] = (
    (1, timedelta(days=3)),
    (2, timedelta(days=1)),
    (3, timedelta(hours=3)),
)
PAST_TIER_GRACE_WINDOW = timedelta(hours=1)
CRITICAL_WINDOW = timedelta(hours=3)
ANTI_SNOOZE_THRESHOLD = 2
VALID_SNOOZE_MINUTES = {15, 60, 180}
MAX_SNOOZE_COUNT = 3


def _ensure_aware(dt: datetime | None) -> datetime:
    """Normalizes None or naive datetimes to timezone-aware values when USE_TZ is enabled."""
    if dt is None:
        return timezone.now()
    if settings.USE_TZ and timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def active_due_reminders_qs(user, now: datetime | None = None):
    """Canonical active due WEB reminder queryset shared by navbar badge, inbox, and polling."""
    current_now = _ensure_aware(now or timezone.now())
    return (
        Reminder.objects.filter(user=user, channel="WEB")
        .exclude(task__status__in=("DONE", "ARCHIVED"))
        .exclude(checkpoint__is_done=True)
        .filter(
            Q(status__in=("PENDING", "SENT"), scheduled_for__lte=current_now)
            | Q(status="SNOOZED", snoozed_until__lte=current_now)
        )
    )


def sync_reminders_for_task(task: Task, now: datetime | None = None) -> int:
    """Synchronizes 3-tier WEB reminders for a Task and its incomplete Checkpoints.

    - If the task is DONE or ARCHIVED, closes non-DIGEST active reminders and returns 0.
    - Closes active non-DIGEST reminders belonging to completed checkpoints.
    - For each incomplete Checkpoint (or the Task itself if it has no checkpoints),
      creates/updates WEB reminders at H-3d (L1), H-1d (L2), and H-3h (L3) in batch queries.
    - Skips creating new past L1/L2 tiers when scheduled_for < now - 1h, and only prunes
      existing lower-tier PENDING reminders once the target enters the next tier's window.
    """
    current_now = _ensure_aware(now)

    with transaction.atomic():
        if task.status in ("DONE", "ARCHIVED"):
            Reminder.objects.filter(
                task=task,
                status__in=("PENDING", "SNOOZED", "SENT"),
            ).exclude(channel="DIGEST").update(status="DONE")
            return 0

        # Close active non-DIGEST reminders on checkpoints that have already been completed.
        Reminder.objects.filter(
            task=task,
            checkpoint__is_done=True,
            status__in=("PENDING", "SNOOZED", "SENT"),
        ).exclude(channel="DIGEST").update(status="DONE")

        all_checkpoints = list(task.checkpoints.all())
        targets: list[tuple[Checkpoint | None, datetime]] = []

        if all_checkpoints:
            # Close obsolete task-level WEB reminders once checkpoints are defined.
            Reminder.objects.filter(
                task=task,
                checkpoint__isnull=True,
                channel="WEB",
                status__in=("PENDING", "SNOOZED", "SENT"),
            ).update(status="DONE")

            for cp in all_checkpoints:
                if not cp.is_done:
                    targets.append((cp, _ensure_aware(cp.target_deadline)))
        else:
            targets.append((None, _ensure_aware(task.deadline)))

        if not targets:
            return 0

        # Load all existing WEB reminders for this task in one query.
        existing_web_reminders = list(
            Reminder.objects.filter(task=task, channel="WEB").order_by("id")
        )
        active_by_key: dict[tuple[int | None, int], Reminder] = {}
        pending_ids_by_key: dict[tuple[int | None, int], list[int]] = {}
        dismissed_keys: set[tuple[int | None, int]] = set()

        for rem in existing_web_reminders:
            key = (rem.checkpoint_id, rem.escalation_level)
            if rem.status in ("PENDING", "SNOOZED", "SENT"):
                if key not in active_by_key:
                    active_by_key[key] = rem
                if rem.status == "PENDING":
                    pending_ids_by_key.setdefault(key, []).append(rem.pk)
            elif rem.status == "DISMISSED":
                dismissed_keys.add(key)

        cutoff = current_now - PAST_TIER_GRACE_WINDOW
        delete_ids: list[int] = []
        to_update: list[Reminder] = []
        to_create: list[Reminder] = []
        synced_count = 0

        for checkpoint, target_deadline in targets:
            cp_id = checkpoint.pk if checkpoint is not None else None
            remaining = target_deadline - current_now

            for level, offset in ESCALATION_TIERS:
                key = (cp_id, level)
                scheduled_for = target_deadline - offset
                existing = active_by_key.get(key)

                if level != 3 and scheduled_for < cutoff:
                    in_next_tier_window = (
                        (level == 1 and remaining <= timedelta(days=1))
                        or (level == 2 and remaining <= timedelta(hours=3))
                    )
                    if in_next_tier_window:
                        delete_ids.extend(pending_ids_by_key.get(key, ()))
                        continue
                    if existing is None:
                        continue

                if existing is not None:
                    dirty = False
                    if existing.scheduled_for != scheduled_for:
                        existing.scheduled_for = scheduled_for
                        dirty = True
                        if scheduled_for > current_now:
                            existing.sent_at = None
                            existing.message = ""
                            if existing.status == "SENT":
                                existing.status = "PENDING"
                            if existing.status == "SNOOZED" and (
                                existing.snoozed_until is None
                                or _ensure_aware(existing.snoozed_until) >= target_deadline
                                or _ensure_aware(existing.snoozed_until) < scheduled_for
                            ):
                                existing.status = "PENDING"
                                existing.snoozed_until = None
                        elif existing.status == "SNOOZED" and (
                            existing.snoozed_until is None
                            or _ensure_aware(existing.snoozed_until) >= target_deadline
                        ):
                            existing.status = "PENDING"
                            existing.snoozed_until = None
                            existing.sent_at = None
                            existing.message = ""

                    if existing.user_id != task.user_id:
                        existing.user = task.user
                        dirty = True

                    if dirty:
                        to_update.append(existing)
                    synced_count += 1
                    continue

                # Avoid recreating an identical tier that the user explicitly dismissed.
                if key in dismissed_keys:
                    continue

                to_create.append(
                    Reminder(
                        user=task.user,
                        task=task,
                        checkpoint=checkpoint,
                        scheduled_for=scheduled_for,
                        escalation_level=level,
                        channel="WEB",
                        status="PENDING",
                    )
                )
                synced_count += 1

        if delete_ids:
            Reminder.objects.filter(pk__in=delete_ids).delete()
        if to_update:
            Reminder.objects.bulk_update(
                to_update,
                ["scheduled_for", "user", "status", "snoozed_until", "sent_at", "message"],
            )
        if to_create:
            Reminder.objects.bulk_create(to_create)

    return synced_count


def evaluate_due_reminders(now: datetime | None = None, user=None) -> list[Reminder]:
    """Evaluates due PENDING and expired SNOOZED reminders, applying auto-escalation, dedup, and coaching."""
    current_now = _ensure_aware(now)

    due_queryset = (
        Reminder.objects.select_related("task", "task__course", "checkpoint", "user")
        .filter(
            Q(status="PENDING", scheduled_for__lte=current_now)
            | Q(status="SNOOZED", snoozed_until__lte=current_now)
        )
        .order_by("-escalation_level", "scheduled_for", "-id")
    )
    if user is not None:
        due_queryset = due_queryset.filter(user=user)

    finished_ids: list[int] = []
    candidates: list[Reminder] = []

    for reminder in due_queryset:
        if reminder.channel != "DIGEST":
            is_task_finished = reminder.task.status in ("DONE", "ARCHIVED")
            is_checkpoint_done = bool(reminder.checkpoint_id and reminder.checkpoint.is_done)
            if is_task_finished or is_checkpoint_done:
                finished_ids.append(reminder.pk)
                continue
        candidates.append(reminder)

    if finished_ids:
        Reminder.objects.filter(pk__in=finished_ids).update(status="DONE")

    changed_fields_map: dict[int, set[str]] = {}
    level_increased_map: dict[int, bool] = {}

    for reminder in candidates:
        changed: set[str] = set()

        # Wake up expired SNOOZED reminders and clear cached message so fresh/anti-snooze copy regenerates.
        if reminder.status == "SNOOZED":
            reminder.status = "PENDING"
            reminder.snoozed_until = None
            changed.update(("status", "snoozed_until"))
            if _ensure_aware(reminder.scheduled_for) > current_now:
                reminder.scheduled_for = current_now
                changed.add("scheduled_for")
            if reminder.sent_at is not None:
                reminder.sent_at = None
                changed.add("sent_at")
            if reminder.message:
                reminder.message = ""
                changed.add("message")

        # DIGEST reminders carry their own pre-rendered summary; skip single-task escalation.
        if reminder.channel != "DIGEST":
            target_deadline = _ensure_aware(
                reminder.checkpoint.target_deadline
                if reminder.checkpoint_id and reminder.checkpoint
                else reminder.task.deadline
            )
            previous_level = reminder.escalation_level
            should_promote = (
                target_deadline <= current_now + CRITICAL_WINDOW
                or reminder.snooze_count >= ANTI_SNOOZE_THRESHOLD
            )
            if should_promote and reminder.escalation_level < 3:
                reminder.escalation_level = 3
                changed.add("escalation_level")
                if reminder.sent_at is not None:
                    reminder.sent_at = None
                    changed.add("sent_at")

            level_increased_map[reminder.pk] = reminder.escalation_level > previous_level

        changed_fields_map[reminder.pk] = changed

    # Prevent duplicate active alerts for the same (user_id, task_id, checkpoint_id):
    # keep the highest-escalation (and latest id) active reminder and mark superseded rows DISMISSED.
    non_digest_groups: dict[tuple[int, int, int | None], list[Reminder]] = {}
    for reminder in candidates:
        if reminder.channel != "DIGEST":
            group_key = (reminder.user_id, reminder.task_id, reminder.checkpoint_id)
            non_digest_groups.setdefault(group_key, []).append(reminder)

    superseded_ids: list[int] = []
    for group in non_digest_groups.values():
        if len(group) > 1:
            winner = max(group, key=lambda r: (r.escalation_level, r.pk))
            for rem in group:
                if rem.pk != winner.pk:
                    superseded_ids.append(rem.pk)

    superseded_set = set(superseded_ids)
    if superseded_ids:
        Reminder.objects.filter(pk__in=superseded_ids).update(status="DISMISSED")

    active_due: list[Reminder] = []
    for reminder in candidates:
        if reminder.pk in superseded_set:
            continue

        changed = changed_fields_map[reminder.pk]
        if reminder.channel != "DIGEST":
            if not reminder.message or level_increased_map.get(reminder.pk, False):
                coaching_text, _source = generate_coaching_message(
                    reminder,
                    now=current_now,
                )
                reminder.message = coaching_text
                changed.add("message")

        if changed:
            reminder.save(update_fields=sorted(changed))

        active_due.append(reminder)

    active_due.sort(key=lambda r: (-r.escalation_level, r.scheduled_for))
    return active_due


def snooze_reminder_action(
    reminder_pk: int,
    *,
    minutes: int = 60,
    user=None,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Atomically validates and snoozes a reminder across Web and Telegram channels."""
    if isinstance(minutes, bool) or minutes not in VALID_SNOOZE_MINUTES:
        return False, {
            "ok": False,
            "reason": "invalid_duration",
            "error": "Durasi tunda tidak valid. Pilih 15 menit, 1 jam, atau 3 jam.",
        }

    current_now = _ensure_aware(now)

    with transaction.atomic():
        qs = Reminder.objects.select_for_update().select_related("task", "checkpoint").filter(
            pk=reminder_pk
        )
        if user is not None:
            qs = qs.filter(user=user)
        reminder = qs.first()

        if reminder is None:
            return False, {
                "ok": False,
                "reason": "not_found",
                "error": "Reminder tidak ditemukan.",
                "reminder_id": reminder_pk,
            }

        if (
            reminder.status in ("DONE", "DISMISSED")
            or reminder.task.status in ("DONE", "ARCHIVED")
            or (reminder.checkpoint_id and reminder.checkpoint.is_done)
        ):
            return False, {
                "ok": False,
                "reason": "already_closed",
                "error": "Reminder sudah selesai atau ditutup.",
                "reminder_id": reminder.pk,
            }

        if reminder.snooze_count >= MAX_SNOOZE_COUNT:
            return False, {
                "ok": False,
                "reason": "max_snooze_reached",
                "error": "Batas tunda maksimal (3x) sudah tercapai! Yuk mulai checkpoint ini sekarang.",
                "reminder_id": reminder.pk,
                "snooze_count": reminder.snooze_count,
            }

        proposed = current_now + timedelta(minutes=minutes)
        target_deadline = _ensure_aware(
            reminder.checkpoint.target_deadline
            if (reminder.checkpoint_id and reminder.checkpoint)
            else reminder.task.deadline
        )
        if proposed >= target_deadline:
            return False, {
                "ok": False,
                "reason": "deadline_too_close",
                "error": "Tidak bisa menunda melewati tenggat waktu target.",
                "reminder_id": reminder.pk,
            }

        reminder.status = "SNOOZED"
        reminder.snoozed_until = proposed
        reminder.snooze_count += 1
        update_fields = ["status", "snoozed_until", "snooze_count"]
        if reminder.snooze_count >= ANTI_SNOOZE_THRESHOLD:
            reminder.escalation_level = 3
            reminder.message = ""
            update_fields.extend(["escalation_level", "message"])
        reminder.save(update_fields=update_fields)

    return True, {
        "ok": True,
        "status": "SNOOZED",
        "reminder_id": reminder.pk,
        "snooze_count": reminder.snooze_count,
        "snoozed_until": reminder.snoozed_until.isoformat(),
        "escalation_level": reminder.escalation_level,
        "reminder": reminder,
    }


def complete_reminder_action(
    reminder_pk: int,
    *,
    user=None,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Atomically marks a reminder DONE and syncs linked checkpoint/task state and sibling reminders."""
    current_now = _ensure_aware(now)

    with transaction.atomic():
        qs = Reminder.objects.select_for_update().select_related("task", "checkpoint").filter(
            pk=reminder_pk
        )
        if user is not None:
            qs = qs.filter(user=user)
        reminder = qs.first()

        if reminder is None:
            return False, {
                "ok": False,
                "reason": "not_found",
                "error": "Reminder tidak ditemukan.",
                "reminder_id": reminder_pk,
            }

        reminder.status = "DONE"
        reminder.save(update_fields=["status"])

        if reminder.channel != "DIGEST":
            task = reminder.task
            if reminder.checkpoint_id and reminder.checkpoint:
                cp = reminder.checkpoint
                if not cp.is_done or cp.completed_at is None:
                    cp.is_done = True
                    cp.completed_at = cp.completed_at or current_now
                    cp.save(update_fields=["is_done", "completed_at"])
                task.sync_status_from_checkpoints(save=False)
            else:
                if task.checkpoints.count() == 0:
                    if task.status != "ARCHIVED":
                        task.status = "DONE"
                        task.completed_at = task.completed_at or current_now
                else:
                    task.sync_status_from_checkpoints(save=False)
            recalculate_task_priority(task, now=current_now)
            task.save(
                update_fields=[
                    "status",
                    "completed_at",
                    "priority_score",
                    "priority_level",
                    "priority_reason",
                    "updated_at",
                ]
            )
            sync_reminders_for_task(task, now=current_now)

    return True, {
        "ok": True,
        "status": "DONE",
        "reminder_id": reminder.pk,
        "task_id": reminder.task_id,
        "checkpoint_id": reminder.checkpoint_id,
        "reminder": reminder,
    }
