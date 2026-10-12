from __future__ import annotations

import asyncio
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

from app.engine import calculate_corridor_deviation, evaluate_anomaly, haversine
from app.notifier import dispatch_emergency_alert

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("SENTINEL_DB", ROOT / "sentineltrace.db"))
HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv("SENTINEL_WARNING_SECONDS", "10"))
GRACE_PERIOD_SECONDS = int(os.getenv("SENTINEL_GRACE_SECONDS", "5"))
SOFT_CHECK_IN_TIMEOUT_SECONDS = 35

app = FastAPI(title="SentinelTrace - Intelligent Sentinel Escort System", version="2.0.0")
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
            last_telemetry_epoch INTEGER,
            travel_mode TEXT DEFAULT 'WALKING',
            destination_name TEXT,
            destination_lat REAL,
            destination_lon REAL,
            corridor_tolerance_m REAL DEFAULT 100.0,
            planned_corridor TEXT,
            current_risk_score INTEGER DEFAULT 0,
            risk_breakdown TEXT,
            telegram_chat_id TEXT,
            soft_warning_at TEXT,
            is_check_in_acknowledged INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE, sequence INTEGER NOT NULL, event_time TEXT NOT NULL,
            received_at TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            accuracy REAL, speed REAL, bearing REAL, distance_m REAL NOT NULL,
            previous_hash TEXT NOT NULL, current_hash TEXT NOT NULL,
            risk_score INTEGER DEFAULT 0,
            anomaly_flags TEXT,
            trigger_type TEXT DEFAULT 'NORMAL',
            deviation_m REAL DEFAULT 0.0,
            UNIQUE(session_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS state_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            from_status TEXT, to_status TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
            reason TEXT NOT NULL, created_at TEXT NOT NULL, acknowledged_at TEXT,
            acknowledged_by TEXT, current_responder INTEGER NOT NULL DEFAULT 0,
            risk_score INTEGER DEFAULT 0,
            dispatched_channels TEXT
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
        """)
        # Safe schema migration for any preexisting database files
        session_cols = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        telemetry_cols = {row["name"] for row in db.execute("PRAGMA table_info(telemetry)")}
        alert_cols = {row["name"] for row in db.execute("PRAGMA table_info(alerts)")}

        if "last_telemetry_epoch" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN last_telemetry_epoch INTEGER")
        if "travel_mode" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN travel_mode TEXT DEFAULT 'WALKING'")
        if "destination_name" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN destination_name TEXT")
        if "destination_lat" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN destination_lat REAL")
        if "destination_lon" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN destination_lon REAL")
        if "corridor_tolerance_m" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN corridor_tolerance_m REAL DEFAULT 100.0")
        if "planned_corridor" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN planned_corridor TEXT")
        if "current_risk_score" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN current_risk_score INTEGER DEFAULT 0")
        if "risk_breakdown" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN risk_breakdown TEXT")
        if "telegram_chat_id" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN telegram_chat_id TEXT")
        if "soft_warning_at" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN soft_warning_at TEXT")
        if "is_check_in_acknowledged" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN is_check_in_acknowledged INTEGER DEFAULT 0")

        if "risk_score" not in telemetry_cols:
            db.execute("ALTER TABLE telemetry ADD COLUMN risk_score INTEGER DEFAULT 0")
        if "anomaly_flags" not in telemetry_cols:
            db.execute("ALTER TABLE telemetry ADD COLUMN anomaly_flags TEXT")
        if "trigger_type" not in telemetry_cols:
            db.execute("ALTER TABLE telemetry ADD COLUMN trigger_type TEXT DEFAULT 'NORMAL'")
        if "deviation_m" not in telemetry_cols:
            db.execute("ALTER TABLE telemetry ADD COLUMN deviation_m REAL DEFAULT 0.0")

        if "risk_score" not in alert_cols:
            db.execute("ALTER TABLE alerts ADD COLUMN risk_score INTEGER DEFAULT 0")
        if "dispatched_channels" not in alert_cols:
            db.execute("ALTER TABLE alerts ADD COLUMN dispatched_channels TEXT")

        db.execute(
            "UPDATE sessions SET last_telemetry_epoch = CAST(strftime('%s', last_heartbeat) AS INTEGER) "
            "WHERE last_telemetry_epoch IS NULL AND last_heartbeat IS NOT NULL"
        )


init_db()


class SessionCreate(BaseModel):
    duration_minutes: int = Field(default=15, ge=1, le=240)
    safe_pin: str = Field(default="1234", min_length=4, max_length=12)
    duress_pin: str = Field(default="9999", min_length=4, max_length=12)
    travel_mode: str = Field(default="WALKING")
    destination_name: str | None = None
    destination_lat: float | None = None
    destination_lon: float | None = None
    corridor_tolerance_m: float = Field(default=100.0, ge=10.0, le=2000.0)
    planned_corridor: list[list[float]] | None = None
    telegram_chat_id: str | None = None


class TelemetryIn(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sequence: int = Field(ge=0)
    event_time: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    speed: float | None = Field(default=None, ge=0)
    bearing: float | None = Field(default=None, ge=0, lt=360)
    trigger_type: str = Field(default="NORMAL")
    sensor_accel_peak: float | None = Field(default=None, ge=0)


class PinIn(BaseModel):
    pin: str


class AckIn(BaseModel):
    responder_id: str


class CheckInIn(BaseModel):
    acknowledged: bool = True
    reason: str = Field(default="Intentional detour / Traveler confirmed safe")


class PanicIn(BaseModel):
    trigger_type: str = Field(default="SHAKE_PANIC")
    sensor_accel_peak: float | None = None
    latitude: float | None = None
    longitude: float | None = None


class GuardianJoin(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class GuardianLocationIn(BaseModel):
    guardian_id: int = Field(ge=1)
    event_time: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)


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
    db.execute(
        "UPDATE sessions SET status = ?, "
        "warning_at = CASE WHEN ? = 'WARNING' THEN ? ELSE warning_at END, "
        "distress_at = CASE WHEN ? = 'DISTRESS' THEN ? ELSE distress_at END "
        "WHERE id = ?",
        (target, target, timestamp, target, timestamp, session_id),
    )
    db.execute(
        "INSERT INTO state_events(session_id, from_status, to_status, reason, created_at) VALUES(?,?,?,?,?)",
        (session_id, current, target, reason, timestamp),
    )


def make_alert(
    db: sqlite3.Connection,
    session_id: str,
    reason: str,
    risk_score: int = 0,
    latitude: float | None = None,
    longitude: float | None = None,
) -> str:
    alert_id = f"ST-{uuid.uuid4().hex[:8].upper()}"
    timestamp = now_iso()
    session = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    chat_id = session["telegram_chat_id"] if session and "telegram_chat_id" in session.keys() else None

    db.execute(
        "INSERT INTO alerts(id, session_id, status, reason, created_at, risk_score, dispatched_channels) "
        "VALUES(?,?,?,?,?,?,?)",
        (alert_id, session_id, "ALERTING", reason, timestamp, risk_score, "OUTBOX,TELEGRAM"),
    )
    notify_responder(db, alert_id, 0)

    # Trigger background alert dispatch without blocking response
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            dispatch_emergency_alert(
                session_id=session_id,
                reason=reason,
                risk_score=risk_score,
                latitude=latitude,
                longitude=longitude,
                chat_id=chat_id,
            )
        )
    except RuntimeError:
        pass

    return alert_id


def notify_responder(db: sqlite3.Connection, alert_id: str, index: int) -> None:
    if index >= len(RESPONDERS):
        db.execute("UPDATE alerts SET status = 'UNACKNOWLEDGED' WHERE id = ?", (alert_id,))
        return
    db.execute("UPDATE alerts SET current_responder = ? WHERE id = ?", (index, alert_id))
    db.execute(
        "INSERT INTO notifications(alert_id, responder_id, status, attempted_at) VALUES(?,?,?,?)",
        (alert_id, RESPONDERS[index]["id"], "RECORDED_IN_OUTBOX", now_iso()),
    )


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

    planned_corridor = json.loads(session["planned_corridor"]) if session["planned_corridor"] else []
    risk_breakdown = json.loads(session["risk_breakdown"]) if session["risk_breakdown"] else {}

    # Calculate soft check-in countdown
    soft_warning_remaining = None
    if session["soft_warning_at"] and not session["is_check_in_acknowledged"]:
        soft_epoch = int(datetime.fromisoformat(session["soft_warning_at"]).timestamp())
        elapsed = int(time.time()) - soft_epoch
        soft_warning_remaining = max(0, SOFT_CHECK_IN_TIMEOUT_SECONDS - elapsed)

    return {
        "session": {
            **dict(session),
            "planned_corridor": planned_corridor,
            "risk_breakdown": risk_breakdown,
            "soft_warning_remaining": soft_warning_remaining,
        },
        "telemetry": [
            {
                **dict(point),
                "anomaly_flags": json.loads(point["anomaly_flags"]) if point["anomaly_flags"] else [],
            }
            for point in points
        ],
        "events": [dict(event) for event in events],
        "alerts": [dict(alert) for alert in alerts],
        "responders": RESPONDERS,
        "guardians": [dict(guardian) for guardian in guardians],
        "guardian_locations": [dict(location) for location in guardian_locations],
    }


def check_timeouts() -> None:
    with _db_lock, connect() as db:
        active = db.execute(
            "SELECT * FROM sessions WHERE status IN ('ACTIVE', 'WARNING') AND last_telemetry_epoch IS NOT NULL"
        ).fetchall()
        current_epoch = int(time.time())
        for session in active:
            elapsed = current_epoch - session["last_telemetry_epoch"]
            sid = session["id"]

            # 1. Soft check-in timeout verification (Two-stage off-route escalation)
            if session["soft_warning_at"] and not session["is_check_in_acknowledged"]:
                soft_epoch = int(datetime.fromisoformat(session["soft_warning_at"]).timestamp())
                if (current_epoch - soft_epoch) > SOFT_CHECK_IN_TIMEOUT_SECONDS:
                    transition(db, sid, session["status"], "DISTRESS", "Unacknowledged safe-corridor divergence")
                    make_alert(db, sid, "Unacknowledged safe-corridor divergence: Traveler off route", risk_score=85)
                    db.execute("UPDATE sessions SET soft_warning_at = NULL WHERE id = ?", (sid,))
                    continue

            # 2. Heartbeat dead-man switch
            if session["status"] == "ACTIVE" and elapsed > HEARTBEAT_TIMEOUT_SECONDS + GRACE_PERIOD_SECONDS:
                transition(db, sid, "ACTIVE", "DISTRESS", "Heartbeat timeout expired: signal lost")
                make_alert(db, sid, "Heartbeat timeout expired: signal lost", risk_score=90)
            elif session["status"] == "ACTIVE" and elapsed > HEARTBEAT_TIMEOUT_SECONDS:
                transition(db, sid, "ACTIVE", "WARNING", "Heartbeat timeout; grace period started")
            elif session["status"] == "WARNING" and elapsed > HEARTBEAT_TIMEOUT_SECONDS + GRACE_PERIOD_SECONDS:
                transition(db, sid, "WARNING", "DISTRESS", "Heartbeat grace period expired")
                make_alert(db, sid, "Unexpected telemetry loss / possible distress", risk_score=95)


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
    corridor_json = json.dumps(payload.planned_corridor) if payload.planned_corridor else None

    with _db_lock, connect() as db:
        db.execute(
            "INSERT INTO sessions("
            "  id, status, safe_pin, duress_pin, started_at, last_hash, last_telemetry_epoch, "
            "  travel_mode, destination_name, destination_lat, destination_lon, corridor_tolerance_m, "
            "  planned_corridor, current_risk_score, telegram_chat_id"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                "ACTIVE",
                payload.safe_pin,
                payload.duress_pin,
                timestamp,
                "0" * 64,
                None,
                payload.travel_mode.upper(),
                payload.destination_name,
                payload.destination_lat,
                payload.destination_lon,
                payload.corridor_tolerance_m,
                corridor_json,
                0,
                payload.telegram_chat_id,
            ),
        )
        db.execute(
            "INSERT INTO state_events(session_id, to_status, reason, created_at) VALUES(?,?,?,?)",
            (
                session_id,
                "ACTIVE",
                f"Escort session started ({payload.travel_mode.upper()} mode)",
                timestamp,
            ),
        )
    return session_payload(connect(), session_id)


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
        if session["status"] in ("CANCELLED", "RESOLVED", "EXPIRED", "COMPLETED"):
            raise HTTPException(409, "Session is no longer active")
        db.execute("INSERT INTO guardians(session_id, name, joined_at) VALUES(?,?,?)", (session["id"], name, now_iso()))
        return session_payload(db, session["id"])


@app.post("/api/sessions/{session_id}/guardian-location")
def guardian_location(session_id: str, payload: GuardianLocationIn) -> dict[str, str]:
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session["status"] in ("CANCELLED", "RESOLVED", "EXPIRED", "COMPLETED"):
            raise HTTPException(409, "Session is no longer active")
        guardian = db.execute("SELECT id FROM guardians WHERE id = ? AND session_id = ?", (payload.guardian_id, session["id"])).fetchone()
        if not guardian:
            raise HTTPException(403, "Guardian is not part of this session")
        db.execute(
            "INSERT INTO guardian_locations(guardian_id,session_id,event_time,received_at,latitude,longitude,accuracy) VALUES(?,?,?,?,?,?,?)",
            (payload.guardian_id, session["id"], payload.event_time, now_iso(), payload.latitude, payload.longitude, payload.accuracy),
        )
    return {"status": "recorded"}


@app.post("/api/sessions/{session_id}/heartbeat")
def heartbeat(session_id: str) -> dict[str, str]:
    check_timeouts()
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session["status"] in ("CANCELLED", "RESOLVED", "EXPIRED", "COMPLETED"):
            raise HTTPException(409, "Session is no longer active")
        db.execute("UPDATE sessions SET last_heartbeat = ? WHERE id = ?", (now_iso(), session["id"]))
    return {"status": "heartbeat_recorded"}


@app.post("/api/sessions/{session_id}/check-in")
def check_in_detour(session_id: str, payload: CheckInIn) -> dict[str, Any]:
    """
    Mitigates false positives: Traveler confirms intentional detour or ride.
    Clears active soft warning and registers audit acknowledgment.
    """
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        sid = session["id"]
        timestamp = now_iso()
        db.execute(
            "UPDATE sessions SET is_check_in_acknowledged = 1, soft_warning_at = NULL WHERE id = ?",
            (sid,),
        )
        db.execute(
            "INSERT INTO state_events(session_id, from_status, to_status, reason, created_at) VALUES(?,?,?,?,?)",
            (sid, session["status"], session["status"], f"Check-in: {payload.reason}", timestamp),
        )
    return {"status": "acknowledged", "message": "Detour verified as safe"}


@app.post("/api/sessions/{session_id}/panic")
def trigger_panic(session_id: str, payload: PanicIn) -> dict[str, Any]:
    """
    Immediate emergency trigger activated by phone shake, fall impact, or panic gesture.
    """
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        sid = session["id"]
        reason = f"Emergency Alert: {payload.trigger_type.replace('_', ' ').title()}"
        transition(db, sid, session["status"], "DISTRESS", reason)
        alert_id = make_alert(
            db,
            sid,
            reason,
            risk_score=100,
            latitude=payload.latitude,
            longitude=payload.longitude,
        )
    return {"status": "DISTRESS", "alert_id": alert_id, "trigger": payload.trigger_type}


@app.post("/api/sessions/{session_id}/telemetry")
def telemetry(session_id: str, payload: TelemetryIn) -> dict[str, Any]:
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        session_id = session["id"]
        if session["status"] in ("CANCELLED", "RESOLVED", "EXPIRED", "COMPLETED"):
            raise HTTPException(409, "Session is no longer active")

        existing = db.execute(
            "SELECT id FROM telemetry WHERE event_id = ? OR (session_id = ? AND sequence = ?)",
            (payload.event_id, session_id, payload.sequence),
        ).fetchone()
        if existing:
            return {"status": "duplicate_ignored", "id": existing["id"]}

        previous = db.execute("SELECT * FROM telemetry WHERE session_id = ? ORDER BY sequence DESC LIMIT 1", (session_id,)).fetchone()
        distance = 0.0 if not previous else haversine(previous["latitude"], previous["longitude"], payload.latitude, payload.longitude)
        derived_bearing = payload.bearing if payload.bearing is not None else (
            calculate_bearing(previous["latitude"], previous["longitude"], payload.latitude, payload.longitude) if previous else None
        )

        # 1. Anomaly & Geofence Evaluation
        corridor = json.loads(session["planned_corridor"]) if session["planned_corridor"] else None
        corridor_tol = session["corridor_tolerance_m"] or 100.0
        mode = session["travel_mode"] or "WALKING"
        is_ack = bool(session["is_check_in_acknowledged"])

        # Calculate dwell time in seconds if stationary
        dwell_seconds = 0
        if previous and distance < 5.0 and payload.speed is not None and payload.speed < 0.5:
            try:
                prev_time = datetime.fromisoformat(previous["event_time"]).timestamp()
                curr_time = datetime.fromisoformat(payload.event_time).timestamp()
                dwell_seconds = int(max(0.0, curr_time - prev_time))
            except Exception:
                pass

        anomaly_eval = evaluate_anomaly(
            travel_mode=mode,
            current_lat=payload.latitude,
            current_lon=payload.longitude,
            speed_mps=payload.speed,
            corridor=corridor,
            corridor_tolerance_m=corridor_tol,
            dwell_seconds=dwell_seconds,
            trigger_type=payload.trigger_type,
            sensor_accel_peak=payload.sensor_accel_peak,
            is_check_in_acknowledged=is_ack,
        )

        risk_score = anomaly_eval["risk_score"]
        flags = anomaly_eval["anomaly_flags"]
        deviation_m = anomaly_eval["breakdown"]["deviation_m"]

        # 2. Check for Direct Sensor Panic Triggers
        if payload.trigger_type in ("SHAKE_PANIC", "FALL_DETECTED"):
            reason = f"Immediate Sensor Distress: {payload.trigger_type}"
            transition(db, session_id, session["status"], "DISTRESS", reason)
            make_alert(db, session_id, reason, risk_score=100, latitude=payload.latitude, longitude=payload.longitude)

        # 3. Soft Warning Trigger (Off-route detected, starts 35s check-in timer)
        elif "OFF_ROUTE_DEVIATION" in flags and not is_ack and session["soft_warning_at"] is None:
            db.execute("UPDATE sessions SET soft_warning_at = ? WHERE id = ?", (now_iso(), session_id))
            db.execute(
                "INSERT INTO state_events(session_id, from_status, to_status, reason, created_at) VALUES(?,?,?,?,?)",
                (session_id, session["status"], session["status"], f"Soft check-in prompted: {deviation_m:.1f}m off corridor", now_iso()),
            )

        # 4. Cryptographic Hash-Chain Computation (strictly backward-compatible record structure)
        record = {
            "session_id": session_id,
            "event_id": payload.event_id,
            "sequence": payload.sequence,
            "event_time": payload.event_time,
            "received_at": now_iso(),
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "accuracy": payload.accuracy,
            "speed": payload.speed,
            "bearing": derived_bearing,
            "distance_m": round(distance, 2),
        }
        previous_hash = session["last_hash"]
        current_hash = hashlib.sha256(
            (json.dumps(record, sort_keys=True, separators=(",", ":")) + previous_hash).encode()
        ).hexdigest()

        db.execute(
            "INSERT INTO telemetry("
            "  session_id, event_id, sequence, event_time, received_at, latitude, longitude, "
            "  accuracy, speed, bearing, distance_m, previous_hash, current_hash, "
            "  risk_score, anomaly_flags, trigger_type, deviation_m"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *record.values(),
                previous_hash,
                current_hash,
                risk_score,
                json.dumps(flags),
                payload.trigger_type,
                deviation_m,
            ),
        )

        db.execute(
            "UPDATE sessions SET last_hash = ?, last_heartbeat = ?, last_telemetry_epoch = ?, "
            "current_risk_score = ?, risk_breakdown = ? WHERE id = ?",
            (
                current_hash,
                now_iso(),
                int(time.time()),
                risk_score,
                json.dumps(anomaly_eval["breakdown"]),
                session_id,
            ),
        )

    return {
        "status": "recorded",
        "hash": current_hash,
        "distance_m": round(distance, 2),
        "risk_score": risk_score,
        "risk_level": anomaly_eval["risk_level"],
        "anomaly_flags": flags,
        "deviation_m": deviation_m,
    }


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
        transition(
            db,
            session_id,
            session["status"],
            target,
            "Duress PIN received" if target == "DISTRESS" else "Safe PIN received",
        )
        if target == "DISTRESS":
            make_alert(db, session_id, "Explicit duress signal", risk_score=100)
        else:
            db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (now_iso(), session_id))
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
        db.execute(
            "UPDATE alerts SET status = 'ACKNOWLEDGED', acknowledged_at = ?, acknowledged_by = ? WHERE id = ?",
            (timestamp, payload.responder_id, alert_id),
        )
        db.execute(
            "UPDATE sessions SET status = 'RESOLVED', ended_at = ? WHERE id = ? AND status = 'DISTRESS'",
            (timestamp, alert["session_id"]),
        )
        db.execute(
            "INSERT INTO state_events(session_id,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?)",
            (alert["session_id"], "DISTRESS", "RESOLVED", f"Acknowledged by {payload.responder_id}", timestamp),
        )
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
            record = {
                key: point[key]
                for key in (
                    "session_id",
                    "event_id",
                    "sequence",
                    "event_time",
                    "received_at",
                    "latitude",
                    "longitude",
                    "accuracy",
                    "speed",
                    "bearing",
                    "distance_m",
                )
            }
            expected = hashlib.sha256(
                (json.dumps(record, sort_keys=True, separators=(",", ":")) + previous_hash).encode()
            ).hexdigest()
            valid = valid and expected == point["current_hash"] and point["previous_hash"] == previous_hash
            previous_hash = point["current_hash"]
        return {"valid": valid and previous_hash == session["last_hash"], "records": len(points)}


@app.get("/api/sessions/{session_id}/report")
def report(session_id: str) -> StreamingResponse:
    with connect() as db:
        data = session_payload(db, session_id)
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )
    styles = getSampleStyleSheet()
    session = data["session"]
    alert = data["alerts"][0] if data["alerts"] else None
    risk_score = session.get("current_risk_score", 0)

    story = [
        Paragraph("SENTINELTRACE FORENSIC INCIDENT & ANOMALY REPORT", styles["Title"]),
        Spacer(1, 10),
        Paragraph(
            f"<b>Session ID:</b> {session['id']} | <b>Status:</b> {session['status']} | "
            f"<b>Travel Mode:</b> {session.get('travel_mode', 'WALKING')}",
            styles["Normal"],
        ),
        Paragraph(
            f"<b>Started:</b> {session['started_at']} | <b>Last Heartbeat:</b> {session['last_heartbeat'] or 'None'} | "
            f"<b>Peak Risk Index:</b> {risk_score}%",
            styles["Normal"],
        ),
        Spacer(1, 10),
    ]

    if alert:
        story += [
            Paragraph(
                f"<b>Incident:</b> {alert['id']} | <b>Trigger:</b> {alert['reason']}",
                styles["Normal"],
            ),
            Paragraph(
                f"<b>Alert Status:</b> {alert['status']} | <b>Acknowledged by:</b> {alert['acknowledged_by'] or 'Unacknowledged'}",
                styles["Normal"],
            ),
            Spacer(1, 10),
        ]

    # Geofence & Anomaly Assessment Summary
    dest_name = session.get("destination_name") or "None designated (Free Roam)"
    corridor_tol = session.get("corridor_tolerance_m", 100.0)
    story += [
        Paragraph("Sentinel Threat & Corridor Assessment", styles["Heading2"]),
        Paragraph(
            f"<b>Destination:</b> {dest_name} | <b>Corridor Tolerance:</b> {corridor_tol:.0f}m | "
            f"<b>Detour Mitigation:</b> {'Acknowledged by user' if session.get('is_check_in_acknowledged') else 'Strict'}",
            styles["Normal"],
        ),
        Spacer(1, 10),
    ]

    rows = [["Seq", "Time", "Latitude", "Longitude", "Speed", "Dev (m)", "Risk", "Flags"]]
    for point in data["telemetry"]:
        flags_str = ", ".join(point.get("anomaly_flags", [])) or "NORMAL"
        rows.append([
            str(point["sequence"]),
            point["event_time"][-8:],
            f"{point['latitude']:.5f}",
            f"{point['longitude']:.5f}",
            f"{(point['speed'] or 0) * 3.6:.1f}kph",
            f"{point.get('deviation_m', 0.0):.0f}m",
            f"{point.get('risk_score', 0)}%",
            flags_str[:22],
        ])

    table = Table(rows, repeatRows=1)
    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#172b4d")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ])
    )
    story += [
        Paragraph("Cryptographic Telemetry Audit Trail", styles["Heading2"]),
        table,
        Spacer(1, 12),
        Paragraph(
            f"<b>SHA-256 Hash-chain Verification:</b> "
            f"<font color='{'green' if verify(session_id)['valid'] else 'red'}'>"
            f"{'PASS (TAMPER-FREE)' if verify(session_id)['valid'] else 'FAIL (HASH MISMATCH)'}</font>",
            styles["Normal"],
        ),
    ]

    document.build(story)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={session_id}-incident-report.pdf"},
    )
