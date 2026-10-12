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
            speed_score = 25.0
            flags.append("VEHICULAR_SPEED_ANOMALY")
        elif speed_kmh > 12.0:
            speed_score = 12.0
            flags.append("HIGH_VELOCITY_SPRINT")
    elif travel_mode == "TRANSIT":
        if speed_kmh > 130.0:
            speed_score = 15.0
            flags.append("EXCESSIVE_TRANSIT_SPEED")

    # 3. Dwell / Immobility Risk
    # Extended immobility > 180 seconds away from start/dest
    if dwell_seconds > 180 and speed_kmh < 1.0 and travel_mode != "FREE_ROAM":
        dwell_score = min(20.0, 5.0 + (dwell_seconds - 180) / 30.0)
        flags.append("PROLONGED_DWELL")

    # 4. Sensor & Direct Trigger Anomaly
    if trigger_type == "SHAKE_PANIC":
        sensor_score = 40.0
        flags.append("PANIC_SHAKE_DETECTED")
    elif trigger_type == "FALL_DETECTED":
        sensor_score = 35.0
        flags.append("IMPACT_FALL_DETECTED")
    elif sensor_accel_peak is not None and sensor_accel_peak > 30.0:
        sensor_score = 20.0
        flags.append("HIGH_G_FORCE_IMPACT")

    # Composite sum clamped between 0 and 100
    total_score = int(min(100.0, spatial_score + speed_score + dwell_score + sensor_score))

    if total_score >= 70 or "PANIC_SHAKE_DETECTED" in flags or "IMPACT_FALL_DETECTED" in flags:
        level = "CRITICAL"
    elif total_score >= 35 or "OFF_ROUTE_DEVIATION" in flags:
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
        "level": level,
    }

    return {
        "risk_score": total_score,
        "risk_level": level,
        "anomaly_flags": flags,
        "breakdown": breakdown,
    }
