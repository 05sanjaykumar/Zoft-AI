"""Bright Smile Dental, the mock clinic the agent's HTTP tools call.

    POST /mock/slots        check_slots  {date}
    POST /mock/book         book_slot    {date, time, name, phone?, reason?}
    POST /mock/call-ended   receives the after-call webhook (for testing)
    GET  /mock/bookings     everything booked, for checking a demo
"""

import json
import time as clock
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.clinic import db
from app.config import DATA_DIR

router = APIRouter(prefix="/mock", tags=["mock clinic"])
WEBHOOK_LOG = DATA_DIR / "webhooks.jsonl"


class SlotsRequest(BaseModel):
    date: str


class BookRequest(BaseModel):
    date: str
    time: str
    name: str
    phone: str | None = None
    reason: str | None = None


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@router.post("/slots")
def check_slots(body: SlotsRequest):
    day = _parse_day(body.date)
    if day is None:
        return JSONResponse({"error": "bad_date", "detail": "Use YYYY-MM-DD."}, status_code=400)
    conn = db.connect()
    try:
        free = db.free_slots(conn, day)
        result = {"date": day.isoformat(), "weekday": day.strftime("%A"), "free": free}
        if not free:
            result["reason"] = "closed" if not db.all_slots(day) else "fully_booked_or_past"
            nxt = db.next_available(conn, day)
            if nxt:
                result["next_available"] = {"date": nxt[0], "free": nxt[1]}
        return result
    finally:
        conn.close()


@router.post("/book")
def book_slot(body: BookRequest):
    day = _parse_day(body.date)
    if day is None:
        return JSONResponse({"error": "bad_date", "detail": "Use YYYY-MM-DD."}, status_code=400)
    conn = db.connect()
    try:
        booking_id = db.book(conn, day, body.time, body.name, body.phone, body.reason)
        return {"booked": True, "booking_id": booking_id, "date": day.isoformat(),
                "time": body.time, "name": body.name}
    except db.SlotTaken as exc:
        # The second caller for a slot lands here. Hand back alternatives so
        # the agent can offer another time in the same breath.
        return JSONResponse(
            {"error": "slot_taken", "detail": str(exc),
             "alternatives": db.free_slots(conn, day)[:3]},
            status_code=409,
        )
    except db.InvalidSlot as exc:
        return JSONResponse({"error": "invalid_slot", "detail": str(exc)}, status_code=400)
    finally:
        conn.close()


@router.get("/bookings")
def list_bookings():
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM bookings ORDER BY date, time")]
    finally:
        conn.close()


@router.post("/call-ended")
async def call_ended(request: Request):
    payload = await request.json()
    with WEBHOOK_LOG.open("a") as f:
        f.write(json.dumps({"received_at": clock.time(), "payload": payload}) + "\n")
    return {"ok": True}
