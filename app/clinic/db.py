"""The mock clinic's database: one bookings table in local SQLite.

Free slots are derived from opening hours minus bookings, so there is nothing
to pre-generate. The double-booking guarantee is the UNIQUE (date, time)
constraint: two callers racing for one slot both reach the INSERT, and the
database lets exactly one of them through. No application check can do that.

The database is local on purpose. MediConnect's Postgres was in Sydney at
~0.46s per round trip, which a caller hears as silence on every tool call.
"""

import sqlite3
from datetime import date, datetime, time, timedelta

from app.config import CLINIC_DB_PATH

SLOT_MINUTES = 30
OPEN_AT, CLOSE_AT = time(9, 0), time(17, 0)
LUNCH = (time(13, 0), time(14, 0))
CLOSED_WEEKDAYS = {6}  # Sunday

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,      -- YYYY-MM-DD
    time         TEXT NOT NULL,      -- HH:MM, 24-hour
    patient_name TEXT NOT NULL,
    phone        TEXT,
    reason       TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (date, time)
);
"""


class SlotTaken(Exception):
    pass


class InvalidSlot(Exception):
    pass


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or CLINIC_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def all_slots(day: date) -> list[str]:
    if day.weekday() in CLOSED_WEEKDAYS:
        return []
    slots, t = [], datetime.combine(day, OPEN_AT)
    end = datetime.combine(day, CLOSE_AT)
    while t < end:
        if not (LUNCH[0] <= t.time() < LUNCH[1]):
            slots.append(t.strftime("%H:%M"))
        t += timedelta(minutes=SLOT_MINUTES)
    return slots


def free_slots(conn: sqlite3.Connection, day: date, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    taken = {r["time"] for r in conn.execute(
        "SELECT time FROM bookings WHERE date = ?", (day.isoformat(),))}
    return [s for s in all_slots(day)
            if s not in taken and datetime.combine(day, time.fromisoformat(s)) > now]


def next_available(conn: sqlite3.Connection, after: date, days: int = 14) -> tuple[str, list[str]] | None:
    for i in range(1, days + 1):
        day = after + timedelta(days=i)
        free = free_slots(conn, day)
        if free:
            return day.isoformat(), free[:3]
    return None


def book(conn: sqlite3.Connection, day: date, slot: str, name: str,
         phone: str | None = None, reason: str | None = None) -> int:
    if slot not in all_slots(day):
        raise InvalidSlot(f"{slot} on {day.isoformat()} is not a clinic slot")
    if datetime.combine(day, time.fromisoformat(slot)) <= datetime.now():
        raise InvalidSlot(f"{slot} on {day.isoformat()} is in the past")
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO bookings (date, time, patient_name, phone, reason) VALUES (?, ?, ?, ?, ?)",
                (day.isoformat(), slot, name, phone, reason),
            )
    except sqlite3.IntegrityError:
        raise SlotTaken(f"{slot} on {day.isoformat()} is already booked")
    return cur.lastrowid
