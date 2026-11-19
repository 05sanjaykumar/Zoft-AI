"""Map the API's voice and language names onto what the providers accept.

The create-call API speaks in product names ("female-en-in-1", "en-IN"), not
provider ids, so the voice provider can change without breaking callers.

Kokoro (v1.0) has no en-IN voice. Its Hindi voices (hf_*, hm_*) reading
English text are the closest match to Indian English; that is a judgement to
check by ear on a real call, not a measured fact. A raw Kokoro voice id
("af_heart") is accepted too.
"""

from pipecat.transcriptions.language import Language

from app.config import KOKORO_DEFAULT_VOICE

VOICE_MAP: dict[str, str] = {
    "female-en-in-1": "hf_alpha",
    "female-en-in-2": "hf_beta",
    "male-en-in-1": "hm_omega",
    "male-en-in-2": "hm_psi",
    "female-en-us-1": "af_heart",
    "male-en-us-1": "am_michael",
    "female-en-gb-1": "bf_emma",
    "male-en-gb-1": "bm_george",
}

KOKORO_VOICE_PREFIXES = ("af_", "am_", "bf_", "bm_", "hf_", "hm_")


def kokoro_voice(voice: str) -> str:
    if voice in VOICE_MAP:
        return VOICE_MAP[voice]
    if voice.startswith(KOKORO_VOICE_PREFIXES):
        return voice
    return KOKORO_DEFAULT_VOICE


def _language(code: str) -> Language:
    try:
        return Language(code)
    except ValueError:
        return Language.EN_US


def stt_language(code: str) -> Language:
    """NVIDIA's streaming ASR knows en-US / en-GB / hi-IN, not en-IN."""
    lang = _language(code)
    if lang.value.startswith("hi"):
        return Language.HI_IN
    if lang == Language.EN_GB:
        return Language.EN_GB
    return Language.EN_US


def tts_language(code: str) -> Language:
    """Kokoro phonemizes through espeak-ng; en-IN text is read as English."""
    lang = _language(code)
    if lang.value.startswith("hi"):
        return Language.HI
    if lang == Language.EN_GB:
        return Language.EN_GB
    return Language.EN_US
