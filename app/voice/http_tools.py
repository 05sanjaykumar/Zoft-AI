"""Turn the tools in a call's JSON config into tools the model can call.

Each tool is an HTTP endpoint. The model's arguments become the request body
(or the query string for GET), and whatever comes back becomes the tool
result. A timeout or an error is not raised: it is handed to the model as a
result, so the agent can tell the caller out loud instead of going silent.
Every run is logged on the call record (inputs, result, time taken).
"""

import time

import httpx
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from app.calls.store import CallRecord
from app.schemas import ToolSpec


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


def build_http_tools(tools: list[ToolSpec], record: CallRecord,
                     client: httpx.AsyncClient) -> list[FunctionSchema]:
    schemas = []
    for spec in tools:
        schemas.append(_schema_for(spec, record, client))
    return schemas


def _schema_for(spec: ToolSpec, record: CallRecord, client: httpx.AsyncClient) -> FunctionSchema:
    async def handler(params: FunctionCallParams):
        arguments = dict(params.arguments or {})
        started = time.monotonic()
        result, ok = await run_http_tool(spec, arguments, client)
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
