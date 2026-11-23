from django.urls import path

from tasks import views

app_name = "alerts"

urlpatterns = [
    path("", views.alert_inbox_view, name="inbox"),
    path("poll/", views.poll_due_alerts, name="poll"),
    path("process/", views.trigger_process_reminders, name="process_now"),
    path("<int:pk>/snooze/", views.snooze_reminder, name="snooze"),
    path("<int:pk>/dismiss/", views.dismiss_reminder, name="dismiss"),
    path("<int:pk>/done/", views.done_reminder, name="done"),
    path("digest/today/", views.daily_digest_view, name="daily_digest"),
    path("telegram/connect/", views.telegram_connect_view, name="telegram_connect"),
    path("telegram/webhook/<str:secret>/", views.telegram_webhook_view, name="telegram_webhook"),
    path("coaching/preview/", views.preview_coaching_tone, name="coaching_preview"),
]
