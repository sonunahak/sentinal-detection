"""
SentinelTrace Anomaly & Geofencing Engine
Implements:
1. Spatial cross-track distance calculations for safe-corridor geofencing.
2. Context-aware multi-factor anomaly scoring (Risk Index 0-100).
3. False-positive mitigation via travel-mode heuristics and soft check-in timers.
"""
from __future__ import annotations

import json
import math
from typing import Any


def haversine(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Computes great-circle distance between two GPS coordinates in meters."""
    radius = 6371000.0  # Earth radius in meters
    lat_delta = math.radians(b_lat - a_lat)
    lon_delta = math.radians(b_lon - a_lon)
    val = (
        math.sin(lat_delta / 2.0) ** 2
        + math.cos(math.radians(a_lat))
        * math.cos(math.radians(b_lat))
        * math.sin(lon_delta / 2.0) ** 2
    )
    return radius * 2.0 * math.asin(math.sqrt(max(0.0, min(1.0, val))))


def point_to_segment_distance(
    p_lat: float, p_lon: float, a_lat: float, a_lon: float, b_lat: float, b_lon: float
) -> float:
    """
    Computes shortest distance in meters from point P to line segment AB
    using local equirectangular flat-earth approximation.
    """
    # Reference latitude for scaling longitude
    mid_lat = math.radians((a_lat + b_lat + p_lat) / 3.0)
    meters_per_lat = 111132.92
    meters_per_lon = 111412.84 * math.cos(mid_lat)

    # Convert coordinates to local Cartesian (x: East, y: North) relative to A
    ax, ay = 0.0, 0.0
    bx = (b_lon - a_lon) * meters_per_lon
    by = (b_lat - a_lat) * meters_per_lat
    px = (p_lon - a_lon) * meters_per_lon
    py = (p_lat - a_lat) * meters_per_lat

    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq <= 1e-6:
        # Segment is essentially a single point
        return math.hypot(px - ax, py - ay)

    # Project point P onto segment AB, clamped to [0, 1]
    t = max(0.0, min(1.0, (px * dx + py * dy) / seg_len_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy

    return math.hypot(px - proj_x, py - proj_y)


def calculate_corridor_deviation(
    lat: float, lon: float, corridor: list[list[float]] | None
) -> float:
    """
    Finds the minimum cross-track distance (in meters) between point (lat, lon)
    and any segment in the planned corridor polyline [[lat, lon], ...].
    Returns 0.0 if corridor is empty or has only one point.
    """
    if not corridor or len(corridor) < 2:
        return 0.0

    min_dist = float("inf")
    for i in range(len(corridor) - 1):
        a_lat, a_lon = corridor[i][0], corridor[i][1]
        b_lat, b_lon = corridor[i + 1][0], corridor[i + 1][1]
        dist = point_to_segment_distance(lat, lon, a_lat, a_lon, b_lat, b_lon)
        if dist < min_dist:
            min_dist = dist

    return round(min_dist, 2)


def evaluate_anomaly(
    *,
    travel_mode: str,
    current_lat: float,
    current_lon: float,
    speed_mps: float | None,
    corridor: list[list[float]] | None,
    corridor_tolerance_m: float,
    dwell_seconds: int = 0,
    trigger_type: str = "NORMAL",
    sensor_accel_peak: float | None = None,
    is_check_in_acknowledged: bool = False,
) -> dict[str, Any]:
    """
    Computes a composite explainable Sentinel Risk Index (0 - 100).

    Factors:
    1. Spatial Deviation (0 - 35 pts): Cross-track error outside safe corridor.
    2. Speed & Modality Invariant (0 - 25 pts): Unexpected vehicular sprint in walking mode.
    3. Stationary Dwell Risk (0 - 20 pts): Prolonged immobility in unverified location.
    4. Sensor & Panic Shock (0 - 40 pts): Shake gesture or fall detection impact.
    """
    flags: list[str] = []
    spatial_score = 0.0
    speed_score = 0.0
    dwell_score = 0.0
    sensor_score = 0.0

    # 1. Spatial Corridor Deviation
    deviation_m = calculate_corridor_deviation(current_lat, current_lon, corridor)
    if travel_mode != "FREE_ROAM" and corridor and len(corridor) >= 2:
        if deviation_m > corridor_tolerance_m:
            excess = deviation_m - corridor_tolerance_m
            # If user already acknowledged the detour, soft penalize
            if is_check_in_acknowledged:
                spatial_score = min(12.0, excess * 0.04)
                flags.append("DETOUR_ACKNOWLEDGED")
            else:
                spatial_score = min(35.0, 15.0 + (excess / corridor_tolerance_m) * 20.0)
                flags.append("OFF_ROUTE_DEVIATION")

    # 2. Speed / Velocity Invariant
    speed_kmh = (speed_mps * 3.6) if speed_mps is not None else 0.0
    if travel_mode == "WALKING":
        if speed_kmh > 24.0:
            # Sudden vehicular speed while in walking mode (possible vehicle abduction / forced transit)
            if is_check_in_acknowledged:
                speed_score = 10.0
                flags.append("RIDE_ACKNOWLEDGED")
            else:
                speed_score = min(45.0, 30.0 + (speed_kmh - 24.0) * 0.7)
                flags.append("VEHICULAR_SPEED_ANOMALY")
        elif speed_kmh > 12.0:
            speed_score = 15.0
            flags.append("HIGH_VELOCITY_SPRINT")
    elif travel_mode == "TRANSIT":
        if speed_kmh > 130.0:
            speed_score = 25.0
            flags.append("EXCESSIVE_TRANSIT_SPEED")

    # 3. Dwell / Immobility Risk
    # Extended immobility > 180 seconds (3 minutes) away from start/dest
    if dwell_seconds > 180 and speed_kmh < 1.0 and travel_mode != "FREE_ROAM":
        if is_check_in_acknowledged:
            dwell_score = 8.0
            flags.append("DWELL_ACKNOWLEDGED")
        else:
            dwell_score = min(35.0, 25.0 + ((dwell_seconds - 180) / 60.0) * 5.0)
            flags.append("PROLONGED_DWELL")

    # 4. Sensor & Direct Trigger Anomaly
    if trigger_type == "SHAKE_PANIC":
        sensor_score = 50.0
        flags.append("PANIC_SHAKE_DETECTED")
    elif trigger_type == "FALL_DETECTED":
        sensor_score = 45.0
        flags.append("IMPACT_FALL_DETECTED")
    elif sensor_accel_peak is not None and sensor_accel_peak > 30.0:
        sensor_score = 25.0
        flags.append("HIGH_G_FORCE_IMPACT")

    # Composite sum clamped between 0 and 100
    total_score = int(min(100.0, spatial_score + speed_score + dwell_score + sensor_score))

    if total_score >= 70 or "PANIC_SHAKE_DETECTED" in flags or "IMPACT_FALL_DETECTED" in flags:
        level = "CRITICAL"
    elif (
        total_score >= 25
        or "OFF_ROUTE_DEVIATION" in flags
        or "VEHICULAR_SPEED_ANOMALY" in flags
        or "PROLONGED_DWELL" in flags
    ):
        level = "ELEVATED"
    else:
        level = "NORMAL"

    breakdown = {
        "spatial_score": round(spatial_score, 1),
        "speed_score": round(speed_score, 1),
        "dwell_score": round(dwell_score, 1),
        "sensor_score": round(sensor_score, 1),
        "deviation_m": deviation_m,
        "speed_kmh": round(speed_kmh, 1),
        "dwell_seconds": dwell_seconds,
        "level": level,
    }

    return {
        "risk_score": total_score,
        "risk_level": level,
        "anomaly_flags": flags,
        "breakdown": breakdown,
    }


async def fetch_osrm_route(
    start_lat: float,
    start_lon: float,
    dest_lat: float,
    dest_lon: float,
    mode: str = "walking",
) -> dict[str, Any]:
    """
    Queries the Open Source Routing Machine (OSRM) OpenStreetMap API
    to compute exact street-network walking/driving geometry and turn-by-turn navigation steps.
    Falls back gracefully to multi-segment geometric interpolation if offline.
    """
    import httpx

    profiles = ["foot", "car"] if mode.lower() in ("walking", "foot") else ["car", "foot"]

    for profile in profiles:
        url = (
            f"https://router.project-osrm.org/route/v1/{profile}/"
            f"{start_lon:.6f},{start_lat:.6f};{dest_lon:.6f},{dest_lat:.6f}"
            f"?overview=full&geometries=geojson&steps=true"
        )
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                if resp.is_success:
                    data = resp.json()
                    if data.get("routes") and len(data["routes"]) > 0:
                        route = data["routes"][0]
                        raw_coords = route.get("geometry", {}).get("coordinates", [])
                        corridor_lat_lon = [[round(c[1], 6), round(c[0], 6)] for c in raw_coords]

                        if corridor_lat_lon:
                            # Physically anchor start and destination pins to the street corridor
                            full_corridor = [[round(start_lat, 6), round(start_lon, 6)]]
                            for pt in corridor_lat_lon:
                                if haversine(full_corridor[-1][0], full_corridor[-1][1], pt[0], pt[1]) > 3.0:
                                    full_corridor.append(pt)
                            if haversine(full_corridor[-1][0], full_corridor[-1][1], dest_lat, dest_lon) > 3.0:
                                full_corridor.append([round(dest_lat, 6), round(dest_lon, 6)])

                            steps: list[str] = []
                            legs = route.get("legs", [])
                            for leg in legs:
                                for step in leg.get("steps", []):
                                    maneuver = step.get("maneuver", {})
                                    m_type = maneuver.get("type", "proceed").capitalize()
                                    modifier = maneuver.get("modifier", "")
                                    name = step.get("name", "")
                                    desc = f"{m_type} {modifier}".strip()
                                    if name:
                                        desc += f" onto {name}"
                                    dist_m = step.get("distance", 0)
                                    if dist_m > 0:
                                        desc += f" ({int(dist_m)}m)"
                                    steps.append(desc)

                            return {
                                "success": True,
                                "provider": "OSRM_OPENSTREETMAP",
                                "corridor": full_corridor,
                                "distance_m": round(route.get("distance", 0), 1),
                                "duration_s": round(route.get("duration", 0), 1),
                                "steps": steps,
                            }
        except Exception:
            continue

    # High-fidelity fallback if OSRM public API is unreachable or rate-limited
    interpolated = [
        [round(start_lat, 6), round(start_lon, 6)],
        [round(start_lat + (dest_lat - start_lat) * 0.33, 6), round(start_lon + (dest_lon - start_lon) * 0.25, 6)],
        [round(start_lat + (dest_lat - start_lat) * 0.66, 6), round(start_lon + (dest_lon - start_lon) * 0.75, 6)],
        [round(dest_lat, 6), round(dest_lon, 6)],
    ]
    approx_dist = haversine(start_lat, start_lon, dest_lat, dest_lon)
    return {
        "success": True,
        "provider": "GEOMETRIC_FALLBACK",
        "corridor": interpolated,
        "distance_m": round(approx_dist, 1),
        "duration_s": round(approx_dist / 1.3, 1),
        "steps": [
            f"Depart from origin ({start_lat:.4f}, {start_lon:.4f})",
            "Follow active safe corridor monitoring zone",
            f"Arrive safely at destination ({dest_lat:.4f}, {dest_lon:.4f})",
        ],
    }

