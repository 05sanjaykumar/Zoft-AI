"""After the call: cost per component, the summary, the webhook, the recorder."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx

from app.calls.costs import call_cost
from app.calls.finalize import finalize, latency_summary, percentile, summary
from app.calls.recorder import CallRecorder
from app.calls.store import CallRecord
from app.schemas import CallConfig

PRICING = {
    "as_of": "2026-11-19",
    "usd_inr": {"rate": 80.0},
    "telephony": {"inbound_per_min_usd": 0.01, "outbound_per_min_usd": 0.02},
    "stt": {"per_min_usd": 0.006},
    "llm": {"input_per_mtok_usd": 0.15, "cached_input_per_mtok_usd": 0.075, "output_per_mtok_usd": 0.6},
    "tts": {"per_char_usd": 0.0},
    "server": {"per_hour_usd": 0.10, "concurrent_calls": 10},
}


def record(direction="inbound", seconds=90.0, webhook=None) -> CallRecord:
    config = CallConfig(systemPrompt="x", greeting="hi", direction=direction,
                        to="+911234567890" if direction == "outbound" else None,
                        webhookUrl=webhook)
    r = CallRecord(config=config, direction=direction)
    r.started_at = time.time() - seconds
    r.ended_at = r.started_at + seconds
    r.usage.update(llm_prompt_tokens=10_000, llm_completion_tokens=500, llm_calls=4,
                   stt_audio_seconds=seconds, tts_characters=600)
    return r


def test_cost_per_component():
    cost = call_cost(record(seconds=90), PRICING)
    c = cost["components"]
    assert c["telephony"]["usd"] == 0.02               # 90s bills as 2 minutes inbound
    assert round(c["stt"]["usd"], 6) == 0.009          # 1.5 min x 0.006
    assert round(c["llm"]["usd"], 6) == round(0.0015 + 0.0003, 6)
    assert c["tts"]["usd"] == 0.0
    assert round(c["server"]["usd"], 6) == round(90 / 3600 * 0.10 / 10, 6)
    assert cost["total_inr"] == round(cost["total_usd"] * 80, 4)
    assert cost["per_minute_inr"] == round(cost["total_usd"] * 80 / 1.5, 4)


def test_outbound_uses_outbound_rate():
    cost = call_cost(record(direction="outbound", seconds=30), PRICING)
    assert cost["components"]["telephony"]["usd"] == 0.02   # 1 min x 0.02


def test_percentile_and_latency_summary():
    assert percentile([], 95) is None
    assert percentile(list(range(1, 21)), 95) == 19
    r = record()
    r.latency = [{"total_secs": s} for s in (1.0, 2.0, 3.0)] + [{"total_secs": None}]
    s = latency_summary(r)
    assert s["turns"] == 3 and s["median_secs"] == 2.0 and s["p95_secs"] == 3.0


def test_webhook_receives_the_summary(monkeypatch):
    import app.calls.costs as costs
    monkeypatch.setattr(costs, "load_pricing", lambda: PRICING)
    received = {}

    def handler(request):
        received.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    r = record(webhook="https://hooks.test/call-ended")
    r.transcript = [{"role": "user", "text": "hello"}]
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(finalize(r, client))

    assert received["id"] == r.id and received["transcript"][0]["text"] == "hello"
    assert received["cost"]["components"]["telephony"]["inr"] > 0
    assert r.webhook["ok"] is True and r.webhook["status_code"] == 200


def test_webhook_failure_is_recorded_not_raised(monkeypatch):
    import app.calls.costs as costs
    monkeypatch.setattr(costs, "load_pricing", lambda: PRICING)

    def handler(request):
        raise httpx.ConnectError("down", request=request)

    r = record(webhook="https://hooks.test/down")
    asyncio.run(finalize(r, httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    assert r.webhook == {"url": "https://hooks.test/down", "ok": False, "error": "ConnectError"}


def test_recorder_turns_and_latency():
    r = record()
    rec = CallRecorder(r)
    rec.add_turn("user", "  do you have slots monday  ", r.started_at + 5, r.started_at + 7)
    rec.add_turn("agent", "", r.started_at + 8, r.started_at + 9)      # empty: dropped
    rec.add_turn("agent", "Yes, at nine.", r.started_at + 8, r.started_at + 10, interrupted=True)
    assert [t["role"] for t in r.transcript] == ["user", "agent"]
    assert r.transcript[0]["text"] == "do you have slots monday" and r.transcript[0]["t_start"] == 5.0
    assert r.transcript[1]["interrupted"] is True

    breakdown = SimpleNamespace(
        ttfb=[SimpleNamespace(processor="NvidiaSTTService#0", duration_secs=0.3),
              SimpleNamespace(processor="GroqLLMService#0", duration_secs=0.5),
              SimpleNamespace(processor="GroqLLMService#0", duration_secs=0.4),
              SimpleNamespace(processor="KokoroTTSService#0", duration_secs=0.9)],
        user_turn_secs=0.85,
        function_calls=[SimpleNamespace(function_name="check_slots", duration_secs=0.03)],
    )
    rec.add_latency(2.4, breakdown)
    [entry] = r.latency
    assert entry["total_secs"] == 2.4 and entry["ttfb_secs"] == {"stt": 0.3, "llm": 0.9, "tts": 0.9}
    assert entry["tools"] == [{"name": "check_slots", "secs": 0.03}]


def test_summary_has_what_the_brief_asks_for():
    s = summary(record())
    for key in ("transcript", "tools", "recording_url", "cost", "duration_seconds", "latency"):
        assert key in s
