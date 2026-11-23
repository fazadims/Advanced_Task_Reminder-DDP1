"""Person B views: Task/Checkpoint CRUD, Auto-Checkpoints, Focus Pomodoro, AI Suggestions, Alerts/Snooze, Digest, Telegram, and Coaching."""

import json
import os
import secrets
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from brp.models import Course
from tasks.forms import CheckpointForm, TaskForm
from tasks.models import (
    Checkpoint,
    DailyDigestLog,
    FocusSession,
    Reminder,
    Task,
    TelegramLink,
)
from tasks.services.ai_checkpoints import suggest_checkpoints
from tasks.services.ai_coaching import generate_coaching_message
from tasks.services.checkpoint_gen import generate_checkpoints_for_task
from tasks.services.digest import build_daily_digest
from tasks.services.notifier import evaluate_due_reminders, sync_reminders_for_task
from tasks.services.telegram_bot import handle_telegram_update, send_telegram_alert

VALID_SNOOZE_MINUTES = {15, 60, 180}
VALID_PHASES = {choice[0] for choice in Checkpoint.PHASE_CHOICES}


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _parse_json_or_post(request) -> dict:
    """Supports both JSON fetch() bodies and standard form-encoded POST payloads."""
    if request.content_type and "application/json" in request.content_type:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}
    return request.POST.dict()


def _today_focus_minutes(user) -> int:
    today = timezone.localdate()
    agg = FocusSession.objects.filter(
        user=user,
        status="COMPLETED",
        started_at__date=today,
    ).aggregate(total=Sum("actual_minutes"))
    return int(agg["total"] or 0)


# =============================================================================
# F3.1 & F6.2 — Task CRUD, Detail, Archive & Paginated History
# =============================================================================


@login_required
def task_list(request):
    """Filterable task workspace scoped strictly to the logged-in user."""
    qs = (
        Task.objects.filter(user=request.user)
        .select_related("course", "component")
        .prefetch_related("checkpoints")
    )

    course_id = request.GET.get("course", "").strip()
    status_filter = request.GET.get("status", "").strip()
    category_filter = request.GET.get("category", "").strip()

    if course_id.isdigit():
        qs = qs.filter(course_id=int(course_id))
    if status_filter:
        qs = qs.filter(status=status_filter)
    else:
        qs = qs.exclude(status="ARCHIVED")
    if category_filter:
        qs = qs.filter(category=category_filter)

    courses = Course.objects.filter(user=request.user)
    return render(
        request,
        "tasks/task_list.html",
        {
            "tasks": qs,
            "courses": courses,
            "status_choices": Task.STATUS_CHOICES,
            "category_choices": Task.CATEGORY_CHOICES,
        },
    )


@login_required
def task_create(request):
    """Creates a Task, optionally auto-generates checkpoints (F3.2), and syncs reminders (F4.1)."""
    if request.method == "POST":
        form = TaskForm(request.POST, user=request.user)
        if form.is_valid():
            task = form.save(commit=False)
            task.user = request.user
            task.save()
            if form.cleaned_data.get("auto_generate_checkpoints", True):
                generate_checkpoints_for_task(task)
            sync_reminders_for_task(task)
            messages.success(request, f"Task '{task.title}' created.")
            return redirect("tasks:task_detail", pk=task.pk)
    else:
        form = TaskForm(user=request.user)

    return render(
        request,
        "tasks/task_form.html",
        {"form": form, "is_edit": False},
    )


@login_required
def task_detail(request, pk: int):
    """Task detail workspace with HTMX checkpoints, focus logs, and upcoming reminders (F3.1 & F6.2)."""
    task = get_object_or_404(
        Task.objects.select_related("course", "component").prefetch_related("checkpoints"),
        pk=pk,
        user=request.user,
    )
    checkpoints = task.checkpoints.all()
    focus_sessions = task.focus_sessions.select_related("checkpoint")[:10]
    reminders = task.reminders.select_related("checkpoint").exclude(
        status__in=["DONE", "DISMISSED"]
    )[:10]

    return render(
        request,
        "tasks/task_detail.html",
        {
            "task": task,
            "checkpoints": checkpoints,
            "checkpoint_form": CheckpointForm(),
            "focus_sessions": focus_sessions,
            "total_focus_minutes": task.total_focus_minutes(),
            "reminders": reminders,
        },
    )


@login_required
def task_update(request, pk: int):
    """Edits an existing task owned by the current user and resyncs reminders."""
    task = get_object_or_404(Task, pk=pk, user=request.user)
    if request.method == "POST":
        form = TaskForm(request.POST, instance=task, user=request.user)
        if form.is_valid():
            task = form.save(commit=False)
            if task.status == "DONE" and task.completed_at is None:
                task.completed_at = timezone.now()
            elif task.status != "DONE":
                task.completed_at = None
            task.save()
            sync_reminders_for_task(task)
            messages.success(request, f"Task '{task.title}' updated.")
            return redirect("tasks:task_detail", pk=task.pk)
    else:
        form = TaskForm(instance=task, user=request.user)

    return render(
        request,
        "tasks/task_form.html",
        {"form": form, "task": task, "is_edit": True},
    )


@login_required
@require_POST
def task_delete(request, pk: int):
    """Deletes a task owned by the logged-in user."""
    task = get_object_or_404(Task, pk=pk, user=request.user)
    title = task.title
    task.delete()
    messages.success(request, f"Task '{title}' deleted.")
    return redirect("tasks:task_list")


@login_required
@require_POST
def task_archive(request, pk: int):
    """Toggles ARCHIVED status on a task and updates pending reminders."""
    task = get_object_or_404(Task, pk=pk, user=request.user)
    if task.status == "ARCHIVED":
        task.status = "TODO"
        messages.info(request, f"Task '{task.title}' restored from archive.")
    else:
        task.status = "ARCHIVED"
        messages.info(request, f"Task '{task.title}' archived.")
    task.save(update_fields=["status", "updated_at"])
    sync_reminders_for_task(task)
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
def task_history(request):
    """Paginated completed tasks history comparing estimated vs actual minutes (F6.2)."""
    qs = (
        Task.objects.filter(user=request.user, status="DONE")
        .select_related("course")
        .prefetch_related("focus_sessions")
        .order_by("-completed_at", "-updated_at")
    )
    course_id = request.GET.get("course", "").strip()
    if course_id.isdigit():
        qs = qs.filter(course_id=int(course_id))

    paginator = Paginator(qs, 15)
    page_obj = paginator.get_page(request.GET.get("page"))
    courses = Course.objects.filter(user=request.user)
    return render(
        request,
        "tasks/history.html",
        {"page_obj": page_obj, "courses": courses},
    )


# =============================================================================
# F3.1, F3.2 & F3.4 — Real-Time Checkpoints, Auto-Generator & AI Suggestions
# =============================================================================


@login_required
@require_POST
def add_checkpoint(request, task_pk: int):
    """Adds a checkpoint inline via HTMX (or normal POST) and updates progress & reminders."""
    task = get_object_or_404(Task, pk=task_pk, user=request.user)
    title = request.POST.get("title", "").strip()
    phase = request.POST.get("phase", "CUSTOM").strip().upper()
    if phase not in VALID_PHASES:
        phase = "CUSTOM"

    raw_deadline = request.POST.get("target_deadline", "").strip()
    target_dt = parse_datetime(raw_deadline) if raw_deadline else None
    if target_dt is not None and timezone.is_naive(target_dt):
        target_dt = timezone.make_aware(target_dt)
    if target_dt is None:
        target_dt = max(timezone.now(), task.deadline - timedelta(hours=1))

    try:
        est_minutes = max(5, min(600, int(request.POST.get("estimated_minutes") or 30)))
    except ValueError:
        est_minutes = 30

    if title:
        next_order = (task.checkpoints.order_by("-order").values_list("order", flat=True).first() or 0) + 1
        Checkpoint.objects.create(
            task=task,
            title=title[:200],
            phase=phase,
            target_deadline=target_dt,
            estimated_minutes=est_minutes,
            order=next_order,
        )
        task.sync_status_from_checkpoints()
        sync_reminders_for_task(task)

    if _is_htmx(request):
        return render(
            request,
            "tasks/partials/checkpoint_list.html",
            {
                "task": task,
                "checkpoints": task.checkpoints.all(),
                "include_oob_progress": True,
            },
        )
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
@require_POST
def toggle_checkpoint(request, pk: int):
    """Toggles checkpoint completion via HTMX, recalculates parent status, and returns OOB progress bar."""
    checkpoint = get_object_or_404(
        Checkpoint.objects.select_related("task"),
        pk=pk,
        task__user=request.user,
    )
    checkpoint.is_done = not checkpoint.is_done
    checkpoint.completed_at = timezone.now() if checkpoint.is_done else None
    checkpoint.save(update_fields=["is_done", "completed_at"])

    task = checkpoint.task
    task.sync_status_from_checkpoints()
    sync_reminders_for_task(task)

    if _is_htmx(request):
        return render(
            request,
            "tasks/partials/checkpoint_row.html",
            {"cp": checkpoint, "include_oob_progress": True},
        )
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
@require_POST
def edit_checkpoint(request, pk: int):
    """Edits checkpoint title, phase, or target_deadline."""
    checkpoint = get_object_or_404(
        Checkpoint.objects.select_related("task"),
        pk=pk,
        task__user=request.user,
    )
    form = CheckpointForm(request.POST, instance=checkpoint)
    if form.is_valid():
        checkpoint = form.save()
        sync_reminders_for_task(checkpoint.task)

    if _is_htmx(request):
        return render(
            request,
            "tasks/partials/checkpoint_row.html",
            {"cp": checkpoint, "include_oob_progress": True},
        )
    return redirect("tasks:task_detail", pk=checkpoint.task_id)


@login_required
@require_POST
def delete_checkpoint(request, pk: int):
    """Deletes a checkpoint and resyncs task progress & reminders."""
    checkpoint = get_object_or_404(
        Checkpoint.objects.select_related("task"),
        pk=pk,
        task__user=request.user,
    )
    task = checkpoint.task
    checkpoint.delete()
    task.sync_status_from_checkpoints()
    sync_reminders_for_task(task)

    if _is_htmx(request):
        return render(
            request,
            "tasks/partials/progress_bar.html",
            {"task": task, "oob": True},
        )
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
@require_POST
def regenerate_checkpoints(request, pk: int):
    """Auto-generates checkpoints (preserving completed ones) and returns refreshed checklist partial (F3.2)."""
    task = get_object_or_404(Task, pk=pk, user=request.user)
    generate_checkpoints_for_task(task, overwrite=True)
    task.sync_status_from_checkpoints()
    sync_reminders_for_task(task)

    if _is_htmx(request):
        return render(
            request,
            "tasks/partials/checkpoint_list.html",
            {
                "task": task,
                "checkpoints": task.checkpoints.all(),
                "include_oob_progress": True,
            },
        )
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
@require_POST
def preview_ai_checkpoints(request, pk: int):
    """Returns editable AI/fallback sub-task suggestions partial (F3.4)."""
    task = get_object_or_404(Task, pk=pk, user=request.user)
    extra_context = request.POST.get("extra_context", "").strip()
    suggestions, source = suggest_checkpoints(task, extra_context=extra_context)
    return render(
        request,
        "tasks/partials/ai_checkpoint_preview.html",
        {"task": task, "suggestions": suggestions, "source": source},
    )


@login_required
@require_POST
def apply_ai_checkpoints(request, pk: int):
    """Saves selected AI/fallback suggested sub-tasks evenly spaced before task.deadline (F3.4)."""
    task = get_object_or_404(Task, pk=pk, user=request.user)
    selected_indices = request.POST.getlist("selected")
    source = request.POST.get("source", "fallback").strip().lower()
    is_ai = source == "ai"

    parsed_rows = []
    for raw_idx in selected_indices:
        title = request.POST.get(f"title_{raw_idx}", "").strip()
        if not title:
            continue
        phase = request.POST.get(f"phase_{raw_idx}", "CUSTOM").strip().upper()
        if phase not in VALID_PHASES:
            phase = "CUSTOM"
        try:
            est = max(5, min(600, int(request.POST.get(f"estimated_minutes_{raw_idx}") or 30)))
        except ValueError:
            est = 30
        parsed_rows.append((title[:200], phase, est))

    if parsed_rows:
        now = timezone.now()
        cap_end = task.deadline - timedelta(minutes=15)
        if cap_end <= now:
            base_start = task.deadline - timedelta(hours=2)
            total_span = timedelta(minutes=105)
        else:
            base_start = now
            total_span = cap_end - now

        max_order = task.checkpoints.order_by("-order").values_list("order", flat=True).first() or 0
        count = len(parsed_rows)
        new_objs = []
        for idx, (title, phase, est) in enumerate(parsed_rows, start=1):
            fraction = idx / count
            target_dt = min(base_start + total_span * fraction, task.deadline - timedelta(minutes=1))
            new_objs.append(
                Checkpoint(
                    task=task,
                    title=title,
                    phase=phase,
                    target_deadline=target_dt,
                    estimated_minutes=est,
                    order=max_order + idx,
                    is_ai_generated=is_ai,
                )
            )
        Checkpoint.objects.bulk_create(new_objs)
        task.sync_status_from_checkpoints()
        sync_reminders_for_task(task)

    if _is_htmx(request):
        return render(
            request,
            "tasks/partials/checkpoint_list.html",
            {
                "task": task,
                "checkpoints": task.checkpoints.all(),
                "include_oob_progress": True,
            },
        )
    return redirect("tasks:task_detail", pk=task.pk)


# =============================================================================
# F3.3 — Focus Mode & Pomodoro Timer
# =============================================================================


@login_required
def focus_mode_view(request, pk: int):
    """Renders distraction-free Pomodoro Focus Mode for a task (F3.3)."""
    task = get_object_or_404(Task, pk=pk, user=request.user)
    active_checkpoints = task.checkpoints.filter(is_done=False)
    return render(
        request,
        "tasks/focus_mode.html",
        {
            "task": task,
            "checkpoints": active_checkpoints,
            "today_focus_minutes": _today_focus_minutes(request.user),
        },
    )


@login_required
@require_POST
def start_focus_session(request):
    """Starts a RUNNING FocusSession and returns JSON session metadata (F3.3)."""
    data = _parse_json_or_post(request)
    task_id = data.get("task_id")
    task = get_object_or_404(Task, pk=task_id, user=request.user)

    checkpoint = None
    checkpoint_id = data.get("checkpoint_id")
    if checkpoint_id:
        checkpoint = get_object_or_404(Checkpoint, pk=checkpoint_id, task=task)

    mode = str(data.get("mode", "POMODORO_25_5")).upper()
    valid_modes = {choice[0] for choice in FocusSession.MODE_CHOICES}
    if mode not in valid_modes:
        mode = "POMODORO_25_5"

    try:
        planned_minutes = max(1, min(180, int(data.get("planned_minutes") or 25)))
    except (TypeError, ValueError):
        planned_minutes = 25

    session = FocusSession.objects.create(
        user=request.user,
        task=task,
        checkpoint=checkpoint,
        mode=mode,
        planned_minutes=planned_minutes,
        status="RUNNING",
    )
    return JsonResponse(
        {
            "session_id": session.pk,
            "planned_minutes": session.planned_minutes,
            "mode": session.mode,
            "status": session.status,
        }
    )


@login_required
@require_POST
def finish_focus_session(request, session_id: int):
    """Completes or abandons a FocusSession, logs actual minutes, and optionally marks checkpoint done."""
    session = get_object_or_404(
        FocusSession.objects.select_related("task", "checkpoint"),
        pk=session_id,
        user=request.user,
    )
    data = _parse_json_or_post(request)
    now = timezone.now()

    raw_status = str(data.get("status", "COMPLETED")).upper()
    new_status = "ABANDONED" if raw_status == "ABANDONED" else "COMPLETED"

    elapsed_seconds = data.get("elapsed_seconds")
    if elapsed_seconds is not None:
        try:
            sec_val = max(0, int(elapsed_seconds))
            computed_minutes = max(1, round(sec_val / 60)) if sec_val > 0 or new_status == "COMPLETED" else 0
            actual_minutes = min(computed_minutes, session.planned_minutes)
        except (TypeError, ValueError):
            actual_minutes = max(1, round((now - session.started_at).total_seconds() / 60))
    else:
        actual_minutes = max(1, round((now - session.started_at).total_seconds() / 60))
        actual_minutes = min(actual_minutes, session.planned_minutes)

    session.ended_at = now
    session.actual_minutes = actual_minutes
    session.status = new_status
    session.save(update_fields=["ended_at", "actual_minutes", "status"])

    task = session.task
    if new_status == "COMPLETED":
        task.actual_minutes = task.total_focus_minutes()
        task.save(update_fields=["actual_minutes", "updated_at"])

    mark_done_raw = data.get("mark_checkpoint_done", False)
    mark_done = str(mark_done_raw).lower() in ("1", "true", "yes", "on")
    checkpoint_marked = False
    if new_status == "COMPLETED" and mark_done and session.checkpoint_id:
        cp = session.checkpoint
        if not cp.is_done:
            cp.is_done = True
            cp.completed_at = now
            cp.save(update_fields=["is_done", "completed_at"])
            task.sync_status_from_checkpoints()
            sync_reminders_for_task(task)
            checkpoint_marked = True

    return JsonResponse(
        {
            "session_id": session.pk,
            "status": session.status,
            "actual_minutes": session.actual_minutes,
            "checkpoint_marked": checkpoint_marked,
            "today_focus_minutes": _today_focus_minutes(request.user),
            "task_total_focus_minutes": task.total_focus_minutes(),
        }
    )


# =============================================================================
# F4.1 – F4.5 — Alerts Inbox, Polling, Snooze, Digest, Telegram & Coaching
# =============================================================================


@login_required
def alert_inbox_view(request):
    """Alert inbox displaying active/due, snoozed, and resolved reminders (F4.2)."""
    now = timezone.now()
    base_qs = Reminder.objects.filter(user=request.user).select_related("task", "checkpoint")

    active_reminders = base_qs.filter(
        Q(status="PENDING") | Q(status="SENT") | Q(status="SNOOZED", snoozed_until__lte=now)
    ).order_by("-escalation_level", "scheduled_for")
    snoozed_reminders = base_qs.filter(
        status="SNOOZED",
        snoozed_until__gt=now,
    ).order_by("snoozed_until")
    closed_reminders = base_qs.filter(
        status__in=["DONE", "DISMISSED"],
    ).order_by("-scheduled_for")[:20]

    return render(
        request,
        "tasks/alerts/inbox.html",
        {
            "active_reminders": active_reminders,
            "snoozed_reminders": snoozed_reminders,
            "closed_reminders": closed_reminders,
        },
    )


@login_required
@require_GET
def poll_due_alerts(request):
    """Returns JSON array of due WEB reminders for browser Notification API & toast container (F4.2)."""
    now = timezone.now()
    due_qs = (
        Reminder.objects.filter(user=request.user, channel="WEB")
        .select_related("task", "checkpoint")
        .filter(
            Q(status="PENDING", scheduled_for__lte=now)
            | Q(status="SNOOZED", snoozed_until__lte=now)
        )
        .order_by("-escalation_level", "scheduled_for")
    )

    alerts_payload = []
    for r in due_qs:
        if r.task.status in ("DONE", "ARCHIVED") or (r.checkpoint_id and r.checkpoint.is_done):
            r.status = "DONE"
            r.save(update_fields=["status"])
            continue
        if not r.message:
            msg, _ = generate_coaching_message(r, now=now)
            r.message = msg
            r.save(update_fields=["message"])
        alerts_payload.append(
            {
                "id": r.pk,
                "task_id": r.task_id,
                "task_title": r.task.title,
                "title": r.checkpoint.title if r.checkpoint_id else r.task.title,
                "message": r.message,
                "escalation_level": r.escalation_level,
                "snooze_count": r.snooze_count,
                "snooze_url": reverse("alerts:snooze", kwargs={"pk": r.pk}),
                "dismiss_url": reverse("alerts:dismiss", kwargs={"pk": r.pk}),
                "done_url": reverse("alerts:done", kwargs={"pk": r.pk}),
                "task_url": reverse("tasks:task_detail", kwargs={"pk": r.task_id}),
            }
        )

    return JsonResponse({"alerts": alerts_payload})


@login_required
@require_POST
def trigger_process_reminders(request):
    """Manual UI trigger for local development (runs evaluate_due_reminders without OS cron)."""
    due = evaluate_due_reminders()
    user_due = [r for r in due if r.user_id == request.user.pk]
    tg_sent = 0
    for r in user_due:
        if r.channel != "DIGEST" and r.sent_at is None:
            if send_telegram_alert(r):
                r.sent_at = timezone.now()
                r.save(update_fields=["sent_at"])
                tg_sent += 1
    messages.info(
        request,
        f"Checked due reminders: {len(user_due)} active for you ({tg_sent} sent via Telegram).",
    )
    return redirect("alerts:inbox")


@login_required
@require_POST
def snooze_reminder(request, pk: int):
    """Snoozes a reminder by 15, 60, or 180 minutes with snooze_count >= 3 and deadline anti-procrastination guards."""
    reminder = get_object_or_404(
        Reminder.objects.select_related("task", "checkpoint"),
        pk=pk,
        user=request.user,
    )
    data = _parse_json_or_post(request)
    try:
        minutes = int(data.get("minutes", 15))
    except (TypeError, ValueError):
        minutes = 15

    if minutes not in VALID_SNOOZE_MINUTES:
        return JsonResponse({"ok": False, "error": "Invalid snooze duration. Use 15, 60, or 180."}, status=400)

    if reminder.snooze_count >= 3:
        msg = "Maximum snooze limit (3x) reached! Please start this checkpoint now."
        if _is_htmx(request):
            return HttpResponse(
                f'<div id="reminder-{reminder.pk}" class="rounded-xl border border-red-300 bg-red-50 p-3 text-xs text-red-800 font-semibold">{msg}</div>',
                status=400,
            )
        return JsonResponse({"ok": False, "error": msg}, status=400)

    now = timezone.now()
    proposed = now + timedelta(minutes=minutes)
    if proposed >= reminder.task.deadline:
        msg = "Cannot snooze past the task deadline."
        if _is_htmx(request):
            return HttpResponse(
                f'<div id="reminder-{reminder.pk}" class="rounded-xl border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900 font-semibold">{msg}</div>',
                status=400,
            )
        return JsonResponse({"ok": False, "error": msg}, status=400)

    reminder.status = "SNOOZED"
    reminder.snoozed_until = proposed
    reminder.snooze_count += 1
    if reminder.snooze_count >= 2:
        reminder.escalation_level = 3
    reminder.save(update_fields=["status", "snoozed_until", "snooze_count", "escalation_level"])

    if _is_htmx(request):
        return HttpResponse("")
    return JsonResponse(
        {
            "ok": True,
            "status": reminder.status,
            "snoozed_until": reminder.snoozed_until.isoformat(),
            "snooze_count": reminder.snooze_count,
        }
    )


@login_required
@require_POST
def dismiss_reminder(request, pk: int):
    """Dismisses a reminder owned by the current user."""
    reminder = get_object_or_404(Reminder, pk=pk, user=request.user)
    reminder.status = "DISMISSED"
    reminder.save(update_fields=["status"])
    if _is_htmx(request):
        return HttpResponse("")
    return JsonResponse({"ok": True, "status": reminder.status})


@login_required
@require_POST
def done_reminder(request, pk: int):
    """Marks both the reminder and its linked checkpoint as DONE."""
    reminder = get_object_or_404(
        Reminder.objects.select_related("task", "checkpoint"),
        pk=pk,
        user=request.user,
    )
    now = timezone.now()
    reminder.status = "DONE"
    reminder.save(update_fields=["status"])

    if reminder.checkpoint_id and not reminder.checkpoint.is_done:
        cp = reminder.checkpoint
        cp.is_done = True
        cp.completed_at = now
        cp.save(update_fields=["is_done", "completed_at"])
        reminder.task.sync_status_from_checkpoints()
        sync_reminders_for_task(reminder.task)

    if _is_htmx(request):
        return HttpResponse("")
    return JsonResponse({"ok": True, "status": "DONE"})


@login_required
def daily_digest_view(request):
    """Web preview of today's anti-spam daily briefing and recent digest logs (F4.3)."""
    slot = request.GET.get("slot", "MORNING").upper()
    if slot not in ("MORNING", "EVENING"):
        slot = "MORNING"
    digest = build_daily_digest(request.user, slot=slot)
    logs = DailyDigestLog.objects.filter(user=request.user)[:10]
    return render(
        request,
        "tasks/alerts/daily_digest.html",
        {
            "digest": digest,
            "logs": logs,
            "slot": slot,
        },
    )


@login_required
def telegram_connect_view(request):
    """Generates/regenerates one-time Telegram link token and shows connection status (F4.4)."""
    link, created = TelegramLink.objects.get_or_create(
        user=request.user,
        defaults={"link_token": secrets.token_urlsafe(12)},
    )
    if request.method == "POST":
        link.link_token = secrets.token_urlsafe(12)
        link.chat_id = None
        link.is_verified = False
        link.linked_at = None
        link.save(update_fields=["link_token", "chat_id", "is_verified", "linked_at"])
        messages.info(request, "Generated a new Telegram link token.")
        return redirect("alerts:telegram_connect")

    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip() or "YourATRBot"
    return render(
        request,
        "tasks/alerts/telegram_connect.html",
        {"link": link, "bot_username": bot_username},
    )


@csrf_exempt
@require_POST
def telegram_webhook_view(request, secret: str):
    """Production Telegram webhook endpoint protected by TELEGRAM_WEBHOOK_SECRET (F4.4)."""
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not expected_secret or not secrets.compare_digest(secret, expected_secret):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    result = handle_telegram_update(payload)
    return JsonResponse(result)


@login_required
@require_http_methods(["GET", "POST"])
def preview_coaching_tone(request):
    """HTMX preview of Friendly, Strict, or Minimal adaptive coaching copy (F4.5)."""
    tone = (request.POST.get("tone") or request.GET.get("tone") or "friendly").strip().lower()
    if tone not in ("friendly", "strict", "minimal"):
        tone = "friendly"

    now = timezone.now()
    sample_reminder = (
        Reminder.objects.filter(user=request.user)
        .select_related("task", "checkpoint")
        .first()
    )
    if sample_reminder is None:
        sample_task = Task(
            user=request.user,
            title="TP 4 — Pemrograman Berorientasi Objek",
            deadline=now + timedelta(hours=18),
        )
        sample_cp = Checkpoint(
            task=sample_task,
            title="Test edge cases & revise",
            target_deadline=now + timedelta(hours=14),
        )
        sample_reminder = Reminder(
            user=request.user,
            task=sample_task,
            checkpoint=sample_cp,
            scheduled_for=now,
            escalation_level=2,
            snooze_count=0,
        )

    message_text, source = generate_coaching_message(sample_reminder, user_tone=tone, now=now)
    return render(
        request,
        "tasks/alerts/partials/coaching_preview.html",
        {
            "tone": tone,
            "source": source,
            "message": message_text,
            "preview_message": message_text,
        },
    )
