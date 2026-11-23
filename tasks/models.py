"""Person B canonical models: Task, Checkpoint, FocusSession, Reminder, DailyDigestLog, TelegramLink."""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Count, Q, Sum
from django.utils import timezone

TELEGRAM_LINK_TOKEN_TTL_MINUTES = 30


class Task(models.Model):
    """Core academic assignment/task managed by a student (F3.1 & F6.2)."""

    CATEGORY_CHOICES = [
        ("LAB", "Lab / Praktikum"),
        ("TP", "Tugas Pemrograman (TP)"),
        ("QUIZ", "Kuis / Quiz"),
        ("EXAM", "Ujian (UTS/UAS)"),
        ("PROJECT", "Proyek Kelompok"),
        ("OTHER", "Lainnya"),
    ]

    STATUS_CHOICES = [
        ("TODO", "Belum Dikerjakan"),
        ("IN_PROGRESS", "Sedang Dikerjakan"),
        ("DONE", "Selesai"),
        ("ARCHIVED", "Diarsipkan"),
    ]

    PRIORITY_CHOICES = [
        ("CRITICAL", "Critical"),
        ("HIGH", "High"),
        ("MEDIUM", "Medium"),
        ("LOW", "Low"),
    ]

    SOURCE_CHOICES = [
        ("manual", "Manual"),
        ("ics", "SCELE / EMAS2 (.ics)"),
        ("demo", "Mode Demo"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tasks",
    )
    course = models.ForeignKey(
        "brp.Course",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
    )
    component = models.ForeignKey(
        "brp.GradingComponent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="TP")
    deadline = models.DateTimeField(db_index=True)
    estimated_minutes = models.PositiveIntegerField(default=120)
    actual_minutes = models.PositiveIntegerField(default=0)
    difficulty = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    is_group_project = models.BooleanField(default=False)
    weight_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
    )
    priority_score = models.FloatField(default=0.0)
    priority_level = models.CharField(
        max_length=15,
        choices=PRIORITY_CHOICES,
        default="MEDIUM",
    )
    priority_reason = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="TODO")
    completed_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual")
    external_uid = models.CharField(max_length=255, blank=True, default="", db_index=True)
    gcal_event_id = models.CharField(max_length=255, blank=True, default="")
    mapping_confidence = models.FloatField(default=0.0)
    needs_review = models.BooleanField(default=False)
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["deadline", "-priority_score"]
        indexes = [
            models.Index(fields=["user", "status", "deadline"], name="task_user_status_dl_idx"),
            models.Index(fields=["user", "status", "-completed_at"], name="task_user_status_comp_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "external_uid"],
                condition=~Q(external_uid=""),
                name="unique_user_external_uid",
            ),
        ]

    def __str__(self) -> str:
        prefix = f"[{self.course.code}] " if self.course_id else ""
        return f"{prefix}{self.title}"

    def clean(self) -> None:
        super().clean()
        if self.course_id and self.user_id and self.course.user_id != self.user_id:
            raise ValidationError({"course": "Selected course does not belong to this user."})
        if self.component_id:
            if not self.course_id or self.component.course_id != self.course_id:
                raise ValidationError(
                    {"component": "Selected grading component does not belong to the task's course."}
                )

    def progress_percent(self) -> int:
        """Returns 0-100 completion percentage based on checkpoints or task status."""
        checkpoints = list(self.checkpoints.all())
        total = len(checkpoints)
        if total == 0:
            return 100 if self.status == "DONE" else 0
        done = sum(1 for cp in checkpoints if cp.is_done)
        return round((done / total) * 100)

    def total_focus_minutes(self) -> int:
        """Returns sum of actual_minutes across all COMPLETED FocusSession logs.

        Uses pre-annotated ``logged_focus_minutes`` or prefetched ``focus_sessions``
        when available to avoid N+1 aggregate queries in list/history views.
        """
        annotated = getattr(self, "logged_focus_minutes", None)
        if annotated is not None:
            return int(annotated)
        prefetched = getattr(self, "_prefetched_objects_cache", None)
        if prefetched and "focus_sessions" in prefetched:
            return sum(
                int(s.actual_minutes or 0)
                for s in prefetched["focus_sessions"]
                if s.status == "COMPLETED"
            )
        agg = self.focus_sessions.filter(status="COMPLETED").aggregate(
            total=Sum("actual_minutes")
        )
        return int(agg["total"] or 0)

    @property
    def effective_actual_minutes(self) -> int:
        """Returns logged focus minutes or explicit actual_minutes."""
        logged = self.total_focus_minutes()
        return logged if logged > 0 else self.actual_minutes

    @property
    def duration_diff_minutes(self) -> int:
        """Positive = took longer than estimated; negative = faster than estimated."""
        return self.effective_actual_minutes - self.estimated_minutes

    @property
    def is_overdue(self) -> bool:
        return self.status not in ("DONE", "ARCHIVED") and self.deadline < timezone.now()

    def sync_status_from_checkpoints(self) -> str:
        """Recalculates parent task.status and completed_at from its checkpoints in one query."""
        if self.status == "ARCHIVED":
            return self.status

        counts = self.checkpoints.aggregate(
            total=Count("id"),
            done=Count("id", filter=Q(is_done=True)),
        )
        total = int(counts["total"] or 0)
        done = int(counts["done"] or 0)

        if total == 0:
            if self.status == "IN_PROGRESS":
                self.status = "TODO"
                self.completed_at = None
                self.save(update_fields=["status", "completed_at", "updated_at"])
            return self.status

        if done == total:
            self.status = "DONE"
            if self.completed_at is None:
                self.completed_at = timezone.now()
        elif done > 0:
            self.status = "IN_PROGRESS"
            self.completed_at = None
        else:
            self.status = "TODO"
            self.completed_at = None
        self.save(update_fields=["status", "completed_at", "updated_at"])
        return self.status


class Checkpoint(models.Model):
    """Actionable milestone / sub-task inside a Task (F3.1, F3.2, F3.4)."""

    PHASE_CHOICES = [
        ("RESEARCH", "Riset & Pahami Soal"),
        ("DRAFT", "Pengerjaan Inti / Draft"),
        ("REVIEW", "Review & Testing"),
        ("SUBMIT", "Finalisasi & Submit"),
        ("CUSTOM", "Kustom"),
    ]

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="checkpoints",
    )
    title = models.CharField(max_length=200)
    phase = models.CharField(max_length=20, choices=PHASE_CHOICES, default="CUSTOM")
    target_deadline = models.DateTimeField()
    estimated_minutes = models.PositiveIntegerField(default=30)
    is_done = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    order = models.PositiveSmallIntegerField(default=1)
    is_ai_generated = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "target_deadline", "id"]
        indexes = [
            models.Index(fields=["task", "is_done", "order"], name="cp_task_done_order_idx"),
            models.Index(fields=["is_done", "target_deadline"], name="cp_done_deadline_idx"),
        ]

    def __str__(self) -> str:
        mark = "x" if self.is_done else " "
        return f"[{mark}] {self.order}. {self.title}"


class FocusSession(models.Model):
    """Pomodoro / Deep Work focus session logged against a Task/Checkpoint (F3.3)."""

    MODE_CHOICES = [
        ("POMODORO_25_5", "Pomodoro (25/5)"),
        ("DEEP_50_10", "Deep Work (50/10)"),
        ("CUSTOM", "Custom"),
    ]

    STATUS_CHOICES = [
        ("PLANNED", "Terjadwal"),
        ("RUNNING", "Berjalan"),
        ("COMPLETED", "Selesai"),
        ("ABANDONED", "Dibatalkan"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="focus_sessions",
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="focus_sessions",
    )
    checkpoint = models.ForeignKey(
        Checkpoint,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="focus_sessions",
    )
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default="POMODORO_25_5")
    planned_minutes = models.PositiveSmallIntegerField(default=25)
    actual_minutes = models.PositiveSmallIntegerField(default=0)
    planned_start_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="RUNNING")

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["user", "status", "started_at"], name="focus_user_stat_start_idx"),
            models.Index(fields=["task", "status"], name="focus_task_status_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        if self.task_id and self.user_id and self.task.user_id != self.user_id:
            raise ValidationError({"task": "FocusSession task must belong to the same user."})
        if self.checkpoint_id and self.task_id and self.checkpoint.task_id != self.task_id:
            raise ValidationError(
                {"checkpoint": "FocusSession checkpoint must belong to the same task."}
            )

    def __str__(self) -> str:
        return f"{self.task.title} ({self.mode} - {self.status})"


class Reminder(models.Model):
    """Progressive checkpoint reminder with 3-tier urgency escalation (F4.1, F4.2, F4.5)."""

    ESCALATION_CHOICES = [
        (1, "Gentle (H-3d)"),
        (2, "Urgent (H-1d)"),
        (3, "Critical (H-3h)"),
    ]

    CHANNEL_CHOICES = [
        ("WEB", "Web / Desktop"),
        ("TELEGRAM", "Telegram Bot"),
        ("DIGEST", "Daily Digest"),
    ]

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("SENT", "Sent"),
        ("SNOOZED", "Snoozed"),
        ("DISMISSED", "Dismissed"),
        ("DONE", "Done"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reminders",
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="reminders",
    )
    checkpoint = models.ForeignKey(
        Checkpoint,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="reminders",
    )
    scheduled_for = models.DateTimeField(db_index=True)
    escalation_level = models.PositiveSmallIntegerField(choices=ESCALATION_CHOICES, default=1)
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default="WEB")
    message = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    snoozed_until = models.DateTimeField(null=True, blank=True)
    snooze_count = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-escalation_level", "scheduled_for"]
        indexes = [
            models.Index(
                fields=["user", "channel", "status", "scheduled_for"],
                name="rem_user_chan_stat_sched_idx",
            ),
            models.Index(fields=["status", "scheduled_for"], name="rem_stat_sched_idx"),
            models.Index(fields=["status", "snoozed_until"], name="rem_stat_snooze_idx"),
            models.Index(
                fields=["user", "status", "-escalation_level"],
                name="rem_user_stat_esc_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.task_id and self.user_id and self.task.user_id != self.user_id:
            raise ValidationError({"task": "Reminder task must belong to the same user."})
        if self.checkpoint_id and self.task_id and self.checkpoint.task_id != self.task_id:
            raise ValidationError(
                {"checkpoint": "Reminder checkpoint must belong to the same task."}
            )

    def __str__(self) -> str:
        target = self.checkpoint.title if self.checkpoint_id else self.task.title
        return f"L{self.escalation_level} [{self.status}] {target}"


class DailyDigestLog(models.Model):
    """Guarantees at most one anti-spam Morning/Evening digest per user per day (F4.3)."""

    SLOT_CHOICES = [
        ("MORNING", "Morning Briefing"),
        ("EVENING", "Evening Recap"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="daily_digest_logs",
    )
    digest_date = models.DateField()
    slot = models.CharField(max_length=15, choices=SLOT_CHOICES)
    item_count = models.PositiveSmallIntegerField(default=0)
    payload_summary = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-digest_date", "-sent_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "digest_date", "slot"],
                name="unique_user_digest_slot",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} - {self.digest_date} ({self.slot})"


class TelegramLink(models.Model):
    """Links a Django user account to a Telegram chat via one-time expiring token (F4.4)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="telegram_link",
    )
    chat_id = models.BigIntegerField(unique=True, null=True, blank=True)
    link_token = models.CharField(max_length=64, unique=True)
    token_created_at = models.DateTimeField(default=timezone.now)
    is_verified = models.BooleanField(default=False)
    linked_at = models.DateTimeField(null=True, blank=True)

    def is_token_expired(
        self,
        ttl_minutes: int = TELEGRAM_LINK_TOKEN_TTL_MINUTES,
        now=None,
    ) -> bool:
        """Returns True if this link_token is older than ``ttl_minutes``."""
        if self.token_created_at is None:
            return True
        current_now = now or timezone.now()
        return current_now > self.token_created_at + timedelta(minutes=ttl_minutes)

    def __str__(self) -> str:
        state = "Verified" if self.is_verified else "Pending"
        return f"Telegram({self.user}, {state})"

