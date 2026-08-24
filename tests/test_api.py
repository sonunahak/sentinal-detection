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
