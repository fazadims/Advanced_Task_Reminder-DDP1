"""Wave 4 UX regression tests: backend copy localization, archived parity, and single-UPDATE focus finish."""

from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from tasks.forms import CheckpointForm
from tasks.models import Checkpoint, FocusSession, Task
from tasks.services.ai_coaching import ANTI_SNOOZE_TEMPLATES, COACHING_TEMPLATES, OVERDUE_TEMPLATES
from tasks.services.digest import build_daily_digest
from tasks.services.telegram_bot import ESCALATION_LABELS
from tasks.tests.base import FastTestCase

User = get_user_model()

ENGLISH_TOKENS = ("Focus Mode", "[H-3d]", "[H-1d]", "Anti-Snooze", "snooze", "OVERDUE", "Reminder", "Urgent", "CRITICAL")


class Wave4UxRegressionTests(FastTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="wave4_student", email="wave4@ui.ac.id", password="StrongPassword123!"
        )

    def setUp(self):
        self.now = timezone.now()
        self.client.force_login(self.user)

    def test_coaching_templates_and_telegram_labels_are_fully_indonesian(self):
        all_copy = (
            list(COACHING_TEMPLATES.values())
            + list(ANTI_SNOOZE_TEMPLATES.values())
            + list(OVERDUE_TEMPLATES.values())
            + list(ESCALATION_LABELS.values())
        )
        for text in all_copy:
            for token in ENGLISH_TOKENS:
                self.assertNotIn(token, text, msg=f"English token {token!r} leaked in {text!r}")
        self.assertEqual(ESCALATION_LABELS[3], "🔴 KRITIS (H-3 jam)")

    def test_daily_digest_summary_uses_indonesian_header_and_tags(self):
        overdue = Task.objects.create(
            user=self.user, title="Lab Terlambat", deadline=self.now - timedelta(hours=3), status="TODO"
        )
        Task.objects.create(
            user=self.user, title="TP Segera", deadline=self.now + timedelta(hours=20), status="TODO"
        )
        morning = build_daily_digest(self.user, slot="MORNING", now=self.now)
        evening = build_daily_digest(self.user, slot="EVENING", now=self.now)
        self.assertIn("Ringkasan Pagi", morning["summary_text"])
        self.assertIn("Rekap Malam", evening["summary_text"])
        self.assertIn("[TERLAMBAT]", morning["summary_text"])
        self.assertIn(overdue.title, morning["summary_text"])
        for token in ("Morning Briefing", "Evening Recap", "[OVERDUE", "[48H]"):
            self.assertNotIn(token, morning["summary_text"])

    def test_checkpoint_form_required_and_invalid_errors_are_indonesian(self):
        task = Task.objects.create(user=self.user, title="Proyek", deadline=self.now + timedelta(days=5))
        form = CheckpointForm(
            data={"title": "", "target_deadline": "bukan-tanggal", "estimated_minutes": "abc"}, task=task
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Judul checkpoint wajib diisi.", form.errors["title"])
        self.assertIn("Format tanggal/waktu tidak valid. Gunakan pemilih tanggal.", form.errors["target_deadline"])
        self.assertIn("Estimasi durasi harus berupa angka (menit).", form.errors["estimated_minutes"])
        flat = " ".join(str(e) for errs in form.errors.values() for e in errs)
        self.assertNotIn("This field is required", flat)
        self.assertNotIn("Enter a valid", flat)

    def test_archived_task_card_hides_quick_done_and_detail_shows_archived_badge(self):
        archived = Task.objects.create(
            user=self.user, title="Arsip Semester Lalu", deadline=self.now - timedelta(days=4), status="ARCHIVED"
        )
        list_resp = self.client.get(reverse("tasks:task_list"), {"status": "ARCHIVED"})
        self.assertContains(list_resp, archived.title)
        self.assertNotContains(list_resp, reverse("tasks:task_quick_done", kwargs={"pk": archived.pk}))
        self.assertContains(list_resp, 'role="progressbar"')

        detail_resp = self.client.get(reverse("tasks:task_detail", kwargs={"pk": archived.pk}))
        self.assertContains(detail_resp, "Diarsipkan")
        self.assertNotContains(detail_resp, "Terlambat 4 hari")
        self.assertNotContains(detail_resp, reverse("tasks:focus_mode", kwargs={"pk": archived.pk}))
        self.assertNotContains(list_resp, reverse("tasks:focus_mode", kwargs={"pk": archived.pk}))

    def test_finish_focus_session_with_checkpoint_done_issues_single_task_update(self):
        task = Task.objects.create(
            user=self.user, title="Tugas Fokus", deadline=self.now + timedelta(days=3), status="IN_PROGRESS"
        )
        cp = Checkpoint.objects.create(
            task=task, title="Langkah 1", target_deadline=self.now + timedelta(days=1), order=1
        )
        Checkpoint.objects.create(
            task=task, title="Langkah 2", target_deadline=self.now + timedelta(days=2), order=2
        )
        session = FocusSession.objects.create(
            user=self.user, task=task, checkpoint=cp, mode="POMODORO_25_5", planned_minutes=25, status="RUNNING"
        )
        url = reverse("tasks:finish_focus_session", kwargs={"session_id": session.pk})
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.post(
                url,
                data=json.dumps({"status": "COMPLETED", "elapsed_seconds": 1500, "mark_checkpoint_done": True}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["checkpoint_marked"])
        task_updates = [q["sql"] for q in ctx.captured_queries if q["sql"].startswith('UPDATE "tasks_task"')]
        self.assertEqual(len(task_updates), 1)
        task.refresh_from_db()
        self.assertEqual(task.actual_minutes, 25)
        self.assertEqual(task.status, "IN_PROGRESS")

    def test_htmx_checkpoint_error_fragment_has_alert_role(self):
        task = Task.objects.create(user=self.user, title="Tugas", deadline=self.now + timedelta(days=2))
        cp = Checkpoint.objects.create(
            task=task, title="Awal", target_deadline=self.now + timedelta(days=1), order=1
        )
        resp = self.client.post(
            reverse("tasks:edit_checkpoint", kwargs={"pk": cp.pk}),
            {"title": "Awal", "target_deadline": (self.now + timedelta(days=9)).strftime("%Y-%m-%dT%H:%M")},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, 'data-orig-value="Awal"', status_code=400)
        self.assertContains(resp, 'data-editor-error role="alert"', status_code=400)
