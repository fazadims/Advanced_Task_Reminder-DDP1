"""Management command to evaluate due reminders and dispatch Telegram alerts (F4.1)."""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from tasks.models import TelegramLink
from tasks.services.notifier import evaluate_due_reminders
from tasks.services.telegram_bot import send_telegram_alert


class Command(BaseCommand):
    help = "Evaluate due reminders, generate adaptive coaching copy, and dispatch Telegram alerts."

    def handle(self, *args, **options) -> None:
        now = timezone.now()
        due_reminders = evaluate_due_reminders(now=now)

        verified_user_ids = set(
            TelegramLink.objects.filter(
                is_verified=True,
                chat_id__isnull=False,
            ).values_list("user_id", flat=True)
        )

        due_count = len(due_reminders)
        telegram_sent = 0
        telegram_skipped = 0

        for reminder in due_reminders:
            if reminder.channel == "DIGEST" or reminder.sent_at is not None:
                telegram_skipped += 1
                continue

            if reminder.user_id not in verified_user_ids:
                telegram_skipped += 1
                continue

            if send_telegram_alert(reminder):
                reminder.sent_at = timezone.now()
                reminder.save(update_fields=["sent_at"])
                telegram_sent += 1
            else:
                telegram_skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"process_reminders complete: due={due_count}, "
                f"telegram_sent={telegram_sent}, skipped={telegram_skipped}"
            )
        )
