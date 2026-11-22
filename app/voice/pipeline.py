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
    install_goodbye_hangup,
    ReplyResumer,
)
from app.voice.knowledge import KNOWLEDGE_RULES, build_knowledge_tool
from app.voice.http_tools import build_http_tools
from app.voice.services import get_llm, get_stt, get_tts
from app.voice.spelling import install_spelling
from app.voice.turns import VoiceStartStrategy

CLINIC_TZ = ZoneInfo("Asia/Kolkata")
VAD_STOP_SECS = 0.2
VAD_CONFIDENCE = 0.5
VAD_MIN_VOLUME = 0.2
WORDLESS_TURN_TIMEOUT = 2.0

VOICE_RULES = """
You are speaking on a phone call. Keep every reply to one or two short
sentences, with at most one question, and end the reply after it. Never use
markdown, lists, emoji or symbols; write everything the way it should be
spoken, including times ("ten thirty in the morning") and money ("twelve
hundred rupees", never the rupee sign).
If a tool returns an error, say so plainly and offer another option.
Never make up details the caller hasn't given you, such as a name. If you
didn't catch something, ask again. Names are often misheard on the phone:
when you take a caller's name, ask them to spell it, then read the spelling
back and wait for a yes before using it. Letters recovered from spelling are
marked like "(spelled: S A N J A Y)"; use those letters. A name that comes
through as an ordinary word ("Sunday", "Andy") is almost certainly misheard.
""".strip()


def system_prompt(record: CallRecord) -> str:
    now = datetime.now(CLINIC_TZ)
    today = f"Today is {now:%A}, {now:%d %B %Y}. Dates for tools are YYYY-MM-DD."
    parts = [record.config.systemPrompt, today, VOICE_RULES, END_CALL_RULES]
    if record.config.knowledgeBase:
        parts.append(KNOWLEDGE_RULES)
    return "\n\n".join(parts)


def build_call_task(transport, record: CallRecord,
                    client: httpx.AsyncClient) -> PipelineTask:
    config = record.config
    stt = get_stt(config)
    llm = get_llm(config)
    tts = get_tts(config)

    watcher = ReplyTextWatcher()
    tools = build_http_tools(config.tools, record, client, watcher) + [build_end_call_tool(record, watcher)]
    if config.knowledgeBase:
        tools.append(build_knowledge_tool(record, config.knowledgeBase))
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
    # confidence 0.7 -> 0.5 and min_volume 0.6 -> 0.2 (Pipecat's defaults are
    # tuned for 16 kHz mics): on a real call's 8 kHz audio the defaults missed
    # 2 of 9 caller utterances, these caught all 9 (app/voice/turns.py).
    vad = SileroVADAnalyzer(params=VADParams(confidence=VAD_CONFIDENCE, min_volume=VAD_MIN_VOLUME,
                                             stop_secs=VAD_STOP_SECS))
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                # Voice starts a turn (and interrupts); words are the backup
                # for speech the VAD misses (app/voice/turns.py).
                start=[VoiceStartStrategy()],
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=USER_SPEECH_TIMEOUT)],
            ),
            vad_analyzer=vad,
            user_idle_timeout=config.inactivity.afterSeconds,
            # A turn with no words (a cough) closes after this much quiet, so
            # the agent resumes in ~2s rather than Pipecat's default 5s. Speech
            # and transcripts restart the clock, so it never cuts a sentence.
            user_turn_stop_timeout=WORDLESS_TURN_TIMEOUT,
        ),
    )

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
    install_spelling(aggregators.user())   # "Yes, A and J" -> "... (spelled: S A N J)"
    install_goodbye_hangup(task, record, aggregators.assistant())
    ReplyResumer(task, record).install(aggregators.user(), aggregators.assistant())
    recorder.install(aggregators.user(), aggregators.assistant())
    return task
