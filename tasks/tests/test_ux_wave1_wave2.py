"""Comprehensive unit & integration tests for Wave 1 & Wave 2 UX features.

Covers:
1. Priority engine (`tasks.services.priority`) and task/checkpoint/reminder lifecycle sync.
2. Template tags & filters (`tasks.templatetags.task_ux`).
3. Quick Add Course & Quick Done (`tasks:quick_add_course`, `tasks:task_quick_done`).
4. Task List search, sorting, urgency quick-filters, stats, and History KPIs.
5. Focus Mode checkpoint deep-linking, edit_checkpoint 400 value preservation, and HTMX toasts.
6. Unified navbar alert badge, inbox OOB sync, `remindersChanged` triggers, and Indonesian auth errors.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from brp.models import Course
from tasks.context_processors import nav_badges
from tasks.models import Checkpoint, FocusSession, Reminder, Task
from tasks.services.notifier import complete_reminder_action
from tasks.services.priority import recalculate_task_priority
from tasks.templatetags.task_ux import (
    COURSE_PALETTE,
    abs_int,
    checkpoint_progress,
    course_badge_class,
    difficulty_info,
    focus_mode_label,
    format_weight,
    human_duration,
    relative_deadline,
    reminder_escalation_label,
    reminder_status_label,
    urgency_stripe_class,
)
from tasks.tests.base import FastTestCase

User = get_user_model()


# =============================================================================
# 1. Priority Engine & Lifecycle Sync
# =============================================================================


class PriorityEngineAndLifecycleTests(FastTestCase):
    """Tests deterministic priority scoring and automatic sync across task lifecycle."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="prio_student",
            email="prio@ui.ac.id",
            password="StrongPassword123!",
        )
        cls.course = Course.objects.create(
            user=cls.user,
            code="CSGE601020",
            name="Dasar-Dasar Pemrograman 1",
        )

    def setUp(self):
        self.now = timezone.now()
        self.client.force_login(self.user)

    def test_recalculate_task_priority_tiers_and_cap(self):
        # CRITICAL: overdue (50) + weight 35 (25 cap) + diff 5 (15) + group (5) + 180m (5) = 100.0
        critical_task = Task(
            user=self.user,
            title="Proyek Akhir Terlambat",
            deadline=self.now - timedelta(hours=5),
            weight_percentage=Decimal("35.00"),
            difficulty=5,
            is_group_project=True,
            estimated_minutes=240,
            status="IN_PROGRESS",
        )
        score, level, reason = recalculate_task_priority(critical_task, now=self.now)
        self.assertLessEqual(score, 100.0)
        self.assertEqual(score, 100.0)
        self.assertEqual(level, "CRITICAL")
        self.assertIn("Terlambat 5 jam", reason)
        self.assertIn("Bobot BRP 35%", reason)
        self.assertIn("Kesulitan Sangat Sulit (5/5)", reason)
        self.assertIn("Proyek kelompok", reason)

        # HIGH: 20h left (45) + weight 10 (8.5) + diff 3 (9) = 62.5
        high_task = Task(
            user=self.user,
            title="TP Mendesak",
            deadline=self.now + timedelta(hours=20),
            weight_percentage=Decimal("10.00"),
            difficulty=3,
            estimated_minutes=60,
            status="TODO",
        )
        score, level, reason = recalculate_task_priority(high_task, now=self.now)
        self.assertEqual(score, 62.5)
        self.assertEqual(level, "HIGH")
        self.assertIn("Deadline 20 jam lagi", reason)
        self.assertIn("Bobot BRP 10%", reason)
        self.assertIn("Kesulitan Sedang (3/5)", reason)

        # MEDIUM: 4d left (24) + weight 12.5 (10.625) + diff 2 (6) = 40.6
        medium_task = Task(
            user=self.user,
            title="Lab Mingguan",
            deadline=self.now + timedelta(days=4),
            weight_percentage=Decimal("12.50"),
            difficulty=2,
            estimated_minutes=45,
            status="TODO",
        )
        score, level, reason = recalculate_task_priority(medium_task, now=self.now)
        self.assertEqual(score, 40.6)
        self.assertEqual(level, "MEDIUM")
        self.assertIn("Deadline 4 hari lagi", reason)
        self.assertIn("Bobot BRP 12.5%", reason)
        self.assertIn("Kesulitan Mudah (2/5)", reason)

        # LOW: 15d left (6) + weight 0 (0) + diff 1 (3) = 9.0
        low_task = Task(
            user=self.user,
            title="Bacaan Ringan",
            deadline=self.now + timedelta(days=15),
            weight_percentage=Decimal("0.00"),
            difficulty=1,
            estimated_minutes=30,
            status="TODO",
        )
        score, level, reason = recalculate_task_priority(low_task, now=self.now)
        self.assertEqual(score, 9.0)
        self.assertEqual(level, "LOW")
        self.assertIn("Deadline 15 hari lagi", reason)
        self.assertIn("Kesulitan Sangat Mudah (1/5)", reason)
        self.assertNotIn("Bobot BRP", reason)

    def test_recalculate_task_priority_done_archived_none_deadline_and_naive_now(self):
        for closed_status in ("DONE", "ARCHIVED"):
            closed_task = Task(
                user=self.user,
                title=f"Tugas {closed_status}",
                deadline=self.now - timedelta(hours=2),
                weight_percentage=Decimal("30.00"),
                difficulty=5,
                status=closed_status,
            )
            score, level, reason = recalculate_task_priority(closed_task, now=self.now)
            self.assertEqual(score, 0.0)
            self.assertEqual(level, "LOW")
            self.assertEqual(reason, "Tugas sudah selesai atau diarsipkan.")
            self.assertEqual(closed_task.priority_score, 0.0)
            self.assertEqual(closed_task.priority_level, "LOW")

        # Defensive deadline=None guard
        no_deadline_task = Task(
            user=self.user,
            title="Tanpa Tenggat",
            deadline=None,
            status="TODO",
        )
        score, level, reason = recalculate_task_priority(no_deadline_task, now=self.now)
        self.assertEqual(score, 0.0)
        self.assertEqual(level, "LOW")
        self.assertEqual(reason, "Tenggat waktu belum ditentukan.")

        # Naive `now` handling against aware `task.deadline`
        aware_deadline = timezone.make_aware(
            datetime(2026, 11, 24, 15, 0, 0),
            timezone.get_current_timezone(),
        )
        naive_now = datetime(2026, 11, 24, 10, 0, 0)
        task_naive_test = Task(
            user=self.user,
            title="Uji Naive Now",
            deadline=aware_deadline,
            weight_percentage=Decimal("20.00"),
            difficulty=4,
            status="TODO",
        )
        score, level, reason = recalculate_task_priority(task_naive_test, now=naive_now)
        self.assertGreater(score, 70.0)
        self.assertIn(level, ("HIGH", "CRITICAL"))
        self.assertIn("Deadline 5 jam lagi", reason)

    def test_task_create_and_update_automatically_compute_priority(self):
        deadline_str = timezone.localtime(self.now + timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M")
        create_resp = self.client.post(
            reverse("tasks:task_create"),
            {
                "title": "TP 2 Priority Auto",
                "course": self.course.pk,
                "category": "TP",
                "deadline": deadline_str,
                "estimated_minutes": 180,
                "weight_percentage": "20.00",
                "difficulty": 4,
                "status": "TODO",
                "auto_generate_checkpoints": "",
            },
        )
        self.assertEqual(create_resp.status_code, 302)
        task = Task.objects.get(user=self.user, title="TP 2 Priority Auto")
        self.assertGreater(task.priority_score, 0.0)
        self.assertIn(task.priority_level, ("HIGH", "CRITICAL"))
        self.assertIn("Bobot BRP 20%", task.priority_reason)
        self.assertIn("Kesulitan Sulit (4/5)", task.priority_reason)

        # Updating urgency & attributes recalculates priority automatically
        overdue_str = timezone.localtime(self.now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        update_resp = self.client.post(
            reverse("tasks:task_update", kwargs={"pk": task.pk}),
            {
                "title": "TP 2 Priority Auto",
                "course": self.course.pk,
                "category": "PROJECT",
                "deadline": overdue_str,
                "estimated_minutes": 240,
                "weight_percentage": "30.00",
                "difficulty": 5,
                "is_group_project": "on",
                "status": "IN_PROGRESS",
            },
        )
        self.assertEqual(update_resp.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.priority_score, 100.0)
        self.assertEqual(task.priority_level, "CRITICAL")
        self.assertIn("Terlambat", task.priority_reason)
        self.assertIn("Proyek kelompok", task.priority_reason)

    def test_toggle_last_checkpoint_htmx_marks_done_resets_priority_and_returns_oob(self):
        task = Task.objects.create(
            user=self.user,
            course=self.course,
            title="Lab OOB Sync",
            deadline=self.now + timedelta(hours=12),
            weight_percentage=Decimal("20.00"),
            difficulty=4,
            status="IN_PROGRESS",
        )
        recalculate_task_priority(task, now=self.now)
        task.save(update_fields=["priority_score", "priority_level", "priority_reason"])
        self.assertGreater(task.priority_score, 0.0)

        Checkpoint.objects.create(
            task=task,
            title="Langkah 1",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(hours=4),
            is_done=True,
            completed_at=self.now - timedelta(hours=1),
            order=1,
        )
        last_cp = Checkpoint.objects.create(
            task=task,
            title="Langkah 2",
            phase="SUBMIT",
            target_deadline=self.now + timedelta(hours=10),
            is_done=False,
            order=2,
        )

        resp = self.client.post(
            reverse("tasks:toggle_checkpoint", kwargs={"pk": last_cp.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "DONE")
        self.assertEqual(task.priority_score, 0.0)
        self.assertEqual(task.priority_level, "LOW")

        html = resp.content.decode("utf-8")
        self.assertIn('id="task-priority-card"', html)
        self.assertIn('id="task-progress-bar"', html)
        self.assertIn('id="upcoming-reminders"', html)
        self.assertIn('hx-swap-oob="true"', html)

    def test_untoggle_checkpoint_restores_in_progress_and_nonzero_priority(self):
        task = Task.objects.create(
            user=self.user,
            course=self.course,
            title="Tugas Reopen CP",
            deadline=self.now + timedelta(days=1),
            weight_percentage=Decimal("15.00"),
            difficulty=4,
            status="DONE",
            completed_at=self.now - timedelta(hours=1),
            priority_score=0.0,
            priority_level="LOW",
            priority_reason="Tugas sudah selesai atau diarsipkan.",
        )
        Checkpoint.objects.create(
            task=task,
            title="CP 1",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(hours=6),
            is_done=True,
            completed_at=self.now - timedelta(hours=2),
            order=1,
        )
        cp2 = Checkpoint.objects.create(
            task=task,
            title="CP 2",
            phase="DRAFT",
            target_deadline=self.now + timedelta(hours=12),
            is_done=True,
            completed_at=self.now - timedelta(hours=1),
            order=2,
        )

        resp = self.client.post(
            reverse("tasks:toggle_checkpoint", kwargs={"pk": cp2.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "IN_PROGRESS")
        self.assertIsNone(task.completed_at)
        self.assertGreater(task.priority_score, 0.0)
        self.assertIn("Bobot BRP 15%", task.priority_reason)
        self.assertNotEqual(task.priority_reason, "Tugas sudah selesai atau diarsipkan.")

    def test_complete_reminder_action_syncs_task_priority(self):
        task_with_cp = Task.objects.create(
            user=self.user,
            title="Reminder Priority CP",
            deadline=self.now + timedelta(hours=18),
            weight_percentage=Decimal("25.00"),
            difficulty=4,
            status="TODO",
        )
        recalculate_task_priority(task_with_cp, now=self.now)
        task_with_cp.save(update_fields=["priority_score", "priority_level", "priority_reason"])
        self.assertGreater(task_with_cp.priority_score, 0.0)

        cp = Checkpoint.objects.create(
            task=task_with_cp,
            title="Selesaikan Kode",
            phase="DRAFT",
            target_deadline=self.now + timedelta(hours=12),
            order=1,
        )
        rem_cp = Reminder.objects.create(
            user=self.user,
            task=task_with_cp,
            checkpoint=cp,
            scheduled_for=self.now - timedelta(minutes=10),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )

        ok, result = complete_reminder_action(rem_cp.pk, user=self.user, now=self.now)
        self.assertTrue(ok)
        self.assertEqual(result["status"], "DONE")
        cp.refresh_from_db()
        task_with_cp.refresh_from_db()
        self.assertTrue(cp.is_done)
        self.assertEqual(task_with_cp.status, "DONE")
        self.assertEqual(task_with_cp.priority_score, 0.0)
        self.assertEqual(task_with_cp.priority_level, "LOW")

        # Task without checkpoints also resets priority_score when task-level reminder is completed
        task_no_cp = Task.objects.create(
            user=self.user,
            title="Reminder Priority Task Only",
            deadline=self.now + timedelta(hours=10),
            weight_percentage=Decimal("15.00"),
            difficulty=3,
            status="TODO",
        )
        recalculate_task_priority(task_no_cp, now=self.now)
        task_no_cp.save(update_fields=["priority_score", "priority_level", "priority_reason"])
        rem_task = Reminder.objects.create(
            user=self.user,
            task=task_no_cp,
            checkpoint=None,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )

        ok2, _ = complete_reminder_action(rem_task.pk, user=self.user, now=self.now)
        self.assertTrue(ok2)
        task_no_cp.refresh_from_db()
        self.assertEqual(task_no_cp.status, "DONE")
        self.assertEqual(task_no_cp.priority_score, 0.0)
        self.assertEqual(task_no_cp.priority_level, "LOW")


# =============================================================================
# 2. Template Tags & Filters (`tasks/templatetags/task_ux.py`)
# =============================================================================


class TemplateTagsAndFiltersTests(FastTestCase):
    """Tests all UX template filters and deterministic badge/duration formatting."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="filter_student",
            email="filter@ui.ac.id",
            password="StrongPassword123!",
        )
        cls.course = Course.objects.create(
            user=cls.user,
            code="CSGE602040",
            name="Struktur Data & Algoritma",
        )

    def test_human_duration_filter(self):
        self.assertEqual(human_duration(0), "0 mnt")
        self.assertEqual(human_duration(-15), "0 mnt")
        self.assertEqual(human_duration(45), "45 mnt")
        self.assertEqual(human_duration(60), "1 jam")
        self.assertEqual(human_duration(150), "2 jam 30 mnt")
        self.assertEqual(human_duration("not-a-number"), "0 mnt")
        self.assertEqual(human_duration(None), "0 mnt")

    def test_relative_deadline_filter_all_ranges(self):
        tz = timezone.get_current_timezone()
        fixed_now = timezone.make_aware(datetime(2026, 11, 23, 10, 0, 0), tz)

        with patch("tasks.templatetags.task_ux.timezone.now", return_value=fixed_now):
            # None guard
            none_res = relative_deadline(None)
            self.assertEqual(none_res["text"], "—")
            self.assertEqual(none_res["tone"], "normal")
            self.assertFalse(none_res["is_overdue"])

            # Overdue: minutes, hours, days
            ov_min = relative_deadline(fixed_now - timedelta(minutes=25))
            self.assertEqual(ov_min["text"], "Terlambat 25 mnt")
            self.assertEqual(ov_min["tone"], "overdue")
            self.assertTrue(ov_min["is_overdue"])

            ov_hr = relative_deadline(fixed_now - timedelta(hours=5))
            self.assertEqual(ov_hr["text"], "Terlambat 5 jam")
            self.assertEqual(ov_hr["tone"], "overdue")

            ov_day = relative_deadline(fixed_now - timedelta(days=3))
            self.assertEqual(ov_day["text"], "Terlambat 3 hari")
            self.assertEqual(ov_day["tone"], "overdue")

            # < 60s -> "< 1 mnt lagi" (never "< 1 jam lagi")
            under_min = relative_deadline(fixed_now + timedelta(seconds=35))
            self.assertEqual(under_min["text"], "< 1 mnt lagi")
            self.assertNotEqual(under_min["text"], "< 1 jam lagi")
            self.assertEqual(under_min["tone"], "urgent")
            self.assertFalse(under_min["is_overdue"])

            # 60–119s -> "1 mnt lagi"
            one_min = relative_deadline(fixed_now + timedelta(seconds=90))
            self.assertEqual(one_min["text"], "1 mnt lagi")
            self.assertEqual(one_min["tone"], "urgent")

            # 30m -> "30 mnt lagi"
            thirty_min = relative_deadline(fixed_now + timedelta(minutes=30))
            self.assertEqual(thirty_min["text"], "30 mnt lagi")
            self.assertEqual(thirty_min["tone"], "urgent")

            # 12h -> "12 jam lagi"
            twelve_hrs = relative_deadline(fixed_now + timedelta(hours=12))
            self.assertEqual(twelve_hrs["text"], "12 jam lagi")
            self.assertEqual(twelve_hrs["tone"], "urgent")

            # Tomorrow (>24h and <=48h, date == tomorrow) -> "Besok, HH:MM"
            tomorrow_dt = timezone.make_aware(datetime(2026, 11, 24, 18, 30, 0), tz)
            tomorrow_res = relative_deadline(tomorrow_dt)
            self.assertEqual(tomorrow_res["text"], "Besok, 18:30")
            self.assertEqual(tomorrow_res["tone"], "soon")

            # 5d -> "5 hari lagi"
            five_days = relative_deadline(fixed_now + timedelta(days=5))
            self.assertEqual(five_days["text"], "5 hari lagi")
            self.assertEqual(five_days["tone"], "normal")

    def test_difficulty_info_and_checkpoint_progress_filters(self):
        expected_labels = {
            1: "Sangat Mudah",
            2: "Mudah",
            3: "Sedang",
            4: "Sulit",
            5: "Sangat Sulit",
        }
        for lvl, label in expected_labels.items():
            info = difficulty_info(lvl)
            self.assertEqual(info["level"], lvl)
            self.assertEqual(info["label"], label)

        # Clamping & invalid fallback
        self.assertEqual(difficulty_info(0)["level"], 1)
        self.assertEqual(difficulty_info(-4)["label"], "Sangat Mudah")
        self.assertEqual(difficulty_info(9)["level"], 5)
        self.assertEqual(difficulty_info(9)["label"], "Sangat Sulit")
        self.assertEqual(difficulty_info("bad")["level"], 3)

        # checkpoint_progress: None, 0-cp TODO, 0-cp DONE, partial checkpoints
        self.assertEqual(checkpoint_progress(None), {"done": 0, "total": 0, "percent": 0})

        now = timezone.now()
        todo_task = Task.objects.create(
            user=self.user, title="0-cp TODO", deadline=now + timedelta(days=2), status="TODO"
        )
        self.assertEqual(checkpoint_progress(todo_task), {"done": 0, "total": 0, "percent": 0})

        done_task = Task.objects.create(
            user=self.user, title="0-cp DONE", deadline=now + timedelta(days=2), status="DONE"
        )
        self.assertEqual(checkpoint_progress(done_task), {"done": 0, "total": 0, "percent": 100})

        partial_task = Task.objects.create(
            user=self.user, title="Partial CP", deadline=now + timedelta(days=3), status="IN_PROGRESS"
        )
        for idx, done_flag in enumerate((True, True, False), start=1):
            Checkpoint.objects.create(
                task=partial_task,
                title=f"Step {idx}",
                target_deadline=now + timedelta(days=idx),
                is_done=done_flag,
                order=idx,
            )
        self.assertEqual(checkpoint_progress(partial_task), {"done": 2, "total": 3, "percent": 67})

    def test_abs_int_format_weight_and_indonesian_label_filters(self):
        self.assertEqual(abs_int(-42), 42)
        self.assertEqual(abs_int(17), 17)
        self.assertEqual(abs_int(0), 0)
        self.assertEqual(abs_int("invalid"), 0)
        self.assertEqual(abs_int(None), 0)

        self.assertEqual(format_weight(Decimal("15.00")), "15%")
        self.assertEqual(format_weight(Decimal("12.50")), "12.5%")
        self.assertEqual(format_weight(Decimal("7.25")), "7.25%")
        self.assertEqual(format_weight(0), "0%")
        self.assertEqual(format_weight("bad"), "0%")

        self.assertEqual(reminder_escalation_label(1), "Ringan (H-3 hari)")
        self.assertEqual(reminder_escalation_label(2), "Mendesak (H-1 hari)")
        self.assertEqual(reminder_escalation_label(3), "Kritis (H-3 jam)")
        self.assertEqual(reminder_escalation_label(99), "Pengingat")

        self.assertEqual(reminder_status_label("PENDING"), "Menunggu")
        self.assertEqual(reminder_status_label("SENT"), "Terkirim")
        self.assertEqual(reminder_status_label("SNOOZED"), "Ditunda")
        self.assertEqual(reminder_status_label("DISMISSED"), "Ditutup")
        self.assertEqual(reminder_status_label("DONE"), "Selesai")
        self.assertEqual(reminder_status_label("UNKNOWN"), "UNKNOWN")

        self.assertEqual(focus_mode_label("POMODORO_25_5"), "Pomodoro (25/5)")
        self.assertEqual(focus_mode_label("DEEP_50_10"), "Deep Work (50/10)")
        self.assertEqual(focus_mode_label("CUSTOM"), "Kustom")
        self.assertEqual(focus_mode_label("OTHER"), "OTHER")

    def test_course_badge_class_and_urgency_stripe_class(self):
        self.assertEqual(
            course_badge_class(None),
            "bg-slate-100 text-slate-600 border-slate-200",
        )
        expected_course_cls = COURSE_PALETTE[self.course.pk % len(COURSE_PALETTE)]
        self.assertEqual(course_badge_class(self.course), expected_course_cls)

        unsaved_course = Course(code="CSGE9999", name="Demo")
        self.assertIn(course_badge_class(unsaved_course), COURSE_PALETTE)

        now = timezone.now()
        self.assertEqual(urgency_stripe_class(None), "border-l-indigo-500")
        self.assertEqual(
            urgency_stripe_class(Task(status="DONE", deadline=now - timedelta(days=1))),
            "border-l-emerald-500",
        )
        self.assertEqual(
            urgency_stripe_class(Task(status="ARCHIVED", deadline=now - timedelta(days=1))),
            "border-l-slate-300",
        )
        self.assertEqual(
            urgency_stripe_class(
                Task(status="TODO", deadline=now - timedelta(hours=1), priority_level="MEDIUM")
            ),
            "border-l-rose-500",
        )
        self.assertEqual(
            urgency_stripe_class(
                Task(status="TODO", deadline=now + timedelta(days=5), priority_level="CRITICAL")
            ),
            "border-l-rose-500",
        )
        self.assertEqual(
            urgency_stripe_class(
                Task(status="TODO", deadline=now + timedelta(hours=24), priority_level="MEDIUM")
            ),
            "border-l-amber-500",
        )
        self.assertEqual(
            urgency_stripe_class(
                Task(status="TODO", deadline=now + timedelta(days=10), priority_level="HIGH")
            ),
            "border-l-amber-500",
        )
        self.assertEqual(
            urgency_stripe_class(
                Task(status="TODO", deadline=now + timedelta(days=10), priority_level="MEDIUM")
            ),
            "border-l-indigo-500",
        )


# =============================================================================
# 3. Quick Add Course & Quick Done
# =============================================================================


class QuickAddCourseAndQuickDoneTests(FastTestCase):
    """Tests inline quick-add course endpoint and quick-done/reopen lifecycle."""

    @classmethod
    def setUpTestData(cls):
        cls.user_a = User.objects.create_user(
            username="quick_a",
            email="quick_a@ui.ac.id",
            password="StrongPassword123!",
        )
        cls.user_b = User.objects.create_user(
            username="quick_b",
            email="quick_b@ui.ac.id",
            password="StrongPassword123!",
        )

    def setUp(self):
        self.now = timezone.now()
        self.client.force_login(self.user_a)

    def test_quick_add_course_creates_and_deduplicates_per_user(self):
        url = reverse("tasks:quick_add_course")
        resp = self.client.post(
            url,
            data=json.dumps({"code": "  csge601020  ", "name": "Dasar Pemrograman 1"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["created"])
        self.assertEqual(payload["code"], "CSGE601020")
        self.assertEqual(payload["name"], "Dasar Pemrograman 1")
        self.assertEqual(payload["label"], "CSGE601020 - Dasar Pemrograman 1")
        self.assertEqual(Course.objects.filter(user=self.user_a, code="CSGE601020").count(), 1)

        # Duplicate for same user returns created=False without creating a second record
        dup_resp = self.client.post(
            url,
            data=json.dumps({"code": "csge601020", "name": "Nama Lain"}),
            content_type="application/json",
        )
        self.assertEqual(dup_resp.status_code, 200)
        dup_payload = dup_resp.json()
        self.assertTrue(dup_payload["ok"])
        self.assertFalse(dup_payload["created"])
        self.assertEqual(dup_payload["id"], payload["id"])
        self.assertEqual(Course.objects.filter(user=self.user_a, code="CSGE601020").count(), 1)

    def test_quick_add_course_validation_and_cross_user_isolation(self):
        url = reverse("tasks:quick_add_course")
        for bad_payload in (
            {"code": "", "name": "Valid Name"},
            {"code": "CS101", "name": "   "},
            {"code": "", "name": ""},
        ):
            resp = self.client.post(
                url,
                data=json.dumps(bad_payload),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 400)
            self.assertFalse(resp.json()["ok"])

        # User A creates CSGE602040, then User B can create the same code independently
        resp_a = self.client.post(
            url,
            data=json.dumps({"code": "csge602040", "name": "SDA User A"}),
            content_type="application/json",
        )
        self.assertEqual(resp_a.status_code, 200)
        self.assertTrue(resp_a.json()["created"])

        self.client.force_login(self.user_b)
        resp_b = self.client.post(
            url,
            data=json.dumps({"code": "CSGE602040", "name": "SDA User B"}),
            content_type="application/json",
        )
        self.assertEqual(resp_b.status_code, 200)
        payload_b = resp_b.json()
        self.assertTrue(payload_b["ok"])
        self.assertTrue(payload_b["created"])
        self.assertNotEqual(payload_b["id"], resp_a.json()["id"])
        self.assertTrue(
            Course.objects.filter(pk=payload_b["id"], user=self.user_b, code="CSGE602040").exists()
        )

    def test_task_quick_done_completes_checkpoints_cancels_reminders_and_honors_safe_next(self):
        task = Task.objects.create(
            user=self.user_a,
            title="TP Quick Done",
            deadline=self.now + timedelta(days=2),
            weight_percentage=Decimal("20.00"),
            difficulty=4,
            status="IN_PROGRESS",
            priority_score=72.0,
            priority_level="HIGH",
        )
        cp1 = Checkpoint.objects.create(
            task=task,
            title="Step 1",
            target_deadline=self.now + timedelta(days=1),
            is_done=True,
            completed_at=self.now - timedelta(hours=2),
            order=1,
        )
        cp2 = Checkpoint.objects.create(
            task=task,
            title="Step 2",
            target_deadline=self.now + timedelta(hours=36),
            is_done=False,
            order=2,
        )
        rem = Reminder.objects.create(
            user=self.user_a,
            task=task,
            checkpoint=cp2,
            scheduled_for=self.now - timedelta(minutes=15),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )

        safe_next = "/tasks/?sort=priority"
        resp = self.client.post(
            reverse("tasks:task_quick_done", kwargs={"pk": task.pk}),
            {"next": safe_next},
        )
        self.assertRedirects(resp, safe_next, fetch_redirect_response=False)

        task.refresh_from_db()
        cp1.refresh_from_db()
        cp2.refresh_from_db()
        rem.refresh_from_db()

        self.assertEqual(task.status, "DONE")
        self.assertIsNotNone(task.completed_at)
        self.assertEqual(task.priority_score, 0.0)
        self.assertEqual(task.priority_level, "LOW")
        self.assertTrue(cp1.is_done)
        self.assertTrue(cp2.is_done)
        self.assertIsNotNone(cp2.completed_at)
        self.assertEqual(rem.status, "DONE")

    def test_task_quick_done_blocks_open_redirect(self):
        task = Task.objects.create(
            user=self.user_a,
            title="Tugas Open Redirect Guard",
            deadline=self.now + timedelta(days=1),
            status="TODO",
        )
        resp = self.client.post(
            reverse("tasks:task_quick_done", kwargs={"pk": task.pk}),
            {"next": "https://evil.example.com/"},
        )
        self.assertRedirects(resp, reverse("tasks:task_list"), fetch_redirect_response=False)

    def test_task_quick_done_reopen_single_and_three_checkpoint_tasks(self):
        # Case A: Reopening a DONE task with 1 checkpoint -> unchecks it (TODO, 0%, priority > 0)
        task_one = Task.objects.create(
            user=self.user_a,
            title="Done 1 CP",
            deadline=self.now + timedelta(days=2),
            weight_percentage=Decimal("10.00"),
            difficulty=3,
            status="DONE",
            completed_at=self.now - timedelta(hours=1),
            priority_score=0.0,
            priority_level="LOW",
        )
        cp_only = Checkpoint.objects.create(
            task=task_one,
            title="Only Step",
            target_deadline=self.now + timedelta(days=1),
            is_done=True,
            completed_at=self.now - timedelta(hours=1),
            order=1,
        )

        resp1 = self.client.post(reverse("tasks:task_quick_done", kwargs={"pk": task_one.pk}))
        self.assertEqual(resp1.status_code, 302)
        task_one.refresh_from_db()
        cp_only.refresh_from_db()
        self.assertFalse(cp_only.is_done)
        self.assertEqual(task_one.status, "TODO")
        self.assertEqual(task_one.progress_percent(), 0)
        self.assertGreater(task_one.priority_score, 0.0)

        # Case B: Reopening a DONE task with 3 checkpoints -> unchecks ONLY last checkpoint
        # (IN_PROGRESS, 67%, non-zero priority — never TODO + 100%)
        task_three = Task.objects.create(
            user=self.user_a,
            title="Done 3 CP",
            deadline=self.now + timedelta(days=3),
            weight_percentage=Decimal("20.00"),
            difficulty=4,
            status="DONE",
            completed_at=self.now - timedelta(hours=1),
            priority_score=0.0,
            priority_level="LOW",
        )
        cps = [
            Checkpoint.objects.create(
                task=task_three,
                title=f"Step {i}",
                target_deadline=self.now + timedelta(hours=12 * i),
                is_done=True,
                completed_at=self.now - timedelta(hours=1),
                order=i,
            )
            for i in (1, 2, 3)
        ]

        resp3 = self.client.post(reverse("tasks:task_quick_done", kwargs={"pk": task_three.pk}))
        self.assertEqual(resp3.status_code, 302)
        task_three.refresh_from_db()
        for cp in cps:
            cp.refresh_from_db()

        self.assertTrue(cps[0].is_done)
        self.assertTrue(cps[1].is_done)
        self.assertFalse(cps[2].is_done)
        self.assertEqual(task_three.status, "IN_PROGRESS")
        self.assertEqual(task_three.progress_percent(), 67)
        self.assertGreater(task_three.priority_score, 0.0)

    def test_task_quick_done_cross_user_returns_404(self):
        task_a = Task.objects.create(
            user=self.user_a,
            title="Private Task A",
            deadline=self.now + timedelta(days=1),
            status="TODO",
        )
        self.client.force_login(self.user_b)
        resp = self.client.post(reverse("tasks:task_quick_done", kwargs={"pk": task_a.pk}))
        self.assertEqual(resp.status_code, 404)
        task_a.refresh_from_db()
        self.assertEqual(task_a.status, "TODO")


# =============================================================================
# 4. Task List Search, Sort, Urgency Quick-Filters & History Reflection KPIs
# =============================================================================


class TaskListAndHistoryTests(FastTestCase):
    """Tests task_list search/sort/urgency/stats and task_history KPIs & empty state."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="list_student",
            email="list@ui.ac.id",
            password="StrongPassword123!",
        )
        cls.course_ddp = Course.objects.create(
            user=cls.user,
            code="CSGE601020",
            name="Dasar-Dasar Pemrograman 1",
        )
        cls.course_sda = Course.objects.create(
            user=cls.user,
            code="CSGE602040",
            name="Struktur Data & Algoritma",
        )
        cls.course_empty = Course.objects.create(
            user=cls.user,
            code="CSGE603999",
            name="Mata Kuliah Tanpa Selesai",
        )

    def setUp(self):
        self.now = timezone.now()
        self.client.force_login(self.user)

    def test_task_list_search_q_matches_title_description_and_course_code(self):
        t_title = Task.objects.create(
            user=self.user,
            course=self.course_ddp,
            title="Algoritma Rekursi Python",
            description="Latihan dasar",
            deadline=self.now + timedelta(days=3),
        )
        t_desc = Task.objects.create(
            user=self.user,
            course=self.course_ddp,
            title="Tugas Mingguan",
            description="Implementasi AVL Tree lengkap dengan rotasi",
            deadline=self.now + timedelta(days=4),
        )
        t_code = Task.objects.create(
            user=self.user,
            course=self.course_sda,
            title="Kuis Graf",
            description="Bab shortest path",
            deadline=self.now + timedelta(days=5),
        )

        url = reverse("tasks:task_list")

        r_title = self.client.get(url, {"q": "Rekursi"})
        self.assertEqual([t.pk for t in r_title.context["tasks"]], [t_title.pk])

        r_desc = self.client.get(url, {"q": "AVL Tree"})
        self.assertEqual([t.pk for t in r_desc.context["tasks"]], [t_desc.pk])

        r_code = self.client.get(url, {"q": "csge602040"})
        self.assertEqual([t.pk for t in r_code.context["tasks"]], [t_code.pk])

    def test_task_list_sort_modes(self):
        t_early_low = Task.objects.create(
            user=self.user,
            title="Early Low",
            deadline=self.now + timedelta(hours=10),
            priority_score=30.0,
            status="TODO",
        )
        t_mid_high = Task.objects.create(
            user=self.user,
            title="Mid High",
            deadline=self.now + timedelta(days=2),
            priority_score=90.0,
            status="IN_PROGRESS",
        )
        t_late_done = Task.objects.create(
            user=self.user,
            title="Late Done",
            deadline=self.now + timedelta(days=5),
            priority_score=0.0,
            status="DONE",
            completed_at=self.now,
        )

        # Deterministic created_at ordering for ?sort=newest
        Task.objects.filter(pk=t_early_low.pk).update(created_at=self.now - timedelta(days=3))
        Task.objects.filter(pk=t_mid_high.pk).update(created_at=self.now - timedelta(days=1))
        Task.objects.filter(pk=t_late_done.pk).update(created_at=self.now - timedelta(hours=2))

        # Checkpoints for ?sort=progress: t_late_done=100% (DONE, 0 cp), t_mid_high=50% (1/2), t_early_low=0% (0/2)
        Checkpoint.objects.create(
            task=t_mid_high, title="M1", target_deadline=self.now + timedelta(days=1), is_done=True, order=1
        )
        Checkpoint.objects.create(
            task=t_mid_high, title="M2", target_deadline=self.now + timedelta(days=2), is_done=False, order=2
        )
        Checkpoint.objects.create(
            task=t_early_low, title="E1", target_deadline=self.now + timedelta(hours=5), is_done=False, order=1
        )

        url = reverse("tasks:task_list")

        r_deadline = self.client.get(url, {"sort": "deadline"})
        self.assertEqual(
            [t.pk for t in r_deadline.context["tasks"]],
            [t_early_low.pk, t_mid_high.pk, t_late_done.pk],
        )

        r_priority = self.client.get(url, {"sort": "priority"})
        self.assertEqual(
            [t.pk for t in r_priority.context["tasks"]],
            [t_mid_high.pk, t_early_low.pk, t_late_done.pk],
        )

        r_progress = self.client.get(url, {"sort": "progress"})
        self.assertEqual(
            [t.pk for t in r_progress.context["tasks"]],
            [t_late_done.pk, t_mid_high.pk, t_early_low.pk],
        )

        r_newest = self.client.get(url, {"sort": "newest"})
        self.assertEqual(
            [t.pk for t in r_newest.context["tasks"]],
            [t_late_done.pk, t_mid_high.pk, t_early_low.pk],
        )

    def test_task_list_urgency_filters_stats_and_has_active_filters(self):
        t_overdue = Task.objects.create(
            user=self.user,
            course=self.course_ddp,
            title="Terlambat",
            category="TP",
            deadline=self.now - timedelta(hours=4),
            status="TODO",
        )
        t_soon = Task.objects.create(
            user=self.user,
            course=self.course_ddp,
            title="Segera 24j",
            category="LAB",
            deadline=self.now + timedelta(hours=24),
            status="IN_PROGRESS",
        )
        t_future = Task.objects.create(
            user=self.user,
            course=self.course_sda,
            title="Minggu Depan",
            category="QUIZ",
            deadline=self.now + timedelta(days=7),
            status="TODO",
        )
        Task.objects.create(
            user=self.user,
            course=self.course_ddp,
            title="Sudah Selesai",
            category="TP",
            deadline=self.now - timedelta(hours=10),
            status="DONE",
            completed_at=self.now - timedelta(hours=12),
        )
        Task.objects.create(
            user=self.user,
            title="Diarsipkan",
            deadline=self.now - timedelta(days=2),
            status="ARCHIVED",
        )

        url = reverse("tasks:task_list")

        base_resp = self.client.get(url)
        self.assertFalse(base_resp.context["has_active_filters"])
        self.assertEqual(
            base_resp.context["stats"],
            {"overdue": 1, "due_soon": 1, "active": 3, "done": 1},
        )

        # Urgency filters
        r_ov = self.client.get(url, {"urgency": "overdue"})
        self.assertEqual([t.pk for t in r_ov.context["tasks"]], [t_overdue.pk])

        r_soon = self.client.get(url, {"urgency": "soon"})
        self.assertEqual([t.pk for t in r_soon.context["tasks"]], [t_soon.pk])

        r_active = self.client.get(url, {"urgency": "active"})
        self.assertEqual(
            {t.pk for t in r_active.context["tasks"]},
            {t_overdue.pk, t_soon.pk, t_future.pk},
        )

        # has_active_filters is True whenever any of q, course, status, category, urgency is set
        for params in (
            {"q": "Terlambat"},
            {"course": str(self.course_ddp.pk)},
            {"status": "TODO"},
            {"category": "LAB"},
            {"urgency": "overdue"},
        ):
            r = self.client.get(url, params)
            self.assertTrue(r.context["has_active_filters"], msg=f"Failed for {params}")

    def test_task_history_reflection_kpis_human_duration_variance(self):
        # Task 1: estimated 60m, actual 210m (diff = +150m -> "2 jam 30 mnt")
        t_tracked = Task.objects.create(
            user=self.user,
            course=self.course_ddp,
            title="TP Fokus Panjang",
            deadline=self.now - timedelta(days=1),
            estimated_minutes=60,
            status="DONE",
            completed_at=self.now - timedelta(hours=3),
        )
        FocusSession.objects.create(
            user=self.user,
            task=t_tracked,
            mode="DEEP_50_10",
            planned_minutes=120,
            actual_minutes=120,
            status="COMPLETED",
        )
        FocusSession.objects.create(
            user=self.user,
            task=t_tracked,
            mode="DEEP_50_10",
            planned_minutes=90,
            actual_minutes=90,
            status="COMPLETED",
        )
        # Abandoned session must not be counted
        FocusSession.objects.create(
            user=self.user,
            task=t_tracked,
            mode="POMODORO_25_5",
            planned_minutes=25,
            actual_minutes=20,
            status="ABANDONED",
        )

        # Task 2: completed without tracked focus minutes
        Task.objects.create(
            user=self.user,
            course=self.course_ddp,
            title="Kuis Tanpa Timer",
            deadline=self.now - timedelta(days=2),
            estimated_minutes=45,
            actual_minutes=0,
            status="DONE",
            completed_at=self.now - timedelta(days=1),
        )

        resp = self.client.get(reverse("tasks:task_history"))
        self.assertEqual(resp.status_code, 200)
        stats = resp.context["history_stats"]
        self.assertEqual(stats["total_completed"], 2)
        self.assertEqual(stats["total_focus_minutes"], 210)
        self.assertEqual(stats["tracked_count"], 1)
        self.assertEqual(stats["avg_accuracy_label"], "Rata-rata 2 jam 30 mnt lebih lama")
        self.assertContains(resp, "2 jam 30 mnt")

    def test_task_history_course_filtered_empty_state_shows_all_courses_cta(self):
        Task.objects.create(
            user=self.user,
            course=self.course_ddp,
            title="Selesai di DDP",
            deadline=self.now - timedelta(days=1),
            status="DONE",
            completed_at=self.now - timedelta(hours=5),
        )

        resp = self.client.get(
            reverse("tasks:task_history"),
            {"course": self.course_empty.pk},
        )
        self.assertEqual(resp.status_code, 200)
        stats = resp.context["history_stats"]
        self.assertEqual(stats["total_completed"], 0)
        self.assertEqual(stats["avg_accuracy_label"], "Belum ada sesi fokus tercatat")
        self.assertContains(resp, "Tampilkan Semua Mata Kuliah")


# =============================================================================
# 5. Focus Mode Deep-Link, Edit Checkpoint 400 Preservation & HTMX Toasts
# =============================================================================


class FocusDeepLinkEditCheckpointAndToastsTests(FastTestCase):
    """Tests Focus Mode checkpoint deep-linking, 400 edit preservation, and HTMX toast headers."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="focus_student",
            email="focus@ui.ac.id",
            password="StrongPassword123!",
        )
        cls.other_user = User.objects.create_user(
            username="other_focus_student",
            email="other_focus@ui.ac.id",
            password="StrongPassword123!",
        )

    def setUp(self):
        self.now = timezone.now()
        self.client.force_login(self.user)
        self.task = Task.objects.create(
            user=self.user,
            title="Tugas Mode Fokus",
            deadline=self.now + timedelta(days=3),
            status="IN_PROGRESS",
        )
        self.active_cp = Checkpoint.objects.create(
            task=self.task,
            title="Implementasi Parser",
            phase="DRAFT",
            target_deadline=self.now + timedelta(days=1),
            estimated_minutes=45,
            is_done=False,
            order=1,
        )
        self.done_cp = Checkpoint.objects.create(
            task=self.task,
            title="Baca Spesifikasi",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(hours=12),
            estimated_minutes=20,
            is_done=True,
            completed_at=self.now - timedelta(hours=1),
            order=2,
        )

    def test_focus_mode_deeplink_selects_active_checkpoint(self):
        resp = self.client.get(
            reverse("tasks:focus_mode", kwargs={"pk": self.task.pk}),
            {"checkpoint": self.active_cp.pk},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["selected_checkpoint_id"], self.active_cp.pk)
        html = resp.content.decode("utf-8")
        self.assertIn(f'<option value="{self.active_cp.pk}" selected>', html)

    def test_focus_mode_deeplink_ignores_done_other_task_other_user_and_non_int(self):
        other_task = Task.objects.create(
            user=self.user,
            title="Tugas Lain",
            deadline=self.now + timedelta(days=4),
        )
        other_task_cp = Checkpoint.objects.create(
            task=other_task,
            title="CP Tugas Lain",
            target_deadline=self.now + timedelta(days=2),
            is_done=False,
            order=1,
        )
        foreign_task = Task.objects.create(
            user=self.other_user,
            title="Tugas User Lain",
            deadline=self.now + timedelta(days=4),
        )
        foreign_cp = Checkpoint.objects.create(
            task=foreign_task,
            title="CP User Lain",
            target_deadline=self.now + timedelta(days=2),
            is_done=False,
            order=1,
        )

        url = reverse("tasks:focus_mode", kwargs={"pk": self.task.pk})
        for invalid_val in (self.done_cp.pk, other_task_cp.pk, foreign_cp.pk, "abc"):
            resp = self.client.get(url, {"checkpoint": invalid_val})
            self.assertEqual(resp.status_code, 200)
            self.assertIsNone(
                resp.context["selected_checkpoint_id"],
                msg=f"Expected None for checkpoint={invalid_val!r}",
            )

    def test_edit_checkpoint_400_preserves_typed_values_and_error_attribute(self):
        too_late_str = timezone.localtime(self.task.deadline + timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        resp = self.client.post(
            reverse("tasks:edit_checkpoint", kwargs={"pk": self.active_cp.pk}),
            {
                "title": "Judul Revisi Pengguna",
                "phase": "REVIEW",
                "target_deadline": too_late_str,
                "estimated_minutes": "75",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 400)
        html = resp.content.decode("utf-8")
        self.assertIn("data-editor-error", html)
        self.assertIn('value="Judul Revisi Pengguna"', html)
        self.assertIn(f'value="{too_late_str}"', html)
        self.assertIn('value="75"', html)
        self.assertIn('<option value="REVIEW" selected>', html)

        # Original DB row remains untouched
        self.active_cp.refresh_from_db()
        self.assertEqual(self.active_cp.title, "Implementasi Parser")
        self.assertEqual(self.active_cp.estimated_minutes, 45)

    def test_checkpoint_htmx_actions_emit_ascii_safe_toast_headers(self):
        def assert_ascii_toast(response, expected_tone: str):
            raw_header = response.get("HX-Trigger")
            self.assertIsNotNone(raw_header)
            # Ensure strictly ASCII-safe JSON header (latin-1 / HTTP header safe)
            raw_header.encode("ascii")
            data = json.loads(raw_header)
            self.assertIn("showToast", data)
            self.assertTrue(data["showToast"]["message"])
            self.assertEqual(data["showToast"]["tone"], expected_tone)

        # 1. add_checkpoint
        valid_dl = timezone.localtime(self.now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        add_resp = self.client.post(
            reverse("tasks:add_checkpoint", kwargs={"task_pk": self.task.pk}),
            {
                "title": "Uji Unit Lengkap",
                "phase": "REVIEW",
                "target_deadline": valid_dl,
                "estimated_minutes": 40,
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(add_resp.status_code, 200)
        assert_ascii_toast(add_resp, "success")

        # 2. toggle_checkpoint (mark done -> success, untoggle -> info)
        toggle_done_resp = self.client.post(
            reverse("tasks:toggle_checkpoint", kwargs={"pk": self.active_cp.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(toggle_done_resp.status_code, 200)
        assert_ascii_toast(toggle_done_resp, "success")

        toggle_undo_resp = self.client.post(
            reverse("tasks:toggle_checkpoint", kwargs={"pk": self.active_cp.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(toggle_undo_resp.status_code, 200)
        assert_ascii_toast(toggle_undo_resp, "info")

        # 3. delete_checkpoint
        del_resp = self.client.post(
            reverse("tasks:delete_checkpoint", kwargs={"pk": self.active_cp.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(del_resp.status_code, 200)
        assert_ascii_toast(del_resp, "info")


# =============================================================================
# 6. Unified Navbar Badge, Inbox OOB Sync, remindersChanged & Indonesian Auth
# =============================================================================


class NavBadgeInboxOobSyncAndAuthLocalizationTests(FastTestCase):
    """Tests nav_badges processor, poll_due_alerts count parity, inbox OOB, and Indonesian auth."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="badge_student",
            email="badge@ui.ac.id",
            password="StrongPassword123!",
        )

    def setUp(self):
        self.now = timezone.now()
        self.factory = RequestFactory()
        self.client.force_login(self.user)

    def _seed_mixed_reminders(self) -> tuple[Reminder, Reminder]:
        """Creates a mix of due/inactive/future reminders where exactly 2 are active due WEB."""
        active_task = Task.objects.create(
            user=self.user,
            title="Tugas Aktif Badge",
            deadline=self.now + timedelta(days=2),
            status="IN_PROGRESS",
        )
        active_cp = Checkpoint.objects.create(
            task=active_task,
            title="CP Aktif",
            target_deadline=self.now + timedelta(days=1),
            is_done=False,
            order=1,
        )
        done_cp = Checkpoint.objects.create(
            task=active_task,
            title="CP Sudah Selesai",
            target_deadline=self.now + timedelta(hours=12),
            is_done=True,
            completed_at=self.now - timedelta(hours=1),
            order=2,
        )
        done_task = Task.objects.create(
            user=self.user,
            title="Tugas Sudah Selesai",
            deadline=self.now + timedelta(days=1),
            status="DONE",
            completed_at=self.now - timedelta(hours=2),
        )

        # Counted (#1): due PENDING WEB reminder on active checkpoint
        due_1 = Reminder.objects.create(
            user=self.user,
            task=active_task,
            checkpoint=active_cp,
            scheduled_for=self.now - timedelta(minutes=20),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
            message="Pengingat aktif 1",
        )
        # Counted (#2): expired SNOOZED WEB reminder on active task
        due_2 = Reminder.objects.create(
            user=self.user,
            task=active_task,
            checkpoint=None,
            scheduled_for=self.now - timedelta(hours=2),
            snoozed_until=self.now - timedelta(minutes=5),
            escalation_level=3,
            channel="WEB",
            status="SNOOZED",
            message="Pengingat aktif 2",
        )
        # Excluded: due WEB reminder on DONE task
        Reminder.objects.create(
            user=self.user,
            task=done_task,
            checkpoint=None,
            scheduled_for=self.now - timedelta(minutes=15),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
            message="Harus diabaikan (task DONE)",
        )
        # Excluded: due WEB reminder on done checkpoint
        Reminder.objects.create(
            user=self.user,
            task=active_task,
            checkpoint=done_cp,
            scheduled_for=self.now - timedelta(minutes=10),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
            message="Harus diabaikan (cp done)",
        )
        # Excluded: future WEB reminder
        Reminder.objects.create(
            user=self.user,
            task=active_task,
            checkpoint=active_cp,
            scheduled_for=self.now + timedelta(hours=6),
            escalation_level=1,
            channel="WEB",
            status="PENDING",
        )
        # Excluded: TELEGRAM channel
        Reminder.objects.create(
            user=self.user,
            task=active_task,
            checkpoint=active_cp,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=2,
            channel="TELEGRAM",
            status="PENDING",
        )
        return due_1, due_2

    def test_nav_badges_anonymous_authenticated_and_htmx_zero_queries(self):
        self._seed_mixed_reminders()

        # Anonymous user -> 0 with 0 DB queries
        anon_req = self.factory.get("/tasks/")
        anon_req.user = AnonymousUser()
        with self.assertNumQueries(0):
            self.assertEqual(nav_badges(anon_req), {"nav_active_alert_count": 0})

        # Authenticated normal GET -> exact active due WEB count (2)
        auth_req = self.factory.get("/tasks/")
        auth_req.user = self.user
        self.assertEqual(nav_badges(auth_req), {"nav_active_alert_count": 2})

        # Authenticated HTMX request -> 0 with ZERO DB queries
        htmx_req = self.factory.get("/tasks/", HTTP_HX_REQUEST="true")
        htmx_req.user = self.user
        with self.assertNumQueries(0):
            self.assertEqual(nav_badges(htmx_req), {"nav_active_alert_count": 0})

    def test_poll_due_alerts_active_alert_count_matches_nav_badges(self):
        self._seed_mixed_reminders()
        auth_req = self.factory.get("/tasks/")
        auth_req.user = self.user
        expected_count = nav_badges(auth_req)["nav_active_alert_count"]
        self.assertEqual(expected_count, 2)

        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            resp = self.client.get(reverse("alerts:poll"))

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["active_alert_count"], expected_count)
        self.assertEqual(len(payload["alerts"]), expected_count)

    def test_htmx_snooze_reminder_returns_oob_nav_badge_and_triggers(self):
        due_1, _ = self._seed_mixed_reminders()

        resp = self.client.post(
            reverse("alerts:snooze", kwargs={"pk": due_1.pk}),
            {"minutes": 60},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "tasks/alerts/partials/inbox_sections.html")
        html = resp.content.decode("utf-8")
        self.assertIn('<span id="nav-alert-badge" hx-swap-oob="outerHTML"', html)

        trigger = json.loads(resp.get("HX-Trigger", "{}"))
        self.assertIn("showToast", trigger)
        self.assertTrue(trigger["showToast"]["message"])
        self.assertTrue(trigger.get("remindersChanged"))

    def test_htmx_dismiss_and_done_reminder_return_oob_nav_badge_and_triggers(self):
        due_1, due_2 = self._seed_mixed_reminders()

        dismiss_resp = self.client.post(
            reverse("alerts:dismiss", kwargs={"pk": due_2.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(dismiss_resp.status_code, 200)
        self.assertTemplateUsed(dismiss_resp, "tasks/alerts/partials/inbox_sections.html")
        self.assertIn(
            '<span id="nav-alert-badge" hx-swap-oob="outerHTML"',
            dismiss_resp.content.decode("utf-8"),
        )
        dismiss_trigger = json.loads(dismiss_resp.get("HX-Trigger", "{}"))
        self.assertIn("showToast", dismiss_trigger)
        self.assertTrue(dismiss_trigger.get("remindersChanged"))

        done_resp = self.client.post(
            reverse("alerts:done", kwargs={"pk": due_1.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(done_resp.status_code, 200)
        self.assertTemplateUsed(done_resp, "tasks/alerts/partials/inbox_sections.html")
        self.assertIn(
            '<span id="nav-alert-badge" hx-swap-oob="outerHTML"',
            done_resp.content.decode("utf-8"),
        )
        done_trigger = json.loads(done_resp.get("HX-Trigger", "{}"))
        self.assertIn("showToast", done_trigger)
        self.assertTrue(done_trigger.get("remindersChanged"))

        due_1.refresh_from_db()
        due_1.checkpoint.refresh_from_db()
        self.assertEqual(due_1.status, "DONE")
        self.assertTrue(due_1.checkpoint.is_done)

    def test_register_view_indonesian_password_mismatch_error(self):
        self.client.logout()
        resp = self.client.post(
            reverse("register"),
            {
                "username": "mahasiswa_baru",
                "password1": "RahasiaKuat123!",
                "password2": "TidakCocok456!",
            },
        )
        self.assertEqual(resp.status_code, 200)
        form = resp.context["form"]
        self.assertFalse(form.is_valid())
        self.assertIn("Konfirmasi kata sandi tidak cocok.", form.errors.get("password2", []))
        self.assertContains(resp, "Konfirmasi kata sandi tidak cocok.")

    def test_register_view_indonesian_password_validators_no_english_leaks(self):
        self.client.logout()
        cases = [
            # Short + numeric
            ("mhs_short", "12345", ["Minimal harus 8 karakter", "hanya terdiri dari angka"]),
            # Too common
            ("mhs_common", "password", ["terlalu umum"]),
            # Too similar to username
            ("budisantoso99", "budisantoso99!", ["terlalu mirip"]),
        ]
        english_phrases = (
            "too short",
            "entirely numeric",
            "too common",
            "too similar",
            "This password is",
        )
        for username, pwd, expected_snippets in cases:
            resp = self.client.post(
                reverse("register"),
                {"username": username, "password1": pwd, "password2": pwd},
            )
            self.assertEqual(resp.status_code, 200)
            form = resp.context["form"]
            self.assertFalse(form.is_valid())
            joined = " ".join(form.errors.get("password2", []))
            for snippet in expected_snippets:
                self.assertIn(snippet, joined)
            html = resp.content.decode("utf-8")
            for phrase in english_phrases:
                self.assertNotIn(phrase, html)

    def test_complete_reminder_and_unarchive_batch_task_updates_into_single_sql(self):
        due_1, _ = self._seed_mixed_reminders()
        with CaptureQueriesContext(connection) as ctx:
            complete_reminder_action(due_1.pk, user=self.user)
        task_updates = [
            q["sql"] for q in ctx.captured_queries if q["sql"].startswith('UPDATE "tasks_task"')
        ]
        self.assertEqual(len(task_updates), 1)

        archived_task = Task.objects.create(
            user=self.user,
            title="Tugas Arsip",
            deadline=self.now + timedelta(days=2),
            status="ARCHIVED",
        )
        with CaptureQueriesContext(connection) as ctx_unarchive:
            resp = self.client.post(reverse("tasks:task_archive", kwargs={"pk": archived_task.pk}))
        self.assertEqual(resp.status_code, 302)
        unarchive_updates = [
            q["sql"]
            for q in ctx_unarchive.captured_queries
            if q["sql"].startswith('UPDATE "tasks_task"')
        ]
        self.assertEqual(len(unarchive_updates), 1)
        archived_task.refresh_from_db()
        self.assertEqual(archived_task.status, "TODO")

    def test_task_list_urgency_preserved_in_filter_form_and_archived_detail_hides_quick_done(self):
        archived_task = Task.objects.create(
            user=self.user,
            title="Arsip Lama",
            deadline=self.now - timedelta(days=2),
            status="ARCHIVED",
        )
        list_resp = self.client.get(reverse("tasks:task_list"), {"urgency": "overdue"})
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, '<input type="hidden" name="urgency" value="overdue">')
        # Active KPI card toggles back to unfiltered /tasks/ with aria-current="page"
        self.assertContains(list_resp, 'aria-current="page"')
        self.assertContains(list_resp, 'ring-2 ring-rose-500')

        detail_resp = self.client.get(reverse("tasks:task_detail", kwargs={"pk": archived_task.pk}))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertNotContains(
            detail_resp,
            reverse("tasks:task_quick_done", kwargs={"pk": archived_task.pk}),
        )

    def test_htmx_inbox_action_emits_desktop_and_mobile_oob_nav_badges(self):
        due_1, _ = self._seed_mixed_reminders()
        resp = self.client.post(
            reverse("alerts:snooze", kwargs={"pk": due_1.pk}),
            {"minutes": 60},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn(
            '<span id="nav-alert-badge" hx-swap-oob="outerHTML" data-nav-alert-badge',
            html,
        )
        self.assertIn(
            '<span id="nav-alert-badge-mobile" hx-swap-oob="outerHTML" data-nav-alert-badge',
            html,
        )

