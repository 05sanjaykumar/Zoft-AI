"""Settings read from the environment (.env).

Model names and provider choices live here so swapping one is a one-line
change in .env, not a code change. Providers retire models without much
warning (Groq retired its Llama chat line during MediConnect), so nothing
below is hardcoded in the services.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------------ LLM
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
# gpt-oss models reason before answering; on a call that reasoning is silence.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low")

# ------------------------------------------------------------------------ STT
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
# "nvidia" (nemotron-asr-streaming, default) or "deepgram" (Nova-2, en-IN).
# Deepgram was tried on real calls on 2026-11-22: it scored better on replayed
# clips, but on live calls the caller found NVIDIA clearly more accurate, so
# NVIDIA stayed. Deepgram remains one setting away.
STT_PROVIDER = (os.getenv("STT_PROVIDER") or "nvidia").lower()

# ------------------------------------------------------------------------ TTS
# Kokoro runs locally (needs espeak-ng). Used when a call's voice is unmapped.
KOKORO_DEFAULT_VOICE = os.getenv("KOKORO_DEFAULT_VOICE", "af_heart")

# -------------------------------------------------------------- turn-taking
# Seconds of silence before the caller's turn is treated as finished.
USER_SPEECH_TIMEOUT = float(os.getenv("USER_SPEECH_TIMEOUT", "0.8"))

# ------------------------------------------------------------------ telephony
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
# Public host Twilio reaches us on, without the scheme (an ngrok host in dev).
PUBLIC_HOST = (os.getenv("PUBLIC_HOST") or "").replace("https://", "").replace("http://", "").rstrip("/")
# Measurement only: ask Twilio for a dual-channel recording of each call, made
# at Twilio's edge. Compared with our own recording it gives the Twilio <->
# server round trip for the latency report. Costs $0.0025/min.
TWILIO_DUAL_RECORDING = os.getenv("TWILIO_DUAL_RECORDING", "false").lower() == "true"

# ------------------------------------------------------------------ mock clinic
CLINIC_DB_PATH = Path(os.getenv("CLINIC_DB_PATH", DATA_DIR / "clinic.db"))
