"""Tests for F4.2 — Alert Inbox, Browser Polling, Snooze Guards, Dismiss & Done Actions."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tasks.models import Checkpoint, Reminder, Task

User = get_user_model()


class SnoozeAndAlertsTests(TestCase):
    """Covers /alerts/, /alerts/poll/, snooze validation/guards, dismiss, done, and isolation."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice_alerts",
            email="alice@example.com",
            password="testpass123!",
        )
        self.other_user = User.objects.create_user(
            username="bob_alerts",
            email="bob@example.com",
            password="testpass123!",
        )
        self.now = timezone.now().replace(microsecond=0)
        self.task = Task.objects.create(
            user=self.user,
            title="TP 3 Struktur Data",
            deadline=self.now + timedelta(days=2),
        )
        self.checkpoint = Checkpoint.objects.create(
            task=self.task,
            title="Implement AVL Tree",
            phase="DRAFT",
            target_deadline=self.now + timedelta(days=1),
            order=1,
        )
        self.client.force_login(self.user)

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
    def test_poll_due_alerts_returns_only_own_due_web_reminders(self) -> None:
        """GET /alerts/poll/ returns only logged-in user's due PENDING and expired SNOOZED WEB alerts."""
        due_pending = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=10),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )
        expired_snoozed = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(hours=2),
            escalation_level=3,
            channel="WEB",
            status="SNOOZED",
            snoozed_until=self.now - timedelta(minutes=5),
            snooze_count=1,
            message="Preset urgent copy",
        )
        # Excluded: future PENDING
        Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now + timedelta(hours=3),
            escalation_level=1,
            channel="WEB",
            status="PENDING",
        )
        # Excluded: active SNOOZED (not yet expired)
        Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(hours=1),
            escalation_level=2,
            channel="WEB",
            status="SNOOZED",
            snoozed_until=self.now + timedelta(minutes=30),
        )
        # Excluded: non-WEB channel (TELEGRAM & DIGEST)
        Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=2,
            channel="TELEGRAM",
            status="PENDING",
        )
        Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=None,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=1,
            channel="DIGEST",
            status="PENDING",
        )
        # Excluded: another user's due WEB reminder
        other_task = Task.objects.create(
            user=self.other_user,
            title="Other User Task",
            deadline=self.now + timedelta(days=1),
        )
        Reminder.objects.create(
            user=self.other_user,
            task=other_task,
            checkpoint=None,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=3,
            channel="WEB",
            status="PENDING",
        )

        resp = self.client.get(reverse("alerts:poll"))
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        returned_ids = [item["id"] for item in payload["alerts"]]

        self.assertEqual(set(returned_ids), {due_pending.pk, expired_snoozed.pk})
        # Higher escalation_level comes first.
        self.assertEqual(returned_ids[0], expired_snoozed.pk)
        # Missing message on due_pending was populated.
        due_pending.refresh_from_db()
        self.assertTrue(bool(due_pending.message.strip()))

    def test_snooze_valid_durations_15_60_180(self) -> None:
        """POST /alerts/<pk>/snooze/ with minutes=15, 60, 180 updates status, snoozed_until, and snooze_count."""
        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=1,
            channel="WEB",
            status="PENDING",
        )
        url = reverse("alerts:snooze", kwargs={"pk": reminder.pk})

        for expected_count, minutes in enumerate((15, 60, 180), start=1):
            before = timezone.now()
            resp = self.client.post(
                url,
                data=json.dumps({"minutes": minutes}),
                content_type="application/json",
            )
            after = timezone.now()

            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["status"], "SNOOZED")
            self.assertEqual(data["snooze_count"], expected_count)

            reminder.refresh_from_db()
            self.assertEqual(reminder.status, "SNOOZED")
            self.assertEqual(reminder.snooze_count, expected_count)
            self.assertIsNotNone(reminder.snoozed_until)
            self.assertGreaterEqual(
                reminder.snoozed_until,
                before + timedelta(minutes=minutes) - timedelta(seconds=2),
            )
            self.assertLessEqual(
                reminder.snoozed_until,
                after + timedelta(minutes=minutes) + timedelta(seconds=2),
            )
            if expected_count >= 2:
                self.assertEqual(reminder.escalation_level, 3)

    def test_snooze_invalid_duration_returns_400(self) -> None:
        """Unsupported snooze duration (minutes=999) is rejected with HTTP 400."""
        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=1,
            channel="WEB",
            status="PENDING",
        )
        resp = self.client.post(
            reverse("alerts:snooze", kwargs={"pk": reminder.pk}),
            data={"minutes": 999},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "PENDING")
        self.assertEqual(reminder.snooze_count, 0)

    def test_snooze_max_count_guard_returns_400(self) -> None:
        """Reminder with snooze_count >= 3 cannot be snoozed again and returns HTTP 400."""
        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=3,
            channel="WEB",
            status="PENDING",
            snooze_count=3,
        )
        resp = self.client.post(
            reverse("alerts:snooze", kwargs={"pk": reminder.pk}),
            data={"minutes": 15},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        reminder.refresh_from_db()
        self.assertEqual(reminder.snooze_count, 3)
        self.assertEqual(reminder.status, "PENDING")

    def test_snooze_past_task_deadline_returns_400(self) -> None:
        """Snoozing beyond task.deadline is rejected with HTTP 400."""
        urgent_task = Task.objects.create(
            user=self.user,
            title="Imminent Submission",
            deadline=self.now + timedelta(minutes=30),
        )
        reminder = Reminder.objects.create(
            user=self.user,
            task=urgent_task,
            checkpoint=None,
            scheduled_for=self.now - timedelta(minutes=10),
            escalation_level=3,
            channel="WEB",
            status="PENDING",
        )
        resp = self.client.post(
            reverse("alerts:snooze", kwargs={"pk": reminder.pk}),
            data={"minutes": 60},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "PENDING")
        self.assertEqual(reminder.snooze_count, 0)

    def test_dismiss_reminder_marks_dismissed(self) -> None:
        """POST /alerts/<pk>/dismiss/ sets reminder status='DISMISSED'."""
        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=1,
            channel="WEB",
            status="PENDING",
        )
        resp = self.client.post(reverse("alerts:dismiss", kwargs={"pk": reminder.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "status": "DISMISSED"})

        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "DISMISSED")

    def test_done_reminder_marks_reminder_and_linked_checkpoint_done(self) -> None:
        """POST /alerts/<pk>/done/ marks the reminder DONE and completes its linked checkpoint."""
        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )
        sibling_reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now + timedelta(hours=5),
            escalation_level=3,
            channel="WEB",
            status="PENDING",
        )

        resp = self.client.post(reverse("alerts:done", kwargs={"pk": reminder.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "status": "DONE"})

        reminder.refresh_from_db()
        sibling_reminder.refresh_from_db()
        self.checkpoint.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(reminder.status, "DONE")
        self.assertTrue(self.checkpoint.is_done)
        self.assertIsNotNone(self.checkpoint.completed_at)
        self.assertEqual(sibling_reminder.status, "DONE")
        self.assertEqual(self.task.status, "DONE")

    def test_cross_user_isolation_returns_404(self) -> None:
        """Another user receives HTTP 404 when attempting snooze, dismiss, or done on someone else's reminder."""
        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )

        self.client.force_login(self.other_user)
        resp_snooze = self.client.post(
            reverse("alerts:snooze", kwargs={"pk": reminder.pk}),
            data={"minutes": 15},
        )
        resp_dismiss = self.client.post(reverse("alerts:dismiss", kwargs={"pk": reminder.pk}))
        resp_done = self.client.post(reverse("alerts:done", kwargs={"pk": reminder.pk}))

        self.assertEqual(resp_snooze.status_code, 404)
        self.assertEqual(resp_dismiss.status_code, 404)
        self.assertEqual(resp_done.status_code, 404)

        reminder.refresh_from_db()
        self.checkpoint.refresh_from_db()
        self.assertEqual(reminder.status, "PENDING")
        self.assertEqual(reminder.snooze_count, 0)
        self.assertFalse(self.checkpoint.is_done)

    def test_alert_inbox_renders_200_for_authenticated_user(self) -> None:
        """GET /alerts/ renders HTTP 200 with active, snoozed, and closed reminders."""
        Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=10),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
            message="Active reminder message",
        )
        Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(hours=1),
            escalation_level=1,
            channel="WEB",
            status="SNOOZED",
            snoozed_until=self.now + timedelta(hours=1),
            snooze_count=1,
        )
        Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(days=1),
            escalation_level=1,
            channel="WEB",
            status="DISMISSED",
        )

        resp = self.client.get(reverse("alerts:inbox"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Alert Inbox")
        self.assertContains(resp, "TP 3 Struktur Data")
        self.assertContains(resp, "Active reminder message")
