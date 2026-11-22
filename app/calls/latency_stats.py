"""Latency across calls: median and p95 per stage, by kind of turn.

One place for the maths, used by both the dashboard (GET /dashboard/latency)
and tools/measure.py (REPORT.md), so the two can't disagree.

Each call record's `latency` list has one entry per agent reply, from Pipecat's
UserBotLatencyObserver: total (caller stopped -> agent's first audio frame,
server side), turn detection, and each service's time to first byte. The phone
network both ways is not in these; tools/measure.py twilio adds the
Twilio <-> server leg from Twilio's own recording.
"""

import statistics

STAGES = (
    ("total", "Total (caller stopped → first audio)"),
    ("turn", "Turn detection (VAD + 0.8s pause)"),
    ("stt", "Speech-to-text, time to first byte"),
    ("llm", "Model, time to first token (all model calls in the turn)"),
    ("tts", "Text-to-speech, time to first audio"),
    ("tools", "Tool time"),
)
KINDS = ("all", "plain", "tool", "knowledge")
TARGET_TURNS = 30   # the brief: "at least 30 turns" on real calls


def pct(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(p / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def is_real_call(call: dict) -> bool:
    """A call that went through Twilio, not the fake client or a test."""
    sid = call.get("twilio_sid") or ""
    return sid.startswith("CA") and not sid.startswith(("CAfake", "CAtest"))


def turn_kind(turn: dict) -> str:
    names = [t["name"] for t in turn.get("tools", []) if t["name"] != "end_call"]
    if any(k in n for n in names for k in ("faq", "knowledge", "kb", "lookup")):
        return "knowledge"
    return "tool" if names else "plain"


def measured_turns(calls: list[dict]) -> list[dict]:
    """Replies with a total (the greeting has none: nobody spoke before it)."""
    return [dict(t, call=c["id"]) for c in calls for t in c.get("latency", [])
            if t.get("total_secs") is not None]


def stage_values(turns: list[dict], stage: str) -> list[float]:
    if stage == "total":
        return [t["total_secs"] for t in turns]
    if stage == "turn":
        return [t["user_turn_secs"] for t in turns if t.get("user_turn_secs") is not None]
    if stage == "tools":
        secs = [sum(x["secs"] for x in t.get("tools", []) if x["name"] != "end_call") for t in turns
                if any(x["name"] != "end_call" for x in t.get("tools", []))]
        return secs
    return [t["ttfb_secs"][stage] for t in turns if stage in (t.get("ttfb_secs") or {})]


def summarise(values: list[float]) -> dict:
    return {"n": len(values),
            "median": round(statistics.median(values), 3) if values else None,
            "p95": round(pct(values, 95), 3) if values else None}


def latency_table(calls: list[dict]) -> dict:
    turns = measured_turns(calls)
    groups = {}
    for kind in KINDS:
        group = turns if kind == "all" else [t for t in turns if turn_kind(t) == kind]
        groups[kind] = {stage: summarise(stage_values(group, stage)) for stage, _ in STAGES}
    return {
        "calls": len(calls),
        "turns": len(turns),
        "target_turns": TARGET_TURNS,
        "stages": [{"key": k, "label": label} for k, label in STAGES],
        "groups": groups,
        "measured": "server side, per reply (Pipecat UserBotLatencyObserver); the phone "
                    "network both ways is not included",
    }
