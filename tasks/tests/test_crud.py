"""Tests for F3.1 Task & Checkpoint CRUD, filtering, isolation, and HTMX toggling."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from brp.models import Course
from tasks.models import Checkpoint, Task

User = get_user_model()


class TaskCRUDAndCheckpointTests(TestCase):
    """Covers F3.1: auth redirects, filtering, CRUD, archive, isolation, and HTMX toggle."""

    def setUp(self):
        self.password = "StrongPass123!"
        self.user_a = User.objects.create_user(
            username="student_a",
            email="a@ui.ac.id",
            password=self.password,
        )
        self.user_b = User.objects.create_user(
            username="student_b",
            email="b@ui.ac.id",
            password=self.password,
        )
        self.course_ddp = Course.objects.create(
            user=self.user_a,
            code="CSGE601020",
            name="Dasar-Dasar Pemrograman 1",
            credits=4,
        )
        self.course_sda = Course.objects.create(
            user=self.user_a,
            code="CSGE602040",
            name="Struktur Data & Algoritma",
            credits=4,
        )
        self.now = timezone.now()
        self.task_tp = Task.objects.create(
            user=self.user_a,
            course=self.course_ddp,
            title="TP 1 Turtle Graphics",
            category="TP",
            deadline=self.now + timedelta(days=5),
            estimated_minutes=180,
            weight_percentage=Decimal("15.00"),
            difficulty=3,
            status="TODO",
        )
        self.task_lab = Task.objects.create(
            user=self.user_a,
            course=self.course_sda,
            title="Lab 3 Linked List",
            category="LAB",
            deadline=self.now + timedelta(days=2),
            estimated_minutes=90,
            status="IN_PROGRESS",
        )
        self.task_done = Task.objects.create(
            user=self.user_a,
            course=self.course_ddp,
            title="Quiz 1 Python Basics",
            category="QUIZ",
            deadline=self.now - timedelta(days=1),
            estimated_minutes=45,
            status="DONE",
            completed_at=self.now - timedelta(hours=12),
        )
        self.task_archived = Task.objects.create(
            user=self.user_a,
            course=self.course_sda,
            title="Old Archived Assignment",
            category="OTHER",
            deadline=self.now + timedelta(days=10),
            status="ARCHIVED",
        )

    def _login_a(self):
        self.client.force_login(self.user_a)

    def _login_b(self):
        self.client.force_login(self.user_b)

    # ------------------------------------------------------------------
    # Authentication redirects for anonymous users
    # ------------------------------------------------------------------

    def test_anonymous_users_redirected_to_login(self):
        cp = Checkpoint.objects.create(
            task=self.task_tp,
            title="Initial Step",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(days=1),
            order=1,
        )
        get_urls = [
            reverse("tasks:task_list"),
            reverse("tasks:task_create"),
            reverse("tasks:task_detail", kwargs={"pk": self.task_tp.pk}),
            reverse("tasks:task_update", kwargs={"pk": self.task_tp.pk}),
        ]
        for url in get_urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, msg=f"Expected redirect for GET {url}")
            self.assertIn("/login/", resp["Location"])

        post_urls = [
            reverse("tasks:task_delete", kwargs={"pk": self.task_tp.pk}),
            reverse("tasks:task_archive", kwargs={"pk": self.task_tp.pk}),
            reverse("tasks:add_checkpoint", kwargs={"task_pk": self.task_tp.pk}),
            reverse("tasks:toggle_checkpoint", kwargs={"pk": cp.pk}),
        ]
        for url in post_urls:
            resp = self.client.post(url)
            self.assertEqual(resp.status_code, 302, msg=f"Expected redirect for POST {url}")
            self.assertIn("/login/", resp["Location"])

    # ------------------------------------------------------------------
    # Task list filtering (course, status, category)
    # ------------------------------------------------------------------

    def test_task_list_default_excludes_archived_and_other_users(self):
        other_task = Task.objects.create(
            user=self.user_b,
            title="User B Secret Task",
            category="TP",
            deadline=self.now + timedelta(days=3),
            status="TODO",
        )
        self._login_a()
        resp = self.client.get(reverse("tasks:task_list"))
        self.assertEqual(resp.status_code, 200)
        returned_ids = set(resp.context["tasks"].values_list("pk", flat=True))
        self.assertEqual(
            returned_ids,
            {self.task_tp.pk, self.task_lab.pk, self.task_done.pk},
        )
        self.assertNotIn(self.task_archived.pk, returned_ids)
        self.assertNotIn(other_task.pk, returned_ids)

    def test_task_list_filter_by_course_status_and_category(self):
        self._login_a()
        list_url = reverse("tasks:task_list")

        # Filter by course
        resp_course = self.client.get(list_url, {"course": self.course_ddp.pk})
        self.assertEqual(resp_course.status_code, 200)
        self.assertEqual(
            set(resp_course.context["tasks"].values_list("pk", flat=True)),
            {self.task_tp.pk, self.task_done.pk},
        )

        # Filter by status (including explicit ARCHIVED)
        resp_status = self.client.get(list_url, {"status": "IN_PROGRESS"})
        self.assertEqual(
            set(resp_status.context["tasks"].values_list("pk", flat=True)),
            {self.task_lab.pk},
        )
        resp_archived = self.client.get(list_url, {"status": "ARCHIVED"})
        self.assertEqual(
            set(resp_archived.context["tasks"].values_list("pk", flat=True)),
            {self.task_archived.pk},
        )

        # Filter by category
        resp_category = self.client.get(list_url, {"category": "LAB"})
        self.assertEqual(
            set(resp_category.context["tasks"].values_list("pk", flat=True)),
            {self.task_lab.pk},
        )

        # Combined filters
        resp_combo = self.client.get(
            list_url,
            {"course": self.course_ddp.pk, "status": "DONE", "category": "QUIZ"},
        )
        self.assertEqual(
            set(resp_combo.context["tasks"].values_list("pk", flat=True)),
            {self.task_done.pk},
        )

    # ------------------------------------------------------------------
    # Task create (with and without auto-generate checkpoints)
    # ------------------------------------------------------------------

    def test_task_create_with_auto_generate_checkpoints(self):
        self._login_a()
        deadline_str = (self.now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
        payload = {
            "title": "TP 2 Matrix Calculator",
            "course": self.course_ddp.pk,
            "category": "TP",
            "deadline": deadline_str,
            "estimated_minutes": "240",
            "weight_percentage": "20.00",
            "difficulty": "4",
            "description": "Implement matrix operations in Python.",
            "status": "TODO",
            "auto_generate_checkpoints": "on",
        }
        resp = self.client.post(reverse("tasks:task_create"), data=payload)
        created = Task.objects.get(title="TP 2 Matrix Calculator")
        self.assertRedirects(resp, reverse("tasks:task_detail", kwargs={"pk": created.pk}))
        self.assertEqual(created.user, self.user_a)
        self.assertEqual(created.course, self.course_ddp)
        self.assertEqual(created.checkpoints.count(), 4)
        self.assertTrue(created.reminders.exists())

    def test_task_create_without_auto_generate_checkpoints(self):
        self._login_a()
        deadline_str = (self.now + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M")
        payload = {
            "title": "Manual Lab Without Auto CP",
            "course": self.course_sda.pk,
            "category": "LAB",
            "deadline": deadline_str,
            "estimated_minutes": "60",
            "weight_percentage": "5.00",
            "difficulty": "2",
            "status": "TODO",
            # Omitting auto_generate_checkpoints represents an unchecked checkbox
        }
        resp = self.client.post(reverse("tasks:task_create"), data=payload)
        created = Task.objects.get(title="Manual Lab Without Auto CP")
        self.assertRedirects(resp, reverse("tasks:task_detail", kwargs={"pk": created.pk}))
        self.assertEqual(created.checkpoints.count(), 0)

    # ------------------------------------------------------------------
    # Task edit, delete, archive / unarchive
    # ------------------------------------------------------------------

    def test_task_update_edits_fields_and_completed_at(self):
        self._login_a()
        update_url = reverse("tasks:task_update", kwargs={"pk": self.task_tp.pk})
        self.assertEqual(self.client.get(update_url).status_code, 200)

        new_deadline = (self.now + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M")
        resp = self.client.post(
            update_url,
            data={
                "title": "TP 1 Updated Title",
                "course": self.course_sda.pk,
                "category": "PROJECT",
                "deadline": new_deadline,
                "estimated_minutes": "300",
                "weight_percentage": "25.00",
                "difficulty": "5",
                "description": "Updated requirements",
                "status": "DONE",
            },
        )
        self.assertRedirects(resp, reverse("tasks:task_detail", kwargs={"pk": self.task_tp.pk}))
        self.task_tp.refresh_from_db()
        self.assertEqual(self.task_tp.title, "TP 1 Updated Title")
        self.assertEqual(self.task_tp.course, self.course_sda)
        self.assertEqual(self.task_tp.category, "PROJECT")
        self.assertEqual(self.task_tp.status, "DONE")
        self.assertIsNotNone(self.task_tp.completed_at)

        # Moving status back to IN_PROGRESS clears completed_at
        self.client.post(
            update_url,
            data={
                "title": "TP 1 Updated Title",
                "course": self.course_sda.pk,
                "category": "PROJECT",
                "deadline": new_deadline,
                "estimated_minutes": "300",
                "weight_percentage": "25.00",
                "difficulty": "5",
                "status": "IN_PROGRESS",
            },
        )
        self.task_tp.refresh_from_db()
        self.assertEqual(self.task_tp.status, "IN_PROGRESS")
        self.assertIsNone(self.task_tp.completed_at)

    def test_task_delete_removes_task_and_redirects_to_list(self):
        self._login_a()
        delete_url = reverse("tasks:task_delete", kwargs={"pk": self.task_lab.pk})
        self.assertEqual(self.client.get(delete_url).status_code, 405)

        resp = self.client.post(delete_url)
        self.assertRedirects(resp, reverse("tasks:task_list"))
        self.assertFalse(Task.objects.filter(pk=self.task_lab.pk).exists())

    def test_task_archive_and_unarchive_toggles_status(self):
        self._login_a()
        archive_url = reverse("tasks:task_archive", kwargs={"pk": self.task_tp.pk})

        # Archive
        resp1 = self.client.post(archive_url)
        self.assertRedirects(resp1, reverse("tasks:task_detail", kwargs={"pk": self.task_tp.pk}))
        self.task_tp.refresh_from_db()
        self.assertEqual(self.task_tp.status, "ARCHIVED")

        # Unarchive
        resp2 = self.client.post(archive_url)
        self.assertRedirects(resp2, reverse("tasks:task_detail", kwargs={"pk": self.task_tp.pk}))
        self.task_tp.refresh_from_db()
        self.assertEqual(self.task_tp.status, "TODO")

    # ------------------------------------------------------------------
    # Cross-user 404 isolation
    # ------------------------------------------------------------------

    def test_cross_user_404_isolation_across_task_and_checkpoint_endpoints(self):
        cp = Checkpoint.objects.create(
            task=self.task_tp,
            title="Private Checkpoint",
            phase="DRAFT",
            target_deadline=self.now + timedelta(days=2),
            order=1,
        )
        self._login_b()

        # Detail
        resp_detail = self.client.get(reverse("tasks:task_detail", kwargs={"pk": self.task_tp.pk}))
        self.assertEqual(resp_detail.status_code, 404)

        # Edit (GET & POST)
        edit_url = reverse("tasks:task_update", kwargs={"pk": self.task_tp.pk})
        self.assertEqual(self.client.get(edit_url).status_code, 404)
        self.assertEqual(
            self.client.post(
                edit_url,
                data={
                    "title": "Hacked Title",
                    "deadline": (self.now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M"),
                },
            ).status_code,
            404,
        )
        self.task_tp.refresh_from_db()
        self.assertEqual(self.task_tp.title, "TP 1 Turtle Graphics")

        # Delete
        resp_del = self.client.post(reverse("tasks:task_delete", kwargs={"pk": self.task_tp.pk}))
        self.assertEqual(resp_del.status_code, 404)
        self.assertTrue(Task.objects.filter(pk=self.task_tp.pk).exists())

        # Archive
        resp_arch = self.client.post(reverse("tasks:task_archive", kwargs={"pk": self.task_tp.pk}))
        self.assertEqual(resp_arch.status_code, 404)
        self.task_tp.refresh_from_db()
        self.assertEqual(self.task_tp.status, "TODO")

        # Toggle checkpoint
        resp_toggle = self.client.post(
            reverse("tasks:toggle_checkpoint", kwargs={"pk": cp.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_toggle.status_code, 404)
        cp.refresh_from_db()
        self.assertFalse(cp.is_done)

        # Add checkpoint
        resp_add = self.client.post(
            reverse("tasks:add_checkpoint", kwargs={"task_pk": self.task_tp.pk}),
            data={"title": "Injected Checkpoint", "phase": "CUSTOM", "estimated_minutes": "20"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp_add.status_code, 404)
        self.assertEqual(self.task_tp.checkpoints.count(), 1)

    # ------------------------------------------------------------------
    # HTMX toggle_checkpoint progress bar OOB & task status transitions
    # ------------------------------------------------------------------

    def test_htmx_toggle_checkpoint_progress_and_status_transitions(self):
        self._login_a()
        cp1 = Checkpoint.objects.create(
            task=self.task_tp,
            title="Step 1 Research",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(days=1),
            order=1,
        )
        cp2 = Checkpoint.objects.create(
            task=self.task_tp,
            title="Step 2 Implement",
            phase="DRAFT",
            target_deadline=self.now + timedelta(days=2),
            order=2,
        )
        cp3 = Checkpoint.objects.create(
            task=self.task_tp,
            title="Step 3 Submit",
            phase="SUBMIT",
            target_deadline=self.now + timedelta(days=3),
            order=3,
        )

        # Start at 0% / TODO
        self.assertEqual(self.task_tp.progress_percent(), 0)
        self.assertEqual(self.task_tp.status, "TODO")

        # 0% -> 33%, TODO -> IN_PROGRESS
        resp1 = self.client.post(
            reverse("tasks:toggle_checkpoint", kwargs={"pk": cp1.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp1.status_code, 200)
        cp1.refresh_from_db()
        self.task_tp.refresh_from_db()
        self.assertTrue(cp1.is_done)
        self.assertIsNotNone(cp1.completed_at)
        self.assertEqual(self.task_tp.progress_percent(), 33)
        self.assertEqual(self.task_tp.status, "IN_PROGRESS")
        html1 = resp1.content.decode()
        self.assertIn(f'id="cp-{cp1.pk}"', html1)
        self.assertIn('hx-swap-oob="true"', html1)
        self.assertIn("33%", html1)

        # 33% -> 67%, stays IN_PROGRESS
        resp2 = self.client.post(
            reverse("tasks:toggle_checkpoint", kwargs={"pk": cp2.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp2.status_code, 200)
        self.task_tp.refresh_from_db()
        self.assertEqual(self.task_tp.progress_percent(), 67)
        self.assertEqual(self.task_tp.status, "IN_PROGRESS")
        html2 = resp2.content.decode()
        self.assertIn(f'id="cp-{cp2.pk}"', html2)
        self.assertIn('hx-swap-oob="true"', html2)
        self.assertIn("67%", html2)

        # 67% -> 100%, IN_PROGRESS -> DONE
        resp3 = self.client.post(
            reverse("tasks:toggle_checkpoint", kwargs={"pk": cp3.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp3.status_code, 200)
        self.task_tp.refresh_from_db()
        self.assertEqual(self.task_tp.progress_percent(), 100)
        self.assertEqual(self.task_tp.status, "DONE")
        self.assertIsNotNone(self.task_tp.completed_at)
        html3 = resp3.content.decode()
        self.assertIn(f'id="cp-{cp3.pk}"', html3)
        self.assertIn('hx-swap-oob="true"', html3)
        self.assertIn("100%", html3)

        # Untoggle cp3: 100% -> 67%, DONE -> IN_PROGRESS
        resp4 = self.client.post(
            reverse("tasks:toggle_checkpoint", kwargs={"pk": cp3.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp4.status_code, 200)
        cp3.refresh_from_db()
        self.task_tp.refresh_from_db()
        self.assertFalse(cp3.is_done)
        self.assertIsNone(cp3.completed_at)
        self.assertEqual(self.task_tp.progress_percent(), 67)
        self.assertEqual(self.task_tp.status, "IN_PROGRESS")
        self.assertIsNone(self.task_tp.completed_at)
        html4 = resp4.content.decode()
        self.assertIn('hx-swap-oob="true"', html4)
        self.assertIn("67%", html4)

    def test_htmx_add_checkpoint_appends_item_and_returns_oob_progress(self):
        self._login_a()
        resp = self.client.post(
            reverse("tasks:add_checkpoint", kwargs={"task_pk": self.task_tp.pk}),
            data={
                "title": "Draft edge-case tests",
                "phase": "REVIEW",
                "estimated_minutes": "45",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.task_tp.checkpoints.count(), 1)
        cp = self.task_tp.checkpoints.first()
        self.assertEqual(cp.title, "Draft edge-case tests")
        self.assertEqual(cp.phase, "REVIEW")
        self.assertEqual(cp.estimated_minutes, 45)
        html = resp.content.decode()
        self.assertIn('id="checkpoint-list"', html)
        self.assertIn('hx-swap-oob="true"', html)
