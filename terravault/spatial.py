"""Small dependency-free spatial helpers used for catalogue selection."""

from __future__ import annotations

from typing import Any, Sequence


def _ring_area(ring: Sequence[Sequence[float]]) -> float:
    if len(ring) < 3:
        return 0.0
    twice_area = 0.0
    for current, following in zip(ring, (*ring[1:], ring[0]), strict=True):
        twice_area += float(current[0]) * float(following[1])
        twice_area -= float(following[0]) * float(current[1])
    return abs(twice_area) / 2


def _polygon_area(coordinates: Sequence[Any]) -> float:
    if not coordinates:
        return 0.0
    exterior = _ring_area(coordinates[0])
    holes = sum(_ring_area(ring) for ring in coordinates[1:])
    return max(0.0, exterior - holes)


def geometry_area(geometry: dict[str, Any] | None) -> float:
    """Return planar polygon area for relative footprint comparisons.

    The result is in squared coordinate units. TerraVault uses it only to
    compare WGS84 footprints belonging to the same MGRS tile, where a planar
    approximation is sufficient and avoids a heavyweight geometry dependency.
    """

    if not geometry:
        return 0.0
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)):
        return 0.0
    if geometry_type == "Polygon":
        return _polygon_area(coordinates)
    if geometry_type == "MultiPolygon":
        return sum(_polygon_area(polygon) for polygon in coordinates)
    return 0.0
