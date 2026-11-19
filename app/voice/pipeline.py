"""Assemble one call's pipeline from its config.

    caller audio -> VAD -> STT -> user turn -> LLM (+ tools) -> TTS -> caller

The transport is passed in, so the same builder serves a real Twilio call and
the fake Twilio client in tools/fake_twilio_call.py.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from app.calls.recorder import CallRecorder
from app.calls.store import CallRecord
from app.config import USER_SPEECH_TIMEOUT
from app.voice.call_controls import (
    END_CALL_RULES,
    InactivityMonitor,
    ReplyTextWatcher,
    build_end_call_tool,
    install_filler,
)
from app.voice.http_tools import build_http_tools
from app.voice.services import get_llm, get_stt, get_tts
from app.voice.turns import BackchannelAwareStartStrategy

CLINIC_TZ = ZoneInfo("Asia/Kolkata")
VAD_STOP_SECS = 0.2

VOICE_RULES = """
You are speaking on a phone call. Keep every reply to one or two short
sentences. Never use markdown, lists, emoji or symbols; write everything the
way it should be spoken, including times ("ten thirty in the morning").
If a tool returns an error, say so plainly and offer another option.
""".strip()


def system_prompt(record: CallRecord) -> str:
    now = datetime.now(CLINIC_TZ)
    today = f"Today is {now:%A}, {now:%d %B %Y}. Dates for tools are YYYY-MM-DD."
    return f"{record.config.systemPrompt}\n\n{today}\n\n{VOICE_RULES}\n\n{END_CALL_RULES}"


def build_call_task(transport, record: CallRecord,
                    client: httpx.AsyncClient) -> PipelineTask:
    config = record.config
    stt = get_stt(config)
    llm = get_llm(config)
    tts = get_tts(config)

    tools = build_http_tools(config.tools, record, client) + [build_end_call_tool(record)]
    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt(record)}],
        tools=tools,
    )

    # A phone sends only audio, no speech events, so turn boundaries come from
    # a server-side VAD plus a fixed pause. MediConnect tried Smart Turn first;
    # it ended turns on "I want to see" before the rest arrived.
    # VAD stop_secs stays at Pipecat's recommended 0.2s: it only marks "speech
    # paused". The speech-timeout strategy decides when the turn is over.
    # (MediConnect set both to 0.8s, which stacks the two waits.)
    vad = SileroVADAnalyzer(params=VADParams(stop_secs=VAD_STOP_SECS))
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                # Words, not voice activity, start a turn: a cough or an
                # "mm-hmm" never interrupts the agent (app/voice/turns.py).
                start=[BackchannelAwareStartStrategy()],
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=USER_SPEECH_TIMEOUT)],
            ),
            vad_analyzer=vad,
            user_idle_timeout=config.inactivity.afterSeconds,
        ),
    )

    watcher = ReplyTextWatcher()
    recorder = CallRecorder(record)
    pipeline = Pipeline([
        transport.input(),
        stt,
        aggregators.user(),
        llm,
        watcher,
        tts,
        transport.output(),
        recorder.audio,          # sees caller audio and agent audio as played
        aggregators.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            # Per-service TTFB and usage (tokens, characters) for the latency
            # and cost reports.
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=recorder.observers,
    )

    install_filler(llm, record, watcher)
    InactivityMonitor(task, record).install(aggregators.user())
    recorder.install(aggregators.user(), aggregators.assistant())
    return task
