"""Auto-checkpoint milestone generator for tasks (F3.2)."""

from datetime import timedelta

from django.db.models import Max
from django.utils import timezone

from tasks.models import Checkpoint, Task

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


def generate_checkpoints_for_task(
    task: Task,
    overwrite: bool = False,
    now=None,
) -> list[Checkpoint]:
    """Generate ordered phase checkpoints spaced before ``task.deadline``.

    When ``overwrite=False`` and checkpoints already exist, returns existing
    checkpoints untouched. When ``overwrite=True``, removes only incomplete
    checkpoints, preserves completed ones, and appends generated milestones
    after the highest existing order.
    """
    if overwrite:
        task.checkpoints.filter(is_done=False).delete()
    elif task.checkpoints.exists():
        return list(task.checkpoints.all())

    start = now if now is not None else timezone.now()
    deadline = task.deadline
    if timezone.is_aware(deadline) and timezone.is_naive(start):
        start = timezone.make_aware(start, timezone.get_current_timezone())
    elif timezone.is_naive(deadline) and timezone.is_aware(start):
        start = timezone.make_naive(start, timezone.get_current_timezone())

    steps = PHASE_TEMPLATES.get(task.category, PHASE_TEMPLATES["OTHER"])
    min_span = timedelta(hours=2)
    cap = deadline - timedelta(minutes=15)
    remaining = deadline - start
    span = max(remaining, min_span)

    max_order = task.checkpoints.aggregate(max_order=Max("order"))["max_order"] or 0
    per_step_minutes = max(15, round((task.estimated_minutes or 0) / len(steps)))

    checkpoints: list[Checkpoint] = []
    for idx, (phase, label, ratio) in enumerate(steps, start=1):
        if remaining >= min_span:
            target_deadline = min(start + span * ratio, cap)
        elif cap > start:
            available = cap - start
            target_deadline = start + available * ratio
        else:
            anchor_start = deadline - min_span
            target_deadline = anchor_start + min_span * ratio

        checkpoints.append(
            Checkpoint(
                task=task,
                title=label,
                phase=phase,
                target_deadline=target_deadline,
                estimated_minutes=per_step_minutes,
                order=max_order + idx,
                is_ai_generated=False,
            )
        )

    return Checkpoint.objects.bulk_create(checkpoints)
