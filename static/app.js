const $ = (id) => document.getElementById(id);
let sessionId = new URLSearchParams(window.location.search).get('session');
let currentRole = sessionId ? 'traveler' : null;
let timer = null;
let heartbeatTimer = null;
let locationWatch = null;
let guardianId = null;
let seq = 0;
let simIndex = 0;
let waypoints = [];
let lastObservedStatus = null;
const map = L.map('map', { zoomControl: false }).setView([0, 0], 2);
L.control.zoom({ position: 'bottomright' }).addTo(map);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap contributors' }).addTo(map);
const route = L.polyline([], { color: '#e6ae3f', weight: 5 }).addTo(map);
let markers = [];

function toast(text) {
  $('toast').textContent = text;
  $('toast').classList.add('show');
  setTimeout(() => $('toast').classList.remove('show'), 2800);
}

function iso() { return new Date().toISOString(); }

function promptValue(message, defaultValue = '') {
  if (typeof window.prompt === 'function') {
    try {
      return window.prompt(message, defaultValue);
    } catch (error) {
      // Some browsers and embedded contexts block prompt() unexpectedly.
    }
  }

  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.style.position = 'fixed';
    overlay.style.inset = '0';
    overlay.style.background = 'rgba(15, 23, 42, 0.72)';
    overlay.style.display = 'flex';
    overlay.style.alignItems = 'center';
    overlay.style.justifyContent = 'center';
    overlay.style.zIndex = '2000';

    const modal = document.createElement('div');
    modal.style.width = 'min(92vw, 360px)';
    modal.style.background = '#fff';
    modal.style.borderRadius = '14px';
    modal.style.boxShadow = '0 20px 48px rgba(15, 23, 42, 0.25)';
    modal.style.padding = '20px';
    modal.style.color = '#0f172a';
    modal.style.fontFamily = 'DM Sans, sans-serif';

    const title = document.createElement('div');
    title.textContent = message;
    title.style.marginBottom = '12px';
    title.style.fontWeight = '700';

    const input = document.createElement('input');
    input.type = 'text';
    input.value = defaultValue;
    input.style.width = '100%';
    input.style.boxSizing = 'border-box';
    input.style.padding = '10px 12px';
    input.style.border = '1px solid #cbd5e1';
    input.style.borderRadius = '10px';
    input.style.fontSize = '16px';
    input.style.marginBottom = '14px';

    const actions = document.createElement('div');
    actions.style.display = 'flex';
    actions.style.justifyContent = 'flex-end';
    actions.style.gap = '8px';

    const cancel = document.createElement('button');
    cancel.textContent = 'Cancel';
    cancel.type = 'button';
    cancel.style.padding = '10px 12px';
    cancel.style.border = 'none';
    cancel.style.borderRadius = '10px';
    cancel.style.background = '#e2e8f0';
    cancel.style.cursor = 'pointer';
    cancel.onclick = () => { cleanup(); resolve(null); };

    const submit = document.createElement('button');
    submit.textContent = 'OK';
    submit.type = 'button';
    submit.style.padding = '10px 12px';
    submit.style.border = 'none';
    submit.style.borderRadius = '10px';
    submit.style.background = '#172b4d';
    submit.style.color = '#fff';
    submit.style.cursor = 'pointer';
    submit.onclick = () => { cleanup(); resolve(input.value); };

    const cleanup = () => {
      overlay.remove();
      input.blur();
    };

    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit.click();
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        cancel.click();
      }
    });

    actions.append(cancel, submit);
    modal.append(title, input, actions);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    input.focus();
    input.select();
  });
}

function setState(status) {
  $('state').textContent = status;
  $('session-badge').textContent = status;
  $('state-pulse').className = `state-pulse ${status.toLowerCase()}`;
  $('signal-status').textContent = status === 'ACTIVE' ? 'Connected' : status === 'NO SESSION' ? 'Offline' : 'Incident active';
}

function setRole(role) {
  currentRole = role;
  const isTraveler = role === 'traveler';
  $('signal-controls').style.display = isTraveler ? '' : 'none';
  $('observer-mode').classList.toggle('hidden', isTraveler);
}

function openModal() { setRole('traveler'); $('modal').classList.remove('hidden'); }
function closeModal() { $('modal').classList.add('hidden'); }
$('new-session').onclick = openModal;
$('close-modal').onclick = closeModal;

async function api(url, options = {}) {
  const response = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
  const contentType = response.headers.get('content-type') || '';
  const body = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof body === 'string' ? body : body.detail || 'Request failed';
    throw new Error(`${response.status}: ${message}`);
  }
  return body;
}

function setSessionUrl(id) {
  const url = new URL(window.location.href);
  url.searchParams.set('session', id);
  window.history.replaceState({}, '', url);
}

function startTimers() {
  clearInterval(timer);
  clearInterval(heartbeatTimer);
  timer = setInterval(refresh, 2000);
  heartbeatTimer = setInterval(sendHeartbeat, 5000);
}

async function start() {
  try {
    const data = await api('/api/sessions', { method: 'POST', body: JSON.stringify({ duration_minutes: +$('duration').value, safe_pin: $('safe-pin').value, duress_pin: $('duress-pin').value }) });
    sessionId = data.session.id;
    lastObservedStatus = null;
    seq = 0;
    simIndex = 0;
    setSessionUrl(sessionId);
    closeModal();
    setState('ACTIVE');
    $('session-short').textContent = sessionId.slice(-10);
    toast('Escort started. Share this page link with the responder.');
    refresh();
    startTimers();
  } catch (error) { toast(error.message); }
}
$('start-session').onclick = start;

async function join() {
  const entered = await promptValue('Paste the session link or enter the session ID');
  if (!entered) return;
  const guardianName = await promptValue('Enter your name as the guardian');
  if (!guardianName || !guardianName.trim()) return toast('Guardian name is required to join');
  setRole('guardian');
  try {
    const value = entered.includes('session=') ? new URL(entered).searchParams.get('session') : entered.trim();
    if (!value) throw new Error('No session ID found');
    sessionId = value;
    setSessionUrl(sessionId);
    const data = await api(`/api/sessions/${sessionId}/join`, { method: 'POST', body: JSON.stringify({ name: guardianName }) });
    guardianId = data.guardians.at(-1).id;
    render(data);
    startTimers();
    toast('Joined escort session');
  } catch (error) { sessionId = null; toast(error.message); }
}
$('join-session').onclick = join;

async function sendHeartbeat() {
  if (!sessionId) return;
  try {
    if (currentRole === 'traveler' && locationWatch !== null) {
      const position = await getCurrentPosition();
      await sendPoint(position.coords.latitude, position.coords.longitude, position.coords);
    } else {
      await api(`/api/sessions/${sessionId}/heartbeat`, { method: 'POST' });
    }
  } catch (error) { if (currentRole === 'traveler' && locationWatch !== null) return; toast(error.message); }
}
$('heartbeat').onclick = sendHeartbeat;

async function sendPoint(latitude, longitude, position = {}) {
  if (!sessionId) return;
  seq += 1;
  try {
    await api(`/api/sessions/${sessionId}/telemetry`, { method: 'POST', body: JSON.stringify({ event_id: `gps-${sessionId}-${seq}-${Date.now()}`, sequence: seq, event_time: iso(), latitude, longitude, accuracy: position.accuracy ?? null, speed: position.speed ?? null, bearing: position.heading ?? null }) });
    refresh();
  } catch (error) { toast(error.message); }
}

function useLocation() {
  if (!sessionId) return toast('Join or start an escort first');
  if (!window.isSecureContext) return toast('Location requires an HTTPS link');
  if (!navigator.geolocation) return toast('This browser does not support location');
  if (locationWatch !== null) navigator.geolocation.clearWatch(locationWatch);
  locationWatch = navigator.geolocation.watchPosition(
    (position) => sendPoint(position.coords.latitude, position.coords.longitude, position.coords),
    (error) => toast(`Location unavailable: ${error.message}`),
    { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 },
  );
  toast('Live location enabled');
}
$('use-location').onclick = useLocation;

async function sendSimulatedPoint() {
  if (!sessionId || simIndex >= waypoints.length) return toast('Route simulation complete');
  const [latitude, longitude] = waypoints[simIndex++];
  await sendPoint(latitude, longitude, { accuracy: 7 + seq, speed: 1.3 + seq * 0.45 });
}
$('simulate').onclick = async () => { try { await prepareSimulation(); for (let index = 0; index < waypoints.length; index += 1) { await sendSimulatedPoint(); await new Promise((resolve) => setTimeout(resolve, 350)); } } catch (error) { toast(`Simulation unavailable: ${error.message}`); } };

function getCurrentPosition() {
  return new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, reject, { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 }));
}

async function prepareSimulation() {
  if (!window.isSecureContext || !navigator.geolocation) throw new Error('Simulation requires location permission on an HTTPS link');
  const position = await getCurrentPosition();
  const { latitude, longitude } = position.coords;
  const latitudeStep = 0.0014;
  const longitudeStep = 0.0014 / Math.max(Math.cos(latitude * Math.PI / 180), 0.2);
  waypoints = Array.from({ length: 6 }, (_, index) => [latitude + index * latitudeStep, longitude + index * longitudeStep]);
  simIndex = 0;
}

async function endSession(message) {
  if (!sessionId) return;
  const pin = await promptValue('Enter the PIN');
  if (!pin) return;
  try { await api(`/api/sessions/${sessionId}/cancel`, { method: 'POST', body: JSON.stringify({ pin }) }); toast(message); refresh(); } catch (error) { toast(error.message); }
}
$('stop-signal').onclick = () => endSession('Session ended');
$('duress').onclick = () => endSession('Duress signal sent');

function formatTime(value) { return value ? new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '--'; }

function render(data) {
  setRole(currentRole || 'traveler');
  const session = data.session;
  const points = data.telemetry;
  const statusChanged = lastObservedStatus !== null && lastObservedStatus !== session.status;
  lastObservedStatus = session.status;
  setState(session.status);
  if (statusChanged && (session.status === 'WARNING' || session.status === 'DISTRESS')) {
    toast(session.status === 'DISTRESS' ? 'ALERT: Heartbeat signal lost' : 'Warning: heartbeat signal delayed');
  }
  $('session-short').textContent = session.id.slice(-10);
  $('last-contact').textContent = formatTime(session.last_heartbeat);
  const total = points.reduce((sum, point) => sum + point.distance_m, 0);
  $('distance').textContent = total < 1000 ? `${Math.round(total)} m` : `${(total / 1000).toFixed(2)} km`;
  const last = points[points.length - 1];
  if (last) {
    const coordinates = points.map((point) => [point.latitude, point.longitude]);
    route.setLatLngs(coordinates);
    markers.forEach((marker) => map.removeLayer(marker));
    markers = [L.circleMarker(coordinates[0], { radius: 7, color: '#0d8179', fillOpacity: 1 }).addTo(map).bindTooltip('Start'), L.circleMarker(coordinates.at(-1), { radius: 8, color: '#e7654f', fillOpacity: 1 }).addTo(map).bindTooltip('Last known position')];
    if (coordinates.length === 1) map.setView(coordinates[0], 15); else map.fitBounds(route.getBounds(), { padding: [35, 35] });
  }
  fetch(`/api/sessions/${sessionId}/verify`).then((response) => response.json()).then((verification) => { $('hash-status').textContent = verification.valid ? 'verified' : 'failed'; });
  $('events').innerHTML = data.events.length ? data.events.slice().reverse().map((event) => `<div class="event"><time>${formatTime(event.created_at)}</time><span>${event.reason}</span><span class="event-state">${event.to_status}</span></div>`).join('') : '<p class="muted">No events yet.</p>';
  const guardians = data.guardians || [];
  $('guardian-inspection').innerHTML = guardians.length ? guardians.map((guardian) => `<span class="inspection-name">${escapeHtml(guardian.name)}</span> is inspecting this escort.`).join('<br>') : 'No guardian is currently inspecting this escort.';
  renderResponders(data);
}

function escapeHtml(value) {
  return value.replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]);
}

function renderResponders(data) {
  const alert = data.alerts[0];
  if (!alert) return $('responders').innerHTML = '<p class="muted">No incident in progress.</p>';
  const current = alert.current_responder;
  $('responders').innerHTML = data.responders.map((responder, index) => { const notified = index <= current; const acknowledged = alert.acknowledged_by === responder.id; const action = currentRole === 'guardian' && !acknowledged && alert.status === 'ALERTING' && index === current ? `<button class="ack-button" onclick="ack('${alert.id}','${responder.id}')">Acknowledge</button>` : acknowledged ? '<span class="ack">ACK</span>' : ''; return `<div class="responder"><span><strong>${responder.name}</strong><small>${responder.role} · ${notified ? 'Notified' : 'Queued'}</small></span>${action}</div>`; }).join('');
}
window.ack = async (alertId, responderId) => { try { await api(`/api/alerts/${alertId}/acknowledge`, { method: 'POST', body: JSON.stringify({ responder_id: responderId }) }); toast('Responder acknowledgement recorded'); refresh(); } catch (error) { toast(error.message); } };

async function refresh() {
  if (!sessionId) return;
  try { render(await api(`/api/sessions/${sessionId}`)); } catch (error) { toast(error.message); }
}
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const input = document.createElement('textarea');
  input.value = text;
  input.setAttribute('readonly', '');
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.select();
  const copied = document.execCommand('copy');
  input.remove();
  if (!copied) throw new Error('Clipboard access is unavailable. Copy the link from the address bar.');
}
$('download').onclick = async () => {
  if (!sessionId) return toast('Start or join an escort first');
  try {
    const response = await fetch(`/api/sessions/${sessionId}/report`);
    if (!response.ok) throw new Error('Report could not be generated');
    const blob = await response.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `${sessionId}-incident-report.pdf`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    toast('Incident report downloaded');
  } catch (error) { toast(error.message); }
};
setInterval(() => { $('clock').textContent = new Date().toLocaleTimeString(); }, 1000);
if (sessionId) { refresh(); startTimers(); } else setState('NO SESSION');
