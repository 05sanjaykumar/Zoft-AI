"""Everything a call leaves behind, captured while it runs.

* Recording: Pipecat's AudioBufferProcessor, stereo (caller left, agent right)
  at 8 kHz, written to data/recordings/<id>.wav when the call ends. Stereo
  matters: it lets the interruption test measure, from the file, how long the
  agent kept talking after the caller started.
* Transcript: one entry per turn from the aggregators' turn events, with wall
  clock timestamps and seconds since the call started.
* Latency: Pipecat's UserBotLatencyObserver. Per turn: caller stopped
  speaking -> agent's first audio, plus each service's time to first byte.
  Measured on the server, so it excludes the phone network both ways.
* Usage: LLM tokens, STT audio seconds and TTS characters from Pipecat's usage
  metrics, which the cost breakdown multiplies by list prices.
"""

import time
import wave
from datetime import datetime

from loguru import logger
from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import LLMUsageMetricsData, STTUsageMetricsData, TTSUsageMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

from app.calls.store import CallRecord
from app.config import DATA_DIR

RECORDINGS_DIR = DATA_DIR / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
SAMPLE_RATE = 8000


def _epoch(timestamp) -> float:
    """Pipecat turn timestamps are ISO 8601 strings."""
    if isinstance(timestamp, (int, float)):
        return float(timestamp)
    try:
        return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


class UsageObserver(BaseObserver):
    """Sums usage metrics. A frame is seen at every hop, so count each once."""

    def __init__(self, record: CallRecord):
        super().__init__()
        self.record = record
        self._seen: set[int] = set()

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if not isinstance(frame, MetricsFrame) or frame.id in self._seen:
            return
        self._seen.add(frame.id)
        usage = self.record.usage
        for item in frame.data:
            if isinstance(item, LLMUsageMetricsData):
                usage["llm_prompt_tokens"] += item.value.prompt_tokens
                usage["llm_completion_tokens"] += item.value.completion_tokens
                usage["llm_cached_tokens"] += item.value.cache_read_input_tokens or 0
                usage["llm_calls"] += 1
            elif isinstance(item, STTUsageMetricsData):
                usage["stt_audio_seconds"] += item.value.audio_seconds
            elif isinstance(item, TTSUsageMetricsData):
                usage["tts_characters"] += item.value


class CallRecorder:
    def __init__(self, record: CallRecord):
        self.record = record
        self.audio = AudioBufferProcessor(sample_rate=SAMPLE_RATE, num_channels=2,
                                          auto_start_recording=True)
        self.latency = UserBotLatencyObserver()
        self.usage = UsageObserver(record)
        self._user_started: float | None = None
        self._bot_started: float | None = None
        self._pending_latency: float | None = None

        @self.audio.event_handler("on_audio_data")
        async def _save(processor, audio, sample_rate, num_channels):
            self.save_wav(audio, sample_rate, num_channels)

        @self.latency.event_handler("on_latency_measured")
        async def _measured(observer, latency_seconds):
            self._pending_latency = latency_seconds

        @self.latency.event_handler("on_latency_breakdown")
        async def _breakdown(observer, breakdown):
            self.add_latency(self._pending_latency, breakdown)
            self._pending_latency = None

    @property
    def observers(self) -> list[BaseObserver]:
        return [self.latency, self.usage]

    # ----------------------------------------------------------- transcript

    def install(self, user_aggregator, assistant_aggregator) -> None:
        @user_aggregator.event_handler("on_user_turn_started")
        async def _user_started(aggregator, strategy):
            self._user_started = time.time()

        @user_aggregator.event_handler("on_user_turn_stopped")
        async def _user_stopped(aggregator, strategy, message):
            started = self._user_started or _epoch(message.timestamp)
            self.add_turn("user", message.content, started, time.time())

        @assistant_aggregator.event_handler("on_assistant_turn_started")
        async def _bot_started(aggregator):
            self._bot_started = time.time()

        @assistant_aggregator.event_handler("on_assistant_turn_stopped")
        async def _bot_stopped(aggregator, message):
            started = self._bot_started or _epoch(message.timestamp)
            self.add_turn("agent", message.content, started, time.time(),
                          interrupted=bool(getattr(message, "interrupted", False)))

    def add_turn(self, role: str, text: str | None, start: float, end: float,
                 interrupted: bool = False) -> None:
        text = (text or "").strip()
        if not text:
            return
        origin = self.record.started_at or start
        entry = {
            "role": role,
            "text": text,
            "start": round(start, 3),
            "end": round(end, 3),
            "t_start": round(start - origin, 2),
            "t_end": round(end - origin, 2),
        }
        if role == "agent":
            entry["interrupted"] = interrupted
        self.record.transcript.append(entry)

    # -------------------------------------------------------------- latency

    def add_latency(self, total: float | None, breakdown) -> None:
        ttfb = {}
        for item in breakdown.ttfb:
            name = item.processor.split("#")[0]
            key = ("stt" if "STT" in name else "llm" if "LLM" in name
                   else "tts" if "TTS" in name else name)
            # A tool turn runs the LLM twice; keep both.
            ttfb[key] = round(ttfb.get(key, 0) + item.duration_secs, 3)
        self.record.latency.append({
            "at": round(time.time(), 3),
            "total_secs": round(total, 3) if total is not None else None,
            "user_turn_secs": (round(breakdown.user_turn_secs, 3)
                               if breakdown.user_turn_secs is not None else None),
            "ttfb_secs": ttfb,
            "tools": [{"name": f.function_name, "secs": round(f.duration_secs, 3)}
                      for f in breakdown.function_calls],
        })

    # ------------------------------------------------------------ recording

    def save_wav(self, audio: bytes, sample_rate: int, num_channels: int) -> None:
        if not audio:
            return
        path = RECORDINGS_DIR / f"{self.record.id}.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(num_channels)
            out.setsampwidth(2)
            out.setframerate(sample_rate)
            out.writeframes(audio)
        self.record.recording_path = str(path)
        logger.info(f"[{self.record.id}] recording saved ({len(audio) / (2 * num_channels * sample_rate):.1f}s)")
