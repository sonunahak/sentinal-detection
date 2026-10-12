import os
import tempfile
import time
from fastapi.testclient import TestClient

os.environ["SENTINEL_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

from app.engine import calculate_corridor_deviation, evaluate_anomaly, point_to_segment_distance
from app.main import app, connect

client = TestClient(app)


def test_spatial_corridor_calculations():
    # Segment from (20.0, 85.0) to (20.0, 85.01)
    # Point at (20.001, 85.005) -> ~111 meters North of segment midpoint
    dist = point_to_segment_distance(20.001, 85.005, 20.0, 85.0, 20.0, 85.01)
    assert 100 < dist < 125

    # Point directly on segment
    on_dist = point_to_segment_distance(20.0, 85.005, 20.0, 85.0, 20.0, 85.01)
    assert on_dist < 1.0

    # Polyline corridor calculation
    corridor = [[20.0, 85.0], [20.0, 85.01], [20.01, 85.01]]
    dev = calculate_corridor_deviation(20.0, 85.005, corridor)
    assert dev < 1.0


def test_mode_aware_anomaly_and_speed_evaluation():
    corridor = [[20.2961, 85.8245], [20.3000, 85.8280]]

    # 1. Normal walking on path: should have low risk
    eval_norm = evaluate_anomaly(
        travel_mode="WALKING",
        current_lat=20.2970,
        current_lon=20.2970,
        speed_mps=1.4,  # ~5 km/h
        corridor=corridor,
        corridor_tolerance_m=100.0,
    )
    assert eval_norm["risk_level"] in ("NORMAL", "ELEVATED")

    # 2. Walking mode but vehicular sprint (10 m/s = 36 km/h): triggers vehicular speed anomaly
    eval_veh = evaluate_anomaly(
        travel_mode="WALKING",
        current_lat=20.2961,
        current_lon=85.8245,
        speed_mps=10.0,
        corridor=corridor,
        corridor_tolerance_m=100.0,
    )
    assert "VEHICULAR_SPEED_ANOMALY" in eval_veh["anomaly_flags"]
    assert eval_veh["risk_score"] >= 25

    # 3. Shake panic trigger: instant critical risk
    eval_panic = evaluate_anomaly(
        travel_mode="WALKING",
        current_lat=20.2961,
        current_lon=85.8245,
        speed_mps=1.0,
        corridor=corridor,
        corridor_tolerance_m=100.0,
        trigger_type="SHAKE_PANIC",
    )
    assert "PANIC_SHAKE_DETECTED" in eval_panic["anomaly_flags"]
    assert eval_panic["risk_level"] == "CRITICAL"
    assert eval_panic["risk_score"] >= 40


def test_session_creation_with_corridor_and_travel_mode():
    corridor = [[20.2961, 85.8245], [20.3000, 85.8300]]
    payload = {
        "duration_minutes": 30,
        "travel_mode": "WALKING",
        "destination_name": "Campus Library",
        "destination_lat": 20.3000,
        "destination_lon": 85.8300,
        "corridor_tolerance_m": 80.0,
        "planned_corridor": corridor,
    }
    response = client.post("/api/sessions", json=payload)
    assert response.status_code == 200
    session = response.json()["session"]
    assert session["travel_mode"] == "WALKING"
    assert session["destination_name"] == "Campus Library"
    assert len(session["planned_corridor"]) == 2


def test_soft_check_in_mitigates_false_alarm():
    session = client.post(
        "/api/sessions",
        json={"travel_mode": "WALKING", "planned_corridor": [[20.0, 85.0], [20.0, 85.01]]},
    ).json()["session"]
    sid = session["id"]

    # Send off-route telemetry
    off_point = {
        "sequence": 1,
        "event_time": "2026-01-01T12:00:00+00:00",
        "latitude": 20.01,  # ~1.1 km off route
        "longitude": 85.0,
        "speed": 1.2,
    }
    res_telem = client.post(f"/api/sessions/{sid}/telemetry", json=off_point)
    assert res_telem.status_code == 200
    assert "OFF_ROUTE_DEVIATION" in res_telem.json()["anomaly_flags"]

    # Session state should reflect soft warning active
    sess_data = client.get(f"/api/sessions/{sid}").json()
    assert sess_data["session"]["soft_warning_remaining"] is not None

    # Traveler taps "I'm OK / Intentional Detour"
    check_in_res = client.post(
        f"/api/sessions/{sid}/check-in",
        json={"acknowledged": True, "reason": "Decided to take the market road"},
    )
    assert check_in_res.status_code == 200

    # Soft warning countdown should now be cleared
    sess_after = client.get(f"/api/sessions/{sid}").json()
    assert sess_after["session"]["soft_warning_remaining"] is None
    assert sess_after["session"]["is_check_in_acknowledged"] == 1


def test_panic_endpoint_triggers_instant_distress():
    session = client.post("/api/sessions", json={}).json()["session"]
    sid = session["id"]

    res = client.post(
        f"/api/sessions/{sid}/panic",
        json={"trigger_type": "SHAKE_PANIC", "latitude": 20.296, "longitude": 85.824},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "DISTRESS"

    # Verify session state and alert
    session_info = client.get(f"/api/sessions/{sid}").json()
    assert session_info["session"]["status"] == "DISTRESS"
    assert "Shake Panic" in session_info["alerts"][0]["reason"]
