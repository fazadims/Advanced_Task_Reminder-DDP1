# Advanced Task Reminder — Proyek Akhir DDP 1 (Django Workspace)

Repositori kerja tim untuk proyek **Advanced Task Reminder** menggunakan framework **Django (Python)**.

## Struktur Folder (Apps)
- `config/` — Pengaturan utama Django (`settings.py`, `urls.py`)
- `core/` — Setup awal, preferensi user, integrasi SCELE/Google Calendar & Dashboard
- `brp/` — Manajemen & analisis dokumen BRP
- `tasks/` — Manajemen tugas, sistem checkpoint & pelacakan progres
- `analytics/` — Mesin analisis, skor prioritas & detektor minggu padat

## Cara Setup di Laptop Masing-Masing

1. **Clone repository:**
   ```bash
   git clone https://github.com/fazadims/ddp1-lab-git.git
   cd ddp1-lab-git
   ```

2. **Buat & aktifkan Virtual Environment:**
   - Windows:
     ```powershell
     python -m venv .venv
     .venv\Scripts\activate
     ```
   - macOS / Linux:
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Migrasi database & jalankan server:**
   ```bash
   python manage.py migrate
   python manage.py runserver
   ```
   Buka browser di `http://127.0.0.1:8000/` untuk memastikan halaman starter Django berhasil berjalan.

## Workflow Git Kelompok
- Buat branch baru untuk setiap fitur yang dikerjakan:
  ```bash
  git checkout -b feat/nama-fitur
  ```
- Push branch dan buat Pull Request ke `main` agar tidak terjadi konflik antar anggota tim.
