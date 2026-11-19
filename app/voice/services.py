"""STT, LLM and TTS services, built per call from its config.

Ported from MediConnect, where each choice was measured:

* STT: NVIDIA nemotron-asr-streaming streams partial transcripts while the
  caller is still talking (Groq Whisper is batch and waits for the utterance).
* LLM: Groq openai/gpt-oss-120b, fast tool calling (~0.13s to first token
  measured in MediConnect), reasoning effort kept low.
* TTS: Kokoro, local ONNX, no per-character cost. Chunked at clauses: that
  took time-to-first-audio from 1.67s to 0.46s in MediConnect.
"""

from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.nvidia.stt import NvidiaSTTService
from pipecat.utils.text.base_text_aggregator import Aggregation, AggregationType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

from app.config import GROQ_API_KEY, LLM_MODEL, LLM_REASONING_EFFORT, NVIDIA_API_KEY
from app.schemas import CallConfig
from app.voice.voices import kokoro_voice, stt_language, tts_language


def get_stt(config: CallConfig) -> NvidiaSTTService:
    return NvidiaSTTService(
        api_key=NVIDIA_API_KEY,
        settings=NvidiaSTTService.Settings(language=stt_language(config.language)),
    )


def get_llm(config: CallConfig) -> GroqLLMService:
    return GroqLLMService(
        api_key=GROQ_API_KEY,
        settings=GroqLLMService.Settings(
            model=LLM_MODEL,
            temperature=0.3,
            top_p=0.9,
            max_completion_tokens=1024,
            frequency_penalty=0.3,
            extra={"reasoning_effort": LLM_REASONING_EFFORT},
        ),
    )


# ----------------------------------------------------------------------- TTS

# Cut at a clause boundary once the pending text is at least this long.
CLAUSE_MIN_CHARS = 60
CLAUSE_BOUNDARIES = (",", ";", ":", "—", "–")


def _speakable(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


class ClauseTextAggregator(SimpleTextAggregator):
    """Sentence aggregation, plus clause cuts for long sentences.

    Kokoro emits nothing until a whole chunk is synthesised and synthesis time
    scales with chunk length, so one long sentence is one long silence.
    """

    async def _check_sentence_with_lookahead(self, char: str) -> Aggregation | None:
        found = await super()._check_sentence_with_lookahead(char)
        if found is not None:
            return found if _speakable(found.text) else None
        if (
            char.isspace()
            and len(self._text) >= CLAUSE_MIN_CHARS
            and self._text.rstrip().endswith(CLAUSE_BOUNDARIES)
        ):
            result, self._text = self._text.rstrip(), ""
            self._needs_lookahead = False
            return Aggregation(text=result, type=AggregationType.SENTENCE)
        return None

    async def flush(self) -> Aggregation | None:
        remaining = await super().flush()
        if remaining is not None and not _speakable(remaining.text):
            return None
        return remaining


def get_tts(config: CallConfig) -> KokoroTTSService:
    tts = KokoroTTSService(
        settings=KokoroTTSService.Settings(
            voice=kokoro_voice(config.voice),
            language=tts_language(config.language),
        )
    )
    # TTSService has no constructor hook for this; it reads the attribute on
    # every text frame.
    tts._text_aggregator = ClauseTextAggregator(aggregation_type=tts._text_aggregation_mode)
    return tts
