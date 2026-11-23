from django.contrib import admin

from tasks.models import (
    Checkpoint,
    DailyDigestLog,
    FocusSession,
    Reminder,
    Task,
    TelegramLink,
)


class CheckpointInline(admin.TabularInline):
    model = Checkpoint
    extra = 0


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "user",
        "course",
        "category",
        "deadline",
        "status",
        "weight_percentage",
        "priority_level",
    )
    list_filter = ("status", "category", "priority_level", "source")
    search_fields = ("title", "description")
    inlines = [CheckpointInline]


@admin.register(FocusSession)
class FocusSessionAdmin(admin.ModelAdmin):
    list_display = ("task", "user", "mode", "planned_minutes", "actual_minutes", "status", "started_at")
    list_filter = ("mode", "status")


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ("task", "checkpoint", "user", "escalation_level", "channel", "status", "scheduled_for", "snooze_count")
    list_filter = ("escalation_level", "channel", "status")


@admin.register(DailyDigestLog)
class DailyDigestLogAdmin(admin.ModelAdmin):
    list_display = ("user", "digest_date", "slot", "item_count", "sent_at")
    list_filter = ("slot", "digest_date")


@admin.register(TelegramLink)
class TelegramLinkAdmin(admin.ModelAdmin):
    list_display = ("user", "chat_id", "is_verified", "linked_at")
    list_filter = ("is_verified",)
