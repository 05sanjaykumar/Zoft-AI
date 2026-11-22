"""Kokoro audio stays in range, one model is shared, the first chunk is short."""

import asyncio

import numpy as np
import pipecat.services.kokoro.tts as kokoro_module

from app.voice import services
from app.voice.services import ClauseTextAggregator, SharedKokoro


class LoudKokoro:
    async def create_stream(self, *args, **kwargs):
        yield np.array([0.5, 1.18, -1.3, 0.2], dtype=np.float32), 24000


def test_audio_is_capped_so_it_cannot_wrap_around():
    shared = SharedKokoro.__new__(SharedKokoro)
    shared.kokoro = LoudKokoro()

    async def collect():
        return [s async for s, _ in shared.create_stream("hi", voice="x")]

    [samples] = asyncio.run(collect())
    assert samples.max() <= 1.0 and samples.min() >= -1.0
    as_int16 = (samples * 32767).astype(np.int16)        # what Pipecat does next
    assert as_int16[1] > 0 and as_int16[2] < 0             # loud stays loud, no flip


def test_every_call_gets_the_same_model(monkeypatch):
    services.shared_kokoro.cache_clear()
    monkeypatch.setattr(services, "Kokoro", lambda m, v: object())
    assert kokoro_module.Kokoro is services.shared_kokoro
    assert kokoro_module.Kokoro("m", "v") is kokoro_module.Kokoro("m", "v")
    services.shared_kokoro.cache_clear()


def chunks(text: str, agg: ClauseTextAggregator) -> list[str]:
    async def run():
        out = []
        async for a in agg.aggregate(text):
            out.append(a.text)
        if (last := await agg.flush()) is not None:
            out.append(last.text)
        return out
    return asyncio.run(run())


def test_first_chunk_is_short_and_later_ones_are_long():
    agg = ClauseTextAggregator()
    reply = ("We provide check-ups, cleanings, fillings, root canals, extractions and "
             "teeth whitening, for adults, children and anyone else who calls us today")
    first, *rest = chunks(reply, agg)
    assert first == "We provide check-ups,"                  # cut at the first clause past 20 chars
    assert all(len(c) >= 40 for c in rest[:-1])             # later cuts wait for 60 chars
    # the next reply starts short again
    assert chunks("Sure, nine thirty works, and I have booked it for you.", agg)[0] == \
        "Sure, nine thirty works,"


# ----------------------------------------------------------------- STT choice


def test_stt_provider_switch(monkeypatch):
    from pipecat.services.deepgram.stt import DeepgramSTTService
    from pipecat.services.nvidia.stt import NvidiaSTTService
    from pipecat.transcriptions.language import Language

    from app.schemas import CallConfig
    from app.voice.voices import deepgram_language

    config = CallConfig.model_validate({"systemPrompt": "x", "greeting": "hi", "direction": "inbound",
                                        "language": "en-IN"})
    monkeypatch.setattr(services, "STT_PROVIDER", "deepgram")
    monkeypatch.setattr(services, "DEEPGRAM_API_KEY", "test-key")
    assert isinstance(services.get_stt(config), DeepgramSTTService)
    monkeypatch.setattr(services, "STT_PROVIDER", "nvidia")
    assert isinstance(services.get_stt(config), NvidiaSTTService)

    assert deepgram_language("en-IN") == Language.EN_IN       # NVIDIA's model rejects en-IN
    assert deepgram_language("hi-IN") == Language.HI
    assert deepgram_language("en-US") == Language.EN_US
