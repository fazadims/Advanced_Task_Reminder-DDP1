"""Management command to generate and dispatch anti-spam Morning/Evening Daily Digests (F4.3)."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction
from django.utils import timezone

from tasks.models import DailyDigestLog, Reminder, TelegramLink
from tasks.services.digest import build_daily_digest
from tasks.services.telegram_bot import send_telegram_digest


class Command(BaseCommand):
    help = "Build and send anti-spam Morning or Evening daily digests for active users."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--slot",
            type=str.lower,
            choices=["morning", "evening"],
            default="morning",
            help="Digest slot to dispatch: 'morning' or 'evening' (default: morning).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Preview digest counts without saving DailyDigestLog/Reminder or sending Telegram.",
        )

    def handle(self, *args, **options) -> None:
        slot = options["slot"].upper()
        dry_run: bool = options["dry_run"]
        now = timezone.now()
        today = timezone.localdate(now)

        user_model = get_user_model()
        active_users = user_model.objects.filter(is_active=True).order_by("id")

        verified_user_ids = set(
            TelegramLink.objects.filter(
                is_verified=True,
                chat_id__isnull=False,
            ).values_list("user_id", flat=True)
        )

        created_count = 0
        dry_run_count = 0
        telegram_sent = 0
        skipped_existing = 0
        skipped_empty = 0

        for user in active_users:
            if DailyDigestLog.objects.filter(
                user=user,
                digest_date=today,
                slot=slot,
            ).exists():
                skipped_existing += 1
                continue

            digest = build_daily_digest(user, slot=slot, now=now)
            if digest["total_items"] == 0:
                skipped_empty += 1
                continue

            if dry_run:
                dry_run_count += 1
                self.stdout.write(
                    f"[DRY-RUN] user={user.username} slot={slot} "
                    f"items={digest['total_items']} overflow={digest['overflow_count']}"
                )
                continue

            anchor_task = digest["items"][0]["task"]
            escalation_level = 2 if digest["overdue_tasks"] else 1

            try:
                with transaction.atomic():
                    DailyDigestLog.objects.create(
                        user=user,
                        digest_date=digest["digest_date"],
                        slot=slot,
                        item_count=digest["total_items"],
                        payload_summary=digest["summary_text"],
                    )
                    digest_reminder = Reminder.objects.create(
                        user=user,
                        task=anchor_task,
                        checkpoint=None,
                        scheduled_for=now,
                        escalation_level=escalation_level,
                        channel="DIGEST",
                        message=digest["summary_text"],
                        status="PENDING",
                    )
            except IntegrityError:
                skipped_existing += 1
                continue

            created_count += 1

            if user.pk in verified_user_ids and send_telegram_digest(user, digest):
                digest_reminder.sent_at = timezone.now()
                digest_reminder.save(update_fields=["sent_at"])
                telegram_sent += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"send_daily_digest DRY-RUN ({slot}, {today}): "
                    f"would_create={dry_run_count}, skipped_existing={skipped_existing}, "
                    f"skipped_empty={skipped_empty}"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"send_daily_digest complete ({slot}, {today}): "
                    f"created={created_count}, telegram_sent={telegram_sent}, "
                    f"skipped_existing={skipped_existing}, skipped_empty={skipped_empty}"
                )
            )
