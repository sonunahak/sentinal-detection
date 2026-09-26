from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.context import DemoContextProvider, SafetyContextEngine

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("SENTINEL_DB", ROOT / "sentineltrace.db"))
HEARTBEAT_TIMEOUT_SECONDS = 10
GRACE_PERIOD_SECONDS = 5
CONTEXT_PROVIDER_MODE = os.getenv("SENTINEL_CONTEXT_PROVIDER", "none").strip().lower()
CONTEXT_ENGINE = SafetyContextEngine()

app = FastAPI(title="SentinelTrace", version="1.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
_db_lock = threading.Lock()

RESPONDERS = [
    {"id": "responder-1", "name": "Primary guardian", "role": "First acknowledgement"},
    {"id": "responder-2", "name": "Backup guardian", "role": "Second acknowledgement"},
    {"id": "responder-3", "name": "Escalation contact", "role": "Final escalation"},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, safe_pin TEXT NOT NULL,
            duress_pin TEXT NOT NULL, started_at TEXT NOT NULL, last_heartbeat TEXT,
            warning_at TEXT, distress_at TEXT, ended_at TEXT, last_hash TEXT NOT NULL,
            last_telemetry_epoch INTEGER, traveler_name TEXT, trip_guardian TEXT,
            start_location TEXT, destination TEXT, expected_arrival_at TEXT,
            trip_status TEXT, trip_started_at TEXT, trip_ended_at TEXT,
            start_latitude REAL, start_longitude REAL, destination_latitude REAL,
            destination_longitude REAL, route_json TEXT
        );
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE, sequence INTEGER NOT NULL, event_time TEXT NOT NULL,
            received_at TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            accuracy REAL, speed REAL, bearing REAL, distance_m REAL NOT NULL,
            previous_hash TEXT NOT NULL, current_hash TEXT NOT NULL,
            UNIQUE(session_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS state_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            from_status TEXT, to_status TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
            reason TEXT NOT NULL, created_at TEXT NOT NULL, acknowledged_at TEXT,
            acknowledged_by TEXT, current_responder INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT NOT NULL,
            responder_id TEXT NOT NULL, status TEXT NOT NULL, attempted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS guardians (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            name TEXT NOT NULL, joined_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS guardian_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guardian_id INTEGER NOT NULL,
            session_id TEXT NOT NULL, event_time TEXT NOT NULL,
            received_at TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            accuracy REAL
        );
        CREATE TABLE IF NOT EXISTS trip_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            event_type TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            provider_event_id TEXT NOT NULL, event_type TEXT NOT NULL,
            severity TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            distance_m REAL NOT NULL, route_distance_m REAL, ahead INTEGER NOT NULL,
            source TEXT NOT NULL, explanation TEXT NOT NULL, description TEXT NOT NULL,
            event_time TEXT NOT NULL, observed_at TEXT NOT NULL, is_demo INTEGER NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            UNIQUE(session_id, provider_event_id)
        );
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        if "last_telemetry_epoch" not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN last_telemetry_epoch INTEGER")
        db.execute("UPDATE sessions SET last_telemetry_epoch = CAST(strftime('%s', last_heartbeat) AS INTEGER) WHERE last_telemetry_epoch IS NULL AND last_heartbeat IS NOT NULL")
        trip_columns = {
            "traveler_name": "TEXT", "trip_guardian": "TEXT", "start_location": "TEXT",
            "destination": "TEXT", "expected_arrival_at": "TEXT", "trip_status": "TEXT",
            "trip_started_at": "TEXT", "trip_ended_at": "TEXT", "start_latitude": "REAL",
            "start_longitude": "REAL", "destination_latitude": "REAL",
            "destination_longitude": "REAL", "route_json": "TEXT",
        }
        for column, column_type in trip_columns.items():
            if column not in columns:
                db.execute(f"ALTER TABLE sessions ADD COLUMN {column} {column_type}")
        context_columns = {row["name"] for row in db.execute("PRAGMA table_info(context_events)")}
        if "is_current" not in context_columns:
            db.execute("ALTER TABLE context_events ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1")
init_db()


class SessionCreate(BaseModel):
    duration_minutes: int = Field(default=15, ge=1, le=240)
    safe_pin: str = Field(default="1234", min_length=4, max_length=12)
    duress_pin: str = Field(default="9999", min_length=4, max_length=12)


class TelemetryIn(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sequence: int = Field(ge=0)
    event_time: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    speed: float | None = Field(default=None, ge=0)
    bearing: float | None = Field(default=None, ge=0, lt=360)


class PinIn(BaseModel):
    pin: str


class AckIn(BaseModel):
    responder_id: str


class GuardianJoin(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class GuardianLocationIn(BaseModel):
    guardian_id: int = Field(ge=1)
    event_time: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)


class TripPoint(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class TripCreate(BaseModel):
    traveler_name: str = Field(min_length=1, max_length=80)
    guardian_name: str = Field(min_length=1, max_length=80)
    start_location: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    expected_arrival_at: datetime
    safe_pin: str = Field(default="1234", min_length=4, max_length=12)
    duress_pin: str = Field(default="9999", min_length=4, max_length=12)
    start_latitude: float | None = Field(default=None, ge=-90, le=90)
    start_longitude: float | None = Field(default=None, ge=-180, le=180)
    destination_latitude: float | None = Field(default=None, ge=-90, le=90)
    destination_longitude: float | None = Field(default=None, ge=-180, le=180)
    expected_route: list[TripPoint] = Field(default_factory=list, max_length=500)


class DemoContextRefresh(BaseModel):
    pass


def haversine(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    radius = 6371000
    lat_delta = math.radians(b_lat - a_lat)
    lon_delta = math.radians(b_lon - a_lon)
    value = math.sin(lat_delta / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(lon_delta / 2) ** 2
    return radius * 2 * math.asin(math.sqrt(value))


def calculate_bearing(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    lat1, lat2 = math.radians(a_lat), math.radians(b_lat)
    delta_lon = math.radians(b_lon - a_lon)
    x = math.sin(delta_lon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def transition(db: sqlite3.Connection, session_id: str, current: str, target: str, reason: str) -> None:
    if current == target:
        return
    timestamp = now_iso()
    db.execute("UPDATE sessions SET status = ?, warning_at = CASE WHEN ? = 'WARNING' THEN ? ELSE warning_at END, distress_at = CASE WHEN ? = 'DISTRESS' THEN ? ELSE distress_at END WHERE id = ?", (target, target, timestamp, target, timestamp, session_id))
    db.execute("INSERT INTO state_events(session_id, from_status, to_status, reason, created_at) VALUES(?,?,?,?,?)", (session_id, current, target, reason, timestamp))
    if target == "DISTRESS":
        trip = db.execute("SELECT trip_status FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if trip and trip["trip_status"] == "ACTIVE":
            db.execute("UPDATE sessions SET trip_status = 'INCIDENT', trip_ended_at = ? WHERE id = ?", (timestamp, session_id))
            add_trip_event(db, session_id, "TRIP_INCIDENT", "INCIDENT", reason)


def make_alert(db: sqlite3.Connection, session_id: str, reason: str) -> str:
    alert_id = f"ST-{uuid.uuid4().hex[:8].upper()}"
    timestamp = now_iso()
    db.execute("INSERT INTO alerts(id, session_id, status, reason, created_at) VALUES(?,?,?,?,?)", (alert_id, session_id, "ALERTING", reason, timestamp))
    notify_responder(db, alert_id, 0)
    return alert_id


def notify_responder(db: sqlite3.Connection, alert_id: str, index: int) -> None:
    if index >= len(RESPONDERS):
        db.execute("UPDATE alerts SET status = 'UNACKNOWLEDGED' WHERE id = ?", (alert_id,))
        return
    db.execute("UPDATE alerts SET current_responder = ? WHERE id = ?", (index, alert_id))
    db.execute("INSERT INTO notifications(alert_id, responder_id, status, attempted_at) VALUES(?,?,?,?)", (alert_id, RESPONDERS[index]["id"], "RECORDED_IN_OUTBOX", now_iso()))


def find_session(db: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    token = session_id.strip()
    session = db.execute("SELECT * FROM sessions WHERE id = ?", (token,)).fetchone()
    if session:
        return session
    patterns = (f"{token}%", f"session-{token}%")
    return db.execute("SELECT * FROM sessions WHERE id LIKE ? OR id LIKE ? ORDER BY id LIMIT 1", patterns).fetchone()


def session_payload(db: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    session = find_session(db, session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    session_id = session["id"]
    points = db.execute("SELECT * FROM telemetry WHERE session_id = ? ORDER BY sequence", (session_id,)).fetchall()
    events = db.execute("SELECT * FROM state_events WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    alerts = db.execute("SELECT * FROM alerts WHERE session_id = ? ORDER BY created_at DESC", (session_id,)).fetchall()
    guardians = db.execute("SELECT id, name, joined_at FROM guardians WHERE session_id = ? ORDER BY joined_at", (session_id,)).fetchall()
    guardian_locations = db.execute("SELECT guardian_id, event_time, received_at, latitude, longitude, accuracy FROM guardian_locations WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    trip_events = db.execute("SELECT * FROM trip_events WHERE session_id = ? ORDER BY created_at, id", (session_id,)).fetchall()
    contexts = db.execute("SELECT * FROM context_events WHERE session_id = ? AND is_current = 1 ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END, distance_m, observed_at DESC", (session_id,)).fetchall()
    return {"session": dict(session), "telemetry": [dict(point) for point in points], "events": [dict(event) for event in events], "alerts": [dict(alert) for alert in alerts], "responders": RESPONDERS, "guardians": [dict(guardian) for guardian in guardians], "guardian_locations": [dict(location) for location in guardian_locations], "trip_events": [dict(event) for event in trip_events], "context_events": [dict(event) for event in contexts]}


def add_trip_event(db: sqlite3.Connection, session_id: str, event_type: str, status: str, details: str) -> None:
    db.execute("INSERT INTO trip_events(session_id,event_type,status,details,created_at) VALUES(?,?,?,?,?)", (session_id, event_type, status, details, now_iso()))


def parse_route(value: str | None) -> list[tuple[float, float]]:
    if not value:
        return []
    return [(point["latitude"], point["longitude"]) for point in json.loads(value)]


def configured_context_provider() -> DemoContextProvider | None:
    if CONTEXT_PROVIDER_MODE == "demo":
        return DemoContextProvider()
    return None


def chronological_key(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0.0


def check_timeouts() -> None:
    with _db_lock, connect() as db:
        active = db.execute("SELECT * FROM sessions WHERE status IN ('ACTIVE','WARNING') AND (trip_status IS NULL OR trip_status = 'ACTIVE') AND last_telemetry_epoch IS NOT NULL").fetchall()
        current_epoch = int(time.time())
        for session in active:
            elapsed = current_epoch - session["last_telemetry_epoch"]
            if session["status"] == "ACTIVE" and elapsed > HEARTBEAT_TIMEOUT_SECONDS + GRACE_PERIOD_SECONDS:
                transition(db, session["id"], "ACTIVE", "DISTRESS", "Heartbeat timeout expired: signal lost")
                make_alert(db, session["id"], "Heartbeat timeout expired: signal lost")
            elif session["status"] == "ACTIVE" and elapsed > HEARTBEAT_TIMEOUT_SECONDS:
                transition(db, session["id"], "ACTIVE", "WARNING", "Heartbeat timeout; grace period started")
            elif session["status"] == "WARNING" and elapsed > HEARTBEAT_TIMEOUT_SECONDS + GRACE_PERIOD_SECONDS:
                transition(db, session["id"], "WARNING", "DISTRESS", "Heartbeat grace period expired")
                make_alert(db, session["id"], "Unexpected telemetry loss / possible distress")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    check_timeouts()
    return {"status": "ok"}


@app.get("/api/responders")
def responders() -> list[dict[str, str]]:
    return RESPONDERS


@app.post("/api/sessions")
def create_session(payload: SessionCreate) -> dict[str, Any]:
    if payload.safe_pin == payload.duress_pin:
        raise HTTPException(400, "Safe and duress PINs must be different")
    session_id = f"session-{uuid.uuid4().hex[:10]}"
    timestamp = now_iso()
    with _db_lock, connect() as db:
        db.execute("INSERT INTO sessions(id,status,safe_pin,duress_pin,started_at,last_hash,last_telemetry_epoch) VALUES(?,?,?,?,?,?,?)", (session_id, "ACTIVE", payload.safe_pin, payload.duress_pin, timestamp, "0" * 64, None))
        db.execute("INSERT INTO state_events(session_id,to_status,reason,created_at) VALUES(?,?,?,?)", (session_id, "ACTIVE", "Escort session started", timestamp))
    return session_payload(connect(), session_id)


@app.post("/api/trips")
def create_trip(payload: TripCreate) -> dict[str, Any]:
    if payload.safe_pin == payload.duress_pin:
        raise HTTPException(400, "Safe and duress PINs must be different")
    if payload.expected_arrival_at.tzinfo is None or payload.expected_arrival_at.utcoffset() is None:
        raise HTTPException(422, "Expected arrival must include a timezone")
    if (payload.start_latitude is None) != (payload.start_longitude is None):
        raise HTTPException(422, "Start latitude and longitude must be provided together")
    if (payload.destination_latitude is None) != (payload.destination_longitude is None):
        raise HTTPException(422, "Destination latitude and longitude must be provided together")
    if payload.expected_arrival_at <= datetime.now(timezone.utc):
        raise HTTPException(422, "Expected arrival must be in the future")
    traveler_name = " ".join(payload.traveler_name.split())
    guardian_name = " ".join(payload.guardian_name.split())
    start_location = payload.start_location.strip()
    destination = payload.destination.strip()
    if not traveler_name or not guardian_name or not start_location or not destination:
        raise HTTPException(422, "Traveler, guardian, start location, and destination are required")
    session_id = f"session-{uuid.uuid4().hex[:10]}"
    timestamp = now_iso()
    route = [point.model_dump() for point in payload.expected_route]
    with _db_lock, connect() as db:
        db.execute("""INSERT INTO sessions(
            id,status,safe_pin,duress_pin,started_at,last_hash,last_telemetry_epoch,
            traveler_name,trip_guardian,start_location,destination,expected_arrival_at,
            trip_status,start_latitude,start_longitude,destination_latitude,
            destination_longitude,route_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            session_id, "ACTIVE", payload.safe_pin, payload.duress_pin, timestamp,
            "0" * 64, None, traveler_name, guardian_name, start_location,
            destination, payload.expected_arrival_at.isoformat(),
            "PLANNED", payload.start_latitude, payload.start_longitude,
            payload.destination_latitude, payload.destination_longitude,
            json.dumps(route),
        ))
        add_trip_event(db, session_id, "TRIP_CREATED", "PLANNED", f"Trip planned from {start_location} to {destination}")
    return session_payload(connect(), session_id)


@app.get("/api/trips")
def list_trips() -> list[dict[str, Any]]:
    with connect() as db:
        trips = db.execute("SELECT id, traveler_name, trip_guardian, start_location, destination, expected_arrival_at, trip_status, trip_started_at, trip_ended_at FROM sessions WHERE trip_status IS NOT NULL ORDER BY started_at DESC").fetchall()
        return [dict(trip) for trip in trips]


@app.post("/api/trips/{session_id}/start")
def start_trip(session_id: str) -> dict[str, str]:
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Trip not found")
        if not session["trip_status"]:
            raise HTTPException(409, "Session is not a planned trip")
        if session["trip_status"] != "PLANNED":
            raise HTTPException(409, f"Trip cannot start from {session['trip_status']}")
        timestamp = now_iso()
        db.execute("UPDATE sessions SET trip_status = 'ACTIVE', trip_started_at = ? WHERE id = ?", (timestamp, session["id"]))
        add_trip_event(db, session["id"], "TRIP_STARTED", "ACTIVE", "Traveler started the trip")
    return {"status": "ACTIVE", "trip_started_at": timestamp}


@app.post("/api/trips/{session_id}/complete")
def complete_trip(session_id: str) -> dict[str, str]:
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Trip not found")
        if session["trip_status"] != "ACTIVE":
            raise HTTPException(409, "Only an active trip can be completed")
        timestamp = now_iso()
        db.execute("UPDATE sessions SET trip_status = 'COMPLETED', trip_ended_at = ?, ended_at = ?, status = 'COMPLETED' WHERE id = ?", (timestamp, timestamp, session["id"]))
        db.execute("INSERT INTO state_events(session_id,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?)", (session["id"], session["status"], "COMPLETED", "Trip completed", timestamp))
        add_trip_event(db, session["id"], "TRIP_COMPLETED", "COMPLETED", "Traveler completed the trip")
    return {"status": "COMPLETED", "trip_ended_at": timestamp}


@app.post("/api/trips/{session_id}/cancel")
def cancel_trip(session_id: str, payload: PinIn) -> dict[str, str]:
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Trip not found")
        if not session["trip_status"]:
            raise HTTPException(409, "Session is not a trip")
        if session["trip_status"] not in ("PLANNED", "ACTIVE"):
            raise HTTPException(409, f"Trip cannot be cancelled from {session['trip_status']}")
        if payload.pin != session["safe_pin"]:
            raise HTTPException(403, "Invalid PIN")
        timestamp = now_iso()
        db.execute("UPDATE sessions SET trip_status = 'CANCELLED', trip_ended_at = ?, ended_at = ?, status = 'CANCELLED' WHERE id = ?", (timestamp, timestamp, session["id"]))
        add_trip_event(db, session["id"], "TRIP_CANCELLED", "CANCELLED", "Trip cancelled with safe PIN")
    return {"status": "CANCELLED", "trip_ended_at": timestamp}


@app.post("/api/sessions/{session_id}/context/refresh")
def refresh_context(session_id: str, payload: DemoContextRefresh | None = None) -> dict[str, Any]:
    provider = configured_context_provider()
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if provider is None:
            return {"provider": None, "demo": False, "events": [], "message": "No context provider configured"}
        last = db.execute("SELECT * FROM telemetry WHERE session_id = ? ORDER BY sequence DESC LIMIT 1", (session["id"],)).fetchone()
        if not last:
            return {"provider": provider.name, "demo": True, "events": [], "message": "Context needs a traveler location"}
        route = parse_route(session["route_json"])
        if not route:
            route = [(point["latitude"], point["longitude"]) for point in db.execute("SELECT latitude, longitude FROM telemetry WHERE session_id = ? ORDER BY sequence", (session["id"],)).fetchall()]
        if session["destination_latitude"] is not None:
            route.append((session["destination_latitude"], session["destination_longitude"]))
        try:
            events = CONTEXT_ENGINE.evaluate(provider, last["latitude"], last["longitude"], last["bearing"], route, haversine, calculate_bearing)
        except Exception:
            stored = db.execute("SELECT * FROM context_events WHERE session_id = ? AND is_current = 1 ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END, distance_m, observed_at DESC", (session["id"],)).fetchall()
            return {"provider": provider.name, "demo": CONTEXT_PROVIDER_MODE == "demo", "events": [dict(event) for event in stored], "message": "Context provider temporarily unavailable"}
        db.execute("UPDATE context_events SET is_current = 0 WHERE session_id = ?", (session["id"],))
        observed_at = now_iso()
        for event in events:
            db.execute("""INSERT INTO context_events(
                session_id,provider_event_id,event_type,severity,latitude,longitude,
                distance_m,route_distance_m,ahead,source,explanation,description,
                event_time,observed_at,is_demo
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id,provider_event_id) DO UPDATE SET
                distance_m=excluded.distance_m,route_distance_m=excluded.route_distance_m,
                ahead=excluded.ahead,explanation=excluded.explanation,observed_at=excluded.observed_at,
                is_current=1""", (
                session["id"], event["provider_event_id"], event["event_type"], event["severity"],
                event["latitude"], event["longitude"], event["distance_m"], event["route_distance_m"],
                int(event["ahead"]), event["source"], event["explanation"], event["description"],
                event["event_time"], observed_at, int(event["is_demo"]),
            ))
        add_trip_event(db, session["id"], "CONTEXT_EVALUATED", session["trip_status"] or session["status"], f"Evaluated {len(events)} relevant context events")
        stored = db.execute("SELECT * FROM context_events WHERE session_id = ? AND is_current = 1 ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END, distance_m, observed_at DESC", (session["id"],)).fetchall()
        return {"provider": provider.name, "demo": True, "events": [dict(event) for event in stored]}


@app.get("/api/sessions/{session_id}/context")
def get_context(session_id: str) -> dict[str, Any]:
    with connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        rows = db.execute("SELECT * FROM context_events WHERE session_id = ? AND is_current = 1 ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END, distance_m, observed_at DESC", (session["id"],)).fetchall()
        return {"provider": CONTEXT_PROVIDER_MODE.upper() if CONTEXT_PROVIDER_MODE == "demo" else None, "demo": CONTEXT_PROVIDER_MODE == "demo", "events": [dict(row) for row in rows]}


@app.get("/api/trips/{session_id}/replay")
def trip_replay(session_id: str) -> dict[str, Any]:
    with connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Trip not found")
        if not session["trip_status"]:
            raise HTTPException(409, "Session is not a trip")
        session_id = session["id"]
        telemetry_points = db.execute("SELECT sequence,event_time,latitude,longitude,accuracy,speed,bearing,distance_m FROM telemetry WHERE session_id = ? ORDER BY event_time, sequence", (session_id,)).fetchall()
        timeline = []
        for event in db.execute("SELECT * FROM trip_events WHERE session_id = ?", (session_id,)).fetchall():
            timeline.append({"timestamp": event["created_at"], "type": event["event_type"], "status": event["status"], "details": event["details"], "latitude": None, "longitude": None})
        for event in db.execute("SELECT * FROM state_events WHERE session_id = ?", (session_id,)).fetchall():
            timeline.append({"timestamp": event["created_at"], "type": "STATE_CHANGE", "status": event["to_status"], "details": event["reason"], "latitude": None, "longitude": None})
        for alert in db.execute("SELECT * FROM alerts WHERE session_id = ?", (session_id,)).fetchall():
            timeline.append({"timestamp": alert["created_at"], "type": "ALERT", "status": alert["status"], "details": alert["reason"], "latitude": None, "longitude": None})
        for event in db.execute("SELECT * FROM context_events WHERE session_id = ?", (session_id,)).fetchall():
            timeline.append({"timestamp": event["observed_at"], "type": "SAFETY_CONTEXT", "status": event["severity"], "details": f"{event['description']} — {event['explanation']} ({event['source']})", "latitude": event["latitude"], "longitude": event["longitude"]})
        timeline.sort(key=lambda event: chronological_key(event["timestamp"]))
        start_time = session["trip_started_at"] or session["started_at"]
        end_time = session["trip_ended_at"] or session["ended_at"]
        duration_seconds = max(0, int(chronological_key(end_time) - chronological_key(start_time))) if end_time else None
        return {
            "trip": dict(session),
            "summary": {
                "start": session["start_location"], "destination": session["destination"],
                "start_time": start_time, "end_time": end_time,
                "duration_seconds": duration_seconds, "telemetry_points": len(telemetry_points),
                    "event_count": len(timeline), "final_status": session["trip_status"],
                    "traveler": session["traveler_name"], "guardian": session["trip_guardian"],
            },
            "telemetry": [dict(point) for point in telemetry_points],
            "events": timeline,
        }


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    check_timeouts()
    with connect() as db:
        return session_payload(db, session_id)


@app.post("/api/sessions/{session_id}/join")
def join_session(session_id: str, payload: GuardianJoin) -> dict[str, Any]:
    name = " ".join(payload.name.split())
    if not name:
        raise HTTPException(422, "Guardian name is required")
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session["status"] in ("CANCELLED", "RESOLVED", "EXPIRED"):
            raise HTTPException(409, "Session is no longer active")
        db.execute("INSERT INTO guardians(session_id, name, joined_at) VALUES(?,?,?)", (session["id"], name, now_iso()))
        return session_payload(db, session["id"])


@app.post("/api/sessions/{session_id}/guardian-location")
def guardian_location(session_id: str, payload: GuardianLocationIn) -> dict[str, str]:
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session["status"] in ("CANCELLED", "RESOLVED", "EXPIRED"):
            raise HTTPException(409, "Session is no longer active")
        guardian = db.execute("SELECT id FROM guardians WHERE id = ? AND session_id = ?", (payload.guardian_id, session["id"])).fetchone()
        if not guardian:
            raise HTTPException(403, "Guardian is not part of this session")
        db.execute("INSERT INTO guardian_locations(guardian_id,session_id,event_time,received_at,latitude,longitude,accuracy) VALUES(?,?,?,?,?,?,?)", (payload.guardian_id, session["id"], payload.event_time, now_iso(), payload.latitude, payload.longitude, payload.accuracy))
    return {"status": "recorded"}


@app.post("/api/sessions/{session_id}/heartbeat")
def heartbeat(session_id: str) -> dict[str, str]:
    check_timeouts()
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session["status"] in ("CANCELLED", "RESOLVED", "EXPIRED", "COMPLETED") or session["trip_status"] in ("COMPLETED", "CANCELLED", "INCIDENT"):
            raise HTTPException(409, "Session is no longer active")
        db.execute("UPDATE sessions SET last_heartbeat = ? WHERE id = ?", (now_iso(), session["id"]))
    return {"status": "heartbeat_recorded"}


@app.post("/api/sessions/{session_id}/telemetry")
def telemetry(session_id: str, payload: TelemetryIn) -> dict[str, Any]:
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        session_id = session["id"]
        if session["status"] in ("CANCELLED", "RESOLVED", "EXPIRED", "COMPLETED") or session["trip_status"] in ("COMPLETED", "CANCELLED", "INCIDENT"):
            raise HTTPException(409, "Session is no longer active")
        if session["trip_status"] == "PLANNED":
            raise HTTPException(409, "Trip has not started")
        existing = db.execute("SELECT id FROM telemetry WHERE event_id = ? OR (session_id = ? AND sequence = ?)", (payload.event_id, session_id, payload.sequence)).fetchone()
        if existing:
            return {"status": "duplicate_ignored", "id": existing["id"]}
        previous = db.execute("SELECT * FROM telemetry WHERE session_id = ? ORDER BY sequence DESC LIMIT 1", (session_id,)).fetchone()
        distance = 0.0 if not previous else haversine(previous["latitude"], previous["longitude"], payload.latitude, payload.longitude)
        derived_bearing = payload.bearing if payload.bearing is not None else (calculate_bearing(previous["latitude"], previous["longitude"], payload.latitude, payload.longitude) if previous else None)
        record = {"session_id": session_id, "event_id": payload.event_id, "sequence": payload.sequence, "event_time": payload.event_time, "received_at": now_iso(), "latitude": payload.latitude, "longitude": payload.longitude, "accuracy": payload.accuracy, "speed": payload.speed, "bearing": derived_bearing, "distance_m": round(distance, 2)}
        previous_hash = session["last_hash"]
        current_hash = hashlib.sha256((json.dumps(record, sort_keys=True, separators=(",", ":")) + previous_hash).encode()).hexdigest()
        db.execute("INSERT INTO telemetry(session_id,event_id,sequence,event_time,received_at,latitude,longitude,accuracy,speed,bearing,distance_m,previous_hash,current_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (*record.values(), previous_hash, current_hash))
        db.execute("UPDATE sessions SET last_hash = ?, last_heartbeat = ?, last_telemetry_epoch = ? WHERE id = ?", (current_hash, now_iso(), int(time.time()), session_id))
    return {"status": "recorded", "hash": current_hash, "distance_m": round(distance, 2)}


@app.post("/api/sessions/{session_id}/cancel")
def cancel(session_id: str, payload: PinIn) -> dict[str, str]:
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        session_id = session["id"]
        if payload.pin not in (session["safe_pin"], session["duress_pin"]):
            raise HTTPException(403, "Invalid PIN")
        target = "DISTRESS" if payload.pin == session["duress_pin"] else "CANCELLED"
        transition(db, session_id, session["status"], target, "Duress PIN received" if target == "DISTRESS" else "Safe PIN received")
        if target == "DISTRESS":
            make_alert(db, session_id, "Explicit duress signal")
        else:
            db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (now_iso(), session_id))
            if session["trip_status"] in ("PLANNED", "ACTIVE"):
                timestamp = now_iso()
                db.execute("UPDATE sessions SET trip_status = 'CANCELLED', trip_ended_at = ? WHERE id = ?", (timestamp, session_id))
                add_trip_event(db, session_id, "TRIP_CANCELLED", "CANCELLED", "Trip cancelled with safe PIN")
    return {"status": target}


@app.post("/api/alerts/{alert_id}/acknowledge")
def acknowledge(alert_id: str, payload: AckIn) -> dict[str, str]:
    with _db_lock, connect() as db:
        alert = db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if not alert:
            raise HTTPException(404, "Alert not found")
        if alert["status"] != "ALERTING":
            raise HTTPException(409, "Alert is already acknowledged or closed")
        if payload.responder_id not in {responder["id"] for responder in RESPONDERS}:
            raise HTTPException(400, "Unknown responder")
        timestamp = now_iso()
        db.execute("UPDATE alerts SET status = 'ACKNOWLEDGED', acknowledged_at = ?, acknowledged_by = ? WHERE id = ?", (timestamp, payload.responder_id, alert_id))
        db.execute("UPDATE sessions SET status = 'RESOLVED', ended_at = ? WHERE id = ? AND status = 'DISTRESS'", (timestamp, alert["session_id"]))
        db.execute("INSERT INTO state_events(session_id,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?)", (alert["session_id"], "DISTRESS", "RESOLVED", f"Acknowledged by {payload.responder_id}", timestamp))
    return {"status": "acknowledged"}


@app.get("/api/sessions/{session_id}/verify")
def verify(session_id: str) -> dict[str, Any]:
    with connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        session_id = session["id"]
        points = db.execute("SELECT * FROM telemetry WHERE session_id = ? ORDER BY sequence", (session_id,)).fetchall()
        previous_hash = "0" * 64
        valid = True
        for point in points:
            record = {key: point[key] for key in ("session_id", "event_id", "sequence", "event_time", "received_at", "latitude", "longitude", "accuracy", "speed", "bearing", "distance_m")}
            expected = hashlib.sha256((json.dumps(record, sort_keys=True, separators=(",", ":")) + previous_hash).encode()).hexdigest()
            valid = valid and expected == point["current_hash"] and point["previous_hash"] == previous_hash
            previous_hash = point["current_hash"]
        return {"valid": valid and previous_hash == session["last_hash"], "records": len(points)}


@app.get("/api/sessions/{session_id}/report")
def report(session_id: str) -> StreamingResponse:
    with connect() as db:
        data = session_payload(db, session_id)
    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    session = data["session"]
    alert = data["alerts"][0] if data["alerts"] else None
    story = [Paragraph("SENTINELTRACE INCIDENT REPORT", styles["Title"]), Spacer(1, 12), Paragraph(f"Session ID: {session['id']} | Status: {session['status']}", styles["Normal"]), Paragraph(f"Started: {session['started_at']} | Last heartbeat: {session['last_heartbeat'] or 'None'}", styles["Normal"]), Spacer(1, 12)]
    if alert:
        story += [Paragraph(f"Incident: {alert['id']} | Trigger: {alert['reason']}", styles["Normal"]), Paragraph(f"Alert status: {alert['status']} | Acknowledged by: {alert['acknowledged_by'] or 'No responder'}", styles["Normal"]), Spacer(1, 12)]
    rows = [["Seq", "Event time", "Latitude", "Longitude", "Speed", "Accuracy"]]
    rows += [[str(point["sequence"]), point["event_time"], f"{point['latitude']:.5f}", f"{point['longitude']:.5f}", f"{point['speed'] or 0:.1f}", f"{point['accuracy'] or 0:.1f}m"] for point in data["telemetry"]]
    table = Table(rows, repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#172b4d")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    story += [Paragraph("Telemetry trajectory", styles["Heading2"]), table, Spacer(1, 12), Paragraph(f"Hash-chain verification: {'PASS' if verify(session_id)['valid'] else 'FAIL'}", styles["Normal"])]
    document.build(story)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={session_id}-incident-report.pdf"})
