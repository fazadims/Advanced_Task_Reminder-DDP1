"""Tests for F4.5 — Adaptive AI Coaching, 3x3 Fallback Matrix, Caching & Preview View."""

from __future__ import annotations

import json
import os
import urllib.error
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from tasks.models import Checkpoint, Reminder, Task
from tasks.services.ai_coaching import (
    ANTI_SNOOZE_TEMPLATES,
    VALID_TONES,
    generate_coaching_message,
)
from tasks.services.notifier import evaluate_due_reminders
from tasks.tests.base import FastTestCase

User = get_user_model()


def _mock_gemini_response(text: str) -> MagicMock:
    """Builds a context-manager mock returning a Gemini generateContent JSON response."""
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                }
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_resp
    mock_cm.__exit__.return_value = False
    return mock_cm


class AdaptiveCoachingTests(FastTestCase):
    """Covers 3x3 fallback templates, anti-snooze templates, Gemini mocking, caching, and preview."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="coaching_student",
            email="coach@example.com",
            password="testpass123!",
        )
        self.now = timezone.now().replace(microsecond=0)
        self.task = Task.objects.create(
            user=self.user,
            title="TP 4 DDP1",
            deadline=self.now + timedelta(hours=24),
        )
        self.checkpoint = Checkpoint.objects.create(
            task=self.task,
            title="Write unit tests",
            phase="REVIEW",
            target_deadline=self.now + timedelta(hours=12),
            order=1,
        )

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
    def test_all_nine_tone_and_level_fallback_combinations(self) -> None:
        """All 9 (tone x escalation_level) fallbacks return non-empty strings <= 200 chars with source='fallback'."""
        seen_messages: set[str] = set()
        for tone in VALID_TONES:
            for level in (1, 2, 3):
                with self.subTest(tone=tone, escalation_level=level):
                    reminder = Reminder(
                        user=self.user,
                        task=self.task,
                        checkpoint=self.checkpoint,
                        scheduled_for=self.now,
                        escalation_level=level,
                        snooze_count=0,
                    )
                    msg, source = generate_coaching_message(
                        reminder,
                        user_tone=tone,
                        now=self.now,
                    )
                    self.assertEqual(source, "fallback")
                    self.assertIsInstance(msg, str)
                    self.assertGreater(len(msg.strip()), 0)
                    self.assertLessEqual(len(msg), 200)
                    seen_messages.add(msg)

        # Each of the 9 combinations produces distinct coaching copy.
        self.assertEqual(len(seen_messages), 9)

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
    def test_snooze_count_ge_2_triggers_anti_snooze_templates(self) -> None:
        """snooze_count >= 2 selects ANTI_SNOOZE_TEMPLATES[tone] for friendly, strict, and minimal."""
        for tone in VALID_TONES:
            with self.subTest(tone=tone):
                reminder = Reminder(
                    user=self.user,
                    task=self.task,
                    checkpoint=self.checkpoint,
                    scheduled_for=self.now,
                    escalation_level=3,
                    snooze_count=2,
                )
                msg, source = generate_coaching_message(
                    reminder,
                    user_tone=tone,
                    now=self.now,
                )
                expected = ANTI_SNOOZE_TEMPLATES[tone].format(
                    hours="12",
                    checkpoint=self.checkpoint.title,
                    task=self.task.title,
                )
                self.assertEqual(source, "fallback")
                self.assertEqual(msg, expected)
                self.assertLessEqual(len(msg), 200)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}, clear=False)
    @patch("tasks.services.ai_coaching.urllib.request.urlopen")
    def test_mocked_gemini_success_returns_gemini_source(self, mock_urlopen) -> None:
        """Mocked Gemini API success returns concise AI text with source == 'gemini' and omits PII."""
        ai_reply = "Yuk mulai 20 menit untuk Write unit tests di TP 4 DDP1 sebelum deadline!"
        mock_urlopen.return_value = _mock_gemini_response(ai_reply)

        reminder = Reminder(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now,
            escalation_level=2,
            snooze_count=0,
        )
        msg, source = generate_coaching_message(reminder, user_tone="friendly", now=self.now)

        self.assertEqual(source, "gemini")
        self.assertEqual(msg, ai_reply)
        mock_urlopen.assert_called_once()

        # Verify request carries API key header and excludes user PII.
        req = mock_urlopen.call_args[0][0]
        body_text = req.data.decode("utf-8")
        self.assertEqual(req.get_header("X-goog-api-key"), "fake-gemini-key")
        self.assertNotIn(self.user.username, body_text)
        self.assertNotIn(self.user.email, body_text)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}, clear=False)
    @patch("tasks.services.ai_coaching.urllib.request.urlopen")
    def test_gemini_timeout_gracefully_falls_back(self, mock_urlopen) -> None:
        """Network timeout from Gemini API gracefully falls back to deterministic template."""
        mock_urlopen.side_effect = urllib.error.URLError("timed out")

        reminder = Reminder(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now,
            escalation_level=2,
            snooze_count=0,
        )
        with self.assertLogs("tasks.services.ai_coaching", level="WARNING") as cm:
            msg, source = generate_coaching_message(reminder, user_tone="strict", now=self.now)

        self.assertIn("fallback", "\n".join(cm.output).lower())
        self.assertEqual(source, "fallback")
        self.assertGreater(len(msg.strip()), 0)
        self.assertLessEqual(len(msg), 200)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}, clear=False)
    @patch("tasks.services.ai_coaching.urllib.request.urlopen")
    def test_gemini_overly_long_response_falls_back(self, mock_urlopen) -> None:
        """Gemini output exceeding 220 characters is rejected and falls back cleanly."""
        too_long = "Semangat mengerjakan tugas! " * 15  # > 220 chars
        self.assertGreater(len(too_long), 220)
        mock_urlopen.return_value = _mock_gemini_response(too_long)

        reminder = Reminder(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now,
            escalation_level=1,
            snooze_count=0,
        )
        msg, source = generate_coaching_message(reminder, user_tone="minimal", now=self.now)

        self.assertEqual(source, "fallback")
        self.assertLessEqual(len(msg), 200)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}, clear=False)
    @patch("tasks.services.ai_coaching.urllib.request.urlopen")
    def test_evaluate_due_reminders_caches_message_and_skips_repeat_llm_call(
        self,
        mock_urlopen,
    ) -> None:
        """evaluate_due_reminders persists reminder.message and does not re-call LLM at the same level."""
        cached_reply = "Ayo tuntaskan Write unit tests dalam 25 menit fokus sekarang!"
        mock_urlopen.return_value = _mock_gemini_response(cached_reply)

        reminder = Reminder.objects.create(
            user=self.user,
            task=self.task,
            checkpoint=self.checkpoint,
            scheduled_for=self.now - timedelta(minutes=5),
            escalation_level=2,
            channel="WEB",
            status="PENDING",
            message="",
        )

        # First evaluation calls Gemini and caches message on Reminder.
        first_due = evaluate_due_reminders(now=self.now)
        self.assertEqual([r.pk for r in first_due], [reminder.pk])
        self.assertEqual(mock_urlopen.call_count, 1)

        reminder.refresh_from_db()
        self.assertEqual(reminder.message, cached_reply)

        # Second evaluation at the same escalation level reuses cached message without calling LLM.
        second_due = evaluate_due_reminders(now=self.now + timedelta(minutes=5))
        self.assertEqual([r.pk for r in second_due], [reminder.pk])
        self.assertEqual(mock_urlopen.call_count, 1)
        reminder.refresh_from_db()
        self.assertEqual(reminder.message, cached_reply)

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
    def test_coaching_preview_view_get_and_post_render_200_with_styled_partial(self) -> None:
        """GET and POST to /alerts/coaching/preview/ return HTTP 200 with tone-styled partial."""
        self.client.force_login(self.user)
        url = reverse("alerts:coaching_preview")

        resp_get = self.client.get(url, {"tone": "friendly"})
        self.assertEqual(resp_get.status_code, 200)
        self.assertTemplateUsed(resp_get, "tasks/alerts/partials/coaching_preview.html")
        self.assertContains(resp_get, "Tone: Friendly")
        self.assertContains(resp_get, "bg-emerald-50")
        self.assertContains(resp_get, "Deterministic Fallback")

        resp_post_strict = self.client.post(url, {"tone": "strict"})
        self.assertEqual(resp_post_strict.status_code, 200)
        self.assertTemplateUsed(resp_post_strict, "tasks/alerts/partials/coaching_preview.html")
        self.assertContains(resp_post_strict, "Tone: Strict")
        self.assertContains(resp_post_strict, "bg-red-50")

        resp_post_minimal = self.client.post(url, {"tone": "minimal"})
        self.assertEqual(resp_post_minimal.status_code, 200)
        self.assertContains(resp_post_minimal, "Tone: Minimal")
        self.assertContains(resp_post_minimal, "bg-slate-50")
