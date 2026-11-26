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


class RecommendTest(TestCase):
    NOW = datetime(2026, 12, 1, 6, 0, tzinfo=timezone.utc)  # Selasa 06:00

    def _busy(self, days=8):
        return s.sample_busy_blocks(self.NOW.date(), days, timezone.utc)

    def _task(self, hours_cat="Tugas", weight=0, days=3):
        return SimpleNamespace(category=hours_cat, weight=weight,
                               deadline=self.NOW + timedelta(days=days), est_hours=None)

    def test_sessions_cover_needed_hours(self):
        task = self._task("Tugas", 0, 3)  # 4 jam
        res = s.recommend_slots(task, self._busy(), self.NOW)
        total = sum((x["end"] - x["start"]).total_seconds() for x in res["sessions"]) / 3600
        self.assertEqual(total, 4.0)
        self.assertEqual(res["unscheduled_hours"], 0)

    def test_sessions_avoid_busy_blocks(self):
        task = self._task("Tugas", 0, 3)
        busy = self._busy()
        for x in s.recommend_slots(task, busy, self.NOW)["sessions"]:
            for b_start, b_end in busy:
                self.assertFalse(x["start"] < b_end and b_start < x["end"])

    def test_respects_wake_and_sleep_hours(self):
        task = self._task("Proyek", 0, 6)
        for x in s.recommend_slots(task, [], self.NOW)["sessions"]:
            self.assertGreaterEqual(x["start"].hour, 8)
            self.assertLessEqual(x["end"].hour * 60 + x["end"].minute, 22 * 60)

    def test_daily_cap(self):
        task = self._task("Proyek", 0, 6)  # 20 jam
        res = s.recommend_slots(task, [], self.NOW)
        per_day = {}
        for x in res["sessions"]:
            d = x["start"].date()
            per_day[d] = per_day.get(d, 0) + (x["end"] - x["start"]).total_seconds() / 3600
        self.assertTrue(all(h <= 4 for h in per_day.values()))

    def test_unscheduled_when_deadline_too_close(self):
        task = self._task("Proyek", 0, 1)  # 20 jam, deadline 1 hari
        res = s.recommend_slots(task, self._busy(), self.NOW)
        self.assertGreater(res["unscheduled_hours"], 0)

    def test_sample_busy_skips_weekend(self):
        # 1 Des 2026 = Selasa; 7 hari -> 5 hari kerja x 2 blok
        self.assertEqual(len(self._busy(7)), 10)
