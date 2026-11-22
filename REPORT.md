# REPORT

Every number says how it was measured. All numbers below come from **10 real phone calls** on
2026-11-22 (Twilio trial, outbound to a verified Indian mobile), regenerated with the commands shown.
The setup changed during the day as real calls exposed problems; where that matters, numbers are
split by setup. Fake-client calls are excluded everywhere (`is_real_call` in
`app/calls/latency_stats.py`).

Setup for all measurements: the server on one MacBook (Apple Silicon, CPU only) behind ngrok,
Twilio trial account with a US number, Groq `openai/gpt-oss-120b` (reasoning effort low), NVIDIA
`nemotron-asr-streaming` for speech-to-text, Kokoro TTS locally, Silero VAD at 0.2s with a 0.8s speech
timeout. Deepgram (Nova-3, then Nova-2, en-IN) was tried on real calls on 2026-11-22. Replayed clips
favoured Nova-2 (words wrong: Nova-2 20%, NVIDIA 28%, Nova-3 36%; 10 utterances, 2 runs), but on live
calls NVIDIA was clearly more accurate for this caller, so NVIDIA stayed; `STT_PROVIDER=deepgram`
switches. Real-call numbers below are from NVIDIA calls.

---

## 1. Latency

**What is measured:** time from the caller stopping speaking to the first sound of the agent's reply.

**How:**

- **Server side, per stage**: Pipecat's `UserBotLatencyObserver` (`app/calls/recorder.py`) logs every
  reply: total from the VAD "user stopped" moment (adjusted back by the 0.2s VAD stop window) to the
  first audio frame the agent sends, plus turn-detection time and each service's time to first byte.
  Stored per turn in `GET /calls/{id}` → `latency`.
  `python tools/measure.py latency --since <date>` gives median and p95.
- **Telephony round trip**: with `TWILIO_DUAL_RECORDING=true`, Twilio records each call at its edge
  (caller and agent on separate channels). `python tools/measure.py twilio <call_id>` compares the
  reply gaps in Twilio's recording with the gaps in our server recording; the difference is the
  Twilio ↔ server round trip. The phone ↔ Twilio carrier leg is outside both recordings and is
  not measured.
- On a tool turn the first sound is the filler line ("Let me check that."), which is what the brief
  defines as the first sound. The time until the actual answer is reported separately.

### All turns (38 replies across 10 real calls)

`python tools/measure.py latency`. Server side: caller stopped speaking → agent's first audio frame.

| Stage | n | median (s) | p95 (s) |
| --- | --- | --- | --- |
| **Total (server side)** | 38 | **2.71** | **4.44** |
| Turn detection (VAD 0.2s + 0.8s timeout) | 37 | 1.00 | 1.49 |
| Speech-to-text, time to first byte | 35 | 0.54 | 0.85 |
| Model, time to first token (all model calls in the turn) | 37 | 0.45 | 1.01 |
| Text-to-speech, time to first audio | 38 | 0.90 | 1.60 |
| Telephony round trip (Twilio ↔ server), per call | 8 calls | 0.59 | 0.65 (max) |

What the caller hears is server total + the Twilio ↔ server round trip + the phone network leg
(not measurable here). At Twilio's edge, the per-call median gap from caller-stopped to agent-audio
was 2.7-3.6s.

Turn detection and STT overlap: STT finalises during the 0.8s wait. The stages are not additive.

**By setup** (total, median / p95):

| Setup | Replies | Total | TTS first audio |
| --- | --- | --- | --- |
| NVIDIA, word-based interruptions, Kokoro loaded per call (first 4 calls) | 17 | 2.81 / 7.11 | 1.05 / 1.56 |
| Deepgram Nova-3 / Nova-2 (3 calls, rolled back) | 10 | 2.64 / 4.44 | 1.10 / 1.60 |
| **NVIDIA, final setup** (shared warm Kokoro, short first chunk, voice interruptions) | 8 | **2.48 / 3.78** | **0.57 / 0.74** |

The final setup has only one call of 8 replies, so its numbers are indicative. The TTS change is the
one clearly visible: sharing one warmed model and cutting the first chunk at ~20 characters halved
time to first audio.

### Tool turns (`check_slots`, `book_slot`): 20 replies

| Stage | n | median (s) | p95 (s) |
| --- | --- | --- | --- |
| Total (server side) | 20 | 2.59 | 3.89 |
| Turn detection | 20 | 1.00 | 1.45 |
| Speech-to-text | 20 | 0.55 | 0.77 |
| Model (both calls: tool call + answer) | 20 | 0.46 | 1.01 |
| Text-to-speech | 20 | 0.82 | 1.45 |
| Tool HTTP time | 20 | 0.015 | 0.025 |

Until 21:00 a filler line ("Let me check that.") played on every tool turn, so on most of these
calls the first sound was the filler. That turned out to delay the actual answer by ~2s: the tool
answers in 15 ms, but Pipecat keeps frames in order, so the tool result waited behind the filler
audio, and callers said "hello?" into the gap, which interrupted and restarted the answer. The
filler now plays only if a tool is still running after 0.5s. On a fake call after the change, a
slot question was answered 2.8s after the caller stopped, with no filler.

### Knowledge-base turns (`lookup_knowledge`): 2 replies

| Stage | n | values (s) |
| --- | --- | --- |
| Total (server side) | 2 | 2.75, 7.11 |
| Model (two calls) | 2 | 0.30, 1.27 |
| Text-to-speech | 2 | 1.32, 3.61 |
| Lookup itself | 2 | < 0.001 |

Too few for a median. What the lookup adds is a second model call (~0.3-0.8s on Groq). The search
itself runs in-process in under 1 ms (`tools[].ms` per run). The 7.1s reply came from a long first
sentence on the per-call Kokoro model, fixed since. On a fake call after the TTS fix, a knowledge
turn took 4.1s (two model calls, then TTS).

---

## 2. Cost per minute

1,000 calls a day, 3 minutes each, in rupees. Generated by `python tools/cost_model.py`, which
multiplies the list prices in [pricing.json](pricing.json) by usage measured from call records.

**Prices used** (all pages opened 2026-11-19):

| Item | Price | Source |
| --- | --- | --- |
| Twilio inbound, US local number | $0.0085/min | [twilio.com/en-us/voice/pricing/us](https://www.twilio.com/en-us/voice/pricing/us) |
| Twilio outbound to an Indian mobile | $0.0496/min | [twilio.com/en-us/voice/pricing/in](https://www.twilio.com/en-us/voice/pricing/in) |
| Twilio Media Streams | $0.0044/min | [twilio.com/en-us/voice/pricing/us](https://www.twilio.com/en-us/voice/pricing/us) |
| Twilio US local number | $1.15/month | same |
| STT: Deepgram Nova-3 streaming (as the paid equivalent; NVIDIA's hosted API is trial-only) | $0.0048/min | [deepgram.com/pricing](https://deepgram.com/pricing) |
| LLM: Groq gpt-oss-120b | $0.15 / $0.60 per 1M input / output tokens; cached input 50% off | [console.groq.com/docs/models](https://console.groq.com/docs/models), [prompt caching](https://console.groq.com/docs/prompt-caching) |
| TTS: Kokoro self-hosted | $0 per character (CPU time is in "server") | — |
| Server: AWS EC2 c7i.large, Mumbai, on-demand | $0.08925/hour | [aws.amazon.com/ec2/pricing/on-demand](https://aws.amazon.com/ec2/pricing/on-demand/) |
| USD → INR | ₹93.3461 | [RBI / FBIL reference rate](https://www.rbi.org.in/), 2026-11-19 |

**Usage assumptions**: measured per call-minute from the 8 real calls over 20s (10.2 call-minutes):
**7,410 prompt + 1,838 cached + 1,005 output tokens per minute**, and **452 characters spoken by the
agent per minute**. The prompt is large because the whole context, including tool definitions, is
re-sent on every model call, and tool turns make two calls.

| | Raw (list prices) | Realistic |
| --- | --- | --- |
| Inbound | **₹1.89/min** · ₹5,684/day | **₹3.14/min** · ₹9,408/day |
| Outbound | **₹5.73/min** · ₹17,193/day | **₹8.81/min** · ₹26,442/day |

Raw inbound per minute: telephony ₹1.20, STT ₹0.45, LLM ₹0.17, server ₹0.07, TTS ₹0.
Telephony is 64% of raw inbound and 88% of raw outbound.

For comparison, the 9 real calls that connected cost ₹5.19-11.49 each by the same per-call formula
(`GET /calls/{id}` → `cost`), almost all of it outbound telephony billed by the started minute.

**Hidden costs included in "realistic"** (assumptions in `REALISTIC` in `tools/cost_model.py`):

1. Per-minute rounding: Twilio bills every started minute; calls averaging 3:00 bill about 0.5 min more.
2. Servers sized for the busy hour and always on: 3,000 call-minutes in 10 busy hours is an average
   of 5 concurrent calls; ×3 peak and 2 calls per VM → 8 VMs + 1 standby, 24 h a day.
3. LLM overhead +15%: interrupted replies are still billed, plus re-prompts and retries.
4. Phone number rental: 5 numbers.
5. Outbound only: 20% of attempts reach voicemail and bill about a minute.
6. GST 18% (IGST on imported online services).

**Not included** (no sourced price, or depends on the deployment): recording storage, logging and
monitoring, an NVIDIA AI Enterprise licence if self-hosting STT ($4,500 per GPU per year plus the
GPU), engineering and on-call time.

---

## 3. Interruption test

**Method.** Every time the caller talked over the agent on the real calls, measured on **Twilio's
recording** (made at Twilio's edge, caller and agent on separate channels, so it is what the caller
heard minus the phone leg) with `python tools/measure.py twilio <call_id>`. Time to go quiet = from
the caller's first sound to the agent's last sound, energy-based per channel. "Clean" = the agent
stopped, didn't carry on its old sentence, and answered what was said, judged from the call's
transcript (not by listening). These are the natural interruptions from 4 calls, not one dedicated
10-interruption call: that call wasn't made, to stay inside the trial credit.

**Voice-based interruptions (current rule), 10 interruptions:**

| # | Call | Caller said | Agent was saying | Quiet after (s) | Clean? |
| --- | --- | --- | --- | --- | --- |
| 1 | 2efec | "Hello" | the greeting | 0.98 | yes |
| 2 | 2efec | "I am Sanjay" | "Good morning, Bright Smile Dental, …" | 0.78 | yes: "Which day…, Sanjay?" |
| 3 | 2efec | "Hello, hello, hello" | "We have slots at nine o'clock, …" | 0.84 | no: carried on listing times |
| 4 | 2efec | "nine o'clock" ×3 | "nine o'clock, nine thirty, …" | 1.02 | yes: confirmed nine o'clock |
| 5 | a4255 | "Hello?" | the greeting | 1.18 | yes |
| 6 | a4255 | "nine o'clock" ×2, heard as "Handbook" (Nova-3) | "We have openings tomorrow at nine…" | 0.74 | no: misheard, carried on |
| 7 | aaf21 | "Hello?" | "…which day would you like…?" | 0.48 | yes: asked again |
| 8 | 4b2d5 | "Hello" | the greeting | 1.18 | yes |
| 9 | 4b2d5 | speech the recogniser returned no words for | "We have openings at eleven…" | 1.40 | stopped; resumed after 2s by design |
| 10 | 4b2d5 | "Eleven o'clock" | "eleven, eleven thirty, …" | 0.86 | yes: asked for the name |

**All 10 stopped**, median **0.92s**, max 1.40s to go quiet at Twilio's edge (this includes the
~0.3s server → Twilio leg). **7 of 10 clean.** The 3 that weren't came from the words, not the
stopping: a misrecognition (#6), no words at all (#9), and "hello" taken as a cue to carry on (#3).

**Word-based interruptions (first rule, first 3 calls), 9 overlaps:** median **1.30s**, max 4.96s
to go quiet, and one short "hello" over a time offer never stopped the agent.

Backchannels ("mm-hmm") were not tested separately. Under the current rule they interrupt too (see
below).

**Why the rule changed after the first real calls.** The first version interrupted only on words
from the recogniser, so a cough or "mm-hmm" never stopped the agent. On real calls that was too slow:

- At Twilio's edge, the agent went quiet 1.2-1.7s after the caller started talking, and one short
  "hello" said over a time offer never stopped it.
- Fed the same audio offline, the hosted NVIDIA recogniser returned its first word 1.0-1.3s after
  the caller started, and turned that "hello" into "N".
- Silero VAD with Pipecat's defaults (confidence 0.7, min volume 0.6) missed 2 of the call's 9
  caller utterances. At confidence 0.5 and min volume 0.2 it caught all 9, about 0.25s in.

So voice now interrupts, and words are the backup. The cost: an "mm-hmm" stops the agent too. If a
turn ends with no words (a cough), the agent resumes its reply instead of going silent. The proper
fix, not built: pause on voice and resume if the words turn out to be a backchannel.

---

## 4. What I would change first

**To cut latency** (expected effects are estimates):

1. **Turn detection: 1.0s of every reply.** The fixed 0.8s pause after a 0.2s VAD stop is now the
   largest stage. A turn model (Pipecat's Smart Turn v3) with the timeout as a fallback could end
   most turns in ~0.3s. MediConnect saw Smart Turn cut people off, so it needs tuning on real calls.
   Expected: -0.5s median.
2. **TTS: 0.57-1.05s median, up to 3.6s.** Kokoro on a laptop CPU needs 0.6-1.2s for even a short
   phrase, and more under load (benchmarked warm, 18-25 characters, server running). A streaming TTS with low first-byte latency, or Kokoro on a GPU, should
   bring it under 0.3s. Expected: -0.5s median, and most of the p95.
3. **Network: 0.59s Twilio ↔ server round trip.** The audio goes India → Twilio (US) → ngrok →
   a laptop in India and back. A server next to Twilio's media region, or an Indian number on an
   Indian provider, removes most of it. Expected: -0.3 to -0.5s.

**To cut cost:**

1. **Telephony (64-88% of the bill):** an Indian number on an Indian provider (Exotel or Plivo) or a
   SIP trunk instead of a US Twilio number plus Media Streams. Outbound to Indian mobiles is ₹5.04 of
   the ₹5.73 raw minute.
2. **STT (₹0.45/min):** at this volume, self-host the recogniser on a GPU shared by many calls, or a
   committed-use rate. Deepgram was tested here (section 1 setup notes) and rejected on accuracy.
3. **LLM (₹0.17/min):** prompt caching already halves cached input. Trim tool definitions from the
   context and try `gpt-oss-20b` ($0.075 / $0.30 per 1M) for routine turns.

**Not done because of the trial / free-tier limits** (as the brief allows): an inbound call from an
Indian phone (dialling the US trial number needs ISD balance; inbound is tested with
`tools/fake_twilio_call.py`, which speaks Twilio's media-stream protocol), and a dedicated
10-interruption call.
