"""Tests for F6.2 Task Detail Workspace & Paginated Completed Task History."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from brp.models import Course
from tasks.models import Checkpoint, FocusSession, Reminder, Task

User = get_user_model()


class TaskDetailAndHistoryTests(TestCase):
    """Covers F6.2 task detail rendering/isolation and history filtering, pagination, & variance badges."""

    def setUp(self):
        self.user_a = User.objects.create_user(
            username="history_a",
            email="history_a@ui.ac.id",
            password="StrongPass123!",
        )
        self.user_b = User.objects.create_user(
            username="history_b",
            email="history_b@ui.ac.id",
            password="StrongPass123!",
        )
        self.course_ddp = Course.objects.create(
            user=self.user_a,
            code="CSGE601020",
            name="DDP 1",
        )
        self.course_matdis = Course.objects.create(
            user=self.user_a,
            code="CSGE601010",
            name="Matematika Diskrit 1",
        )
        self.now = timezone.now()

    def test_task_detail_renders_metadata_checkpoints_focus_sessions_and_isolates_users(self):
        task = Task.objects.create(
            user=self.user_a,
            course=self.course_ddp,
            title="Proyek Akhir ATR",
            description="Complete Django application with HTMX and unit tests.",
            category="PROJECT",
            deadline=self.now + timedelta(days=5),
            estimated_minutes=240,
            weight_percentage=Decimal("30.00"),
            difficulty=4,
            is_group_project=True,
            priority_score=87.5,
            priority_level="HIGH",
            priority_reason="High course weight and approaching deadline.",
            status="IN_PROGRESS",
        )
        cp1 = Checkpoint.objects.create(
            task=task,
            title="Finalize schema contract",
            phase="RESEARCH",
            target_deadline=self.now + timedelta(days=1),
            estimated_minutes=60,
            is_done=True,
            completed_at=self.now - timedelta(hours=3),
            order=1,
        )
        cp2 = Checkpoint.objects.create(
            task=task,
            title="Write end-to-end tests",
            phase="REVIEW",
            target_deadline=self.now + timedelta(days=3),
            estimated_minutes=90,
            is_done=False,
            order=2,
        )
        session = FocusSession.objects.create(
            user=self.user_a,
            task=task,
            checkpoint=cp1,
            mode="DEEP_50_10",
            planned_minutes=50,
            actual_minutes=45,
            ended_at=self.now - timedelta(hours=2),
            status="COMPLETED",
        )
        Reminder.objects.create(
            user=self.user_a,
            task=task,
            checkpoint=cp2,
            scheduled_for=self.now + timedelta(hours=12),
            escalation_level=2,
            channel="WEB",
            message="Time to start writing end-to-end tests!",
            status="PENDING",
        )

        self.client.force_login(self.user_a)
        detail_url = reverse("tasks:task_detail", kwargs={"pk": task.pk})
        resp = self.client.get(detail_url)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["task"], task)
        self.assertEqual(list(resp.context["checkpoints"]), [cp1, cp2])
        self.assertEqual(list(resp.context["focus_sessions"]), [session])
        self.assertEqual(resp.context["total_focus_minutes"], 45)

        html = resp.content.decode()
        # Metadata
        self.assertIn("Proyek Akhir ATR", html)
        self.assertIn("CSGE601020", html)
        self.assertIn("Complete Django application with HTMX and unit tests.", html)
        self.assertIn("Group Project", html)
        self.assertIn("30.00%", html)
        self.assertIn("240 min", html)
        self.assertIn("45 min", html)
        self.assertIn("87.5", html)
        self.assertIn("High course weight and approaching deadline.", html)
        # Checkpoints & progress
        self.assertIn("Finalize schema contract", html)
        self.assertIn("Write end-to-end tests", html)
        self.assertIn("50%", html)
        # Focus session
        self.assertIn("Deep Work (50/10)", html)
        self.assertIn("45/50m", html)
        # Upcoming reminder
        self.assertIn("Time to start writing end-to-end tests!", html)

        # Cross-user 404 isolation
        self.client.force_login(self.user_b)
        resp_other = self.client.get(detail_url)
        self.assertEqual(resp_other.status_code, 404)

    def test_history_filters_only_done_tasks_and_paginates_15_per_page(self):
        # Create 18 DONE tasks for user_a with descending completion timestamps
        for i in range(1, 19):
            Task.objects.create(
                user=self.user_a,
                course=self.course_ddp if i <= 10 else self.course_matdis,
                title=f"Completed Task {i:02d}",
                category="TP",
                deadline=self.now - timedelta(days=i),
                estimated_minutes=60,
                actual_minutes=50,
                status="DONE",
                completed_at=self.now - timedelta(hours=i),
            )

        # Create non-DONE tasks for user_a and a DONE task for user_b (must all be excluded)
        Task.objects.create(
            user=self.user_a,
            course=self.course_ddp,
            title="Active TODO Task",
            deadline=self.now + timedelta(days=2),
            status="TODO",
        )
        Task.objects.create(
            user=self.user_a,
            course=self.course_ddp,
            title="In Progress Task",
            deadline=self.now + timedelta(days=2),
            status="IN_PROGRESS",
        )
        Task.objects.create(
            user=self.user_a,
            course=self.course_ddp,
            title="Archived Task",
            deadline=self.now - timedelta(days=2),
            status="ARCHIVED",
        )
        Task.objects.create(
            user=self.user_b,
            title="Other User Done Task",
            deadline=self.now - timedelta(days=1),
            status="DONE",
            completed_at=self.now - timedelta(minutes=30),
        )

        self.client.force_login(self.user_a)
        history_url = reverse("tasks:task_history")

        # Page 1: 15 items
        resp_p1 = self.client.get(history_url)
        self.assertEqual(resp_p1.status_code, 200)
        page1 = resp_p1.context["page_obj"]
        self.assertEqual(page1.paginator.count, 18)
        self.assertEqual(page1.paginator.num_pages, 2)
        self.assertEqual(len(page1.object_list), 15)
        self.assertTrue(page1.has_next())
        self.assertFalse(page1.has_previous())
        self.assertEqual(page1.object_list[0].title, "Completed Task 01")

        html_p1 = resp_p1.content.decode()
        self.assertIn("Page 1 of 2", html_p1)
        self.assertNotIn("Active TODO Task", html_p1)
        self.assertNotIn("In Progress Task", html_p1)
        self.assertNotIn("Archived Task", html_p1)
        self.assertNotIn("Other User Done Task", html_p1)

        # Page 2: remaining 3 items
        resp_p2 = self.client.get(history_url, {"page": 2})
        self.assertEqual(resp_p2.status_code, 200)
        page2 = resp_p2.context["page_obj"]
        self.assertEqual(len(page2.object_list), 3)
        self.assertTrue(page2.has_previous())
        self.assertFalse(page2.has_next())
        self.assertEqual(
            [t.title for t in page2.object_list],
            ["Completed Task 16", "Completed Task 17", "Completed Task 18"],
        )
        self.assertIn("Page 2 of 2", resp_p2.content.decode())

    def test_history_course_filter_and_variance_badges(self):
        # Faster task (120 est vs 90 actual via completed focus session -> Lebih cepat 30 mnt)
        faster_task = Task.objects.create(
            user=self.user_a,
            course=self.course_ddp,
            title="Fast Lab Submission",
            category="LAB",
            deadline=self.now - timedelta(days=1),
            estimated_minutes=120,
            actual_minutes=0,
            status="DONE",
            completed_at=self.now - timedelta(hours=2),
        )
        FocusSession.objects.create(
            user=self.user_a,
            task=faster_task,
            mode="DEEP_50_10",
            planned_minutes=50,
            actual_minutes=50,
            status="COMPLETED",
        )
        FocusSession.objects.create(
            user=self.user_a,
            task=faster_task,
            mode="CUSTOM",
            planned_minutes=40,
            actual_minutes=40,
            status="COMPLETED",
        )

        # Slower task (60 est vs 95 actual -> Lebih lama 35 mnt)
        slower_task = Task.objects.create(
            user=self.user_a,
            course=self.course_matdis,
            title="Challenging Proof Problem Set",
            category="TP",
            deadline=self.now - timedelta(days=2),
            estimated_minutes=60,
            actual_minutes=95,
            status="DONE",
            completed_at=self.now - timedelta(hours=4),
        )

        self.client.force_login(self.user_a)
        history_url = reverse("tasks:task_history")

        # Unfiltered shows both badges
        resp_all = self.client.get(history_url)
        self.assertEqual(resp_all.status_code, 200)
        html_all = resp_all.content.decode()
        self.assertIn("Fast Lab Submission", html_all)
        self.assertIn("Lebih cepat 30 mnt", html_all)
        self.assertIn("Challenging Proof Problem Set", html_all)
        self.assertIn("Lebih lama 35 mnt", html_all)

        # Filtered by course_ddp returns only faster_task
        resp_ddp = self.client.get(history_url, {"course": self.course_ddp.pk})
        self.assertEqual(resp_ddp.status_code, 200)
        ddp_tasks = list(resp_ddp.context["page_obj"].object_list)
        self.assertEqual(ddp_tasks, [faster_task])
        html_ddp = resp_ddp.content.decode()
        self.assertIn("Lebih cepat 30 mnt", html_ddp)
        self.assertNotIn("Challenging Proof Problem Set", html_ddp)

        # Filtered by course_matdis returns only slower_task
        resp_matdis = self.client.get(history_url, {"course": self.course_matdis.pk})
        self.assertEqual(resp_matdis.status_code, 200)
        matdis_tasks = list(resp_matdis.context["page_obj"].object_list)
        self.assertEqual(matdis_tasks, [slower_task])
        html_matdis = resp_matdis.content.decode()
        self.assertIn("Lebih lama 35 mnt", html_matdis)
        self.assertNotIn("Fast Lab Submission", html_matdis)
