"""STT, LLM and TTS services, built per call from its config.

Ported from MediConnect, where each choice was measured:

* STT: NVIDIA nemotron-asr-streaming (STT_PROVIDER=nvidia, the default).
  Deepgram (STT_PROVIDER=deepgram, Nova-2 en-IN) was tried on real calls on
  2026-11-22. Replayed clips favoured it (words wrong: Nova-2 20%, NVIDIA 28%,
  Nova-3 36%; 10 utterances), but on live calls the caller found NVIDIA
  clearly more accurate, so NVIDIA stayed. A 10-utterance replay is too small
  to overrule that. Short names fail on all of them over 8 kHz audio, so
  names are spelled back.
* LLM: Groq openai/gpt-oss-120b, fast tool calling (~0.13s to first token
  measured in MediConnect), reasoning effort kept low.
* TTS: Kokoro, local ONNX, no per-character cost. Chunked at clauses: that
  took time-to-first-audio from 1.67s to 0.46s in MediConnect.

Two Kokoro problems found on real calls here, fixed below:

* Clicks. Pipecat converts Kokoro's float audio with `samples * 32767` and no
  limit. The hf_alpha voice peaks above 1.0 (1.18 measured), the value wraps
  from loudest-positive to loudest-negative, and the caller hears a click: 35
  on one two-minute call. The shared model below caps samples first.
* A cold model on every call. Pipecat builds a new Kokoro (about 1.1s) in each
  call's service, and its first synthesis is slow (4.2s measured), so every
  greeting waited. Now one model is loaded and warmed up at server start and
  shared by all calls.
"""

import time
from functools import lru_cache

import numpy as np
import pipecat.services.kokoro.tts as kokoro_module
from kokoro_onnx import Kokoro
from loguru import logger

from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.nvidia.stt import NvidiaSTTService
from pipecat.utils.text.base_text_aggregator import Aggregation, AggregationType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

from app.config import (
    DEEPGRAM_API_KEY,
    GROQ_API_KEY,
    LLM_MODEL,
    LLM_REASONING_EFFORT,
    NVIDIA_API_KEY,
    STT_PROVIDER,
)
from app.schemas import CallConfig
from app.voice.voices import deepgram_language, kokoro_voice, stt_language, tts_language


def get_stt(config: CallConfig):
    if STT_PROVIDER == "deepgram":
        return DeepgramSTTService(
            api_key=DEEPGRAM_API_KEY,
            settings=DeepgramSTTService.Settings(
                # Nova-2, not Nova-3: scored live on a real call's 10 caller
                # utterances (2 runs), Nova-2 en-IN got 20% of words wrong,
                # NVIDIA 28%, Nova-3 en-IN 36% ("nine o'clock" -> "Handbook").
                model="nova-2",
                language=deepgram_language(config.language),
                interim_results=True,
                smart_format=True,       # "ten thirty" -> "10:30", which the tools want
                profanity_filter=False,
            ),
        )
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

# Kokoro audio is scaled by this before the cap, so the cap rarely has to act.
TTS_HEADROOM = 0.9


class SharedKokoro:
    """One Kokoro model for every call, with its audio kept inside -1.0..1.0."""

    def __init__(self, model_path: str, voices_path: str):
        started = time.perf_counter()
        self.kokoro = Kokoro(model_path, voices_path)
        logger.info(f"Kokoro model loaded in {time.perf_counter() - started:.2f}s (shared by all calls)")

    async def create_stream(self, *args, **kwargs):
        async for samples, sample_rate in self.kokoro.create_stream(*args, **kwargs):
            yield np.clip(samples * TTS_HEADROOM, -1.0, 1.0), sample_rate


@lru_cache(maxsize=4)
def shared_kokoro(model_path: str, voices_path: str) -> SharedKokoro:
    return SharedKokoro(model_path, voices_path)


# KokoroTTSService.__init__ calls `Kokoro(model, voices)` from its own module;
# pointing that name at the cache gives every call the same warm, capped model.
kokoro_module.Kokoro = shared_kokoro


async def warm_up_tts(voice: str, language) -> None:
    """Load Kokoro and run one synthesis, so the first caller doesn't wait."""
    model = kokoro_module.KOKORO_CACHE_DIR / "kokoro-v1.0.onnx"
    voices = kokoro_module.KOKORO_CACHE_DIR / "voices-v1.0.bin"
    kokoro_module._ensure_model_files(model, voices)
    started = time.perf_counter()
    lang = kokoro_module.language_to_kokoro_language(language)
    async for _ in shared_kokoro(str(model), str(voices)).create_stream("Hello.", voice=voice, lang=lang):
        pass
    logger.info(f"Kokoro warmed up in {time.perf_counter() - started:.2f}s")


# A reply's first chunk is cut early so the caller hears the agent sooner;
# later chunks are synthesised while earlier ones play, so they can be longer
# (fewer joins in the voice).
FIRST_CHUNK_MIN_CHARS = 20
CLAUSE_MIN_CHARS = 60
CLAUSE_BOUNDARIES = (",", ";", ":", "—", "–")


def _speakable(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


class ClauseTextAggregator(SimpleTextAggregator):
    """Sentence aggregation, plus clause cuts for long sentences.

    Kokoro emits nothing until a whole chunk is synthesised and synthesis time
    scales with chunk length, so one long sentence is one long silence.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._first_chunk = True   # the next chunk starts a reply

    async def _check_sentence_with_lookahead(self, char: str) -> Aggregation | None:
        found = await super()._check_sentence_with_lookahead(char)
        if found is not None:
            if not _speakable(found.text):
                return None
            self._first_chunk = False
            return found
        min_chars = FIRST_CHUNK_MIN_CHARS if self._first_chunk else CLAUSE_MIN_CHARS
        if (
            char.isspace()
            and len(self._text) >= min_chars
            and self._text.rstrip().endswith(CLAUSE_BOUNDARIES)
        ):
            result, self._text = self._text.rstrip(), ""
            self._needs_lookahead = False
            self._first_chunk = False
            return Aggregation(text=result, type=AggregationType.SENTENCE)
        return None

    async def flush(self) -> Aggregation | None:
        self._first_chunk = True   # the reply is over
        remaining = await super().flush()
        if remaining is not None and not _speakable(remaining.text):
            return None
        return remaining

    async def reset(self):
        self._first_chunk = True   # interrupted: the next text is a new reply
        await super().reset()


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
