"""Tests for F4.4 — Telegram Bot Connect, Webhook Security, Commands & Inline Callbacks."""

from __future__ import annotations

import io
import json
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from tasks.models import Checkpoint, Reminder, Task, TelegramLink
from tasks.services.telegram_bot import handle_telegram_update, send_telegram_alert
from tasks.tests.base import FastTestCase

User = get_user_model()


def _mock_urlopen_response(payload: dict) -> MagicMock:
    """Builds a context-manager mock compatible with urllib.request.urlopen."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_resp
    mock_cm.__exit__.return_value = False
    return mock_cm


class TelegramIntegrationTests(FastTestCase):
    """Covers TelegramLink token flow, safe unset-token behavior, bot updates, and webhook auth."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="tg_student",
            email="tg@example.com",
            password="testpass123!",
        )
        self.now = timezone.now().replace(microsecond=0)
        self.task = Task.objects.create(
            user=self.user,
            title="TP 5 Telegram",
            deadline=self.now + timedelta(days=2),
        )
        self.checkpoint = Checkpoint.objects.create(
            task=self.task,
            title="Build webhook handler",
            phase="DRAFT",
            target_deadline=self.now + timedelta(days=1),
            order=1,
        )

    def test_telegram_connect_get_creates_and_post_regenerates_token(self) -> None:
        """GET creates TelegramLink.link_token; POST regenerates it and resets verification."""
        self.client.force_login(self.user)
        url = reverse("alerts:telegram_connect")

        resp_get = self.client.get(url)
        self.assertEqual(resp_get.status_code, 200)

        link = TelegramLink.objects.get(user=self.user)
        old_token = link.link_token
        self.assertTrue(bool(old_token))
        self.assertFalse(link.is_verified)
        self.assertContains(resp_get, old_token)

        # Simulate a verified state prior to regeneration.
        link.chat_id = 123456789
        link.is_verified = True
        link.linked_at = self.now
        link.save()

        resp_post = self.client.post(url)
        self.assertEqual(resp_post.status_code, 302)
        self.assertRedirects(resp_post, url)

        link.refresh_from_db()
        self.assertNotEqual(link.link_token, old_token)
        self.assertIsNone(link.chat_id)
        self.assertFalse(link.is_verified)
        self.assertIsNone(link.linked_at)

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "GEMINI_API_KEY": ""}, clear=False)
    def test_send_telegram_alert_returns_false_safely_when_token_unset(self) -> None:
        """send_telegram_alert returns False without raising when TELEGRAM_BOT_TOKEN is unset."""
        TelegramLink.objects.create(
            user=self.user,
            chat_id=555111222,
            link_token="tok-unset-check",
            is_verified=True,
            linked_at=self.now,
        )
        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now,
            escalation_level=2,
            channel="WEB",
            message="Reminder copy",
            status="PENDING",
        )

        with patch("tasks.services.telegram_bot.urllib.request.urlopen") as mock_urlopen:
            with self.assertLogs("tasks.services.telegram_bot", level="WARNING") as cm:
                sent = send_telegram_alert(reminder)

        self.assertIn("TELEGRAM_BOT_TOKEN", "\n".join(cm.output))
        self.assertFalse(sent)
        mock_urlopen.assert_not_called()

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN"}, clear=False)
    @patch("tasks.services.telegram_bot.urllib.request.urlopen")
    def test_send_telegram_alert_dispatches_inline_buttons_via_urlopen(
        self,
        mock_urlopen,
    ) -> None:
        """When token is set and chat is verified, send_telegram_alert posts inline Done/Snooze buttons."""
        mock_urlopen.return_value = _mock_urlopen_response({"ok": True, "result": {"message_id": 7}})
        TelegramLink.objects.create(
            user=self.user,
            chat_id=777000111,
            link_token="tok-send-ok",
            is_verified=True,
            linked_at=self.now,
        )
        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now,
            escalation_level=3,
            channel="WEB",
            message="Kerjakan sekarang!",
            status="PENDING",
        )

        sent = send_telegram_alert(reminder)
        self.assertTrue(sent)
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        sent_body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(sent_body["chat_id"], 777000111)
        keyboard = sent_body["reply_markup"]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["callback_data"], f"cb:done:{reminder.pk}")
        self.assertEqual(keyboard[0][1]["callback_data"], f"cb:snooze60:{reminder.pk}")

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_start_command_binds_chat_id_and_verifies_link(self, mock_api) -> None:
        """/start <link_token> binds chat_id, sets is_verified=True, and replies via Telegram API."""
        link = TelegramLink.objects.create(
            user=self.user,
            link_token="secret-link-token-123",
            is_verified=False,
        )

        result = handle_telegram_update(
            {
                "update_id": 1001,
                "message": {
                    "chat": {"id": 444555666},
                    "text": "/start secret-link-token-123",
                },
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "linked")
        self.assertEqual(result["chat_id"], 444555666)

        link.refresh_from_db()
        self.assertEqual(link.chat_id, 444555666)
        self.assertTrue(link.is_verified)
        self.assertIsNotNone(link.linked_at)
        mock_api.assert_called_once()
        self.assertEqual(mock_api.call_args[0][0], "sendMessage")

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_today_command_dispatches_daily_digest(self, mock_api) -> None:
        """/today for a verified user builds and sends the user's daily digest."""
        TelegramLink.objects.create(
            user=self.user,
            chat_id=333222111,
            link_token="tok-today",
            is_verified=True,
            linked_at=self.now,
        )
        Task.objects.create(
            user=self.user,
            title="Digest Task Within 48h",
            deadline=self.now + timedelta(hours=10),
        )

        result = handle_telegram_update(
            {
                "update_id": 1002,
                "message": {
                    "chat": {"id": 333222111},
                    "text": "/today",
                },
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "today")
        self.assertTrue(result["sent"])
        self.assertGreaterEqual(result["total_items"], 1)
        mock_api.assert_called_once()
        method, payload = mock_api.call_args[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["chat_id"], 333222111)
        self.assertIn("Digest Task Within 48h", payload["text"])

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_callback_done_marks_checkpoint_and_reminder_done(self, mock_api) -> None:
        """cb:done:<id> marks linked checkpoint is_done=True and reminder status='DONE'."""
        TelegramLink.objects.create(
            user=self.user,
            chat_id=888999000,
            link_token="tok-cb-done",
            is_verified=True,
            linked_at=self.now,
        )
        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )

        result = handle_telegram_update(
            {
                "update_id": 1003,
                "callback_query": {
                    "id": "cbq-done-1",
                    "data": f"cb:done:{reminder.pk}",
                    "message": {"chat": {"id": 888999000}},
                },
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "done")

        self.checkpoint.refresh_from_db()
        reminder.refresh_from_db()
        self.task.refresh_from_db()

        self.assertTrue(self.checkpoint.is_done)
        self.assertIsNotNone(self.checkpoint.completed_at)
        self.assertEqual(reminder.status, "DONE")
        self.assertEqual(self.task.status, "DONE")
        mock_api.assert_called_once()
        self.assertEqual(mock_api.call_args[0][0], "answerCallbackQuery")

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_callback_snooze60_snoozes_reminder_60_minutes(self, mock_api) -> None:
        """cb:snooze60:<id> sets status='SNOOZED', snoozed_until ~ +60m, and increments snooze_count."""
        TelegramLink.objects.create(
            user=self.user,
            chat_id=888999111,
            link_token="tok-cb-snooze",
            is_verified=True,
            linked_at=self.now,
        )
        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
            snooze_count=0,
        )

        before = timezone.now()
        result = handle_telegram_update(
            {
                "update_id": 1004,
                "callback_query": {
                    "id": "cbq-snooze-1",
                    "data": f"cb:snooze60:{reminder.pk}",
                    "message": {"chat": {"id": 888999111}},
                },
            }
        )
        after = timezone.now()

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "snooze60")
        self.assertEqual(result["snooze_count"], 1)

        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "SNOOZED")
        self.assertEqual(reminder.snooze_count, 1)
        self.assertIsNotNone(reminder.snoozed_until)
        self.assertGreaterEqual(
            reminder.snoozed_until,
            before + timedelta(minutes=60) - timedelta(seconds=2),
        )
        self.assertLessEqual(
            reminder.snoozed_until,
            after + timedelta(minutes=60) + timedelta(seconds=2),
        )
        mock_api.assert_called_once()
        self.assertEqual(mock_api.call_args[0][0], "answerCallbackQuery")

    @patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "valid-webhook-secret"}, clear=False)
    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_telegram_webhook_secret_enforcement(self, mock_api) -> None:
        """POST /alerts/telegram/webhook/<secret>/ returns 403 on wrong secret and 200 on valid secret."""
        link = TelegramLink.objects.create(
            user=self.user,
            link_token="webhook-link-tok",
            is_verified=False,
        )
        update_payload = {
            "update_id": 2001,
            "message": {
                "chat": {"id": 10102020},
                "text": "/start webhook-link-tok",
            },
        }

        wrong_url = reverse("alerts:telegram_webhook", kwargs={"secret": "wrong-secret"})
        resp_forbidden = self.client.post(
            wrong_url,
            data=json.dumps(update_payload),
            content_type="application/json",
        )
        self.assertEqual(resp_forbidden.status_code, 403)
        link.refresh_from_db()
        self.assertFalse(link.is_verified)

        valid_url = reverse("alerts:telegram_webhook", kwargs={"secret": "valid-webhook-secret"})
        resp_ok = self.client.post(
            valid_url,
            data=json.dumps(update_payload),
            content_type="application/json",
        )
        self.assertEqual(resp_ok.status_code, 200)
        body = resp_ok.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["action"], "linked")

        link.refresh_from_db()
        self.assertTrue(link.is_verified)
        self.assertEqual(link.chat_id, 10102020)
        mock_api.assert_called_once()

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}, clear=False)
    def test_run_telegram_bot_exits_cleanly_when_token_unset(self) -> None:
        """run_telegram_bot --once exits cleanly with warning when TELEGRAM_BOT_TOKEN is unset."""
        out = io.StringIO()
        call_command("run_telegram_bot", "--once", stdout=out)
        self.assertIn("TELEGRAM_BOT_TOKEN is not set", out.getvalue())
