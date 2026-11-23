"""Tests for F3.4 Optional Gemini AI Checkpoint Suggestions & Zero-Error Fallback."""

import json
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from brp.models import Course
from tasks.models import Checkpoint, Task
from tasks.services.ai_checkpoints import suggest_checkpoints
from tasks.tests.base import FastTestCase

User = get_user_model()


def _mock_urlopen_response(payload_bytes: bytes) -> MagicMock:
    """Builds a context-manager mock compatible with `with urllib.request.urlopen(...)`."""
    resp = MagicMock()
    resp.read.return_value = payload_bytes
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class AICheckpointSuggestionTests(FastTestCase):
    """Covers F3.4 Gemini service, fallback paths, preview view, and selective apply."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="ai_student",
            email="ai@ui.ac.id",
            password="StrongPass123!",
        )
        self.other_user = User.objects.create_user(
            username="ai_other",
            email="ai_other@ui.ac.id",
            password="StrongPass123!",
        )
        self.course = Course.objects.create(
            user=self.user,
            code="CSGE601020",
            name="DDP 1",
        )
        self.now = timezone.now()
        self.task = Task.objects.create(
            user=self.user,
            course=self.course,
            title="TP 4 GUI Tkinter App",
            description="Build an interactive grade calculator GUI.",
            category="TP",
            deadline=self.now + timedelta(days=6),
            estimated_minutes=200,
            difficulty=4,
            status="TODO",
        )

    def test_unset_gemini_api_key_returns_fallback_with_zero_network_calls(self):
        env_without_key = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            with patch("urllib.request.urlopen") as mock_urlopen:
                suggestions, source = suggest_checkpoints(self.task)

                mock_urlopen.assert_not_called()
                self.assertEqual(source, "fallback")
                self.assertEqual(len(suggestions), 4)
                for item in suggestions:
                    self.assertTrue(item["title"])
                    self.assertIn(item["phase"], {"RESEARCH", "DRAFT", "REVIEW", "SUBMIT", "CUSTOM"})
                    self.assertGreaterEqual(item["estimated_minutes"], 10)
                    self.assertLessEqual(item["estimated_minutes"], 240)

    def test_valid_mocked_gemini_json_returns_ai_with_normalized_phases_and_clamped_minutes(self):
        gemini_items = [
            {"title": "Sketch Tkinter layout", "phase": "research", "estimated_minutes": 3},
            {"title": "Build calculator controller", "phase": "UNKNOWN_PHASE", "estimated_minutes": 480},
            {"title": "Run GUI edge-case checks", "phase": "Review", "estimated_minutes": 45},
        ]
        api_envelope = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": json.dumps(gemini_items)},
                        ]
                    }
                }
            ]
        }
        mock_cm = _mock_urlopen_response(json.dumps(api_envelope).encode("utf-8"))

        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}):
            with patch("urllib.request.urlopen", return_value=mock_cm) as mock_urlopen:
                suggestions, source = suggest_checkpoints(self.task, extra_context="Use ttk widgets")

                mock_urlopen.assert_called_once()
                self.assertEqual(source, "ai")
                self.assertEqual(len(suggestions), 3)

                # Phase normalized & minutes clamped to [10, 240]
                self.assertEqual(suggestions[0]["title"], "Sketch Tkinter layout")
                self.assertEqual(suggestions[0]["phase"], "RESEARCH")
                self.assertEqual(suggestions[0]["estimated_minutes"], 10)

                self.assertEqual(suggestions[1]["title"], "Build calculator controller")
                self.assertEqual(suggestions[1]["phase"], "CUSTOM")
                self.assertEqual(suggestions[1]["estimated_minutes"], 240)

                self.assertEqual(suggestions[2]["title"], "Run GUI edge-case checks")
                self.assertEqual(suggestions[2]["phase"], "REVIEW")
                self.assertEqual(suggestions[2]["estimated_minutes"], 45)

    def test_preview_ai_checkpoints_view_handles_timeout_and_malformed_json_with_fallback_200(self):
        self.client.force_login(self.user)
        preview_url = reverse("tasks:preview_ai_checkpoints", kwargs={"pk": self.task.pk})

        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}):
            # 1. Simulated TimeoutError
            with patch("urllib.request.urlopen", side_effect=TimeoutError("request timed out")):
                with self.assertLogs("tasks.services.ai_checkpoints", level="WARNING") as cm:
                    resp_timeout = self.client.post(preview_url, HTTP_HX_REQUEST="true")
                log_output = "\n".join(cm.output)
                self.assertIn("fallback", log_output.lower())
                self.assertNotIn("fake-gemini-key", log_output)
                self.assertEqual(resp_timeout.status_code, 200)
                self.assertEqual(resp_timeout.context["source"], "fallback")
                self.assertTrue(len(resp_timeout.context["suggestions"]) >= 3)
                self.assertIn("Template Cerdas", resp_timeout.content.decode())

            # 2. Malformed HTTP body JSON
            malformed_body = _mock_urlopen_response(b"{not-valid-json!!!")
            with patch("urllib.request.urlopen", return_value=malformed_body):
                with self.assertLogs("tasks.services.ai_checkpoints", level="WARNING") as cm:
                    resp_bad_body = self.client.post(preview_url, HTTP_HX_REQUEST="true")
                log_output = "\n".join(cm.output)
                self.assertIn("fallback", log_output.lower())
                self.assertNotIn("fake-gemini-key", log_output)
                self.assertEqual(resp_bad_body.status_code, 200)
                self.assertEqual(resp_bad_body.context["source"], "fallback")
                self.assertIn("Template Cerdas", resp_bad_body.content.decode())

            # 3. Malformed inner Gemini text JSON
            bad_inner = {
                "candidates": [{"content": {"parts": [{"text": "Here are your steps: not json"}]}}]
            }
            malformed_inner = _mock_urlopen_response(json.dumps(bad_inner).encode("utf-8"))
            with patch("urllib.request.urlopen", return_value=malformed_inner):
                with self.assertLogs("tasks.services.ai_checkpoints", level="WARNING") as cm:
                    resp_bad_inner = self.client.post(preview_url, HTTP_HX_REQUEST="true")
                log_output = "\n".join(cm.output)
                self.assertIn("fallback", log_output.lower())
                self.assertNotIn("fake-gemini-key", log_output)
                self.assertEqual(resp_bad_inner.status_code, 200)
                self.assertEqual(resp_bad_inner.context["source"], "fallback")
                self.assertIn("Template Cerdas", resp_bad_inner.content.decode())

    def test_apply_ai_checkpoints_saves_only_checked_items_with_ai_flag_and_spaced_deadlines(self):
        self.client.force_login(self.user)
        apply_url = reverse("tasks:apply_ai_checkpoints", kwargs={"pk": self.task.pk})

        # Submit 3 items in form, but only indices 0 and 2 are checked
        payload = {
            "source": "ai",
            "selected": ["0", "2"],
            "title_0": "Design state machine",
            "phase_0": "RESEARCH",
            "estimated_minutes_0": "35",
            "title_1": "Unchecked Optional Step",
            "phase_1": "DRAFT",
            "estimated_minutes_1": "50",
            "title_2": "Package & submit ZIP",
            "phase_2": "SUBMIT",
            "estimated_minutes_2": "25",
        }
        resp = self.client.post(apply_url, data=payload, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)

        saved = list(self.task.checkpoints.all())
        self.assertEqual(len(saved), 2)
        self.assertEqual([cp.title for cp in saved], ["Design state machine", "Package & submit ZIP"])
        self.assertEqual([cp.phase for cp in saved], ["RESEARCH", "SUBMIT"])
        self.assertEqual([cp.estimated_minutes for cp in saved], [35, 25])

        for cp in saved:
            self.assertTrue(cp.is_ai_generated)
            self.assertLess(cp.target_deadline, self.task.deadline)

        # Evenly spaced chronologically before task.deadline
        self.assertLess(saved[0].target_deadline, saved[1].target_deadline)

        html = resp.content.decode()
        self.assertIn("Design state machine", html)
        self.assertNotIn("Unchecked Optional Step", html)
        self.assertIn('hx-swap-oob="true"', html)

    def test_apply_ai_checkpoints_with_fallback_source_sets_is_ai_generated_false(self):
        self.client.force_login(self.user)
        apply_url = reverse("tasks:apply_ai_checkpoints", kwargs={"pk": self.task.pk})
        resp = self.client.post(
            apply_url,
            data={
                "source": "fallback",
                "selected": ["0"],
                "title_0": "Fallback Step 1",
                "phase_0": "DRAFT",
                "estimated_minutes_0": "40",
            },
        )
        self.assertRedirects(resp, reverse("tasks:task_detail", kwargs={"pk": self.task.pk}))
        cp = Checkpoint.objects.get(task=self.task, title="Fallback Step 1")
        self.assertFalse(cp.is_ai_generated)
        self.assertLess(cp.target_deadline, self.task.deadline)

    def test_cross_user_404_isolation_on_ai_endpoints(self):
        self.client.force_login(self.other_user)
        preview_url = reverse("tasks:preview_ai_checkpoints", kwargs={"pk": self.task.pk})
        apply_url = reverse("tasks:apply_ai_checkpoints", kwargs={"pk": self.task.pk})

        self.assertEqual(self.client.post(preview_url).status_code, 404)
        self.assertEqual(
            self.client.post(
                apply_url,
                data={"source": "ai", "selected": ["0"], "title_0": "Injected"},
            ).status_code,
            404,
        )
        self.assertEqual(self.task.checkpoints.count(), 0)
