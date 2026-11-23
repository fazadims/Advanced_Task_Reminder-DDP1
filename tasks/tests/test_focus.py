"""Tests for F3.3 Focus Mode & Pomodoro Timer session lifecycle and isolation."""

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from tasks.models import Checkpoint, FocusSession, Task

User = get_user_model()


class FocusModeAndSessionTests(TestCase):
    """Covers F3.3 focus mode page, start/finish endpoints, completion sync, and isolation."""

    def setUp(self):
        self.user_a = User.objects.create_user(
            username="focus_a",
            email="focus_a@ui.ac.id",
            password="StrongPass123!",
        )
        self.user_b = User.objects.create_user(
            username="focus_b",
            email="focus_b@ui.ac.id",
            password="StrongPass123!",
        )
        self.now = timezone.now()
        self.task = Task.objects.create(
            user=self.user_a,
            title="TP 3 Recursion & DP",
            category="TP",
            deadline=self.now + timedelta(days=4),
            estimated_minutes=120,
            status="TODO",
        )
        self.cp_active_1 = Checkpoint.objects.create(
            task=self.task,
            title="Implement memoization table",
            phase="DRAFT",
            target_deadline=self.now + timedelta(days=1),
            estimated_minutes=40,
            is_done=False,
            order=1,
        )
        self.cp_active_2 = Checkpoint.objects.create(
            task=self.task,
            title="Run stress test suite",
            phase="REVIEW",
            target_deadline=self.now + timedelta(days=2),
            estimated_minutes=30,
            is_done=False,
            order=2,
        )
        self.cp_done = Checkpoint.objects.create(
            task=self.task,
            title="Read problem specification",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(hours=6),
            estimated_minutes=20,
            is_done=True,
            completed_at=self.now - timedelta(hours=2),
            order=0,
        )

    def test_focus_mode_view_renders_200_with_active_checkpoints(self):
        self.client.force_login(self.user_a)
        url = reverse("tasks:focus_mode", kwargs={"pk": self.task.pk})
        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        active_cps = list(resp.context["checkpoints"])
        self.assertEqual(active_cps, [self.cp_active_1, self.cp_active_2])
        self.assertNotIn(self.cp_done, active_cps)

        html = resp.content.decode()
        self.assertIn(escape(self.task.title), html)
        self.assertIn(self.cp_active_1.title, html)
        self.assertIn(self.cp_active_2.title, html)
        self.assertNotIn(self.cp_done.title, html)

    def test_start_focus_session_creates_running_session_and_returns_metadata(self):
        self.client.force_login(self.user_a)
        start_url = reverse("tasks:start_focus_session")

        resp = self.client.post(
            start_url,
            data=json.dumps(
                {
                    "task_id": self.task.pk,
                    "checkpoint_id": self.cp_active_1.pk,
                    "mode": "DEEP_50_10",
                    "planned_minutes": 50,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIn("session_id", payload)
        self.assertEqual(payload["planned_minutes"], 50)
        self.assertEqual(payload["mode"], "DEEP_50_10")
        self.assertEqual(payload["status"], "RUNNING")

        session = FocusSession.objects.get(pk=payload["session_id"])
        self.assertEqual(session.user, self.user_a)
        self.assertEqual(session.task, self.task)
        self.assertEqual(session.checkpoint, self.cp_active_1)
        self.assertEqual(session.status, "RUNNING")
        self.assertEqual(session.planned_minutes, 50)

    def test_finish_focus_session_completed_marks_checkpoint_and_syncs_task(self):
        self.client.force_login(self.user_a)
        # Remove second active checkpoint so completing cp_active_1 transitions task to DONE
        self.cp_active_2.delete()

        session = FocusSession.objects.create(
            user=self.user_a,
            task=self.task,
            checkpoint=self.cp_active_1,
            mode="POMODORO_25_5",
            planned_minutes=25,
            status="RUNNING",
        )
        finish_url = reverse("tasks:finish_focus_session", kwargs={"session_id": session.pk})

        resp = self.client.post(
            finish_url,
            data=json.dumps(
                {
                    "status": "COMPLETED",
                    "elapsed_seconds": 1200,
                    "mark_checkpoint_done": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        session.refresh_from_db()
        self.cp_active_1.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(session.status, "COMPLETED")
        self.assertIsNotNone(session.ended_at)
        self.assertGreaterEqual(session.actual_minutes, 1)
        self.assertEqual(session.actual_minutes, 20)

        self.assertEqual(self.task.actual_minutes, 20)
        self.assertEqual(self.task.total_focus_minutes(), 20)
        self.assertTrue(self.cp_active_1.is_done)
        self.assertIsNotNone(self.cp_active_1.completed_at)
        self.assertEqual(self.task.status, "DONE")
        self.assertIsNotNone(self.task.completed_at)

        self.assertTrue(data["checkpoint_marked"])
        self.assertEqual(data["actual_minutes"], 20)
        self.assertEqual(data["task_total_focus_minutes"], 20)
        self.assertGreaterEqual(data["today_focus_minutes"], 20)

    def test_finish_focus_session_completed_with_remaining_checkpoint_sets_in_progress(self):
        self.client.force_login(self.user_a)
        session = FocusSession.objects.create(
            user=self.user_a,
            task=self.task,
            checkpoint=self.cp_active_1,
            mode="POMODORO_25_5",
            planned_minutes=25,
            status="RUNNING",
        )
        finish_url = reverse("tasks:finish_focus_session", kwargs={"session_id": session.pk})

        # Even with elapsed_seconds=0, COMPLETED session logs actual_minutes >= 1
        resp = self.client.post(
            finish_url,
            data=json.dumps(
                {
                    "status": "COMPLETED",
                    "elapsed_seconds": 0,
                    "mark_checkpoint_done": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        self.cp_active_1.refresh_from_db()
        self.task.refresh_from_db()

        self.assertGreaterEqual(session.actual_minutes, 1)
        self.assertEqual(self.task.actual_minutes, session.actual_minutes)
        self.assertEqual(self.task.total_focus_minutes(), session.actual_minutes)
        self.assertTrue(self.cp_active_1.is_done)
        self.assertEqual(self.task.status, "IN_PROGRESS")

    def test_finish_focus_session_abandoned_does_not_complete_checkpoint_or_add_focus_minutes(self):
        self.client.force_login(self.user_a)
        session = FocusSession.objects.create(
            user=self.user_a,
            task=self.task,
            checkpoint=self.cp_active_1,
            mode="POMODORO_25_5",
            planned_minutes=25,
            status="RUNNING",
        )
        finish_url = reverse("tasks:finish_focus_session", kwargs={"session_id": session.pk})

        resp = self.client.post(
            finish_url,
            data=json.dumps(
                {
                    "status": "ABANDONED",
                    "elapsed_seconds": 600,
                    "mark_checkpoint_done": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        session.refresh_from_db()
        self.cp_active_1.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(session.status, "ABANDONED")
        self.assertIsNotNone(session.ended_at)
        self.assertFalse(self.cp_active_1.is_done)
        self.assertIsNone(self.cp_active_1.completed_at)
        self.assertEqual(self.task.actual_minutes, 0)
        self.assertEqual(self.task.total_focus_minutes(), 0)
        self.assertEqual(self.task.status, "TODO")
        self.assertFalse(data["checkpoint_marked"])
        self.assertEqual(data["task_total_focus_minutes"], 0)
        self.assertEqual(data["today_focus_minutes"], 0)

    def test_cross_user_404_isolation_on_focus_views(self):
        session_a = FocusSession.objects.create(
            user=self.user_a,
            task=self.task,
            checkpoint=self.cp_active_1,
            mode="POMODORO_25_5",
            planned_minutes=25,
            status="RUNNING",
        )
        self.client.force_login(self.user_b)

        # GET focus page for user_a's task -> 404
        resp_page = self.client.get(reverse("tasks:focus_mode", kwargs={"pk": self.task.pk}))
        self.assertEqual(resp_page.status_code, 404)

        # POST start focus on user_a's task -> 404
        resp_start = self.client.post(
            reverse("tasks:start_focus_session"),
            data=json.dumps({"task_id": self.task.pk, "planned_minutes": 25}),
            content_type="application/json",
        )
        self.assertEqual(resp_start.status_code, 404)
        self.assertEqual(FocusSession.objects.filter(user=self.user_b).count(), 0)

        # POST finish on user_a's session -> 404
        resp_finish = self.client.post(
            reverse("tasks:finish_focus_session", kwargs={"session_id": session_a.pk}),
            data=json.dumps({"status": "COMPLETED", "mark_checkpoint_done": True}),
            content_type="application/json",
        )
        self.assertEqual(resp_finish.status_code, 404)
        session_a.refresh_from_db()
        self.assertEqual(session_a.status, "RUNNING")
        self.cp_active_1.refresh_from_db()
        self.assertFalse(self.cp_active_1.is_done)
