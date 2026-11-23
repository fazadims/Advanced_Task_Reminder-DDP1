"""Interactive Telegram Bot integration for alerts, digests, and quick callbacks (F4.4)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from tasks.models import Reminder, TelegramLink
from tasks.services.ai_coaching import generate_coaching_message
from tasks.services.digest import build_daily_digest

logger = logging.getLogger(__name__)

TELEGRAM_TIMEOUT_SECONDS = 10
MAX_SNOOZE_COUNT = 3
SNOOZE_DURATION = timedelta(minutes=60)
ESCALATION_LABELS = {
    1: "🟢 Reminder (H-3d)",
    2: "🟡 Urgent (H-1d)",
    3: "🔴 CRITICAL (H-3h)",
}


def _telegram_api_post(method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Posts a JSON payload to the Telegram Bot API. Returns None safely if token is unset or call fails."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.warning(
            "TELEGRAM_BOT_TOKEN is unset; skipping Telegram Bot API call '%s'.",
            method,
        )
        return None

    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            if not parsed.get("ok"):
                logger.warning("Telegram API '%s' returned ok=False: %s", method, parsed)
            return parsed
        return None
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ) as exc:
        logger.warning("Telegram API '%s' request failed: %s", method, exc)
        return None


def _get_verified_link_for_user(user: Any) -> TelegramLink | None:
    return (
        TelegramLink.objects.filter(
            user=user,
            is_verified=True,
            chat_id__isnull=False,
        )
        .select_related("user")
        .first()
    )


def _format_local_dt(dt: Any) -> str:
    local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return local_dt.strftime("%d %b %Y %H:%M")


def send_telegram_alert(reminder: Reminder) -> bool:
    """Sends a formatted escalation alert with inline Done, Snooze 1h, and Focus Mode buttons."""
    link = _get_verified_link_for_user(reminder.user)
    if link is None:
        return False

    badge = ESCALATION_LABELS.get(reminder.escalation_level, "🔔 Reminder")
    has_checkpoint = bool(reminder.checkpoint_id and reminder.checkpoint)
    target_title = reminder.checkpoint.title if has_checkpoint else reminder.task.title
    target_deadline = (
        reminder.checkpoint.target_deadline if has_checkpoint else reminder.task.deadline
    )

    coaching_message = reminder.message
    if not coaching_message:
        coaching_message, _ = generate_coaching_message(reminder)

    course_prefix = f"[{reminder.task.course.code}] " if reminder.task.course_id else ""
    lines = [
        badge,
        f"Tugas: {course_prefix}{reminder.task.title}",
    ]
    if has_checkpoint:
        lines.append(f"Checkpoint: {target_title}")
    lines.append(f"Deadline: {_format_local_dt(target_deadline)}")
    if coaching_message:
        lines.append(f"\n{coaching_message}")

    base_url = (
        os.environ.get("APP_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
        or "http://127.0.0.1:8000"
    )
    focus_url = f"{base_url}/tasks/{reminder.task_id}/focus/"

    payload = {
        "chat_id": link.chat_id,
        "text": "\n".join(lines),
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "✅ Done", "callback_data": f"cb:done:{reminder.pk}"},
                    {"text": "⏰ Snooze 1h", "callback_data": f"cb:snooze60:{reminder.pk}"},
                ],
                [
                    {"text": "🎯 Focus Mode", "url": focus_url},
                ],
            ]
        },
    }

    result = _telegram_api_post("sendMessage", payload)
    return bool(result and result.get("ok"))


def send_telegram_digest(user: Any, digest: dict[str, Any]) -> bool:
    """Sends a plain-text daily digest summary to the user's verified Telegram chat."""
    link = _get_verified_link_for_user(user)
    if link is None:
        return False

    summary_text = str(digest.get("summary_text") or "").strip()
    if not summary_text:
        return False

    result = _telegram_api_post(
        "sendMessage",
        {
            "chat_id": link.chat_id,
            "text": summary_text,
        },
    )
    return bool(result and result.get("ok"))


def _answer_callback(callback_query_id: str | None, text: str) -> None:
    if callback_query_id:
        _telegram_api_post(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )


def _handle_callback_query(cb: dict[str, Any]) -> dict[str, Any]:
    callback_id = cb.get("id")
    raw_data = str(cb.get("data") or "").strip()
    chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")

    parts = raw_data.split(":")
    if len(parts) != 3 or parts[0] != "cb":
        _answer_callback(callback_id, "Aksi tidak dikenali.")
        return {"ok": False, "action": "unknown_callback", "data": raw_data}

    action, raw_id = parts[1], parts[2]
    try:
        reminder_id = int(raw_id)
    except ValueError:
        _answer_callback(callback_id, "ID reminder tidak valid.")
        return {"ok": False, "action": "invalid_callback", "data": raw_data}

    reminder = (
        Reminder.objects.select_related("task", "checkpoint", "user")
        .filter(pk=reminder_id)
        .first()
    )
    if reminder is None:
        _answer_callback(callback_id, "Reminder tidak ditemukan.")
        return {"ok": False, "action": "reminder_not_found", "reminder_id": reminder_id}

    # Verify chat ownership when chat_id is supplied in the callback payload.
    if chat_id is not None:
        owner_link = _get_verified_link_for_user(reminder.user)
        if owner_link is not None and owner_link.chat_id != chat_id:
            _answer_callback(callback_id, "Akses ditolak untuk reminder ini.")
            return {"ok": False, "action": "unauthorized", "reminder_id": reminder_id}

    if action == "done":
        now = timezone.now()
        with transaction.atomic():
            if reminder.checkpoint_id and reminder.checkpoint:
                cp = reminder.checkpoint
                if not cp.is_done:
                    cp.is_done = True
                    cp.completed_at = now
                    cp.save(update_fields=["is_done", "completed_at"])
                reminder.task.sync_status_from_checkpoints()
                Reminder.objects.filter(
                    checkpoint=cp,
                    status__in=("PENDING", "SNOOZED"),
                ).exclude(pk=reminder.pk).update(status="DONE")
            else:
                if reminder.task.checkpoints.count() == 0:
                    reminder.task.status = "DONE"
                    if reminder.task.completed_at is None:
                        reminder.task.completed_at = now
                    reminder.task.save(update_fields=["status", "completed_at", "updated_at"])
                else:
                    reminder.task.sync_status_from_checkpoints()

            reminder.status = "DONE"
            reminder.save(update_fields=["status"])

        _answer_callback(callback_id, "✅ Ditandai selesai!")
        return {
            "ok": True,
            "action": "done",
            "reminder_id": reminder.pk,
            "task_id": reminder.task_id,
            "checkpoint_id": reminder.checkpoint_id,
        }

    if action == "snooze60":
        now = timezone.now()
        target_deadline = (
            reminder.checkpoint.target_deadline
            if reminder.checkpoint_id and reminder.checkpoint
            else reminder.task.deadline
        )

        if reminder.snooze_count >= MAX_SNOOZE_COUNT:
            _answer_callback(
                callback_id,
                f"Snooze ditolak: batas maksimal ({MAX_SNOOZE_COUNT}x) sudah tercapai.",
            )
            return {
                "ok": False,
                "action": "snooze_rejected",
                "reason": "max_snooze_reached",
                "reminder_id": reminder.pk,
                "snooze_count": reminder.snooze_count,
            }

        snoozed_until = now + SNOOZE_DURATION
        if snoozed_until >= target_deadline:
            _answer_callback(
                callback_id,
                "Snooze 1 jam ditolak: deadline terlalu dekat atau sudah lewat!",
            )
            return {
                "ok": False,
                "action": "snooze_rejected",
                "reason": "deadline_too_close",
                "reminder_id": reminder.pk,
            }

        reminder.status = "SNOOZED"
        reminder.snoozed_until = snoozed_until
        reminder.snooze_count += 1
        update_fields = ["status", "snoozed_until", "snooze_count"]
        if reminder.snooze_count >= 2:
            if reminder.escalation_level < 3:
                reminder.escalation_level = 3
                update_fields.append("escalation_level")
            if reminder.message:
                reminder.message = ""
                update_fields.append("message")
        reminder.save(update_fields=update_fields)

        _answer_callback(
            callback_id,
            f"⏰ Di-snooze 1 jam (snooze ke-{reminder.snooze_count}).",
        )
        return {
            "ok": True,
            "action": "snooze60",
            "reminder_id": reminder.pk,
            "snooze_count": reminder.snooze_count,
            "snoozed_until": reminder.snoozed_until.isoformat(),
        }

    _answer_callback(callback_id, "Aksi tidak dikenali.")
    return {"ok": False, "action": "unknown_callback", "data": raw_data}


def _handle_message(message: dict[str, Any]) -> dict[str, Any]:
    chat_id = (message.get("chat") or {}).get("id")
    text = str(message.get("text") or "").strip()
    if chat_id is None or not text:
        return {"ok": True, "action": "ignored"}

    parts = text.split(maxsplit=1)
    command = parts[0].split("@")[0].lower()

    if command == "/start":
        token = parts[1].strip() if len(parts) > 1 else ""
        if not token:
            _telegram_api_post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        "Halo! Kirim /start <token> dari halaman pengaturan ATR "
                        "untuk menghubungkan akun Telegram kamu."
                    ),
                },
            )
            return {"ok": False, "action": "start_missing_token", "chat_id": chat_id}

        link = TelegramLink.objects.select_related("user").filter(link_token=token).first()
        if link is None:
            _telegram_api_post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Token tautan tidak valid. Silakan buat token baru di aplikasi ATR.",
                },
            )
            return {"ok": False, "action": "start_invalid_token", "chat_id": chat_id}

        with transaction.atomic():
            # Release chat_id if previously attached to another user record.
            TelegramLink.objects.filter(chat_id=chat_id).exclude(pk=link.pk).update(
                chat_id=None,
                is_verified=False,
            )
            link.chat_id = chat_id
            link.is_verified = True
            link.linked_at = timezone.now()
            link.save(update_fields=["chat_id", "is_verified", "linked_at"])

        _telegram_api_post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"✅ Selamat datang, {link.user.username}! Akun Telegram kamu sudah "
                    "terhubung ke ATR. Ketik /today untuk melihat ringkasan tugas hari ini."
                ),
            },
        )
        return {
            "ok": True,
            "action": "linked",
            "user_id": link.user_id,
            "chat_id": chat_id,
        }

    if command == "/today":
        link = (
            TelegramLink.objects.select_related("user")
            .filter(chat_id=chat_id, is_verified=True)
            .first()
        )
        if link is None:
            _telegram_api_post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        "Akun Telegram ini belum tertaut. Kirim /start <token> "
                        "dari aplikasi ATR terlebih dahulu."
                    ),
                },
            )
            return {"ok": False, "action": "today_unlinked", "chat_id": chat_id}

        local_now = timezone.localtime(timezone.now())
        slot = "MORNING" if local_now.hour < 15 else "EVENING"
        digest = build_daily_digest(link.user, slot=slot)
        sent = send_telegram_digest(link.user, digest)
        return {
            "ok": True,
            "action": "today",
            "user_id": link.user_id,
            "slot": slot,
            "total_items": digest["total_items"],
            "sent": sent,
        }

    return {"ok": True, "action": "ignored", "text": text}


def handle_telegram_update(payload: dict[str, Any]) -> dict[str, Any]:
    """Processes an incoming Telegram webhook or getUpdates payload and returns a structured status dict."""
    if not isinstance(payload, dict):
        return {"ok": False, "action": "invalid_payload"}

    if "callback_query" in payload and isinstance(payload["callback_query"], dict):
        return _handle_callback_query(payload["callback_query"])

    if "message" in payload and isinstance(payload["message"], dict):
        return _handle_message(payload["message"])

    return {"ok": True, "action": "ignored"}
