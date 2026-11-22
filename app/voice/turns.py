"""When the caller's speech starts a turn (and so interrupts the agent).

Voice starts a turn: Silero VAD reports speech, the agent stops. Measured on a
real call, the hosted NVIDIA recogniser needs 1.0-1.3s to return a first word
and turned one short "hello" into "N", so a words-only rule (what this file
did first) left the agent talking for 1.2-1.7s, or never stopped it. VAD,
tuned for 8 kHz phone audio, caught every utterance on that call ~0.25s in.

The cost: an "mm-hmm" now stops the agent too, since it is voice. A short
cough mostly doesn't (Silero is trained to tell speech from noise). If a turn
ends with no words at all, the agent picks its reply up again
(call_controls.ReplyResumer), so a cough costs a pause, not the reply.

Words are the backup for speech VAD misses: while the agent is speaking,
backchannels ("mm-hmm", "yeah", "okay") are ignored, one real word ("wait",
"no", "Tuesday") interrupts, and so do three words of anything. While the
agent is silent, any word starts the caller's turn.
"""

import re

from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame, VADUserStartedSpeakingFrame
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)

# "yes" and "no" are left out on purpose: said over the end of a question
# ("…shall I book it?" "Yes") they are answers, not backchannels.
BACKCHANNELS = {
    "mm", "mmm", "mhm", "mm-hmm", "mmhmm", "hmm", "hm", "uh-huh", "uhhuh", "uh", "huh", "um",
    "ah", "oh", "okay", "ok", "yeah", "yep", "right", "alright",
    "achha", "accha", "haan", "han", "theek", "hai",
}
ANY_WORDS_TO_INTERRUPT = 3


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[\w'-]+", text.lower()) if w.strip("-'")]


def is_interruption(text: str) -> bool:
    """Would this text, heard while the agent speaks, interrupt it?"""
    words = _words(text)
    if len(words) >= ANY_WORDS_TO_INTERRUPT:
        return True
    return any(w not in BACKCHANNELS for w in words)


class VoiceStartStrategy(MinWordsUserTurnStartStrategy):
    def __init__(self, **kwargs):
        super().__init__(min_words=1, use_interim=True, **kwargs)

    async def process_frame(self, frame) -> ProcessFrameResult:
        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        return await super().process_frame(frame)

    async def _handle_transcription(
        self, frame: TranscriptionFrame | InterimTranscriptionFrame
    ) -> ProcessFrameResult:
        trigger = is_interruption(frame.text) if self._bot_speaking else bool(_words(frame.text))
        if trigger:
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        await self.trigger_reset_aggregation()
        return ProcessFrameResult.CONTINUE
