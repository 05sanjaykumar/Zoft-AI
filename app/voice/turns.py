"""When the caller's speech counts as an interruption.

Pipecat's default starts a user turn (and so interrupts the agent) on voice
activity alone, which means a cough or an "mm-hmm" stops the agent mid-word.
This strategy waits for words from the transcript instead:

* While the agent is speaking, backchannels ("mm-hmm", "yeah", "okay") are
  ignored. One real word ("wait", "no", "Tuesday") interrupts at once, and so
  do three words of anything.
* While the agent is silent, any word starts the caller's turn, so a plain
  "yeah" still answers a question.

A cough produces no words, so it never interrupts. Interim transcripts are
used, so the agent stops as soon as the first real word is recognised.
"""

import re

from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
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


class BackchannelAwareStartStrategy(MinWordsUserTurnStartStrategy):
    def __init__(self, **kwargs):
        super().__init__(min_words=1, use_interim=True, **kwargs)

    async def _handle_transcription(
        self, frame: TranscriptionFrame | InterimTranscriptionFrame
    ) -> ProcessFrameResult:
        trigger = is_interruption(frame.text) if self._bot_speaking else bool(_words(frame.text))
        if trigger:
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        await self.trigger_reset_aggregation()
        return ProcessFrameResult.CONTINUE
