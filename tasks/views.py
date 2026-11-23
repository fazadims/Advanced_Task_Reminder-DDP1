"""Person B views: Task/Checkpoint CRUD, Auto-Checkpoints, Focus Pomodoro, AI Suggestions, Alerts/Snooze, Digest, Telegram, and Coaching."""

import json
import os
import secrets
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import format_html
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
from tasks.services import ai_coaching
from tasks.services.ai_checkpoints import suggest_checkpoints
from tasks.services.ai_coaching import generate_coaching_message, get_user_coaching_tone
from tasks.services.checkpoint_gen import generate_checkpoints_for_task
from tasks.services.digest import build_daily_digest
from tasks.services.notifier import (
    complete_reminder_action,
    evaluate_due_reminders,
    snooze_reminder_action,
    sync_reminders_for_task,
)
from tasks.services.telegram_bot import handle_telegram_update, send_telegram_alert

VALID_SNOOZE_MINUTES = {15, 60, 180}
VALID_PHASES = {choice[0] for choice in Checkpoint.PHASE_CHOICES}
VALID_TASK_STATUSES = {choice[0] for choice in Task.STATUS_CHOICES}
VALID_TASK_CATEGORIES = {choice[0] for choice in Task.CATEGORY_CHOICES}
VALID_FOCUS_MODES = {choice[0] for choice in FocusSession.MODE_CHOICES}
MAX_SNOOZE_COUNT = 3
MAX_POLL_LIVE_AI_CALLS = 2
MAX_AI_SELECTED_CHECKPOINTS = 15
MAX_EXTRA_CONTEXT_LENGTH = 500


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _parse_positive_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_json_or_post(request) -> dict:
    """Supports both JSON fetch() bodies and standard form-encoded POST payloads."""
    if request.content_type and "application/json" in request.content_type:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}
    return request.POST.dict()


def _active_reminders_for_task(task: Task):
    """Returns up to 10 active/pending reminders for a task for detail & OOB updates."""
    return task.reminders.select_related("checkpoint").exclude(
        status__in=["DONE", "DISMISSED"]
    )[:10]


def _today_focus_minutes(user) -> int:
    local_now = timezone.localtime(timezone.now())
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    agg = FocusSession.objects.filter(
        user=user,
        status="COMPLETED",
        started_at__gte=day_start,
        started_at__lt=day_end,
    ).aggregate(total=Sum("actual_minutes"))
    return int(agg["total"] or 0)


def _fallback_poll_message(reminder: Reminder, *, user_tone: str, now) -> str:
    """Builds a deterministic offline coaching message when per-poll live AI cap is reached."""
    has_checkpoint = bool(reminder.checkpoint_id and reminder.checkpoint)
    target_deadline = (
        reminder.checkpoint.target_deadline if has_checkpoint else reminder.task.deadline
    )
    checkpoint_title = reminder.checkpoint.title if has_checkpoint else "Target utama"
    hours_left = (target_deadline - now).total_seconds() / 3600.0
    formatter = getattr(ai_coaching, "_format_fallback_message", None)
    if callable(formatter):
        return formatter(
            tone=user_tone,
            escalation_level=min(3, max(1, int(reminder.escalation_level or 1))),
            snooze_count=int(reminder.snooze_count or 0),
            hours_left=hours_left,
            checkpoint_title=checkpoint_title,
            task_title=reminder.task.title,
        )
    hours_str = str(max(0, int(round(hours_left))))
    return f"[{reminder.task.title}] {checkpoint_title} — sisa {hours_str} jam."


# =============================================================================
# F3.1 & F6.2 — Task CRUD, Detail, Archive & Paginated History
# =============================================================================


@login_required
@require_GET
def task_list(request):
    """Filterable task workspace scoped strictly to the logged-in user."""
    qs = (
        Task.objects.filter(user=request.user)
        .select_related("course", "component")
        .prefetch_related("checkpoints")
    )

    course_id = _parse_positive_int(request.GET.get("course"))
    status_filter = request.GET.get("status", "").strip()
    category_filter = request.GET.get("category", "").strip()

    if course_id is not None:
        qs = qs.filter(course_id=course_id)
    if status_filter in VALID_TASK_STATUSES:
        qs = qs.filter(status=status_filter)
    else:
        qs = qs.exclude(status="ARCHIVED")
    if category_filter in VALID_TASK_CATEGORIES:
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
            with transaction.atomic():
                task = form.save(commit=False)
                task.user = request.user
                if task.status == "DONE" and task.completed_at is None:
                    task.completed_at = timezone.now()
                elif task.status != "DONE":
                    task.completed_at = None
                task.save()
                if (
                    form.cleaned_data.get("auto_generate_checkpoints", True)
                    and task.status not in ("DONE", "ARCHIVED")
                ):
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
@require_GET
def task_detail(request, pk: int):
    """Task detail workspace with HTMX checkpoints, focus logs, and upcoming reminders (F3.1 & F6.2)."""
    task = get_object_or_404(
        Task.objects.select_related("course", "component").prefetch_related("checkpoints"),
        pk=pk,
        user=request.user,
    )
    checkpoints = task.checkpoints.all()
    focus_sessions = task.focus_sessions.select_related("checkpoint")[:10]
    reminders = _active_reminders_for_task(task)

    return render(
        request,
        "tasks/task_detail.html",
        {
            "task": task,
            "checkpoints": checkpoints,
            "focus_sessions": focus_sessions,
            "total_focus_minutes": task.total_focus_minutes(),
            "reminders": reminders,
        },
    )


@login_required
def task_update(request, pk: int):
    """Edits an existing task owned by the current user and resyncs reminders."""
    task = get_object_or_404(
        Task.objects.select_related("component"),
        pk=pk,
        user=request.user,
    )
    if request.method == "POST":
        if task.component_id:
            submitted_course_id = _parse_positive_int(request.POST.get("course"))
            if submitted_course_id != task.component.course_id:
                task.component = None
        form = TaskForm(request.POST, instance=task, user=request.user)
        if form.is_valid():
            with transaction.atomic():
                task = form.save(commit=False)
                if task.component_id and (
                    not task.course_id or task.component.course_id != task.course_id
                ):
                    task.component = None
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
    with transaction.atomic():
        if task.status == "ARCHIVED":
            task.status = "TODO"
            task.save(update_fields=["status", "updated_at"])
            if task.checkpoints.exists():
                task.sync_status_from_checkpoints()
            else:
                task.completed_at = None
                task.save(update_fields=["completed_at", "updated_at"])
            info_msg = f"Task '{task.title}' restored from archive."
        else:
            task.status = "ARCHIVED"
            task.save(update_fields=["status", "updated_at"])
            info_msg = f"Task '{task.title}' archived."
        sync_reminders_for_task(task)
    messages.info(request, info_msg)
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
@require_GET
def task_history(request):
    """Paginated completed tasks history comparing estimated vs actual minutes (F6.2)."""
    qs = (
        Task.objects.filter(user=request.user, status="DONE")
        .select_related("course")
        .annotate(
            logged_focus_minutes=Coalesce(
                Sum(
                    "focus_sessions__actual_minutes",
                    filter=Q(focus_sessions__status="COMPLETED"),
                ),
                0,
            )
        )
        .order_by("-completed_at", "-updated_at")
    )
    course_id = _parse_positive_int(request.GET.get("course"))
    if course_id is not None:
        qs = qs.filter(course_id=course_id)

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


def _checkpoint_form_error_response(request, task: Task, error_msg: str):
    """Returns inline HTMX error targeting #checkpoint-form-error or flashes message on non-HTMX."""
    if _is_htmx(request):
        resp = HttpResponse(
            format_html('<p class="text-xs text-red-600 font-medium">{}</p>', error_msg),
            status=400,
        )
        resp["HX-Retarget"] = "#checkpoint-form-error"
        resp["HX-Reswap"] = "innerHTML"
        return resp
    messages.error(request, error_msg)
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
@require_POST
def add_checkpoint(request, task_pk: int):
    """Adds a checkpoint inline via HTMX (or normal POST) and updates progress & reminders."""
    task = get_object_or_404(Task, pk=task_pk, user=request.user)
    title = request.POST.get("title", "").strip()
    if not title:
        return _checkpoint_form_error_response(request, task, "Judul checkpoint wajib diisi.")

    phase = request.POST.get("phase", "CUSTOM").strip().upper()
    if phase not in VALID_PHASES:
        phase = "CUSTOM"

    raw_deadline = request.POST.get("target_deadline", "").strip()
    if raw_deadline:
        target_dt = parse_datetime(raw_deadline)
        if target_dt is None:
            return _checkpoint_form_error_response(
                request,
                task,
                "Format target deadline checkpoint tidak valid.",
            )
        if timezone.is_naive(target_dt):
            target_dt = timezone.make_aware(target_dt)
        if target_dt > task.deadline:
            return _checkpoint_form_error_response(
                request,
                task,
                "Target deadline checkpoint tidak boleh melewati deadline tugas.",
            )
    else:
        target_dt = min(task.deadline, max(timezone.now(), task.deadline - timedelta(hours=1)))

    try:
        est_minutes = max(1, min(600, int(request.POST.get("estimated_minutes") or 30)))
    except (TypeError, ValueError):
        est_minutes = 30

    with transaction.atomic():
        next_order = (
            task.checkpoints.order_by("-order").values_list("order", flat=True).first() or 0
        ) + 1
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
        html = render_to_string(
            "tasks/partials/checkpoint_list.html",
            {
                "task": task,
                "checkpoints": task.checkpoints.all(),
                "include_oob_progress": True,
                "include_oob_reminders": True,
                "reminders": _active_reminders_for_task(task),
            },
            request=request,
        )
        clear_error_oob = '<div id="checkpoint-form-error" hx-swap-oob="innerHTML"></div>'
        return HttpResponse(html + clear_error_oob)
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
@require_POST
def toggle_checkpoint(request, pk: int):
    """Toggles checkpoint completion via HTMX, recalculates parent status, and returns OOB progress & reminders."""
    checkpoint = get_object_or_404(
        Checkpoint.objects.select_related("task"),
        pk=pk,
        task__user=request.user,
    )
    task = checkpoint.task
    with transaction.atomic():
        checkpoint.is_done = not checkpoint.is_done
        checkpoint.completed_at = timezone.now() if checkpoint.is_done else None
        checkpoint.save(update_fields=["is_done", "completed_at"])
        task.sync_status_from_checkpoints()
        sync_reminders_for_task(task)

    if _is_htmx(request):
        return render(
            request,
            "tasks/partials/checkpoint_row.html",
            {
                "cp": checkpoint,
                "task": task,
                "include_oob_progress": True,
                "include_oob_reminders": True,
                "reminders": _active_reminders_for_task(task),
            },
        )
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
@require_POST
def edit_checkpoint(request, pk: int):
    """Edits checkpoint title, phase, target_deadline, or estimated_minutes."""
    checkpoint = get_object_or_404(
        Checkpoint.objects.select_related("task"),
        pk=pk,
        task__user=request.user,
    )
    task = checkpoint.task
    form = CheckpointForm(request.POST, instance=checkpoint, task=task)
    if form.is_valid():
        with transaction.atomic():
            checkpoint = form.save()
            sync_reminders_for_task(task)
        if _is_htmx(request):
            return render(
                request,
                "tasks/partials/checkpoint_row.html",
                {
                    "cp": checkpoint,
                    "task": task,
                    "include_oob_progress": True,
                    "include_oob_reminders": True,
                    "reminders": _active_reminders_for_task(task),
                },
            )
        return redirect("tasks:task_detail", pk=task.pk)

    error_msg = next(
        (msg for err_list in form.errors.values() for msg in err_list),
        "Data checkpoint tidak valid.",
    )
    if _is_htmx(request):
        checkpoint.refresh_from_db()
        return render(
            request,
            "tasks/partials/checkpoint_row.html",
            {
                "cp": checkpoint,
                "task": task,
                "include_oob_progress": False,
                "edit_error": error_msg,
            },
            status=400,
        )
    messages.error(request, error_msg)
    return redirect("tasks:task_detail", pk=task.pk)


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
    with transaction.atomic():
        checkpoint.delete()
        task.sync_status_from_checkpoints()
        sync_reminders_for_task(task)

    if _is_htmx(request):
        reminders = _active_reminders_for_task(task)
        if not task.checkpoints.exists():
            resp = render(
                request,
                "tasks/partials/checkpoint_list.html",
                {
                    "task": task,
                    "checkpoints": [],
                    "include_oob_progress": True,
                    "include_oob_reminders": True,
                    "reminders": reminders,
                },
            )
            resp["HX-Retarget"] = "#checkpoint-list"
            resp["HX-Reswap"] = "outerHTML"
            return resp

        progress_html = render_to_string(
            "tasks/partials/progress_bar.html",
            {"task": task, "oob": True},
            request=request,
        )
        reminders_html = render_to_string(
            "tasks/partials/upcoming_reminders.html",
            {"task": task, "reminders": reminders, "oob": True},
            request=request,
        )
        return HttpResponse(progress_html + reminders_html)
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
@require_POST
def regenerate_checkpoints(request, pk: int):
    """Auto-generates checkpoints (preserving completed ones) and returns refreshed checklist partial (F3.2)."""
    task = get_object_or_404(Task, pk=pk, user=request.user)
    with transaction.atomic():
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
                "include_oob_reminders": True,
                "reminders": _active_reminders_for_task(task),
            },
        )
    return redirect("tasks:task_detail", pk=task.pk)


@login_required
@require_POST
def preview_ai_checkpoints(request, pk: int):
    """Returns editable AI/fallback sub-task suggestions partial (F3.4)."""
    task = get_object_or_404(Task, pk=pk, user=request.user)
    extra_context = request.POST.get("extra_context", "").strip()[:MAX_EXTRA_CONTEXT_LENGTH]
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
    selected_indices = request.POST.getlist("selected")[:MAX_AI_SELECTED_CHECKPOINTS]
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
            est = max(1, min(600, int(request.POST.get(f"estimated_minutes_{raw_idx}") or 30)))
        except (TypeError, ValueError):
            est = 30
        parsed_rows.append((title[:200], phase, est))

    if parsed_rows:
        with transaction.atomic():
            now = timezone.now()
            remaining = task.deadline - now
            max_order = (
                task.checkpoints.order_by("-order").values_list("order", flat=True).first() or 0
            )
            count = len(parsed_rows)
            new_objs = []
            for idx, (title, phase, est) in enumerate(parsed_rows, start=1):
                if remaining > timedelta(minutes=15):
                    cap_end = task.deadline - timedelta(minutes=15)
                    target_dt = min(
                        now + (cap_end - now) * (idx / count),
                        task.deadline - timedelta(minutes=1),
                    )
                elif remaining > timedelta(0):
                    target_dt = now + remaining * (idx / (count + 1))
                else:
                    base_start = task.deadline - timedelta(hours=2)
                    total_span = timedelta(minutes=105)
                    target_dt = min(
                        base_start + total_span * (idx / count),
                        task.deadline - timedelta(minutes=1),
                    )
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
                "include_oob_reminders": True,
                "reminders": _active_reminders_for_task(task),
            },
        )
    return redirect("tasks:task_detail", pk=task.pk)


# =============================================================================
# F3.3 — Focus Mode & Pomodoro Timer
# =============================================================================


@login_required
@require_GET
def focus_mode_view(request, pk: int):
    """Renders distraction-free Pomodoro Focus Mode for a task (F3.3)."""
    task = get_object_or_404(Task.objects.select_related("course"), pk=pk, user=request.user)
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
    task_id = _parse_positive_int(data.get("task_id"))
    if task_id is None:
        return JsonResponse({"ok": False, "error": "Invalid or missing task_id."}, status=400)
    task = get_object_or_404(Task, pk=task_id, user=request.user)

    checkpoint = None
    raw_cp_id = data.get("checkpoint_id")
    if raw_cp_id not in (None, ""):
        cp_id = _parse_positive_int(raw_cp_id)
        if cp_id is None:
            return JsonResponse({"ok": False, "error": "Invalid checkpoint_id."}, status=400)
        checkpoint = get_object_or_404(Checkpoint, pk=cp_id, task=task)

    mode = str(data.get("mode", "POMODORO_25_5")).upper()
    if mode not in VALID_FOCUS_MODES:
        mode = "POMODORO_25_5"

    raw_planned = data.get("planned_minutes")
    if raw_planned in (None, "") or isinstance(raw_planned, bool):
        planned_minutes = 25
    else:
        try:
            planned_minutes = max(1, min(180, int(raw_planned)))
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
    data = _parse_json_or_post(request)
    now = timezone.now()

    raw_status = str(data.get("status", "COMPLETED")).upper()
    new_status = "ABANDONED" if raw_status == "ABANDONED" else "COMPLETED"

    mark_done_raw = data.get("mark_checkpoint_done", False)
    mark_done = str(mark_done_raw).lower() in ("1", "true", "yes", "on")

    with transaction.atomic():
        session = get_object_or_404(
            FocusSession.objects.select_for_update().select_related("task", "checkpoint"),
            pk=session_id,
            user=request.user,
        )
        if session.status in ("COMPLETED", "ABANDONED"):
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Focus session is already finished.",
                    "status": session.status,
                },
                status=409,
            )

        elapsed_seconds = data.get("elapsed_seconds")
        if elapsed_seconds is not None and not isinstance(elapsed_seconds, bool):
            try:
                sec_val = max(0, int(elapsed_seconds))
                computed_minutes = (
                    max(1, round(sec_val / 60))
                    if sec_val > 0 or new_status == "COMPLETED"
                    else 0
                )
            except (TypeError, ValueError):
                computed_minutes = max(
                    1, round((now - session.started_at).total_seconds() / 60)
                )
        else:
            computed_minutes = max(1, round((now - session.started_at).total_seconds() / 60))
        actual_minutes = min(computed_minutes, session.planned_minutes)

        session.ended_at = now
        session.actual_minutes = actual_minutes
        session.status = new_status
        session.save(update_fields=["ended_at", "actual_minutes", "status"])

        task = session.task
        task_total = task.total_focus_minutes()
        if new_status == "COMPLETED":
            task.actual_minutes = task_total
            task.save(update_fields=["actual_minutes", "updated_at"])

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
            "task_total_focus_minutes": task_total,
        }
    )


# =============================================================================
# F4.1 – F4.5 — Alerts Inbox, Polling, Snooze, Digest, Telegram & Coaching
# =============================================================================


def _reminder_htmx_error_response(pk: int, error_msg: str) -> HttpResponse:
    """Returns inline HTMX error targeting #reminder-error-<pk> while keeping the card intact."""
    resp = HttpResponse(
        format_html(
            '<div class="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-800 font-semibold">{}</div>',
            error_msg,
        ),
        status=400,
    )
    resp["HX-Retarget"] = f"#reminder-error-{pk}"
    resp["HX-Reswap"] = "innerHTML"
    return resp


@login_required
@require_GET
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
            "max_snooze_count": MAX_SNOOZE_COUNT,
        },
    )


@login_required
@require_GET
def poll_due_alerts(request):
    """Returns JSON array of due WEB reminders for browser Notification API & toast container (F4.2)."""
    now = timezone.now()
    due_qs = (
        Reminder.objects.filter(user=request.user, channel="WEB")
        .select_related("task", "checkpoint", "user")
        .filter(
            Q(status="PENDING", scheduled_for__lte=now)
            | Q(status="SNOOZED", snoozed_until__lte=now)
        )
        .order_by("-escalation_level", "scheduled_for")
    )

    user_tone = get_user_coaching_tone(request.user)
    live_ai_enabled = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    live_ai_calls = 0

    finished_ids: list[int] = []
    to_update_msg: list[Reminder] = []
    alerts_payload = []

    for r in due_qs:
        if r.task.status in ("DONE", "ARCHIVED") or (r.checkpoint_id and r.checkpoint.is_done):
            finished_ids.append(r.pk)
            continue
        if not r.message:
            if not live_ai_enabled or live_ai_calls < MAX_POLL_LIVE_AI_CALLS:
                msg, _ = generate_coaching_message(r, user_tone=user_tone, now=now)
                if live_ai_enabled:
                    live_ai_calls += 1
            else:
                msg = _fallback_poll_message(r, user_tone=user_tone, now=now)
            r.message = msg
            to_update_msg.append(r)
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

    if finished_ids:
        Reminder.objects.filter(pk__in=finished_ids).update(status="DONE")
    if to_update_msg:
        Reminder.objects.bulk_update(to_update_msg, ["message"])

    return JsonResponse({"alerts": alerts_payload})


@login_required
@require_POST
def trigger_process_reminders(request):
    """Manual UI trigger for local development (runs evaluate_due_reminders without OS cron)."""
    user_due = evaluate_due_reminders(user=request.user)
    chat_id = (
        TelegramLink.objects.filter(
            user=request.user,
            is_verified=True,
            chat_id__isnull=False,
        )
        .values_list("chat_id", flat=True)
        .first()
    )
    tg_sent = 0
    if chat_id is not None:
        sent_targets: set[tuple[int, int | None]] = set()
        sent_ids: list[int] = []
        sent_at = timezone.now()
        for r in user_due:
            target_key = (r.task_id, r.checkpoint_id)
            if r.channel == "DIGEST" or r.sent_at is not None or target_key in sent_targets:
                continue
            if send_telegram_alert(r, chat_id=chat_id):
                sent_targets.add(target_key)
                sent_ids.append(r.pk)
                tg_sent += 1
        if sent_ids:
            Reminder.objects.filter(pk__in=sent_ids).update(sent_at=sent_at)
    messages.info(
        request,
        f"Checked due reminders: {len(user_due)} active for you ({tg_sent} sent via Telegram).",
    )
    return redirect("alerts:inbox")


@login_required
@require_POST
def snooze_reminder(request, pk: int):
    """Snoozes a reminder by 15, 60, or 180 minutes with snooze_count >= 3 and deadline anti-procrastination guards."""
    data = _parse_json_or_post(request)
    raw_minutes = data.get("minutes")
    if raw_minutes in (None, ""):
        minutes = 15
    elif isinstance(raw_minutes, bool):
        minutes = -1
    else:
        try:
            minutes = int(raw_minutes)
        except (TypeError, ValueError):
            minutes = -1

    if minutes not in VALID_SNOOZE_MINUTES:
        err_msg = "Invalid snooze duration. Use 15, 60, or 180."
        if _is_htmx(request):
            return _reminder_htmx_error_response(pk, err_msg)
        return JsonResponse({"ok": False, "error": err_msg}, status=400)

    ok, result = snooze_reminder_action(pk, minutes=minutes, user=request.user)
    if not ok:
        if result.get("reason") == "not_found":
            raise Http404("Reminder not found.")
        err_msg = result.get("error") or "Unable to snooze reminder."
        if _is_htmx(request):
            return _reminder_htmx_error_response(pk, err_msg)
        return JsonResponse({"ok": False, "error": err_msg}, status=400)

    if _is_htmx(request):
        return HttpResponse("")

    reminder_obj = result.get("reminder")
    snoozed_until = result.get("snoozed_until")
    if snoozed_until is None and reminder_obj is not None:
        snoozed_until = reminder_obj.snoozed_until
    if hasattr(snoozed_until, "isoformat"):
        snoozed_until = snoozed_until.isoformat()
    status_val = result.get("status") or (
        reminder_obj.status if reminder_obj is not None else "SNOOZED"
    )
    snooze_count = result.get("snooze_count")
    if snooze_count is None and reminder_obj is not None:
        snooze_count = reminder_obj.snooze_count

    return JsonResponse(
        {
            "ok": True,
            "status": status_val,
            "snoozed_until": snoozed_until,
            "snooze_count": snooze_count,
        }
    )


@login_required
@require_POST
def dismiss_reminder(request, pk: int):
    """Dismisses a reminder owned by the current user."""
    with transaction.atomic():
        reminder = get_object_or_404(
            Reminder.objects.select_for_update(),
            pk=pk,
            user=request.user,
        )
        if reminder.status not in ("DONE", "DISMISSED"):
            reminder.status = "DISMISSED"
            reminder.save(update_fields=["status"])
    if _is_htmx(request):
        return HttpResponse("")
    return JsonResponse({"ok": True, "status": reminder.status})


@login_required
@require_POST
def done_reminder(request, pk: int):
    """Marks both the reminder and its linked checkpoint as DONE."""
    ok, result = complete_reminder_action(pk, user=request.user)
    if not ok:
        if result.get("reason") == "not_found":
            raise Http404("Reminder not found.")
        err_msg = result.get("error") or "Unable to complete reminder."
        if _is_htmx(request):
            return _reminder_htmx_error_response(pk, err_msg)
        return JsonResponse({"ok": False, "error": err_msg}, status=400)

    if _is_htmx(request):
        return HttpResponse("")
    return JsonResponse({"ok": True, "status": result.get("status", "DONE")})


@login_required
@require_GET
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
    now = timezone.now()
    link, _ = TelegramLink.objects.get_or_create(
        user=request.user,
        defaults={
            "link_token": secrets.token_urlsafe(24),
            "token_created_at": now,
        },
    )
    if request.method == "POST":
        link.link_token = secrets.token_urlsafe(24)
        link.token_created_at = timezone.now()
        link.chat_id = None
        link.is_verified = False
        link.linked_at = None
        link.save(
            update_fields=[
                "link_token",
                "token_created_at",
                "chat_id",
                "is_verified",
                "linked_at",
            ]
        )
        messages.info(request, "Generated a new Telegram link token.")
        return redirect("alerts:telegram_connect")

    raw_bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    bot_username = raw_bot_username or "YourATRBot"
    return render(
        request,
        "tasks/alerts/telegram_connect.html",
        {
            "link": link,
            "bot_username": bot_username,
            "bot_username_configured": bool(raw_bot_username),
        },
    )


@csrf_exempt
@require_POST
def telegram_webhook_view(request, secret: str):
    """Production Telegram webhook endpoint protected by TELEGRAM_WEBHOOK_SECRET (F4.4)."""
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not expected_secret:
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    if "X-Telegram-Bot-Api-Secret-Token" in request.headers:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "").strip()
        if not secrets.compare_digest(header_secret, expected_secret):
            return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)
    elif not secrets.compare_digest(secret, expected_secret):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "Invalid JSON payload"}, status=400)

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
        },
    )
