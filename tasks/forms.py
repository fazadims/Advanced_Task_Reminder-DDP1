"""Forms for Task CRUD and Checkpoint management (F3.1)."""

from decimal import Decimal

from django import forms

from brp.models import Course
from tasks.models import Checkpoint, Task

DATETIME_FORMAT = "%Y-%m-%dT%H:%M"
DATETIME_INPUT_FORMATS = [
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
]

INPUT_CLASS = (
    "w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm "
    "text-slate-900 focus:border-indigo-500 focus:outline-none "
    "focus:ring-2 focus:ring-indigo-500/20"
)
CHECKBOX_CLASS = "h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"


class TaskForm(forms.ModelForm):
    """ModelForm for creating and editing an academic Task."""

    deadline = forms.DateTimeField(
        required=True,
        input_formats=DATETIME_INPUT_FORMATS,
        error_messages={
            "required": "Tenggat waktu wajib diisi.",
            "invalid": "Format tanggal dan waktu tidak valid.",
        },
        widget=forms.DateTimeInput(
            format=DATETIME_FORMAT,
            attrs={"type": "datetime-local", "class": INPUT_CLASS},
        ),
        label="Tenggat Waktu (Deadline)",
        help_text="Gunakan tombol cepat di bawah atau pilih tanggal & jam secara manual.",
    )
    auto_generate_checkpoints = forms.BooleanField(
        required=False,
        initial=True,
        label="Buat checkpoint otomatis",
        help_text="Otomatis membagi tugas menjadi 3–4 langkah bertahap sebelum deadline.",
        widget=forms.CheckboxInput(attrs={"class": CHECKBOX_CLASS}),
    )

    class Meta:
        model = Task
        fields = [
            "title",
            "course",
            "category",
            "deadline",
            "estimated_minutes",
            "weight_percentage",
            "difficulty",
            "is_group_project",
            "description",
            "status",
        ]
        labels = {
            "title": "Judul Tugas",
            "course": "Mata Kuliah",
            "category": "Kategori Tugas",
            "deadline": "Tenggat Waktu (Deadline)",
            "estimated_minutes": "Estimasi Durasi (menit)",
            "weight_percentage": "Bobot Nilai BRP (%)",
            "difficulty": "Tingkat Kesulitan",
            "is_group_project": "Ini adalah proyek kelompok",
            "description": "Catatan / Link Soal",
            "status": "Status Pengerjaan",
        }
        error_messages = {
            "title": {
                "required": "Judul tugas wajib diisi.",
            },
        }
        help_texts = {
            "course": "Opsional. Pilih mata kuliah atau klik '+ Tambah Mata Kuliah' di kanan atas.",
            "deadline": "Gunakan tombol cepat di bawah atau pilih tanggal & jam secara manual.",
            "estimated_minutes": "Perkiraan total waktu pengerjaan. Klik chip durasi cepat agar lebih praktis.",
            "weight_percentage": "Lihat persentase nilai di BRP. Biarkan 0% jika belum tahu.",
            "difficulty": "Pilih skala 1–5 sesuai perkiraan kerumitan tugas.",
        }
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "Contoh: Tugas Pemrograman 2 - Struktur Data",
                }
            ),
            "course": forms.Select(attrs={"class": INPUT_CLASS}),
            "category": forms.Select(attrs={"class": INPUT_CLASS}),
            "estimated_minutes": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": "0", "step": "5"}
            ),
            "weight_percentage": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": "0", "max": "100", "step": "0.01"}
            ),
            "difficulty": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": "1", "max": "5"}
            ),
            "is_group_project": forms.CheckboxInput(attrs={"class": CHECKBOX_CLASS}),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 3,
                    "placeholder": "Catatan tambahan, link soal, atau kriteria penilaian...",
                }
            ),
            "status": forms.Select(attrs={"class": INPUT_CLASS}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["course"].queryset = Course.objects.filter(user=user)
        else:
            self.fields["course"].queryset = Course.objects.none()
        self.fields["course"].required = False
        self.fields["course"].empty_label = "— Tanpa Mata Kuliah —"
        self.fields["category"].required = False
        self.fields["estimated_minutes"].required = False
        self.fields["weight_percentage"].required = False
        self.fields["difficulty"].required = False
        self.fields["status"].required = False
        if self.instance and self.instance.pk:
            # Auto-generation applies to creation only; editing uses the dedicated regenerate button.
            self.fields.pop("auto_generate_checkpoints", None)

    def clean_deadline(self):
        deadline = self.cleaned_data.get("deadline")
        if not deadline:
            raise forms.ValidationError("Deadline wajib diisi.")
        return deadline

    def clean_category(self):
        value = self.cleaned_data.get("category")
        if not value:
            return self.instance.category if self.instance.pk else "TP"
        return value

    def clean_estimated_minutes(self):
        value = self.cleaned_data.get("estimated_minutes")
        if value is None:
            return self.instance.estimated_minutes if self.instance.pk else 120
        if value < 0 or value > 10080:
            raise forms.ValidationError("Estimasi waktu harus antara 0 dan 10.080 menit (1 minggu).")
        return value

    def clean_weight_percentage(self):
        value = self.cleaned_data.get("weight_percentage")
        if value is None:
            return self.instance.weight_percentage if self.instance.pk else Decimal("0.00")
        if value < Decimal("0") or value > Decimal("100"):
            raise forms.ValidationError("Bobot nilai harus antara 0 dan 100%.")
        return value

    def clean_difficulty(self):
        value = self.cleaned_data.get("difficulty")
        if value is None:
            return self.instance.difficulty if self.instance.pk else 3
        if value < 1 or value > 5:
            raise forms.ValidationError("Tingkat kesulitan harus antara 1 dan 5.")
        return value

    def clean_status(self):
        value = self.cleaned_data.get("status")
        if not value:
            return self.instance.status if self.instance.pk else "TODO"
        return value


class CheckpointForm(forms.ModelForm):
    """ModelForm for creating and editing a Checkpoint milestone."""

    target_deadline = forms.DateTimeField(
        required=True,
        input_formats=DATETIME_INPUT_FORMATS,
        widget=forms.DateTimeInput(
            format=DATETIME_FORMAT,
            attrs={"type": "datetime-local", "class": INPUT_CLASS},
        ),
        label="Target Tenggat",
    )

    class Meta:
        model = Checkpoint
        fields = ["title", "phase", "target_deadline", "estimated_minutes"]
        labels = {
            "title": "Judul Checkpoint",
            "phase": "Fase Pengerjaan",
            "target_deadline": "Target Tenggat",
            "estimated_minutes": "Estimasi Durasi (menit)",
        }
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "Contoh: Implementasi fungsi utama",
                }
            ),
            "phase": forms.Select(attrs={"class": INPUT_CLASS}),
            "estimated_minutes": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": "1", "max": "600", "step": "5"}
            ),
        }

    def __init__(self, *args, task=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.task = task or (
            self.instance.task if (self.instance and self.instance.pk and self.instance.task_id) else None
        )
        self.fields["phase"].required = False
        self.fields["estimated_minutes"].required = False

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise forms.ValidationError("Judul checkpoint wajib diisi.")
        return title[:200]

    def clean_target_deadline(self):
        target_deadline = self.cleaned_data.get("target_deadline")
        if not target_deadline:
            raise forms.ValidationError("Target deadline checkpoint wajib diisi.")
        if self.task is not None and target_deadline > self.task.deadline:
            raise forms.ValidationError(
                "Target deadline checkpoint tidak boleh melewati deadline tugas."
            )
        return target_deadline

    def clean_phase(self):
        value = self.cleaned_data.get("phase")
        if not value:
            return self.instance.phase if self.instance.pk else "CUSTOM"
        return value

    def clean_estimated_minutes(self):
        value = self.cleaned_data.get("estimated_minutes")
        if value is None:
            return self.instance.estimated_minutes if self.instance.pk else 30
        if value < 1 or value > 600:
            raise forms.ValidationError("Estimasi checkpoint harus antara 1 dan 600 menit.")
        return value

