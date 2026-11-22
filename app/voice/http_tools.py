"""Turn the tools in a call's JSON config into tools the model can call.

Each tool is an HTTP endpoint. The model's arguments become the request body
(or the query string for GET), and whatever comes back becomes the tool
result. A timeout or an error is not raised: it is handed to the model as a
result, so the agent can tell the caller out loud instead of going silent.
Every run is logged on the call record (inputs, result, time taken).
"""

import re
import time

import httpx
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import FunctionCallResultProperties
from pipecat.services.llm_service import FunctionCallParams

from app.calls.store import CallRecord
from app.schemas import ToolSpec
from app.voice.spelling import spelled_runs


async def run_http_tool(spec: ToolSpec, arguments: dict, client: httpx.AsyncClient) -> tuple[dict, bool]:
    """Call the tool's endpoint. Returns (result for the model, ok)."""
    http = spec.http
    try:
        response = await client.request(
            http.method,
            http.url,
            json=arguments if http.method != "GET" else None,
            params=arguments if http.method == "GET" else None,
            headers=http.headers,
            timeout=http.timeoutMs / 1000,
        )
    except httpx.TimeoutException:
        return {
            "error": "timeout",
            "detail": f"{spec.name} did not answer within {http.timeoutMs} ms. "
                      "Tell the caller the system is slow and offer to try again.",
        }, False
    except httpx.HTTPError as exc:
        return {
            "error": "unreachable",
            "detail": f"{spec.name} could not be reached ({type(exc).__name__}). "
                      "Tell the caller and offer another way to help.",
        }, False

    try:
        body = response.json()
    except ValueError:
        body = response.text

    if response.is_success:
        return (body if isinstance(body, dict) else {"result": body}), True
    return {"error": f"http_{response.status_code}", "detail": body}, False


# "[Caller]", "<name>", "{date}": the model filling a slot it doesn't have yet.
_PLACEHOLDER = re.compile(r"^\s*[\[<{].*[\]>}]\s*$")


# Values models invent when they didn't catch the real one. On a real call the
# caller's name was misheard ("Sunday") and the model booked "John Doe".
_MADE_UP = {"john doe", "jane doe", "john smith", "jane smith", "caller", "the caller", "customer",
            "patient", "unknown", "n/a", "na", "none", "null", "name", "your name", "test", "user"}


def placeholder_arguments(arguments: dict) -> list[str]:
    """Names of string arguments that hold a placeholder instead of a value."""
    return [k for k, v in arguments.items() if isinstance(v, str)
            and (_PLACEHOLDER.match(v) or v.strip().lower().strip(".") in _MADE_UP)]


def _caller_heard(record: CallRecord | None) -> tuple[set[str], str]:
    """Words the caller said on this call, and the letters they spelled.

    "S a n j a y" or "Yes, A and J" (app/voice/spelling.py) count as spelled.
    """
    words, spelled = set(), []
    for turn in (record.transcript if record else []):
        if turn.get("role") != "user":
            continue
        text = turn.get("text") or ""
        words.update(re.findall(r"[a-z]+", text.lower()))
        spelled.extend(spelled_runs(text))
    return words, " ".join(spelled)


def unheard_names(arguments: dict, record: CallRecord | None) -> list[str]:
    """Name arguments with a word the caller never said or spelled.

    On a real call the model tried to book 11:00 for "Arun Kumar", a name the
    caller never gave. Without a transcript (tests, no record) nothing is checked.
    """
    if record is None or not any(t.get("role") == "user" for t in record.transcript):
        return []
    words, spelled = _caller_heard(record)
    out = []
    for key, value in arguments.items():
        if "name" not in key.lower() or not isinstance(value, str):
            continue
        for part in re.findall(r"[a-z]+", value.lower()):
            if part not in words and part not in spelled:
                out.append(key)
                break
    return out


def check_before_running(arguments: dict, watcher, record: CallRecord | None = None) -> tuple[dict, bool] | None:
    """Why a tool call shouldn't reach the endpoint, as (result, run_llm), or None.

    Both cases come from a real call: the model asked "which time would you
    prefer?" and, in the same reply, booked 10:30 for "[Caller]". That junk
    booking then blocked the caller's real one.
    """
    if watcher is not None and watcher.asked:
        # The reply handed the turn to the caller; wait for their answer.
        return {"error": "not_run", "detail": "You just asked the caller a question. Wait for "
                                               "their answer before calling tools."}, False
    if missing := placeholder_arguments(arguments):
        return {"error": "not_run", "detail": f"{', '.join(missing)} is a placeholder, not what the "
                                               "caller said. Ask the caller for it."}, True
    if unheard := unheard_names(arguments, record):
        return {"error": "not_run", "detail": f"The caller never said the {', '.join(unheard)} you used. "
                                               "Ask for their name, have them spell it, and use exactly that."}, True
    return None


def build_http_tools(tools: list[ToolSpec], record: CallRecord,
                     client: httpx.AsyncClient, watcher=None) -> list[FunctionSchema]:
    return [_schema_for(spec, record, client, watcher) for spec in tools]


def _schema_for(spec: ToolSpec, record: CallRecord, client: httpx.AsyncClient,
                watcher=None) -> FunctionSchema:
    async def handler(params: FunctionCallParams):
        arguments = dict(params.arguments or {})
        started = time.monotonic()
        if refused := check_before_running(arguments, watcher, record):
            result, run_llm = refused
            record.tools.append({"name": spec.name, "arguments": arguments, "result": result,
                                 "ok": False, "ms": 0, "at": time.time()})
            logger.info(f"[{record.id}] tool {spec.name}({arguments}) not run: {result['detail']}")
            await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=run_llm))
            return
        if watcher is not None:
            watcher.tools_running += 1
        try:
            result, ok = await run_http_tool(spec, arguments, client)
        finally:
            if watcher is not None:
                watcher.tools_running -= 1
        ms = round((time.monotonic() - started) * 1000)
        record.tools.append({
            "name": spec.name,
            "arguments": arguments,
            "result": result,
            "ok": ok,
            "ms": ms,
            "at": time.time(),
        })
        logger.info(f"[{record.id}] tool {spec.name}({arguments}) -> ok={ok} in {ms} ms")
        await params.result_callback(result)

    parameters = spec.parameters or {}
    return FunctionSchema(
        name=spec.name,
        description=spec.description,
        properties=parameters.get("properties", {}),
        required=parameters.get("required", []),
        handler=handler,
    )
