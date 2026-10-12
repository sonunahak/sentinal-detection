const $ = (id) => document.getElementById(id);
let sessionId = new URLSearchParams(window.location.search).get('session');
let currentRole = sessionId ? 'traveler' : null;
let timer = null;
let heartbeatTimer = null;
let locationWatch = null;
let seq = 0;
let hasAutoCentered = false;

const map = L.map('map', { zoomControl: false }).setView([0, 0], 2);
L.control.zoom({ position: 'bottomright' }).addTo(map);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap contributors' }).addTo(map);
const route = L.polyline([], { color: '#eab13f', weight: 5 }).addTo(map);
let markers = [];

function centerRoute(coordinates) {
  if (!coordinates.length) return;
  if (coordinates.length === 1) map.setView(coordinates[0], 15);
  else map.fitBounds(route.getBounds(), { padding: [35, 35] });
}

function toast(message) {
  const toastEl = $('toast');
  toastEl.textContent = message;
  toastEl.classList.add('show');
  setTimeout(() => toastEl.classList.remove('show'), 2800);
}

function formatTime(value) {
  if (!value) return '--';
  return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function setState(status) {
  const safeStatus = status || 'NO SESSION';
  const cssClass = safeStatus.toLowerCase().replace(/\s+/g, '-');
  $('state').textContent = safeStatus;
  $('session-badge').textContent = safeStatus === 'ACTIVE' ? 'ACTIVE' : safeStatus === 'NO SESSION' ? 'IDLE' : safeStatus;
  $('state-pulse').className = `state-pulse ${cssClass}`;
  $('signal-status').textContent = safeStatus === 'ACTIVE' ? 'Connected' : safeStatus === 'NO SESSION' ? 'Offline' : 'Incident active';
}

function setRole(role) {
  currentRole = role;
  const isTraveler = role === 'traveler';
  $('signal-controls').style.display = isTraveler ? '' : 'none';
  $('observer-mode').classList.toggle('hidden', isTraveler);
}

function openModal() {
  setRole('traveler');
  $('modal').classList.remove('hidden');
}

function closeModal() {
  $('modal').classList.add('hidden');
}

$('new-session').onclick = openModal;
$('close-modal').onclick = closeModal;

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
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

function promptValue(message, defaultValue = '') {
  if (typeof window.prompt === 'function') {
    try {
      return window.prompt(message, defaultValue);
    } catch (error) {
      // blocked by browser policy
    }
  }

  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.style.position = 'fixed';
    overlay.style.inset = '0';
    overlay.style.background = 'rgba(15, 23, 42, 0.65)';
    overlay.style.display = 'grid';
    overlay.style.placeItems = 'center';
    overlay.style.zIndex = '50';

    const card = document.createElement('div');
    card.style.width = 'min(92vw, 360px)';
    card.style.background = '#fff';
    card.style.borderRadius = '16px';
    card.style.padding = '18px';
    card.style.boxShadow = '0 18px 46px rgba(15, 23, 42, 0.18)';

    const title = document.createElement('div');
    title.textContent = message;
    title.style.fontWeight = '700';
    title.style.marginBottom = '12px';

    const input = document.createElement('input');
    input.value = defaultValue;
    input.style.width = '100%';
    input.style.boxSizing = 'border-box';
    input.style.padding = '10px 12px';
    input.style.borderRadius = '10px';
    input.style.border = '1px solid #dfe3de';
    input.style.marginBottom = '12px';

    const actions = document.createElement('div');
    actions.style.display = 'flex';
    actions.style.justifyContent = 'flex-end';
    actions.style.gap = '8px';

    const cancel = document.createElement('button');
    cancel.textContent = 'Cancel';
    cancel.type = 'button';
    cancel.style.padding = '10px 12px';
    cancel.style.borderRadius = '10px';
    cancel.style.border = 'none';
    cancel.style.background = '#e2e8f0';
    cancel.style.cursor = 'pointer';
    cancel.onclick = () => { overlay.remove(); resolve(null); };

    const ok = document.createElement('button');
    ok.textContent = 'OK';
    ok.type = 'button';
    ok.style.padding = '10px 12px';
    ok.style.borderRadius = '10px';
    ok.style.background = '#15222d';
    ok.style.border = 'none';
    ok.style.color = '#fff';
    ok.style.cursor = 'pointer';
    ok.onclick = () => { overlay.remove(); resolve(input.value); };

    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); ok.click(); }
      if (event.key === 'Escape') { event.preventDefault(); cancel.click(); }
    });

    actions.append(cancel, ok);
    card.append(title, input, actions);
    overlay.append(card);
    document.body.appendChild(overlay);
    input.focus();
    input.select();
  });
}

async function start() {
  try {
    const payload = {
      duration_minutes: Number($('duration').value),
      safe_pin: $('safe-pin').value,
      duress_pin: $('duress-pin').value,
    };
    const data = await api('/api/sessions', { method: 'POST', body: JSON.stringify(payload) });
    sessionId = data.session.id;
    hasAutoCentered = false;
    seq = 0;
    setSessionUrl(sessionId);
    closeModal();
    setState('ACTIVE');
    $('session-short').textContent = sessionId.slice(-10);
    toast('Escort started. Share this page link with the responder.');
    refresh();
    startTimers();
  } catch (error) {
    toast(error.message);
  }
}
$('start-session').onclick = start;

async function join() {
  const entered = await promptValue('Paste the session link or enter the session ID');
  if (!entered || !entered.trim()) return;

  const guardianName = await promptValue('Enter your name as the guardian');
  if (!guardianName || !guardianName.trim()) return toast('Guardian name is required to join');

  try {
    const sessionToken = entered.includes('session=') ? new URL(entered).searchParams.get('session') : entered.trim();
    if (!sessionToken) throw new Error('No session ID found');
    sessionId = sessionToken;
    setSessionUrl(sessionId);
    const data = await api(`/api/sessions/${sessionId}/join`, { method: 'POST', body: JSON.stringify({ name: guardianName.trim() }) });
    setRole('guardian');
    render(data);
    startTimers();
    toast('Joined escort session');
  } catch (error) {
    sessionId = null;
    toast(error.message);
  }
}
$('join-session').onclick = join;

async function getCurrentPosition() {
  return new Promise((resolve, reject) => {
    navigator.geolocation.getCurrentPosition(resolve, reject, {
      enableHighAccuracy: true,
      maximumAge: 5000,
      timeout: 15000,
    });
  });
}

function trackLocation() {
  if (!navigator.geolocation) return toast('This browser does not support location');
  if (locationWatch !== null) navigator.geolocation.clearWatch(locationWatch);
  locationWatch = navigator.geolocation.watchPosition(
    (position) => sendPoint(position.coords.latitude, position.coords.longitude, position.coords),
    (error) => toast(`Location unavailable: ${error.message}`),
    { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 },
  );
  toast('Live location enabled');
}

$('use-location').onclick = () => {
  if (!sessionId) return toast('Start or join an escort first');
  if (!window.isSecureContext) return toast('Location requires an HTTPS link');
  trackLocation();
};

async function sendPoint(latitude, longitude, coords = {}) {
  if (!sessionId) return;
  seq += 1;
  try {
    await api(`/api/sessions/${sessionId}/telemetry`, {
      method: 'POST',
      body: JSON.stringify({
        event_id: `gps-${sessionId}-${seq}-${Date.now()}`,
        sequence: seq,
        event_time: new Date().toISOString(),
        latitude,
        longitude,
        accuracy: coords.accuracy ?? null,
        speed: coords.speed ?? null,
        bearing: coords.heading ?? null,
      }),
    });
    refresh();
  } catch (error) {
    toast(error.message);
  }
}

async function sendHeartbeat() {
  if (!sessionId) return;
  try {
    if (currentRole === 'traveler' && navigator.geolocation && window.isSecureContext) {
      try {
        const position = await getCurrentPosition();
        await sendPoint(position.coords.latitude, position.coords.longitude, position.coords);
        return;
      } catch (error) {
        // fall through to heartbeat-only ping if location is unavailable
      }
    }
    await api(`/api/sessions/${sessionId}/heartbeat`, { method: 'POST' });
    refresh();
  } catch (error) {
    toast(error.message);
  }
}
$('heartbeat').onclick = sendHeartbeat;

$('simulate').onclick = async () => {
  if (!sessionId) return toast('Start an escort before simulating route');
  try {
    const startLat = 20.2961;
    const startLng = 85.8245;
    for (let step = 0; step < 6; step += 1) {
      const latitude = startLat + step * 0.0008;
      const longitude = startLng + step * 0.0012;
      await sendPoint(latitude, longitude, { accuracy: 8, speed: 1.4 });
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  } catch (error) {
    toast(error.message);
  }
};

async function stopSession(actionText) {
  if (!sessionId) return;
  const pin = await promptValue('Enter the safe PIN');
  if (!pin) return;
  try {
    await api(`/api/sessions/${sessionId}/cancel`, { method: 'POST', body: JSON.stringify({ pin }) });
    toast(actionText);
    refresh();
  } catch (error) {
    toast(error.message);
  }
}
$('stop-signal').onclick = () => stopSession('Stop signal sent');
$('duress').onclick = () => stopSession('Duress signal triggered');

function render(data) {
  const session = data.session || {};
  const points = data.telemetry || [];
  const events = data.events || [];
  const alerts = data.alerts || [];
  const guardians = data.guardians || [];

  setState(session.status || 'NO SESSION');
  $('session-short').textContent = session.id ? session.id.slice(-10) : 'Waiting to begin';
  $('last-contact').textContent = formatTime(session.last_heartbeat);

  if (points.length) {
    const coordinates = points.map((point) => [point.latitude, point.longitude]);
    route.setLatLngs(coordinates);
    markers.forEach((marker) => map.removeLayer(marker));
    markers = [
      L.circleMarker(coordinates[0], { radius: 7, color: '#0d8179', fillOpacity: 1 }).addTo(map).bindTooltip('Start'),
      L.circleMarker(coordinates[coordinates.length - 1], { radius: 8, color: '#e7654f', fillOpacity: 1 }).addTo(map).bindTooltip('Last known position'),
    ];
    if (!hasAutoCentered) {
      centerRoute(coordinates);
      hasAutoCentered = true;
    }
  } else {
    route.setLatLngs([]);
    markers.forEach((marker) => map.removeLayer(marker));
    markers = [];
  }

  const totalDistance = points.reduce((sum, point) => sum + Number(point.distance_m || 0), 0);
  $('distance').textContent = totalDistance < 1000 ? `${Math.round(totalDistance)} m` : `${(totalDistance / 1000).toFixed(2)} km`;

  $('guardian-inspection').innerHTML = guardians.length
    ? guardians.map((guardian) => `<span class="inspection-name">${guardian.name}</span>`).join(', ') + ' is inspecting this escort.'
    : 'No guardian is currently inspecting this escort.';

  $('events').innerHTML = events.length
    ? events.slice().reverse().map((event) => `<div class="event"><time>${formatTime(event.created_at)}</time><span>${event.reason}</span><span class="event-state">${event.to_status}</span></div>`).join('')
    : '<p class="muted">Start an escort to begin the audit trail.</p>';

  $('responders').innerHTML = alerts.length
    ? alerts.map((alert) => `<div class="responder"><strong>${alert.reason}</strong><small>${alert.status}</small></div>`).join('')
    : '<p class="muted">No incident in progress.</p>';

  $('hash-status').textContent = session.last_hash ? 'verified' : 'pending';
}

async function refresh() {
  if (!sessionId) {
    setState('NO SESSION');
    return;
  }
  try {
    const data = await api(`/api/sessions/${sessionId}`);
    render(data);
  } catch (error) {
    toast(error.message);
  }
}

$('download').onclick = async () => {
  if (!sessionId) return toast('Start or join a session first');
  window.open(`/api/sessions/${sessionId}/report`, '_blank');
};

function clock() {
  const now = new Date();
  $('clock').textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
setInterval(clock, 1000);
clock();

if (sessionId) {
  setRole('traveler');
  refresh();
  startTimers();
} else {
  setRole('traveler');
  setState('NO SESSION');
}
