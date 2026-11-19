"""Cost of one call, per component, from measured usage x list prices.

Prices live in pricing.json (with the page each came from) so they can be
updated without touching code. Usage comes from the call itself:

    telephony  billed minutes (Twilio rounds up per started minute) x rate
    stt        audio seconds streamed to the recogniser x per-minute rate
    llm        prompt / cached / completion tokens x per-token rates
    tts        characters spoken x per-character rate (0 for self-hosted Kokoro)
    server     call duration x VM price / concurrent calls per VM
"""

import json
import math
from functools import lru_cache
from typing import Any

from app.calls.store import CallRecord
from app.config import ROOT

PRICING_PATH = ROOT / "pricing.json"


@lru_cache
def load_pricing() -> dict[str, Any]:
    return json.loads(PRICING_PATH.read_text())


def call_cost(record: CallRecord, pricing: dict[str, Any] | None = None) -> dict[str, Any]:
    p = pricing or load_pricing()
    usd_inr = p["usd_inr"]["rate"]
    seconds = record.duration_seconds or 0.0
    minutes = seconds / 60
    usage = record.usage

    tel = p["telephony"]
    billed_minutes = math.ceil(minutes) if seconds > 0 else 0
    tel_rate = tel["outbound_per_min_usd"] if record.direction == "outbound" else tel["inbound_per_min_usd"]
    stt_seconds = usage.get("stt_audio_seconds") or seconds   # streaming STT bills the whole stream
    llm = p["llm"]
    server = p["server"]

    components = {
        "telephony": (billed_minutes * tel_rate,
                      f"{billed_minutes} billed min x ${tel_rate}/min ({record.direction})"),
        "stt": (stt_seconds / 60 * p["stt"]["per_min_usd"],
                f"{stt_seconds:.0f}s audio x ${p['stt']['per_min_usd']}/min"),
        "llm": (usage["llm_prompt_tokens"] / 1e6 * llm["input_per_mtok_usd"]
                + usage["llm_cached_tokens"] / 1e6 * llm.get("cached_input_per_mtok_usd", llm["input_per_mtok_usd"])
                + usage["llm_completion_tokens"] / 1e6 * llm["output_per_mtok_usd"],
                f"{usage['llm_prompt_tokens']} in + {usage['llm_cached_tokens']} cached + "
                f"{usage['llm_completion_tokens']} out tokens ({usage['llm_calls']} calls)"),
        "tts": (usage["tts_characters"] * p["tts"]["per_char_usd"],
                f"{usage['tts_characters']} characters x ${p['tts']['per_char_usd']}/char"),
        "server": (seconds / 3600 * server["per_hour_usd"] / server["concurrent_calls"],
                   f"{seconds:.0f}s x ${server['per_hour_usd']}/h / {server['concurrent_calls']} calls per VM"),
    }

    total_usd = sum(usd for usd, _ in components.values())
    return {
        "currency": "INR",
        "usd_inr": usd_inr,
        "pricing_as_of": p.get("as_of"),
        "components": {
            name: {"usd": round(usd, 6), "inr": round(usd * usd_inr, 4), "basis": basis}
            for name, (usd, basis) in components.items()
        },
        "total_usd": round(total_usd, 6),
        "total_inr": round(total_usd * usd_inr, 4),
        "per_minute_inr": round(total_usd * usd_inr / minutes, 4) if minutes else None,
    }
