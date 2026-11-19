import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.calls import store
from app.main import app
from app.schemas import CallConfig

client = TestClient(app)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def test_examples_validate():
    CallConfig.model_validate(example("inbound-clinic.json"))
    CallConfig.model_validate(example("outbound-clinic.json"))


def test_outbound_needs_a_number():
    body = example("outbound-clinic.json")
    body.pop("to")
    with pytest.raises(ValidationError):
        CallConfig.model_validate(body)


def test_agent_first_needs_a_greeting():
    body = example("inbound-clinic.json")
    body.pop("greeting")
    with pytest.raises(ValidationError):
        CallConfig.model_validate(body)


def test_inbound_config_is_stored():
    body = example("inbound-clinic.json")
    body["greeting"] = "Hello from the test."
    r = client.post("/calls", json=body)
    assert r.status_code == 201 and r.json()["direction"] == "inbound"
    assert store.get_inbound_config().greeting == "Hello from the test."


def test_outbound_without_twilio_config_is_503():
    r = client.post("/calls", json=example("outbound-clinic.json"))
    assert r.status_code == 503
    assert "TWILIO_ACCOUNT_SID" in r.json()["detail"]


def test_inbound_webhook_creates_a_call_and_streams_it():
    r = client.post("/twilio/voice", data={"CallSid": "CAtest1", "From": "+919800000001",
                                           "To": "+10000000000"})
    assert r.status_code == 200
    assert "<Stream" in r.text
    call_id = re.search(r'name="call_id" value="([^"]+)"', r.text).group(1)

    call = client.get(f"/calls/{call_id}").json()
    assert call["direction"] == "inbound"
    assert call["from_number"] == "+919800000001"
    assert call["twilio_sid"] == "CAtest1"
    for field in ("transcript", "tools", "recording_url", "cost", "duration_seconds"):
        assert field in call


def test_status_callback_marks_call_completed():
    r = client.post("/twilio/voice", data={"CallSid": "CAtest2", "From": "+919800000002"})
    call_id = re.search(r'name="call_id" value="([^"]+)"', r.text).group(1)
    client.post("/twilio/status", data={"CallSid": "CAtest2", "CallStatus": "completed"})
    assert client.get(f"/calls/{call_id}").json()["status"] == "completed"
    assert client.post(f"/calls/{call_id}/end").json()["message"] == "Call already ended."


def test_unknown_call_is_404():
    assert client.get("/calls/nope").status_code == 404
