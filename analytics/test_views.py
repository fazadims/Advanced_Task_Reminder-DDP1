from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tasks.models import Checkpoint, Course, Task


class DashboardViewTest(TestCase):
    def setUp(self):
        self.course = Course.objects.create(name="DDP 1")
        self.task = Task.objects.create(
            course=self.course, title="Proyek Kelompok", category="Proyek",
            weight=25, deadline=timezone.now() + timedelta(days=5),
        )
        Checkpoint.objects.create(
            task=self.task, title="Riset", due_date=timezone.localdate()
        )

    def test_dashboard_ok(self):
        res = self.client.get(reverse("analytics:dashboard"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Proyek Kelompok")
        self.assertContains(res, "Riset")

    def test_dashboard_empty(self):
        Task.objects.all().delete()
        res = self.client.get(reverse("analytics:dashboard"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Belum ada tugas")

    def test_done_task_hidden(self):
        self.task.done = True
        self.task.save()
        res = self.client.get(reverse("analytics:dashboard"))
        self.assertNotContains(res, "Proyek Kelompok")


class TaskDetailViewTest(TestCase):
    def setUp(self):
        course = Course.objects.create(name="DDP 1")
        self.task = Task.objects.create(
            course=course, title="Kuis 3", category="Kuis", weight=5,
            deadline=timezone.now() + timedelta(days=3),
        )

    def test_detail_ok(self):
        res = self.client.get(reverse("analytics:task_detail", args=[self.task.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Kuis 3")
        self.assertContains(res, "Rekomendasi waktu pengerjaan")

    def test_detail_404(self):
        res = self.client.get(reverse("analytics:task_detail", args=[9999]))
        self.assertEqual(res.status_code, 404)

    def test_progress_percentage(self):
        Checkpoint.objects.create(task=self.task, title="A", due_date=timezone.localdate(), done=True)
        Checkpoint.objects.create(task=self.task, title="B", due_date=timezone.localdate())
        res = self.client.get(reverse("analytics:task_detail", args=[self.task.pk]))
        self.assertEqual(res.context["progress"], 50)
