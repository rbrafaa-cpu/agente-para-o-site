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

# A location is only worth geocoding when the query actually names one.
# A bare "hotel" is not enough — "recommend a hotel there" and "hotel pickup"
# both contain it without naming anywhere.
_LOCATION_PATTERNS = [
    r"\bstaying at\b",
    r"\bstay(?:ing)?\s+in\b",
    r"\bwe['\s]re\s+at\b",
    r"\bwe\s+are\s+at\b",
    r"\bbased\s+at\b",
    r"\bour\s+hotel\b",
    r"\bour\s+hostel\b",
    r"\bour\s+airbnb\b",
    r"\bour\s+apartment\b",
    r"\bour\s+accommodation\b",
    r"\bpick\s*(?:us|me)\s*up\b",
    r"\bcollect\s*(?:us|me)\b",
    r"\brua\s+\w",
    r"\bavenida\s+\w",
    r"\btravessa\s+\w",
    r"\blargo\s+\w",
    r"\bpra[cç]a\s+\w",
    r"\bcal[cç]ada\s+\w",
]

# "Hotel Avenida Palace" names a place; "a hotel there" does not. The
# difference is a proper noun immediately after, so this one runs against the
# original casing rather than the lowercased query.
_ACCOMMODATION_PROPER = re.compile(
    r"\b(?:hotel|hostel|pousada|residencial|guest\s?house)\s+(?=[A-Z0-9])"
)


def needs_location_check(query: str) -> bool:
    """Return True if the query actually names a location worth geocoding."""
    q = query.lower()
    if any(re.search(pat, q) for pat in _LOCATION_PATTERNS):
        return True
    return bool(_ACCOMMODATION_PROPER.search(query))


# ---------------------------------------------------------------------------
# Location extraction
# ---------------------------------------------------------------------------

# Matched against the ORIGINAL casing: "Hotel" plus the proper nouns and
# Portuguese particles that follow it.
_PROPER_NAME_RE = re.compile(
    r"\b((?:Hotel|Hostel|Pousada|Residencial)"
    r"(?:\s+(?:[A-Z][\w'\-]*|d[aeo]s?|e))+)"
)

# Every pattern is bounded so a match cannot swallow the whole sentence.
_EXTRACTION_PATTERNS = [
    # street addresses — most specific, so they win over the phrasal patterns
    r"((?:rua|avenida|travessa|largo|pra[cç]a|cal[cç]ada|estrada)"
    r"\s+[\w'\-]+(?:\s+[\w'\-]+){0,3})",
    # "<name> Hotel" — at most four words of name
    r"(?:at|in|from)\s+(?:the\s+)?((?:[\w'\-]+\s+){1,4}hotel)\b",
    r"staying at (?:the )?(.+?)(?:\.|,|\?|$)",
    r"stay(?:ing)?\s+in (?:the )?(.+?)(?:\.|,|\?|$)",
    r"we['\s]re (?:staying )?at (?:the )?(.+?)(?:\.|,|\?|$)",
    r"we are (?:staying )?at (?:the )?(.+?)(?:\.|,|\?|$)",
    r"based at (?:the )?(.+?)(?:\.|,|\?|$)",
    r"our (?:hotel|airbnb|apartment|hostel|accommodation)"
    r"(?:\s+is)?(?:\s+(?:at|in))?\s+(?:the )?(.+?)(?:\.|,|\?|$)",
    # "pick us up from/at/in <place>" — not just "from"
    r"(?:pick\s*(?:us|me)\s*up|collect\s*(?:us|me))"
    r"\s+(?:from|at|in|near|outside)\s+(?:the )?(.+?)(?:\.|,|\?|$)",
]

# Politeness and filler that the geocoder should never see.
_TRAILING_FILLER = re.compile(
    r"\s+(?:please|thanks|thank\s+you|ok|okay|cheers)\b.*$", re.IGNORECASE
)


def extract_location(query: str) -> str | None:
    """
    Extract the specific location string from the query.
    Returns the location mention, or the full query as a fallback.
    """
    # A named accommodation wins outright — it is the most precise signal.
    m = _PROPER_NAME_RE.search(query)
    if m:
        return _TRAILING_FILLER.sub("", m.group(1)).strip()

    q_lower = query.lower()
    for pat in _EXTRACTION_PATTERNS:
        m = re.search(pat, q_lower, re.IGNORECASE)
        if m:
            loc = _TRAILING_FILLER.sub("", m.group(1)).strip().rstrip(".,?! ")
            if len(loc) > 3:
                # Restore original casing from source query
                start = q_lower.find(loc)
                if start >= 0:
                    return query[start : start + len(loc)]
                return loc
    # Fallback: let the geocoder try the full query. Safe now that the gate
    # above only admits queries that genuinely name a place.
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
