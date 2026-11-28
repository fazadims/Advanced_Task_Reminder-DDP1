import re
from typing import Dict, List, Optional, Tuple
from brp.models import Course, GradingComponent


KEYWORD_CATEGORY_MAP = [
    ("PROYEK", [r"\bproyek\b", r"\bproject\b", r"\bfinal\s*project\b", r"\btubes\b"]),
    ("UTS", [r"\buts\b", r"\bmidterm\b", r"\bujian\s*tengah\b"]),
    ("UAS", [r"\buas\b", r"\bfinal\s*exam\b", r"\bujian\s*akhir\b"]),
    ("LAB", [r"\blab\b", r"\bpraktikum\b", r"\bhands[\s-]*on\b"]),
    ("KUIS", [r"\bkuis\b", r"\bquiz\b", r"\bpost[\s-]*test\b", r"\bpre[\s-]*test\b"]),
    ("TP", [r"\btp\b", r"\btugas\b", r"\bassignment\b", r"\bpr\b", r"\bworksheet\b"]),
]


def detect_category_from_title(task_title: str) -> str:
    """Mendeteksi kategori tugas dari judul menggunakan regex deterministik."""
    normalized = task_title.lower().strip()
    for category, patterns in KEYWORD_CATEGORY_MAP:
        for pattern in patterns:
            if re.search(pattern, normalized):
                return category
    return "LAINNYA"


def match_task_to_brp(
    course: Course, task_title: str
) -> Tuple[Optional[GradingComponent], float]:
    """
    Mencocokkan tugas ke GradingComponent di mata kuliah terkait.
    Mengembalikan tuple: (GradingComponent atau None, estimasi_bobot_persen).
    """
    components = list(course.components.all())
    if not components:
        return None, 0.0

    normalized_title = task_title.lower().strip()

    # 1. Cek kecocokan nama komponen secara langsung
    for comp in components:
        comp_name_clean = comp.name.lower()
        if any(word in normalized_title for word in comp_name_clean.split() if len(word) >= 2):
            return comp, comp.weight_per_item

    # 2. Deteksi kategori lewat keyword map
    detected_cat = detect_category_from_title(task_title)
    for comp in components:
        if comp.category == detected_cat:
            return comp, comp.weight_per_item

    return None, 0.0


def get_task_weight_info(course: Course, task_title: str) -> Dict:
    """
    Helper siap pakai untuk app `tasks` saat membuat/menampilkan tugas.
    Mengembalikan metadata komponen BRP dan bobot per item tugas.
    """
    component, weight = match_task_to_brp(course, task_title)
    if component:
        return {
            "matched": True,
            "component_id": component.id,
            "component_name": component.name,
            "category": component.category,
            "weight_per_item": weight,
            "total_component_weight": component.weight_percentage,
        }
    return {
        "matched": False,
        "component_id": None,
        "component_name": "Tidak Terkategori",
        "category": "LAINNYA",
        "weight_per_item": 0.0,
        "total_component_weight": 0.0,
    }


def score_to_letter_grade(score: float) -> str:
    """Konversi nilai angka (0-100) ke indeks huruf standar UI."""
    if score >= 85:
        return "A"
    if score >= 80:
        return "A-"
    if score >= 75:
        return "B+"
    if score >= 70:
        return "B"
    if score >= 65:
        return "B-"
    if score >= 60:
        return "C+"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "E"


def calculate_course_grade(course: Course, graded_tasks: List[Dict]) -> Dict:
    """
    Menghitung akumulasi nilai mata kuliah berdasarkan daftar tugas yang sudah dinilai.
    Parameter `graded_tasks`: list of dict, contoh:
        [{"title": "TP 1", "score": 90.0}, {"title": "Lab 1", "score": 85.0}]
    """
    earned_points = 0.0
    completed_weight = 0.0
    breakdown = []

    for item in graded_tasks:
        title = item.get("title", "")
        raw_score = item.get("score")
        if raw_score is None:
            continue

        score = max(0.0, min(100.0, float(raw_score)))
        component, item_weight = match_task_to_brp(course, title)
        contribution = round((score / 100.0) * item_weight, 2)

        earned_points += contribution
        completed_weight += item_weight
        breakdown.append({
            "title": title,
            "component_name": component.name if component else "Tidak cocok dengan BRP",
            "score": score,
            "weight": item_weight,
            "contribution": contribution,
        })

    earned_points = round(earned_points, 2)
    completed_weight = round(completed_weight, 2)
    current_average = round((earned_points / completed_weight) * 100, 2) if completed_weight > 0 else 0.0

    return {
        "course_code": course.code,
        "course_name": course.name,
        "earned_points": earned_points,
        "completed_weight": completed_weight,
        "remaining_weight": round(max(0.0, 100.0 - completed_weight), 2),
        "current_average": current_average,
        "projected_letter": score_to_letter_grade(current_average if completed_weight > 0 else 0.0),
        "breakdown": breakdown,
    }