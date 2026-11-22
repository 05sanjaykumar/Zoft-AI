"""The dashboard page and the data it reads."""

from fastapi.testclient import TestClient

from app.calls import store
from app.main import app
from app.schemas import CallConfig

client = TestClient(app)


def test_page_and_info():
    r = client.get("/dashboard")
    assert r.status_code == 200 and "Zoft voice agent" in r.text
    info = client.get("/dashboard/info").json()
    assert info["outbound_example"]["direction"] == "outbound"
    assert info["active_calls"] == 0


def test_calls_saved_before_a_restart_are_listed():
    config = CallConfig.model_validate({"systemPrompt": "Hi", "greeting": "Hello", "direction": "inbound"})
    record = store.create(config, from_number="+919800000002")
    store.save(record)
    store._LIVE.clear()                      # as after a restart
    listed = {c["id"]: c for c in client.get("/calls").json()}
    assert record.id in listed and listed[record.id]["turns"] == 0
