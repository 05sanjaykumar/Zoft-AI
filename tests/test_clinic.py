from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.clinic import db
from app.main import app

client = TestClient(app)


def next_weekday(start: date | None = None) -> date:
    day = (start or date.today()) + timedelta(days=1)
    while day.weekday() in db.CLOSED_WEEKDAYS:
        day += timedelta(days=1)
    return day


def next_sunday() -> date:
    day = date.today() + timedelta(days=1)
    while day.weekday() != 6:
        day += timedelta(days=1)
    return day


def test_slots_skip_lunch():
    slots = db.all_slots(next_weekday())
    assert slots[0] == "09:00" and slots[-1] == "16:30"
    assert "13:00" not in slots and "13:30" not in slots


def test_check_slots_lists_free_times():
    day = next_weekday(date.today() + timedelta(days=20))
    r = client.post("/mock/slots", json={"date": day.isoformat()})
    assert r.status_code == 200
    assert "09:00" in r.json()["free"]


def test_closed_day_points_to_next_available():
    r = client.post("/mock/slots", json={"date": next_sunday().isoformat()})
    body = r.json()
    assert body["free"] == [] and body["reason"] == "closed"
    assert body["next_available"]["free"]


def test_double_booking_fails_for_second_caller():
    day = next_weekday(date.today() + timedelta(days=30)).isoformat()
    first = client.post("/mock/book", json={"date": day, "time": "10:00", "name": "Asha Rao"})
    assert first.status_code == 200 and first.json()["booked"] is True

    second = client.post("/mock/book", json={"date": day, "time": "10:00", "name": "Ravi Kumar"})
    assert second.status_code == 409
    body = second.json()
    assert body["error"] == "slot_taken"
    assert "10:00" not in body["alternatives"] and body["alternatives"]

    free = client.post("/mock/slots", json={"date": day}).json()["free"]
    assert "10:00" not in free


def test_rejects_times_that_are_not_slots():
    day = next_weekday(date.today() + timedelta(days=40)).isoformat()
    r = client.post("/mock/book", json={"date": day, "time": "13:00", "name": "Asha Rao"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_slot"
