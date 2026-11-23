from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from tasks.models import Checkpoint, Course, Task


class Command(BaseCommand):
    help = "Isi database dengan data demo (Demo Mode)."

    def handle(self, *args, **options):
        Checkpoint.objects.all().delete()
        Task.objects.all().delete()
        Course.objects.all().delete()

        now = timezone.now()
        today = timezone.localdate()
        ddp = Course.objects.create(name="DDP 1")
        mat = Course.objects.create(name="Matematika Diskret")
        pbp = Course.objects.create(name="Sistem Digital")

        data = [
            (ddp, "Proyek Kelompok", "Proyek", 25, 17),
            (ddp, "Kuis 3", "Kuis", 5, 2),
            (mat, "UTS Matdis", "UTS", 30, 6),
            (mat, "Tugas Pemodelan", "Tugas", 10, 4),
            (pbp, "Lab 7", "Lab", 3, 1),
            (pbp, "UAS Sisdig", "UAS", 30, 25),
        ]
        for course, title, cat, weight, days in data:
            task = Task.objects.create(
                course=course, title=title, category=cat, weight=weight,
                deadline=now + timedelta(days=days),
            )
            for i, step in enumerate(["Riset", "Draft", "Finalisasi"]):
                Checkpoint.objects.create(
                    task=task, title=f"{step} - {title}",
                    due_date=today + timedelta(days=i * max(days // 3, 1)),
                    done=(i == 0 and days > 10),
                )
        self.stdout.write(self.style.SUCCESS("Data demo dibuat."))
