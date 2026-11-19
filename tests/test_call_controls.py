"""Interruption filter, filler lines, end_call and the inactivity stages."""

import asyncio

import pytest
from pipecat.frames.frames import EndFrame, EndWorkerFrame, TTSSpeakFrame, UserIdleTimeoutUpdateFrame
from pydantic import ValidationError

from app.calls.store import CallRecord
from app.schemas import CallConfig
from app.voice.call_controls import (
    IDLE_GOODBYE,
    InactivityMonitor,
    build_end_call_tool,
    filler_for,
)
from app.voice.turns import is_interruption


def config(**overrides) -> CallConfig:
    data = {"systemPrompt": "x", "greeting": "hi", "direction": "inbound",
            "inactivity": {"afterSeconds": 8, "message": "Are you still there?", "hangUpAfterSeconds": 20}}
    data.update(overrides)
    return CallConfig.model_validate(data)


# --------------------------------------------------------------- interruptions


@pytest.mark.parametrize("text", ["mm-hmm", "Mm hmm.", "yeah", "okay okay", "uh huh", "haan", ""])
def test_backchannels_do_not_interrupt(text):
    assert not is_interruption(text)


@pytest.mark.parametrize("text", ["wait", "No", "yes", "Tuesday", "actually can you", "yeah yeah yeah"])
def test_real_words_interrupt(text):
    assert is_interruption(text)


# --------------------------------------------------------------------- filler


def test_filler_matches_the_tool():
    assert filler_for("check_slots") == "Let me check that."
    assert filler_for("book_slot") == "Let me book that for you."
    assert filler_for("lookup_faq") == "Let me check that."


# ------------------------------------------------------------------- end_call


class FakeLLM:
    def __init__(self):
        self.pushed = []

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)


class FakeParams:
    def __init__(self, arguments):
        self.arguments = arguments
        self.llm = FakeLLM()
        self.result = None
        self.properties = None

    async def result_callback(self, result, properties=None):
        self.result, self.properties = result, properties


def test_end_call_speaks_goodbye_then_ends():
    record = CallRecord(config=config())
    tool = build_end_call_tool(record)
    params = FakeParams({"goodbye": "See you on Monday, goodbye!"})
    asyncio.run(tool.handler(params))

    speak, end = params.llm.pushed
    assert isinstance(speak, TTSSpeakFrame) and speak.text == "See you on Monday, goodbye!"
    assert isinstance(end, EndWorkerFrame)
    assert params.properties.run_llm is False
    assert record.end_reason == "agent_ended"
    assert record.tools[-1]["name"] == "end_call"


def test_end_call_is_reserved():
    with pytest.raises(ValidationError):
        config(tools=[{"name": "end_call", "description": "x",
                       "http": {"url": "https://example.test"}}])


# ----------------------------------------------------------------- inactivity


class FakeTask:
    def __init__(self):
        self.queued = []

    async def queue_frames(self, frames):
        self.queued.extend(frames)

    async def queue_frame(self, frame):
        self.queued.append(frame)


def test_inactivity_checks_in_then_hangs_up():
    task, record = FakeTask(), CallRecord(config=config())
    monitor = InactivityMonitor(task, record)

    asyncio.run(monitor.on_idle())
    update, prompt = task.queued
    assert isinstance(update, UserIdleTimeoutUpdateFrame) and update.timeout == 12
    assert prompt.text == "Are you still there?"

    asyncio.run(monitor.on_idle())
    goodbye, end = task.queued[2:]
    assert goodbye.text == IDLE_GOODBYE and isinstance(end, EndFrame)
    assert record.end_reason == "inactivity"


def test_caller_speaking_resets_inactivity():
    task, record = FakeTask(), CallRecord(config=config())
    monitor = InactivityMonitor(task, record)
    asyncio.run(monitor.on_idle())
    asyncio.run(monitor.on_caller_spoke())
    assert task.queued[-1].timeout == 8

    asyncio.run(monitor.on_idle())   # a new silence starts with the check-in again
    assert task.queued[-1].text == "Are you still there?"


def test_hang_up_must_come_after_check_in():
    with pytest.raises(ValidationError):
        config(inactivity={"afterSeconds": 10, "message": "Hello?", "hangUpAfterSeconds": 5})
