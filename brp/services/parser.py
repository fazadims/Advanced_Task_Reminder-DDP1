import re
from typing import List, Dict
from pypdf import PdfReader
from brp.services.matcher import detect_category_from_title


def extract_text_from_pdf(pdf_file) -> str:
    """Membaca seluruh teks dari file PDF yang diunggah."""
    reader = PdfReader(pdf_file)
    pages_text = []
    for page in reader.pages:
        content = page.extract_text()
        if content:
            pages_text.append(content)
    return "\n".join(pages_text)


def parse_brp_components(raw_text: str) -> List[Dict]:
    """
    Mengekstrak nama komponen dan bobot persentase dari teks BRP.
    Mendukung pola seperti:
    - Tugas Pemrograman (TP 1-4): 24%
    - UTS 30%
    - Kuis - 15.5%
    """
    results = []
    seen_names = set()

    pattern = re.compile(
        r"([A-Za-z0-9\s\(\)\-\/&]{3,60}?)\s*[:\-–]?\s*(\d{1,3}(?:[.,]\d{1,2})?)\s*%"
    )

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue

        matches = pattern.findall(line)
        for raw_name, raw_weight in matches:
            clean_name = re.sub(r"^[\-\*\d\.\)\s]+", "", raw_name).strip()
            if len(clean_name) < 2 or clean_name.lower() in seen_names:
                continue

            weight = float(raw_weight.replace(",", "."))
            if not (0 < weight <= 100):
                continue

            item_count = 1
            range_match = re.search(r"1\s*[\-–]\s*(\d+)", clean_name)
            if range_match:
                item_count = max(1, int(range_match.group(1)))

            category = detect_category_from_title(clean_name)
            seen_names.add(clean_name.lower())

            results.append({
                "name": clean_name,
                "category": category,
                "weight_percentage": weight,
                "item_count": item_count,
            })

    return results