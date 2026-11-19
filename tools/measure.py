#!/usr/bin/env python
"""Turn call records and recordings into the numbers REPORT.md needs.

    python tools/measure.py latency                 # all calls in data/calls
    python tools/measure.py latency --since 2026-11-20
    python tools/measure.py audio data/recordings/<id>.wav
    python tools/measure.py twilio <call_id>        # needs TWILIO_DUAL_RECORDING=true on that call

latency  Server-side numbers from each call's record (UserBotLatencyObserver):
         caller stopped -> agent's first audio, split into turn detection, STT,
         LLM, TTS and tool time. Median and p95, for plain turns and tool turns.

audio    From a stereo recording: per-turn gaps (caller stops -> agent starts)
         and interruptions (caller starts while the agent talks -> agent quiet).
         Our recording is caller left / agent right. For a Twilio recording the
         agent channel is found automatically (the agent speaks first).

twilio   Downloads the call's Twilio dual-channel recording (made at Twilio's
         edge) and compares its gaps with our server recording. The difference
         is the Twilio <-> server round trip. The phone <-> Twilio leg is still
         outside both recordings.
"""

import argparse
import json
import statistics
import sys
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FRAME_SECS = 0.02


# ------------------------------------------------------------------ stats


def pct(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(p / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def stat_row(name: str, values: list[float]) -> str:
    if not values:
        return f"| {name} | 0 | | |"
    return (f"| {name} | {len(values)} | {statistics.median(values):.3f} | "
            f"{pct(values, 95):.3f} |")


# ---------------------------------------------------------------- latency


def load_calls(since: str | None) -> list[dict]:
    from app.config import DATA_DIR
    calls = []
    cutoff = datetime.fromisoformat(since).timestamp() if since else None
    for path in sorted((DATA_DIR / "calls").glob("*.json")):
        call = json.loads(path.read_text())
        if cutoff and (call.get("created_at") or 0) < cutoff:
            continue
        calls.append(call)
    return calls


def turn_kind(turn: dict) -> str:
    names = [t["name"] for t in turn.get("tools", []) if t["name"] != "end_call"]
    if any(k in n for n in names for k in ("faq", "knowledge", "kb", "lookup")):
        return "knowledge"
    return "tool" if names else "plain"


def latency_report(calls: list[dict]) -> str:
    turns = [dict(t, call=c["id"]) for c in calls for t in c.get("latency", [])
             if t.get("total_secs") is not None]
    out = [f"{len(turns)} measured turns across {len(calls)} calls "
           "(server side: caller stopped -> agent's first audio frame).", ""]
    for kind in ("all", "plain", "tool", "knowledge"):
        group = turns if kind == "all" else [t for t in turns if turn_kind(t) == kind]
        if not group:
            continue
        out += [f"**{kind} turns**", "", "| Stage | n | median (s) | p95 (s) |", "| --- | --- | --- | --- |",
                stat_row("Total", [t["total_secs"] for t in group]),
                stat_row("Turn detection (VAD + speech timeout)",
                         [t["user_turn_secs"] for t in group if t.get("user_turn_secs") is not None])]
        for stage, label in (("stt", "STT time to first byte"), ("llm", "LLM time to first token (all calls in the turn)"),
                             ("tts", "TTS time to first audio")):
            out.append(stat_row(label, [t["ttfb_secs"][stage] for t in group if stage in t.get("ttfb_secs", {})]))
        tool_secs = [sum(x["secs"] for x in t.get("tools", []) if x["name"] != "end_call") for t in group]
        if any(tool_secs):
            out.append(stat_row("Tool HTTP time", tool_secs))
        out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------ audio


def read_stereo(path: Path) -> tuple[np.ndarray, np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 2 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: need a 16-bit stereo WAV")
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2)
    return data[:, 0].astype(np.float32), data[:, 1].astype(np.float32), rate


def speech_segments(x: np.ndarray, rate: int, merge_gap: float, min_len: float = 0.15) -> list[tuple[float, float]]:
    """Energy-based speech segments: frames above (noise floor + 15 dB)."""
    n = int(rate * FRAME_SECS)
    frames = len(x) // n
    if frames == 0:
        return []
    rms = np.sqrt(np.mean(x[: frames * n].reshape(frames, n) ** 2, axis=1) + 1e-9)
    db = 20 * np.log10(rms / 32768)
    threshold = max(np.percentile(db, 10) + 15, -50)
    active = db > threshold
    segments, start = [], None
    for i, on in enumerate(active):
        t = i * FRAME_SECS
        if on and start is None:
            start = t
        elif not on and start is not None:
            segments.append([start, t])
            start = None
    if start is not None:
        segments.append([start, frames * FRAME_SECS])
    merged = []
    for s, e in segments:
        if merged and s - merged[-1][1] < merge_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(round(s, 2), round(e, 2)) for s, e in merged if e - s >= min_len]


def analyse(caller: list[tuple[float, float]], agent: list[tuple[float, float]]) -> dict:
    gaps, interruptions = [], []
    for cs, ce in caller:
        # Interruption: the caller starts while an agent segment is playing.
        covering = [a for a in agent if a[0] + 0.3 < cs < a[1]]
        if covering:
            interruptions.append({"at": cs, "agent_quiet_after": round(covering[0][1] - cs, 2)})
            continue
        # Reply gap: next agent start after the caller stops, with no caller
        # speech in between.
        nxt = next((a for a in agent if a[0] >= ce), None)
        if nxt and nxt[0] - ce < 10 and not any(ce < s < nxt[0] for s, _ in caller):
            gaps.append({"caller_stopped": ce, "agent_started": nxt[0], "gap": round(nxt[0] - ce, 2)})
    return {"gaps": gaps, "interruptions": interruptions}


def audio_report(path: Path, agent_channel: str = "auto") -> tuple[str, dict]:
    left, right, rate = read_stereo(path)
    # Caller turns tolerate the 0.8s mid-sentence pauses the turn timeout allows.
    left_c, right_c = speech_segments(left, rate, 1.0), speech_segments(right, rate, 1.0)
    left_a, right_a = speech_segments(left, rate, 0.4), speech_segments(right, rate, 0.4)
    if agent_channel == "auto":
        agent_is_right = bool(right_a) and (not left_a or right_a[0][0] <= left_a[0][0])
    else:
        agent_is_right = agent_channel == "right"
    caller, agent = (left_c, right_a) if agent_is_right else (right_c, left_a)
    result = analyse(caller, agent)
    g = [x["gap"] for x in result["gaps"]]
    stops = [x["agent_quiet_after"] for x in result["interruptions"]]
    lines = [f"{path.name}: agent on the {'right' if agent_is_right else 'left'} channel", "",
             "| Measure | n | median (s) | p95 (s) |", "| --- | --- | --- | --- |",
             stat_row("Caller stopped -> agent audio", g),
             stat_row("Caller started over agent -> agent quiet", stops), ""]
    for i, x in enumerate(result["interruptions"], 1):
        lines.append(f"interruption {i}: at {x['at']:.2f}s, agent quiet after {x['agent_quiet_after']:.2f}s")
    return "\n".join(lines), result


# ----------------------------------------------------------------- twilio


def fetch_twilio_recording(call_id: str) -> Path:
    import httpx

    from app.calls import store
    from app.config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
    from app.calls.recorder import RECORDINGS_DIR

    record = store.get(call_id)
    if record is None or not record.twilio_recording_sid:
        raise SystemExit(f"{call_id}: no Twilio recording (set TWILIO_DUAL_RECORDING=true before the call)")
    url = (f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}"
           f"/Recordings/{record.twilio_recording_sid}.wav")
    response = httpx.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), follow_redirects=True, timeout=30)
    response.raise_for_status()
    path = RECORDINGS_DIR / f"{call_id}-twilio.wav"
    path.write_bytes(response.content)
    return path


def twilio_report(call_id: str) -> str:
    from app.calls import store

    record = store.get(call_id)
    if record is None or not record.recording_path:
        raise SystemExit(f"{call_id}: no server recording")
    twilio_path = fetch_twilio_recording(call_id)
    server_text, server = audio_report(Path(record.recording_path), "right")
    twilio_text, edge = audio_report(twilio_path, "auto")
    s = [x["gap"] for x in server["gaps"]]
    t = [x["gap"] for x in edge["gaps"]]
    lines = [server_text, twilio_text]
    if s and t:
        rtt = statistics.median(t) - statistics.median(s)
        lines.append(f"Twilio <-> server round trip (median gap at Twilio's edge minus median gap at "
                     f"the server): {rtt:.3f}s")
    return "\n\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    lat = sub.add_parser("latency")
    lat.add_argument("--since", help="only calls created after this ISO date/time")
    aud = sub.add_parser("audio")
    aud.add_argument("wav", type=Path)
    aud.add_argument("--agent-channel", choices=["auto", "left", "right"], default="right")
    tw = sub.add_parser("twilio")
    tw.add_argument("call_id")
    args = parser.parse_args()

    if args.cmd == "latency":
        print(latency_report(load_calls(args.since)))
    elif args.cmd == "audio":
        print(audio_report(args.wav, args.agent_channel)[0])
    else:
        print(twilio_report(args.call_id))


if __name__ == "__main__":
    main()
