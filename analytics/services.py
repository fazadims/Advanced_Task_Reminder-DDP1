"""Mesin analisis ATR: estimasi jam, skor prioritas, crunch week, slot kosong.

Sengaja ditulis sebagai fungsi murni (tanpa query DB) agar mudah dites.
Objek `task` hanya perlu punya: category, weight, deadline, est_hours.
"""
from datetime import datetime, time, timedelta

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


# ---------------------------------------------------------------------------
# Rekomendasi waktu pengerjaan
# ---------------------------------------------------------------------------
DEFAULT_PREFS = {
    "wake_hour": 8,          # mulai jam produktif / bangun
    "sleep_hour": 22,        # batas akhir (jam tidur/istirahat)
    "session_minutes": 90,   # durasi maksimal satu sesi
    "break_minutes": 15,     # jeda setelah satu sesi
    "max_hours_per_day": 4,  # batas jam belajar per hari
}
MIN_SESSION = timedelta(minutes=30)


def sample_busy_blocks(start_date, days, tzinfo):
    """DATA PALSU: kuliah Senin-Jumat 08-10 dan 13-15.

    GANTI dengan jadwal asli dari Google Calendar / jadwal kuliah rutin
    (bagian anggota tim lain). Bentuknya cukup list of (start, end) aware datetime.
    """
    busy = []
    for i in range(days):
        d = start_date + timedelta(days=i)
        if d.weekday() < 5:
            for h1, h2 in ((8, 10), (13, 15)):
                busy.append((
                    datetime.combine(d, time(h1), tzinfo=tzinfo),
                    datetime.combine(d, time(h2), tzinfo=tzinfo),
                ))
    return busy


def recommend_slots(task, busy, now, prefs=None, max_days=7):
    """Susun sesi belajar untuk `task` di celah kosong sebelum deadline.

    Aturan: hanya antara wake_hour dan sleep_hour, menghindari `busy`,
    sesi maksimal session_minutes, ada jeda, dan batas jam per hari.
    Return {"sessions": [{"start", "end"}], "unscheduled_hours": float}.
    """
    p = {**DEFAULT_PREFS, **(prefs or {})}
    tz = now.tzinfo
    session = timedelta(minutes=p["session_minutes"])
    brk = timedelta(minutes=p["break_minutes"])
    remaining = timedelta(hours=task_hours(task))
    sessions = []

    last_day = min(task.deadline.date(), now.date() + timedelta(days=max_days - 1))
    day = now.date()
    while day <= last_day and remaining > timedelta(0):
        start = max(datetime.combine(day, time(p["wake_hour"]), tzinfo=tz), now)
        end = min(datetime.combine(day, time(p["sleep_hour"]), tzinfo=tz), task.deadline)
        budget = timedelta(hours=p["max_hours_per_day"])
        day_busy = [(s, e) for s, e in busy if e > start and s < end]

        for slot_start, slot_end in find_free_slots(day_busy, start, end, 30):
            cursor = slot_start
            while remaining > timedelta(0) and budget > timedelta(0) and cursor < slot_end:
                length = min(session, slot_end - cursor, remaining, budget)
                if length <= timedelta(0) or (length < MIN_SESSION and length < remaining):
                    break
                sessions.append({"start": cursor, "end": cursor + length})
                remaining -= length
                budget -= length
                cursor += length + brk
        day += timedelta(days=1)

    return {
        "sessions": sessions,
        "unscheduled_hours": round(max(remaining.total_seconds(), 0) / 3600, 1),
    }
