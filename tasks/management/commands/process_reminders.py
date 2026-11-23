"""Management command to evaluate due reminders and dispatch Telegram alerts (F4.1)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from tasks.models import Reminder, TelegramLink
from tasks.services.digest import get_user_digest_preferences
from tasks.services.notifier import evaluate_due_reminders
from tasks.services.telegram_bot import send_telegram_alert

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Evaluate due reminders, generate adaptive coaching copy, and dispatch Telegram alerts."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--ignore-quiet-hours",
            action="store_true",
            default=False,
            help="Dispatch non-critical Telegram alerts even during a user's quiet hours.",
        )

    def handle(self, *args, **options) -> None:
        ignore_quiet_hours: bool = options.get("ignore_quiet_hours", False)
        now = timezone.now()
        due_reminders = evaluate_due_reminders(now=now)

        verified_chat_map: dict[int, int] = dict(
            TelegramLink.objects.filter(
                is_verified=True,
                chat_id__isnull=False,
            ).values_list("user_id", "chat_id")
        )

        local_now = timezone.localtime(now)
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        sent_today_counts: dict[int, int] = dict(
            Reminder.objects.filter(
                sent_at__gte=day_start,
                sent_at__lt=day_end,
            )
            .exclude(channel="DIGEST")
            .values("user_id")
            .annotate(total=Count("id"))
            .values_list("user_id", "total")
        )

        user_prefs_cache: dict[int, dict[str, Any]] = {}
        sent_target_keys: set[tuple[int, int, int | None]] = set()

        due_count = len(due_reminders)
        telegram_sent = 0
        telegram_skipped = 0
        quiet_deferred = 0
        rate_limited = 0

        for reminder in due_reminders:
            if reminder.channel == "DIGEST" or reminder.sent_at is not None:
                telegram_skipped += 1
                continue

            chat_id = verified_chat_map.get(reminder.user_id)
            if chat_id is None:
                telegram_skipped += 1
                continue

            target_key = (reminder.user_id, reminder.task_id, reminder.checkpoint_id)
            if target_key in sent_target_keys:
                telegram_skipped += 1
                continue

            if reminder.escalation_level < 3:
                prefs = user_prefs_cache.get(reminder.user_id)
                if prefs is None:
                    prefs = get_user_digest_preferences(reminder.user, now=now)
                    user_prefs_cache[reminder.user_id] = prefs

                if prefs["is_quiet_hours"] and not ignore_quiet_hours:
                    quiet_deferred += 1
                    telegram_skipped += 1
                    continue

                if sent_today_counts.get(reminder.user_id, 0) >= prefs["max_alerts_per_day"]:
                    rate_limited += 1
                    telegram_skipped += 1
                    continue

            claim_ts = timezone.now()
            claimed = Reminder.objects.filter(
                pk=reminder.pk,
                sent_at__isnull=True,
            ).update(sent_at=claim_ts)
            if claimed != 1:
                telegram_skipped += 1
                continue

            try:
                sent_ok = send_telegram_alert(reminder, chat_id=chat_id)
            except Exception:
                logger.exception(
                    "Unexpected error sending Telegram alert for reminder #%s",
                    reminder.pk,
                )
                sent_ok = False

            if sent_ok:
                reminder.sent_at = claim_ts
                sent_target_keys.add(target_key)
                sent_today_counts[reminder.user_id] = (
                    sent_today_counts.get(reminder.user_id, 0) + 1
                )
                telegram_sent += 1
            else:
                Reminder.objects.filter(
                    pk=reminder.pk,
                    sent_at=claim_ts,
                ).update(sent_at=None)
                telegram_skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"process_reminders complete: due={due_count}, "
                f"telegram_sent={telegram_sent}, skipped={telegram_skipped}, "
                f"quiet_deferred={quiet_deferred}, rate_limited={rate_limited}"
            )
        )
