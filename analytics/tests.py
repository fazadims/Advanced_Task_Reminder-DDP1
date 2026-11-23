from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase

from . import services as s

NOW = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)


def mk(category="Tugas", weight=10, days=5, est=None):
    return SimpleNamespace(category=category, weight=weight,
                           deadline=NOW + timedelta(days=days), est_hours=est)


class EstimateTest(TestCase):
    def test_estimate_scales_with_weight(self):
        self.assertEqual(s.estimate_hours(mk("Tugas", 0)), 4.0)
        self.assertEqual(s.estimate_hours(mk("Tugas", 50)), 6.0)

    def test_unknown_category_uses_default(self):
        self.assertEqual(s.estimate_hours(mk("Aneh", 0)), 3.0)


class PriorityTest(TestCase):
    def test_close_heavy_task_is_high(self):
        score, reasons = s.priority_score(mk("Proyek", 30, days=1), NOW)
        self.assertEqual(s.priority_label(score)[0], "high")
        self.assertTrue(any("deadline" in r for r in reasons))

    def test_far_light_task_is_normal(self):
        score, _ = s.priority_score(mk("Kuis", 2, days=30), NOW)
        self.assertEqual(s.priority_label(score)[0], "normal")

    def test_rank_orders_descending(self):
        rows = s.rank_tasks([mk(days=20), mk("Proyek", 30, days=1)], NOW)
        self.assertGreater(rows[0]["score"], rows[1]["score"])

    def test_overdue_does_not_crash(self):
        score, _ = s.priority_score(mk(days=-3), NOW)
        self.assertGreater(score, 0)


class CrunchTest(TestCase):
    def test_detects_heavy_week(self):
        tasks = [mk("Proyek", 0, days=2), mk("UTS", 0, days=3)]  # 20 + 10 jam
        self.assertEqual(len(s.crunch_weeks(tasks, threshold=25)), 1)

    def test_light_week_ignored(self):
        self.assertEqual(s.crunch_weeks([mk("Kuis", 0, days=2)], 25), {})


class WorkloadTest(TestCase):
    def test_workload_length_and_sum(self):
        labels, hours = s.workload_by_day([mk("Tugas", 0, days=2)], NOW.date(), 14)
        self.assertEqual(len(labels), 14)
        self.assertEqual(hours[2], 4.0)


class FreeSlotTest(TestCase):
    def test_gaps(self):
        d = datetime(2026, 12, 1, tzinfo=timezone.utc)
        h = lambda x: d + timedelta(hours=x)
        slots = s.find_free_slots([(h(10), h(12)), (h(13), h(14))], h(8), h(18), 60)
        self.assertEqual(slots, [(h(8), h(10)), (h(12), h(13)), (h(14), h(18))])
