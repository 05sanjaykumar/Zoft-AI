# Zoft voice agent

A service that answers and places real phone calls with a voice agent: it listens, thinks, calls
HTTP tools mid-call, and speaks back. Built on [Pipecat](https://github.com/pipecat-ai/pipecat)
over Twilio Media Streams. The voice core is ported from my earlier project
[MediConnect AI](https://github.com/05sanjaykumar/MediConnect-AI), where it already ran on real calls.

```
caller ──PSTN── Twilio ──WebSocket (8 kHz mu-law)── /twilio/stream
                                                        │
                    Silero VAD → NVIDIA STT → Groq LLM → Kokoro TTS → back to the caller
                                                 │
                                          HTTP tools from the call's JSON config
                                                 │
                                    /mock (Bright Smile Dental, SQLite)
```

## Run it

Requires Python 3.11 or 3.12 (Pipecat's audio code uses `audioop`, removed in 3.13) and `espeak-ng` for Kokoro.

```bash
brew install espeak-ng            # Ubuntu: sudo apt-get install espeak-ng
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # then fill in the keys
uvicorn app.main:app --port 8000
```

The first call downloads the Kokoro model and voices (about 350 MB) into `~/.cache/pipecat`.

### Dashboard

Open **http://localhost:8000/dashboard** while the server runs. One page, refreshed every 3 seconds:

- **Call me**: type a number and the agent calls it (uses `examples/outbound-clinic.json`).
- **Calls**: every call with status, duration, turns and cost; click one for its transcript, tool
  runs (arguments and results), per-reply latency split into STT / LLM / TTS, cost per component,
  the recording, and an **End call** button while it's live.
- **Bookings**: what the mock clinic has booked, so a booking made on a call shows up here.
- **Latency across calls**: median and p95 per stage (turn detection, STT, model, TTS, tools)
  over all real calls, split into plain, tool and knowledge-base turns, with a count toward the
  30 turns REPORT.md needs. Fake test calls are left out unless you tick the box. Same maths as
  `tools/measure.py latency` ([app/calls/latency_stats.py](app/calls/latency_stats.py)).

It reads the same API as everything else (`/calls`, `/mock/bookings`, `/dashboard/latency`); there's nothing it can do
that `curl` can't.

### Try it without a phone

`tools/fake_twilio_call.py` behaves like Twilio on an inbound call: it asks the webhook for TwiML,
opens the media stream, and plays a WAV file into it.

```bash
say -o question.wav --data-format=LEI16@16000 "Do you have any free slots next Monday morning?"
python tools/fake_twilio_call.py --speech question.wav --seconds 30
afplay fake_call.wav              # what the agent said back
```

### Connect a real phone number

1. Expose the server: `ngrok http 8000`, then put the host (no `https://`) in `PUBLIC_HOST`.
2. In the Twilio console, set the number's **Voice → A call comes in** webhook to
   `https://<PUBLIC_HOST>/twilio/voice` (HTTP POST).
3. Restart the server. Call the number and the agent answers.

On a Twilio trial account, callers first hear a trial notice and must press a key, and outbound
calls only reach verified numbers. A free ngrok URL changes on restart, so steps 1–2 repeat.

## API

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/calls` | `direction: "outbound"`: dial `to` now. `direction: "inbound"`: calls to our number use this config from now on |
| `GET` | `/calls` | Recent calls |
| `GET` | `/calls/{id}` | Status, duration, transcript, recording link, tool runs, cost |
| `POST` | `/calls/{id}/end` | Hang up a live call (or cancel one still ringing) |
| `GET` | `/calls/{id}/recording` | Stereo WAV: caller left, agent right |
| `GET` | `/dashboard` | The page above |
| `POST` | `/twilio/voice` | Twilio webhook: returns TwiML that opens the media stream |
| `POST` | `/twilio/status` | Twilio status callback (ringing, completed, no-answer…) |
| `WS` | `/twilio/stream` | Twilio media stream |
| `POST` | `/mock/slots` | Mock clinic `check_slots` |
| `POST` | `/mock/book` | Mock clinic `book_slot` (409 with alternatives if the slot is taken) |
| `GET` | `/mock/bookings` | Everything booked |
| `POST` | `/mock/call-ended` | Test receiver for the after-call webhook |

```bash
# Set how inbound calls behave
curl -X POST localhost:8000/calls -H 'content-type: application/json' -d @examples/inbound-clinic.json

# Make the agent call someone (edit "to" first)
curl -X POST localhost:8000/calls -H 'content-type: application/json' -d @examples/outbound-clinic.json

curl localhost:8000/calls/<id>
curl -X POST localhost:8000/calls/<id>/end
```

The body follows the assignment's example exactly: `systemPrompt`, `voice`, `language`,
`firstSpeaker`, `greeting`, `maxDurationSeconds`, `inactivity`, `tools` (each with an `http`
endpoint and `timeoutMs`), `knowledgeBase`, `direction`, `to`, `webhookUrl`.
See [examples/](examples/) and [app/schemas.py](app/schemas.py).

## Providers

| Part | Choice | Why |
| --- | --- | --- |
| Telephony | Twilio Media Streams | Trial account available, two-way audio over a WebSocket, Pipecat ships the serializer |
| Speech-to-text | NVIDIA `nemotron-asr-streaming` (`STT_PROVIDER=nvidia`, default; `deepgram` for Deepgram Nova-2 en-IN) | Streams partial transcripts while the caller talks. Deepgram was tried on real calls: replayed clips favoured it (words wrong Nova-2 20%, NVIDIA 28%, Nova-3 36%, 10 utterances), but on live calls NVIDIA was clearly more accurate for this caller, so it stayed. Short names fail on all of them over 8 kHz audio, so names are spelled back |
| LLM | Groq `openai/gpt-oss-120b`, reasoning effort low | Fast first token and reliable tool calling (measured in MediConnect) |
| Text-to-speech | Kokoro, local ONNX | No per-character cost; chunked at clauses so long sentences start speaking sooner |
| Turn-taking | Silero VAD (0.2s) + 0.8s speech timeout | Smart Turn cut callers off mid-sentence in MediConnect; a fixed pause is predictable |

Voice names map to Kokoro voices in [app/voice/voices.py](app/voice/voices.py). Kokoro has no
`en-IN` voice, so `female-en-in-1` uses the Hindi voice `hf_alpha` reading English. Check it by ear.

## Layout

```
app/
  main.py              FastAPI app
  config.py            settings from .env
  schemas.py           the POST /calls body (CallConfig)
  calls/store.py       call records (memory + data/calls/*.json), inbound config
  calls/recorder.py    recording, per-turn transcript, per-turn latency, usage
  calls/costs.py       cost per component from usage x pricing.json
  calls/latency_stats.py  median / p95 per stage across calls (dashboard + tools/measure.py)
  calls/finalize.py    at call end: cost, save, POST summary to webhookUrl
  routes/calls.py      /calls API
  routes/twilio.py     Twilio webhook, status callback, media stream
  routes/dashboard.py  /dashboard page and its /dashboard/info
  static/dashboard.html  the page: plain HTML + JS, no build step
  voice/pipeline.py    builds one call's Pipecat pipeline from its config
  voice/services.py    STT, LLM, TTS (with the clause chunker)
  voice/http_tools.py  config tools → HTTP calls, errors and timeouts returned to the model
  voice/voices.py      voice and language mapping
  voice/turns.py       interruption rule: voice starts a turn, words as backup
  voice/call_controls.py  filler, end_call, inactivity, max duration, one question per reply
  voice/knowledge.py   lookup_knowledge tool: keyword search over knowledge/<name>.md
  voice/spelling.py    spelled letters recovered from recogniser text ("yes" = S)
  clinic/              mock clinic: SQLite bookings table, UNIQUE (date, time)
pricing.json           list prices with their source pages
examples/              create-call bodies
knowledge/clinic-faq.md  knowledge base document, one passage per ## section
tools/fake_twilio_call.py
tests/
```

## Tests

```bash
pytest
```

They cover the mock clinic (including a second booking of the same slot failing with alternatives),
the create-call contract, the Twilio webhook and status callback, and HTTP tools returning results,
HTTP errors and timeouts to the model. No test places a real call.

## Status

**Working (verified on 10 real outbound calls on the Twilio trial, plus fake-client calls for inbound):** inbound webhook and media stream, agent-first greeting,
streaming STT → LLM → TTS, `check_slots` called mid-call with the right date, mock clinic with the
double-booking guard, `POST /calls` (inbound config + outbound dial), `GET /calls/:id`,
`POST /calls/:id/end`, Twilio status callback, tool runs logged on the call, and the call controls below.

| Control | How | Result (fake or real calls, as noted) |
| --- | --- | --- |
| Interruptions | Voice starts a turn: Silero VAD tuned for 8 kHz phone audio (confidence 0.5, min volume 0.2); words from the recogniser are the backup; a turn with no words resumes the cut-off reply ([app/voice/turns.py](app/voice/turns.py)) | Changed after real calls: the words-only rule took 1.2-1.7s at Twilio's edge (the recogniser needs 1.0-1.3s for a first word) and missed one short "hello". VAD caught all 9 utterances on that call ~0.25s in. Trade-off: "mm-hmm" now interrupts too |
| Filler during tools | `on_function_calls_started` starts a 0.5s timer; if the tool is still running then, "Let me check that." plays (not if the model already spoke); kept out of the history | Changed after real calls: an always-on filler delayed the answer ~2s behind its own audio (the tool takes ~20 ms), and callers said "hello?" into the gap |
| Agent hang-up | `end_call` tool; its argument is the closing line, then an end frame flushes behind it | Closing line played in full, then the call ended |
| Inactivity | Pipecat idle event: check-in at `afterSeconds`, goodbye and hang-up when the silence reaches `hangUpAfterSeconds` (counted from the end of the check-in) | Check-in at 3s, hang-up 4s after it (config 3s / 7s) |
| Knowledge base | `"knowledgeBase": "clinic-faq"` adds a `lookup_knowledge` tool; the model looks facts up during the call (the document is not in the prompt) and answers only from what it returns ([app/voice/knowledge.py](app/voice/knowledge.py)) | "What do you do here?" → the clinic overview; "How much does a cleaning cost?" → "A cleaning costs ₹1,200. Would you like to book an appointment?" (since then told to say amounts in words) Lookup under 1 ms |
| One question per reply | A reply stops at its first question; tools (including `end_call`) don't run in the same reply as a question, and placeholder arguments like `"[Caller]"` are sent back to the model instead of the endpoint | Added after the first real call, where the model offered slots, ran on with more questions and hung up before the caller answered |
| Spoken goodbye | If the model says goodbye as plain speech instead of calling `end_call`, the call ends once that line has played (not if the caller cut in) | Added after a real call where it said "You're welcome! Goodbye." and stayed on the line |
| Clean TTS audio | One Kokoro model loaded and warmed up at server start, shared by all calls; its samples are capped before Pipecat converts them, and a reply's first chunk is cut at ~20 characters ([app/voice/services.py](app/voice/services.py)) | Real call before: 35 clicks in 2 minutes (samples up to 1.18 wrapped around). Fake call after: 0, peak 88% of full scale. First chunk 70 chars / 2.45s → 30 chars / 1.5s |
| Spelled names | The agent asks callers to spell their name. Letters the recogniser writes as words ("yes" = S, "and" = N, "why" = Y) are recovered per turn and added to the transcript as "(spelled: S A N J)"; a booking name must be something the caller said or spelled ([app/voice/spelling.py](app/voice/spelling.py)) | Real call: "S, A, N, J" came through as "Yes, A and J" |
| Max duration | Timer from stream start, ends with a closing line | Ended at 8s (config 8s) after the line played |

Twilio hangs up only after the last line finishes ("Bot stopped speaking" comes before the end frame in the logs).

**After the call** (verified on real calls): `GET /calls/{id}` and the webhook carry a
per-turn transcript with timestamps, every tool run with inputs, result and time, a stereo recording,
per-turn latency (caller stopped → agent's first audio, split into STT / LLM / TTS / tools), usage
(tokens, audio seconds, characters) and a cost breakdown per component in rupees. A 39-second
inbound booking call cost ₹1.60: telephony ₹1.20 (one billed minute), STT ₹0.28, LLM ₹0.07,
server ₹0.05, TTS ₹0. Prices and their sources are in [pricing.json](pricing.json).

**Not verified on a real line:** an inbound call. Dialling the US trial number from an Indian phone
needs ISD balance, so inbound is tested with `tools/fake_twilio_call.py`, which speaks Twilio's
webhook and media-stream protocol. Before taking real inbound calls, point the number's
**Voice → A call comes in** webhook at `https://<PUBLIC_HOST>/twilio/voice` (it still points at an
older project). Also not checked: the Hindi voice by ear.

**Measured results** are in [REPORT.md](REPORT.md): 38 replies over 10 real calls (median 2.71s server
side, p95 4.44s), 10 interruptions (median 0.92s to go quiet at Twilio's edge), cost per minute from
real usage.

## Measuring (for REPORT.md)

```bash
# in .env for measurement calls only: Twilio records each call at its edge ($0.0025/min)
TWILIO_DUAL_RECORDING=true

python tools/measure.py latency --since 2026-11-20      # median / p95 per stage, real calls only
python tools/measure.py audio data/recordings/<id>.wav  # reply gaps and interruption stop times
python tools/measure.py twilio <id>                     # adds the Twilio <-> server round trip
python tools/cost_model.py                              # 1,000 calls/day x 3 min, raw and realistic, in ₹
```
