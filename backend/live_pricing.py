"""
live_pricing.py — Live Bókun prices and availability for the chat assistant.

Every tour price the assistant quotes comes from here, fetched from Bókun at
question time. There is no local price table to fall back on: if Bókun cannot
be reached, the context block says so and the model is told to quote nothing.
(Until 2026-11-10 prices came from a hand-typed pricing.json whose "winter"
rows were ~25% below what Bókun actually charged.)

Flow, per chat turn:
  1. resolve_request()  — read tour(s), date and group size from the current
                          message, falling back to earlier messages so a
                          follow-up like "and for tomorrow?" keeps its tour.
  2. build_live_context() — fetch Bókun availability for that tour/date and
                          format exact totals for the model.

Bókun pricing model (verified 2026-11-10): every tour has one rate, priced per
person with a tier per exact group size (1 pax €240 pp, 2 pax €130 pp, ...).
The total is tier × pax. No seasons, no daily overrides.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

LISBON = ZoneInfo("Europe/Lisbon")
BOKUN_BASE_URL = "https://api.bokun.io"
CACHE_TTL_SECONDS = 300
LOOKAHEAD_DAYS = 14  # window searched when the client gives no date


# ---------------------------------------------------------------------------
# Tours
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Tour:
    slug: str
    bokun_id: int
    name: str
    booking_url: str
    vehicle: str  # "tuktuk" | "van"


TOURS: dict[str, Tour] = {t.slug: t for t in [
    Tour("4hr-lisbon", 189265, "4-Hour Tuk Tuk Tour — Discover Lisbon with a Local (best seller)",
         "https://itookatuktuk.com/tuktuktours/lisbon-overview/", "tuktuk"),
    Tour("fullday-lisbon", 347318, "Full Day Tuk Tuk Tour",
         "https://itookatuktuk.com/tuktuktours/discover-lisbons-charm/", "tuktuk"),
    Tour("3hr-medieval-belem", 347299, "3-Hour Tour — Medieval Lisbon + Belém",
         "https://itookatuktuk.com/tuktuktours/medieval-belem/", "tuktuk"),
    Tour("3hr-medieval-boho", 347314, "3-Hour Tour — Medieval Lisbon + Bohemian Districts",
         "https://itookatuktuk.com/tuktuktours/medieval-bohemian/", "tuktuk"),
    Tour("2hr-belem", 189262, "2-Hour Tour — Belém District",
         "https://itookatuktuk.com/tuktuktours/discovering-belem/", "tuktuk"),
    Tour("2hr-oldtown", 189264, "2-Hour Tour — Old Town",
         "https://itookatuktuk.com/tuktuktours/medieval-lisbon/", "tuktuk"),
    Tour("1hr-chiado", 189267, "1-Hour Tour — Chiado & Bairro Alto",
         "https://itookatuktuk.com/tuktuktours/bohemian-lisbon/", "tuktuk"),
    Tour("sintra", 189268, "Sintra & Cabo da Roca — Private Van Tour",
         "https://itookatuktuk.com/tours-de-van/monumental-sintra/", "van"),
    Tour("arrabida", 189263, "Arrábida, Setúbal & the Blue Coast — Private Van Tour",
         "https://itookatuktuk.com/tours-de-van/arrabida-setubal-and-the-blue-coast/", "van"),
    Tour("fatima", 189269, "Fátima & the West Coast — Private Van Tour",
         "https://itookatuktuk.com/tours-de-van/fatima-the-west-coast/", "van"),
    Tour("porto", 189266, "Porto — Private Day Trip from Lisbon",
         "https://itookatuktuk.com/tours-de-van/porto-van-trip/", "van"),
    Tour("templars", 843588, "Templar Knights Route — Almourol & Tomar",
         "https://itookatuktuk.com/tours-de-van/templar-knights-route/", "van"),
]}

_H = r"(?:h\b|hr?s?\b|hours?\b|horas?\b|heures?\b|-?hour\b)"


def _dur(n: str) -> str:
    """Regex for 'N hour' in its common spellings: 4h, 4 hr, 4-hour, four hours."""
    words = {"1": "one|uma|una|une", "2": "two|duas|dos|deux", "3": "three|tr[eê]s",
             "4": "four|quatro|cuatro|quatre", "8": "eight|oito|ocho|huit"}[n]
    return rf"\b(?:{n}|{words})\s*-?\s*{_H}"


# Checked in order; a message can match several tours.
_TOUR_PATTERNS: list[tuple[str, str]] = [
    ("fullday-lisbon", r"full[\s-]?day|all[\s-]day|whole day|dia inteiro|d[ií]a completo|journ[ée]e compl|" + _dur("8")),
    ("4hr-lisbon", _dur("4") + r"|best[\s-]?seller|lisbon overview|half[\s-]day|meio dia|medio d[ií]a|demi[\s-]journ"),
    ("3hr-medieval-belem", r"(?:" + _dur("3") + r").{0,60}bel[eé]m|bel[eé]m.{0,60}(?:" + _dur("3") + r")"),
    ("3hr-medieval-boho", r"(?:" + _dur("3") + r").{0,60}(?:bohemian|boho|bo[eé]mi)|(?:bohemian|boho).{0,60}(?:" + _dur("3") + r")"),
    ("2hr-belem", r"(?:" + _dur("2") + r").{0,60}bel[eé]m|bel[eé]m.{0,60}(?:" + _dur("2") + r")"),
    ("2hr-oldtown", r"(?:" + _dur("2") + r").{0,60}(?:old town|medieval|alfama)|(?:old town|alfama).{0,60}(?:" + _dur("2") + r")"),
    ("1hr-chiado", _dur("1") + r"|chiado|bairro alto|express tour"),
    ("sintra", r"sintra|cabo da roca|pena palace"),
    ("arrabida", r"arr[aá]bida|set[uú]bal|blue coast"),
    ("fatima", r"f[aá]tima|west coast|nazar[eé]|[oó]bidos"),
    ("porto", r"\bo?porto\b"),
    ("templars", r"templ[aá]r|tomar|almourol"),
]

# Bare durations that fit two tours each ("the 3 hour tour").
_AMBIGUOUS_DURATIONS = [
    (_dur("3"), ["3hr-medieval-belem", "3hr-medieval-boho"]),
    (_dur("2"), ["2hr-belem", "2hr-oldtown"]),
]


def detect_tours(text: str) -> list[str]:
    t = text.lower()
    found = [slug for slug, pat in _TOUR_PATTERNS if re.search(pat, t)]
    for pat, slugs in _AMBIGUOUS_DURATIONS:
        if re.search(pat, t) and not any(s in found for s in slugs):
            found.extend(slugs)
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTHS = {
    1: "jan|january|janeiro|enero|janvier", 2: "feb|february|fev|fevereiro|febrero|f[ée]vr|f[ée]vrier",
    3: "mar|march|mar[cç]o|marzo|mars", 4: "apr|april|abr|abril|avr|avril",
    5: "may|mai|maio|mayo", 6: "jun|june|junho|junio|juin",
    7: "jul|july|julho|julio|juil|juillet", 8: "aug|august|ago|agosto|ao[uû]t",
    9: "sep|sept|september|set|setembro|septiembre|septembre", 10: "oct|october|out|outubro|octubre|octobre",
    11: "nov|november|novembro|noviembre|novembre", 12: "dec|december|dez|dezembro|dic|diciembre|d[ée]c|d[ée]cembre",
}
_MONTH_RE = "|".join(f"(?P<m{n}>{alts})" for n, alts in _MONTHS.items())

# Full names only: "sun", "sat" and "mon" are ordinary words, and Portuguese
# weekdays need "-feira" ("quinta" alone is also Quinta da Regaleira).
_WEEKDAYS = {
    0: r"monday|segunda[\s-]feira|lunes|lundi", 1: r"tuesday|ter[cç]a[\s-]feira|martes|mardi",
    2: r"wednesday|quarta[\s-]feira|mi[eé]rcoles|mercredi", 3: r"thursday|quinta[\s-]feira|jueves|jeudi",
    4: r"friday|sexta[\s-]feira|viernes|vendredi", 5: r"saturday|s[aá]bado|samedi",
    6: r"sunday|domingo|dimanche",
}

_DAY_AFTER_TOMORROW = r"day after tomorrow|depois de amanh[aã]|pasado ma[nñ]ana|apr[eè]s[\s-]demain"
_TOMORROW = r"\btomorrow\b|\btmrw\b|\bamanh[aã]\b|\bma[nñ]ana\b|\bdemain\b"
_TODAY = r"\btoday\b|\btonight\b|\bthis (?:morning|afternoon|evening)\b|\bhoje\b|\bhoy\b|aujourd'?hui|\bce soir\b"


def _month_of(m: re.Match) -> int:
    return next(n for n in _MONTHS if m.group(f"m{n}"))


def _future(day: int, month: int, today: date, year: int | None = None) -> date | None:
    """Build a date; with no year, roll to next year if the day has passed."""
    try:
        if year:
            return date(year if year > 100 else 2000 + year, month, day)
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def detect_date(text: str, today: date) -> date | None:
    t = text.lower()

    if re.search(_DAY_AFTER_TOMORROW, t):
        return today + timedelta(days=2)
    # "por la mañana" is Spanish for "in the morning", not tomorrow.
    if re.search(_TOMORROW, re.sub(r"(?:por|de|en|esta) la ma[nñ]ana", " ", t)):
        return today + timedelta(days=1)
    if re.search(_TODAY, t):
        return today

    m = re.search(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        return _future(int(m.group(3)), int(m.group(2)), today, int(m.group(1)))

    # 12 nov / 12th of November / 12 de novembro / 12 novembre
    m = re.search(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th|º|°)?\s*(?:of\s+|de\s+)?(?:{_MONTH_RE})\b\.?"
                  rf"(?:\s*(?:,|de)?\s*(?P<y>20\d\d))?", t)
    if not m:
        # November 12 / Nov 12th, 2027
        m = re.search(rf"\b(?:{_MONTH_RE})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b(?:\s*,?\s*(?P<y>20\d\d))?", t)
    if m:
        return _future(int(m.group("d")), _month_of(m), today, int(m.group("y")) if m.group("y") else None)

    # 12/11, 12.11.2026 — European day/month unless that is impossible.
    m = re.search(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        day, month = (b, a) if b > 12 and a <= 12 else (a, b)
        if 1 <= month <= 12:
            return _future(day, month, today, year)

    for wd, pat in _WEEKDAYS.items():
        if m := re.search(rf"\b(?:{pat})", t):
            ahead = (wd - today.weekday()) % 7
            said_next = re.search(r"(?:next|pr[oó]xim[oa]|pr[oó]chain)\s*$", t[:m.start()])
            if ahead == 0 and (said_next or re.search(r"next week|semana que vem|semaine prochaine", t)):
                ahead = 7
            return today + timedelta(days=ahead)

    # "on the 14th"
    m = re.search(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", t)
    if m:
        day = int(m.group(1))
        month = today.month if day >= today.day else today.month % 12 + 1
        year = today.year if month >= today.month else today.year + 1
        return _future(day, month, today, year)

    return None


# ---------------------------------------------------------------------------
# Group size
# ---------------------------------------------------------------------------

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "tres": 3, "quatro": 4, "cinco": 5,
    "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
    "una": 1, "dos": 2, "cuatro": 4, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
    "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
}
_N = r"(\d{1,3}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"
_PEOPLE = (r"(?:people|persons?|pax|adults?|guests?|passengers?|travell?ers?|of us|ppl|"
           r"pessoas?|adultos?|personas?|personnes?|adultes?|viajantes?|passageiros?)")
_KIDS = r"(?:kids?|children|child|teens?|teenagers?|crian[cç]as?|ni[nñ]os?|enfants?|filhos?|hijos?)"


def _num(s: str) -> int:
    return int(s) if s.isdigit() else _NUM_WORDS[s]


def detect_pax(text: str) -> int | None:
    t = text.lower()
    adults = [_num(m.group(1)) for m in re.finditer(rf"\b{_N}\s+{_PEOPLE}\b", t)]
    kids = [_num(m.group(1)) for m in re.finditer(rf"\b{_N}\s+(?:\w+\s+)?{_KIDS}\b", t)]
    if adults or kids:
        return sum(adults) + sum(kids) or None

    m = re.search(rf"\b(?:we are|we're|there are|there will be|group of|family of|party of|"
                  rf"somos|nous sommes|on est|grupo de|groupe de|fam[ií]lia de|familia de)\s+{_N}\b", t)
    if m:
        return _num(m.group(1))
    m = re.search(rf"\b(?:for|para|pour)\s+{_N}\b(?!\s*(?:h\b|hrs?\b|hours?|horas?|heures?|days?|dias?|"
                  rf"d[ií]as|jours?|nights?|noites?|noches?|nuits?|weeks?|pm\b|am\b|[:.h/]?\d|euros?|€|min|"
                  rf"{_MONTH_RE}))", t)
    if m:
        return _num(m.group(1))
    if re.search(r"\b(?:a couple|my (?:wife|husband|partner|girlfriend|boyfriend|fianc[eé]e?)|"
                 r"the two of us|both of us|honeymoon)\b", t):
        return 2
    if re.search(r"\b(?:just me|only me|solo|alone|by myself|travelling alone|traveling alone|sozinh[oa])\b", t):
        return 1
    return None


# ---------------------------------------------------------------------------
# Conversation → request
# ---------------------------------------------------------------------------

_PRICE_WORDS = re.compile(
    r"price|cost|how much|quanto|cu[aá]nto|combien|prix|pre[cç]o|precio|tarif|rate\b|fee\b|"
    r"cheap|expensive|budget|afford|€|eur\b|euro|pay\b|total|quote", re.I)
_AVAIL_WORDS = re.compile(
    r"availab|free slot|spots?|book|reserv|open\b|sold out|full\b|dispon|vagas?|plazas?|"
    r"places?\b|what time|which time|times\b|slots?|start|hor[aá]rio|horaire", re.I)


@dataclass
class PricingRequest:
    tours: list[str] = field(default_factory=list)
    day: date | None = None
    pax: int | None = None
    asked: bool = False          # current message is about price/availability/date/size/tour
    tour_from_history: bool = False


def resolve_request(message: str, history: list[dict] | None, today: date) -> PricingRequest:
    """
    Work out tour, date and group size for this turn.

    Each slot comes from the current message if it is there, otherwise from
    the most recent earlier user message that has it. A tour can also come
    from the bot's last reply when that reply was about one or two tours,
    so "how much is it for Friday?" after a recommendation still resolves.
    """
    req = PricingRequest()
    req.tours = detect_tours(message)
    req.day = detect_date(message, today)
    req.pax = detect_pax(message)
    req.asked = bool(
        req.tours or req.day or req.pax
        or _PRICE_WORDS.search(message) or _AVAIL_WORDS.search(message)
    )

    earlier = list(reversed(history or []))
    user_msgs = [h["content"] for h in earlier if h.get("role") == "user" and isinstance(h.get("content"), str)]
    if not req.tours:
        for msg in user_msgs:
            if tours := detect_tours(msg):
                req.tours, req.tour_from_history = tours, True
                break
    if not req.tours:
        last_bot = next((h["content"] for h in earlier if h.get("role") == "assistant"), "")
        if isinstance(last_bot, str):
            tours = detect_tours(last_bot)
            if 1 <= len(tours) <= 2:
                req.tours, req.tour_from_history = tours, True
    if req.day is None:
        for msg in user_msgs:
            if d := detect_date(msg, today):
                req.day = d
                break
    if req.pax is None:
        for msg in user_msgs:
            if p := detect_pax(msg):
                req.pax = p
                break
    return req


# ---------------------------------------------------------------------------
# Bókun API
# ---------------------------------------------------------------------------

class BokunUnavailable(RuntimeError):
    pass


def _signed_headers(path_with_query: str) -> dict[str, str]:
    access_key = os.getenv("BOKUN_ACCESS_KEY", "")
    secret_key = os.getenv("BOKUN_SECRET_KEY", "")
    if not access_key or not secret_key:
        raise BokunUnavailable("BOKUN_ACCESS_KEY / BOKUN_SECRET_KEY not set")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sig = hmac.new(secret_key.encode(), f"{stamp}{access_key}GET{path_with_query}".encode(), hashlib.sha1).digest()
    return {
        "X-Bokun-Date": stamp,
        "X-Bokun-AccessKey": access_key,
        "X-Bokun-Signature": base64.b64encode(sig).decode(),
    }


_cache: dict[tuple, tuple[float, list]] = {}
_cache_lock = threading.Lock()


def fetch_availability(bokun_id: int, start: date, end: date) -> list[dict]:
    """Raw Bókun availability rows (one per date × start time) for a range."""
    key = (bokun_id, start, end)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    # The signature covers the query string, so build it in a fixed order.
    query = f"start={start.isoformat()}&end={end.isoformat()}&currency=EUR&lang=EN"
    path = f"/activity.json/{bokun_id}/availabilities?{query}"
    try:
        r = httpx.get(BOKUN_BASE_URL + path, headers=_signed_headers(path), timeout=8)
        r.raise_for_status()
        rows = r.json()
    except BokunUnavailable:
        raise
    except Exception as e:
        raise BokunUnavailable(f"Bókun request failed for {bokun_id}: {e}") from e
    if not isinstance(rows, list):
        raise BokunUnavailable(f"Unexpected Bókun response for {bokun_id}")
    with _cache_lock:
        _cache[key] = (now, rows)
    return rows


@dataclass
class Slot:
    day: date
    time: str
    seats: int
    per_person: dict[int, float]  # group size → price per person (EUR)

    def total(self, pax: int) -> float | None:
        pp = self.per_person.get(pax)
        return round(pp * pax, 2) if pp is not None else None

    @property
    def max_pax(self) -> int:
        return max(self.per_person) if self.per_person else 0


def parse_slots(rows: list[dict]) -> list[Slot]:
    """Bookable slots only, with the per-person tier ladder of the first rate."""
    slots = []
    for row in rows:
        if row.get("unavailable") or row.get("soldOut"):
            continue
        seats = row.get("availabilityCount")
        if not isinstance(seats, int) or seats <= 0:
            continue
        rates = row.get("pricesByRate") or []
        if not rates:
            continue
        ladder: dict[int, float] = {}
        for unit in rates[0].get("pricePerCategoryUnit") or []:
            lo, hi = unit.get("minParticipantsRequired"), unit.get("maxParticipantsRequired")
            amount = (unit.get("amount") or {}).get("amount")
            if (unit.get("amount") or {}).get("currency", "EUR") != "EUR" or amount is None or not lo:
                continue
            for n in range(int(lo), int(hi or lo) + 1):
                ladder[n] = float(amount)
        if not ladder:
            continue
        day = datetime.fromtimestamp(row["date"] / 1000, tz=timezone.utc).date()
        slots.append(Slot(day=day, time=row.get("startTime", ""), seats=seats, per_person=ladder))
    slots.sort(key=lambda s: (s.day, s.time))
    return slots


def cheapest_split(ladder: dict[int, float], pax: int, max_per_booking: int) -> tuple[float, list[int]] | None:
    """
    Cheapest way to seat `pax` across bookings of 1..max_per_booking each.
    Returns (total, [group size per booking]) or None if a size is unpriced.
    """
    best: list[tuple[float, list[int]] | None] = [(0.0, [])] + [None] * pax
    for n in range(1, pax + 1):
        for size in range(1, min(n, max_per_booking) + 1):
            prev = best[n - size]
            if prev is None or size not in ladder:
                continue
            cand = (round(prev[0] + ladder[size] * size, 2), prev[1] + [size])
            if best[n] is None or (cand[0], len(cand[1])) < (best[n][0], len(best[n][1])):
                best[n] = cand
    if best[pax] is None:
        return None
    total, sizes = best[pax]
    return total, sorted(sizes, reverse=True)


# ---------------------------------------------------------------------------
# Context block for the model
# ---------------------------------------------------------------------------

def _eur(x: float) -> str:
    return f"€{x:,.2f}"


def _day(d: date) -> str:
    return d.strftime("%A %-d %B %Y")


def _ladder_line(ladder: dict[int, float], upto: int) -> str:
    return " | ".join(f"{n} pax: {_eur(ladder[n] * n)}" for n in range(1, upto + 1) if n in ladder)


def _price_lines(slot: Slot, pax: int | None) -> list[str]:
    max_pb = slot.max_pax
    lines = []
    if pax:
        if pax <= max_pb:
            lines.append(f"  TOTAL for {pax} passenger{'s' if pax > 1 else ''}: {_eur(slot.total(pax))} (one booking)")
        else:
            split = cheapest_split(slot.per_person, pax, max_pb)
            if split:
                total, sizes = split
                lines.append(
                    f"  {pax} passengers is more than one booking allows (max {max_pb}). "
                    f"Cheapest split: {len(sizes)} bookings of {' + '.join(map(str, sizes))} passengers "
                    f"= {_eur(total)} TOTAL ("
                    + ", ".join(f"{s} pax {_eur(slot.total(s))}" for s in sizes) + ")"
                )
    lines.append(f"  Total price by group size (whole group, one booking): {_ladder_line(slot.per_person, min(max_pb, 8))}")
    if max_pb > 8:
        lines.append(f"  (one booking takes up to {max_pb} passengers; ask before quoting sizes above 8)")
    return lines


def _tour_block(tour: Tour, req: PricingRequest, today: date) -> list[str]:
    head = [f"• {tour.name}", f"  Booking link: [Book here]({tour.booking_url})"]

    if req.day:
        if req.day < today:
            return head + [f"  {_day(req.day)} is in the past."]
        slots = parse_slots(fetch_availability(tour.bokun_id, req.day, req.day))
        if req.pax:
            slots = [s for s in slots if s.seats >= req.pax]
        if slots:
            times = ", ".join(s.time for s in slots)
            return head + [f"  {_day(req.day)}: bookable start times {times}"] + _price_lines(slots[0], req.pax)

        # Nothing bookable that day: say so, and give the next open dates.
        after = parse_slots(fetch_availability(tour.bokun_id, req.day + timedelta(days=1),
                                               req.day + timedelta(days=LOOKAHEAD_DAYS)))
        if req.pax:
            after = [s for s in after if s.seats >= req.pax]
        next_days = sorted({s.day for s in after})[:4]
        group = f" for {req.pax} passengers" if req.pax else ""
        lines = head + [f"  {_day(req.day)}: NO start times can be booked online that day{group} "
                        f"(fully booked, not running, or too close to book online)."]
        if next_days:
            lines.append("  Next dates with open times: " + ", ".join(d.strftime("%a %-d %b") for d in next_days))
            lines += _price_lines(next(s for s in after if s.day == next_days[0]), req.pax)
            lines.append(f"  (prices above are for {_day(next_days[0])})")
        return lines

    slots = parse_slots(fetch_availability(tour.bokun_id, today, today + timedelta(days=LOOKAHEAD_DAYS)))
    if not slots:
        return head + [f"  No dates can be booked online in the next {LOOKAHEAD_DAYS} days. Do not quote a price."]
    first = slots[0]
    same = all(s.per_person == first.per_person for s in slots)
    note = ("same price on every date checked" if same
            else "price differs between dates — quote only with a date")
    return head + [
        f"  No date given. Prices for the next open date, {_day(first.day)} ({note}, next {LOOKAHEAD_DAYS} days):"
    ] + _price_lines(first, req.pax)


def build_live_context(req: PricingRequest, now: datetime | None = None) -> str:
    """
    Format live Bókun data for the tours in `req`, or all tours when none were
    named. Never raises: on any Bókun failure the block tells the model to
    quote no price at all.
    """
    now = now or datetime.now(LISBON)
    today = now.date()
    tours = [TOURS[s] for s in req.tours] or list(TOURS.values())

    header = [
        "--- LIVE BÓKUN PRICES & AVAILABILITY ---",
        f"Fetched from the booking system {now.strftime('%Y-%m-%d %H:%M')} Lisbon time. "
        f"Today is {_day(today)}; tomorrow is {_day(today + timedelta(days=1))}.",
        "These are the ONLY prices and times you may quote. Every amount is the TOTAL for the whole group in EUR.",
        "Copy amounts exactly. Do not calculate, round, estimate or adjust them.",
    ]
    asked = []
    if req.day:
        asked.append(f"date {_day(req.day)}")
    if req.pax:
        asked.append(f"{req.pax} passengers")
    if req.tour_from_history:
        asked.append("tour carried over from earlier in the chat")
    if asked:
        header.append("Client asked about: " + ", ".join(asked) + ".")
    if not req.tours:
        header.append("No specific tour named, so all tours are listed.")
    header.append("")

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            blocks = list(pool.map(lambda t: _tour_block(t, req, today), tours))
    except Exception as e:  # BokunUnavailable or anything unexpected
        print(f"[live_pricing] {e}")
        return unavailable_context()

    body = []
    for b in blocks:
        body += b + [""]
    return "\n".join(header + body + ["--- END LIVE BÓKUN DATA ---"])


def unavailable_context() -> str:
    return (
        "--- LIVE BÓKUN PRICES & AVAILABILITY ---\n"
        "The booking system could not be reached for this message. You have NO price or availability data.\n"
        "Do not quote, estimate or recall any price, total or start time, including figures from earlier "
        "in the chat or from the knowledge base. Say prices and times are shown live on the booking page "
        "and give the booking link.\n"
        "--- END LIVE BÓKUN DATA ---"
    )


NO_LOOKUP_CONTEXT = (
    "--- LIVE BÓKUN PRICES & AVAILABILITY ---\n"
    "No price lookup was made for this message. If the client asks about a price, total or start time, "
    "do not answer from memory or the knowledge base: give the booking link, or ask which tour, date and "
    "group size they mean.\n"
    "--- END LIVE BÓKUN DATA ---"
)


def live_context_for(message: str, history: list[dict] | None) -> str:
    """Entry point used by rag.build_request()."""
    now = datetime.now(LISBON)
    req = resolve_request(message, history, now.date())
    if not req.asked:
        return NO_LOOKUP_CONTEXT
    return build_live_context(req, now)
