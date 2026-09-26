from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Protocol


class ContextProvider(Protocol):
    name: str

    def events_for(self, latitude: float, longitude: float, bearing: float | None) -> list[dict[str, Any]]:
        ...


def offset_location(latitude: float, longitude: float, distance_m: float, bearing: float) -> tuple[float, float]:
    angular_distance = distance_m / 6_371_000
    bearing_radians = math.radians(bearing)
    latitude_radians = math.radians(latitude)
    longitude_radians = math.radians(longitude)
    target_latitude = math.asin(
        math.sin(latitude_radians) * math.cos(angular_distance)
        + math.cos(latitude_radians) * math.sin(angular_distance) * math.cos(bearing_radians)
    )
    target_longitude = longitude_radians + math.atan2(
        math.sin(bearing_radians) * math.sin(angular_distance) * math.cos(latitude_radians),
        math.cos(angular_distance) - math.sin(latitude_radians) * math.sin(target_latitude),
    )
    return math.degrees(target_latitude), math.degrees(target_longitude)


class DemoContextProvider:
    name = "MOCK_DEMO"

    def events_for(self, latitude: float, longitude: float, bearing: float | None) -> list[dict[str, Any]]:
        heading = bearing if bearing is not None else 0.0
        timestamp = datetime.now(timezone.utc).isoformat()
        samples = [
            ("demo-weather", "SEVERE_WEATHER", "HIGH", 1_400, heading, "Synthetic severe-weather sample"),
            ("demo-transport", "TRANSPORT_DISRUPTION", "MEDIUM", 550, heading + 90, "Synthetic transport-disruption sample"),
            ("demo-incident", "SAFETY_INCIDENT", "MEDIUM", 1_100, heading + 180, "Synthetic nearby-incident sample"),
            ("demo-facility", "EMERGENCY_FACILITY", "INFO", 2_200, heading + 35, "Synthetic emergency-facility sample"),
        ]
        events = []
        for event_id, event_type, severity, distance, direction, description in samples:
            event_latitude, event_longitude = offset_location(latitude, longitude, distance, direction)
            events.append({
                "provider_event_id": event_id,
                "event_type": event_type,
                "severity": severity,
                "latitude": event_latitude,
                "longitude": event_longitude,
                "event_time": timestamp,
                "source": self.name,
                "description": description,
                "is_demo": True,
            })
        return events


class SafetyContextEngine:
    def __init__(self, radius_m: float = 5_000, route_corridor_m: float = 1_500):
        self.radius_m = radius_m
        self.route_corridor_m = route_corridor_m

    def evaluate(
        self,
        provider: ContextProvider,
        latitude: float,
        longitude: float,
        bearing: float | None,
        route: list[tuple[float, float]],
        distance_between: Any,
        bearing_between: Any,
    ) -> list[dict[str, Any]]:
        relevant = []
        for event in provider.events_for(latitude, longitude, bearing):
            event_latitude = float(event["latitude"])
            event_longitude = float(event["longitude"])
            distance = distance_between(latitude, longitude, event_latitude, event_longitude)
            route_distance = self._route_distance(event_latitude, event_longitude, route) if len(route) > 1 else None
            near_route = route_distance is not None and route_distance <= self.route_corridor_m
            if distance > self.radius_m and not near_route:
                continue
            direction = ""
            ahead = False
            if bearing is not None:
                event_bearing = bearing_between(latitude, longitude, event_latitude, event_longitude)
                angle = abs((event_bearing - bearing + 180) % 360 - 180)
                ahead = angle <= 70
                if ahead:
                    direction = " ahead"
            if near_route:
                explanation = f"Within {round(route_distance)} m of the planned route"
            else:
                explanation = f"Approximately {self._format_distance(distance)}{direction}; within the {self._format_distance(self.radius_m)} context radius"
            item = dict(event)
            item.update({
                "distance_m": round(distance, 1),
                "route_distance_m": round(route_distance, 1) if route_distance is not None else None,
                "ahead": ahead,
                "explanation": explanation,
            })
            relevant.append(item)
        return sorted(relevant, key=lambda event: (-self._severity_rank(event["severity"]), event["distance_m"]))

    @staticmethod
    def _format_distance(distance_m: float) -> str:
        return f"{distance_m / 1000:.1f} km" if distance_m >= 1000 else f"{round(distance_m)} m"

    @staticmethod
    def _severity_rank(severity: str) -> int:
        return {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}.get(severity.upper(), 0)

    @staticmethod
    def _route_distance(latitude: float, longitude: float, route: list[tuple[float, float]]) -> float:
        scale_x = 111_320 * math.cos(math.radians(latitude))
        scale_y = 111_320
        point_x, point_y = longitude * scale_x, latitude * scale_y
        minimum = float("inf")
        for start, end in zip(route, route[1:]):
            start_x, start_y = start[1] * scale_x, start[0] * scale_y
            end_x, end_y = end[1] * scale_x, end[0] * scale_y
            delta_x, delta_y = end_x - start_x, end_y - start_y
            length_squared = delta_x * delta_x + delta_y * delta_y
            fraction = 0 if not length_squared else max(0, min(1, ((point_x - start_x) * delta_x + (point_y - start_y) * delta_y) / length_squared))
            projected_x = start_x + fraction * delta_x
            projected_y = start_y + fraction * delta_y
            minimum = min(minimum, math.hypot(point_x - projected_x, point_y - projected_y))
        return minimum