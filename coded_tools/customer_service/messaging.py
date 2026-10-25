"""
Companion messaging (companion plan P5): the rules every proactive message to a customer shares, in code.

Two senders use it: the Resolution Guardian (service updates) and the Stylist (launch invites). Their messages go to
the outbox (and so into the customer's thread). The limits are the customer's, not each sender's:

    quiet hours     none from guardian.quiet_hours (21:00-08:00 on the simulator's clock); they wait
    daily limit     guardian.max_updates_per_day messages a day in total, service updates and invites together
    service first   an invite may use a slot only if no service update is waiting for the customer, and it always
                    leaves stylist.reserve_for_service slots of the day free for service updates
    weekly limit    at most stylist.max_invites_per_week invites in 7 days (counted by the Stylist)
    consent         service updates: the service_updates consent (the Guardian's opt-out); invites: stylist_mode

Counts read both senders' own tables in the case store's SQLite file (a table not created yet counts as none).
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

QUIET_HOURS = "quiet_hours"
DAILY_LIMIT = "daily_limit"
SERVICE_FIRST = "service_first"


def quiet(now: datetime, guardian_settings: dict[str, Any]) -> bool:
    start, end = (int(h) for h in guardian_settings["quiet_hours"])
    return now.hour >= start or now.hour < end if start > end else start <= now.hour < end


def _count(path: Path, sql: str, args: tuple) -> int:
    try:
        with sqlite3.connect(path, timeout=10) as db:
            return db.execute(sql, args).fetchone()[0]
    except sqlite3.OperationalError:  # that sender's tables do not exist yet
        return 0


def sent_on(path: Path, customer_id: str, day: str) -> int:
    """Messages sent to the customer on this day (YYYY-MM-DD, simulator clock): service updates and invites."""
    service = _count(path, "SELECT COUNT(*) FROM guardian_notices WHERE customer_id = ? AND status = 'sent' "
                           "AND substr(sent_at, 1, 10) = ?", (customer_id, day))
    invites = _count(path, "SELECT COUNT(*) FROM stylist_invites WHERE customer_id = ? AND status = 'sent' "
                           "AND substr(sent_at, 1, 10) = ?", (customer_id, day))
    return service + invites


def service_waiting(path: Path, customer_id: str) -> bool:
    """A service update for the customer is waiting to be sent (held)."""
    return _count(path, "SELECT COUNT(*) FROM guardian_notices WHERE customer_id = ? AND status = 'held'",
                  (customer_id,)) > 0


def service_hold_reason(path: Path, customer_id: str, now: datetime, policy: dict[str, Any]) -> str | None:
    """Why a service update must wait now, or None to send it."""
    if quiet(now, policy["guardian"]):
        return QUIET_HOURS
    if sent_on(path, customer_id, now.strftime("%Y-%m-%d")) >= int(policy["guardian"]["max_updates_per_day"]):
        return DAILY_LIMIT
    return None


def invite_hold_reason(path: Path, customer_id: str, now: datetime, policy: dict[str, Any]) -> str | None:
    """Why an invite must wait now, or None to send it: service updates always come first."""
    if quiet(now, policy["guardian"]):
        return QUIET_HOURS
    if service_waiting(path, customer_id):
        return SERVICE_FIRST
    room = int(policy["guardian"]["max_updates_per_day"]) - int(policy["stylist"]["reserve_for_service"])
    if sent_on(path, customer_id, now.strftime("%Y-%m-%d")) >= room:
        return DAILY_LIMIT
    return None
