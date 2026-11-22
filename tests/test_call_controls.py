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


# ------------------------------------------------------- one question per reply


def test_reply_stops_at_its_first_question():
    # The runaway reply from a real call, streamed in chunks.
    from app.voice.call_controls import ReplyTextWatcher

    w = ReplyTextWatcher()
    chunks = ["We have openings at nine, nine thirty, or ten.", " Which time works for you?Which",
              " time would you prefer?", " Goodbye."]
    spoken = "".join(w.admit(c) for c in chunks)
    assert spoken == "We have openings at nine, nine thirty, or ten. Which time works for you?"
    assert w.asked and w.spoke


def test_end_call_refused_in_the_same_reply_as_a_question():
    from app.voice.call_controls import ReplyTextWatcher

    record, w = CallRecord(config=config()), ReplyTextWatcher()
    w.admit("Which time works for you?")
    params = FakeParams({"goodbye": "Bye!"})
    asyncio.run(build_end_call_tool(record, w).handler(params))
    assert params.llm.pushed == []                     # nothing spoken, nothing ended
    assert params.result["ok"] is False and params.properties.run_llm is False
    assert record.end_reason is None and record.tools[-1]["ok"] is False


def test_end_call_allowed_after_a_plain_reply():
    from app.voice.call_controls import ReplyTextWatcher

    record, w = CallRecord(config=config()), ReplyTextWatcher()
    w.admit("You're welcome.")
    params = FakeParams({"goodbye": "See you Tuesday, goodbye!"})
    asyncio.run(build_end_call_tool(record, w).handler(params))
    assert isinstance(params.llm.pushed[-1], EndWorkerFrame)


# ------------------------------------------------ tools held back, goodbye hang-up


def test_tools_wait_when_the_reply_asked_a_question():
    from app.voice.call_controls import ReplyTextWatcher
    from app.voice.http_tools import check_before_running

    w = ReplyTextWatcher()
    w.admit("Which of those times would you prefer?")
    result, run_llm = check_before_running({"time": "10:30", "name": "Sanjay"}, w)
    assert result["error"] == "not_run" and run_llm is False


def test_placeholder_arguments_are_not_sent():
    from app.voice.call_controls import ReplyTextWatcher
    from app.voice.http_tools import check_before_running

    w = ReplyTextWatcher()
    w.admit("Booking that now.")
    result, run_llm = check_before_running({"time": "10:30", "name": "[Caller]"}, w)
    assert "name" in result["detail"] and run_llm is True      # the model asks for the name
    assert check_before_running({"time": "10:30", "name": "Sanjay Kumar"}, w) is None


@pytest.mark.parametrize("text, ends", [
    ("You're welcome! Goodbye.", True),
    ("Thanks for calling, bye!", True),
    ("Good-bye", True),
    ("Anything else? Goodbye.", False),           # a question still wants an answer
    ("Say goodbye to tooth pain with a cleaning.", False),
    ("Your appointment is set for nine thirty.", False),
])
def test_spoken_goodbye_is_recognised(text, ends):
    from app.voice.call_controls import is_goodbye

    assert is_goodbye(text) is ends


# ------------------------------------------- voice interrupts, wordless turns resume


def test_voice_starts_a_turn_even_while_the_agent_speaks():
    from pipecat.frames.frames import VADUserStartedSpeakingFrame
    from pipecat.turns.types import ProcessFrameResult

    from app.voice.turns import VoiceStartStrategy

    s = VoiceStartStrategy()
    fired = []

    async def started(*_a, **_k):
        fired.append(True)

    s.trigger_user_turn_started = started
    s._bot_speaking = True
    result = asyncio.run(s.process_frame(VADUserStartedSpeakingFrame()))
    assert fired and result == ProcessFrameResult.STOP


def test_a_wordless_turn_resumes_an_unfinished_reply():
    from pipecat.frames.frames import LLMMessagesAppendFrame

    from app.voice.call_controls import ReplyResumer

    task, record = FakeTask(), CallRecord(config=config())
    r = ReplyResumer(task, record)
    asyncio.run(r.on_user_turn("Do you have Tuesday?"))   # the model owes an answer
    r.on_agent_turn(interrupted=True)                       # a cough cut it off
    asyncio.run(r.on_user_turn(""))                         # ... and said no words
    [frame] = task.queued
    assert isinstance(frame, LLMMessagesAppendFrame) and frame.run_llm


def test_a_wordless_turn_after_a_finished_reply_does_nothing():
    from app.voice.call_controls import ReplyResumer

    task, record = FakeTask(), CallRecord(config=config())
    r = ReplyResumer(task, record)
    asyncio.run(r.on_user_turn("Do you have Tuesday?"))
    r.on_agent_turn(interrupted=False)                      # answered in full
    asyncio.run(r.on_user_turn(""))                         # a noise while it waits
    assert task.queued == []


@pytest.mark.parametrize("name", ["John Doe", "jane doe", "Caller", "unknown", "[Caller]"])
def test_invented_names_are_not_booked(name):
    from app.voice.http_tools import placeholder_arguments

    assert placeholder_arguments({"name": name, "time": "10:30"}) == ["name"]


def test_real_names_are_booked():
    from app.voice.http_tools import placeholder_arguments

    assert placeholder_arguments({"name": "Sanjay Kumar", "time": "10:30"}) == []


# ------------------------------------------- filler only for slow tools, heard names


class FakeService:
    def __init__(self):
        self.pushed = []

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)


@pytest.mark.parametrize("tool_secs, filler", [(0.02, False), (1.0, True)])
def test_filler_plays_only_when_the_tool_is_slow(monkeypatch, tool_secs, filler):
    from app.voice import call_controls
    from app.voice.call_controls import ReplyTextWatcher, _filler_if_slow

    monkeypatch.setattr(call_controls, "FILLER_DELAY_SECS", 0.1)
    w, service, record = ReplyTextWatcher(), FakeService(), CallRecord(config=config())

    async def run():
        w.tools_running += 1
        timer = asyncio.create_task(_filler_if_slow(service, record, w, "Let me check that."))
        await asyncio.sleep(tool_secs * 0.2)    # the tool (scaled down)
        if tool_secs < 0.5:
            w.tools_running -= 1                # finished before the timer
        await timer

    asyncio.run(run())
    assert bool(service.pushed) is filler


def test_names_must_be_said_or_spelled_by_the_caller():
    from app.voice.http_tools import unheard_names

    record = CallRecord(config=config())
    record.transcript = [{"role": "user", "text": "Andy."}, {"role": "agent", "text": "Could you spell that?"},
                         {"role": "user", "text": "S a n j a y."}, {"role": "user", "text": "Yes."}]
    assert unheard_names({"name": "Sanjay", "time": "09:00"}, record) == []       # spelled
    assert unheard_names({"name": "Andy"}, record) == []                          # said
    assert unheard_names({"name": "Arun Kumar"}, record) == ["name"]              # invented
    assert unheard_names({"name": "Arun Kumar"}, CallRecord(config=config())) == []   # no transcript yet


def test_a_reply_cancelled_before_any_words_still_resumes():
    from app.voice.call_controls import ReplyResumer

    task, record = FakeTask(), CallRecord(config=config())
    r = ReplyResumer(task, record)
    asyncio.run(r.on_user_turn("Do you have anything on Tuesday?"))
    r.on_agent_turn(interrupted=False, text="")          # cancelled, nothing said
    asyncio.run(r.on_user_turn(""))                       # the sound that cancelled it
    assert len(task.queued) == 1
