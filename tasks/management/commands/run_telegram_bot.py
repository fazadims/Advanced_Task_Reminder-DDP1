"""Management command for local Telegram Bot long-polling without HTTPS/ngrok (F4.4)."""

from __future__ import annotations

import os
import time
from typing import Any

from django.core.management.base import BaseCommand

from tasks.services.telegram_bot import _telegram_api_post, handle_telegram_update

DEFAULT_POLL_TIMEOUT_SECONDS = 5
ERROR_BACKOFF_SECONDS = 2.0


class Command(BaseCommand):
    help = "Run local Telegram Bot long-polling via getUpdates (no webhook/ngrok required)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            default=False,
            help="Fetch and process a single batch of Telegram updates, then exit.",
        )

    def handle(self, *args, **options) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            self.stdout.write(
                self.style.WARNING(
                    "TELEGRAM_BOT_TOKEN is not set. Exiting Telegram bot polling cleanly."
                )
            )
            return

        once: bool = options["once"]
        offset: int | None = None
        processed_count = 0

        self.stdout.write(
            self.style.SUCCESS(
                "Starting Telegram Bot polling loop"
                + (" (single pass --once)..." if once else " (Ctrl+C to stop)...")
            )
        )

        try:
            while True:
                payload: dict[str, Any] = {
                    "timeout": 0 if once else DEFAULT_POLL_TIMEOUT_SECONDS,
                    "allowed_updates": ["message", "callback_query"],
                }
                if offset is not None:
                    payload["offset"] = offset

                response = _telegram_api_post("getUpdates", payload)
                if not response or not response.get("ok"):
                    self.stderr.write(
                        self.style.WARNING(
                            "Failed to poll Telegram getUpdates; retrying..."
                            if not once
                            else "Failed to poll Telegram getUpdates."
                        )
                    )
                    if once:
                        break
                    time.sleep(ERROR_BACKOFF_SECONDS)
                    continue

                updates = response.get("result") or []
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    result = handle_telegram_update(update)
                    processed_count += 1
                    self.stdout.write(f"Handled update #{update_id}: {result}")

                if once:
                    break
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nTelegram Bot polling stopped by user."))

        self.stdout.write(
            self.style.SUCCESS(
                f"run_telegram_bot finished: processed={processed_count}"
            )
        )
