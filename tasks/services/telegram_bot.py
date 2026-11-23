"""Interactive Telegram Bot integration for alerts, digests, and quick callbacks (F4.4)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from tasks.models import Reminder, TelegramLink
from tasks.services.ai_coaching import generate_coaching_message
from tasks.services.digest import build_daily_digest
from tasks.services.notifier import (
    MAX_SNOOZE_COUNT,
    complete_reminder_action,
    snooze_reminder_action,
)

logger = logging.getLogger(__name__)

TELEGRAM_TIMEOUT_SECONDS = 10
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
SNOOZE_DURATION = timedelta(minutes=60)
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
ESCALATION_LABELS = {
    1: "🟢 Reminder (H-3d)",
    2: "🟡 Urgent (H-1d)",
    3: "🔴 CRITICAL (H-3h)",
}


def _truncate_telegram_text(text: str, max_len: int = MAX_TELEGRAM_MESSAGE_LENGTH) -> str:
    """Ensures outgoing Telegram message text stays within Telegram's 4096-char limit."""
    cleaned = str(text or "")
    if len(cleaned) <= max_len:
        return cleaned
    suffix = "\n..."
    if max_len <= len(suffix):
        return cleaned[:max_len]
    return f"{cleaned[: max_len - len(suffix)].rstrip()}{suffix}"


def _is_public_telegram_button_url(url: str) -> bool:
    """Returns True only for non-loopback http/https URLs accepted by Telegram inline URL buttons."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname or hostname in LOCAL_HOSTNAMES or hostname.endswith(".localhost"):
        return False
    return True


def _telegram_api_post(
    method: str,
    payload: dict[str, Any],
    *,
    timeout: int = TELEGRAM_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Posts a JSON payload to the Telegram Bot API.

    Returns parsed response dict (including ok=False error payloads on HTTPError so callers
    can inspect 403/409/429 error codes), or None if token is unset or transport fails.
    """
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            if not parsed.get("ok"):
                logger.warning(
                    "Telegram API '%s' returned ok=False (code=%s): %s",
                    method,
                    parsed.get("error_code"),
                    parsed.get("description"),
                )
            return parsed
        return None
    except urllib.error.HTTPError as exc:
        raw_err = ""
        try:
            raw_err = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw_err = ""

        parsed_err: dict[str, Any] | None = None
        try:
            maybe_dict = json.loads(raw_err)
            if isinstance(maybe_dict, dict):
                parsed_err = maybe_dict
        except (json.JSONDecodeError, ValueError):
            parsed_err = None

        description = (
            str(parsed_err.get("description") or "")
            if parsed_err is not None
            else raw_err[:200]
        )
        safe_description = description.replace(token, "[REDACTED]")
        logger.warning(
            "Telegram API '%s' returned HTTP %s: %s",
            method,
            exc.code,
            safe_description,
        )
        if parsed_err is not None:
            result = dict(parsed_err)
            result["ok"] = False
            result.setdefault("error_code", exc.code)
            result.setdefault("description", safe_description)
            return result
        return {
            "ok": False,
            "error_code": exc.code,
            "description": safe_description,
        }
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ) as exc:
        reason_text = str(getattr(exc, "reason", exc)).replace(token, "[REDACTED]")
        logger.warning(
            "Telegram API '%s' request failed (%s): %s",
            method,
            type(exc).__name__,
            reason_text,
        )
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


def send_telegram_alert(reminder: Reminder, *, chat_id: int | None = None) -> bool:
    """Sends a formatted escalation alert with inline Done, Snooze 1h, and optional Focus Mode button."""
    target_chat_id = chat_id
    if target_chat_id is None:
        link = _get_verified_link_for_user(reminder.user)
        if link is None:
            return False
        target_chat_id = link.chat_id

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

    inline_keyboard: list[list[dict[str, str]]] = [
        [
            {"text": "✅ Done", "callback_data": f"cb:done:{reminder.pk}"},
            {"text": "⏰ Snooze 1h", "callback_data": f"cb:snooze60:{reminder.pk}"},
        ]
    ]
    if _is_public_telegram_button_url(focus_url):
        inline_keyboard.append([{"text": "🎯 Focus Mode", "url": focus_url}])
    else:
        lines.append(f"Focus Mode: {focus_url}")

    payload = {
        "chat_id": target_chat_id,
        "text": _truncate_telegram_text("\n".join(lines)),
        "reply_markup": {"inline_keyboard": inline_keyboard},
    }

    result = _telegram_api_post("sendMessage", payload)
    return bool(result and result.get("ok"))


def send_telegram_digest(
    user: Any,
    digest: dict[str, Any],
    *,
    chat_id: int | None = None,
) -> bool:
    """Sends a plain-text daily digest summary to the user's verified Telegram chat."""
    target_chat_id = chat_id
    if target_chat_id is None:
        link = _get_verified_link_for_user(user)
        if link is None:
            return False
        target_chat_id = link.chat_id

    summary_text = str(digest.get("summary_text") or "").strip()
    if not summary_text:
        return False

    result = _telegram_api_post(
        "sendMessage",
        {
            "chat_id": target_chat_id,
            "text": _truncate_telegram_text(summary_text),
        },
    )
    return bool(result and result.get("ok"))


def _answer_callback(callback_query_id: str | None, text: str) -> None:
    if callback_query_id:
        _telegram_api_post(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )


def _clear_inline_keyboard(chat_id: int, message_id: Any) -> None:
    """Clears stale inline keyboard buttons after a reminder is resolved."""
    if isinstance(message_id, int) and not isinstance(message_id, bool):
        _telegram_api_post(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )


def _handle_callback_query(cb: dict[str, Any]) -> dict[str, Any]:
    callback_id = cb.get("id")
    raw_data = str(cb.get("data") or "").strip()
    message_obj = cb.get("message")
    chat_obj = message_obj.get("chat") if isinstance(message_obj, dict) else None
    chat_id = chat_obj.get("id") if isinstance(chat_obj, dict) else None
    message_id = message_obj.get("message_id") if isinstance(message_obj, dict) else None
    from_obj = cb.get("from")
    from_id = from_obj.get("id") if isinstance(from_obj, dict) else None

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

    # Fail-closed authorization: require verified TelegramLink matching chat_id (and from.id if present).
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        _answer_callback(callback_id, "Akses ditolak untuk reminder ini.")
        return {"ok": False, "action": "unauthorized", "reminder_id": reminder_id}

    owner_link = _get_verified_link_for_user(reminder.user)
    if owner_link is None or owner_link.chat_id != chat_id:
        _answer_callback(callback_id, "Akses ditolak untuk reminder ini.")
        return {"ok": False, "action": "unauthorized", "reminder_id": reminder_id}

    if from_id is not None and (
        not isinstance(from_id, int)
        or isinstance(from_id, bool)
        or owner_link.chat_id != from_id
    ):
        _answer_callback(callback_id, "Akses ditolak untuk reminder ini.")
        return {"ok": False, "action": "unauthorized", "reminder_id": reminder_id}

    if action == "done":
        ok, res = complete_reminder_action(reminder.pk, user=reminder.user)
        if not ok:
            _answer_callback(callback_id, res.get("error", "Reminder tidak ditemukan."))
            return {
                "ok": False,
                "action": "reminder_not_found",
                "reminder_id": reminder_id,
            }

        _answer_callback(callback_id, "✅ Ditandai selesai!")
        _clear_inline_keyboard(chat_id, message_id)
        return {
            "ok": True,
            "action": "done",
            "reminder_id": res["reminder_id"],
            "task_id": res["task_id"],
            "checkpoint_id": res["checkpoint_id"],
        }

    if action == "snooze60":
        ok, res = snooze_reminder_action(reminder.pk, minutes=60, user=reminder.user)
        if not ok:
            reason = res.get("reason", "snooze_rejected")
            if reason == "already_closed":
                _answer_callback(callback_id, "Reminder sudah selesai atau ditutup.")
                _clear_inline_keyboard(chat_id, message_id)
            elif reason == "max_snooze_reached":
                _answer_callback(
                    callback_id,
                    f"Snooze ditolak: batas maksimal ({MAX_SNOOZE_COUNT}x) sudah tercapai.",
                )
            elif reason == "deadline_too_close":
                _answer_callback(
                    callback_id,
                    "Snooze 1 jam ditolak: deadline terlalu dekat atau sudah lewat!",
                )
            else:
                _answer_callback(callback_id, res.get("error", "Snooze ditolak."))

            out: dict[str, Any] = {
                "ok": False,
                "action": "snooze_rejected",
                "reason": reason,
                "reminder_id": res.get("reminder_id", reminder_id),
            }
            if "snooze_count" in res:
                out["snooze_count"] = res["snooze_count"]
            return out

        _answer_callback(
            callback_id,
            f"⏰ Di-snooze 1 jam (snooze ke-{res['snooze_count']}).",
        )
        return {
            "ok": True,
            "action": "snooze60",
            "reminder_id": res["reminder_id"],
            "snooze_count": res["snooze_count"],
            "snoozed_until": res["snoozed_until"],
        }

    _answer_callback(callback_id, "Aksi tidak dikenali.")
    return {"ok": False, "action": "unknown_callback", "data": raw_data}


def _handle_message(message: dict[str, Any]) -> dict[str, Any]:
    chat_obj = message.get("chat")
    chat_id = chat_obj.get("id") if isinstance(chat_obj, dict) else None
    chat_type = (
        str(chat_obj.get("type") or "").strip().lower()
        if isinstance(chat_obj, dict)
        else ""
    )
    text = str(message.get("text") or "").strip()
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or not text:
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

        if (chat_type and chat_type != "private") or chat_id < 0:
            _telegram_api_post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        "Demi keamanan, tautkan akun ATR hanya melalui chat pribadi "
                        "langsung dengan bot ini, bukan di grup atau channel."
                    ),
                },
            )
            return {"ok": False, "action": "start_non_private_chat", "chat_id": chat_id}

        now = timezone.now()
        linked_user_id: int | None = None
        linked_username = ""
        link_outcome = "invalid"

        with transaction.atomic():
            link = (
                TelegramLink.objects.select_for_update()
                .select_related("user")
                .filter(link_token=token, is_verified=False)
                .first()
            )
            if link is None:
                link_outcome = "invalid"
            elif link.is_token_expired(now=now):
                link_outcome = "expired"
            else:
                link_outcome = "linked"
                linked_user_id = link.user_id
                linked_username = link.user.username
                # Release chat_id if previously attached to another user record.
                TelegramLink.objects.filter(chat_id=chat_id).exclude(pk=link.pk).update(
                    chat_id=None,
                    is_verified=False,
                )
                link.chat_id = chat_id
                link.is_verified = True
                link.linked_at = now
                # Immediately rotate the one-time token so it cannot be replayed.
                link.link_token = secrets.token_urlsafe(24)
                link.token_created_at = now
                link.save(
                    update_fields=[
                        "chat_id",
                        "is_verified",
                        "linked_at",
                        "link_token",
                        "token_created_at",
                    ]
                )

        if link_outcome == "invalid":
            _telegram_api_post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Token tautan tidak valid atau sudah digunakan. Silakan buat token baru di aplikasi ATR.",
                },
            )
            return {"ok": False, "action": "start_invalid_token", "chat_id": chat_id}

        if link_outcome == "expired":
            _telegram_api_post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Token tautan sudah kedaluwarsa. Silakan buat token baru di aplikasi ATR.",
                },
            )
            return {"ok": False, "action": "start_expired_token", "chat_id": chat_id}

        _telegram_api_post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"✅ Selamat datang, {linked_username}! Akun Telegram kamu sudah "
                    "terhubung ke ATR. Ketik /today untuk melihat ringkasan tugas hari ini."
                ),
            },
        )
        return {
            "ok": True,
            "action": "linked",
            "user_id": linked_user_id,
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
        sent = send_telegram_digest(link.user, digest, chat_id=chat_id)
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
