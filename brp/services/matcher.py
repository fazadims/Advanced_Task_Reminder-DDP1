import re
from typing import Optional, Tuple
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