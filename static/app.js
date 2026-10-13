const $ = (id) => document.getElementById(id);
let sessionId = new URLSearchParams(window.location.search).get('session');
let currentRole = sessionId ? 'traveler' : null;
let timer = null;
let heartbeatTimer = null;
let locationWatch = null;
let seq = 0;
let hasAutoCentered = false;

// Destination & Corridor State
let plannedDestination = null; // { lat, lng, name }
let plannedCorridorPoints = null; // [[lat, lng], ...]
let isPickingDestination = false;

// Motion Sensor & Panic State
let lastAcceleration = { x: 0, y: 0, z: 0 };
let shakeCount = 0;
let lastShakeTime = 0;
let panicCountdownTimer = null;
let panicRemainingSeconds = 5;

// Leaflet Map Initialization
const map = L.map('map', { zoomControl: false }).setView([20.2961, 85.8245], 14);
L.control.zoom({ position: 'bottomright' }).addTo(map);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors',
}).addTo(map);

// Map Layers
const route = L.polyline([], { color: '#e6ae3f', weight: 5 }).addTo(map);
let corridorBufferLayer = L.polyline([], { color: '#3b82f6', weight: 26, opacity: 0.16 }).addTo(map);
let corridorCenterLayer = L.polyline([], { color: '#2563eb', weight: 3, dashArray: '6, 8', opacity: 0.85 }).addTo(map);
let markers = [];
let destMarker = null;

function centerRoute(coordinates) {
  if (!coordinates.length) return;
  if (coordinates.length === 1) map.setView(coordinates[0], 15);
  else map.fitBounds(route.getBounds(), { padding: [35, 35] });
}

$('recenter-route').onclick = () => {
  const coords = route.getLatLngs();
  if (coords.length) centerRoute(coords);
  else if (plannedCorridorPoints && plannedCorridorPoints.length) {
    map.fitBounds(L.polyline(plannedCorridorPoints).getBounds(), { padding: [35, 35] });
  }
};

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
    } catch (e) {}
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

// -------------------------------------------------------------
// Destination Picking & Corridor Generation
// -------------------------------------------------------------
$('pick-dest-map').onclick = () => {
  closeModal();
  isPickingDestination = true;
  toast('Tap any location on the map to set your destination');
  map.getContainer().style.cursor = 'crosshair';

  map.once('click', async (e) => {
    isPickingDestination = false;
    map.getContainer().style.cursor = '';
    const destLat = e.latlng.lat;
    const destLng = e.latlng.lng;

    // Determine starting anchor point
    let startLat = 20.2961, startLng = 85.8245;
    if (navigator.geolocation && window.isSecureContext) {
      try {
        const pos = await getCurrentPosition();
        startLat = pos.coords.latitude;
        startLng = pos.coords.longitude;
      } catch (err) {}
    }

    // Generate corridor segments between start and destination
    plannedCorridorPoints = [
      [startLat, startLng],
      [startLat + (destLat - startLat) * 0.35, startLng + (destLng - startLng) * 0.25],
      [startLat + (destLat - startLat) * 0.70, startLng + (destLng - startLng) * 0.75],
      [destLat, destLng],
    ];

    plannedDestination = { lat: destLat, lng: destLng, name: $('destination-name').value || 'Selected Destination' };

    // Update map preview
    drawCorridor(plannedCorridorPoints);
    if (destMarker) map.removeLayer(destMarker);
    destMarker = L.circleMarker([destLat, destLng], { radius: 9, color: '#8b5cf6', fillOpacity: 0.9 })
      .addTo(map)
      .bindTooltip(`🏁 ${plannedDestination.name}`, { permanent: true })
      .openTooltip();

    map.fitBounds(L.polyline(plannedCorridorPoints).getBounds(), { padding: [40, 40] });

    if (!$('destination-name').value) {
      $('destination-name').value = `Dest (${destLat.toFixed(4)}, ${destLng.toFixed(4)})`;
    }
    openModal();
    toast('Destination corridor designated!');
  });
};

function drawCorridor(points) {
  if (!points || points.length < 2) {
    corridorBufferLayer.setLatLngs([]);
    corridorCenterLayer.setLatLngs([]);
    return;
  }
  corridorBufferLayer.setLatLngs(points);
  corridorCenterLayer.setLatLngs(points);
}

// -------------------------------------------------------------
// Start & Join Escort Sessions
// -------------------------------------------------------------
async function start() {
  try {
    const payload = {
      duration_minutes: Number($('duration').value),
      safe_pin: $('safe-pin').value,
      duress_pin: $('duress-pin').value,
      travel_mode: $('travel-mode').value,
      destination_name: $('destination-name').value.trim() || null,
      destination_lat: plannedDestination ? plannedDestination.lat : null,
      destination_lon: plannedDestination ? plannedDestination.lng : null,
      corridor_tolerance_m: Number($('corridor-tol').value),
      planned_corridor: plannedCorridorPoints,
      telegram_chat_id: $('telegram-chat-id').value.trim() || null,
    };

    const data = await api('/api/sessions', { method: 'POST', body: JSON.stringify(payload) });
    sessionId = data.session.id;
    hasAutoCentered = false;
    seq = 0;
    setSessionUrl(sessionId);
    closeModal();
    setState('ACTIVE');
    $('session-short').textContent = sessionId.slice(-10);
    toast('Escort started. Safe corridor & threat radar active.');
    armMotionGuard();
    render(data);
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
    toast('Joined escort session as Observer Guardian');
  } catch (error) {
    sessionId = null;
    toast(error.message);
  }
}
$('join-session').onclick = join;

// -------------------------------------------------------------
// Soft Warning / False Alarm Check-in
// -------------------------------------------------------------
$('confirm-check-in').onclick = async () => {
  if (!sessionId) return;
  try {
    await api(`/api/sessions/${sessionId}/check-in`, {
      method: 'POST',
      body: JSON.stringify({ acknowledged: true, reason: 'Intentional detour confirmed by traveler' }),
    });
    $('check-in-banner').classList.add('hidden');
    toast('Detour acknowledged as safe. False alarm cleared.');
    refresh();
  } catch (error) {
    toast(error.message);
  }
};

// -------------------------------------------------------------
// Mobile Device Motion / Shake & Fall Sensor Guard
// -------------------------------------------------------------
function armMotionGuard() {
  if (typeof window.DeviceMotionEvent !== 'undefined') {
    if (typeof DeviceMotionEvent.requestPermission === 'function') {
      DeviceMotionEvent.requestPermission()
        .then((state) => {
          if (state === 'granted') window.addEventListener('devicemotion', handleMotion);
        })
        .catch(() => {});
    } else {
      window.addEventListener('devicemotion', handleMotion);
    }
  }
}

function handleMotion(event) {
  if (!sessionId || currentRole !== 'traveler') return;
  const accel = event.accelerationIncludingGravity || event.acceleration;
  if (!accel || accel.x === null) return;

  const deltaX = Math.abs(accel.x - lastAcceleration.x);
  const deltaY = Math.abs(accel.y - lastAcceleration.y);
  const deltaZ = Math.abs(accel.z - lastAcceleration.z);
  const totalDelta = deltaX + deltaY + deltaZ;

  lastAcceleration = { x: accel.x, y: accel.y, z: accel.z };

  const now = Date.now();
  if (totalDelta > 22.0) {
    if (now - lastShakeTime < 1400) {
      shakeCount += 1;
      if (shakeCount >= 3) {
        shakeCount = 0;
        triggerPanicCountdown('SHAKE_PANIC');
      }
    } else {
      shakeCount = 1;
    }
    lastShakeTime = now;
  }
}

function triggerPanicCountdown(triggerType) {
  if (panicCountdownTimer) return; // Already active

  if (navigator.vibrate) {
    navigator.vibrate([200, 100, 200, 100, 400]);
  }

  panicRemainingSeconds = 5;
  $('panic-counter-num').textContent = panicRemainingSeconds;
  $('panic-overlay').classList.remove('hidden');

  panicCountdownTimer = setInterval(async () => {
    panicRemainingSeconds -= 1;
    $('panic-counter-num').textContent = panicRemainingSeconds;

    if (panicRemainingSeconds <= 0) {
      clearInterval(panicCountdownTimer);
      panicCountdownTimer = null;
      $('panic-overlay').classList.add('hidden');

      // Dispatch emergency panic distress to server
      try {
        let lat = null, lon = null;
        try {
          const pos = await getCurrentPosition();
          lat = pos.coords.latitude;
          lon = pos.coords.longitude;
        } catch (e) {}

        await api(`/api/sessions/${sessionId}/panic`, {
          method: 'POST',
          body: JSON.stringify({ trigger_type: triggerType, latitude: lat, longitude: lon }),
        });
        toast('🚨 EMERGENCY DISTRESS BROADCASTED');
        refresh();
      } catch (err) {
        toast(err.message);
      }
    }
  }, 1000);
}

$('cancel-panic-btn').onclick = () => {
  if (panicCountdownTimer) {
    clearInterval(panicCountdownTimer);
    panicCountdownTimer = null;
  }
  $('panic-overlay').classList.add('hidden');
  toast('Panic gesture cancelled (False Alarm dismissed)');
};

// Viva / Presentation Simulators
$('simulate-shake').onclick = () => {
  if (!sessionId) return toast('Start an escort session first');
  triggerPanicCountdown('SHAKE_PANIC');
};

$('simulate-detour').onclick = async () => {
  if (!sessionId) return toast('Start an escort session first');
  // Send point 350 meters off corridor
  const offLat = (plannedDestination ? plannedDestination.lat : 20.2961) + 0.0035;
  const offLng = (plannedDestination ? plannedDestination.lng : 85.8245) + 0.0035;
  toast('Sending detour point (350m off corridor)...');
  await sendPoint(offLat, offLng, { speed: 1.3, accuracy: 6 });
};

$('simulate-speed').onclick = async () => {
  if (!sessionId) return toast('Start an escort session first');
  // Send vehicular speed (12.5 m/s = 45 km/h) in walking mode
  const lat = (plannedDestination ? plannedDestination.lat : 20.2961) + 0.001;
  const lng = (plannedDestination ? plannedDestination.lng : 85.8245) + 0.001;
  toast('Simulating vehicular speed: 45 km/h in Walking mode...');
  await sendPoint(lat, lng, { speed: 12.5, accuracy: 5 });
};

// -------------------------------------------------------------
// Telemetry & GPS Tracking
// -------------------------------------------------------------
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
  toast('Live GPS tracking enabled');
}

$('use-location').onclick = () => {
  if (!sessionId) return toast('Start or join an escort first');
  if (!window.isSecureContext) return toast('Location requires an HTTPS link');
  trackLocation();
  armMotionGuard();
};

async function sendPoint(latitude, longitude, coords = {}, triggerType = 'NORMAL') {
  if (!sessionId) return;
  seq += 1;
  try {
    const res = await api(`/api/sessions/${sessionId}/telemetry`, {
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
        trigger_type: triggerType,
      }),
    });

    if (res.anomaly_flags && res.anomaly_flags.length) {
      toast(`Radar: ${res.anomaly_flags.join(', ')}`);
    }
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
      } catch (error) {}
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
    const corridor = plannedCorridorPoints || [
      [20.2961, 85.8245],
      [20.2980, 85.8270],
      [20.3000, 85.8295],
      [20.3020, 85.8320],
    ];

    for (let step = 0; step < corridor.length; step += 1) {
      const [latitude, longitude] = corridor[step];
      await sendPoint(latitude, longitude, { accuracy: 6, speed: 1.4 });
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
    toast('On-path simulation complete');
  } catch (error) {
    toast(error.message);
  }
};

async function stopSession(actionText) {
  if (!sessionId) return;
  const pin = await promptValue('Enter PIN');
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

// -------------------------------------------------------------
// UI Rendering & HUD Updates
// -------------------------------------------------------------
function render(data) {
  const session = data.session || {};
  const points = data.telemetry || [];
  const events = data.events || [];
  const alerts = data.alerts || [];
  const guardians = data.guardians || [];

  setState(session.status || 'NO SESSION');
  $('session-short').textContent = session.id ? session.id.slice(-10) : 'Waiting to begin';
  $('last-contact').textContent = formatTime(session.last_heartbeat);
  $('mode-display').textContent = session.travel_mode || 'WALKING';

  // 1. Sentinel Risk Meter & Threat Radar
  const riskScore = Number(session.current_risk_score || 0);
  const riskBar = $('risk-bar-fill');
  const riskPill = $('risk-pill');
  const hudLevel = $('hud-level');

  riskBar.style.width = `${Math.min(100, Math.max(0, riskScore))}%`;

  if (riskScore >= 70) {
    riskPill.className = 'risk-badge risk-critical';
    riskPill.textContent = `${riskScore}% CRITICAL`;
    riskBar.style.background = '#e7654f';
    hudLevel.textContent = 'CRITICAL ALERT';
    hudLevel.style.background = '#fde2dc';
    hudLevel.style.color = '#b82d1b';
  } else if (riskScore >= 35) {
    riskPill.className = 'risk-badge risk-elevated';
    riskPill.textContent = `${riskScore}% ELEVATED`;
    riskBar.style.background = '#e6ae3f';
    hudLevel.textContent = 'ELEVATED RISK';
    hudLevel.style.background = '#fdf3d8';
    hudLevel.style.color = '#9c6c10';
  } else {
    riskPill.className = 'risk-badge risk-normal';
    riskPill.textContent = `${riskScore}% SAFE`;
    riskBar.style.background = '#0d8179';
    hudLevel.textContent = 'NORMAL INVARIANTS';
    hudLevel.style.background = '#edf8f6';
    hudLevel.style.color = '#0d8179';
  }

  // 2. HUD Pills
  const lastPoint = points.length ? points[points.length - 1] : null;
  const deviationM = lastPoint ? Number(lastPoint.deviation_m || 0) : 0;
  $('deviation-stat').textContent = `${Math.round(deviationM)} m`;

  if (deviationM > (session.corridor_tolerance_m || 100)) {
    $('pill-corridor').textContent = `🛰️ Corridor: Off by ${Math.round(deviationM)}m`;
    $('pill-corridor').style.borderColor = '#e6ae3f';
  } else {
    $('pill-corridor').textContent = `🛰️ Corridor: In Bounds (${Math.round(deviationM)}m)`;
    $('pill-corridor').style.borderColor = '#e2e8f0';
  }

  const speedKmh = lastPoint && lastPoint.speed ? (lastPoint.speed * 3.6).toFixed(1) : '0.0';
  $('pill-speed').textContent = `⚡ Velocity: ${speedKmh} km/h`;

  // 3. Soft Warning Check-in Banner
  if (session.soft_warning_remaining !== null && session.soft_warning_remaining !== undefined && session.soft_warning_remaining > 0) {
    $('check-in-banner').classList.remove('hidden');
    $('check-in-countdown').textContent = session.soft_warning_remaining;
  } else {
    $('check-in-banner').classList.add('hidden');
  }

  // 4. Map Telemetry & Layers
  if (session.planned_corridor && session.planned_corridor.length) {
    plannedCorridorPoints = session.planned_corridor;
    drawCorridor(session.planned_corridor);
  }

  if (session.destination_lat && session.destination_lon && !destMarker) {
    destMarker = L.circleMarker([session.destination_lat, session.destination_lon], { radius: 9, color: '#8b5cf6', fillOpacity: 0.9 })
      .addTo(map)
      .bindTooltip(`🏁 ${session.destination_name || 'Destination'}`, { permanent: true })
      .openTooltip();
  }

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

  renderResponders(data);

  fetch(`/api/sessions/${session.id}/verify`).then((r) => r.json()).then((v) => {
    $('hash-status').textContent = v.valid ? 'verified (tamper-free)' : 'hash failed';
  }).catch(() => {});
}

function renderResponders(data) {
  const alert = data.alerts ? data.alerts[0] : null;
  if (!alert) return $('responders').innerHTML = '<p class="muted">No incident in progress.</p>';
  const current = alert.current_responder;
  $('responders').innerHTML = data.responders.map((responder, index) => {
    const notified = index <= current;
    const acknowledged = alert.acknowledged_by === responder.id;
    const action = currentRole === 'guardian' && !acknowledged && alert.status === 'ALERTING' && index === current
      ? `<button class="ack-button" onclick="ack('${alert.id}','${responder.id}')">Acknowledge</button>`
      : acknowledged ? '<span class="ack">ACK</span>' : '';
    return `<div class="responder"><span><strong>${responder.name}</strong><small>${responder.role} · ${notified ? 'Notified' : 'Queued'}</small></span>${action}</div>`;
  }).join('');
}

window.ack = async (alertId, responderId) => {
  try {
    await api(`/api/alerts/${alertId}/acknowledge`, { method: 'POST', body: JSON.stringify({ responder_id: responderId }) });
    toast('Responder acknowledgement recorded');
    refresh();
  } catch (error) {
    toast(error.message);
  }
};

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
  setRole(currentRole || 'traveler');
  refresh();
  startTimers();
} else {
  setRole('traveler');
  setState('NO SESSION');
}
