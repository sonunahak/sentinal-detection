import os
import tempfile
import time

os.environ["SENTINEL_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_session_telemetry_is_idempotent_and_verifiable():
    session = client.post("/api/sessions", json={"duration_minutes": 15}).json()["session"]
    session_id = session["id"]
    point = {"event_id": "event-1", "sequence": 1, "event_time": "2026-01-01T12:00:00+00:00", "latitude": 20.2961, "longitude": 85.8245, "accuracy": 8, "speed": 1.5}
    assert client.post(f"/api/sessions/{session_id}/telemetry", json=point).status_code == 200
    assert client.post(f"/api/sessions/{session_id}/telemetry", json=point).json()["status"] == "duplicate_ignored"
    assert client.get(f"/api/sessions/{session_id}/verify").json() == {"valid": True, "records": 1}


def test_duress_pin_creates_distress_alert():
    session_id = client.post("/api/sessions", json={"safe_pin": "1111", "duress_pin": "9999"}).json()["session"]["id"]
    response = client.post(f"/api/sessions/{session_id}/cancel", json={"pin": "9999"})
    assert response.json()["status"] == "DISTRESS"
    assert client.get(f"/api/sessions/{session_id}").json()["alerts"][0]["reason"] == "Explicit duress signal"


def test_session_can_be_joined_with_id_prefix():
    response = client.post("/api/sessions", json={"safe_pin": "2222", "duress_pin": "8888"})
    assert response.status_code == 200
    session_id = response.json()["session"]["id"]
    response = client.get(f"/api/sessions/{session_id[:14]}")
    assert response.status_code == 200
    assert response.json()["session"]["id"] == session_id


def test_session_can_be_joined_with_ten_character_token():
    session_id = client.post("/api/sessions", json={}).json()["session"]["id"]
    token = session_id.removeprefix("session-")
    response = client.get(f"/api/sessions/{token}")
    assert response.status_code == 200
    assert response.json()["session"]["id"] == session_id


def test_guardian_name_is_shared_after_joining():
    session_id = client.post("/api/sessions", json={}).json()["session"]["id"]
    response = client.post(f"/api/sessions/{session_id}/join", json={"name": "  Alex   Morgan  "})
    assert response.status_code == 200
    assert response.json()["guardians"][0]["name"] == "Alex Morgan"
    assert client.get(f"/api/sessions/{session_id}").json()["guardians"][0]["name"] == "Alex Morgan"


def test_guardian_location_is_stored_separately_from_telemetry():
    session_id = client.post("/api/sessions", json={}).json()["session"]["id"]
    joined = client.post(f"/api/sessions/{session_id}/join", json={"name": "Alex"}).json()
    guardian_id = joined["guardians"][0]["id"]
    response = client.post(f"/api/sessions/{session_id}/guardian-location", json={"guardian_id": guardian_id, "event_time": "2026-01-01T12:00:00+00:00", "latitude": 20.3, "longitude": 85.8, "accuracy": 5})
    assert response.status_code == 200
    data = client.get(f"/api/sessions/{session_id}").json()
    assert len(data["telemetry"]) == 0
    assert data["guardian_locations"][0]["guardian_id"] == guardian_id


def test_stale_heartbeat_enters_distress_on_session_read():
    session_id = client.post("/api/sessions", json={}).json()["session"]["id"]
    from app.main import connect

    with connect() as db:
        db.execute("UPDATE sessions SET last_telemetry_epoch = ? WHERE id = ?", (int(time.time()) - 16, session_id))
    response = client.get(f"/api/sessions/{session_id}")
    assert response.json()["session"]["status"] == "DISTRESS"
    assert response.json()["events"][-1]["reason"] == "Heartbeat timeout expired: signal lost"
    assert response.json()["alerts"][0]["reason"] == "Heartbeat timeout expired: signal lost"


def test_heartbeat_does_not_keep_location_signal_alive():
    session_id = client.post("/api/sessions", json={}).json()["session"]["id"]
    from app.main import connect

    with connect() as db:
        db.execute("UPDATE sessions SET last_telemetry_epoch = ? WHERE id = ?", (int(time.time()) - 16, session_id))
    assert client.post(f"/api/sessions/{session_id}/heartbeat").status_code == 200
    assert client.get(f"/api/sessions/{session_id}").json()["session"]["status"] == "DISTRESS"


def test_new_session_waits_for_first_telemetry_before_timeout():
    session_id = client.post("/api/sessions", json={}).json()["session"]["id"]
    with client as session_client:
        time.sleep(0)
        response = session_client.get(f"/api/sessions/{session_id}")
    assert response.json()["session"]["status"] == "ACTIVE"


def test_incident_report_download_endpoint_returns_pdf():
    session_id = client.post("/api/sessions", json={}).json()["session"]["id"]
    response = client.get(f"/api/sessions/{session_id}/report")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_alert_cannot_be_acknowledged_twice():
    session_id = client.post("/api/sessions", json={"safe_pin": "3333", "duress_pin": "7777"}).json()["session"]["id"]
    client.post(f"/api/sessions/{session_id}/cancel", json={"pin": "7777"})
    alert_id = client.get(f"/api/sessions/{session_id}").json()["alerts"][0]["id"]
    assert client.post(f"/api/alerts/{alert_id}/acknowledge", json={"responder_id": "responder-1"}).status_code == 200
    assert client.post(f"/api/alerts/{alert_id}/acknowledge", json={"responder_id": "responder-2"}).status_code == 409


def create_trip_payload(**overrides):
    from datetime import datetime, timedelta, timezone

    payload = {
        "traveler_name": "Taylor",
        "guardian_name": "Jordan",
        "start_location": "North entrance",
        "destination": "Central station",
        "expected_arrival_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    payload.update(overrides)
    return payload


def test_trip_lifecycle_reuses_session_telemetry_and_closes_on_completion():
    created = client.post("/api/trips", json=create_trip_payload())
    assert created.status_code == 200
    session = created.json()["session"]
    session_id = session["id"]
    assert session["trip_status"] == "PLANNED"
    assert session["traveler_name"] == "Taylor"
    assert session["destination"] == "Central station"

    point = {"sequence": 1, "event_time": "2026-09-26T12:00:00+00:00", "latitude": 20.3, "longitude": 85.8}
    assert client.post(f"/api/sessions/{session_id}/telemetry", json=point).status_code == 409
    assert client.post(f"/api/trips/{session_id}/start").json()["status"] == "ACTIVE"
    assert client.post(f"/api/sessions/{session_id}/telemetry", json=point).status_code == 200
    assert client.post(f"/api/trips/{session_id}/complete").json()["status"] == "COMPLETED"
    assert client.post(f"/api/sessions/{session_id}/telemetry", json={**point, "sequence": 2}).status_code == 409


def test_trip_input_requires_timezone_and_paired_coordinates():
    missing_timezone = client.post("/api/trips", json=create_trip_payload(expected_arrival_at="2026-09-26T12:00:00"))
    assert missing_timezone.status_code == 422
    unpaired_coordinates = client.post("/api/trips", json=create_trip_payload(start_latitude=20.3))
    assert unpaired_coordinates.status_code == 422
    blank_name = client.post("/api/trips", json=create_trip_payload(traveler_name="   "))
    assert blank_name.status_code == 422


def test_context_refresh_saves_labeled_demo_events(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "CONTEXT_PROVIDER_MODE", "demo")
    session_id = client.post("/api/trips", json=create_trip_payload()).json()["session"]["id"]
    client.post(f"/api/trips/{session_id}/start")
    point = {"sequence": 1, "event_time": "2026-09-26T12:00:00+00:00", "latitude": 20.3, "longitude": 85.8, "bearing": 0}
    assert client.post(f"/api/sessions/{session_id}/telemetry", json=point).status_code == 200

    response = client.post(f"/api/sessions/{session_id}/context/refresh")
    assert response.status_code == 200
    events = response.json()["events"]
    assert events
    assert all(event["is_demo"] == 1 and event["source"] == "MOCK_DEMO" for event in events)
    assert all(event["explanation"] for event in events)
    assert len(client.get(f"/api/sessions/{session_id}/context").json()["events"]) == len(events)

    def fail_provider(self, latitude, longitude, bearing):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(main.DemoContextProvider, "events_for", fail_provider)
    unavailable = client.post(f"/api/sessions/{session_id}/context/refresh").json()
    assert unavailable["message"] == "Context provider temporarily unavailable"
    assert len(unavailable["events"]) == len(events)
    monkeypatch.setattr(main.DemoContextProvider, "events_for", lambda self, latitude, longitude, bearing: [])
    assert client.post(f"/api/sessions/{session_id}/context/refresh").json()["events"] == []
    assert client.get(f"/api/sessions/{session_id}/context").json()["events"] == []
    assert client.post("/api/sessions/not-a-session/context/refresh").status_code == 404
    replay = client.get(f"/api/trips/{session_id}/replay").json()
    assert len([event for event in replay["events"] if event["type"] == "SAFETY_CONTEXT"]) == len(events)


def test_trip_replay_orders_telemetry_and_includes_actual_events():
    session_id = client.post("/api/trips", json=create_trip_payload()).json()["session"]["id"]
    client.post(f"/api/trips/{session_id}/start")
    later = {"sequence": 2, "event_time": "2026-09-26T12:02:00+00:00", "latitude": 20.31, "longitude": 85.81}
    earlier = {"sequence": 1, "event_time": "2026-09-26T12:01:00+00:00", "latitude": 20.30, "longitude": 85.80}
    client.post(f"/api/sessions/{session_id}/telemetry", json=later)
    client.post(f"/api/sessions/{session_id}/telemetry", json=earlier)
    client.post(f"/api/trips/{session_id}/complete")

    replay = client.get(f"/api/trips/{session_id}/replay").json()
    assert [point["event_time"] for point in replay["telemetry"]] == [earlier["event_time"], later["event_time"]]
    assert replay["summary"]["telemetry_points"] == 2
    assert [event["type"] for event in replay["events"]].count("TRIP_STARTED") == 1
    assert [event["type"] for event in replay["events"]].count("TRIP_COMPLETED") == 1


def test_duress_during_a_trip_marks_incident_and_stops_telemetry():
    session_id = client.post("/api/trips", json=create_trip_payload()).json()["session"]["id"]
    client.post(f"/api/trips/{session_id}/start")
    assert client.post(f"/api/sessions/{session_id}/cancel", json={"pin": "9999"}).json()["status"] == "DISTRESS"
    data = client.get(f"/api/sessions/{session_id}").json()
    assert data["session"]["trip_status"] == "INCIDENT"
    assert data["trip_events"][-1]["event_type"] == "TRIP_INCIDENT"
    assert client.post(f"/api/sessions/{session_id}/heartbeat").status_code == 409
