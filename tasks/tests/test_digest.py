"""Tests for F4.3 — Anti-Spam Daily Digest Builder, send_daily_digest Command & Web Preview."""

from __future__ import annotations

import io
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from tasks.models import Checkpoint, DailyDigestLog, Reminder, Task
from tasks.services.digest import build_daily_digest
from tasks.tests.base import FastTestCase

User = get_user_model()


class DailyDigestTests(FastTestCase):
    """Covers build_daily_digest sorting/capping, send_daily_digest uniqueness/dry-run, and web view."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="digest_student",
            email="digest@example.com",
            password="testpass123!",
        )
        self.empty_user = User.objects.create_user(
            username="empty_student",
            email="empty@example.com",
            password="testpass123!",
        )
        # Anchor at midday local time to prevent midnight boundary drift for today_checkpoints.
        self.now = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)

    def test_build_daily_digest_sorts_overdue_first_and_caps_at_five_with_overflow(self) -> None:
        """build_daily_digest orders overdue first then deadline ascending, capping items at 5."""
        # 2 overdue tasks (older overdue first)
        overdue_older = Task.objects.create(
            user=self.user,
            title="Overdue Oldest",
            deadline=self.now - timedelta(hours=10),
        )
        overdue_recent = Task.objects.create(
            user=self.user,
            title="Overdue Recent",
            deadline=self.now - timedelta(hours=2),
        )
        # 5 upcoming tasks within 48h (total = 7 active items -> cap 5, overflow 2)
        upcoming_1 = Task.objects.create(
            user=self.user,
            title="Upcoming +3h",
            deadline=self.now + timedelta(hours=3),
        )
        upcoming_2 = Task.objects.create(
            user=self.user,
            title="Upcoming +8h",
            deadline=self.now + timedelta(hours=8),
        )
        upcoming_3 = Task.objects.create(
            user=self.user,
            title="Upcoming +16h",
            deadline=self.now + timedelta(hours=16),
        )
        Task.objects.create(
            user=self.user,
            title="Upcoming +30h",
            deadline=self.now + timedelta(hours=30),
        )
        Task.objects.create(
            user=self.user,
            title="Upcoming +44h",
            deadline=self.now + timedelta(hours=44),
        )
        # Excluded tasks: DONE, ARCHIVED, and beyond 48h horizon
        Task.objects.create(
            user=self.user,
            title="Completed Task",
            deadline=self.now + timedelta(hours=1),
            status="DONE",
        )
        Task.objects.create(
            user=self.user,
            title="Archived Task",
            deadline=self.now - timedelta(hours=1),
            status="ARCHIVED",
        )
        Task.objects.create(
            user=self.user,
            title="Far Future Task",
            deadline=self.now + timedelta(days=5),
        )

        digest = build_daily_digest(self.user, slot="MORNING", now=self.now)

        self.assertEqual(digest["total_items"], 7)
        self.assertEqual(len(digest["items"]), 5)
        self.assertEqual(digest["overflow_count"], 2)

        ordered_titles = [item["title"] for item in digest["items"]]
        self.assertEqual(
            ordered_titles,
            [
                overdue_older.title,
                overdue_recent.title,
                upcoming_1.title,
                upcoming_2.title,
                upcoming_3.title,
            ],
        )
        self.assertTrue(digest["items"][0]["is_overdue"])
        self.assertTrue(digest["items"][1]["is_overdue"])
        self.assertFalse(digest["items"][2]["is_overdue"])
        self.assertIn("2 item lainnya", digest["summary_text"])

    def test_build_daily_digest_includes_due_today_checkpoints(self) -> None:
        """Incomplete checkpoints scheduled for today appear in today_checkpoints and headline items."""
        task = Task.objects.create(
            user=self.user,
            title="Long-horizon Project",
            deadline=self.now + timedelta(days=6),
        )
        cp = Checkpoint.objects.create(
            task=task,
            title="Draft system architecture",
            phase="DRAFT",
            target_deadline=self.now + timedelta(hours=3),
            is_done=False,
            order=1,
        )
        Checkpoint.objects.create(
            task=task,
            title="Already completed step",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(hours=2),
            is_done=True,
            order=2,
        )

        digest = build_daily_digest(self.user, slot="EVENING", now=self.now)
        self.assertEqual(digest["slot"], "EVENING")
        self.assertEqual(digest["total_items"], 1)
        self.assertEqual(len(digest["today_checkpoints"]), 1)
        self.assertEqual(digest["today_checkpoints"][0].pk, cp.pk)
        self.assertEqual(digest["items"][0]["kind"], "today_checkpoint")

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}, clear=False)
    def test_send_daily_digest_twice_creates_only_one_log_per_user(self) -> None:
        """Running send_daily_digest --slot morning twice on the same day creates only 1 DailyDigestLog."""
        Task.objects.create(
            user=self.user,
            title="TP 6 Recursion",
            deadline=timezone.now() + timedelta(hours=12),
        )

        out1 = io.StringIO()
        call_command("send_daily_digest", "--slot", "morning", stdout=out1)
        self.assertIn("created=1", out1.getvalue())
        self.assertEqual(
            DailyDigestLog.objects.filter(user=self.user, slot="MORNING").count(),
            1,
        )
        self.assertEqual(
            Reminder.objects.filter(user=self.user, channel="DIGEST").count(),
            1,
        )

        out2 = io.StringIO()
        call_command("send_daily_digest", "--slot", "morning", stdout=out2)
        self.assertIn("created=0", out2.getvalue())
        self.assertIn("skipped_existing=1", out2.getvalue())
        self.assertEqual(
            DailyDigestLog.objects.filter(user=self.user, slot="MORNING").count(),
            1,
        )
        self.assertEqual(
            Reminder.objects.filter(user=self.user, channel="DIGEST").count(),
            1,
        )

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}, clear=False)
    def test_user_with_zero_pending_tasks_gets_zero_digest_logs(self) -> None:
        """Users with no overdue, today-checkpoint, or 48h-upcoming tasks receive 0 DailyDigestLog rows."""
        # empty_user has only a completed task and a task far in the future.
        Task.objects.create(
            user=self.empty_user,
            title="Already Done",
            deadline=timezone.now() + timedelta(hours=4),
            status="DONE",
        )
        Task.objects.create(
            user=self.empty_user,
            title="Next Month Task",
            deadline=timezone.now() + timedelta(days=10),
        )

        out = io.StringIO()
        call_command("send_daily_digest", "--slot", "morning", stdout=out)

        self.assertEqual(DailyDigestLog.objects.filter(user=self.empty_user).count(), 0)
        self.assertEqual(
            Reminder.objects.filter(user=self.empty_user, channel="DIGEST").count(),
            0,
        )

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}, clear=False)
    def test_send_daily_digest_dry_run_creates_zero_rows(self) -> None:
        """--dry-run previews digest counts without persisting DailyDigestLog or Reminder rows."""
        Task.objects.create(
            user=self.user,
            title="Lab 9 Dry Run",
            deadline=timezone.now() + timedelta(hours=6),
        )

        out = io.StringIO()
        call_command("send_daily_digest", "--slot", "morning", "--dry-run", stdout=out)
        output = out.getvalue()

        self.assertIn("DRY-RUN", output)
        self.assertIn("would_create=1", output)
        self.assertEqual(DailyDigestLog.objects.count(), 0)
        self.assertEqual(Reminder.objects.filter(channel="DIGEST").count(), 0)

    def test_daily_digest_today_view_renders_200_with_briefing_content(self) -> None:
        """GET /alerts/digest/today/ renders HTTP 200 with overdue and upcoming briefing items."""
        Task.objects.create(
            user=self.user,
            title="Overdue Essay Brief",
            deadline=timezone.now() - timedelta(hours=3),
        )
        Task.objects.create(
            user=self.user,
            title="Upcoming Lab Brief",
            deadline=timezone.now() + timedelta(hours=10),
        )

        self.client.force_login(self.user)
        resp = self.client.get(reverse("alerts:daily_digest"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Daily Digest")
        self.assertContains(resp, "Overdue Essay Brief")
        self.assertContains(resp, "Upcoming Lab Brief")
