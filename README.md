# Advanced Task Reminder — Proyek Akhir DDP 1 (Django Workspace)

Repositori kerja tim untuk proyek **Advanced Task Reminder (ATR)** menggunakan framework **Django (Python)**.

## Struktur Folder (Apps)
- `config/` — Pengaturan utama Django (`settings.py`, `urls.py`)
- `core/` — Setup awal, autentikasi, preferensi user, integrasi SCELE/Google Calendar & Dashboard
- `brp/` — Manajemen mata kuliah & bobot komponen penilaian BRP
- `tasks/` — Manajemen tugas, checkpoint, Focus Mode (Pomodoro), eskalasi reminder, Daily Digest, Telegram Bot & AI fallback (`F3.1–F3.4`, `F4.1–F4.5`, `F6.2`)
- `analytics/` — Mesin analisis, skor prioritas & detektor minggu padat

## Cara Setup di Laptop Masing-Masing

1. **Clone repository:**
   ```bash
   git clone https://github.com/fazadims/Advanced_Task_Reminder-DDP1.git
   cd Advanced_Task_Reminder-DDP1
   ```

2. **Buat & aktifkan Virtual Environment:**
   - Windows (PowerShell):
     ```powershell
     python -m venv .venv
     .\.venv\Scripts\Activate.ps1
     ```
   - Windows (Git Bash):
     ```bash
     python -m venv .venv
     source .venv/Scripts/activate
     ```
   - macOS / Linux:
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```

3. **Install dependencies & siapkan `.env`:**
   ```bash
   pip install -r requirements.txt
   cp .env.example .env
   ```
   *(Biarkan `GEMINI_API_KEY` dan `TELEGRAM_BOT_TOKEN` kosong di `.env` jika ingin menjalankan aplikasi 100% offline dengan template bawaan).*

4. **Migrasi database, buat akun, & jalankan server:**
   ```bash
   python manage.py migrate
   python manage.py createsuperuser
   python manage.py runserver
   ```
   Buka browser di `http://127.0.0.1:8000/tasks/`.

## Management Commands (Scheduler & Bot Lokal)
- **Evaluasi & eskalasi reminder (F4.1):**
  ```bash
  python manage.py process_reminders
  ```
- **Kirim Daily Digest anti-spam (F4.3):**
  ```bash
  python manage.py send_daily_digest --slot morning --dry-run
  python manage.py send_daily_digest --slot morning
  ```
- **Jalankan Telegram Bot polling lokal tanpa webhook/ngrok (F4.4):**
  ```bash
  python manage.py run_telegram_bot
  ```

## Menjalankan Unit Test
```bash
python manage.py test
```

## Workflow Git Kelompok
- Buat/pindah ke branch fitur masing-masing sebelum commit:
  ```bash
  git checkout -b feat/nama-fitur
  ```
- Push ke branch pribadi dan buat Pull Request ke `main` agar tidak terjadi konflik antar anggota tim.

