# SentinelTrace

A local prototype emergency-monitoring system for escort sessions. It records browser GPS telemetry, maintains a tamper-evident hash chain, detects missed heartbeats, escalates unacknowledged alerts across responders, and renders the known route on an interactive Leaflet map.

> SentinelTrace is a prototype. It cannot track a powered-off device, prove an attack, or replace official emergency infrastructure.

## Quick start

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0
```

On the computer, open http://127.0.0.1:8000. The local address is only for testing on that computer. For two devices anywhere on the internet, deploy the service to Render using the instructions below and use the single generated `https://...onrender.com` link on both devices.

### Two-device tracking demo

1. Open the deployed HTTPS link and click **New escort**.
2. Click **Copy Session ID**. It copies the complete session link, including the session token. Send that one link to the responder and open it on the tracking phone.
3. When the responder clicks **Join escort**, they enter their guardian name. That name appears on the shared session screen as “Name is inspecting this escort.”
4. On the phone, click **Use my location** and allow precise location permission. The phone sends browser GPS updates to the shared server; the responder page refreshes the map every 2.5 seconds.
5. Keep the tracking page visible and the phone awake for the most reliable updates. Mobile operating systems may pause browser JavaScript when a tab is backgrounded, the screen is locked, battery saving is enabled, or the browser is closed.

**Use my location** uses `watchPosition` with high accuracy and records the browser's reported latitude, longitude, speed, heading, and accuracy estimate. It cannot guarantee an exact position: GPS, Wi-Fi positioning, indoors, device settings, and permission choices affect the result. A secure HTTPS deployment is required; ordinary LAN HTTP URLs generally cannot use mobile geolocation. **Simulate route** is only test data.

## Demo path

1. Start an escort session with the default PINs (`1234` safe, `9999` duress).
2. On the tracking device, click **Use my location** or **Simulate route** and allow location access.
3. On the tracking device, click **Use my location** or **Simulate route**. Trigger **Duress** or stop the heartbeat and wait for the warning/grace timers.
4. Acknowledge the alert from the responder panel and download the incident PDF.

The default timeout values are intentionally short for local demos: warning after 12 seconds without contact, distress after a further 18 seconds. Override them with `SENTINEL_WARNING_SECONDS` and `SENTINEL_GRACE_SECONDS`.

## API highlights

- `POST /api/sessions` creates an escort session.
- `POST /api/sessions/{id}/join` registers a guardian name and returns the shared inspection list.
- `POST /api/sessions/{id}/telemetry` accepts idempotent, sequenced telemetry.
- `POST /api/sessions/{id}/heartbeat` refreshes expected communication.
- `POST /api/sessions/{id}/cancel` accepts a safe or duress PIN.
- `POST /api/alerts/{id}/acknowledge` records responder acknowledgement.
- `GET /api/sessions/{id}/report` returns a generated PDF.
- `GET /api/sessions/{id}/verify` verifies the telemetry hash chain.

Data is stored in `sentineltrace.db`. Email delivery is represented by an auditable outbox in this local prototype; SMTP can be connected at the notification boundary later.

## Deploy to Render for a phone demo

This project is a FastAPI application, not a Streamlit application. The included `render.yaml` configures a Render web service with the correct build and start commands. To deploy:

1. Push this repository, including `app/`, `static/`, `requirements.txt`, and `render.yaml`, to GitHub.
2. In Render, choose **New > Blueprint**, connect the repository, and deploy it.
3. Open the generated `https://` URL on the computer, create an escort, and share the resulting session link with the phone and responder.

The HTTPS URL should allow browser geolocation on supported mobile browsers without LAN or localhost restrictions. The free service uses `/tmp/sentineltrace.db`, so session data can disappear when the service restarts or is redeployed. Free instances can also sleep. Use this deployment for demonstrations with test data, not real emergency monitoring. For a real service, use a managed persistent database such as Render PostgreSQL, authentication and private session links, monitoring, a real notification provider, and a native/background-capable mobile client rather than relying only on a browser tab.
