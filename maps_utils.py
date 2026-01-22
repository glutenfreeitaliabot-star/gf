from typing import List, Optional, Tuple, Callable
from urllib.parse import quote
import sqlite3

NormalizeCoordsFn = Callable[[object, object], Tuple[Optional[float], Optional[float]]]

def _maps_coord(lat: float, lon: float) -> str:
    return f"{lat:.6f},{lon:.6f}"

def build_google_maps_multi_url(
    rows: List[sqlite3.Row],
    normalize_coords_fn: NormalizeCoordsFn,
    user_location: Optional[Tuple[float, float]] = None,
    limit: int = 10,
    travelmode: str = "walking",
) -> Optional[str]:
    """
    Genera un link Google Maps Directions con più tappe (gratis, niente API key).
    - rows: lista ristoranti (serve lat/lon)
    - normalize_coords_fn: la tua _normalize_coords
    - user_location: (lat, lon) se disponibile (per "Vicino a me")
    - limit: massimo tappe incluse (consigliato 10-20)
    """
    coords = []
    for r in rows:
        lat, lon = normalize_coords_fn(r["lat"], r["lon"])
        if lat is None or lon is None:
            continue
        coords.append((lat, lon))
        if len(coords) >= limit:
            break

    if not coords:
        return None

    destination = _maps_coord(coords[-1][0], coords[-1][1])
    waypoints = coords[:-1]

    params = ["api=1", f"destination={quote(destination)}"]

    if user_location:
        origin = _maps_coord(user_location[0], user_location[1])
        params.append(f"origin={quote(origin)}")

    if waypoints:
        wp = "|".join(_maps_coord(lat, lon) for lat, lon in waypoints)
        params.append(f"waypoints={quote(wp)}")

    params.append(f"travelmode={quote(travelmode)}")

    return "https://www.google.com/maps/dir/?" + "&".join(params)
