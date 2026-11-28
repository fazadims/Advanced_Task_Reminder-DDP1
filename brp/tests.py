from django.test import TestCase
from django.contrib.auth.models import User
from brp.models import Course, GradingComponent
from brp.services.matcher import (
    detect_category_from_title,
    match_task_to_brp,
    calculate_course_grade,
)


class BRPMatcherAndGradeTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="mahasiswa", password="password123")
        self.course = Course.objects.create(
            user=self.user,
            code="CSGE601020",
            name="Dasar-Dasar Pemrograman 1",
            sks=4,
        )
        self.comp_tp = GradingComponent.objects.create(
            course=self.course,
            name="Tugas Pemrograman",
            category="TP",
            weight_percentage=40.0,
            item_count=4,  # 10% per TP
        )
        self.comp_uts = GradingComponent.objects.create(
            course=self.course,
            name="UTS",
            category="UTS",
            weight_percentage=30.0,
            item_count=1,
        )
        self.comp_uas = GradingComponent.objects.create(
            course=self.course,
            name="UAS",
            category="UAS",
            weight_percentage=30.0,
            item_count=1,
        )

    def test_detect_category(self):
        self.assertEqual(detect_category_from_title("TP 1 - Python Dasar"), "TP")
        self.assertEqual(detect_category_from_title("Ujian Tengah Semester"), "UTS")

    def test_match_task_to_brp(self):
        comp, weight = match_task_to_brp(self.course, "Tugas Pemrograman 2")
        self.assertEqual(comp, self.comp_tp)
        self.assertEqual(weight, 10.0)

    def test_calculate_course_grade(self):
        graded = [
            {"title": "TP 1", "score": 90},   # 90% * 10% = 9.0
            {"title": "UTS", "score": 80},    # 80% * 30% = 24.0
        ]
        result = calculate_course_grade(self.course, graded)
        self.assertEqual(result["earned_points"], 33.0)
        self.assertEqual(result["completed_weight"], 40.0)
        self.assertEqual(result["current_average"], 82.5)
        self.assertEqual(result["projected_letter"], "A-")