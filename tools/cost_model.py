#!/usr/bin/env python
"""Cost per minute for 1,000 calls a day at 3 minutes each, in rupees.

    python tools/cost_model.py              # usage measured from data/calls/*.json
    python tools/cost_model.py --defaults   # ignore call records, use the defaults below

Two numbers, as the brief asks:

raw        list prices x usage, nothing else.
realistic  raw plus the costs a real deployment pays that a per-minute list
           price hides (each listed below with its assumption).

Prices come from pricing.json (each with its source page). Usage per minute
(LLM tokens, agent characters) comes from the measured calls when there are
any, otherwise from DEFAULTS. Every assumption is printed with the result.
"""

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CALLS_PER_DAY = 1000
MINUTES_PER_CALL = 3.0

DEFAULTS = {
    "llm_prompt_tokens_per_min": 6000,      # whole context re-sent on every LLM call
    "llm_cached_tokens_per_min": 800,
    "llm_completion_tokens_per_min": 380,
    "agent_chars_per_min": 530,
}

# Realistic-cost assumptions. Each is a judgement, stated so it can be argued with.
REALISTIC = {
    "extra_billed_min_per_call": 0.5,   # calls vary around 3:00; Twilio bills each started minute
    "llm_overhead": 0.15,               # interrupted replies are still billed; re-prompts, retries
    "busy_hours_per_day": 10,           # the 3,000 call-minutes land in 10 hours
    "peak_factor": 3.0,                 # busy-hour peak vs. average concurrency
    "standby_vms": 1,                   # one spare for deploys and failures
    "vm_hours_per_day": 24,             # always on
    "phone_numbers": 5,                 # a few numbers for concurrency and caller-ID reputation
    "outbound_voicemail_share": 0.2,    # outbound attempts that reach voicemail and are billed
    "voicemail_billed_min": 1.0,
    "gst": 0.18,                        # IGST on imported online services (reverse charge)
}


def measured_usage() -> tuple[dict, str]:
    from app.config import DATA_DIR

    from app.calls.latency_stats import is_real_call

    calls = [json.loads(p.read_text()) for p in (DATA_DIR / "calls").glob("*.json")]
    # Real Twilio calls only: fake-client calls are short scripted turns.
    calls = [c for c in calls if is_real_call(c) and (c.get("duration_seconds") or 0) > 20 and c.get("usage")]
    minutes = sum(c["duration_seconds"] for c in calls) / 60
    if not calls or minutes == 0:
        return DEFAULTS, "defaults (no real calls over 20s yet)"
    total = lambda key: sum(c["usage"].get(key, 0) for c in calls)
    usage = {
        "llm_prompt_tokens_per_min": total("llm_prompt_tokens") / minutes,
        "llm_cached_tokens_per_min": total("llm_cached_tokens") / minutes,
        "llm_completion_tokens_per_min": total("llm_completion_tokens") / minutes,
        "agent_chars_per_min": total("tts_characters") / minutes,
    }
    return usage, f"measured from {len(calls)} real calls, {minutes:.1f} call-minutes"


def per_minute_raw(p: dict, u: dict, direction: str) -> dict[str, float]:
    tel = p["telephony"]["outbound_per_min_usd" if direction == "outbound" else "inbound_per_min_usd"]
    llm = p["llm"]
    return {
        "telephony": tel,
        "stt": p["stt"]["per_min_usd"],
        "llm": (u["llm_prompt_tokens_per_min"] * llm["input_per_mtok_usd"]
                + u["llm_cached_tokens_per_min"] * llm["cached_input_per_mtok_usd"]
                + u["llm_completion_tokens_per_min"] * llm["output_per_mtok_usd"]) / 1e6,
        "tts": u["agent_chars_per_min"] * p["tts"]["per_char_usd"],
        "server": p["server"]["per_hour_usd"] / p["server"]["concurrent_calls"] / 60,
    }


def daily(p: dict, u: dict, direction: str) -> dict:
    minutes = CALLS_PER_DAY * MINUTES_PER_CALL
    raw_min = per_minute_raw(p, u, direction)
    raw = {k: v * minutes for k, v in raw_min.items()}

    r = REALISTIC
    tel_rate = raw_min["telephony"]
    extra = {
        "per-minute rounding": CALLS_PER_DAY * r["extra_billed_min_per_call"] * tel_rate,
        "LLM overhead (interrupted replies, retries)": raw["llm"] * r["llm_overhead"],
    }
    avg_concurrency = minutes / (r["busy_hours_per_day"] * 60)
    vms = math.ceil(avg_concurrency * r["peak_factor"] / p["server"]["concurrent_calls"]) + r["standby_vms"]
    sized_server = vms * r["vm_hours_per_day"] * p["server"]["per_hour_usd"]
    extra["servers sized for peak, always on"] = sized_server - raw["server"]
    extra["phone number rental"] = (r["phone_numbers"]
                                    * p["telephony"]["breakdown"]["us_local_number_per_month"] / 30)
    if direction == "outbound":
        extra["voicemail attempts"] = (CALLS_PER_DAY * r["outbound_voicemail_share"]
                                       * r["voicemail_billed_min"] * tel_rate)
    pre_tax = sum(raw.values()) + sum(extra.values())
    extra["GST 18%"] = pre_tax * r["gst"]
    return {"raw": raw, "extra": extra, "vms": vms, "avg_concurrency": avg_concurrency,
            "minutes": minutes}


def render(p: dict, u: dict, source: str) -> str:
    fx = p["usd_inr"]["rate"]
    inr = lambda usd: f"₹{usd * fx:,.0f}"
    lines = [f"Scenario: {CALLS_PER_DAY:,} calls/day x {MINUTES_PER_CALL:g} min = "
             f"{CALLS_PER_DAY * MINUTES_PER_CALL:,.0f} call-minutes/day. "
             f"USD→INR {fx} ({p['usd_inr']['source']}, {p['as_of']}).",
             f"Usage per minute: {source}: "
             f"{u['llm_prompt_tokens_per_min']:.0f} prompt + {u['llm_cached_tokens_per_min']:.0f} cached + "
             f"{u['llm_completion_tokens_per_min']:.0f} output tokens, "
             f"{u['agent_chars_per_min']:.0f} agent characters.", ""]
    for direction in ("inbound", "outbound"):
        d = daily(p, u, direction)
        raw_total = sum(d["raw"].values())
        real_total = raw_total + sum(d["extra"].values())
        lines += [f"**{direction.capitalize()}**", "",
                  "| Component | Per day | Per minute |", "| --- | --- | --- |"]
        for k, v in d["raw"].items():
            lines.append(f"| {k} | {inr(v)} | ₹{v * fx / d['minutes']:.3f} |")
        lines.append(f"| **Raw total** | **{inr(raw_total)}** | **₹{raw_total * fx / d['minutes']:.2f}** |")
        for k, v in d["extra"].items():
            lines.append(f"| + {k} | {inr(v)} | ₹{v * fx / d['minutes']:.3f} |")
        lines += [f"| **Realistic total** | **{inr(real_total)}** | **₹{real_total * fx / d['minutes']:.2f}** |",
                  "", f"Servers: average concurrency {d['avg_concurrency']:.1f} calls, "
                  f"{d['vms']} VMs at peak x{REALISTIC['peak_factor']:g} plus standby.", ""]
    lines.append("Realistic-cost assumptions: " + "; ".join(f"{k} = {v}" for k, v in REALISTIC.items()))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--defaults", action="store_true", help="ignore call records")
    args = parser.parse_args()
    pricing = json.loads((ROOT / "pricing.json").read_text())
    usage, source = (DEFAULTS, "defaults") if args.defaults else measured_usage()
    print(render(pricing, usage, source))


if __name__ == "__main__":
    main()
