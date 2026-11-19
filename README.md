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
| Speech-to-text | NVIDIA `nemotron-asr-streaming` | Streams partial transcripts while the caller talks |
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
  calls/finalize.py    at call end: cost, save, POST summary to webhookUrl
  routes/calls.py      /calls API
  routes/twilio.py     Twilio webhook, status callback, media stream
  voice/pipeline.py    builds one call's Pipecat pipeline from its config
  voice/services.py    STT, LLM, TTS (with the clause chunker)
  voice/http_tools.py  config tools → HTTP calls, errors and timeouts returned to the model
  voice/voices.py      voice and language mapping
  voice/turns.py       interruption rule: words, not voice activity; backchannels ignored
  voice/call_controls.py  filler, end_call, inactivity, max duration
  clinic/              mock clinic: SQLite bookings table, UNIQUE (date, time)
pricing.json           list prices with their source pages
examples/              create-call bodies
knowledge/clinic-faq.md  knowledge base document (lookup not wired yet)
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

**Working (verified on fake Twilio calls):** inbound webhook and media stream, agent-first greeting,
streaming STT → LLM → TTS, `check_slots` called mid-call with the right date, mock clinic with the
double-booking guard, `POST /calls` (inbound config + outbound dial), `GET /calls/:id`,
`POST /calls/:id/end`, Twilio status callback, tool runs logged on the call, and the call controls below.

| Control | How | Fake-call result |
| --- | --- | --- |
| Interruptions | Words from interim transcripts start a turn, not voice activity; backchannels are ignored while the agent talks ([app/voice/turns.py](app/voice/turns.py)) | "Mm hmm" over the greeting: no interruption. "Wait, sorry…": agent quiet 0.17s after speech was detected |
| Filler during tools | `on_function_calls_started` speaks "Let me check that." unless the model already said something; kept out of the history | One filler, then the answer |
| Agent hang-up | `end_call` tool; its argument is the closing line, then an end frame flushes behind it | Closing line played in full, then the call ended |
| Inactivity | Pipecat idle event: check-in at `afterSeconds`, goodbye and hang-up when the silence reaches `hangUpAfterSeconds` (counted from the end of the check-in) | Check-in at 3s, hang-up 4s after it (config 3s / 7s) |
| Max duration | Timer from stream start, ends with a closing line | Ended at 8s (config 8s) after the line played |

Twilio hangs up only after the last line finishes ("Bot stopped speaking" comes before the end frame in the logs).

**After the call** (verified on a fake booking call): `GET /calls/{id}` and the webhook carry a
per-turn transcript with timestamps, every tool run with inputs, result and time, a stereo recording,
per-turn latency (caller stopped → agent's first audio, split into STT / LLM / TTS / tools), usage
(tokens, audio seconds, characters) and a cost breakdown per component in rupees. A 39-second
inbound booking call cost ₹1.60: telephony ₹1.20 (one billed minute), STT ₹0.28, LLM ₹0.07,
server ₹0.05, TTS ₹0. Prices and their sources are in [pricing.json](pricing.json).

**Not verified yet:** any of this on a real phone line, outbound dialling, and the Hindi voice by ear.

**Next:**

- [ ] Real phone calls: 30+ measured turns, 10 interruptions → fill the PENDING sections of [REPORT.md](REPORT.md)
- [ ] Demo video
- Knowledge base: optional in the brief and not built; `knowledge/clinic-faq.md` is the document it would use.

## Measuring (for REPORT.md)

```bash
# in .env for measurement calls only: Twilio records each call at its edge ($0.0025/min)
TWILIO_DUAL_RECORDING=true

python tools/measure.py latency --since 2026-11-20      # median / p95 per stage, plain vs tool turns
python tools/measure.py audio data/recordings/<id>.wav  # reply gaps and interruption stop times
python tools/measure.py twilio <id>                     # adds the Twilio <-> server round trip
python tools/cost_model.py                              # 1,000 calls/day x 3 min, raw and realistic, in ₹
```

**Latency findings so far (fake calls, local machine, server side):**

- Caller stopped → agent's first audio: 1.9–2.5s on the booking call's three replies. About 1.0s of
  that is turn detection (0.2s VAD + 0.8s speech timeout).
- Kokoro's time to first audio varied from 0.45s to 2.8s across runs (one word: 0.42s). Line length,
  CPU contention and warm-up are the suspects; to be measured.
- On a tool turn, the model's answer waits for the filler line to finish playing (about 1.8s once).
- STT 0.26–0.78s, Groq LLM 0.24–1.1s, `check_slots` / `book_slot` 16–34 ms.
