"""HTTP tools: results, errors and timeouts all reach the model as results."""

import asyncio
import json

import httpx

from app.calls.store import CallRecord
from app.schemas import CallConfig, ToolSpec
from app.voice.http_tools import build_http_tools, run_http_tool

SPEC = ToolSpec.model_validate({
    "name": "check_slots",
    "description": "Find free slots",
    "parameters": {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]},
    "http": {"method": "POST", "url": "https://clinic.test/slots", "timeoutMs": 500},
})


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def test_success_passes_body_through():
    def handler(request):
        assert json.loads(request.content) == {"date": "2026-12-01"}
        return httpx.Response(200, json={"free": ["09:00"]})

    result, ok = run(run_http_tool(SPEC, {"date": "2026-12-01"}, client_for(handler)))
    assert ok and result == {"free": ["09:00"]}


def test_http_error_becomes_a_spoken_error():
    def handler(request):
        return httpx.Response(409, json={"error": "slot_taken", "alternatives": ["10:30"]})

    result, ok = run(run_http_tool(SPEC, {"date": "2026-12-01"}, client_for(handler)))
    assert not ok and result["error"] == "http_409"
    assert result["detail"]["alternatives"] == ["10:30"]


def test_timeout_becomes_a_spoken_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    result, ok = run(run_http_tool(SPEC, {"date": "2026-12-01"}, client_for(handler)))
    assert not ok and result["error"] == "timeout"
    assert "500 ms" in result["detail"]


class FakeParams:
    def __init__(self, arguments):
        self.arguments = arguments
        self.result = None

    async def result_callback(self, result, **kwargs):
        self.result = result


def test_handler_logs_the_run_on_the_call():
    config = CallConfig(systemPrompt="x", greeting="hi", direction="inbound", tools=[SPEC])
    record = CallRecord(config=config)
    client = client_for(lambda request: httpx.Response(200, json={"free": []}))
    [schema] = build_http_tools(config.tools, record, client)

    assert schema.name == "check_slots" and schema.required == ["date"]
    params = FakeParams({"date": "2026-12-01"})
    run(schema.handler(params))

    assert params.result == {"free": []}
    [entry] = record.tools
    assert entry["name"] == "check_slots" and entry["ok"] is True
    assert entry["arguments"] == {"date": "2026-12-01"}
