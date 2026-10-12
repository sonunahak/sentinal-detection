# SentinelTrace 2.0 — Intelligent Escort & Anomaly Operations

**A Capstone B.Tech Major Project in Autonomous Safety Monitoring, Spatial Geofencing & Applied Digital Forensics**

SentinelTrace 2.0 is an intelligent real-time emergency monitoring and escort tracking platform designed for traveler personal safety. It integrates context-aware anomaly detection, safe-corridor geofencing, false-positive mitigation, mobile device sensor triggers (shake/fall detection), real-time emergency dispatch (Telegram Bot API & Webhooks), and cryptographically chained forensic audit trails.

---

## 🌟 Major Project Contributions & Algorithmic Highlights

### 1. Spatial Safe-Corridor Geofencing
- **Cross-Track Distance Calculation**: Computes the perpendicular geometric distance from the traveler's GPS coordinates to the nearest segment of a planned corridor polyline using equirectangular flat-earth projection.
- **Dynamic Tolerance Buffers**: Configurable buffer envelopes ($80\text{ m}$, $120\text{ m}$, $250\text{ m}$) tailored to travel mode (`WALKING`, `TRANSIT`, or `FREE_ROAM`).

### 2. Context-Aware Multi-Factor Threat & Anomaly Scoring
Calculates a real-time explainable **Sentinel Risk Index ($R \in [0, 100]$)** across 4 mathematical invariants:
$$R = \min\left(100,\; S_{\text{spatial}} + S_{\text{velocity}} + S_{\text{dwell}} + S_{\text{sensor}}\right)$$

- **Spatial Deviation Score ($S_{\text{spatial}} \in [0, 35]$)**: Scaled penalty for cross-track deviations exceeding the corridor buffer.
- **Velocity & Modality Invariant ($S_{\text{velocity}} \in [0, 25]$)**: Flags anomalous vehicular speeds ($> 24\text{ km/h}$) while in `WALKING` mode (detecting potential vehicle abduction or forced transit).
- **Stationary Dwell Invariant ($S_{\text{dwell}} \in [0, 20]$)**: Detects unexpected prolonged immobility in unverified non-destination locations.
- **Sensor Shock Invariant ($S_{\text{sensor}} \in [0, 40]$)**: Immediate spike upon violent phone shake or fall impact.

### 3. False-Positive Mitigation (Human-in-the-Loop Soft Check-In)
- Real-world safety systems frequently suffer from false alarms when travelers intentionally take a detour or catch a ride.
- **Two-Stage Dead-Man Escalation**:
  1. *Stage 1 (Soft Warning)*: On route departure, the mobile UI activates a 35-second check-in banner (*"Route divergence detected. Still safe?"*).
  2. *Intentional Detour Acknowledgment*: Tapping **"I'm OK"** logs an audited verification event, clears the active warning, and suppresses false sirens.
  3. *Stage 2 (Escalation)*: If unacknowledged within 35 seconds, the system escalates directly to `DISTRESS` and notifies responders.

### 4. Mobile Sensor Guard (Shake-to-Alert & Fall Detection)
- Utilizes the HTML5 `DeviceMotionEvent` API to monitor 3-axis accelerometer and gyroscope readings.
- **Panic Shake Gesture**: Detects rapid acceleration variance ($\Delta a > 22\text{ m/s}^2$ across 3 direction reversals within $1.4\text{ s}$).
- **Safety Interlock**: Triggers a 5-second countdown with haptic vibration (`navigator.vibrate`) allowing cancellation before dispatching emergency sirens.

### 5. Multi-Channel Emergency Dispatch
- Dispatches emergency alerts with live GPS coordinates, Google Maps links, and session URLs via the **Telegram Bot API** directly to guardians' smartphones.
- Outbox persistence in SQLite ensures tamper-resistant auditability.

### 6. Cryptographic Telemetry Chaining & Forensic PDF Reporting
- Sequential SHA-256 hash chaining:
  $$\text{Hash}_i = \text{SHA256}\left(\text{JSON}(\text{Telemetry}_i) \;\|\; \text{Hash}_{i-1}\right)$$
- Detects any retrospective database manipulation or coordinate tampering.
- Generates official forensic PDF incident reports with anomaly breakdown charts and cryptographic verification badges.

---

## 🚀 Quick Start

### Local Setup
```powershell
# 1. Create and activate virtual environment
py -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run test suite
python -m pytest

# 4. Start local development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
Open **http://127.0.0.1:8000** in your browser.

---

## 🌐 Deploy to Render (Free HTTPS Deployment)

Modern smartphones **strictly require an HTTPS link** for GPS geolocation and accelerometer access. Render provides instant free HTTPS hosting:

1. **Push to GitHub**:
   ```powershell
   git add .
   git commit -m "Upgrade to SentinelTrace 2.0 Major Project"
   git push origin main
   ```
2. **Deploy on Render**:
   - Go to [dashboard.render.com](https://dashboard.render.com) and click **New + > Blueprint**.
   - Select your repository (`sonunahak/sentinal-detection`).
   - Render reads `render.yaml` and deploys your service.
3. Open your generated `https://sentineltrace-xxxx.onrender.com` link on your laptop and smartphone!

---

## 🔬 Demonstration Script for Viva / Evaluators

| Step | Action | What to Tell the Evaluators |
| :--- | :--- | :--- |
| **1. Session Creation** | Click **+ New Escort**. Select **Walking** mode, click **Set Destination on Map**, and tap a point. | *"The system establishes a safe corridor with a dynamic 120m buffer and initializes the cryptographic SHA-256 genesis hash."* |
| **2. Normal Telemetry** | Click **Simulate on-path** or **Use my location**. | *"Telemetry is streamed and hashed. The Sentinel Risk Meter indicates 0% SAFE with all invariants satisfied."* |
| **3. False-Alarm Mitigation** | Click **Simulate detour**. Notice the yellow 35s banner appear. Click **I'm OK**. | *"Instead of immediately crying wolf, our two-stage architecture initiates a soft check-in. The traveler confirms the detour, mitigating false positives."* |
| **4. Sensor Panic Gesture** | Click **Simulate shake** (or shake your phone). Notice the 5s red countdown dialog and haptic buzz. | *"The device accelerometer detected sudden agitation. If not cancelled within 5 seconds, emergency distress activates automatically."* |
| **5. Escalation & Forensics** | Allow distress to trigger or click **Trigger duress**. Acknowledge via responder, then click **Download forensic report**. | *"Real-time alerts dispatch to Telegram/guardians. The generated PDF provides forensic incident records with cryptographic tamper verification."* |

---

## 📂 Architecture Overview

```text
sentinal-detection/
├── app/
│   ├── engine.py           # Cross-track corridor geometry & multi-factor anomaly scoring
│   ├── notifier.py         # Telegram Bot API & emergency alert dispatcher
│   ├── main.py             # FastAPI REST endpoints, state machine, and PDF reportlab
│   └── context.py          # Legacy escort context compatibility
├── static/
│   ├── index.html          # Sentinel operations dashboard, HUD, and modal controls
│   ├── app.js              # Leaflet GIS mapping, device motion sensor guard, telemetry client
│   └── styles.css          # Modern operations styling, risk meters, and panic animations
├── tests/
│   ├── test_api.py         # Base API integration & hash-chain verification tests
│   └── test_sentinel_major.py # Corridor geometry, mode invariants, panic & check-in tests
├── render.yaml             # Render cloud deployment blueprint
└── requirements.txt        # Production dependencies
```
