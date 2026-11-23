"""Tests for F4.1 — Progressive 3-tier Reminder Escalation, Auto-Promotion & process_reminders."""

from __future__ import annotations

import io
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from tasks.models import Checkpoint, Reminder, Task, TelegramLink
from tasks.services.notifier import evaluate_due_reminders, sync_reminders_for_task
from tasks.tests.base import FastTestCase

User = get_user_model()


class ReminderEscalationTests(FastTestCase):
    """Covers sync_reminders_for_task, evaluate_due_reminders, and process_reminders command."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="student_esc",
            email="esc@example.com",
            password="testpass123!",
        )
        self.now = timezone.now().replace(microsecond=0)

    def test_sync_creates_three_tiers_for_future_checkpoint(self) -> None:
        """Future checkpoint (>3d out) gets L1 (H-3d), L2 (H-1d), and L3 (H-3h) WEB reminders."""
        deadline = self.now + timedelta(days=5)
        task = Task.objects.create(
            user=self.user,
            title="TP 4 OOP",
            deadline=deadline,
        )
        cp = Checkpoint.objects.create(
            task=task,
            title="Implement core classes",
            phase="DRAFT",
            target_deadline=deadline - timedelta(hours=6),
            order=1,
        )

        synced = sync_reminders_for_task(task, now=self.now)
        self.assertEqual(synced, 3)

        reminders = list(
            Reminder.objects.filter(task=task, checkpoint=cp, channel="WEB").order_by(
                "escalation_level"
            )
        )
        self.assertEqual(len(reminders), 3)
        self.assertEqual([r.escalation_level for r in reminders], [1, 2, 3])
        self.assertEqual(reminders[0].scheduled_for, cp.target_deadline - timedelta(days=3))
        self.assertEqual(reminders[1].scheduled_for, cp.target_deadline - timedelta(days=1))
        self.assertEqual(reminders[2].scheduled_for, cp.target_deadline - timedelta(hours=3))
        self.assertTrue(all(r.status == "PENDING" for r in reminders))

        # Re-running sync is idempotent and does not duplicate reminders.
        synced_again = sync_reminders_for_task(task, now=self.now)
        self.assertEqual(synced_again, 3)
        self.assertEqual(Reminder.objects.filter(task=task, checkpoint=cp).count(), 3)

    def test_sync_skips_past_l1_and_l2_while_keeping_l3(self) -> None:
        """Short-horizon checkpoint skips already-past L1/L2 tiers but still creates L3."""
        deadline = self.now + timedelta(hours=10)
        task = Task.objects.create(
            user=self.user,
            title="Lab 7 Urgent",
            deadline=deadline,
        )
        cp = Checkpoint.objects.create(
            task=task,
            title="Submit lab report",
            phase="SUBMIT",
            target_deadline=deadline,
            order=1,
        )

        synced = sync_reminders_for_task(task, now=self.now)
        self.assertEqual(synced, 1)

        reminders = list(Reminder.objects.filter(task=task, checkpoint=cp, channel="WEB"))
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].escalation_level, 3)
        self.assertEqual(reminders[0].scheduled_for, cp.target_deadline - timedelta(hours=3))

    def test_sync_keeps_l3_even_for_overdue_checkpoint(self) -> None:
        """Overdue checkpoint still gets an L3 critical reminder while omitting L1/L2."""
        deadline = self.now - timedelta(hours=2)
        task = Task.objects.create(
            user=self.user,
            title="Overdue Quiz Prep",
            deadline=deadline,
        )
        cp = Checkpoint.objects.create(
            task=task,
            title="Finish practice sheet",
            phase="REVIEW",
            target_deadline=deadline,
            order=1,
        )

        synced = sync_reminders_for_task(task, now=self.now)
        self.assertEqual(synced, 1)

        levels = list(
            Reminder.objects.filter(task=task, checkpoint=cp).values_list(
                "escalation_level", flat=True
            )
        )
        self.assertEqual(levels, [3])

    def test_marking_checkpoint_done_cancels_reminders_via_sync(self) -> None:
        """Completing a checkpoint and syncing marks its pending/snoozed reminders DONE."""
        deadline = self.now + timedelta(days=5)
        task = Task.objects.create(user=self.user, title="TP 5", deadline=deadline)
        cp = Checkpoint.objects.create(
            task=task,
            title="Research spec",
            phase="RESEARCH",
            target_deadline=deadline - timedelta(days=1),
            order=1,
        )
        sync_reminders_for_task(task, now=self.now)
        self.assertEqual(Reminder.objects.filter(checkpoint=cp, status="PENDING").count(), 3)

        cp.is_done = True
        cp.completed_at = self.now
        cp.save(update_fields=["is_done", "completed_at"])

        synced = sync_reminders_for_task(task, now=self.now)
        self.assertEqual(synced, 0)
        self.assertEqual(Reminder.objects.filter(checkpoint=cp, status="DONE").count(), 3)
        self.assertEqual(
            Reminder.objects.filter(checkpoint=cp, status__in=("PENDING", "SNOOZED")).count(),
            0,
        )

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
    def test_evaluate_due_reminders_cancels_completed_checkpoint_reminders(self) -> None:
        """evaluate_due_reminders marks due reminders DONE when linked checkpoint is done."""
        task = Task.objects.create(
            user=self.user,
            title="Lab 8",
            deadline=self.now + timedelta(hours=12),
        )
        cp = Checkpoint.objects.create(
            task=task,
            title="Write unit tests",
            phase="REVIEW",
            target_deadline=self.now + timedelta(hours=6),
            is_done=True,
            completed_at=self.now,
            order=1,
        )
        reminder = Reminder.objects.create(
            user=self.user,
            task=task,
            checkpoint=cp,
            scheduled_for=self.now - timedelta(minutes=10),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )

        active_due = evaluate_due_reminders(now=self.now)
        reminder.refresh_from_db()

        self.assertEqual(active_due, [])
        self.assertEqual(reminder.status, "DONE")

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
    def test_evaluate_promotes_imminent_and_overdue_to_level_3_with_message(self) -> None:
        """Due reminders with target deadline <=3h or overdue auto-promote to L3 and get message."""
        imminent_task = Task.objects.create(
            user=self.user,
            title="Imminent TP",
            deadline=self.now + timedelta(hours=2),
        )
        imminent_cp = Checkpoint.objects.create(
            task=imminent_task,
            title="Final commit",
            phase="SUBMIT",
            target_deadline=self.now + timedelta(hours=2),
            order=1,
        )
        imminent_rem = Reminder.objects.create(
            user=self.user,
            task=imminent_task,
            checkpoint=imminent_cp,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=1,
            channel="WEB",
            status="PENDING",
        )

        overdue_task = Task.objects.create(
            user=self.user,
            title="Overdue Lab",
            deadline=self.now - timedelta(hours=1),
        )
        overdue_rem = Reminder.objects.create(
            user=self.user,
            task=overdue_task,
            checkpoint=None,
            scheduled_for=self.now - timedelta(hours=4),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )

        active_due = evaluate_due_reminders(now=self.now)
        self.assertEqual({r.pk for r in active_due}, {imminent_rem.pk, overdue_rem.pk})

        imminent_rem.refresh_from_db()
        overdue_rem.refresh_from_db()

        self.assertEqual(imminent_rem.escalation_level, 3)
        self.assertTrue(bool(imminent_rem.message.strip()))
        self.assertEqual(overdue_rem.escalation_level, 3)
        self.assertTrue(bool(overdue_rem.message.strip()))

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
    def test_evaluate_promotes_snooze_count_ge_2_and_wakes_expired_snooze(self) -> None:
        """Expired SNOOZED reminder with snooze_count >= 2 wakes to PENDING, promotes to L3, and gets message."""
        far_deadline = self.now + timedelta(days=4)
        task = Task.objects.create(
            user=self.user,
            title="Proyek Kelompok",
            deadline=far_deadline,
        )
        cp = Checkpoint.objects.create(
            task=task,
            title="Draft ERD",
            phase="DRAFT",
            target_deadline=far_deadline - timedelta(days=1),
            order=1,
        )
        snoozed_rem = Reminder.objects.create(
            user=self.user,
            task=task,
            checkpoint=cp,
            scheduled_for=self.now - timedelta(hours=2),
            escalation_level=1,
            channel="WEB",
            status="SNOOZED",
            snoozed_until=self.now - timedelta(minutes=1),
            snooze_count=2,
        )

        active_due = evaluate_due_reminders(now=self.now)
        self.assertEqual([r.pk for r in active_due], [snoozed_rem.pk])

        snoozed_rem.refresh_from_db()
        self.assertEqual(snoozed_rem.status, "PENDING")
        self.assertIsNone(snoozed_rem.snoozed_until)
        self.assertEqual(snoozed_rem.escalation_level, 3)
        self.assertTrue(bool(snoozed_rem.message.strip()))

    @patch.dict(os.environ, {"GEMINI_API_KEY": "", "TELEGRAM_BOT_TOKEN": ""}, clear=False)
    @patch("tasks.management.commands.process_reminders.send_telegram_alert", return_value=True)
    def test_process_reminders_command_handles_due_and_skips_completed(
        self,
        mock_send_tg,
    ) -> None:
        """process_reminders runs cleanly, updates due reminders, skips done checkpoints, and dispatches verified Telegram."""
        TelegramLink.objects.create(
            user=self.user,
            chat_id=99887766,
            link_token="tok-process-cmd",
            is_verified=True,
            linked_at=self.now,
        )

        task = Task.objects.create(
            user=self.user,
            title="UAS Prep",
            deadline=self.now + timedelta(hours=2),
        )
        active_cp = Checkpoint.objects.create(
            task=task,
            title="Review Chapter 5",
            phase="REVIEW",
            target_deadline=self.now + timedelta(hours=2),
            is_done=False,
            order=1,
        )
        done_cp = Checkpoint.objects.create(
            task=task,
            title="Read slides",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(hours=1),
            is_done=True,
            completed_at=self.now,
            order=2,
        )

        due_rem = Reminder.objects.create(
            user=self.user,
            task=task,
            checkpoint=active_cp,
            scheduled_for=self.now - timedelta(minutes=15),
            escalation_level=1,
            channel="WEB",
            status="PENDING",
        )
        done_rem = Reminder.objects.create(
            user=self.user,
            task=task,
            checkpoint=done_cp,
            scheduled_for=self.now - timedelta(minutes=15),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )
        future_rem = Reminder.objects.create(
            user=self.user,
            task=task,
            checkpoint=active_cp,
            scheduled_for=self.now + timedelta(hours=5),
            escalation_level=1,
            channel="WEB",
            status="PENDING",
        )

        out = io.StringIO()
        call_command("process_reminders", stdout=out)
        output = out.getvalue()

        self.assertIn("process_reminders complete:", output)
        self.assertIn("due=1", output)
        self.assertIn("telegram_sent=1", output)

        due_rem.refresh_from_db()
        done_rem.refresh_from_db()
        future_rem.refresh_from_db()

        self.assertEqual(due_rem.escalation_level, 3)
        self.assertTrue(bool(due_rem.message.strip()))
        self.assertIsNotNone(due_rem.sent_at)
        self.assertEqual(done_rem.status, "DONE")
        self.assertEqual(future_rem.status, "PENDING")
        self.assertEqual(future_rem.message, "")
        mock_send_tg.assert_called_once()
