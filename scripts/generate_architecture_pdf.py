from __future__ import annotations

import math
from pathlib import Path

from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "sentinel_architecture.pdf"


def draw_box(c, x, y, w, h, title, lines, fill, stroke=black):
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(1.5)
    c.rect(x, y, w, h, fill=1, stroke=1)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x + 12, y + h - 22, title)
    c.setFont("Helvetica", 10)
    for idx, line in enumerate(lines):
        c.drawString(x + 12, y + h - 48 - idx * 16, line)


def draw_arrow(c, x1, y1, x2, y2):
    c.setStrokeColor(HexColor("#1f2937"))
    c.setLineWidth(2)
    c.line(x1, y1, x2, y2)
    angle = math.atan2(y2 - y1, x2 - x1)
    arrow_size = 10
    c.line(x2, y2, x2 - arrow_size * math.cos(angle - math.pi / 6), y2 - arrow_size * math.sin(angle - math.pi / 6))
    c.line(x2, y2, x2 - arrow_size * math.cos(angle + math.pi / 6), y2 - arrow_size * math.sin(angle + math.pi / 6))


def draw_label(c, x, y, text):
    c.setFillColor(HexColor("#334155"))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, y, text)


def main() -> None:
    width, height = letter
    c = canvas.Canvas(str(OUTPUT_PATH), pagesize=letter)
    c.setTitle("SentinelTrace Architecture")
    c.setPageCompression(1)

    c.setFillColor(HexColor("#f8fafc"))
    c.rect(0, 0, width, height, fill=1, stroke=0)

    c.setFillColor(HexColor("#0f172a"))
    c.setFont("Helvetica-Bold", 24)
    c.drawString(42, 740, "SentinelTrace Architecture Overview")

    c.setFillColor(HexColor("#475569"))
    c.setFont("Helvetica", 11)
    c.drawString(44, 716, "Emergency escort tracking system for traveler safety, responder alerts, and incident reporting.")

    # Left: client / mobile devices
    draw_box(
        c,
        48,
        510,
        250,
        145,
        "Client / Mobile UI",
        [
            "- Browser-based traveler + guardian app",
            "- Geolocation API / watchPosition()",
            "- Leaflet map + UI controls",
            "- Session join, heartbeat, cancel, ack",
            "- Download PDF incident report"
        ],
        HexColor("#dbeafe"),
    )

    # Center: API server
    draw_box(
        c,
        360,
        430,
        380,
        250,
        "FastAPI Application",
        [
            "Endpoints: /api/sessions, /join, /telemetry, /heartbeat, /cancel",
            "- Session lifecycle management",
            "- Timeout and distress detection",
            "- Alert routing across responders",
            "- Hash-chain verification of telemetry",
            "- PDF generation using ReportLab",
            "- SQLite DB access via sqlite3"
        ],
        HexColor("#dcfce7"),
    )

    # Right: Data layer
    draw_box(
        c,
        780,
        510,
        240,
        160,
        "Persistence Layer",
        [
            "- sessions",
            "- telemetry",
            "- state_events",
            "- alerts",
            "- notifications",
            "- guardians / guardian_locations",
            "- SQLite database file"
        ],
        HexColor("#fef3c7"),
    )

    # Flow labels
    draw_label(c, 125, 675, "Traveler device")
    draw_label(c, 470, 688, "FastAPI backend")
    draw_label(c, 825, 685, "SQLite data store")

    # arrows
    draw_arrow(c, 298, 590, 360, 590)
    draw_arrow(c, 740, 590, 780, 590)

    # interactions / worker logic sub-box
    draw_box(
        c,
        90,
        300,
        220,
        120,
        "Core Logic",
        [
            "- Hash-chain integrity checks",
            "- Warning / distress transitions",
            "- Safe vs duress PIN rules",
            "- Responder acknowledgement flow"
        ],
        HexColor("#ede9fe"),
    )

    draw_box(
        c,
        370,
        240,
        220,
        120,
        "Security & Safety Rules",
        [
            "- unique telemetry sequences",
            "- idempotent event handling",
            "- heartbeat timeout thresholds",
            "- tamper-evident event chain"
        ],
        HexColor("#fae8ff"),
    )

    draw_box(
        c,
        640,
        300,
        180,
        100,
        "Reporting",
        [
            "- PDF report generation",
            "- incident status summary",
            "- route + event timeline"
        ],
        HexColor("#d1fae5"),
    )

    # connection arrows to logic
    draw_arrow(c, 310, 360, 370, 330)
    draw_arrow(c, 590, 330, 640, 330)
    draw_arrow(c, 470, 430, 470, 360)

    # Tech stack section
    c.setFillColor(HexColor("#0f172a"))
    c.setFont("Helvetica-Bold", 18)
    c.drawString(42, 200, "Tech Stack")

    tech_y = 160
    tech_items = [
        ("Frontend", "HTML, CSS, JavaScript, Leaflet, browser geolocation"),
        ("Backend", "Python 3, FastAPI, Pydantic, Uvicorn"),
        ("Database", "SQLite with session, telemetry, alert, guardian and audit tables"),
        ("Security", "Hash-chain verification, sequence checks, PIN validation, alert lifecycle"),
        ("Reporting", "ReportLab for PDF generation"),
        ("Deployment", "Render-ready app with uvicorn and static assets"),
    ]

    for idx, (label, description) in enumerate(tech_items):
        y = tech_y - idx * 28
        c.setFillColor(HexColor("#1e293b"))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(50, y, label + ":")
        c.setFont("Helvetica", 10)
        c.drawString(150, y, description)

    # How it works section
    c.setFont("Helvetica-Bold", 18)
    c.drawString(42, 70, "How the System Works")
    c.setFont("Helvetica", 10)
    steps = [
        "1. Traveler starts an escort session and receives a session ID link.",
        "2. Guardian joins the escort and can share location while both devices monitor the route.",
        "3. Browser GPS telemetry is sent to FastAPI with sequence numbers and timestamps.",
        "4. FastAPI validates sessions, stores telemetry, creates a hash chain, and updates state.",
        "5. If heartbeats stop, the system transitions from ACTIVE to WARNING to DISTRESS and creates alerts.",
        "6. Responders acknowledge alerts; the app resolves the incident and generates a PDF report."
    ]
    for idx, step in enumerate(steps):
        c.drawString(48, 48 - idx * 12, step)

    c.save()


if __name__ == "__main__":
    main()
    print(f"Architecture PDF generated at {OUTPUT_PATH}")
