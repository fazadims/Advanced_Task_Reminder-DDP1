"""Auto-checkpoint milestone generator for tasks (F3.2)."""

import math
from datetime import timedelta

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from tasks.models import Checkpoint, FocusSession, Task

PHASE_TEMPLATES: dict[str, list[tuple[str, str, float]]] = {
    "TP": [
        ("RESEARCH", "Read spec & gather references", 0.25),
        ("DRAFT", "Build core solution / first draft", 0.55),
        ("REVIEW", "Test edge cases & revise", 0.80),
        ("SUBMIT", "Final check & upload submission", 0.95),
    ],
    "PROJECT": [
        ("RESEARCH", "Read spec & gather references", 0.25),
        ("DRAFT", "Build core solution / first draft", 0.55),
        ("REVIEW", "Test edge cases & revise", 0.80),
        ("SUBMIT", "Final check & upload submission", 0.95),
    ],
    "LAB": [
        ("RESEARCH", "Pelajari materi & pahami soal", 0.30),
        ("DRAFT", "Kerjakan implementasi / latihan", 0.75),
        ("SUBMIT", "Verifikasi hasil & kumpulkan", 0.95),
    ],
    "QUIZ": [
        ("RESEARCH", "Pelajari materi & pahami soal", 0.30),
        ("DRAFT", "Kerjakan implementasi / latihan", 0.75),
        ("SUBMIT", "Verifikasi hasil & kumpulkan", 0.95),
    ],
    "EXAM": [
        ("RESEARCH", "Ringkas materi & slide kuliah", 0.35),
        ("DRAFT", "Latihan soal tahun lalu", 0.75),
        ("REVIEW", "Review kelemahan & persiapan akhir", 0.92),
    ],
    "OTHER": [
        ("RESEARCH", "Persiapan & riset awal", 0.30),
        ("DRAFT", "Pengerjaan utama", 0.75),
        ("SUBMIT", "Review akhir & finalisasi", 0.95),
    ],
}

PHASE_WEIGHTS: dict[str, tuple[float, ...]] = {
    "TP": (0.15, 0.45, 0.25, 0.15),
    "PROJECT": (0.15, 0.45, 0.25, 0.15),
    "LAB": (0.25, 0.55, 0.20),
    "QUIZ": (0.25, 0.55, 0.20),
    "EXAM": (0.30, 0.45, 0.25),
    "OTHER": (0.25, 0.55, 0.20),
}


def _distribute_estimated_minutes(total_minutes: int, weights: list[float]) -> list[int]:
    """Allocates total_minutes across steps using largest-remainder rounding clamped to [5, 600]."""
    count = len(weights)
    if count == 0:
        return []
    if total_minutes <= 0:
        return [15] * count

    weight_sum = sum(weights) or float(count)
    raw = [total_minutes * (w / weight_sum) for w in weights]
    allocated = [int(math.floor(val)) for val in raw]
    remainder = total_minutes - sum(allocated)
    rank_order = sorted(
        range(count),
        key=lambda i: (raw[i] - allocated[i], weights[i], -i),
        reverse=True,
    )
    for idx in rank_order[:remainder]:
        allocated[idx] += 1

    clamped = [max(5, min(600, val)) for val in allocated]
    if 5 * count <= total_minutes <= 600 * count:
        diff = total_minutes - sum(clamped)
        while diff > 0:
            progressed = False
            for idx in rank_order:
                if clamped[idx] < 600:
                    clamped[idx] += 1
                    diff -= 1
                    progressed = True
                    if diff == 0:
                        break
            if not progressed:
                break
        while diff < 0:
            progressed = False
            for idx in reversed(rank_order):
                if clamped[idx] > 5:
                    clamped[idx] -= 1
                    diff += 1
                    progressed = True
                    if diff == 0:
                        break
            if not progressed:
                break

    return clamped


def generate_checkpoints_for_task(
    task: Task,
    overwrite: bool = False,
    now=None,
) -> list[Checkpoint]:
    """Generate ordered phase checkpoints spaced before ``task.deadline``.

    When ``overwrite=False`` and checkpoints already exist, returns existing
    checkpoints untouched. When ``overwrite=True``, preserves completed checkpoints
    and any incomplete checkpoints with a RUNNING FocusSession, skips regenerating
    those preserved phases, and appends remaining milestones after the highest order.
    """
    with transaction.atomic():
        if overwrite:
            running_cp_ids = set(
                FocusSession.objects.filter(
                    task=task,
                    status="RUNNING",
                    checkpoint_id__isnull=False,
                ).values_list("checkpoint_id", flat=True)
            )
            task.checkpoints.filter(is_done=False).exclude(pk__in=running_cp_ids).delete()
            completed_phases = set(
                task.checkpoints.filter(is_done=True).values_list("phase", flat=True)
            )
            running_phases = (
                set(
                    task.checkpoints.filter(pk__in=running_cp_ids).values_list(
                        "phase", flat=True
                    )
                )
                if running_cp_ids
                else set()
            )
            preserved_phases = completed_phases | running_phases
        else:
            if task.checkpoints.exists():
                return list(task.checkpoints.all())
            preserved_phases = set()

        start = now if now is not None else timezone.now()
        deadline = task.deadline
        if timezone.is_aware(deadline) and timezone.is_naive(start):
            start = timezone.make_aware(start, timezone.get_current_timezone())
        elif timezone.is_naive(deadline) and timezone.is_aware(start):
            start = timezone.make_naive(start, timezone.get_current_timezone())

        category_key = task.category if task.category in PHASE_TEMPLATES else "OTHER"
        template_steps = PHASE_TEMPLATES[category_key]
        template_weights = PHASE_WEIGHTS[category_key]

        selected_steps = [
            (phase, label, ratio, weight)
            for (phase, label, ratio), weight in zip(template_steps, template_weights)
            if phase not in preserved_phases
        ]
        if not selected_steps:
            return []

        min_span = timedelta(hours=2)
        cap = deadline - timedelta(minutes=15)
        remaining = deadline - start
        span = max(remaining, min_span)

        max_order = task.checkpoints.aggregate(max_order=Max("order"))["max_order"] or 0
        step_minutes = _distribute_estimated_minutes(
            int(task.estimated_minutes or 0),
            [weight for _phase, _label, _ratio, weight in selected_steps],
        )

        checkpoints: list[Checkpoint] = []
        for idx, ((phase, label, ratio, _weight), est_minutes) in enumerate(
            zip(selected_steps, step_minutes),
            start=1,
        ):
            if remaining >= min_span:
                target_deadline = min(start + span * ratio, cap)
            elif cap > start:
                available = cap - start
                target_deadline = start + available * ratio
            elif remaining > timedelta(0):
                target_deadline = start + remaining * ratio
            else:
                anchor_start = deadline - min_span
                target_deadline = anchor_start + min_span * ratio

            checkpoints.append(
                Checkpoint(
                    task=task,
                    title=label,
                    phase=phase,
                    target_deadline=target_deadline,
                    estimated_minutes=est_minutes,
                    order=max_order + idx,
                    is_ai_generated=False,
                )
            )

        return Checkpoint.objects.bulk_create(checkpoints)
