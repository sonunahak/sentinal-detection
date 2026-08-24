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

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("SENTINEL_DB", ROOT / "sentineltrace.db"))
WARNING_SECONDS = int(os.getenv("SENTINEL_WARNING_SECONDS", "12"))
GRACE_SECONDS = int(os.getenv("SENTINEL_GRACE_SECONDS", "18"))

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
            warning_at TEXT, distress_at TEXT, ended_at TEXT, last_hash TEXT NOT NULL
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
        """)


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
    return {"session": dict(session), "telemetry": [dict(point) for point in points], "events": [dict(event) for event in events], "alerts": [dict(alert) for alert in alerts], "responders": RESPONDERS}


def check_timeouts() -> None:
    with _db_lock, connect() as db:
        active = db.execute("SELECT * FROM sessions WHERE status IN ('ACTIVE','WARNING') AND last_heartbeat IS NOT NULL").fetchall()
        current_time = time.time()
        for session in active:
            age = current_time - datetime.fromisoformat(session["last_heartbeat"]).timestamp()
            if session["status"] == "ACTIVE" and age >= WARNING_SECONDS:
                transition(db, session["id"], "ACTIVE", "WARNING", "Heartbeat timeout; grace period started")
            elif session["status"] == "WARNING" and age >= WARNING_SECONDS + GRACE_SECONDS:
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
        db.execute("INSERT INTO sessions(id,status,safe_pin,duress_pin,started_at,last_hash) VALUES(?,?,?,?,?,?)", (session_id, "ACTIVE", payload.safe_pin, payload.duress_pin, timestamp, "0" * 64))
        db.execute("INSERT INTO state_events(session_id,to_status,reason,created_at) VALUES(?,?,?,?)", (session_id, "ACTIVE", "Escort session started", timestamp))
    return session_payload(connect(), session_id)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    check_timeouts()
    with connect() as db:
        return session_payload(db, session_id)


@app.post("/api/sessions/{session_id}/heartbeat")
def heartbeat(session_id: str) -> dict[str, str]:
    check_timeouts()
    with _db_lock, connect() as db:
        session = find_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session["status"] in ("CANCELLED", "RESOLVED", "EXPIRED"):
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
        if session["status"] in ("CANCELLED", "RESOLVED", "EXPIRED"):
            raise HTTPException(409, "Session is no longer active")
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
        db.execute("UPDATE sessions SET last_hash = ?, last_heartbeat = ? WHERE id = ?", (current_hash, now_iso(), session_id))
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
