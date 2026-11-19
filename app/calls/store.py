"""Call records: what GET /calls/:id returns, kept in memory and on disk.

A live call lives in memory (it also holds the running pipeline task); when it
ends, the record is written to data/calls/<id>.json so it survives a restart.
Fields that later milestones fill in (transcript, recording, cost) exist from
the start so the API shape doesn't change under the reviewers.
"""

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from app.config import DATA_DIR
from app.schemas import CallConfig

CALLS_DIR = DATA_DIR / "calls"
CALLS_DIR.mkdir(parents=True, exist_ok=True)
INBOUND_CONFIG_PATH = DATA_DIR / "inbound_config.json"

# queued -> ringing -> in-progress -> completed | failed | no-answer | busy | canceled
FINAL_STATUSES = {"completed", "failed", "no-answer", "busy", "canceled"}


@dataclass
class CallRecord:
    config: CallConfig
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    direction: str = "outbound"
    status: str = "queued"
    twilio_sid: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None   # media stream connected
    ended_at: float | None = None
    end_reason: str | None = None
    # One entry per turn: {"role", "text", "start", "end", "t_start", "t_end"};
    # start/end are epoch seconds, t_* are seconds since the call connected.
    transcript: list[dict[str, Any]] = field(default_factory=list)
    # One entry per tool run: {"name", "arguments", "result", "ok", "ms", "at"}.
    tools: list[dict[str, Any]] = field(default_factory=list)
    # One entry per agent reply: total caller-stopped -> agent-audio, plus TTFBs.
    latency: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, float] = field(default_factory=lambda: {
        "llm_prompt_tokens": 0, "llm_completion_tokens": 0, "llm_cached_tokens": 0,
        "llm_calls": 0, "stt_audio_seconds": 0.0, "tts_characters": 0,
    })
    recording_path: str | None = None
    recording_url: str | None = None
    cost: dict[str, Any] | None = None
    webhook: dict[str, Any] | None = None   # result of POSTing the summary

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.ended_at or time.time()
        return round(end - self.started_at, 2)

    def to_public(self) -> dict[str, Any]:
        data = asdict(self)
        data["config"] = self.config.model_dump()
        data["duration_seconds"] = self.duration_seconds
        return data


_LIVE: dict[str, CallRecord] = {}
# Pipeline tasks of calls in progress, keyed by call id (kept apart from the
# record so the record stays plain data).
TASKS: dict[str, Any] = {}


def create(config: CallConfig, **kwargs) -> CallRecord:
    record = CallRecord(config=config, direction=config.direction, **kwargs)
    _LIVE[record.id] = record
    return record


def get(call_id: str) -> CallRecord | None:
    if call_id in _LIVE:
        return _LIVE[call_id]
    path = CALLS_DIR / f"{call_id}.json"
    if path.exists():
        data = json.loads(path.read_text())
        data.pop("duration_seconds", None)
        data["config"] = CallConfig.model_validate(data["config"])
        record = CallRecord(**data)
        _LIVE[call_id] = record
        return record
    return None


def find_by_twilio_sid(sid: str) -> CallRecord | None:
    return next((r for r in _LIVE.values() if r.twilio_sid == sid), None)


def all_records() -> list[CallRecord]:
    return sorted(_LIVE.values(), key=lambda r: r.created_at, reverse=True)


def save(record: CallRecord) -> None:
    path = CALLS_DIR / f"{record.id}.json"
    path.write_text(json.dumps(record.to_public(), indent=2, default=str))


# -------------------------------------------------------- inbound behaviour


def set_inbound_config(config: CallConfig) -> None:
    INBOUND_CONFIG_PATH.write_text(config.model_dump_json(indent=2))


def get_inbound_config() -> CallConfig | None:
    if INBOUND_CONFIG_PATH.exists():
        return CallConfig.model_validate_json(INBOUND_CONFIG_PATH.read_text())
    return None
