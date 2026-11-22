"""The create-call API.

    POST /calls            outbound: dial `to` now. inbound: set how calls
                           to our Twilio number behave from now on.
    GET  /calls            recent calls
    GET  /calls/{id}       status, duration, transcript, recording, tools, cost
    POST /calls/{id}/end   hang up a live call
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from loguru import logger
from twilio.rest import Client

from app.calls import store
from app.config import PUBLIC_HOST, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER
from app.schemas import CallConfig

router = APIRouter(prefix="/calls", tags=["calls"])


def _require_twilio() -> Client:
    missing = [name for name, value in (
        ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
        ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
        ("TWILIO_PHONE_NUMBER", TWILIO_PHONE_NUMBER),
        ("PUBLIC_HOST", PUBLIC_HOST),
    ) if not value]
    if missing:
        raise HTTPException(503, f"Telephony is not configured. Missing in .env: {', '.join(missing)}")
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


@router.post("", status_code=201)
def create_call(config: CallConfig):
    if config.direction == "inbound":
        store.set_inbound_config(config)
        logger.info(f"Inbound config set for {TWILIO_PHONE_NUMBER}")
        return {
            "direction": "inbound",
            "number": TWILIO_PHONE_NUMBER,
            "message": "Calls to this number now use this config.",
        }

    client = _require_twilio()
    record = store.create(config, from_number=TWILIO_PHONE_NUMBER, to_number=config.to)
    try:
        call = client.calls.create(
            to=config.to,
            from_=TWILIO_PHONE_NUMBER,
            url=f"https://{PUBLIC_HOST}/twilio/voice?call_id={record.id}",
            status_callback=f"https://{PUBLIC_HOST}/twilio/status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            time_limit=config.maxDurationSeconds + 30,  # backstop if our own timer fails
        )
    except Exception as exc:  # twilio raises its own error types
        record.status = "failed"
        record.end_reason = f"twilio_error: {exc}"
        store.save(record)
        logger.error(f"Outbound call to {config.to} failed: {exc}")
        raise HTTPException(502, f"Twilio refused the call: {exc}")

    record.twilio_sid = call.sid
    logger.info(f"Outbound call {record.id} ({call.sid}) placed to {config.to}")
    return {"id": record.id, "status": record.status, "twilio_sid": call.sid, "to": config.to}


@router.get("")
def list_calls():
    return [
        {"id": r.id, "direction": r.direction, "status": r.status,
         "to": r.to_number, "from": r.from_number, "duration_seconds": r.duration_seconds,
         "created_at": r.created_at, "end_reason": r.end_reason,
         "turns": len(r.transcript), "tools": len(r.tools),
         "cost_inr": (r.cost or {}).get("total_inr")}
        for r in store.all_records()
    ]


@router.get("/{call_id}")
def get_call(call_id: str):
    record = store.get(call_id)
    if record is None:
        raise HTTPException(404, "No such call")
    return record.to_public()


@router.get("/{call_id}/recording")
def get_recording(call_id: str):
    record = store.get(call_id)
    if record is None or not record.recording_path or not Path(record.recording_path).exists():
        raise HTTPException(404, "No recording for this call")
    # Stereo WAV: caller on the left channel, agent on the right.
    return FileResponse(record.recording_path, media_type="audio/wav",
                        filename=f"call-{call_id}.wav")


@router.post("/{call_id}/end")
async def end_call(call_id: str):
    record = store.get(call_id)
    if record is None:
        raise HTTPException(404, "No such call")
    if record.status in store.FINAL_STATUSES:
        return {"id": call_id, "status": record.status, "message": "Call already ended."}

    record.end_reason = "ended_by_api"
    task = store.TASKS.get(call_id)
    if task is not None:
        await task.cancel()   # auto_hang_up on the serializer hangs up Twilio's side
    elif record.twilio_sid:
        # Not connected yet (still ringing): cancel it at Twilio.
        _require_twilio().calls(record.twilio_sid).update(status="canceled")
        record.status = "canceled"
        store.save(record)
    return {"id": call_id, "status": record.status, "message": "Ending call."}
