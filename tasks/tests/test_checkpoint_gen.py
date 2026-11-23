"""Tests for F3.2 Auto-Checkpoint Milestone Generator & HTMX Regenerate View."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tasks.models import Checkpoint, Task
from tasks.services.checkpoint_gen import generate_checkpoints_for_task

User = get_user_model()


class CheckpointGeneratorTests(TestCase):
    """Covers F3.2 checkpoint generation rules, edge cases, overwrite, and HTMX view."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="gen_student",
            email="gen@ui.ac.id",
            password="StrongPass123!",
        )
        self.other_user = User.objects.create_user(
            username="other_gen_student",
            email="other@ui.ac.id",
            password="StrongPass123!",
        )
        self.now = timezone.now()

    def _make_task(self, category="TP", delta=timedelta(days=7), estimated_minutes=200):
        return Task.objects.create(
            user=self.user,
            title=f"Sample {category} Task",
            category=category,
            deadline=self.now + delta,
            estimated_minutes=estimated_minutes,
            status="TODO",
        )

    def test_7_day_tp_task_generates_4_chronological_checkpoints_before_deadline(self):
        task = self._make_task(category="TP", delta=timedelta(days=7), estimated_minutes=240)
        created = generate_checkpoints_for_task(task, now=self.now)

        self.assertEqual(len(created), 4)
        self.assertEqual(task.checkpoints.count(), 4)

        checkpoints = list(task.checkpoints.all())
        self.assertEqual([cp.phase for cp in checkpoints], ["RESEARCH", "DRAFT", "REVIEW", "SUBMIT"])
        self.assertEqual([cp.order for cp in checkpoints], [1, 2, 3, 4])

        for cp in checkpoints:
            self.assertLess(cp.target_deadline, task.deadline)
            self.assertGreater(cp.target_deadline, self.now)
            self.assertFalse(cp.is_ai_generated)
            self.assertEqual(cp.estimated_minutes, 60)

        # Strictly chronologically sorted
        for prev_cp, next_cp in zip(checkpoints, checkpoints[1:]):
            self.assertLess(prev_cp.target_deadline, next_cp.target_deadline)

    def test_lab_and_exam_tasks_generate_3_checkpoints(self):
        lab_task = self._make_task(category="LAB", delta=timedelta(days=3), estimated_minutes=90)
        exam_task = self._make_task(category="EXAM", delta=timedelta(days=10), estimated_minutes=300)

        lab_cps = generate_checkpoints_for_task(lab_task, now=self.now)
        exam_cps = generate_checkpoints_for_task(exam_task, now=self.now)

        self.assertEqual(len(lab_cps), 3)
        self.assertEqual([cp.phase for cp in lab_cps], ["RESEARCH", "DRAFT", "SUBMIT"])
        for cp in lab_cps:
            self.assertLess(cp.target_deadline, lab_task.deadline)

        self.assertEqual(len(exam_cps), 3)
        self.assertEqual([cp.phase for cp in exam_cps], ["RESEARCH", "DRAFT", "REVIEW"])
        for cp in exam_cps:
            self.assertLess(cp.target_deadline, exam_task.deadline)

    def test_urgent_3_hour_task_generates_valid_chronological_checkpoints_before_deadline(self):
        urgent_task = self._make_task(category="TP", delta=timedelta(hours=3), estimated_minutes=120)
        created = generate_checkpoints_for_task(urgent_task, now=self.now)

        self.assertEqual(len(created), 4)
        for cp in created:
            self.assertLess(cp.target_deadline, urgent_task.deadline)
            self.assertGreaterEqual(cp.target_deadline, self.now)

        for prev_cp, next_cp in zip(created, created[1:]):
            self.assertLessEqual(prev_cp.target_deadline, next_cp.target_deadline)

    def test_sub_2_hour_and_overdue_tasks_generate_valid_checkpoints_without_crashing(self):
        # Task due in 1 hour (< 2h min_span)
        very_urgent = self._make_task(category="LAB", delta=timedelta(hours=1), estimated_minutes=45)
        urgent_cps = generate_checkpoints_for_task(very_urgent, now=self.now)
        self.assertEqual(len(urgent_cps), 3)
        for cp in urgent_cps:
            self.assertLess(cp.target_deadline, very_urgent.deadline)
        for prev_cp, next_cp in zip(urgent_cps, urgent_cps[1:]):
            self.assertLess(prev_cp.target_deadline, next_cp.target_deadline)

        # Overdue task (deadline 4 hours in the past)
        overdue_task = self._make_task(category="TP", delta=timedelta(hours=-4), estimated_minutes=120)
        overdue_cps = generate_checkpoints_for_task(overdue_task, now=self.now)
        self.assertEqual(len(overdue_cps), 4)
        for cp in overdue_cps:
            self.assertLess(cp.target_deadline, overdue_task.deadline)
        for prev_cp, next_cp in zip(overdue_cps, overdue_cps[1:]):
            self.assertLess(prev_cp.target_deadline, next_cp.target_deadline)

    def test_overwrite_false_does_not_duplicate_existing_checkpoints(self):
        task = self._make_task(category="LAB", delta=timedelta(days=4))
        first_run = generate_checkpoints_for_task(task, overwrite=False, now=self.now)
        second_run = generate_checkpoints_for_task(task, overwrite=False, now=self.now)

        self.assertEqual(len(first_run), 3)
        self.assertEqual([c.pk for c in first_run], [c.pk for c in second_run])
        self.assertEqual(task.checkpoints.count(), 3)

    def test_overwrite_true_preserves_completed_and_replaces_incomplete_checkpoints(self):
        task = self._make_task(category="LAB", delta=timedelta(days=5))
        done_cp = Checkpoint.objects.create(
            task=task,
            title="Completed Custom Research",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(days=1),
            estimated_minutes=30,
            is_done=True,
            completed_at=self.now - timedelta(hours=1),
            order=1,
        )
        incomplete_cp = Checkpoint.objects.create(
            task=task,
            title="Stale Incomplete Step",
            phase="DRAFT",
            target_deadline=self.now + timedelta(days=2),
            estimated_minutes=30,
            is_done=False,
            order=2,
        )

        new_cps = generate_checkpoints_for_task(task, overwrite=True, now=self.now)

        self.assertEqual(len(new_cps), 3)
        # Completed checkpoint must still exist untouched
        self.assertTrue(Checkpoint.objects.filter(pk=done_cp.pk, is_done=True).exists())
        # Old incomplete checkpoint must be deleted
        self.assertFalse(Checkpoint.objects.filter(pk=incomplete_cp.pk).exists())
        # New checkpoints are ordered strictly after the preserved checkpoint
        for cp in new_cps:
            self.assertGreater(cp.order, done_cp.order)
        self.assertEqual(task.checkpoints.count(), 4)

    def test_htmx_regenerate_checkpoints_view_preserves_done_and_updates_progress(self):
        task = self._make_task(category="TP", delta=timedelta(days=7))
        done_cp = Checkpoint.objects.create(
            task=task,
            title="Already Finished Milestone",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(days=1),
            is_done=True,
            completed_at=self.now,
            order=1,
        )
        stale_cp = Checkpoint.objects.create(
            task=task,
            title="Outdated Draft Milestone",
            phase="DRAFT",
            target_deadline=self.now + timedelta(days=2),
            is_done=False,
            order=2,
        )

        self.client.force_login(self.user)
        url = reverse("tasks:regenerate_checkpoints", kwargs={"pk": task.pk})
        resp = self.client.post(url, HTTP_HX_REQUEST="true")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Checkpoint.objects.filter(pk=done_cp.pk).exists())
        self.assertFalse(Checkpoint.objects.filter(pk=stale_cp.pk).exists())
        # 1 preserved done checkpoint + 4 regenerated TP checkpoints = 5 total (20% progress)
        self.assertEqual(task.checkpoints.count(), 5)
        task.refresh_from_db()
        self.assertEqual(task.status, "IN_PROGRESS")
        self.assertEqual(task.progress_percent(), 20)

        html = resp.content.decode()
        self.assertIn('id="checkpoint-list"', html)
        self.assertIn("Already Finished Milestone", html)
        self.assertIn('hx-swap-oob="true"', html)
        self.assertIn("20%", html)

    def test_regenerate_checkpoints_cross_user_returns_404(self):
        task = self._make_task(category="LAB", delta=timedelta(days=3))
        self.client.force_login(self.other_user)
        url = reverse("tasks:regenerate_checkpoints", kwargs={"pk": task.pk})
        resp = self.client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(task.checkpoints.count(), 0)
