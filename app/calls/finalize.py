"""What happens when a call ends: cost it, store it, POST the summary.

Called once per call, from whichever path sees the end first: the media
stream closing (a connected call) or Twilio's final status callback (a call
that never connected: no-answer, busy, failed).
"""

import statistics
import time

import httpx
from loguru import logger

from app.calls import store
from app.calls.costs import call_cost
from app.calls.store import CallRecord
from app.config import PUBLIC_HOST

WEBHOOK_TIMEOUT_SECS = 5.0
_FINALIZED: set[str] = set()


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; fine for tens of turns."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def latency_summary(record: CallRecord) -> dict:
    totals = [t["total_secs"] for t in record.latency if t.get("total_secs") is not None]
    return {
        "turns": len(totals),
        "median_secs": round(statistics.median(totals), 3) if totals else None,
        "p95_secs": percentile(totals, 95),
        "measured": "server side: caller stopped speaking -> agent's first audio frame; "
                    "excludes the phone network both ways",
    }


def summary(record: CallRecord) -> dict:
    return {
        "id": record.id,
        "direction": record.direction,
        "status": record.status,
        "from": record.from_number,
        "to": record.to_number,
        "twilio_sid": record.twilio_sid,
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "duration_seconds": record.duration_seconds,
        "end_reason": record.end_reason,
        "transcript": record.transcript,
        "tools": record.tools,
        "recording_url": record.recording_url,
        "cost": record.cost,
        "latency": latency_summary(record),
    }


async def post_webhook(record: CallRecord, client: httpx.AsyncClient | None = None) -> None:
    url = record.config.webhookUrl
    if not url:
        return
    started = time.monotonic()
    try:
        if client is None:
            async with httpx.AsyncClient() as own:
                response = await own.post(url, json=summary(record), timeout=WEBHOOK_TIMEOUT_SECS)
        else:
            response = await client.post(url, json=summary(record), timeout=WEBHOOK_TIMEOUT_SECS)
        record.webhook = {"url": url, "status_code": response.status_code, "ok": response.is_success,
                          "ms": round((time.monotonic() - started) * 1000)}
    except httpx.HTTPError as exc:
        record.webhook = {"url": url, "ok": False, "error": type(exc).__name__}
    logger.info(f"[{record.id}] webhook {url} -> {record.webhook}")


async def finalize(record: CallRecord, client: httpx.AsyncClient | None = None) -> None:
    if record.id in _FINALIZED:
        return
    _FINALIZED.add(record.id)
    record.ended_at = record.ended_at or time.time()
    if record.recording_path and PUBLIC_HOST:
        record.recording_url = f"https://{PUBLIC_HOST}/calls/{record.id}/recording"
    try:
        record.cost = call_cost(record)
    except Exception as exc:  # a pricing typo must not lose the call record
        logger.error(f"[{record.id}] cost calculation failed: {exc}")
    store.save(record)
    await post_webhook(record, client)
    store.save(record)
