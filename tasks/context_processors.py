"""Template context processors for navigation badges."""

from __future__ import annotations

from tasks.services.notifier import active_due_reminders_qs


def nav_badges(request) -> dict[str, int]:
    """Provides active/due reminder count for the navigation alert badge.

    Skips database queries on HTMX partial requests (which update the badge via OOB
    swaps when needed) and for anonymous users.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"nav_active_alert_count": 0}
    if request.headers.get("HX-Request", "").lower() == "true":
        return {"nav_active_alert_count": 0}

    count = active_due_reminders_qs(user).count()
    return {"nav_active_alert_count": count}
