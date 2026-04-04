"""
bokun.py — Bokun API client (read-only)

Fetches real-time pricing and product details for I Took a Tuk Tuk tours.

Requires in .env:
  BOKUN_ACCESS_KEY   — Bokun API access key
  BOKUN_SECRET_KEY   — Bokun API secret key

Note on availability: Bokun's availability calendar endpoint requires Channel Manager
API access. For now, availability is handled by directing clients to the booking page
where the Bokun widget shows live slots.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

BOKUN_BASE_URL = "https://api.bokun.io"

# Bokun experience IDs (confirmed from API)
TOUR_IDS: dict[str, int] = {
    "4hr-lisbon":          189265,  # True 4Hour Private Tuk Tuk Tour
    "fullday-lisbon":      347318,  # Full Day in Lisbon by Tuktuk
    "3hr-medieval-belem":  347299,  # 3-Hour Medieval Lisbon + Belém
    "3hr-medieval-boho":   347314,  # 3-Hour Medieval Lisbon + Bohemian
    "2hr-belem":           189262,  # 2-Hour Belém District
    "2hr-oldtown":         189264,  # 2-Hour Old Town
    "1hr-chiado":          189267,  # 1-Hour Chiado & Bairro Alto
    "sintra":              189268,  # Sintra & Cabo da Roca
    "arrabida":            189263,  # Arrábida & Blue Coast
    "fatima":              189269,  # Fátima & West Coast
    "porto":               189266,  # Porto Day Trip
    "templars":            843588,  # Templar Knights Route
}

BOOKING_URLS: dict[str, str] = {
    "4hr-lisbon":          "https://itookatuktuk.com/tuktuktours/lisbon-overview/",
    "fullday-lisbon":      "https://itookatuktuk.com/tuktuktours/discover-lisbons-charm/",
    "3hr-medieval-belem":  "https://itookatuktuk.com/tuktuktours/medieval-belem/",
    "3hr-medieval-boho":   "https://itookatuktuk.com/tuktuktours/medieval-bohemian/",
    "2hr-belem":           "https://itookatuktuk.com/tuktuktours/discovering-belem/",
    "2hr-oldtown":         "https://itookatuktuk.com/tuktuktours/medieval-lisbon/",
    "1hr-chiado":          "https://itookatuktuk.com/tuktuktours/bohemian-lisbon/",
    "sintra":              "https://itookatuktuk.com/tours-de-van/monumental-sintra/",
    "arrabida":            "https://itookatuktuk.com/tours-de-van/arrabida-setubal-and-the-blue-coast/",
    "fatima":              "https://itookatuktuk.com/tours-de-van/fatima-the-west-coast/",
    "porto":               "https://itookatuktuk.com/tours-de-van/porto-van-trip/",
    "templars":            "https://itookatuktuk.com/tours-de-van/templar-knights-route/",
}


# ---------------------------------------------------------------------------
# Auth — GET requests require query string in the signature
# ---------------------------------------------------------------------------

def _get_headers(method: str, path: str, params: dict | None = None) -> dict[str, str]:
    from datetime import datetime
    access_key = os.getenv("BOKUN_ACCESS_KEY", "")
    secret_key = os.getenv("BOKUN_SECRET_KEY", "")
    date_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if method.upper() == "GET" and params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        sig_path = f"{path}?{query}"
    else:
        sig_path = path
    message = f"{date_str}{access_key}{method.upper()}{sig_path}"
    sig = hmac.new(secret_key.encode(), message.encode(), hashlib.sha1).digest()
    return {
        "X-Bokun-Date": date_str,
        "X-Bokun-AccessKey": access_key,
        "X-Bokun-Signature": base64.b64encode(sig).decode(),
        "Content-Type": "application/json;charset=UTF-8",
    }


def _get(path: str, params: dict | None = None) -> Any:
    headers = _get_headers("GET", path, params)
    r = httpx.get(f"{BOKUN_BASE_URL}{path}", headers=headers, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict | None = None) -> Any:
    headers = _get_headers("POST", path)
    r = httpx.post(f"{BOKUN_BASE_URL}{path}", headers=headers, json=body or {}, timeout=10)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_product_info(tour_slug: str) -> dict:
    """
    Fetch current pricing and full product details for a tour.

    Returns title, duration, current price (EUR), inclusions, start times,
    max passengers per booking, and booking URL.
    """
    experience_id = TOUR_IDS.get(tour_slug)
    if not experience_id:
        return {"error": f"Unknown tour: {tour_slug}. Available: {list(TOUR_IDS.keys())}"}
    try:
        data = _get(f"/activity.json/{experience_id}", {"lang": "EN", "currency": "EUR"})
        price = data.get("nextDefaultPriceMoney", {})
        rates = data.get("rates", [])
        max_per_booking = max((r.get("maxPerBooking", 0) for r in rates), default=0)
        min_per_booking = min((r.get("minPerBooking", 1) for r in rates), default=1)
        start_times = [
            f"{t.get('hour', 0):02d}:{t.get('minute', 0):02d}"
            for t in data.get("startTimes", [])
        ]
        raw_included = data.get("included", [])
        if isinstance(raw_included, list):
            included = [i.get("text", "") if isinstance(i, dict) else str(i) for i in raw_included if i]
        else:
            included = [str(raw_included)] if raw_included else []
        return {
            "tour": tour_slug,
            "title": data.get("title", ""),
            "duration": data.get("durationText", ""),
            "price": price.get("amount"),
            "currency": price.get("currency", "EUR"),
            "price_text": data.get("nextDefaultPriceAsText", ""),
            "min_passengers": min_per_booking,
            "max_passengers_per_vehicle": max_per_booking,
            "start_times": start_times,
            "included": included,
            "booking_url": BOOKING_URLS.get(tour_slug, ""),
            "description": data.get("excerpt", ""),
        }
    except Exception as e:
        return {"error": str(e)}


def get_all_tour_prices() -> list[dict]:
    """
    Fetch current prices for all tours in one call (uses search endpoint).
    Returns a list with title, price, and booking URL for each tour.
    """
    try:
        data = _post("/activity.json/search", {"pageSize": 50, "currency": "EUR"})
        results = []
        slug_by_id = {v: k for k, v in TOUR_IDS.items()}
        for item in data.get("items", []):
            aid = item.get("id")
            slug = slug_by_id.get(aid, str(aid))
            results.append({
                "tour": slug,
                "title": item.get("title", ""),
                "price": item.get("price"),
                "duration": item.get("durationText", ""),
                "booking_url": BOOKING_URLS.get(slug, ""),
            })
        return results
    except Exception as e:
        return [{"error": str(e)}]
