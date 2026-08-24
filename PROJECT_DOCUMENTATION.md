# SentinelTrace Project Documentation

## 1. Project Overview

SentinelTrace is a browser-based emergency-monitoring prototype for an escorted walk or travel session. A person being monitored, called the **traveler**, shares a session link with a **guardian** or responder. The traveler can send GPS telemetry, send heartbeats, end the session with a safe PIN, or trigger distress with a duress PIN. The guardian watches the same session, sees the route on a Leaflet map, observes status changes, acknowledges alerts, verifies the telemetry hash chain, and downloads a PDF incident report.

The project is intentionally a prototype. It is designed to demonstrate the workflow and technical ideas, not to provide production-grade emergency response. It cannot track a powered-off device, guarantee GPS accuracy, prove that an attack occurred, or replace emergency services.

## 2. Main Capabilities

- Create an escort session with safe and duress PINs.
- Share a session URL containing the session identifier.
- Join a session as a guardian using a full ID, ID prefix, token, or shared link.
- Record browser GPS coordinates and related measurements.
- Simulate a route for demonstrations.
- Calculate distance between telemetry points using the Haversine formula.
- Derive a bearing when the browser does not provide heading data.
- Maintain a SHA-256 hash chain over telemetry records.
- Ignore duplicate telemetry events safely.
- Detect a missing telemetry signal through warning and distress states.
- Create an alert when distress is explicit or telemetry is lost.
- Represent responder notifications in an auditable local outbox.
- Accept one responder acknowledgement.
- Generate and download a PDF incident report.
- Verify whether stored telemetry still matches its hash chain.

## 3. Technology Stack

### Backend

- **Python**: application language.
- **FastAPI**: HTTP API framework and request validation.
- **Uvicorn**: ASGI server used to run the application.
- **Pydantic**: validates JSON request bodies through FastAPI models.
- **SQLite**: local relational database stored in `sentineltrace.db` or the path in `SENTINEL_DB`.
- **ReportLab**: creates PDF incident reports.
- **Standard library**: hashing, JSON serialization, UUID generation, timestamps, geometry calculations, threading, and SQLite access.

### Frontend

- Plain HTML, CSS, and JavaScript.
- Leaflet 1.9.4 for the interactive map.
- OpenStreetMap tiles for map imagery.
- Google Fonts for `DM Sans` and `Space Grotesk`.
- Browser Geolocation API for live GPS data.
- Fetch API for communication with the FastAPI backend.

### Deployment and testing

- Render Blueprint configuration in `render.yaml`.
- Pytest and FastAPI `TestClient` in `tests/test_api.py`.
- Dependencies are pinned in `requirements.txt`.

## 4. Repository Layout

```text
README.md                    User-facing quick start and deployment notes
PROJECT_DOCUMENTATION.md     Detailed architecture and study document
render.yaml                  Render web-service deployment definition
requirements.txt             Python runtime and test dependencies
app/
  main.py                    FastAPI application, database, domain logic, and PDF report
static/
  index.html                 Page structure and controls
  app.js                     Browser state, API calls, polling, GPS, and rendering
  styles.css                 Visual design and responsive layout
tests/
  test_api.py                API and domain behavior tests
```

## 5. How the Application Starts

The application is launched with:

```powershell
uvicorn app.main:app --reload --host 0.0.0.0
```

`app.main:app` means:

1. Python imports the `app` package and the `main` module.
2. The module creates the FastAPI object named `app`.
3. `StaticFiles` exposes the `static` directory at `/static`.
4. `init_db()` runs during import and creates or upgrades the SQLite schema.
5. Uvicorn serves the FastAPI application.

The root route `/` returns `static/index.html`. The HTML then loads Leaflet, `/static/styles.css`, and `/static/app.js`.

The import-time database initialization is convenient for a prototype, but production applications usually use explicit migrations and startup lifecycle hooks.

## 6. End-to-End Architecture

The system has four practical layers:

```text
Browser UI
  index.html + styles.css + app.js
        |
        | HTTP JSON requests, polling, PDF download
        v
FastAPI application
  routes in app/main.py
        |
        | validation, state transitions, calculations, hashing
        v
SQLite database
  sessions, telemetry, state_events, alerts,
  notifications, guardians
        |
        | report query / verification
        v
PDF response
```

External services used by the browser are separate from the backend:

```text
Browser -> OpenStreetMap tile servers through Leaflet
Browser -> Browser Geolocation API
Browser -> SentinelTrace FastAPI service
```

Leaflet and OpenStreetMap render the map but do not store SentinelTrace session data. The browser obtains GPS coordinates locally and sends them to the FastAPI service. The server is the source of truth for session status, telemetry, alerts, and audit data.

## 7. User Roles and Session Model

### Traveler

The traveler starts or opens a session and normally sees signal controls. They can:

- enable live location;
- run simulated telemetry;
- send a manual heartbeat;
- submit the safe PIN to stop normally;
- submit the duress PIN to create a distress alert.

### Guardian

The guardian joins using a shared session link or identifier. The guardian sees the same session data but the signal controls are hidden. The guardian can acknowledge the currently active responder alert and download the report.

The role is a browser-side display role, not a server-enforced identity. The backend does not authenticate a browser as traveler or guardian. The `guardians` table records the entered display name, but the API does not use it as an authorization credential.

### Session statuses

The important session statuses are:

- `ACTIVE`: session is operating normally.
- `WARNING`: telemetry has not arrived within the timeout threshold and the grace period is running.
- `DISTRESS`: the duress PIN was used or the grace period expired.
- `CANCELLED`: the safe PIN ended the session.
- `RESOLVED`: a responder acknowledged a distress alert.
- `EXPIRED`: represented in the schema and client logic, but not currently assigned by the backend.

A state transition is written both to `sessions.status` and to the `state_events` audit table.

## 8. Complete User Flow

### 8.1 Create a session

1. The user clicks **New escort**.
2. `app.js` reads duration, safe PIN, and duress PIN from the modal.
3. The browser sends `POST /api/sessions`.
4. FastAPI validates the body using `SessionCreate`.
5. The backend rejects equal safe and duress PINs.
6. A session ID such as `session-a1b2c3d4e5` is generated.
7. A row is inserted into `sessions` with status `ACTIVE` and an initial all-zero hash.
8. An initial `ACTIVE` row is inserted into `state_events`.
9. The API returns the complete session payload.
10. The browser puts `?session=<session-id>` in the URL without reloading the page and starts polling and heartbeat timers.

### 8.2 Share and join

The copy button copies the current page URL, including the session query parameter. A guardian clicks **Join escort**, pastes the URL or ID, and enters a name.

`app.js` extracts the `session` query parameter when a URL is supplied and calls:

```text
POST /api/sessions/{session_id}/join
Body: { "name": "Alex Morgan" }
```

The server trims repeated whitespace, rejects an empty name, stores the guardian, and returns the current session payload. The guardian browser then starts the same refresh timers but hides traveler controls.

The backend's `find_session()` supports:

- an exact session ID;
- a prefix of the ID;
- the ten-character token without the `session-` prefix;
- a value beginning with `session-` that matches the stored ID.

This is useful for a demo, but prefix matching is not suitable as the only access mechanism for sensitive sessions.

### 8.3 Send live GPS telemetry

When the traveler clicks **Use my location**:

1. The browser checks `window.isSecureContext`.
2. It checks whether `navigator.geolocation` exists.
3. It calls `watchPosition()` with high accuracy enabled.
4. Each position callback creates a telemetry request.
5. The browser sends latitude, longitude, accuracy, speed, heading, event time, sequence, and event ID.
6. The server validates coordinate ranges and numeric constraints.
7. The server calculates distance from the previous point.
8. The server uses browser heading or derives a bearing from two coordinates.
9. The server creates the next hash-chain record.
10. The telemetry row is stored and the session heartbeat fields are updated.
11. The browser refreshes the session view.

Geolocation requires a secure context, normally HTTPS. Mobile browsers may stop or delay JavaScript when the page is backgrounded, the device is locked, the browser is closed, or battery saving is enabled.

### 8.4 Simulate a route

Simulation first requests the browser's current location. It then creates six nearby waypoints by adding latitude and longitude increments and sends them approximately 350 milliseconds apart. The simulated accuracy and speed values are artificial. This feature is test data only.

### 8.5 Heartbeats and timeout detection

The browser runs a heartbeat timer every five seconds. For a traveler with live location enabled, the heartbeat path obtains the current position and submits it as telemetry. Otherwise it calls `POST /api/sessions/{id}/heartbeat`.

Important distinction:

- A normal heartbeat updates `last_heartbeat` only.
- Telemetry updates `last_heartbeat` and `last_telemetry_epoch`.
- Timeout detection uses `last_telemetry_epoch`, not `last_heartbeat`.

Therefore, a heartbeat alone does not keep the location signal alive. This is deliberate and is covered by a test.

The configured backend constants are:

```text
HEARTBEAT_TIMEOUT_SECONDS = 10
GRACE_PERIOD_SECONDS = 5
```

When a session is read through `/api/sessions/{id}` or `/api/health`, `check_timeouts()` examines active sessions:

```text
No telemetry yet                 remain ACTIVE
More than 10 seconds             ACTIVE -> WARNING
More than 15 seconds             ACTIVE/WARNING -> DISTRESS
```

At `DISTRESS`, an alert is created and the first responder is recorded in the notification outbox.

This is a **request-driven watchdog**. There is no background scheduler. If nobody calls a route that invokes `check_timeouts()`, a stale session does not transition at that exact moment. The frontend's polling normally causes checks to happen during an open browser session.

### 8.6 Safe stop and duress

Both controls prompt for a PIN and call `POST /api/sessions/{id}/cancel`.

- Safe PIN: transition to `CANCELLED` and set `ended_at`.
- Duress PIN: transition to `DISTRESS` and create an explicit duress alert.
- Any other PIN: return HTTP 403.

The current implementation does not prevent repeated or contradictory cancellation attempts in every status combination. This is a prototype-level state-management limitation worth addressing before production.

### 8.7 Alert acknowledgement

When an alert exists, the guardian view renders the responder list. The current responder can acknowledge through:

```text
POST /api/alerts/{alert_id}/acknowledge
Body: { "responder_id": "responder-1" }
```

The backend checks that the alert exists, is still `ALERTING`, and uses a known responder ID. It changes the alert to `ACKNOWLEDGED`, records the time and responder, and changes a `DISTRESS` session to `RESOLVED`.

The escalation list is currently a static Python list. The `notify_responder()` function records an outbox row rather than sending email, SMS, push notifications, or a phone call. If all responders are exhausted, the alert becomes `UNACKNOWLEDGED`.

### 8.8 Report download and verification

The browser calls `GET /api/sessions/{id}/report`. The server gathers session data, creates a PDF in memory with ReportLab, and returns it as an attachment.

The report includes:

- session ID and status;
- start time and last heartbeat;
- latest alert information;
- telemetry sequence, event time, coordinates, speed, and accuracy;
- hash-chain verification result.

The verification endpoint independently reconstructs each expected hash from stored fields and the previous hash:

```text
current_hash = SHA256(canonical_telemetry_json + previous_hash)
```

The chain is valid only when every row's hash and `previous_hash` match and the final row matches `sessions.last_hash`.

## 9. Backend File: `app/main.py`

`app/main.py` contains nearly all server responsibilities.

### Configuration and application setup

- Calculates the repository root.
- Reads `SENTINEL_DB`, defaulting to `sentineltrace.db` in the repository root.
- Defines timeout constants.
- Creates the FastAPI app with title and version.
- Mounts the static directory.
- Creates a process-local database lock.
- Defines the static responder list.

### Database helpers

`connect()` opens SQLite with `check_same_thread=False` and returns rows as dictionary-like `sqlite3.Row` values.

`init_db()` creates all tables if missing and adds `last_telemetry_epoch` to older databases when required. It runs immediately when the module is imported.

`find_session()` centralizes exact and token/prefix lookup behavior.

`session_payload()` assembles the API's main read model by joining data from sessions, telemetry, state events, alerts, and guardians. It also returns the static responder definitions.

### Domain calculations

`haversine()` calculates the great-circle distance between two latitude/longitude points in meters. It is more appropriate than a flat Cartesian distance for geographic coordinates.

`calculate_bearing()` calculates the initial compass bearing from one coordinate to the next and normalizes it to 0 through 360 degrees.

### State and alert helpers

`transition()` updates the session status, records warning or distress timestamps, and inserts an audit event.

`make_alert()` creates an alert ID such as `ST-ABC12345`, inserts the alert, and starts responder notification.

`notify_responder()` writes the current responder index and an outbox record. It does not perform external delivery.

`check_timeouts()` is the watchdog logic and is called by selected routes rather than a scheduler.

### HTTP routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Serves the frontend page |
| GET | `/api/health` | Health check and timeout processing |
| GET | `/api/responders` | Returns configured responders |
| POST | `/api/sessions` | Creates a session |
| GET | `/api/sessions/{id}` | Reads current session state and telemetry |
| POST | `/api/sessions/{id}/join` | Adds a guardian name |
| POST | `/api/sessions/{id}/heartbeat` | Records a normal heartbeat |
| POST | `/api/sessions/{id}/telemetry` | Stores a GPS event |
| POST | `/api/sessions/{id}/cancel` | Performs safe stop or duress |
| POST | `/api/alerts/{id}/acknowledge` | Records responder acknowledgement |
| GET | `/api/sessions/{id}/verify` | Verifies the telemetry chain |
| GET | `/api/sessions/{id}/report` | Generates a PDF report |

## 10. Request Models and Validation

FastAPI uses Pydantic models:

### `SessionCreate`

- `duration_minutes`: integer from 1 to 240, default 15.
- `safe_pin`: string from 4 to 12 characters, default `1234`.
- `duress_pin`: string from 4 to 12 characters, default `9999`.

The backend separately checks that the two PINs differ. The duration is currently stored nowhere and does not expire the session.

### `TelemetryIn`

- `event_id`: UUID-like string generated by default if omitted.
- `sequence`: non-negative integer.
- `event_time`: string; it is not parsed into a datetime by the model.
- `latitude`: -90 to 90.
- `longitude`: -180 to 180.
- `accuracy`: optional non-negative number.
- `speed`: optional non-negative number.
- `bearing`: optional number from 0 inclusive to 360 exclusive.

### `PinIn`

Contains a PIN string. The model does not enforce a length or numeric-only format; the session comparison performs the actual authorization check.

### `AckIn`

Contains a responder ID.

### `GuardianJoin`

Requires a name with 1 to 80 characters. The route additionally normalizes whitespace.

## 11. Database Design

### `sessions`

One row per escort session.

- `id`: primary key.
- `status`: current state.
- `safe_pin`, `duress_pin`: stored directly in plaintext in this prototype.
- `started_at`, `ended_at`: lifecycle timestamps.
- `last_heartbeat`: latest heartbeat or telemetry receipt time.
- `warning_at`, `distress_at`: state transition timestamps.
- `last_hash`: hash of the latest telemetry point.
- `last_telemetry_epoch`: integer timestamp used by timeout checks.

### `telemetry`

One row per accepted GPS event.

- Auto-increment database ID.
- Session and event IDs.
- Sequence number and event time.
- Server receive time.
- Location and optional measurements.
- Derived distance.
- Previous and current hash values.
- Unique event ID and unique `(session_id, sequence)` constraint.

### `state_events`

Append-only-style audit records for status transitions and reasons. The database does not enforce immutability, so a privileged database user could still edit rows.

### `alerts`

Tracks explicit or timeout distress incidents, acknowledgement, and current responder index.

### `notifications`

Records attempted responder notifications. The status `RECORDED_IN_OUTBOX` means the system recorded the intent; it does not mean an actual message was delivered.

### `guardians`

Stores guardian display names and join timestamps for the shared session screen.

## 12. Idempotency and Ordering

Telemetry accepts a point only if its `event_id` and `(session_id, sequence)` are both new. A repeated event returns:

```json
{"status": "duplicate_ignored", "id": 1}
```

This protects against retries from an unreliable network. It does not enforce that sequence values arrive in strict increasing order. A new sequence of 100 may be accepted after sequence 1, and a lower unused sequence may also be accepted. Because hash lookup uses the highest sequence while the chain uses `sessions.last_hash`, out-of-order submissions can make the logical sequence order and insertion/hash order diverge. Production code should define ordering rules explicitly.

## 13. Frontend File: `static/index.html`

The HTML defines the single-page command view:

- top navigation and prototype indicator;
- create and join buttons;
- status strip for state, contact time, points, and integrity;
- Leaflet map container;
- session identity and share control;
- guardian inspection indicator;
- route metrics;
- traveler signal controls;
- observer mode banner;
- responder list;
- report download control;
- event timeline;
- security explanation;
- new-session modal;
- toast notification element.

The HTML loads external Leaflet assets before the local JavaScript so `L.map()` is available when `app.js` executes.

## 14. Frontend File: `static/app.js`

The JavaScript is an imperative single-page controller.

### Browser state

- `sessionId`: session from the URL.
- `currentRole`: traveler or guardian.
- `timer`: two-second session refresh interval.
- `heartbeatTimer`: five-second heartbeat interval.
- `locationWatch`: browser geolocation watch handle.
- `seq`: local telemetry sequence counter.
- `waypoints` and `simIndex`: simulation state.
- `lastObservedStatus`: used to show status-change toasts.
- `map`, `route`, and `markers`: Leaflet objects.

### Main helpers

- `api()`: wraps Fetch, parses JSON/text, and raises readable errors.
- `setSessionUrl()`: updates the URL without a page reload.
- `startTimers()`: starts polling and heartbeats.
- `sendPoint()`: creates and submits a telemetry request.
- `useLocation()`: starts browser GPS watching.
- `prepareSimulation()` and `sendSimulatedPoint()`: create demo route data.
- `render()`: updates status, metrics, map, integrity, events, guardians, and responders.
- `renderResponders()`: displays escalation state and acknowledgement controls.
- `refresh()`: retrieves the current session payload.
- `endSession()`: prompts for a PIN and calls the cancel endpoint.

### Rendering behavior

The `render()` function calculates total route distance by summing server-provided `distance_m` fields. It draws the entire route, removes old markers, and adds start and last-known markers. It fits the map bounds for multiple points or centers on one point.

It also calls the verification endpoint separately on every render. This gives the screen a current integrity result, but it creates an additional request for every two-second refresh.

The guardian name is escaped through `escapeHtml()` before insertion into `innerHTML`. Other values inserted into `innerHTML`, such as event reasons and responder data, are server-generated in the current prototype. A production frontend should use safer DOM APIs or escape every dynamic field consistently.

## 15. Frontend File: `static/styles.css`

The stylesheet defines:

- the paper-like application background and teal/coral/yellow status palette;
- typography using the Google Fonts loaded by the HTML;
- responsive grid layout for the map and side panel;
- status indicators and state colors;
- modal, buttons, forms, metrics, responders, timeline, and toast styles;
- mobile layout rules so the map and control panel stack on narrow screens.

It is presentation-only. It does not contain business rules or API behavior.

## 16. Deployment: `render.yaml`

Render runs:

```text
Build: pip install -r requirements.txt
Start: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

The service is configured as a free Python web service named `sentineltrace`. It sets:

- `SENTINEL_DB=/tmp/sentineltrace.db`;
- `SENTINEL_WARNING_SECONDS=12`;
- `SENTINEL_GRACE_SECONDS=18`.

The first variable is used. The warning and grace variables are currently ignored by `app/main.py`, which hard-codes 10 and 5 seconds. This is a configuration mismatch and should be corrected before relying on Render settings.

The `/tmp` database is ephemeral. A restart, redeploy, or instance replacement can remove session history. Free instances can sleep, which makes this unsuitable for real monitoring.

## 17. Testing Strategy

`tests/test_api.py` uses a temporary SQLite database by setting `SENTINEL_DB` before importing the FastAPI app.

The tests cover:

- telemetry recording and duplicate idempotency;
- hash-chain verification;
- duress PIN alert creation;
- session prefix lookup;
- token lookup without `session-`;
- guardian name normalization and sharing;
- stale telemetry becoming distress during a session read;
- normal heartbeat not keeping telemetry alive;
- new sessions waiting for first telemetry before timeout checks;
- PDF report content type and PDF signature;
- rejecting a second acknowledgement.

A normal local test command is:

```powershell
pytest
```

The current tests are valuable API behavior tests, but they do not cover frontend behavior, browser permissions, real GPS, Render deployment, concurrent requests, authentication, database persistence across restart, or actual notification delivery.

## 18. Advantages

### Clear prototype scope

The README and UI state that the system is not emergency infrastructure. This reduces the risk of presenting a demonstration as a guarantee.

### Simple end-to-end design

A small FastAPI service and plain browser client make the entire workflow easy to inspect and run locally.

### Good validation at the API boundary

Pydantic constrains coordinates, ranges, optional telemetry values, session duration, and guardian names before domain logic runs.

### Tamper-evident telemetry concept

The chained SHA-256 records make accidental or unauthorized edits detectable when verification is run. The report includes the verification result.

### Retry tolerance

Unique event IDs and sequence constraints provide basic idempotency for repeated telemetry submissions.

### Useful audit trail

State transitions, alerts, notifications, guardian joins, receive times, and telemetry are stored separately, which makes the workflow inspectable.

### Graceful map degradation in the demo

The application can display either real browser coordinates or simulated points, making the project easier to demonstrate.

### Focused tests

The tests target important domain behaviors rather than only checking that routes return 200.

## 19. Disadvantages and Risks

### No authentication or authorization

Anyone who obtains or guesses a session ID can read data, join as a guardian, submit telemetry, trigger PIN operations if they know a PIN, or acknowledge alerts. Session IDs are not a security boundary.

### PINs are stored as plaintext

A database leak exposes the safe and duress PINs. Production systems should hash PINs with a password hashing function and use secure role-specific credentials.

### Session IDs are shareable secrets

The session ID appears in the URL and is copied to another device. URLs can enter browser history, logs, screenshots, referrer data, or chat systems. A separate expiring access token with limited permissions would be safer.

### No real notifications

The notification outbox only records intended notifications. No email, SMS, push, voice call, or retry worker exists.

### Request-driven timeout watchdog

Distress transitions depend on a request reaching a route that calls `check_timeouts()`. There is no independent background scheduler or worker.

### In-memory/static responder configuration

Responders cannot be managed, authenticated, or configured per organization. Escalation does not actually advance based on elapsed acknowledgement time.

### Duration is not enforced

The UI collects `duration_minutes` and the model validates it, but the value is not stored or used. Sessions never automatically become `EXPIRED`.

### Render timeout settings do not work

`SENTINEL_WARNING_SECONDS` and `SENTINEL_GRACE_SECONDS` are declared in `render.yaml` but not read by the backend.

### Ephemeral database deployment

`/tmp/sentineltrace.db` can disappear on restart or redeploy. There is no backup, replication, migration system, or managed database.

### Single-process locking assumption

`_db_lock` protects operations within one Python process. It does not coordinate multiple Render instances or independent processes. SQLite is not an ideal production store for concurrent distributed ingestion.

### No strict telemetry ordering

The API deduplicates sequence numbers but does not reject out-of-order or old event times. This can make route order, hash order, and client sequence assumptions disagree.

### Client-side role control

Hiding traveler controls in JavaScript is only a UI choice. The server does not receive or validate a role or access token.

### Browser reliability

A browser tab is not a dependable background monitoring agent. Mobile operating systems can suspend it, GPS can be inaccurate, permissions can be revoked, and a dead battery cannot be detected as an emergency with certainty.

### Privacy and retention

Location history and guardian names are stored without a retention policy, deletion workflow, consent workflow, or encryption design. Location data is sensitive and requires strict access control.

### Report limitations

The PDF contains known stored telemetry only. It does not prove completeness, authenticity of the original GPS signal, or that a person was attacked. Report generation is synchronous and may become slow for large telemetry histories.

### External frontend dependencies

Leaflet, OpenStreetMap tiles, and Google Fonts are loaded from external services. The UI can be affected by network availability, provider limits, or policy requirements.

## 20. Recommended Production Evolution

1. Add authentication and short-lived, scoped traveler/guardian access tokens.
2. Hash safe and duress PINs and add rate limiting and lockout rules.
3. Move session and telemetry data to managed PostgreSQL or another durable store.
4. Use a real migration tool and startup health checks.
5. Read timeout configuration from environment variables with safe parsing and tests.
6. Add a background scheduler or queue worker for timeout processing.
7. Add actual notification providers with delivery status, retry, and escalation timing.
8. Store and enforce session expiration using `duration_minutes`.
9. Enforce telemetry ordering and validate event timestamps and clock skew.
10. Add audit immutability controls, database access controls, encryption, and backups.
11. Add privacy consent, retention, export, and deletion policies.
12. Use a native mobile client or background-capable platform integration for reliable monitoring.
13. Add structured logging, metrics, tracing, alerting, and operational runbooks.
14. Add frontend end-to-end tests for joining, GPS permission errors, alert acknowledgement, and downloads.
15. Paginate telemetry and generate reports asynchronously for long sessions.

## 21. Study Guide: Questions and Answers

### What is the source of truth?

The FastAPI backend and SQLite database. The browser only renders the most recently fetched payload and submits user/device events.

### Why is telemetry hash chained?

Each record includes a hash of its canonical fields plus the previous hash. Changing a record or removing a record breaks the expected chain and makes verification fail.

### Why are both event time and received time stored?

`event_time` describes when the browser says the event occurred. `received_at` records when the server accepted it. Comparing them helps identify delays or client clock issues.

### Why does a heartbeat not count as location telemetry?

The project wants to distinguish communication reachability from fresh location evidence. Timeout logic uses `last_telemetry_epoch`, so a device that only sends heartbeat requests does not appear to be sending location updates.

### How is duplicate telemetry handled?

The server checks the unique event ID and the session/sequence pair before inserting. A duplicate returns `duplicate_ignored` instead of creating another record.

### Where does the alert escalation happen?

`make_alert()` creates the alert and calls `notify_responder()` for responder index zero. In the current build, escalation is represented in the database outbox rather than delivered externally.

### What makes the system unsafe for production?

The largest issues are absent authentication, plaintext PINs, browser background limitations, no real notification delivery, ephemeral storage, and request-driven timeout processing.

### How would you fix the timeout configuration mismatch?

Read `SENTINEL_WARNING_SECONDS` and `SENTINEL_GRACE_SECONDS` in `app/main.py`, parse them as positive numbers, use them in `check_timeouts()`, and add tests that override them.

### How would you make session expiration work?

Store `duration_minutes` or an absolute `expires_at` in `sessions`, compare it against the current UTC time during watchdog processing, transition active sessions to `EXPIRED`, and test expiration independently of heartbeat loss.

## 22. Local Runbook

Create and activate a virtual environment:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Start the service:

```powershell
uvicorn app.main:app --reload --host 0.0.0.0
```

Open:

```text
http://127.0.0.1:8000
```

Run tests:

```powershell
pytest
```

For real phone geolocation during a demonstration, deploy through Render and use the generated HTTPS URL. Keep test data only; this implementation is not suitable for real emergency monitoring.

## 23. One-Paragraph Summary

SentinelTrace is a deliberately small FastAPI plus browser application that models an escort safety workflow. The browser creates or joins a session, sends GPS and heartbeat events, and polls the backend. `app/main.py` validates and stores those events in SQLite, calculates route measurements, chains telemetry with SHA-256, changes session state when contact is lost, records alerts and responder acknowledgement, verifies integrity, and generates a PDF. The design is easy to study because most behavior is centralized and tested, but the same simplicity creates serious production limitations: there is no authentication, the database is ephemeral on Render, notifications are only simulated, timeout checks are request-driven, browser execution is unreliable in the background, duration is not enforced, and the deployment timeout variables are currently unused.
