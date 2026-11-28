from django.db import transaction
from django.contrib.auth.models import User
from brp.models import Course, GradingComponent


FASIlKOM_DEMO_COURSES = [
    {
        "code": "CSGE601020",
        "name": "Dasar-Dasar Pemrograman 1",
        "sks": 4,
        "components": [
            {"name": "Tugas Pemrograman (TP 1-4)", "category": "TP", "weight_percentage": 24.0, "item_count": 4},
            {"name": "Lab Mingguan (Lab 0-10)", "category": "LAB", "weight_percentage": 16.0, "item_count": 10},
            {"name": "Kuis", "category": "KUIS", "weight_percentage": 10.0, "item_count": 2},
            {"name": "Proyek Akhir", "category": "PROYEK", "weight_percentage": 15.0, "item_count": 1},
            {"name": "UTS", "category": "UTS", "weight_percentage": 17.5, "item_count": 1},
            {"name": "UAS", "category": "UAS", "weight_percentage": 17.5, "item_count": 1},
        ],
    },
    {
        "code": "CSGE601010",
        "name": "Matematika Diskret 1",
        "sks": 3,
        "components": [
            {"name": "Tugas & PR", "category": "TP", "weight_percentage": 20.0, "item_count": 4},
            {"name": "Kuis", "category": "KUIS", "weight_percentage": 20.0, "item_count": 4},
            {"name": "UTS", "category": "UTS", "weight_percentage": 30.0, "item_count": 1},
            {"name": "UAS", "category": "UAS", "weight_percentage": 30.0, "item_count": 1},
        ],
    },
    {
        "code": "CSGE601011",
        "name": "Kalkulus 1",
        "sks": 3,
        "components": [
            {"name": "Tugas Kelompok & Worksheet", "category": "TP", "weight_percentage": 15.0, "item_count": 5},
            {"name": "Kuis", "category": "KUIS", "weight_percentage": 25.0, "item_count": 4},
            {"name": "UTS", "category": "UTS", "weight_percentage": 30.0, "item_count": 1},
            {"name": "UAS", "category": "UAS", "weight_percentage": 30.0, "item_count": 1},
        ],
    },
]


@transaction.atomic
def seed_demo_courses_and_brp(user: User) -> list[Course]:
    """Mengisi data contoh mata kuliah Fasilkom UI dan bobot BRP untuk Demo Mode."""
    created_courses = []
    for c_data in FASIlKOM_DEMO_COURSES:
        course, created = Course.objects.get_or_create(
            user=user,
            code=c_data["code"],
            defaults={
                "name": c_data["name"],
                "sks": c_data["sks"],
            },
        )
        if created:
            for comp in c_data["components"]:
                GradingComponent.objects.create(course=course, **comp)
        created_courses.append(course)
    return created_courses