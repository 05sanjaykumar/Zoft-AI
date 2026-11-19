"""The phone channel: Twilio webhooks and the media stream.

    caller --PSTN-- Twilio --WebSocket (8 kHz mu-law)-- /twilio/stream -- pipeline

1. Twilio asks POST /twilio/voice what to do with a call. We answer with
   TwiML that opens a two-way media stream, passing our call id along.
2. Twilio opens the WebSocket at /twilio/stream and audio flows both ways.
3. Twilio reports call progress to POST /twilio/status (outbound calls).
"""

import asyncio
import json
import time

import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import PlainTextResponse
from loguru import logger
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.runner import PipelineRunner
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.calls import store
from app.calls.defaults import default_inbound_config
from app.calls.finalize import finalize
from app.config import PUBLIC_HOST, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_DUAL_RECORDING
from app.voice.call_controls import max_duration_timer
from app.voice.pipeline import build_call_task

router = APIRouter(prefix="/twilio", tags=["telephony"])

TELEPHONY_SAMPLE_RATE = 8000
CALL_ID_PARAM = "call_id"


# --------------------------------------------------------------- the webhook


@router.post("/voice", response_class=PlainTextResponse)
async def voice_webhook(request: Request):
    """Tell Twilio to open a media stream for this call.

    An outbound call we placed carries ?call_id= in the webhook URL. Anything
    else is an inbound call: it gets a fresh record built from the inbound
    config set through POST /calls (or the default clinic agent).
    """
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    call_id = request.query_params.get(CALL_ID_PARAM)

    record = store.get(call_id) if call_id else None
    if record is None:
        config = store.get_inbound_config() or default_inbound_config()
        record = store.create(
            config,
            twilio_sid=call_sid,
            from_number=str(form.get("From", "")),
            to_number=str(form.get("To", "")),
        )
        record.direction = "inbound"
    record.status = "ringing" if record.status == "queued" else record.status
    record.twilio_sid = record.twilio_sid or call_sid
    logger.info(f"TwiML requested for {record.direction} call {record.id} ({call_sid})")

    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=f"wss://{PUBLIC_HOST}/twilio/stream")
    stream.parameter(name=CALL_ID_PARAM, value=record.id)
    response.append(connect)
    return str(response)


@router.post("/status")
async def status_callback(request: Request):
    """Twilio's progress reports: ringing, in-progress, completed, no-answer…"""
    form = await request.form()
    record = store.find_by_twilio_sid(str(form.get("CallSid", "")))
    status = str(form.get("CallStatus", ""))
    if record is not None and status:
        # Don't let a late "in-progress" overwrite a final status.
        if record.status not in store.FINAL_STATUSES:
            record.status = status
        if status in store.FINAL_STATUSES:
            record.ended_at = record.ended_at or time.time()
            if record.started_at is None:
                # Never connected (no-answer, busy, failed): no stream will
                # finalize it, so do it here. A connected call is finalized
                # when its stream closes.
                await finalize(record)
            else:
                store.save(record)
    return {"ok": True}


async def _start_twilio_recording(record, call_sid: str) -> None:
    """Measurement only (TWILIO_DUAL_RECORDING=true): record the call at Twilio."""
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        rec = await asyncio.to_thread(
            client.calls(call_sid).recordings.create, recording_channels="dual")
        record.twilio_recording_sid = rec.sid
        logger.info(f"[{record.id}] Twilio dual-channel recording {rec.sid} started")
    except Exception as exc:  # measurement must never break a call
        logger.warning(f"[{record.id}] could not start Twilio recording: {exc}")


# ---------------------------------------------------------- the media stream


async def _read_stream_start(websocket: WebSocket) -> dict:
    """Twilio sends `connected` then `start` before any audio."""
    messages = websocket.iter_text()
    await messages.__anext__()                       # {"event": "connected"}
    start = json.loads(await messages.__anext__())   # {"event": "start", ...}
    return start.get("start", {})


@router.websocket("/stream")
async def twilio_stream(websocket: WebSocket):
    await websocket.accept()

    start = await _read_stream_start(websocket)
    stream_sid = start.get("streamSid")
    call_sid = start.get("callSid")
    call_id = (start.get("customParameters") or {}).get(CALL_ID_PARAM)
    record = store.get(call_id) if call_id else None

    if not stream_sid or record is None:
        logger.error(f"Stream without a known call (stream={stream_sid}, call_id={call_id}); closing")
        await websocket.close()
        return

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=TWILIO_ACCOUNT_SID,
        auth_token=TWILIO_AUTH_TOKEN,
        # Hang the call up when the pipeline ends, so a finished conversation
        # doesn't leave the line open and billing. Needs Twilio credentials, so
        # it is off for the local fake client (tools/fake_twilio_call.py).
        params=TwilioFrameSerializer.InputParams(
            auto_hang_up=bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)),
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            serializer=serializer,
        ),
    )

    async with httpx.AsyncClient() as client:
        task = build_call_task(transport, record, client)

        @transport.event_handler("on_client_connected")
        async def _greet(transport, websocket):
            # The greeting goes straight to TTS (no model call) so the caller
            # hears a voice at once; the assistant aggregator records it.
            if record.config.firstSpeaker == "agent" and record.config.greeting:
                await task.queue_frames([TTSSpeakFrame(record.config.greeting)])

        @transport.event_handler("on_client_disconnected")
        async def _hangup(transport, websocket):
            logger.info(f"Caller hung up ({record.id}); ending the pipeline")
            record.end_reason = record.end_reason or "caller_hung_up"
            await task.cancel()

        record.status = "in-progress"
        record.started_at = time.time()
        store.TASKS[record.id] = task
        logger.info(f"Call {record.id} connected ({record.direction})")
        if TWILIO_DUAL_RECORDING and call_sid:
            asyncio.create_task(_start_twilio_recording(record, call_sid))
        timer = asyncio.create_task(max_duration_timer(task, record))
        try:
            await PipelineRunner(handle_sigint=False).run(task)
        finally:
            timer.cancel()
            store.TASKS.pop(record.id, None)
            record.ended_at = record.ended_at or time.time()
            if record.status not in store.FINAL_STATUSES:
                record.status = "completed"
            logger.info(f"Call {record.id} ended after {record.duration_seconds}s")
            await finalize(record, client)
