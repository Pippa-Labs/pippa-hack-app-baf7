"""Tests for Last Minute Gift Finder.

Every request goes through `App._fetch`, which each test replaces with a stub
that answers from payloads shaped like the real Nominatim and Open-Meteo
replies. No test touches the network (conftest.py enforces that).
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from pippa_agentapp_sdk import (
    LocalRunner,
    OutputShapeViolation,
    RateLimitedError,
    ServiceUnavailableError,
)

import app as app_module
from app import (
    App,
    NoPlace,
    PlaceNotFound,
    match_interests,
    parse_opening_hours,
    shopping_window,
)

# Friday 20 November 2026, 15:00 in London (GMT, so the same as UTC).
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
LAT, LON = 51.5266694, -0.0798926

GEOCODE = [{
    "lat": str(LAT), "lon": str(LON), "class": "place", "type": "suburb",
    "display_name": "Shoreditch, London Borough of Hackney, Greater London, "
                    "England, EC2A 3AR, United Kingdom",
}]
TIMEZONE = {"latitude": 51.53, "longitude": -0.07, "timezone": "Europe/London"}


def shop(name, kind, dlat, hours=None, road="Brick Lane", number="1"):
    extratags = {"opening_hours": hours} if hours else {}
    return {
        "lat": str(LAT + dlat), "lon": str(LON), "category": "shop",
        "type": kind, "name": name, "extratags": extratags,
        "address": {"road": road, "house_number": number},
    }


SHOPS = {
    "books": [
        shop("Brick Lane Bookshop", "books", 0.006, "Mo-Su 10:00-18:00"),
        shop("Late Pages", "books", 0.002, "Mo-Sa 09:00-12:00; Su off"),
        shop("Kiosk Bookshop", "books", 0.004, "Th-Sa 12:00-18:00; Su-We off"),
        # A second branch further away: only the nearest one is listed.
        shop("Brick Lane Bookshop", "books", 0.012, "Mo-Su 10:00-18:00"),
    ],
    "coffee": [shop("Origin Coffee", "coffee", 0.001, "Mo-Fr 07:30-17:00; Sa 08:00-16:00")],
    "gift": [shop("Ufundi", "gift", 0.003)],  # no hours listed
    "florist": [shop("Flower Station", "florist", 0.009, "Mo-Su 09:00-21:00")],
}


def stub(shops=SHOPS, geocode=GEOCODE, fail_shops=None):
    """A `_fetch` that answers like the real services, and records the URLs."""
    seen: list[str] = []

    def fetch(url, timeout=6.0):
        seen.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.netloc == "api.open-meteo.com":
            return json.dumps(TIMEZONE).encode()
        q = query["q"][0]
        if q.startswith("[shop="):
            if fail_shops:
                raise fail_shops
            return json.dumps(shops.get(q[6:-1], [])).encode()
        return json.dumps(geocode).encode()

    fetch.seen = seen
    return fetch


def run(inputs, fetch=None, *, explicit=False):
    app = App()
    app._fetch = fetch or stub()
    app._sleep = lambda seconds: None
    return LocalRunner(app).call(
        "gift.shops_open", inputs, granted_secrets={}, now=NOW,
        request_id="test-1", explicit=explicit,
    ).result


# ── The manifest ───────────────────────────────────────────────────────────


def test_the_manifest_is_valid():
    app = App()
    assert app.manifest.app_id == "hack.app_baf7"
    assert [o.id for o in app.manifest.outcomes] == ["gift.shops_open"]


# ── A whole call ───────────────────────────────────────────────────────────


def test_it_lists_open_shops_that_suit_the_friend():
    result = run({"place": "Shoreditch", "interests": "books, coffee"})
    data = result.data
    assert data["place_found"] == "Shoreditch, London Borough of Hackney, Greater London"
    assert data["local_time"] == "Friday 15:00"
    assert data["checked_for"] == "the rest of the day and Saturday 21 November"
    # Open now, best of each kind first: the coffee shop is nearest.
    assert data["shops"][0].startswith("Origin Coffee (coffee shop, 110 m, 1 Brick Lane): "
                                       "open now until 17:00; Saturday 08:00-16:00")
    assert data["shops"][0].endswith("(suits: coffee)")
    # Then the best open bookshop, before the other bookshops.
    assert data["shops"][1].startswith("Kiosk Bookshop (bookshop")
    assert "open now until 18:00" in data["shops"][1]
    assert data["open_count"] == 4
    # Late Pages closed at noon but opens on Saturday, so it is still listed.
    assert any(s.startswith("Late Pages") and "closed for the rest of the day; "
               "Saturday 09:00-12:00" in s for s in data["shops"])
    # The chain's second branch is not listed twice.
    assert sum(s.startswith("Brick Lane Bookshop") for s in data["shops"]) == 1
    # A gift shop with no hours comes last, and says so.
    assert data["shops"][-1].startswith("Ufundi (gift shop")
    assert "opening hours not listed" in data["shops"][-1]
    assert data["hours_unknown_count"] == 1
    assert data["interests_not_matched"] == []
    assert "Shoreditch" in result.narrative


def test_tonight_leaves_out_shops_already_closed():
    data = run({"place": "Shoreditch", "interests": "books", "when": "tonight"}).data
    assert data["checked_for"] == "the rest of the day"
    names = [s.split(" (")[0] for s in data["shops"]]
    assert "Late Pages" not in names        # closed at 12:00
    assert "Brick Lane Bookshop" in names
    assert "Kiosk Bookshop" in names        # Friday 12:00-18:00
    assert data["closed_count"] == 1


def test_the_next_day_is_checked_on_its_own():
    data = run({"place": "Shoreditch", "interests": "books", "when": "tomorrow morning"}).data
    assert data["checked_for"] == "Saturday 21 November"
    late = next(s for s in data["shops"] if s.startswith("Late Pages"))
    assert "Saturday 09:00-12:00" in late
    assert "open now" not in late


def test_a_weekday_name_is_checked():
    data = run({"place": "Shoreditch", "interests": "books", "when": "Sunday"}).data
    assert data["checked_for"] == "Sunday 22 November"
    names = [s.split(" (")[0] for s in data["shops"]]
    assert "Late Pages" not in names        # Su off
    assert "Kiosk Bookshop" not in names    # Su-We off


def test_without_interests_it_looks_for_safe_last_minute_gifts():
    fetch = stub()
    data = run({"place": "Shoreditch"}, fetch).data
    looked_up = [parse_qs(urlparse(u).query)["q"][0] for u in fetch.seen
                 if "%5Bshop%3D" in u]
    assert looked_up == ["[shop=gift]", "[shop=chocolate]", "[shop=florist]", "[shop=books]"]
    assert "florist" in data["shop_kinds"]


def test_interests_with_no_shop_are_reported():
    data = run({"place": "Shoreditch", "interests": "coffee, crypto"}).data
    assert data["interests_not_matched"] == ["crypto"]


def test_no_shops_found_is_an_answer_not_an_error():
    result = run({"place": "Shoreditch", "interests": "books"}, stub(shops={}))
    assert result.data["shops"] == []
    assert result.data["open_count"] == 0
    assert "none of the kinds" in result.narrative


def test_the_shop_kinds_never_come_from_the_question():
    fetch = stub()
    run({"place": "Shoreditch", "interests": 'books"];out;'}, fetch)
    for url in fetch.seen:
        q = parse_qs(urlparse(url).query)["q"][0] if "nominatim" in url else ""
        if q.startswith("[shop="):
            assert q[6:-1] in app_module.SHOP_LABELS


# ── Refusals and failures ──────────────────────────────────────────────────


@pytest.mark.parametrize("place", ["", "near me", "  "])
def test_no_place_says_what_to_type(place):
    with pytest.raises(NoPlace) as failure:
        run({"place": place, "interests": "books"})
    assert "street" in failure.value.user_message


def test_a_place_that_does_not_exist_is_named():
    with pytest.raises(PlaceNotFound) as failure:
        run({"place": "Narnia"}, stub(geocode=[]))
    assert "Narnia" in failure.value.user_message


def test_picked_by_name_it_answers_and_refuses_as_usual():
    picked = run({"place": "Shoreditch", "interests": "books"}, explicit=True)
    assert picked.data == run({"place": "Shoreditch", "interests": "books"}).data
    with pytest.raises(NoPlace):
        run({"place": ""}, explicit=True)


def test_a_timeout_on_every_shop_lookup_is_retryable():
    with pytest.raises(ServiceUnavailableError):
        run({"place": "Shoreditch", "interests": "books"},
            stub(fail_shops=TimeoutError("read timed out")))


def test_rate_limited_is_retryable():
    error = urllib.error.HTTPError("https://nominatim.openstreetmap.org", 429,
                                   "Too Many Requests", {}, None)
    with pytest.raises(RateLimitedError):
        run({"place": "Shoreditch", "interests": "books"}, stub(fail_shops=error))


def test_geocoder_down_is_retryable():
    def fetch(url, timeout=6.0):
        raise urllib.error.URLError("no route")
    with pytest.raises(ServiceUnavailableError):
        run({"place": "Shoreditch"}, fetch)


def test_timezone_lookup_failing_falls_back_to_the_users_zone():
    base = stub()

    def fetch(url, timeout=6.0):
        if "open-meteo" in url:
            raise TimeoutError("slow")
        return base(url, timeout)

    data = run({"place": "Shoreditch", "interests": "coffee",
                "timezone": "Europe/London"}, fetch).data
    assert data["local_time"] == "Friday 15:00"


# ── The pieces ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, friday", [
    ("Mo-Sa 09:00-18:00, Su 10:00-16:00", [(540, 1080)]),
    ("Tu-Sa 10:00-18:00; Su 12:00-18:00", [(600, 1080)]),
    ("Mo-Fr 09:00-12:00,13:00-17:30; Sa 10:00-14:00; PH off", [(540, 720), (780, 1050)]),
    ("24/7", [(0, 1440)]),
    ("Mo-Su 18:00-02:00", [(1080, 1560)]),
    ("Mo,We,Fr 10:00-16:00", [(600, 960)]),
    ("Fr off", []),
])
def test_opening_hours_are_read(text, friday):
    assert parse_opening_hours(text)[4] == friday


@pytest.mark.parametrize("text", ["", "Dec 24 off", "sunrise-sunset", "by appointment"])
def test_opening_hours_it_cannot_read_are_unknown_not_guessed(text):
    assert parse_opening_hours(text) is None


def test_late_opening_runs_past_midnight():
    data = run({"place": "Shoreditch", "interests": "books", "when": "tonight"},
               stub(shops={"books": [shop("Night Owl", "books", 0.001, "Mo-Su 18:00-02:00")]})).data
    assert "opens later at 18:00 until 02:00" in data["shops"][0]


def test_interests_map_to_shop_kinds():
    kinds, unmatched = match_interests("books, specialty coffee and houseplants, crypto")
    assert list(kinds)[:3] == ["books", "coffee", "florist"]
    assert kinds["florist"] == ["houseplants"]
    assert unmatched == ["crypto"]


def test_bread_is_not_reading():
    assert "books" not in match_interests("bread")[0]


def test_the_window_follows_the_words():
    assert shopping_window("", NOW).include_rest_of_today
    assert shopping_window("tonight", NOW).day is None
    assert shopping_window("first thing tomorrow", NOW).include_rest_of_today is False
    assert shopping_window("friday", NOW).label == "the rest of Friday"


def test_the_runner_enforces_the_manifest(monkeypatch):
    from pippa_agentapp_sdk import Result

    monkeypatch.setattr(App, "handle", lambda self, c: Result(data={"surprise": "hello"}))
    with pytest.raises(OutputShapeViolation):
        run({"place": "Shoreditch"})
