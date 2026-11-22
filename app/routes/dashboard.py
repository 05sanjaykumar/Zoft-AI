"""A one-page dashboard for testing and the demo: calls, bookings, call me.

    GET /dashboard        the page (plain HTML + JS, reads the API below)
    GET /dashboard/info   our number, active calls, the outbound example body
"""

import json
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.calls import store
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
