"""Management command for local Telegram Bot long-polling without HTTPS/ngrok (F4.4)."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import Any

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from tasks.services.telegram_bot import _telegram_api_post, handle_telegram_update

logger = logging.getLogger(__name__)

DEFAULT_POLL_TIMEOUT_SECONDS = 5
ERROR_BACKOFF_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 300.0


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
        stop_requested = False
        conflict_detected = False

        previous_sigterm = None
        in_main_thread = threading.current_thread() is threading.main_thread()

        def _handle_sigterm(signum, frame) -> None:  # noqa: ARG001
            nonlocal stop_requested
            stop_requested = True

        if in_main_thread:
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _handle_sigterm)

        self.stdout.write(
            self.style.SUCCESS(
                "Starting Telegram Bot polling loop"
                + (" (single pass --once)..." if once else " (Ctrl+C to stop)...")
            )
        )

        poll_timeout = 0 if once else DEFAULT_POLL_TIMEOUT_SECONDS
        http_timeout = poll_timeout + 10

        try:
            while not stop_requested:
                close_old_connections()

                payload: dict[str, Any] = {
                    "timeout": poll_timeout,
                    "allowed_updates": ["message", "callback_query"],
                }
                if offset is not None:
                    payload["offset"] = offset

                response = _telegram_api_post("getUpdates", payload, timeout=http_timeout)
                if not response or not response.get("ok"):
                    error_code = (
                        response.get("error_code") if isinstance(response, dict) else None
                    )
                    if error_code == 409:
                        conflict_detected = True
                        msg = (
                            "Telegram API returned 409 Conflict: an active webhook or another "
                            "getUpdates poller is running for this bot token. Run deleteWebhook "
                            "or stop the competing poller, then retry."
                        )
                        logger.warning(msg)
                        self.stderr.write(self.style.WARNING(msg))
                        break

                    if error_code == 429:
                        params = (
                            response.get("parameters")
                            if isinstance(response, dict)
                            else None
                        )
                        raw_retry = (
                            params.get("retry_after") if isinstance(params, dict) else None
                        )
                        if (
                            isinstance(raw_retry, (int, float))
                            and not isinstance(raw_retry, bool)
                            and raw_retry > 0
                        ):
                            backoff = max(1.0, min(float(raw_retry), MAX_RETRY_AFTER_SECONDS))
                        else:
                            backoff = ERROR_BACKOFF_SECONDS
                        msg = (
                            f"Telegram API rate limited (429 Too Many Requests); "
                            f"backing off for {backoff:.1f}s."
                        )
                        logger.warning(msg)
                        self.stderr.write(self.style.WARNING(msg))
                        if once or stop_requested:
                            break
                        time.sleep(backoff)
                        continue

                    self.stderr.write(
                        self.style.WARNING(
                            "Failed to poll Telegram getUpdates; retrying..."
                            if not once
                            else "Failed to poll Telegram getUpdates."
                        )
                    )
                    if once or stop_requested:
                        break
                    time.sleep(ERROR_BACKOFF_SECONDS)
                    continue

                updates = response.get("result") or []
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    update_id = update.get("update_id")
                    if isinstance(update_id, int) and not isinstance(update_id, bool):
                        next_offset = update_id + 1
                        offset = max(offset, next_offset) if offset is not None else next_offset

                    try:
                        result = handle_telegram_update(update)
                        processed_count += 1
                        self.stdout.write(f"Handled update #{update_id}: {result}")
                    except Exception:
                        logger.exception(
                            "Unhandled error while processing Telegram update #%s",
                            update_id,
                        )
                        self.stderr.write(
                            self.style.ERROR(
                                f"Error handling Telegram update #{update_id}; skipping."
                            )
                        )

                if once or stop_requested:
                    break
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nTelegram Bot polling stopped by user."))
        finally:
            if offset is not None and not conflict_detected:
                try:
                    _telegram_api_post(
                        "getUpdates",
                        {"offset": offset, "timeout": 0, "limit": 1},
                        timeout=10,
                    )
                except Exception:
                    logger.exception("Failed to send final Telegram offset acknowledgement.")

            if in_main_thread and previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
            close_old_connections()

        self.stdout.write(
            self.style.SUCCESS(
                f"run_telegram_bot finished: processed={processed_count}"
            )
        )
