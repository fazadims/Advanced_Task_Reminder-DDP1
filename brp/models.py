from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError


class Course(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="courses")
    code = models.CharField(max_length=20, help_text="Contoh: CSGE601020")
    name = models.CharField(max_length=100, help_text="Contoh: Dasar-Dasar Pemrograman 1")
    sks = models.PositiveSmallIntegerField(default=4)
    semester = models.CharField(max_length=30, default="Gasal 2026/2027")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "code")
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} - {self.name}"

    @property
    def total_weight(self) -> float:
        return sum(comp.weight_percentage for comp in self.components.all())

    @property
    def is_weight_valid(self) -> bool:
        return abs(self.total_weight - 100.0) < 0.01


class GradingComponent(models.Model):
    CATEGORY_CHOICES = [
        ("TP", "Tugas Pemrograman / Tugas"),
        ("LAB", "Lab / Praktikum"),
        ("KUIS", "Kuis"),
        ("PROYEK", "Proyek Akhir / Kelompok"),
        ("UTS", "Ujian Tengah Semester (UTS)"),
        ("UAS", "Ujian Akhir Semester (UAS)"),
        ("PARTISIPASI", "Partisipasi / Keaktifan"),
        ("LAINNYA", "Lainnya"),
    ]

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="components")
    name = models.CharField(max_length=100, help_text="Contoh: Tugas Pemrograman 1-4")
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="TP")
    weight_percentage = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(100.0)]
    )
    item_count = models.PositiveSmallIntegerField(
        default=1,
        help_text="Jumlah item dalam komponen ini (misal: 4 TP -> bobot per TP dibagi 4)"
    )

    class Meta:
        ordering = ["-weight_percentage", "name"]

    def __str__(self):
        return f"{self.course.name} - {self.name} ({self.weight_percentage}%)"

    @property
    def weight_per_item(self) -> float:
        if self.item_count > 0:
            return round(self.weight_percentage / self.item_count, 2)
        return self.weight_percentage

    def clean(self):
        existing_total = sum(
            c.weight_percentage for c in self.course.components.exclude(pk=self.pk)
        )
        if existing_total + self.weight_percentage > 100.01:
            raise ValidationError(
                f"Total bobot melebihi 100% (saat ini sudah {existing_total}%)."
            )