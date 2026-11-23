"""Progressive checkpoint reminder scheduler & due-reminder evaluator (F4.1)."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db.models import Q
from django.utils import timezone

from tasks.models import Checkpoint, Reminder, Task
from tasks.services.ai_coaching import generate_coaching_message

ESCALATION_TIERS: tuple[tuple[int, timedelta], ...] = (
    (1, timedelta(days=3)),
    (2, timedelta(days=1)),
    (3, timedelta(hours=3)),
)
PAST_TIER_GRACE_WINDOW = timedelta(hours=1)
CRITICAL_WINDOW = timedelta(hours=3)
ANTI_SNOOZE_THRESHOLD = 2


def sync_reminders_for_task(task: Task, now: datetime | None = None) -> int:
    """Synchronizes 3-tier WEB reminders for a Task and its incomplete Checkpoints.

    - If the task is DONE or ARCHIVED, cancels all PENDING/SNOOZED reminders and returns 0.
    - Cancels active reminders belonging to completed checkpoints.
    - For each incomplete Checkpoint (or the Task itself if it has no checkpoints),
      creates/updates WEB reminders at H-3d (L1), H-1d (L2), and H-3h (L3).
    - Skips L1/L2 tiers whose scheduled_for < now - 1h, while always ensuring L3 exists
      for active urgent or overdue targets.
    """
    current_now = now or timezone.now()

    if task.status in ("DONE", "ARCHIVED"):
        Reminder.objects.filter(
            task=task,
            status__in=("PENDING", "SNOOZED"),
        ).update(status="DONE")
        return 0

    # Cancel active reminders on checkpoints that have already been completed.
    Reminder.objects.filter(
        task=task,
        checkpoint__is_done=True,
        status__in=("PENDING", "SNOOZED"),
    ).update(status="DONE")

    all_checkpoints = list(task.checkpoints.all())
    targets: list[tuple[Checkpoint | None, datetime]] = []

    if all_checkpoints:
        # Remove obsolete task-level WEB reminders once checkpoints are defined.
        Reminder.objects.filter(
            task=task,
            checkpoint__isnull=True,
            channel="WEB",
            status__in=("PENDING", "SNOOZED"),
        ).update(status="DONE")

        for cp in all_checkpoints:
            if not cp.is_done:
                targets.append((cp, cp.target_deadline))
    else:
        targets.append((None, task.deadline))

    cutoff = current_now - PAST_TIER_GRACE_WINDOW
    synced_count = 0

    for checkpoint, target_deadline in targets:
        for level, offset in ESCALATION_TIERS:
            scheduled_for = target_deadline - offset

            if level != 3 and scheduled_for < cutoff:
                # Drop obsolete pending lower-tier reminders if the deadline moved up.
                Reminder.objects.filter(
                    task=task,
                    checkpoint=checkpoint,
                    escalation_level=level,
                    channel="WEB",
                    status="PENDING",
                ).delete()
                continue

            existing = (
                Reminder.objects.filter(
                    task=task,
                    checkpoint=checkpoint,
                    escalation_level=level,
                    channel="WEB",
                    status__in=("PENDING", "SNOOZED"),
                )
                .order_by("id")
                .first()
            )

            if existing is not None:
                update_fields: list[str] = []
                if existing.scheduled_for != scheduled_for:
                    existing.scheduled_for = scheduled_for
                    update_fields.append("scheduled_for")
                if existing.user_id != task.user_id:
                    existing.user = task.user
                    update_fields.append("user")
                if update_fields:
                    existing.save(update_fields=update_fields)
                synced_count += 1
                continue

            # Avoid recreating an identical reminder that the user explicitly dismissed.
            already_dismissed = Reminder.objects.filter(
                task=task,
                checkpoint=checkpoint,
                escalation_level=level,
                channel="WEB",
                status="DISMISSED",
                scheduled_for=scheduled_for,
            ).exists()
            if already_dismissed:
                continue

            Reminder.objects.create(
                user=task.user,
                task=task,
                checkpoint=checkpoint,
                scheduled_for=scheduled_for,
                escalation_level=level,
                channel="WEB",
                status="PENDING",
            )
            synced_count += 1

    return synced_count


def evaluate_due_reminders(now: datetime | None = None) -> list[Reminder]:
    """Evaluates due PENDING and expired SNOOZED reminders, applying auto-escalation and cached coaching."""
    current_now = now or timezone.now()

    due_queryset = (
        Reminder.objects.select_related("task", "checkpoint", "user")
        .filter(
            Q(status="PENDING", scheduled_for__lte=current_now)
            | Q(status="SNOOZED", snoozed_until__lte=current_now)
        )
        .order_by("-escalation_level", "scheduled_for")
    )

    active_due: list[Reminder] = []

    for reminder in due_queryset:
        is_task_finished = reminder.task.status in ("DONE", "ARCHIVED")
        is_checkpoint_done = bool(reminder.checkpoint_id and reminder.checkpoint.is_done)

        if is_task_finished or is_checkpoint_done:
            reminder.status = "DONE"
            reminder.save(update_fields=["status"])
            continue

        update_fields: list[str] = []

        # Wake up expired SNOOZED reminders so they re-enter the active alert queue.
        if reminder.status == "SNOOZED":
            reminder.status = "PENDING"
            reminder.snoozed_until = None
            update_fields.extend(["status", "snoozed_until"])
            if reminder.scheduled_for > current_now:
                reminder.scheduled_for = current_now
                update_fields.append("scheduled_for")
            if reminder.sent_at is not None:
                reminder.sent_at = None
                update_fields.append("sent_at")

        # DIGEST reminders carry their own pre-rendered summary; skip single-task escalation.
        if reminder.channel != "DIGEST":
            target_deadline = (
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
                update_fields.append("escalation_level")
                if reminder.sent_at is not None and "sent_at" not in update_fields:
                    reminder.sent_at = None
                    update_fields.append("sent_at")

            level_increased = reminder.escalation_level > previous_level
            if not reminder.message or level_increased:
                coaching_text, _source = generate_coaching_message(
                    reminder,
                    now=current_now,
                )
                reminder.message = coaching_text
                update_fields.append("message")

        if update_fields:
            reminder.save(update_fields=update_fields)

        active_due.append(reminder)

    active_due.sort(key=lambda r: (-r.escalation_level, r.scheduled_for))
    return active_due
