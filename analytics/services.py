"""Mesin analisis ATR: estimasi jam, skor prioritas, crunch week, slot kosong.

Sengaja ditulis sebagai fungsi murni (tanpa query DB) agar mudah dites.
Objek `task` hanya perlu punya: category, weight, deadline, est_hours.
"""
from datetime import timedelta

BASE_HOURS = {"Tugas": 4, "TP": 3, "Lab": 3, "Kuis": 2, "UTS": 10, "UAS": 12, "Proyek": 20}
DEFAULT_BASE_HOURS = 3

# Bobot rumus skor (jumlah = 1.0). Jelaskan di README.
W_WEIGHT, W_HOURS, W_URGENCY = 0.4, 0.2, 0.4
HIGH_THRESHOLD, MEDIUM_THRESHOLD = 25, 12


def estimate_hours(task):
    """Estimasi jam: jam dasar kategori, dikali (1 + bobot/100)."""
    base = BASE_HOURS.get(task.category, DEFAULT_BASE_HOURS)
    return round(base * (1 + task.weight / 100), 1)


def task_hours(task):
    return task.est_hours if task.est_hours else estimate_hours(task)


def days_left(task, now):
    return max((task.deadline - now).total_seconds() / 86400, 0.1)


def priority_score(task, now):
    """Return (skor, daftar_alasan).

    skor = 0.4*bobot + 0.2*(2*jam) + 0.4*(40/hari_tersisa)
    """
    d = days_left(task, now)
    hours = task_hours(task)
    score = (
        task.weight * W_WEIGHT
        + hours * 2 * W_HOURS
        + (40 / d) * W_URGENCY
    )
    reasons = []
    if d <= 2:
        reasons.append(f"deadline {d:.1f} hari lagi")
    if task.weight >= 15:
        reasons.append(f"bobot {task.weight:g}%")
    if hours >= 8:
        reasons.append(f"estimasi {hours:g} jam")
    if not reasons:
        reasons.append("deadline masih panjang / tugas ringan")
    return round(score, 1), reasons


def priority_label(score):
    """Return (kode, ikon, warna_css)."""
    if score >= HIGH_THRESHOLD:
        return "high", "🛑", "red"
    if score >= MEDIUM_THRESHOLD:
        return "medium", "🟡", "gold"
    return "normal", "🟢", "green"


def rank_tasks(tasks, now):
    """Urutkan tugas dari prioritas tertinggi; kembalikan list dict siap render."""
    rows = []
    for t in tasks:
        score, reasons = priority_score(t, now)
        code, icon, color = priority_label(score)
        rows.append({
            "task": t, "score": score, "code": code, "icon": icon,
            "color": color, "reasons": reasons, "hours": task_hours(t),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def workload_by_day(tasks, start_date, days=14):
    """Jam kerja per hari (dihitung pada hari deadline). Return (labels, hours)."""
    labels, hours = [], []
    for i in range(days):
        d = start_date + timedelta(days=i)
        labels.append(d.strftime("%d %b"))
        hours.append(round(sum(task_hours(t) for t in tasks if t.deadline.date() == d), 1))
    return labels, hours


def crunch_weeks(tasks, threshold=25):
    """Return {(tahun, minggu): total_jam} untuk minggu yang melebihi threshold."""
    weeks = {}
    for t in tasks:
        iso = t.deadline.isocalendar()
        key = (iso[0], iso[1])
        weeks[key] = weeks.get(key, 0) + task_hours(t)
    return {k: round(v, 1) for k, v in weeks.items() if v > threshold}


def find_free_slots(busy, day_start, day_end, min_minutes=60):
    """busy = list (start, end). Return celah kosong >= min_minutes."""
    slots, cursor = [], day_start
    for s, e in sorted(busy):
        if s - cursor >= timedelta(minutes=min_minutes):
            slots.append((cursor, s))
        cursor = max(cursor, e)
    if day_end - cursor >= timedelta(minutes=min_minutes):
        slots.append((cursor, day_end))
    return slots
