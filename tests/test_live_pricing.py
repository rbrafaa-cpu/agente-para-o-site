"""Offline tests for backend/live_pricing.py — no Bókun calls."""

from datetime import date, datetime

import pytest

from backend import live_pricing as lp

TODAY = date(2026, 11, 10)  # a Tuesday


@pytest.mark.parametrize("text,expected", [
    ("how much is the 4 hour tour", ["4hr-lisbon"]),
    ("price of your best seller?", ["4hr-lisbon"]),
    ("full day tuk tuk please", ["fullday-lisbon"]),
    ("the 3 hour tour with Belém", ["3hr-medieval-belem"]),
    ("3h bohemian", ["3hr-medieval-boho"]),
    ("the 3 hour tour", ["3hr-medieval-belem", "3hr-medieval-boho"]),
    ("2 hour tour of belem", ["2hr-belem"]),
    ("2-hour old town", ["2hr-oldtown"]),
    ("the two hour tour", ["2hr-belem", "2hr-oldtown"]),
    ("one hour tour", ["1hr-chiado"]),
    ("Sintra trip", ["sintra"]),
    ("day trip to Porto", ["porto"]),
    ("Templar knights", ["templars"]),
    ("Fátima tour", ["fatima"]),
    ("what tours do you have?", []),
    ("we are 2 people", []),
])
def test_detect_tours(text, expected):
    assert lp.detect_tours(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("for tomorrow", date(2026, 11, 11)),
    ("amanhã de manhã", date(2026, 11, 11)),
    ("mañana", date(2026, 11, 11)),
    ("por la mañana", None),
    ("mañana por la mañana", date(2026, 11, 11)),
    ("demain", date(2026, 11, 11)),
    ("day after tomorrow", date(2026, 11, 12)),
    ("today", TODAY),
    ("on Friday", date(2026, 11, 13)),
    ("next Tuesday", date(2026, 11, 17)),
    ("sexta-feira", date(2026, 11, 13)),
    ("12 Nov", date(2026, 11, 12)),
    ("12th of November", date(2026, 11, 12)),
    ("November 20th", date(2026, 11, 20)),
    ("Dec 3", date(2026, 12, 3)),
    ("3 de dezembro", date(2026, 12, 3)),
    ("15 janvier", date(2027, 1, 15)),
    ("5 March 2027", date(2027, 3, 5)),
    ("1 Nov", date(2027, 11, 1)),  # passed this year → next year
    ("2026-11-20", date(2026, 11, 20)),
    ("20/11", date(2026, 11, 20)),
    ("11/20", date(2026, 11, 20)),  # impossible as d/m → read as m/d
    ("on the 14th", date(2026, 11, 14)),
    ("the 2nd", date(2026, 12, 2)),
    ("Quinta da Regaleira is it included?", None),
    ("is the sun out in Lisbon", None),
    ("the 4 hour tour", None),
    ("how much is it", None),
])
def test_detect_date(text, expected):
    assert lp.detect_date(text, TODAY) == expected


@pytest.mark.parametrize("text,expected", [
    ("we are 4", 4),
    ("2 people", 2),
    ("for two", 2),
    ("for 3 please", 3),
    ("2 adults and 2 kids", 4),
    ("2 adults and 1 teenager", 3),
    ("family of five", 5),
    ("me and my wife", 2),
    ("just me", 1),
    ("somos 6", 6),
    ("4 pessoas", 4),
    ("nous sommes 3", 3),
    ("for 4 hours", None),
    ("for 2 nights", None),
    ("for sept 12", None),
    ("for 12/11", None),
    ("group of 10", 10),
    ("how much is the tour", None),
])
def test_detect_pax(text, expected):
    assert lp.detect_pax(text) == expected


def test_follow_up_keeps_tour_and_pax():
    history = [
        {"role": "user", "content": "How much is the 4 hour tour for 2 people?"},
        {"role": "assistant", "content": "The 4-Hour tour for 2 is €260.00."},
    ]
    req = lp.resolve_request("and for tomorrow?", history, TODAY)
    assert req.tours == ["4hr-lisbon"]
    assert req.pax == 2
    assert req.day == date(2026, 11, 11)
    assert req.asked and req.tour_from_history


def test_tour_from_bot_recommendation():
    history = [
        {"role": "user", "content": "What do you recommend?"},
        {"role": "assistant", "content": "Our best seller is the 4-hour tour: [Book here](x)"},
    ]
    req = lp.resolve_request("how much for Friday?", history, TODAY)
    assert req.tours == ["4hr-lisbon"]
    assert req.day == date(2026, 11, 13)


def test_unrelated_question_does_not_look_up():
    assert not lp.resolve_request("Is it wheelchair accessible?", [], TODAY).asked


LADDER_6 = {1: 215.0, 2: 112.5, 3: 75.0, 4: 58.0, 5: 47.0, 6: 40.0}


def test_cheapest_split():
    # totals: 1=215, 2=225, 3=225, 4=232, 5=235, 6=240
    total, sizes = lp.cheapest_split(LADDER_6, 10, 6)
    assert total == min(
        LADDER_6[a] * a + LADDER_6[10 - a] * (10 - a) for a in range(4, 7)
    )
    assert sum(sizes) == 10 and len(sizes) == 2


def _row(day_ms, time, seats, tiers, unavailable=False):
    return {
        "date": day_ms, "startTime": time, "availabilityCount": seats,
        "unavailable": unavailable, "soldOut": False,
        "pricesByRate": [{"activityRateId": 1, "pricePerCategoryUnit": [
            {"id": 9, "amount": {"amount": a, "currency": "EUR"},
             "minParticipantsRequired": n, "maxParticipantsRequired": n} for n, a in tiers.items()
        ]}],
    }




def test_parse_slots_and_totals(monkeypatch):
    rows = [
        _row(1794441600000, "09:00", 12, {1: 240.0, 2: 130.0}),
        _row(1794441600000, "10:00", 0, {1: 240.0, 2: 130.0}),
        _row(1794441600000, "11:00", 12, {1: 240.0, 2: 130.0}, unavailable=True),
    ]
    slots = lp.parse_slots(rows)
    assert [s.time for s in slots] == ["09:00"]
    assert slots[0].day == date(2026, 11, 12)
    assert slots[0].total(2) == 260.0


def test_context_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise lp.BokunUnavailable("down")
    monkeypatch.setattr(lp, "fetch_availability", boom)
    req = lp.PricingRequest(tours=["4hr-lisbon"], day=date(2026, 11, 12), pax=2, asked=True)
    ctx = lp.build_live_context(req, datetime(2026, 11, 10, 12, 0, tzinfo=lp.LISBON))
    assert "could not be reached" in ctx
    assert "€" not in ctx


def test_context_no_slots_that_day(monkeypatch):
    def fake(bokun_id, start, end):
        if start == date(2026, 11, 11):
            return []
        return [_row(1794441600000, "09:00", 12, {1: 240.0, 2: 130.0})]
    monkeypatch.setattr(lp, "fetch_availability", fake)
    req = lp.PricingRequest(tours=["4hr-lisbon"], day=date(2026, 11, 11), pax=2, asked=True)
    ctx = lp.build_live_context(req, datetime(2026, 11, 10, 12, 0, tzinfo=lp.LISBON))
    assert "NO start times can be booked online" in ctx
    assert "Thu 12 Nov" in ctx
    assert "TOTAL for 2 passengers: €260.00" in ctx
