"""Call controls that sit around the conversation.

* Filler: while an HTTP tool runs, the caller hears a short line instead of
  dead air (Pipecat's `on_function_calls_started` event).
* end_call: a tool the model calls when the conversation is done. Its argument
  is the closing line, so the goodbye is always spoken before the line drops.
* Inactivity: after `afterSeconds` of silence the agent says the configured
  message; if the silence runs on to `hangUpAfterSeconds`, it says goodbye and
  hangs up (Pipecat's user idle detection, re-armed between the two stages).
* Max duration: a timer that ends the call with a closing line.

Every way a call ends goes through an end frame queued after the last spoken
line, so the agent finishes its sentence before Twilio hangs up.
"""

import asyncio
import re
import time

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import (
    EndFrame,
    EndWorkerFrame,
    FunctionCallResultProperties,
    LLMFullResponseEndFrame,
    LLMMessagesAppendFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    UserIdleTimeoutUpdateFrame,
)
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.llm_service import FunctionCallParams

from app.calls.store import CallRecord

END_CALL_TOOL = "end_call"
SILENT_TOOLS = {END_CALL_TOOL, "lookup_knowledge"}   # no filler line for these
DEFAULT_GOODBYE = "Thank you for calling. Goodbye!"
IDLE_GOODBYE = "I haven't heard from you, so I'll end the call now. Please call back anytime. Goodbye!"
MAX_DURATION_GOODBYE = "We've reached the time limit for this call. Thank you for calling, goodbye!"

END_CALL_RULES = f"""
When the caller says goodbye or has everything they need, call {END_CALL_TOOL}
with your short closing line as `goodbye`. Do not say the goodbye yourself;
the tool speaks it. Never call {END_CALL_TOOL} while the caller is still asking
for something, and never in the same reply as a question or an offer: wait for
the caller's answer.
Do not announce tool calls ("let me check"): a filler line is played for you.
""".strip()


# --------------------------------------------------------------------- filler


def filler_for(tool_name: str) -> str:
    name = tool_name.lower()
    if name.startswith(("book", "reserve", "schedule", "create")):
        return "Let me book that for you."
    if name.startswith(("cancel", "delete")):
        return "Let me take care of that."
    return "Let me check that."


class ReplyTextWatcher(FrameProcessor):
    """Sits after the LLM and keeps each reply to what a person would say.

    * Notes whether the current reply has spoken text. Models often say "Sure,
      let me check" before a tool call despite being told not to; when they
      do, that line is the filler and ours is skipped, so the caller never
      hears two.
    * Stops the reply at its first question. On a real call the model once
      offered three slots, then ran on ("Which time works for you? Which time
      would you prefer? ... Goodbye.") without waiting. A question hands the
      turn to the caller, so nothing after it is spoken (or kept in the
      history, since the assistant aggregator only sees what passes here).
    * Remembers that the reply asked something, so end_call in the same reply
      can be refused (see build_end_call_tool).
    """

    def __init__(self):
        super().__init__()
        self._reset()

    tools_running = 0   # HTTP tools in flight (app/voice/http_tools.py); read by the filler

    def _reset(self):
        self.spoke = False
        self.asked = False
        self.dropped = ""

    def admit(self, text: str) -> str:
        """The part of a streamed text chunk that should be spoken."""
        if self.asked:
            self.dropped += text
            return ""
        cut = text.find("?")
        if cut == -1:
            kept = text
        else:
            kept, self.dropped = text[: cut + 1], text[cut + 1 :]
            self.asked = True
        if any(ch.isalnum() for ch in kept):
            self.spoke = True
        return kept

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._reset()
        elif isinstance(frame, LLMFullResponseEndFrame) and self.dropped.strip():
            logger.info(f"reply stopped at its question; not spoken: {self.dropped.strip()[:200]!r}")
        elif isinstance(frame, LLMTextFrame):
            kept = self.admit(frame.text)
            if not kept:
                return
            frame.text = kept   # same frame, so its other fields (e.g. spacing flags) carry over
        await self.push_frame(frame, direction)


# The filler plays only if a tool is still running after this long. On real
# calls it played for every tool, though check_slots answers in ~20 ms: the
# tool's result then waited behind the filler audio (Pipecat keeps frames in
# order), the answer came ~2s later, and callers said "hello?" into the gap,
# which interrupted the answer and restarted it.
FILLER_DELAY_SECS = 0.5


def install_filler(llm, record: CallRecord, watcher: ReplyTextWatcher) -> None:
    @llm.event_handler("on_function_calls_started")
    async def _filler(service, function_calls):
        # end_call speaks its own line; the knowledge lookup is local and takes ~1 ms.
        names = [c.function_name for c in function_calls if c.function_name not in SILENT_TOOLS]
        if not names:
            return
        if watcher.spoke:
            logger.debug(f"[{record.id}] filler skipped: the model already spoke")
            return
        asyncio.create_task(_filler_if_slow(service, record, watcher, filler_for(names[0])))


async def _filler_if_slow(service, record: CallRecord, watcher: ReplyTextWatcher, line: str) -> None:
    await asyncio.sleep(FILLER_DELAY_SECS)
    if watcher.tools_running <= 0:
        logger.debug(f"[{record.id}] filler skipped: the tool finished within {FILLER_DELAY_SECS}s")
        return
    logger.debug(f"[{record.id}] filler: {line}")
    # Kept out of the conversation history: it's a sound, not a turn.
    await service.push_frame(TTSSpeakFrame(line, append_to_context=False))


# ------------------------------------------------------------------- end_call


def build_end_call_tool(record: CallRecord, watcher: ReplyTextWatcher | None = None) -> FunctionSchema:
    async def end_call(params: FunctionCallParams):
        goodbye = str((params.arguments or {}).get("goodbye") or DEFAULT_GOODBYE).strip()
        if watcher is not None and watcher.asked:
            # The same reply just asked the caller something: hanging up now
            # would cut them off before they answer. Seen on a real call.
            result = {"ok": False, "error": "Not ended: you just asked the caller a question. "
                                            "Wait for their answer."}
            record.tools.append({"name": END_CALL_TOOL, "arguments": {"goodbye": goodbye},
                                 "result": result, "ok": False, "ms": 0, "at": time.time()})
            logger.info(f"[{record.id}] end_call refused: the reply asked a question")
            await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))
            return
        record.end_reason = record.end_reason or "agent_ended"
        record.tools.append({"name": END_CALL_TOOL, "arguments": {"goodbye": goodbye},
                             "result": {"ok": True}, "ok": True, "ms": 0, "at": time.time()})
        logger.info(f"[{record.id}] agent ending the call: {goodbye!r}")
        # Don't run the model again: the goodbye is the last thing said.
        await params.result_callback(
            {"ok": True, "ended": True},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        await params.llm.push_frame(TTSSpeakFrame(goodbye))
        # Downstream, behind the goodbye: the pipeline flushes it, then ends.
        await params.llm.push_frame(EndWorkerFrame(reason="agent_ended"))

    return FunctionSchema(
        name=END_CALL_TOOL,
        description="End the phone call after the conversation is finished or the caller says goodbye.",
        properties={"goodbye": {"type": "string",
                                "description": "A complete, warm closing sentence that fits the "
                                               "conversation, e.g. 'Thanks for calling, see you "
                                               "on Monday at ten. Goodbye!'"}},
        required=["goodbye"],
        handler=end_call,
    )


# ----------------------------------------------------------------- endings


async def end_with(task: PipelineTask, record: CallRecord, line: str, reason: str) -> None:
    """Say a last line, then end the call (Twilio hangs up after it plays)."""
    if record.end_reason is None:
        record.end_reason = reason
    logger.info(f"[{record.id}] ending call ({reason})")
    await task.queue_frames([TTSSpeakFrame(line), EndFrame(reason=reason)])


# A reply that ends by saying goodbye ("You're welcome! Goodbye.").
_GOODBYE_END = re.compile(r"\b(good[\s-]?bye|bye(\s+bye)?)\b[\s.!]*$", re.IGNORECASE)


def is_goodbye(text: str) -> bool:
    """Ends with a goodbye and asks nothing (a question still wants an answer)."""
    text = (text or "").strip()
    return "?" not in text and bool(_GOODBYE_END.search(text))


def install_goodbye_hangup(task: PipelineTask, record: CallRecord, assistant_aggregator) -> None:
    """Hang up when the agent says goodbye itself instead of calling end_call.

    On a real call the model answered "Thank you" with "You're welcome!
    Goodbye." as plain speech, and the caller had to hang up. Once such a reply
    has played to the end, the call ends. Not if the caller cut in (they are
    still talking), and not if end_call or another ending already ran.
    """
    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def _maybe_hang_up(aggregator, message):
        if record.end_reason is not None or getattr(message, "interrupted", False):
            return
        if is_goodbye(getattr(message, "content", "")):
            record.end_reason = "agent_said_goodbye"
            logger.info(f"[{record.id}] agent said goodbye without end_call: ending the call")
            await task.queue_frame(EndFrame(reason="agent_said_goodbye"))


# ---------------------------------------------------------------- resume

RESUME_NOTE = ("The caller made a sound but said no words you could hear. Carry on from "
               "where you were, in one short sentence, without starting over.")


class ReplyResumer:
    """Picks the agent's reply up again after a turn with no words in it.

    Voice interrupts the agent (app/voice/turns.py), and a cough or a door is
    voice-like enough to do it. Such a turn ends with nothing to send to the
    model, so Pipecat sends nothing, and the agent would sit silent until the
    inactivity check-in. If the agent still owed the caller a reply (it was cut
    off, or its reply was being prepared), the model is run again instead.
    """

    def __init__(self, task: PipelineTask, record: CallRecord):
        self.task = task
        self.record = record
        self.owes_reply = False

    def install(self, user_aggregator, assistant_aggregator) -> None:
        @user_aggregator.event_handler("on_user_turn_stopped")
        async def _user_stopped(aggregator, strategy, message):
            await self.on_user_turn(getattr(message, "content", "") or "")

        @assistant_aggregator.event_handler("on_assistant_turn_stopped")
        async def _agent_stopped(aggregator, message):
            self.on_agent_turn(bool(getattr(message, "interrupted", False)),
                               getattr(message, "content", "") or "")

    async def on_user_turn(self, text: str) -> None:
        if text.strip():
            self.owes_reply = True          # the model will answer this
            return
        if self.owes_reply and self.record.end_reason is None:
            logger.info(f"[{self.record.id}] caller turn had no words: resuming the reply")
            await self.task.queue_frame(LLMMessagesAppendFrame(
                messages=[{"role": "system", "content": RESUME_NOTE}], run_llm=True))

    def on_agent_turn(self, interrupted: bool, text: str = "x") -> None:
        # Only a reply that said something and played to the end settles it.
        # A reply cancelled before any words can arrive as "not interrupted"
        # with no text (seen on a fake call: the agent then stayed silent).
        self.owes_reply = interrupted or not text.strip()


# ------------------------------------------------------------------ inactivity


class InactivityMonitor:
    """Two-stage silence handling on top of Pipecat's idle event."""

    def __init__(self, task: PipelineTask, record: CallRecord):
        self.task = task
        self.record = record
        self.cfg = record.config.inactivity
        self.prompted = False

    @property
    def first_timeout(self) -> float:
        return self.cfg.afterSeconds

    @property
    def second_timeout(self) -> float:
        # Counted again from when the check-in line finishes.
        return max(self.cfg.hangUpAfterSeconds - self.cfg.afterSeconds, 1.0)

    def install(self, user_aggregator) -> None:
        @user_aggregator.event_handler("on_user_turn_idle")
        async def _idle(aggregator):
            await self.on_idle()

        @user_aggregator.event_handler("on_user_turn_started")
        async def _back(aggregator, strategy):
            await self.on_caller_spoke()

    async def on_idle(self) -> None:
        if not self.prompted:
            self.prompted = True
            logger.info(f"[{self.record.id}] caller silent {self.first_timeout}s: checking in")
            await self.task.queue_frames([
                UserIdleTimeoutUpdateFrame(timeout=self.second_timeout),
                TTSSpeakFrame(self.cfg.message),
            ])
        else:
            await end_with(self.task, self.record, IDLE_GOODBYE, "inactivity")

    async def on_caller_spoke(self) -> None:
        if self.prompted:
            self.prompted = False
            await self.task.queue_frame(UserIdleTimeoutUpdateFrame(timeout=self.first_timeout))


# ---------------------------------------------------------------- max duration


async def max_duration_timer(task: PipelineTask, record: CallRecord) -> None:
    """Started when the media stream connects; cancelled when the call ends."""
    await asyncio.sleep(record.config.maxDurationSeconds)
    await end_with(task, record, MAX_DURATION_GOODBYE, "max_duration")
