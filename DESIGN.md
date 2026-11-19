# DESIGN

A phone call reaches Twilio, which streams 8 kHz audio over a WebSocket to one Pipecat pipeline per
call: Silero VAD → NVIDIA streaming STT → turn rules → Groq LLM with HTTP tools → Kokoro TTS → back to
Twilio. Each call's behaviour (prompt, voice, greeting, tools, timeouts) comes from the `POST /calls`
JSON. When a call ends its record (transcript, tools, recording, latency, cost) is saved and POSTed to
`webhookUrl`.

## 1. Where the time goes in one reply

Measured server side per turn by Pipecat's latency observer (REPORT.md §1). On a fake call, about
2.4s from the caller stopping to the agent's first audio:

| Stage | Time | Why |
| --- | --- | --- |
| Turn detection | ~1.0s | 0.2s VAD stop + 0.8s speech timeout before the turn counts as over |
| STT final | 0.3–0.8s | mostly overlaps the turn wait; the last words finalise after it |
| LLM first token | 0.25–1.1s | Groq gpt-oss-120b, reasoning low; tool turns run the model twice |
| TTS first audio | 0.45–2.8s | Kokoro on CPU synthesises a whole chunk before any audio |
| Network | not visible server side | phone ↔ Twilio ↔ server, both ways (measured from Twilio's recording) |

Three biggest changes:

1. **Faster first audio from TTS**: a streaming TTS with low first-byte latency, or Kokoro on a GPU.
   Largest and most variable stage.
2. **Smarter end of turn**: a turn model that recognises a finished sentence and ends the turn
   before 0.8s, with the timeout as a fallback for unclear cases.
3. **Fewer network hops**: server, STT and LLM in one region near Twilio's media edge; no laptop or
   tunnel in the path.

## 2. Paid API or self-host, per part

| Part | Choice | Latency | Cost |
| --- | --- | --- | --- |
| Telephony | Paid (carrier) | Fixed by the network; pick a provider with an Indian edge | Biggest line (66–89%); negotiate, or use an Indian provider / SIP trunk |
| Speech-to-text | Paid now, self-host at scale | Self-hosted next to the pipeline removes a network hop | Per-minute API vs. GPU + licence; self-host wins once a GPU is kept busy |
| LLM | Paid | Groq's first-token speed is hard to match self-hosted | Small per minute (~₹0.11); self-host only for volume or data rules |
| Text-to-speech | Self-host on GPU, or paid streaming | CPU Kokoro is the slowest stage; GPU or a streaming API fixes it | Self-host has no per-character fee; paid TTS is per character |
| Turn detection | Self-host | Runs in-process, no network hop | Negligible |

## 3. When a provider goes down mid-call

Today: a dropped STT stream reconnects (seen in logs); a tool timeout or error goes back to the model
as a result, so the agent says so out loud; an LLM error ends that reply and the caller can speak
again; the call is always finalised and the webhook still sent.

What I'd add: a fallback per service using Pipecat's `ServiceSwitcher` with its failover strategy
(e.g. a second STT and LLM provider), a short spoken holding line when a stage exceeds its time
budget, and a last-resort closing line ("We're having trouble, please call back") instead of
silence. Every failure would be counted per provider so the switch can be made before callers notice.

## 4. What Pipecat gave me, and what I'd build myself

**Kept from Pipecat:** the frame pipeline and its interruption plumbing, the Twilio serializer
(mu-law, buffer clear on interruption, hang-up), Silero VAD, turn strategies, idle detection,
`UserBotLatencyObserver`, usage metrics and the recording buffer.

**Built here:** the interruption policy (words from interim transcripts start a turn; backchannels are
ignored while the agent talks), the filler that's skipped when the model already spoke, `end_call`
with the closing line as its argument, two-stage inactivity, tools defined as HTTP endpoints in
JSON, the call lifecycle and store, cost accounting.

**What I'd build myself next:** turn-taking and interruption logic are the product's feel, so I'd own
them fully, plus observability (per-stage latency and cost per call on a dashboard). I'd keep using a
framework for transports and codecs.

## 5. The first 30 days

1. **Week 1: measure.** A harness that places scripted calls every night and records p50/p95 per
   stage and cost per call, so every change has a before and after.
2. **Week 2: the two biggest latency items.** TTS first audio and end-of-turn detection, each
   behind a flag and measured on real calls.
3. **Week 3: reliability.** Provider fallbacks, a load test to find concurrent calls per server, and
   alerts on p95 and error rate.
4. **Week 4: cost.** Indian telephony, an STT build-vs-buy decision with numbers, and a short list of
   the next three changes with their expected effect.
