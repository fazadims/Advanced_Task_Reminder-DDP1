from datetime import timedelta

from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from tasks.models import Checkpoint, Task

from . import services


def dashboard(request):
    """Dashboard 'Fokus Hari Ini'."""
    now = timezone.now()
    today = timezone.localdate()
    tasks = list(Task.objects.filter(done=False).select_related("course"))

    rows = services.rank_tasks(tasks, now)
    labels, hours = services.workload_by_day(tasks, today, 14)
    crunch = [
        {"year": y, "week": w, "hours": h}
        for (y, w), h in sorted(services.crunch_weeks(tasks).items())
    ]
    checkpoints = Checkpoint.objects.filter(
        due_date=today, done=False
    ).select_related("task")

    context = {
        "rows": rows,
        "checkpoints": checkpoints,
        "total_hours": round(sum(r["hours"] for r in rows), 1),
        "crunch": crunch,
        "labels": labels,
        "hours": hours,
        "now": now,
    }
    return render(request, "analytics/dashboard.html", context)


def task_detail(request, pk):
    """Detail tugas + alasan skor prioritas."""
    task = get_object_or_404(Task.objects.select_related("course"), pk=pk)
    now = timezone.now()
    score, reasons = services.priority_score(task, now)
    code, icon, color = services.priority_label(score)
    checkpoints = task.checkpoints.all()
    total = checkpoints.count()
    done = checkpoints.filter(done=True).count()
    context = {
        "task": task, "score": score, "reasons": reasons, "icon": icon,
        "color": color, "hours": services.task_hours(task),
        "checkpoints": checkpoints,
        "progress": round(done / total * 100) if total else 0,
    }
    return render(request, "analytics/task_detail.html", context)
