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
import time

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import (
    EndFrame,
    EndWorkerFrame,
    FunctionCallResultProperties,
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
DEFAULT_GOODBYE = "Thank you for calling. Goodbye!"
IDLE_GOODBYE = "I haven't heard from you, so I'll end the call now. Please call back anytime. Goodbye!"
MAX_DURATION_GOODBYE = "We've reached the time limit for this call. Thank you for calling, goodbye!"

END_CALL_RULES = f"""
When the caller says goodbye or has everything they need, call {END_CALL_TOOL}
with your short closing line as `goodbye`. Do not say the goodbye yourself;
the tool speaks it. Never call {END_CALL_TOOL} while the caller is still asking
for something.
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
    """Sits after the LLM and notes whether the current reply has spoken text.

    Models often say "Sure, let me check" before a tool call despite being
    told not to. When they do, that line is the filler and ours is skipped,
    so the caller never hears two.
    """

    def __init__(self):
        super().__init__()
        self.spoke = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self.spoke = False
        elif isinstance(frame, LLMTextFrame) and any(ch.isalnum() for ch in frame.text):
            self.spoke = True
        await self.push_frame(frame, direction)


def install_filler(llm, record: CallRecord, watcher: ReplyTextWatcher) -> None:
    @llm.event_handler("on_function_calls_started")
    async def _filler(service, function_calls):
        names = [c.function_name for c in function_calls if c.function_name != END_CALL_TOOL]
        if not names:
            return
        if watcher.spoke:
            logger.debug(f"[{record.id}] filler skipped: the model already spoke")
            return
        line = filler_for(names[0])
        logger.debug(f"[{record.id}] filler: {line}")
        # Kept out of the conversation history: it's a sound, not a turn.
        await service.push_frame(TTSSpeakFrame(line, append_to_context=False))


# ------------------------------------------------------------------- end_call


def build_end_call_tool(record: CallRecord) -> FunctionSchema:
    async def end_call(params: FunctionCallParams):
        goodbye = str((params.arguments or {}).get("goodbye") or DEFAULT_GOODBYE).strip()
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
