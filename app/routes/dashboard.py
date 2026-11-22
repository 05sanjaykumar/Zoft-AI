"""A one-page dashboard for testing and the demo: calls, bookings, call me.

    GET /dashboard        the page (plain HTML + JS, reads the API below)
    GET /dashboard/info   our number, active calls, the outbound example body
    GET /dashboard/latency  median / p95 per stage across calls (real ones by default)
"""

import json
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.calls import store
from app.calls.latency_stats import is_real_call, latency_table
from app.config import PUBLIC_HOST, TWILIO_PHONE_NUMBER

ROOT = Path(__file__).resolve().parent.parent.parent
PAGE = ROOT / "app" / "static" / "dashboard.html"
OUTBOUND_EXAMPLE = ROOT / "examples" / "outbound-clinic.json"

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("", include_in_schema=False)
def page():
    return FileResponse(PAGE, media_type="text/html")


@router.get("/info")
def info():
    return {
        "phone_number": TWILIO_PHONE_NUMBER or None,
        "public_host": PUBLIC_HOST or None,
        "active_calls": len(store.TASKS),
        "outbound_example": json.loads(OUTBOUND_EXAMPLE.read_text()),
    }


@router.get("/latency")
def latency(include_fake: bool = False):
    calls = [r.to_public() for r in store.all_records()]
    if not include_fake:
        calls = [c for c in calls if is_real_call(c)]
    return latency_table(calls)
