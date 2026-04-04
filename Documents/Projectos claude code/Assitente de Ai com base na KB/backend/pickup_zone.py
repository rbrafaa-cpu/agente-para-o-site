"""
pickup_zone.py — Pickup zone detection and validation.

Detects location mentions in client queries, geocodes them via the Google
Geocoding API, and checks whether the resolved point falls inside the
defined pickup polygon.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

_ZONE_PATH = Path(__file__).parent / "config" / "pickup_zone.json"
_polygon: list[dict] | None = None


def _load_polygon() -> list[dict]:
    global _polygon
    if _polygon is None:
        with open(_ZONE_PATH, encoding="utf-8") as f:
            _polygon = json.load(f)["polygon"]
    return _polygon


# ---------------------------------------------------------------------------
# Point-in-polygon (ray casting)
# ---------------------------------------------------------------------------

def _point_in_polygon(lat: float, lng: float, polygon: list[dict]) -> bool:
    """Return True if (lat, lng) is inside the polygon."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]["lng"], polygon[i]["lat"]
        xj, yj = polygon[j]["lng"], polygon[j]["lat"]
        if ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------------------
# Location signal detection
# ---------------------------------------------------------------------------

_LOCATION_PATTERNS = [
    r"\bstaying at\b",
    r"\bstay(?:ing)?\s+in\b",
    r"\bwe['\s]re\s+at\b",
    r"\bwe\s+are\s+at\b",
    r"\bbased\s+at\b",
    r"\bour\s+hotel\b",
    r"\bour\s+airbnb\b",
    r"\bour\s+apartment\b",
    r"\bour\s+accommodation\b",
    r"\bpick\s*(?:us|me)\s*up\b",
    r"\bcollect\s*(?:us|me)\b",
    r"\bhotel\b",
    r"\bhostel\b",
    r"\bairbnb\b",
    r"\bapartamento\b",
    r"\brua\s+\w",
    r"\bavenida\s+\w",
    r"\btravessa\s+\w",
]


def needs_location_check(query: str) -> bool:
    """Return True if the query likely contains a location/address mention."""
    q = query.lower()
    return any(re.search(pat, q) for pat in _LOCATION_PATTERNS)


# ---------------------------------------------------------------------------
# Location extraction
# ---------------------------------------------------------------------------

_EXTRACTION_PATTERNS = [
    # "staying at [the] <location>"
    r"staying at (?:the )?(.+?)(?:\.|,|\?|$)",
    # "stay in [the] <location>"
    r"stay(?:ing)?\s+in (?:the )?(.+?)(?:\.|,|\?|$)",
    # "we're [staying] at [the] <location>"
    r"we['\s]re (?:staying )?at (?:the )?(.+?)(?:\.|,|\?|$)",
    # "we are [staying] at [the] <location>"
    r"we are (?:staying )?at (?:the )?(.+?)(?:\.|,|\?|$)",
    # "based at [the] <location>"
    r"based at (?:the )?(.+?)(?:\.|,|\?|$)",
    # "our hotel/airbnb/apartment is [the] <location>" or "at <location>"
    r"our (?:hotel|airbnb|apartment|hostel|accommodation) (?:is (?:the )?|at (?:the )?)(.+?)(?:\.|,|\?|$)",
    # "at [the] <name> Hotel"
    r"at (?:the )?([a-z0-9 '\-]+ hotel)(?:\s|,|\.|$)",
    # "[the] <name> Hotel" anywhere
    r"(?:^|[\s,])(?:the )?([a-z0-9 '\-]+ hotel)(?:\s|,|\.|$)",
    # "pick us up from [the] <location>"
    r"pick\s*(?:us|me)\s*up\s+from\s+(?:the )?(.+?)(?:\.|,|\?|$)",
    # "at Rua/Avenida/Travessa <name>"
    r"(?:at |in |from )?((?:rua|avenida|travessa|largo|praça|calcada)\s+[a-z0-9 ]+)(?:\.|,|\?|$)",
]


def extract_location(query: str) -> str | None:
    """
    Extract the specific location string from the query.
    Returns the location mention, or the full query as a fallback.
    """
    q_lower = query.lower()
    for pat in _EXTRACTION_PATTERNS:
        m = re.search(pat, q_lower, re.IGNORECASE)
        if m:
            loc = m.group(1).strip().rstrip(".,?! ")
            if len(loc) > 3:
                # Restore original casing from source query
                start = q_lower.find(loc)
                if start >= 0:
                    return query[start : start + len(loc)]
                return loc
    # Fallback: let the geocoder try the full query
    return query


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def _geocode(location: str) -> dict | None:
    """
    Geocode a location string using the Google Geocoding API.
    Returns the best result dict, or None on failure.
    """
    api_key = os.getenv("Google_Maps_API_Key") or os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return None

    address = f"{location}, Lisbon, Portugal"
    try:
        resp = httpx.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": api_key},
            timeout=5.0,
        )
        data = resp.json()
    except Exception:
        return None

    if data.get("status") != "OK" or not data.get("results"):
        return None

    return data["results"][0]


def _is_precise(result: dict) -> bool:
    """Return True if the geocoding result is precise enough to trust."""
    location_type = result.get("geometry", {}).get("location_type", "")
    if location_type == "APPROXIMATE":
        return False

    # Reject if bounding box spans more than ~2.5 km
    viewport = result.get("geometry", {}).get("viewport", {})
    ne = viewport.get("northeast", {})
    sw = viewport.get("southwest", {})
    if ne and sw:
        lat_span = abs(ne.get("lat", 0) - sw.get("lat", 0))
        lng_span = abs(ne.get("lng", 0) - sw.get("lng", 0))
        if lat_span > 0.025 or lng_span > 0.025:
            return False

    return True


# ---------------------------------------------------------------------------
# Main zone check
# ---------------------------------------------------------------------------

def check_zone(location: str) -> dict:
    """
    Geocode the location and check against the pickup polygon.

    Returns:
        {
            "status": "inside" | "outside" | "unclear",
            "resolved_address": str
        }
    """
    result = _geocode(location)

    if result is None or not _is_precise(result):
        return {"status": "unclear", "resolved_address": location}

    geo = result["geometry"]["location"]
    lat, lng = geo["lat"], geo["lng"]
    resolved = result.get("formatted_address", location)

    inside = _point_in_polygon(lat, lng, _load_polygon())

    return {
        "status": "inside" if inside else "outside",
        "resolved_address": resolved,
    }


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_pickup_context(location: str, status: str, resolved_address: str) -> str:
    """Return a context string to inject into the RAG prompt."""
    if status == "inside":
        return (
            f"PICKUP ZONE CHECK: '{location}' resolved to '{resolved_address}'. "
            f"Status: INSIDE the standard pickup zone. Confirm pickup is available at no charge."
        )
    if status == "outside":
        return (
            f"PICKUP ZONE CHECK: '{location}' resolved to '{resolved_address}'. "
            f"Status: OUTSIDE the standard pickup zone. The client should meet us at our standard "
            f"meeting point: Avenida da Liberdade, nº3 (across from the Hard Rock Café)."
        )
    # unclear
    return (
        f"PICKUP ZONE CHECK: The location '{location}' could not be resolved precisely. "
        f"Ask the client to provide their full hotel name or exact street address so we can confirm."
    )
