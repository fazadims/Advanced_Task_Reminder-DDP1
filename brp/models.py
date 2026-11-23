"""Canonical BRP models (CP 0 Shared Schema Contract) so tasks.Task ForeignKeys resolve cleanly."""

from django.conf import settings
from django.db import models


class Course(models.Model):
    """Represents a university course (Mata Kuliah) owned by a student."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="courses",
    )
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=150)
    credits = models.PositiveSmallIntegerField(default=3)
    semester = models.CharField(max_length=40, default="Gasal 2026/2027")
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code", "name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "code"], name="unique_user_course_code"),
        ]

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"


class GradingComponent(models.Model):
    """Represents a BRP grading weight component (e.g., TP 20%, Lab 15%)."""

    CATEGORY_CHOICES = [
        ("TUGAS", "Tugas / TP"),
        ("LAB", "Lab / Praktikum"),
        ("KUIS", "Kuis"),
        ("PROYEK", "Proyek Kelompok"),
        ("UTS", "UTS"),
        ("UAS", "UAS"),
        ("READING", "Reading / Bacaan"),
        ("LAINNYA", "Lainnya"),
    ]

    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="components",
    )
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="TUGAS")
    weight_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    keyword_pattern = models.CharField(max_length=150, blank=True, default="")

    class Meta:
        ordering = ["-weight_percentage", "name"]

    def __str__(self) -> str:
        return f"{self.course.code} | {self.name} ({self.weight_percentage}%)"
