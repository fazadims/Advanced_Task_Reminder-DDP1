"""Comprehensive audit regression, security, edge-case, and performance tests."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.template import engines
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from brp.models import Course, GradingComponent
from tasks.forms import TaskForm
from tasks.models import (
    Checkpoint,
    DailyDigestLog,
    FocusSession,
    Reminder,
    Task,
    TelegramLink,
)
from tasks.services.ai_checkpoints import suggest_checkpoints
from tasks.services.ai_coaching import generate_coaching_message
from tasks.services.digest import _is_within_quiet_hours, build_daily_digest
from tasks.services.notifier import evaluate_due_reminders, sync_reminders_for_task
from tasks.services.telegram_bot import handle_telegram_update, send_telegram_alert

User = get_user_model()
JAKARTA_TZ = ZoneInfo("Asia/Jakarta")
FAST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def _make_fake_http_response(payload: dict) -> MagicMock:
    """Builds a context-manager mock for urllib.request.urlopen responses."""
    raw = json.dumps(payload).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = raw
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


# =============================================================================
# 1. Telegram Security & Bot Reliability Tests
# =============================================================================


@override_settings(PASSWORD_HASHERS=FAST_HASHERS)
class TelegramSecurityAndReliabilityTests(TestCase):
    """Covers callback IDOR fail-closed, /start token hardening, localhost guard, and poller resilience."""

    def setUp(self):
        self.victim = User.objects.create_user(username="tg_victim", password="pw")
        self.attacker = User.objects.create_user(username="tg_attacker", password="pw")
        self.now = timezone.now()
        self.task = Task.objects.create(
            user=self.victim,
            title="TP Security Audit",
            deadline=self.now + timedelta(days=2),
            estimated_minutes=120,
        )
        self.checkpoint = Checkpoint.objects.create(
            task=self.task,
            title="Write unit tests",
            phase="REVIEW",
            target_deadline=self.now + timedelta(hours=12),
            estimated_minutes=45,
            order=1,
        )
        self.reminder = Reminder.objects.create(
            user=self.victim,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=2,
            channel="WEB",
            message="Kerjakan checkpoint sekarang.",
            status="PENDING",
        )

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_callback_idor_rejects_victim_without_verified_link(self, mock_post):
        for action in ("done", "snooze60"):
            with self.subTest(case="no_link", action=action):
                res = handle_telegram_update(
                    {
                        "callback_query": {
                            "id": f"cb-nolink-{action}",
                            "data": f"cb:{action}:{self.reminder.pk}",
                            "from": {"id": 999111},
                            "message": {"message_id": 10, "chat": {"id": 999111}},
                        }
                    }
                )
                self.assertEqual(
                    res,
                    {"ok": False, "action": "unauthorized", "reminder_id": self.reminder.pk},
                )

        # Also fail closed when TelegramLink exists but is_verified=False.
        TelegramLink.objects.create(
            user=self.victim,
            chat_id=999111,
            link_token="pending-token-123",
            is_verified=False,
        )
        for action in ("done", "snooze60"):
            with self.subTest(case="unverified_link", action=action):
                res = handle_telegram_update(
                    {
                        "callback_query": {
                            "id": f"cb-unverified-{action}",
                            "data": f"cb:{action}:{self.reminder.pk}",
                            "from": {"id": 999111},
                            "message": {"message_id": 11, "chat": {"id": 999111}},
                        }
                    }
                )
                self.assertEqual(
                    res,
                    {"ok": False, "action": "unauthorized", "reminder_id": self.reminder.pk},
                )

        self.reminder.refresh_from_db()
        self.checkpoint.refresh_from_db()
        self.assertEqual(self.reminder.status, "PENDING")
        self.assertEqual(self.reminder.snooze_count, 0)
        self.assertFalse(self.checkpoint.is_done)
        self.assertEqual(mock_post.call_count, 4)

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_callback_idor_rejects_missing_chat_wrong_chat_mismatched_from_and_malformed(
        self, mock_post
    ):
        TelegramLink.objects.create(
            user=self.victim,
            chat_id=555001,
            link_token="verified-tok-abc",
            is_verified=True,
            linked_at=self.now,
        )

        # 1. Missing message.chat.id (both empty chat dict and omitted message) even for linked user
        for label, cb_payload in (
            (
                "empty_chat",
                {
                    "id": "cb-no-chat",
                    "data": f"cb:done:{self.reminder.pk}",
                    "from": {"id": 555001},
                    "message": {"message_id": 20, "chat": {}},
                },
            ),
            (
                "omitted_message",
                {
                    "id": "cb-no-message",
                    "data": f"cb:done:{self.reminder.pk}",
                    "from": {"id": 555001},
                },
            ),
        ):
            with self.subTest(missing_chat_case=label):
                res_missing_chat = handle_telegram_update({"callback_query": cb_payload})
                self.assertEqual(res_missing_chat["ok"], False)
                self.assertEqual(res_missing_chat["action"], "unauthorized")

        # 2. Wrong chat_id
        res_wrong_chat = handle_telegram_update(
            {
                "callback_query": {
                    "id": "cb-wrong-chat",
                    "data": f"cb:snooze60:{self.reminder.pk}",
                    "from": {"id": 888999},
                    "message": {"message_id": 21, "chat": {"id": 888999}},
                }
            }
        )
        self.assertEqual(res_wrong_chat["ok"], False)
        self.assertEqual(res_wrong_chat["action"], "unauthorized")

        # 3. Mismatched from.id while chat.id matches victim
        res_mismatched_from = handle_telegram_update(
            {
                "callback_query": {
                    "id": "cb-bad-from",
                    "data": f"cb:done:{self.reminder.pk}",
                    "from": {"id": 777666},
                    "message": {"message_id": 22, "chat": {"id": 555001}},
                }
            }
        )
        self.assertEqual(res_mismatched_from["ok"], False)
        self.assertEqual(res_mismatched_from["action"], "unauthorized")

        # 4. Malformed non-dict message/chat/from payloads must NOT raise AttributeError
        malformed_callbacks = [
            {
                "callback_query": {
                    "id": "cb-str-msg",
                    "data": f"cb:done:{self.reminder.pk}",
                    "message": "not-a-dict",
                    "from": "not-a-dict",
                }
            },
            {
                "callback_query": {
                    "id": "cb-list-chat",
                    "data": f"cb:done:{self.reminder.pk}",
                    "message": {"chat": ["not", "dict"], "message_id": "bad"},
                    "from": {"id": True},
                }
            },
        ]
        for idx, payload in enumerate(malformed_callbacks):
            with self.subTest(malformed_idx=idx):
                res = handle_telegram_update(payload)
                self.assertEqual(res["ok"], False)
                self.assertEqual(res["action"], "unauthorized")

        # Malformed message update with non-dict chat and non-dict top-level payload
        self.assertEqual(
            handle_telegram_update({"message": {"chat": "not-a-dict", "text": "/start x"}}),
            {"ok": True, "action": "ignored"},
        )
        self.assertEqual(
            handle_telegram_update(["not", "a", "dict"]),  # type: ignore[arg-type]
            {"ok": False, "action": "invalid_payload"},
        )

        # Ensure nothing was mutated across all rejected attempts.
        self.reminder.refresh_from_db()
        self.checkpoint.refresh_from_db()
        self.assertEqual(self.reminder.status, "PENDING")
        self.assertEqual(self.reminder.snooze_count, 0)
        self.assertFalse(self.checkpoint.is_done)
        self.assertTrue(mock_post.called)

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_callback_done_with_message_id_answers_and_clears_inline_keyboard(self, mock_post):
        TelegramLink.objects.create(
            user=self.victim,
            chat_id=555002,
            link_token="verified-tok-clear",
            is_verified=True,
            linked_at=self.now,
        )

        res = handle_telegram_update(
            {
                "callback_query": {
                    "id": "cb-done-777",
                    "data": f"cb:done:{self.reminder.pk}",
                    "from": {"id": 555002},
                    "message": {"message_id": 777, "chat": {"id": 555002}},
                }
            }
        )

        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "done")
        self.reminder.refresh_from_db()
        self.checkpoint.refresh_from_db()
        self.assertEqual(self.reminder.status, "DONE")
        self.assertTrue(self.checkpoint.is_done)

        methods_called = [call.args[0] for call in mock_post.call_args_list]
        self.assertEqual(methods_called, ["answerCallbackQuery", "editMessageReplyMarkup"])
        edit_payload = mock_post.call_args_list[1].args[1]
        self.assertEqual(
            edit_payload,
            {
                "chat_id": 555002,
                "message_id": 777,
                "reply_markup": {"inline_keyboard": []},
            },
        )

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_start_token_rotation_blocks_replay_and_preserves_victim_chat(self, _mock_post):
        old_token = "one-time-secret-token-001"
        link = TelegramLink.objects.create(
            user=self.victim,
            link_token=old_token,
            token_created_at=self.now,
            is_verified=False,
        )

        # Victim links account legitimately in a private chat.
        first_res = handle_telegram_update(
            {
                "message": {
                    "chat": {"id": 600001, "type": "private"},
                    "text": f"/start {old_token}",
                }
            }
        )
        self.assertTrue(first_res["ok"])
        self.assertEqual(first_res["action"], "linked")

        link.refresh_from_db()
        self.assertTrue(link.is_verified)
        self.assertEqual(link.chat_id, 600001)
        self.assertNotEqual(link.link_token, old_token)

        # Attacker replays the consumed token from another private chat.
        replay_res = handle_telegram_update(
            {
                "message": {
                    "chat": {"id": 999999, "type": "private"},
                    "text": f"/start {old_token}",
                }
            }
        )
        self.assertFalse(replay_res["ok"])
        self.assertEqual(replay_res["action"], "start_invalid_token")

        link.refresh_from_db()
        self.assertEqual(link.chat_id, 600001)
        self.assertTrue(link.is_verified)

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_start_rejects_expired_token(self, _mock_post):
        expired_token = "expired-token-60m"
        link = TelegramLink.objects.create(
            user=self.victim,
            link_token=expired_token,
            token_created_at=self.now - timedelta(minutes=60),
            is_verified=False,
        )

        res = handle_telegram_update(
            {
                "message": {
                    "chat": {"id": 600002, "type": "private"},
                    "text": f"/start {expired_token}",
                }
            }
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["action"], "start_expired_token")

        link.refresh_from_db()
        self.assertFalse(link.is_verified)
        self.assertIsNone(link.chat_id)

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_start_rejects_group_and_negative_chat_ids(self, _mock_post):
        token = "private-only-token-xyz"
        link = TelegramLink.objects.create(
            user=self.victim,
            link_token=token,
            token_created_at=self.now,
            is_verified=False,
        )

        for label, chat_payload in (
            ("group_type", {"id": 600003, "type": "group"}),
            ("negative_chat_id", {"id": -100123456}),
        ):
            with self.subTest(case=label):
                res = handle_telegram_update(
                    {
                        "message": {
                            "chat": chat_payload,
                            "text": f"/start {token}",
                        }
                    }
                )
                self.assertFalse(res["ok"])
                self.assertEqual(res["action"], "start_non_private_chat")

        link.refresh_from_db()
        self.assertFalse(link.is_verified)
        self.assertIsNone(link.chat_id)

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_send_telegram_alert_localhost_vs_public_url_button_guard(self, mock_post):
        expected_local_url = f"http://127.0.0.1:8000/tasks/{self.task.pk}/focus/"
        with patch.dict(
            os.environ,
            {"APP_BASE_URL": "http://127.0.0.1:8000", "GEMINI_API_KEY": ""},
            clear=False,
        ):
            ok_local = send_telegram_alert(self.reminder, chat_id=555010)
        self.assertTrue(ok_local)
        local_payload = mock_post.call_args.args[1]
        local_kb = local_payload["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(local_kb), 1)
        self.assertEqual(len(local_kb[0]), 2)
        self.assertTrue(all("url" not in btn for row in local_kb for btn in row))
        self.assertIn(expected_local_url, local_payload["text"])

        mock_post.reset_mock()
        expected_public_url = f"https://atr.cs.ui.ac.id/tasks/{self.task.pk}/focus/"
        with patch.dict(
            os.environ,
            {"APP_BASE_URL": "https://atr.cs.ui.ac.id", "GEMINI_API_KEY": ""},
            clear=False,
        ):
            ok_public = send_telegram_alert(self.reminder, chat_id=555010)
        self.assertTrue(ok_public)
        public_payload = mock_post.call_args.args[1]
        public_kb = public_payload["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(public_kb), 2)
        self.assertEqual(
            public_kb[1],
            [{"text": "🎯 Focus Mode", "url": expected_public_url}],
        )

    def test_run_telegram_bot_once_acks_final_offset(self):
        out = StringIO()
        err = StringIO()
        batch_response = {
            "ok": True,
            "result": [
                {"update_id": 501, "message": {"chat": {"id": 1, "type": "private"}, "text": "hi"}},
                {"update_id": 502, "message": {"chat": {"id": 1, "type": "private"}, "text": "yo"}},
            ],
        }
        ack_response = {"ok": True, "result": []}

        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-bot-token"}, clear=False),
            patch(
                "tasks.management.commands.run_telegram_bot._telegram_api_post",
                side_effect=[batch_response, ack_response],
            ) as mock_api,
            patch(
                "tasks.management.commands.run_telegram_bot.handle_telegram_update",
                return_value={"ok": True, "action": "ignored"},
            ) as mock_handle,
        ):
            call_command("run_telegram_bot", "--once", stdout=out, stderr=err)

        self.assertEqual(mock_handle.call_count, 2)
        self.assertEqual(mock_api.call_count, 2)
        ack_call = mock_api.call_args_list[1]
        self.assertEqual(ack_call.args[0], "getUpdates")
        self.assertEqual(ack_call.args[1]["offset"], 503)
        self.assertIn("processed=2", out.getvalue())

    def test_run_telegram_bot_once_survives_poison_update_and_processes_next(self):
        out = StringIO()
        err = StringIO()
        batch_response = {
            "ok": True,
            "result": [
                {"update_id": 900, "message": {"chat": {"id": 1}, "text": "poison"}},
                {"update_id": 901, "message": {"chat": {"id": 1}, "text": "healthy"}},
            ],
        }
        ack_response = {"ok": True, "result": []}

        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-bot-token"}, clear=False),
            patch(
                "tasks.management.commands.run_telegram_bot._telegram_api_post",
                side_effect=[batch_response, ack_response],
            ) as mock_api,
            patch(
                "tasks.management.commands.run_telegram_bot.handle_telegram_update",
                side_effect=[RuntimeError("simulated poison failure"), {"ok": True, "action": "ignored"}],
            ) as mock_handle,
            self.assertLogs("tasks.management.commands.run_telegram_bot", level="ERROR") as cm,
        ):
            call_command("run_telegram_bot", "--once", stdout=out, stderr=err)

        self.assertEqual(mock_handle.call_count, 2)
        self.assertIn("processed=1", out.getvalue())
        self.assertIn("Error handling Telegram update #900", err.getvalue())
        self.assertTrue(
            any("Unhandled error while processing Telegram update #900" in line for line in cm.output)
        )
        # Final ack offset advances past the healthy update as well.
        self.assertEqual(mock_api.call_args_list[1].args[1]["offset"], 902)


# =============================================================================
# 2. Forms, Models, Views Security & State Integrity Tests
# =============================================================================


@override_settings(PASSWORD_HASHERS=FAST_HASHERS)
class FormsModelsViewsIntegrityTests(TestCase):
    """Covers form scoping, task state transitions, per-user processing isolation, and malformed inputs."""

    def setUp(self):
        self.user_a = User.objects.create_user(username="sec_user_a", password="pw")
        self.user_b = User.objects.create_user(username="sec_user_b", password="pw")
        self.now = timezone.now()

        self.course_a1 = Course.objects.create(
            user=self.user_a, code="CSGE601020", name="DDP 1"
        )
        self.course_a2 = Course.objects.create(
            user=self.user_a, code="CSGE602040", name="SDA"
        )
        self.course_b = Course.objects.create(
            user=self.user_b, code="CSGE602055", name="PBP"
        )
        self.comp_a1 = GradingComponent.objects.create(
            course=self.course_a1,
            name="Tugas Pemrograman",
            category="TUGAS",
            weight_percentage=Decimal("25.00"),
        )

        self.client_a = Client()
        self.client_a.force_login(self.user_a)

    def test_task_form_course_scoping_and_edit_omits_auto_generate(self):
        # TaskForm() with user=None exposes zero courses.
        unbound_no_user = TaskForm()
        self.assertEqual(unbound_no_user.fields["course"].queryset.count(), 0)

        # Submitting other_user's Course.pk in TaskForm(data, user=user_a) is invalid.
        dl_str = timezone.localtime(self.now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        form_cross = TaskForm(
            data={
                "title": "Cross-course injection",
                "course": str(self.course_b.pk),
                "category": "TP",
                "deadline": dl_str,
                "estimated_minutes": "60",
                "weight_percentage": "10.00",
                "difficulty": "3",
                "status": "TODO",
            },
            user=self.user_a,
        )
        self.assertFalse(form_cross.is_valid())
        self.assertIn("course", form_cross.errors)

        # Edit form omits auto_generate_checkpoints while create form keeps it.
        create_form = TaskForm(user=self.user_a)
        self.assertIn("auto_generate_checkpoints", create_form.fields)

        task = Task.objects.create(
            user=self.user_a,
            course=self.course_a1,
            title="Existing task",
            deadline=self.now + timedelta(days=2),
        )
        edit_form = TaskForm(instance=task, user=self.user_a)
        self.assertNotIn("auto_generate_checkpoints", edit_form.fields)

    def test_task_update_changing_course_clears_stale_mismatched_component(self):
        task = Task.objects.create(
            user=self.user_a,
            course=self.course_a1,
            component=self.comp_a1,
            title="Task with component",
            deadline=self.now + timedelta(days=4),
        )
        dl_str = timezone.localtime(self.now + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M")

        # Switching to course_a2 clears component cleanly instead of failing Task.clean().
        resp = self.client_a.post(
            reverse("tasks:task_update", kwargs={"pk": task.pk}),
            data={
                "title": "Task switched course",
                "course": str(self.course_a2.pk),
                "category": "TP",
                "deadline": dl_str,
                "estimated_minutes": "120",
                "weight_percentage": "20.00",
                "difficulty": "3",
                "status": "TODO",
            },
        )
        self.assertEqual(resp.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.course_id, self.course_a2.pk)
        self.assertIsNone(task.component_id)
        task.full_clean()

        # Also verify clearing course to "" clears stale component cleanly.
        task.course = self.course_a1
        task.component = self.comp_a1
        task.save(update_fields=["course", "component"])
        resp_clear = self.client_a.post(
            reverse("tasks:task_update", kwargs={"pk": task.pk}),
            data={
                "title": "Task without course",
                "course": "",
                "category": "TP",
                "deadline": dl_str,
                "estimated_minutes": "120",
                "weight_percentage": "20.00",
                "difficulty": "3",
                "status": "TODO",
            },
        )
        self.assertEqual(resp_clear.status_code, 302)
        task.refresh_from_db()
        self.assertIsNone(task.course_id)
        self.assertIsNone(task.component_id)
        task.full_clean()

        # Verify model clean() itself still rejects mismatched component if set directly.
        task.course = self.course_a2
        task.component = self.comp_a1
        with self.assertRaises(ValidationError) as ctx:
            task.full_clean()
        self.assertIn("component", ctx.exception.message_dict)

    def test_task_create_done_sets_completed_at_and_skips_auto_checkpoints(self):
        dl_str = timezone.localtime(self.now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        resp = self.client_a.post(
            reverse("tasks:task_create"),
            data={
                "title": "Already Finished Lab",
                "course": str(self.course_a1.pk),
                "category": "LAB",
                "deadline": dl_str,
                "estimated_minutes": "90",
                "weight_percentage": "5.00",
                "difficulty": "2",
                "status": "DONE",
                "auto_generate_checkpoints": "on",
            },
        )
        self.assertEqual(resp.status_code, 302)
        created = Task.objects.get(user=self.user_a, title="Already Finished Lab")
        self.assertEqual(created.status, "DONE")
        self.assertIsNotNone(created.completed_at)
        self.assertEqual(created.checkpoints.count(), 0)

    def test_sync_status_preserves_archived_and_unarchive_restores_from_checkpoints(self):
        task = Task.objects.create(
            user=self.user_a,
            title="Archivable Task",
            deadline=self.now + timedelta(days=3),
            status="ARCHIVED",
        )
        cp1 = Checkpoint.objects.create(
            task=task,
            title="Step 1",
            target_deadline=self.now + timedelta(days=1),
            is_done=True,
            completed_at=self.now,
            order=1,
        )
        cp2 = Checkpoint.objects.create(
            task=task,
            title="Step 2",
            target_deadline=self.now + timedelta(days=2),
            is_done=False,
            order=2,
        )

        # Partially complete checkpoints: ARCHIVED is preserved.
        self.assertEqual(task.sync_status_from_checkpoints(), "ARCHIVED")
        task.refresh_from_db()
        self.assertEqual(task.status, "ARCHIVED")

        # All complete checkpoints: ARCHIVED is STILL preserved.
        cp2.is_done = True
        cp2.completed_at = self.now
        cp2.save(update_fields=["is_done", "completed_at"])
        self.assertEqual(task.sync_status_from_checkpoints(), "ARCHIVED")
        task.refresh_from_db()
        self.assertEqual(task.status, "ARCHIVED")

        # Unarchiving via task_archive restores status from checkpoints -> DONE.
        resp_unarchive_done = self.client_a.post(
            reverse("tasks:task_archive", kwargs={"pk": task.pk})
        )
        self.assertEqual(resp_unarchive_done.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, "DONE")
        self.assertIsNotNone(task.completed_at)

        # Archive again, uncheck cp2, unarchive -> IN_PROGRESS.
        self.client_a.post(reverse("tasks:task_archive", kwargs={"pk": task.pk}))
        cp2.is_done = False
        cp2.completed_at = None
        cp2.save(update_fields=["is_done", "completed_at"])
        self.client_a.post(reverse("tasks:task_archive", kwargs={"pk": task.pk}))
        task.refresh_from_db()
        self.assertEqual(task.status, "IN_PROGRESS")
        self.assertIsNone(task.completed_at)
        self.assertTrue(cp1.is_done)

    def test_alerts_process_now_scopes_strictly_to_request_user(self):
        task_a = Task.objects.create(
            user=self.user_a,
            title="User A Urgent Task",
            deadline=self.now + timedelta(hours=2),
        )
        rem_a = Reminder.objects.create(
            user=self.user_a,
            task=task_a,
            scheduled_for=self.now - timedelta(minutes=10),
            escalation_level=1,
            channel="WEB",
            message="",
            status="PENDING",
        )

        task_b = Task.objects.create(
            user=self.user_b,
            title="User B Urgent Task",
            deadline=self.now + timedelta(hours=2),
        )
        rem_b_pending = Reminder.objects.create(
            user=self.user_b,
            task=task_b,
            scheduled_for=self.now - timedelta(minutes=10),
            escalation_level=1,
            channel="WEB",
            message="",
            status="PENDING",
        )
        expired_snooze = self.now - timedelta(minutes=5)
        rem_b_snoozed = Reminder.objects.create(
            user=self.user_b,
            task=task_b,
            scheduled_for=self.now - timedelta(hours=1),
            escalation_level=1,
            channel="WEB",
            message="",
            status="SNOOZED",
            snoozed_until=expired_snooze,
            snooze_count=2,
        )

        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            resp = self.client_a.post(reverse("alerts:process_now"))

        self.assertEqual(resp.status_code, 302)
        rem_a.refresh_from_db()
        self.assertEqual(rem_a.escalation_level, 3)
        self.assertTrue(bool(rem_a.message))

        # User B's due reminders must NOT be promoted, woken, or have messages populated.
        rem_b_pending.refresh_from_db()
        rem_b_snoozed.refresh_from_db()
        self.assertEqual(rem_b_pending.status, "PENDING")
        self.assertEqual(rem_b_pending.escalation_level, 1)
        self.assertEqual(rem_b_pending.message, "")
        self.assertEqual(rem_b_snoozed.status, "SNOOZED")
        self.assertEqual(rem_b_snoozed.snoozed_until, expired_snooze)
        self.assertEqual(rem_b_snoozed.escalation_level, 1)
        self.assertEqual(rem_b_snoozed.message, "")

    def test_malformed_query_and_focus_json_inputs_never_500(self):
        task = Task.objects.create(
            user=self.user_a,
            course=self.course_a1,
            title="Robust Input Task",
            deadline=self.now + timedelta(days=2),
        )

        for query in (
            "?course=abc",
            "?course=\u00b2",  # Unicode superscript '²' where str.isdigit() is True!
            "?status=BOGUS",
            "?category=BOGUS",
            "?course=\u00b2&status=BOGUS&category=BOGUS",
        ):
            with self.subTest(task_list_query=query):
                resp = self.client_a.get(reverse("tasks:task_list") + query)
                self.assertEqual(resp.status_code, 200)

        hist_resp = self.client_a.get(reverse("tasks:task_history") + "?course=\u00b2&page=abc")
        self.assertEqual(hist_resp.status_code, 200)

        start_url = reverse("tasks:start_focus_session")
        bad_payloads = [
            {"task_id": "abc"},
            {},
            {"task_id": "\u00b2"},
            {"task_id": task.pk, "checkpoint_id": "abc"},
        ]
        for payload in bad_payloads:
            with self.subTest(focus_payload=payload):
                resp = self.client_a.post(
                    start_url,
                    data=json.dumps(payload),
                    content_type="application/json",
                )
                self.assertEqual(resp.status_code, 400)
                body = resp.json()
                self.assertFalse(body["ok"])

    def test_finish_focus_session_double_finish_returns_409_and_preserves_state(self):
        task = Task.objects.create(
            user=self.user_a,
            title="Double Finish Focus Task",
            deadline=self.now + timedelta(days=2),
        )
        session = FocusSession.objects.create(
            user=self.user_a,
            task=task,
            mode="POMODORO_25_5",
            planned_minutes=25,
            status="RUNNING",
        )
        finish_url = reverse("tasks:finish_focus_session", kwargs={"session_id": session.pk})

        first = self.client_a.post(
            finish_url,
            data=json.dumps({"status": "COMPLETED", "elapsed_seconds": 900}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "COMPLETED")
        self.assertEqual(first.json()["actual_minutes"], 15)

        second = self.client_a.post(
            finish_url,
            data=json.dumps({"status": "ABANDONED", "elapsed_seconds": 60}),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 409)
        body = second.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "COMPLETED")

        session.refresh_from_db()
        self.assertEqual(session.status, "COMPLETED")
        self.assertEqual(session.actual_minutes, 15)


# =============================================================================
# 3. Checkpoints & Alerts HTMX / Domain Consistency Tests
# =============================================================================


@override_settings(PASSWORD_HASHERS=FAST_HASHERS)
class CheckpointsAndAlertsConsistencyTests(TestCase):
    """Covers HTMX checkpoint CRUD/OOB responses, snooze guards, anti-snooze regeneration, and checkpointless done."""

    def setUp(self):
        self.user = User.objects.create_user(username="htmx_owner", password="pw")
        self.other = User.objects.create_user(username="htmx_other", password="pw")
        self.now = timezone.now()

        self.task = Task.objects.create(
            user=self.user,
            title="HTMX Consistency Task",
            deadline=self.now + timedelta(days=5),
            status="IN_PROGRESS",
        )
        self.cp1 = Checkpoint.objects.create(
            task=self.task,
            title="Checkpoint One",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(days=2),
            estimated_minutes=30,
            order=1,
        )
        self.cp2 = Checkpoint.objects.create(
            task=self.task,
            title="Checkpoint Two",
            phase="DRAFT",
            target_deadline=self.now + timedelta(days=4),
            estimated_minutes=60,
            order=2,
        )

        self.client_owner = Client()
        self.client_owner.force_login(self.user)
        self.client_other = Client()
        self.client_other.force_login(self.other)

    def test_edit_checkpoint_htmx_valid_invalid_and_cross_user(self):
        edit_url = reverse("tasks:edit_checkpoint", kwargs={"pk": self.cp1.pk})
        valid_dl = timezone.localtime(self.now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")

        resp_ok = self.client_owner.post(
            edit_url,
            data={
                "title": "Checkpoint One Updated",
                "phase": "REVIEW",
                "target_deadline": valid_dl,
                "estimated_minutes": "45",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_ok.status_code, 200)
        html_ok = resp_ok.content.decode("utf-8")
        self.assertIn("Checkpoint One Updated", html_ok)
        self.assertIn('id="task-progress-bar" hx-swap-oob="true"', html_ok)
        self.assertIn('id="upcoming-reminders" hx-swap-oob="true"', html_ok)

        self.cp1.refresh_from_db()
        self.assertEqual(self.cp1.title, "Checkpoint One Updated")
        self.assertEqual(self.cp1.phase, "REVIEW")
        self.assertEqual(self.cp1.estimated_minutes, 45)

        # Invalid deadline exceeding task.deadline returns HTTP 400 with edit_error.
        too_late = timezone.localtime(self.task.deadline + timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        resp_bad = self.client_owner.post(
            edit_url,
            data={
                "title": "Should Not Save",
                "phase": "REVIEW",
                "target_deadline": too_late,
                "estimated_minutes": "45",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_bad.status_code, 400)
        self.assertIn(
            "Target deadline checkpoint tidak boleh melewati deadline tugas.",
            resp_bad.content.decode("utf-8"),
        )
        self.cp1.refresh_from_db()
        self.assertEqual(self.cp1.title, "Checkpoint One Updated")

        # Cross-user edit returns 404.
        resp_cross = self.client_other.post(
            edit_url,
            data={
                "title": "Hijacked",
                "phase": "CUSTOM",
                "target_deadline": valid_dl,
                "estimated_minutes": "30",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_cross.status_code, 404)
        self.cp1.refresh_from_db()
        self.assertEqual(self.cp1.title, "Checkpoint One Updated")

    def test_delete_checkpoint_non_last_last_retarget_and_cross_user(self):
        # Cross-user delete returns 404 and keeps checkpoint intact.
        resp_cross = self.client_other.post(
            reverse("tasks:delete_checkpoint", kwargs={"pk": self.cp1.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_cross.status_code, 404)
        self.assertTrue(Checkpoint.objects.filter(pk=self.cp1.pk).exists())

        # Deleting non-last checkpoint returns OOB #task-progress-bar and #upcoming-reminders.
        resp_non_last = self.client_owner.post(
            reverse("tasks:delete_checkpoint", kwargs={"pk": self.cp1.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_non_last.status_code, 200)
        self.assertNotIn("HX-Retarget", resp_non_last)
        html_non_last = resp_non_last.content.decode("utf-8")
        self.assertIn('id="task-progress-bar" hx-swap-oob="true"', html_non_last)
        self.assertIn('id="upcoming-reminders" hx-swap-oob="true"', html_non_last)

        # Ensure task is IN_PROGRESS before deleting the final remaining checkpoint.
        self.task.status = "IN_PROGRESS"
        self.task.save(update_fields=["status"])

        resp_last = self.client_owner.post(
            reverse("tasks:delete_checkpoint", kwargs={"pk": self.cp2.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_last.status_code, 200)
        self.assertEqual(resp_last["HX-Retarget"], "#checkpoint-list")
        self.assertEqual(resp_last["HX-Reswap"], "outerHTML")
        html_last = resp_last.content.decode("utf-8")
        self.assertIn("No checkpoints yet", html_last)
        self.assertIn('id="task-progress-bar" hx-swap-oob="true"', html_last)
        self.assertIn('id="upcoming-reminders" hx-swap-oob="true"', html_last)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "TODO")
        self.assertEqual(self.task.checkpoints.count(), 0)

    def test_add_checkpoint_validation_returns_400_with_htmx_retarget(self):
        add_url = reverse("tasks:add_checkpoint", kwargs={"task_pk": self.task.pk})

        # Blank title
        resp_blank = self.client_owner.post(
            add_url,
            data={"title": "   ", "phase": "CUSTOM", "estimated_minutes": "30"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_blank.status_code, 400)
        self.assertEqual(resp_blank["HX-Retarget"], "#checkpoint-form-error")
        self.assertEqual(resp_blank["HX-Reswap"], "innerHTML")

        # Deadline after task.deadline
        too_late = timezone.localtime(self.task.deadline + timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        resp_late = self.client_owner.post(
            add_url,
            data={
                "title": "Impossible milestone",
                "phase": "SUBMIT",
                "target_deadline": too_late,
                "estimated_minutes": "30",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_late.status_code, 400)
        self.assertEqual(resp_late["HX-Retarget"], "#checkpoint-form-error")
        self.assertEqual(resp_late["HX-Reswap"], "innerHTML")
        self.assertEqual(self.task.checkpoints.count(), 2)

    def test_snooze_htmx_400_headers_for_max_snooze_and_close_checkpoint_deadline(self):
        # Case A: max snooze count already reached (3x)
        rem_max = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.cp2,
            scheduled_for=self.now - timedelta(minutes=10),
            escalation_level=3,
            status="PENDING",
            snooze_count=3,
        )
        resp_max = self.client_owner.post(
            reverse("alerts:snooze", kwargs={"pk": rem_max.pk}),
            data={"minutes": "15"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_max.status_code, 400)
        self.assertEqual(resp_max["HX-Retarget"], f"#reminder-error-{rem_max.pk}")
        self.assertEqual(resp_max["HX-Reswap"], "innerHTML")

        # Case B: task.deadline is 5 days away, but checkpoint.target_deadline is only 30 min away!
        soon_cp = Checkpoint.objects.create(
            task=self.task,
            title="Imminent Milestone",
            phase="DRAFT",
            target_deadline=self.now + timedelta(minutes=30),
            estimated_minutes=20,
            order=3,
        )
        rem_close = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=soon_cp,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=3,
            status="PENDING",
            snooze_count=0,
        )
        resp_close = self.client_owner.post(
            reverse("alerts:snooze", kwargs={"pk": rem_close.pk}),
            data={"minutes": "60"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_close.status_code, 400)
        self.assertEqual(resp_close["HX-Retarget"], f"#reminder-error-{rem_close.pk}")
        self.assertEqual(resp_close["HX-Reswap"], "innerHTML")
        rem_close.refresh_from_db()
        self.assertEqual(rem_close.status, "PENDING")
        self.assertEqual(rem_close.snooze_count, 0)

    @patch("tasks.services.telegram_bot._telegram_api_post", return_value={"ok": True})
    def test_snooze_rejects_done_and_dismissed_reminders_web_and_telegram(self, _mock_post):
        TelegramLink.objects.create(
            user=self.user,
            chat_id=710001,
            link_token="tok-closed-snooze",
            is_verified=True,
            linked_at=self.now,
        )

        for closed_status in ("DONE", "DISMISSED"):
            rem = Reminder.objects.create(
                user=self.user,
                task=self.task,
                checkpoint=self.cp2,
                scheduled_for=self.now - timedelta(hours=1),
                escalation_level=2,
                status=closed_status,
                snooze_count=0,
            )
            with self.subTest(channel="web", status=closed_status):
                web_resp = self.client_owner.post(
                    reverse("alerts:snooze", kwargs={"pk": rem.pk}),
                    data={"minutes": "60"},
                )
                self.assertEqual(web_resp.status_code, 400)
                self.assertFalse(web_resp.json()["ok"])

            with self.subTest(channel="telegram", status=closed_status):
                tg_res = handle_telegram_update(
                    {
                        "callback_query": {
                            "id": f"cb-closed-{closed_status}",
                            "data": f"cb:snooze60:{rem.pk}",
                            "from": {"id": 710001},
                            "message": {"message_id": 50, "chat": {"id": 710001}},
                        }
                    }
                )
                self.assertFalse(tg_res["ok"])
                self.assertEqual(tg_res["action"], "snooze_rejected")
                self.assertEqual(tg_res["reason"], "already_closed")

            rem.refresh_from_db()
            self.assertEqual(rem.status, closed_status)
            self.assertEqual(rem.snooze_count, 0)

    def test_second_web_snooze_promotes_level3_clears_message_and_regenerates_anti_snooze_copy(self):
        rem = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.cp2,
            scheduled_for=self.now - timedelta(minutes=10),
            escalation_level=1,
            channel="WEB",
            message="Old cached gentle message",
            status="PENDING",
            snooze_count=1,
        )

        resp = self.client_owner.post(
            reverse("alerts:snooze", kwargs={"pk": rem.pk}),
            data={"minutes": "15"},
        )
        self.assertEqual(resp.status_code, 200)
        rem.refresh_from_db()
        self.assertEqual(rem.status, "SNOOZED")
        self.assertEqual(rem.snooze_count, 2)
        self.assertEqual(rem.escalation_level, 3)
        self.assertEqual(rem.message, "")

        # Once snooze expires, evaluate_due_reminders regenerates anti-snooze coaching copy.
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            due = evaluate_due_reminders(
                now=rem.snoozed_until + timedelta(minutes=1),
                user=self.user,
            )

        self.assertEqual([r.pk for r in due], [rem.pk])
        rem.refresh_from_db()
        self.assertEqual(rem.status, "PENDING")
        self.assertEqual(rem.escalation_level, 3)
        self.assertNotEqual(rem.message, "Old cached gentle message")
        self.assertIn("snooze", rem.message.lower())

    def test_alerts_done_on_checkpointless_task_marks_task_done_and_cancels_siblings(self):
        task_no_cp = Task.objects.create(
            user=self.user,
            title="Standalone Essay",
            deadline=self.now + timedelta(days=2),
            status="TODO",
        )
        rem_l1 = Reminder.objects.create(
            user=self.user,
            task=task_no_cp,
            checkpoint=None,
            scheduled_for=self.now - timedelta(hours=2),
            escalation_level=1,
            channel="WEB",
            status="PENDING",
        )
        rem_l2 = Reminder.objects.create(
            user=self.user,
            task=task_no_cp,
            checkpoint=None,
            scheduled_for=self.now + timedelta(hours=12),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )

        resp = self.client_owner.post(reverse("alerts:done", kwargs={"pk": rem_l1.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "status": "DONE"})

        task_no_cp.refresh_from_db()
        rem_l1.refresh_from_db()
        rem_l2.refresh_from_db()
        self.assertEqual(task_no_cp.status, "DONE")
        self.assertIsNotNone(task_no_cp.completed_at)
        self.assertEqual(rem_l1.status, "DONE")
        self.assertEqual(rem_l2.status, "DONE")


# =============================================================================
# 4. Escalation, Digest, Quiet Hours & AI Hardening Tests
# =============================================================================


@override_settings(PASSWORD_HASHERS=FAST_HASHERS)
class EscalationDigestQuietHoursAndAIHardeningTests(TestCase):
    """Covers reminder dedup/DIGEST preservation, quiet hours boundaries, digest dedup, and AI hardening."""

    def setUp(self):
        self.user = User.objects.create_user(username="hardening_user", password="pw")
        self.now = timezone.now()

    def test_evaluate_due_reminders_deduplicates_lower_tiers_and_preserves_done_task_digest(self):
        active_task = Task.objects.create(
            user=self.user,
            title="Multi-tier Active Task",
            deadline=self.now + timedelta(days=2),
        )
        cp = Checkpoint.objects.create(
            task=active_task,
            title="Draft core module",
            target_deadline=self.now + timedelta(days=1),
            order=1,
        )
        rem_l1 = Reminder.objects.create(
            user=self.user,
            task=active_task,
            checkpoint=cp,
            scheduled_for=self.now - timedelta(hours=4),
            escalation_level=1,
            channel="WEB",
            status="PENDING",
        )
        rem_l2 = Reminder.objects.create(
            user=self.user,
            task=active_task,
            checkpoint=cp,
            scheduled_for=self.now - timedelta(hours=1),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
        )

        done_task = Task.objects.create(
            user=self.user,
            title="Completed Anchor Task",
            deadline=self.now + timedelta(days=1),
            status="DONE",
            completed_at=self.now - timedelta(hours=1),
        )
        digest_rem = Reminder.objects.create(
            user=self.user,
            task=done_task,
            checkpoint=None,
            scheduled_for=self.now - timedelta(minutes=30),
            escalation_level=1,
            channel="DIGEST",
            message="ATR Morning Briefing summary preserved.",
            status="PENDING",
        )

        # sync_reminders_for_task on DONE task must NOT close DIGEST reminders.
        sync_reminders_for_task(done_task, now=self.now)
        digest_rem.refresh_from_db()
        self.assertEqual(digest_rem.status, "PENDING")

        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            due = evaluate_due_reminders(now=self.now, user=self.user)

        returned_ids = {r.pk for r in due}
        self.assertEqual(returned_ids, {rem_l2.pk, digest_rem.pk})

        rem_l1.refresh_from_db()
        rem_l2.refresh_from_db()
        digest_rem.refresh_from_db()
        self.assertEqual(rem_l1.status, "DISMISSED")
        self.assertEqual(rem_l2.status, "PENDING")
        self.assertEqual(digest_rem.status, "PENDING")
        self.assertEqual(digest_rem.message, "ATR Morning Briefing summary preserved.")

    def test_is_within_quiet_hours_overnight_daytime_and_equal_boundaries(self):
        # Overnight: 22:00 - 07:00
        overnight_cases = [
            (time(21, 59), False),
            (time(22, 0), True),
            (time(0, 0), True),
            (time(6, 59), True),
            (time(7, 0), False),
        ]
        for candidate, expected in overnight_cases:
            with self.subTest(window="22:00-07:00", candidate=candidate):
                self.assertIs(_is_within_quiet_hours(candidate, time(22, 0), time(7, 0)), expected)

        # Daytime: 13:00 - 15:00
        daytime_cases = [
            (time(12, 59), False),
            (time(13, 0), True),
            (time(14, 59), True),
            (time(15, 0), False),
        ]
        for candidate, expected in daytime_cases:
            with self.subTest(window="13:00-15:00", candidate=candidate):
                self.assertIs(_is_within_quiet_hours(candidate, time(13, 0), time(15, 0)), expected)

        # Equal start & end: 08:00 - 08:00 (disabled)
        for candidate in (time(0, 0), time(8, 0), time(23, 59)):
            with self.subTest(window="08:00-08:00", candidate=candidate):
                self.assertFalse(_is_within_quiet_hours(candidate, time(8, 0), time(8, 0)))

    def test_process_reminders_respects_quiet_hours_and_ignore_flag(self):
        quiet_now = datetime(2026, 10, 15, 23, 30, tzinfo=JAKARTA_TZ)
        TelegramLink.objects.create(
            user=self.user,
            chat_id=810001,
            link_token="tok-quiet-proc",
            is_verified=True,
            linked_at=quiet_now - timedelta(days=1),
        )

        task_l1 = Task.objects.create(
            user=self.user, title="Task L1", deadline=quiet_now + timedelta(days=4)
        )
        task_l2 = Task.objects.create(
            user=self.user, title="Task L2", deadline=quiet_now + timedelta(hours=30)
        )
        task_l3 = Task.objects.create(
            user=self.user, title="Task L3 Critical", deadline=quiet_now + timedelta(hours=2)
        )

        rem_l1 = Reminder.objects.create(
            user=self.user,
            task=task_l1,
            scheduled_for=quiet_now - timedelta(minutes=10),
            escalation_level=1,
            status="PENDING",
        )
        rem_l2 = Reminder.objects.create(
            user=self.user,
            task=task_l2,
            scheduled_for=quiet_now - timedelta(minutes=10),
            escalation_level=2,
            status="PENDING",
        )
        rem_l3 = Reminder.objects.create(
            user=self.user,
            task=task_l3,
            scheduled_for=quiet_now - timedelta(minutes=10),
            escalation_level=3,
            status="PENDING",
        )

        out1 = StringIO()
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False),
            patch("django.utils.timezone.now", return_value=quiet_now),
            patch(
                "tasks.management.commands.process_reminders.send_telegram_alert",
                return_value=True,
            ) as mock_send,
        ):
            call_command("process_reminders", stdout=out1)

        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(mock_send.call_args.args[0].pk, rem_l3.pk)
        self.assertIn("telegram_sent=1", out1.getvalue())
        self.assertIn("quiet_deferred=2", out1.getvalue())

        rem_l1.refresh_from_db()
        rem_l2.refresh_from_db()
        rem_l3.refresh_from_db()
        self.assertIsNone(rem_l1.sent_at)
        self.assertIsNone(rem_l2.sent_at)
        self.assertIsNotNone(rem_l3.sent_at)

        # With --ignore-quiet-hours, deferred L1 & L2 are dispatched too.
        out2 = StringIO()
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False),
            patch("django.utils.timezone.now", return_value=quiet_now),
            patch(
                "tasks.management.commands.process_reminders.send_telegram_alert",
                return_value=True,
            ) as mock_send_force,
        ):
            call_command("process_reminders", "--ignore-quiet-hours", stdout=out2)

        sent_ids = {call.args[0].pk for call in mock_send_force.call_args_list}
        self.assertEqual(sent_ids, {rem_l1.pk, rem_l2.pk})
        self.assertIn("telegram_sent=2", out2.getvalue())
        self.assertIn("quiet_deferred=0", out2.getvalue())

    def test_send_daily_digest_suppresses_telegram_in_quiet_hours_unless_forced(self):
        quiet_now = datetime(2026, 10, 15, 23, 30, tzinfo=JAKARTA_TZ)
        TelegramLink.objects.create(
            user=self.user,
            chat_id=820001,
            link_token="tok-quiet-digest",
            is_verified=True,
            linked_at=quiet_now - timedelta(days=1),
        )
        Task.objects.create(
            user=self.user,
            title="Upcoming 48h Task",
            deadline=quiet_now + timedelta(hours=18),
        )

        # Default run during quiet hours: records DailyDigestLog but defers Telegram send.
        out_quiet = StringIO()
        with (
            patch("django.utils.timezone.now", return_value=quiet_now),
            patch(
                "tasks.management.commands.send_daily_digest.send_telegram_digest",
                return_value=True,
            ) as mock_tg_digest,
        ):
            call_command("send_daily_digest", "--slot=evening", stdout=out_quiet)

        self.assertEqual(mock_tg_digest.call_count, 0)
        self.assertIn("quiet_deferred=1", out_quiet.getvalue())
        self.assertIn("telegram_sent=0", out_quiet.getvalue())
        self.assertTrue(
            DailyDigestLog.objects.filter(
                user=self.user,
                digest_date=quiet_now.date(),
                slot="EVENING",
            ).exists()
        )

        # With --force during quiet hours: sends Telegram digest AND records DailyDigestLog.
        out_force = StringIO()
        with (
            patch("django.utils.timezone.now", return_value=quiet_now),
            patch(
                "tasks.management.commands.send_daily_digest.send_telegram_digest",
                return_value=True,
            ) as mock_tg_digest_force,
        ):
            call_command("send_daily_digest", "--slot=morning", "--force", stdout=out_force)

        self.assertEqual(mock_tg_digest_force.call_count, 1)
        self.assertIn("telegram_sent=1", out_force.getvalue())
        self.assertIn("quiet_deferred=0", out_force.getvalue())
        self.assertTrue(
            DailyDigestLog.objects.filter(
                user=self.user,
                digest_date=quiet_now.date(),
                slot="MORNING",
            ).exists()
        )

    def test_build_daily_digest_includes_yesterday_overdue_cp_and_dedups_upcoming_48h(self):
        fixed_now = datetime(2026, 10, 15, 9, 0, tzinfo=JAKARTA_TZ)

        task_with_cp = Task.objects.create(
            user=self.user,
            title="Task With Overdue CP",
            deadline=fixed_now + timedelta(hours=20),
            priority_score=80.0,
        )
        yesterday_cp = Checkpoint.objects.create(
            task=task_with_cp,
            title="Yesterday Overdue Milestone",
            target_deadline=fixed_now - timedelta(days=1),
            is_done=False,
            order=1,
        )
        standalone_48h_task = Task.objects.create(
            user=self.user,
            title="Standalone 48h Task",
            deadline=fixed_now + timedelta(hours=30),
            priority_score=60.0,
        )

        digest = build_daily_digest(self.user, slot="MORNING", now=fixed_now)

        # Yesterday's overdue checkpoint is included.
        self.assertIn(yesterday_cp, digest["today_checkpoints"])
        cp_items = [item for item in digest["items"] if item["checkpoint_id"] == yesterday_cp.pk]
        self.assertEqual(len(cp_items), 1)
        self.assertTrue(cp_items[0]["is_overdue"])
        self.assertIn("[OVERDUE CP]", digest["summary_text"])

        # task_with_cp is in raw upcoming_48h_tasks, but deduplicated out of unified items.
        self.assertIn(task_with_cp, digest["upcoming_48h_tasks"])
        upcoming_item_task_ids = [
            item["task_id"]
            for item in digest["items"]
            if item["kind"] == "upcoming_48h_task"
        ]
        self.assertNotIn(task_with_cp.pk, upcoming_item_task_ids)
        self.assertIn(standalone_48h_task.pk, upcoming_item_task_ids)

    def test_ai_checkpoints_header_auth_pii_scrub_cap_and_non_finite_minutes(self):
        task = Task.objects.create(
            user=self.user,
            title="Proyek Akhir untuk budi@ui.ac.id",
            description="Hubungi +62 812-3456-7890 atau NPM 2306123456 untuk revisi.",
            category="PROJECT",
            deadline=self.now + timedelta(days=5),
            estimated_minutes=240,
        )

        raw_ai_items = [
            {"title": "Step 1 Infinity", "phase": "RESEARCH", "estimated_minutes": "Infinity"},
            {"title": "Step 2 Bool", "phase": "DRAFT", "estimated_minutes": True},
            {"title": "Step 3 NegInf", "phase": "REVIEW", "estimated_minutes": "-Infinity"},
            {"title": "Step 4 NaN", "phase": "SUBMIT", "estimated_minutes": "NaN"},
            {"title": "Step 5 Normal", "phase": "CUSTOM", "estimated_minutes": 45},
            {"title": "Step 6 Cap Edge", "phase": "CUSTOM", "estimated_minutes": 50},
            {"title": "Step 7 Should Be Dropped", "phase": "CUSTOM", "estimated_minutes": 30},
            {"title": "Step 8 Should Be Dropped", "phase": "CUSTOM", "estimated_minutes": 30},
        ]
        gemini_payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": json.dumps(raw_ai_items)}],
                    }
                }
            ]
        }

        captured_requests = []

        def fake_urlopen(req, timeout=10):
            captured_requests.append(req)
            return _make_fake_http_response(gemini_payload)

        api_key = "test-gemini-secret-key-999"
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": api_key}, clear=False),
            patch("tasks.services.ai_checkpoints.urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            suggestions, source = suggest_checkpoints(
                task,
                extra_context="Email asisten: asdos@cs.ui.ac.id WA 081398765432",
            )

        self.assertEqual(source, "ai")
        self.assertEqual(len(captured_requests), 1)
        req = captured_requests[0]

        # Key passed via X-goog-api-key header, NEVER in URL query string.
        self.assertEqual(req.get_header("X-goog-api-key"), api_key)
        self.assertNotIn("?key=", req.full_url)
        self.assertNotIn(api_key, req.full_url)

        # Prompt body scrubs emails, phones, and 10-digit student IDs.
        sent_body = req.data.decode("utf-8")
        self.assertNotIn("budi@ui.ac.id", sent_body)
        self.assertNotIn("asdos@cs.ui.ac.id", sent_body)
        self.assertNotIn("812-3456-7890", sent_body)
        self.assertNotIn("081398765432", sent_body)
        self.assertNotIn("2306123456", sent_body)
        self.assertIn("[REDACTED_EMAIL]", sent_body)
        self.assertIn("[REDACTED_PHONE]", sent_body)
        self.assertIn("[REDACTED_ID]", sent_body)

        # Suggestions capped to <= 6 and Infinity/NaN/bool minutes handled without OverflowError.
        self.assertLessEqual(len(suggestions), 6)
        self.assertEqual(len(suggestions), 6)
        self.assertEqual(suggestions[0]["estimated_minutes"], 30)  # "Infinity"
        self.assertEqual(suggestions[1]["estimated_minutes"], 30)  # True
        self.assertEqual(suggestions[2]["estimated_minutes"], 30)  # "-Infinity"
        self.assertEqual(suggestions[3]["estimated_minutes"], 30)  # "NaN"
        self.assertEqual(suggestions[4]["estimated_minutes"], 45)

    def test_ai_coaching_overdue_subhour_unsaved_preview_and_naive_now(self):
        fixed_now = datetime(2026, 10, 15, 10, 0, tzinfo=JAKARTA_TZ)
        task = Task.objects.create(
            user=self.user,
            title="TP Coaching Edge Cases",
            deadline=fixed_now + timedelta(days=1),
        )

        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            # 1. Overdue reminder (hours_left <= 0) uses overdue copy and never says "0 jam".
            overdue_cp = Checkpoint.objects.create(
                task=task,
                title="Submit PDF report",
                target_deadline=fixed_now - timedelta(hours=2),
                order=1,
            )
            overdue_rem = Reminder.objects.create(
                user=self.user,
                task=task,
                checkpoint=overdue_cp,
                scheduled_for=fixed_now - timedelta(hours=5),
                escalation_level=3,
            )
            for tone in ("friendly", "strict", "minimal"):
                with self.subTest(case="overdue", tone=tone):
                    msg, source = generate_coaching_message(
                        overdue_rem, user_tone=tone, now=fixed_now
                    )
                    self.assertEqual(source, "fallback")
                    self.assertNotIn("0 jam", msg)
                    self.assertTrue(
                        any(kw in msg.lower() for kw in ("lewat", "terlambat", "overdue"))
                    )

            # 2. Sub-hour reminder uses "menit" rather than fractional hours.
            subhour_cp = Checkpoint.objects.create(
                task=task,
                title="Final proofread",
                target_deadline=fixed_now + timedelta(minutes=25),
                order=2,
            )
            subhour_rem = Reminder.objects.create(
                user=self.user,
                task=task,
                checkpoint=subhour_cp,
                scheduled_for=fixed_now,
                escalation_level=3,
            )
            msg_sub, src_sub = generate_coaching_message(
                subhour_rem, user_tone="friendly", now=fixed_now
            )
            self.assertEqual(src_sub, "fallback")
            self.assertIn("25 menit", msg_sub)
            self.assertNotIn("jam", msg_sub)

            # 3. Unsaved preview checkpoint uses checkpoint title, NOT "Target utama".
            unsaved_task = Task(
                user=self.user,
                title="Preview TP 4",
                deadline=fixed_now + timedelta(hours=18),
            )
            unsaved_cp = Checkpoint(
                task=unsaved_task,
                title="Test edge cases & revise",
                target_deadline=fixed_now + timedelta(hours=10),
            )
            unsaved_rem = Reminder(
                user=self.user,
                task=unsaved_task,
                checkpoint=unsaved_cp,
                scheduled_for=fixed_now,
                escalation_level=2,
                snooze_count=0,
            )
            msg_preview, src_preview = generate_coaching_message(
                unsaved_rem, user_tone="minimal", now=fixed_now
            )
            self.assertEqual(src_preview, "fallback")
            self.assertIn("Test edge cases & revise", msg_preview)
            self.assertNotIn("Target utama", msg_preview)

            # 4. Naive `now` is normalized cleanly without TypeError.
            naive_now = datetime(2026, 10, 15, 10, 0)  # intentionally naive
            msg_naive, src_naive = generate_coaching_message(
                subhour_rem, user_tone="strict", now=naive_now
            )
            self.assertEqual(src_naive, "fallback")
            self.assertIn("25 menit", msg_naive)


# =============================================================================
# 5. Performance (Query Count) & Zero Undefined Template Variables Tests
# =============================================================================


@override_settings(PASSWORD_HASHERS=FAST_HASHERS)
class PerformanceAndTemplateIntegrityTests(TestCase):
    """Enforces query-count ceilings and verifies zero undefined template variables across all pages."""

    def setUp(self):
        self.user = User.objects.create_user(username="perf_user", password="pw")
        self.now = timezone.now()
        self.course = Course.objects.create(
            user=self.user, code="CSGE601020", name="DDP 1"
        )
        self.client_auth = Client()
        self.client_auth.force_login(self.user)

    def test_task_history_15_completed_tasks_query_ceiling(self):
        tasks_created = []
        for idx in range(15):
            t = Task.objects.create(
                user=self.user,
                course=self.course,
                title=f"Completed Task #{idx + 1}",
                deadline=self.now - timedelta(days=idx + 1),
                estimated_minutes=60,
                status="DONE",
                completed_at=self.now - timedelta(hours=idx + 1),
            )
            tasks_created.append(t)

        FocusSession.objects.bulk_create(
            [
                FocusSession(
                    user=self.user,
                    task=t,
                    mode="POMODORO_25_5",
                    planned_minutes=25,
                    actual_minutes=25,
                    status="COMPLETED",
                )
                for t in tasks_created
                for _ in range(2)
            ]
        )

        with CaptureQueriesContext(connection) as ctx:
            resp = self.client_auth.get(reverse("tasks:task_history"))

        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(
            len(ctx),
            8,
            f"Expected <= 8 queries on task_history, got {len(ctx)}: "
            f"{[q['sql'] for q in ctx.captured_queries]}",
        )
        self.assertContains(resp, "50 mnt")

    def test_task_detail_query_ceiling_and_no_triple_sum(self):
        task = Task.objects.create(
            user=self.user,
            course=self.course,
            title="Detail Perf Task",
            deadline=self.now + timedelta(days=3),
            estimated_minutes=120,
        )
        cp = Checkpoint.objects.create(
            task=task,
            title="Phase 1",
            target_deadline=self.now + timedelta(days=1),
            order=1,
        )
        for _ in range(3):
            FocusSession.objects.create(
                user=self.user,
                task=task,
                checkpoint=cp,
                mode="POMODORO_25_5",
                planned_minutes=25,
                actual_minutes=20,
                status="COMPLETED",
            )
        sync_reminders_for_task(task, now=self.now)

        with CaptureQueriesContext(connection) as ctx:
            resp = self.client_auth.get(reverse("tasks:task_detail", kwargs={"pk": task.pk}))

        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(
            len(ctx),
            10,
            f"Expected <= 10 queries on task_detail, got {len(ctx)}: "
            f"{[q['sql'] for q in ctx.captured_queries]}",
        )
        sum_queries = [
            q["sql"] for q in ctx.captured_queries if "SUM(" in q["sql"].upper()
        ]
        self.assertLessEqual(
            len(sum_queries),
            1,
            f"Expected at most 1 SUM query on task_detail, found {len(sum_queries)}: {sum_queries}",
        )

    def test_poll_due_alerts_10_web_reminders_query_ceiling(self):
        tasks_list = [
            Task.objects.create(
                user=self.user,
                title=f"Due Poll Task #{i}",
                deadline=self.now + timedelta(hours=6 + i),
            )
            for i in range(10)
        ]
        Reminder.objects.bulk_create(
            [
                Reminder(
                    user=self.user,
                    task=t,
                    scheduled_for=self.now - timedelta(minutes=5),
                    escalation_level=2,
                    channel="WEB",
                    message="",
                    status="PENDING",
                )
                for t in tasks_list
            ]
        )

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False),
            CaptureQueriesContext(connection) as ctx,
        ):
            resp = self.client_auth.get(reverse("alerts:poll"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["alerts"]), 10)
        self.assertLessEqual(
            len(ctx),
            9,
            f"Expected <= 9 queries on poll_due_alerts with 10 reminders, got {len(ctx)}: "
            f"{[q['sql'] for q in ctx.captured_queries]}",
        )

    def test_toggle_checkpoint_4_checkpoint_task_query_ceiling(self):
        task = Task.objects.create(
            user=self.user,
            course=self.course,
            title="4-Checkpoint Toggle Perf Task",
            deadline=self.now + timedelta(days=6),
            estimated_minutes=180,
        )
        checkpoints = [
            Checkpoint.objects.create(
                task=task,
                title=f"CP {i}",
                phase="DRAFT",
                target_deadline=self.now + timedelta(days=i),
                estimated_minutes=45,
                order=i,
            )
            for i in range(1, 5)
        ]
        sync_reminders_for_task(task, now=self.now)

        with CaptureQueriesContext(connection) as ctx:
            resp = self.client_auth.post(
                reverse("tasks:toggle_checkpoint", kwargs={"pk": checkpoints[0].pk}),
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(
            len(ctx),
            16,
            f"Expected <= 16 queries on toggle_checkpoint for 4-checkpoint task, got {len(ctx)}: "
            f"{[q['sql'] for q in ctx.captured_queries]}",
        )

    def test_all_pages_render_with_zero_undefined_template_variables(self):
        # Populate rich realistic records so every template branch/loop is exercised.
        active_task = Task.objects.create(
            user=self.user,
            course=self.course,
            title="Active Template Check Task",
            description="Check every variable in templates.",
            deadline=self.now + timedelta(days=2),
            estimated_minutes=120,
            weight_percentage=Decimal("15.00"),
            difficulty=4,
            is_group_project=True,
            priority_score=85.5,
            priority_level="HIGH",
            status="IN_PROGRESS",
        )
        done_task = Task.objects.create(
            user=self.user,
            course=self.course,
            title="Done Template Check Task",
            deadline=self.now - timedelta(days=1),
            estimated_minutes=60,
            actual_minutes=50,
            status="DONE",
            completed_at=self.now - timedelta(hours=3),
        )
        cp = Checkpoint.objects.create(
            task=active_task,
            title="Verify template vars",
            phase="REVIEW",
            target_deadline=self.now + timedelta(hours=10),
            estimated_minutes=30,
            order=1,
            is_ai_generated=True,
        )
        FocusSession.objects.create(
            user=self.user,
            task=active_task,
            checkpoint=cp,
            mode="POMODORO_25_5",
            planned_minutes=25,
            actual_minutes=25,
            status="COMPLETED",
        )
        Reminder.objects.create(
            user=self.user,
            task=active_task,
            checkpoint=cp,
            scheduled_for=self.now - timedelta(minutes=15),
            escalation_level=2,
            channel="WEB",
            message="Kerjakan checkpoint ini sekarang.",
            status="PENDING",
        )
        Reminder.objects.create(
            user=self.user,
            task=active_task,
            checkpoint=cp,
            scheduled_for=self.now - timedelta(minutes=30),
            escalation_level=1,
            channel="WEB",
            message="Snoozed reminder.",
            status="SNOOZED",
            snoozed_until=self.now + timedelta(hours=1),
            snooze_count=1,
        )
        Reminder.objects.create(
            user=self.user,
            task=done_task,
            scheduled_for=self.now - timedelta(days=1),
            escalation_level=1,
            channel="WEB",
            message="Done reminder.",
            status="DONE",
        )
        DailyDigestLog.objects.create(
            user=self.user,
            digest_date=timezone.localdate(self.now),
            slot="MORNING",
            item_count=1,
            payload_summary="Morning summary",
        )
        TelegramLink.objects.create(
            user=self.user,
            link_token="template-check-token",
            token_created_at=self.now,
            is_verified=False,
        )

        strict_templates = deepcopy(settings.TEMPLATES)
        strict_templates[0]["OPTIONS"]["string_if_invalid"] = "INVALID_VAR:%s"

        auth_urls = [
            ("tasks:task_list", reverse("tasks:task_list")),
            ("tasks:task_create", reverse("tasks:task_create")),
            ("tasks:task_detail", reverse("tasks:task_detail", kwargs={"pk": active_task.pk})),
            ("tasks:task_update", reverse("tasks:task_update", kwargs={"pk": active_task.pk})),
            ("tasks:task_history", reverse("tasks:task_history")),
            ("tasks:focus_mode", reverse("tasks:focus_mode", kwargs={"pk": active_task.pk})),
            ("alerts:inbox", reverse("alerts:inbox")),
            ("alerts:daily_digest", reverse("alerts:daily_digest")),
            ("alerts:telegram_connect", reverse("alerts:telegram_connect")),
            ("alerts:coaching_preview", reverse("alerts:coaching_preview")),
        ]
        anon_urls = [
            ("login", reverse("login")),
            ("register", reverse("register")),
        ]

        anon_client = Client()
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False),
            self.settings(TEMPLATES=strict_templates),
        ):
            self.assertEqual(engines["django"].engine.string_if_invalid, "INVALID_VAR:%s")
            self.assertEqual(
                engines["django"].from_string("{{ undefined_sentinel }}").render({}),
                "INVALID_VAR:undefined_sentinel",
            )

            for label, url in auth_urls:
                with self.subTest(page=label):
                    resp = self.client_auth.get(url)
                    self.assertEqual(resp.status_code, 200)
                    body = resp.content.decode("utf-8")
                    self.assertNotIn(
                        "INVALID_VAR:",
                        body,
                        f"Undefined template variable rendered on {label} ({url})",
                    )

            for label, url in anon_urls:
                with self.subTest(page=label):
                    resp = anon_client.get(url)
                    self.assertEqual(resp.status_code, 200)
                    body = resp.content.decode("utf-8")
                    self.assertNotIn(
                        "INVALID_VAR:",
                        body,
                        f"Undefined template variable rendered on {label} ({url})",
                    )
