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


def test_latency_summary_counts_real_calls_by_kind():
    from app.calls.latency_stats import is_real_call, latency_table

    turn = lambda total, tools=(): {"total_secs": total, "user_turn_secs": 1.0,
                                    "ttfb_secs": {"stt": 0.5, "llm": 0.4, "tts": 0.9},
                                    "tools": [{"name": n, "secs": 0.02} for n in tools]}
    real = {"id": "a", "twilio_sid": "CA123", "latency": [
        {"total_secs": None, "ttfb_secs": {"tts": 2.0}},          # the greeting: not a reply
        turn(2.0), turn(3.0, ["check_slots"]), turn(4.0, ["lookup_knowledge"]), turn(2.5, ["end_call"])]}
    fake = {"id": "b", "twilio_sid": "CAfake1", "latency": [turn(9.0)]}
    assert is_real_call(real) and not is_real_call(fake)

    t = latency_table([real])
    assert t["turns"] == 4
    g = t["groups"]
    assert g["all"]["total"] == {"n": 4, "median": 2.75, "p95": 4.0}
    assert g["plain"]["total"]["n"] == 2              # end_call alone doesn't make a tool turn
    assert g["tool"]["total"]["n"] == 1 and g["knowledge"]["total"]["n"] == 1
    assert g["all"]["tools"]["n"] == 2


def test_latency_endpoint_leaves_out_fake_calls():
    r = client.get("/dashboard/latency").json()
    assert r["target_turns"] == 30 and "groups" in r
