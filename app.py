"""Last Minute Gift Finder.

Given a place and what a friend likes, list nearby shops that suit them and
say whether each is open tonight or the next day.

How one call goes:

1. Nominatim (OpenStreetMap) turns the place into coordinates.
2. Open-Meteo says which time zone those coordinates are in, so "open
   until 18:00" is read in the shop's own clock, not the server's.
3. Nominatim lists shops of each kind that suits the friend's interests
   (at most MAX_KINDS kinds, a second apart), with their `opening_hours`.
4. The opening hours are parsed here (a sensible subset of the OSM syntax)
   and checked against the shopping window.

The app returns facts: names, kinds, distances, hours. Pippa does the
recommending out loud.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    RateLimitedError,
    Result,
    ServiceUnavailableError,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

#: Nominatim answers 403 without a descriptive User-Agent.
USER_AGENT = "pippa-agent-app-gift-finder/0.1 (https://github.com/Pippa-Labs)"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
TIMEZONE_URL = "https://api.open-meteo.com/v1/forecast"
# Overpass would answer in one request, but its public servers were measured
# answering 502/504 or taking 20-30 s (November 2026). Nominatim answers each
# kind of shop in well under a second, so it is one request per kind instead.

#: How far from the place to look, as a box of this half-width.
SEARCH_RADIUS_M = 1500
MAX_SHOPS = 8
#: At most this many kinds of shop are looked up per call.
MAX_KINDS = 6
#: Nominatim's usage policy: no more than one request a second.
NOMINATIM_GAP_S = 1.0
#: Total budget for all requests. The platform kills the call at 20 s.
BUDGET_S = 16.0

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday"]

# ── What a friend likes → which OSM shop kinds suit them ───────────────────

#: Keyword (matched against words in the interests) → shop=* values.
INTEREST_SHOPS: dict[str, tuple[str, ...]] = {
    "book": ("books",), "reading": ("books",), "read": ("books",),
    "novel": ("books",), "poetry": ("books",), "literature": ("books",),
    "comic": ("books", "comics"), "manga": ("books", "comics", "anime"),
    "anime": ("anime", "comics"),
    "coffee": ("coffee",), "tea": ("tea",),
    "flower": ("florist",), "plant": ("florist", "garden_centre"),
    "garden": ("garden_centre", "florist"),
    "chocolate": ("chocolate", "confectionery"),
    "sweet": ("confectionery", "chocolate"), "candy": ("confectionery",),
    "cake": ("pastry", "bakery"), "baking": ("kitchen", "pastry"),
    "wine": ("wine", "alcohol"), "whisky": ("alcohol", "wine"),
    "whiskey": ("alcohol", "wine"), "gin": ("alcohol", "wine"),
    "beer": ("beverages", "alcohol"), "cocktail": ("alcohol",),
    "drink": ("alcohol", "wine"),
    "cheese": ("cheese", "deli"), "food": ("deli", "cheese"),
    "foodie": ("deli", "cheese"), "cooking": ("kitchen", "deli"),
    "cook": ("kitchen", "deli"), "chef": ("kitchen", "deli"),
    "game": ("games", "video_games"), "gaming": ("video_games", "games"),
    "music": ("music", "musical_instrument"), "vinyl": ("music",),
    "record": ("music",), "guitar": ("musical_instrument",),
    "art": ("art", "craft", "frame"), "drawing": ("art", "craft"),
    "painting": ("art", "craft"), "craft": ("craft",),
    "knitting": ("fabric", "craft"), "sewing": ("fabric", "craft"),
    "fashion": ("clothes", "boutique"), "clothes": ("clothes", "boutique"),
    "style": ("clothes", "boutique"), "shoe": ("shoes",),
    "sneaker": ("shoes",), "trainer": ("shoes",),
    "jewellery": ("jewelry",), "jewelry": ("jewelry",),
    "beauty": ("cosmetics", "perfumery"), "makeup": ("cosmetics",),
    "skincare": ("cosmetics",), "perfume": ("perfumery",),
    "candle": ("candles", "interior_decoration"),
    "home": ("interior_decoration", "houseware"),
    "interior": ("interior_decoration",), "decor": ("interior_decoration",),
    "tech": ("electronics", "computer"), "gadget": ("electronics",),
    "electronics": ("electronics",), "computer": ("computer",),
    "sport": ("sports",), "fitness": ("sports",), "running": ("sports",),
    "gym": ("sports",), "football": ("sports",), "cycling": ("bicycle",),
    "bike": ("bicycle",), "hiking": ("outdoor",), "camping": ("outdoor",),
    "outdoors": ("outdoor",), "climbing": ("outdoor", "sports"),
    "toy": ("toys",), "lego": ("toys",), "kid": ("toys",),
    "stationery": ("stationery",), "journaling": ("stationery",),
    "writing": ("stationery", "books"), "pen": ("stationery",),
    "photography": ("photo",), "camera": ("photo",),
    "pet": ("pet",), "dog": ("pet",), "cat": ("pet",),
    "vintage": ("second_hand", "antiques"), "antique": ("antiques",),
    "travel": ("outdoor", "books"),
}

#: Always worth a look for a last-minute present.
GENERIC_SHOPS = ("gift",)
#: Used when nothing the friend likes could be matched.
FALLBACK_SHOPS = ("gift", "chocolate", "florist", "books")

SHOP_LABELS = {
    "books": "bookshop", "comics": "comic shop", "anime": "anime shop",
    "coffee": "coffee shop", "tea": "tea shop", "florist": "florist",
    "garden_centre": "garden centre", "chocolate": "chocolatier",
    "confectionery": "sweet shop", "pastry": "patisserie", "bakery": "bakery",
    "wine": "wine shop", "alcohol": "off-licence", "beverages": "drinks shop",
    "cheese": "cheese shop", "deli": "deli", "kitchen": "kitchenware shop",
    "games": "games shop", "video_games": "video games shop",
    "music": "record shop", "musical_instrument": "music instrument shop",
    "art": "art shop", "craft": "craft shop", "frame": "framing shop",
    "fabric": "fabric shop", "clothes": "clothes shop",
    "boutique": "boutique", "shoes": "shoe shop", "jewelry": "jeweller",
    "cosmetics": "beauty shop", "perfumery": "perfumery",
    "candles": "candle shop", "interior_decoration": "homeware shop",
    "houseware": "homeware shop", "electronics": "electronics shop",
    "computer": "computer shop", "sports": "sports shop",
    "bicycle": "bike shop", "outdoor": "outdoor shop", "toys": "toy shop",
    "stationery": "stationer", "photo": "photo shop", "pet": "pet shop",
    "second_hand": "vintage shop", "antiques": "antiques shop",
    "gift": "gift shop",
}


# ── Errors ──────────────────────────────────────────────────────────────────


class NoPlace(AgentAppError):
    user_message = (
        "Tell me where to look, such as a street, an area, a town or a "
        "postcode, and I will find gift shops close to it."
    )
    retryable = False


class PlaceNotFound(AgentAppError):
    user_message = "I could not find that place."
    retryable = False


# ── Opening hours ───────────────────────────────────────────────────────────

#: weekday (0 = Monday) → list of (start, end) in minutes after midnight.
#: `end` may pass 1440 for hours that run past midnight.
Week = dict[int, list[tuple[int, int]]]

_DAY = r"(?:Mo|Tu|We|Th|Fr|Sa|Su|PH|SH)"
_DAY_LIST = re.compile(rf"^({_DAY}(?:\s*[-,]\s*{_DAY})*)\b\s*(.*)$")
#: "Mo-Sa 09:00-18:00, Su 10:00-16:00": a comma after a time, then a day,
#: starts a new rule.
_RULE_SPLIT = re.compile(rf";|\|\||(?<=\d)\s*,\s*(?={_DAY}\b)")
_SPAN = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def _days(spec: str) -> list[int] | None:
    """`Mo-Fr,Su` → [0, 1, 2, 3, 4, 6]. Public holiday tokens are dropped."""
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if part in ("PH", "SH"):
            continue
        ends = part.split("-")
        if not all(e in DAYS for e in ends) or len(ends) > 2:
            return None
        first = DAYS.index(ends[0])
        last = DAYS.index(ends[-1])
        day = first
        while True:
            out.append(day)
            if day == last:
                break
            day = (day + 1) % 7
    return out


def parse_opening_hours(text: str) -> Week | None:
    """Read the common shapes of OSM's `opening_hours`, or return None.

    None means "I could not read this", which is reported as hours unknown
    rather than guessed. Later rules override earlier ones for the days they
    name, as the OSM syntax says.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text == "24/7":
        return {d: [(0, 1440)] for d in range(7)}

    week: Week = {d: [] for d in range(7)}
    for rule in _RULE_SPLIT.split(text):
        rule = re.sub(r'"[^"]*"', "", rule).strip()
        if not rule:
            continue
        match = _DAY_LIST.match(rule)
        if match:
            days = _days(match.group(1))
            if days is None:
                return None
            if not days:  # a public-holiday-only rule
                continue
            times = match.group(2).strip()
        else:
            days, times = list(range(7)), rule

        if times.lower() in ("off", "closed"):
            for d in days:
                week[d] = []
            continue

        spans: list[tuple[int, int]] = []
        for piece in times.split(","):
            span = _SPAN.match(piece.strip())
            if not span:
                return None
            h1, m1, h2, m2 = (int(g) for g in span.groups())
            start, end = h1 * 60 + m1, h2 * 60 + m2
            if start > 1440 or end > 1440 + 12 * 60 or m1 > 59 or m2 > 59:
                return None
            if end <= start:
                end += 1440  # runs past midnight
            spans.append((start, end))
        for d in days:
            week[d] = spans
    return week


def spans_on(week: Week, day: date, tz: ZoneInfo,
             with_tail: bool = True) -> list[tuple[datetime, datetime]]:
    """The open periods that touch `day`, as local datetimes.

    `with_tail` includes the end of the previous evening's late opening
    (open until 02:00 means open at 01:00 the next day).
    """
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    out = []
    for start, end in week[(day.weekday() - 1) % 7] if with_tail else ():
        if end > 1440:
            out.append((midnight, midnight + timedelta(minutes=end - 1440)))
    for start, end in week[day.weekday()]:
        out.append((midnight + timedelta(minutes=start),
                    midnight + timedelta(minutes=end)))
    return sorted(out)


def _clock(moment: datetime) -> str:
    return f"{moment.hour:02d}:{moment.minute:02d}"


# ── The shopping window ─────────────────────────────────────────────────────


@dataclass
class Window:
    include_rest_of_today: bool
    day: date | None  # a later day to check, or None
    label: str


def shopping_window(when: str, now: datetime) -> Window:
    today = now.date()
    text = (when or "").strip().lower()
    next_day = today + timedelta(days=1)
    next_name = WEEKDAY_NAMES[next_day.weekday()].capitalize()

    for i, name in enumerate(WEEKDAY_NAMES):
        if name in text or re.search(rf"\b{name[:3]}\b", text):
            ahead = (i - today.weekday()) % 7
            if ahead == 0:
                return Window(True, None, f"the rest of {name.capitalize()}")
            target = today + timedelta(days=ahead)
            return Window(False, target,
                          f"{name.capitalize()} {target.day} {target.strftime('%B')}")

    if any(w in text for w in ("tomorrow", "tmrw", "morning", "first thing")):
        return Window(False, next_day,
                      f"{next_name} {next_day.day} {next_day.strftime('%B')}")
    if any(w in text for w in ("now", "tonight", "today", "evening", "late",
                               "asap", "soon", "later")):
        return Window(True, None, "the rest of the day")
    return Window(True, next_day,
                  f"the rest of the day and {next_name} {next_day.day} "
                  f"{next_day.strftime('%B')}")


# ── Interests ───────────────────────────────────────────────────────────────


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def _keyword_for(word: str) -> str | None:
    for candidate in (word, word.rstrip("s"), word[:-2] if word.endswith("es") else ""):
        if candidate and candidate in INTEREST_SHOPS:
            return candidate
    # "houseplants" → plant. Only for keywords of five letters or more
    # and only as a word ending, so "bread" does not become "read".
    stem = word.rstrip("s")
    for key in INTEREST_SHOPS:
        if len(key) >= 5 and stem.endswith(key) and stem != key:
            return key
    return None


def match_interests(interests: str) -> tuple[dict[str, list[str]], list[str]]:
    """→ ({shop kind: [interests it suits]}, [interests with no shop kind]).

    The dict's order is the order to look the kinds up in: the main kind for
    each interest first, then the extras, so five interests get five
    different shops before any one interest gets a second kind.
    """
    hits: list[tuple[str, tuple[str, ...]]] = []
    unmatched: list[str] = []
    items = [i.strip() for i in re.split(r",|;|/|\band\b|\bor\b|&", interests or "")]
    for item in filter(None, items):
        keys = [k for k in map(_keyword_for, _words(item)) if k]
        if not keys:
            unmatched.append(item)
        for key in keys:
            hits.append((item, INTEREST_SHOPS[key]))

    kinds: dict[str, list[str]] = {}
    for rank in range(3):
        for item, shop_kinds in hits:
            if rank < len(shop_kinds):
                suits = kinds.setdefault(shop_kinds[rank], [])
                if item not in suits:
                    suits.append(item)
    return kinds, unmatched


# ── Geometry ────────────────────────────────────────────────────────────────


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)))


def _distance_text(metres: int) -> str:
    if metres < 1000:
        return f"{round(metres, -1)} m"
    return f"{metres / 1000:.1f} km"


# ── The app ─────────────────────────────────────────────────────────────────


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))
        # Replaced in tests, so they do not wait out Nominatim's spacing.
        self._sleep = time.sleep

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    # The one seam the tests replace: every request goes through here.
    def _fetch(self, url: str, timeout: float = 6.0) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def handle(self, call: Call) -> Result:
        if call.outcome_id != "gift.shops_open":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        place = (call.inputs.get("place") or "").strip()
        if not place or place.lower() in ("me", "near me", "here", "nearby"):
            raise NoPlace("no place in the message")
        interests = (call.inputs.get("interests") or "").strip()
        when = (call.inputs.get("when") or "").strip()

        self._deadline = time.monotonic() + BUDGET_S

        lat, lon, place_found = self._geocode(place)
        tz = self._timezone(lat, lon, call.inputs.get("timezone"))
        now = call.context.now.astimezone(tz)
        window = shopping_window(when, now)

        wanted, unmatched = match_interests(interests)
        if not wanted:
            kinds = {k: [] for k in FALLBACK_SHOPS}
        else:
            # The friend's own kinds first, then a gift shop, which always
            # gets one of the MAX_KINDS lookups.
            room = MAX_KINDS - len([k for k in GENERIC_SHOPS if k not in wanted])
            kinds = dict(list(wanted.items())[:room])
            for k in GENERIC_SHOPS:
                kinds.setdefault(k, wanted.get(k, []))

        elements = self._shops(lat, lon, list(kinds))

        found = []
        for el in elements:
            tags = el.get("tags") or {}
            name = (tags.get("name") or "").strip()
            kind = tags.get("shop", "")
            slat = el.get("lat", (el.get("center") or {}).get("lat"))
            slon = el.get("lon", (el.get("center") or {}).get("lon"))
            if not name or kind not in kinds or slat is None or slon is None:
                continue
            found.append((distance_m(lat, lon, float(slat), float(slon)),
                          name, kind, tags))
        found.sort(key=lambda f: f[0])

        ranked = []
        open_count = unknown_count = closed_count = 0
        seen_names = set()
        for dist, name, kind, tags in found:
            # A chain's second branch adds nothing: keep the nearest one.
            if name.lower() in seen_names:
                continue
            seen_names.add(name.lower())
            bucket, status = self._status(tags.get("opening_hours", ""), now, tz, window)
            if bucket is None:
                closed_count += 1
                continue
            if bucket == 2:
                unknown_count += 1
            else:
                open_count += 1
            ranked.append((bucket, 0 if kinds[kind] else 1, dist, kind,
                           self._line(name, kind, dist, tags, status, kinds[kind])))

        # Best first: open now, then open later, then hours unknown; a shop
        # for one of the friend's interests before a general gift shop; then
        # the nearest. The best open shop of each kind goes first, so the
        # list is not eight bookshops.
        ranked.sort(key=lambda r: r[:3])
        firsts, rest, kinds_seen = [], [], set()
        for r in ranked:
            if r[0] < 2 and r[3] not in kinds_seen:
                firsts.append(r)
                kinds_seen.add(r[3])
            else:
                rest.append(r)
        lines = [r[4] for r in (firsts + rest)[:MAX_SHOPS]]

        local_time = f"{WEEKDAY_NAMES[now.weekday()].capitalize()} {_clock(now)}"
        return Result(
            data={
                "place_found": place_found,
                "local_time": local_time,
                "checked_for": window.label,
                "shops": lines,
                "open_count": open_count,
                "hours_unknown_count": unknown_count,
                "closed_count": closed_count,
                "shop_kinds": sorted({SHOP_LABELS.get(k, k) for k in kinds}),
                "interests_not_matched": unmatched,
            },
            narrative=self._sentence(place_found, window, open_count,
                                     unknown_count, lines),
        )

    # ── Requests ────────────────────────────────────────────────────────────

    def _remaining(self, cap: float) -> float:
        left = self._deadline - time.monotonic()
        if left < 1.0:
            raise ServiceUnavailableError("out of time budget")
        return min(cap, left)

    def _get_json(self, url: str, timeout: float):
        try:
            raw = self._fetch(url, timeout=self._remaining(timeout))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimitedError(f"rate limited: {exc.code}")
            raise ServiceUnavailableError(f"upstream returned {exc.code}")
        except TimeoutError as exc:
            raise ServiceUnavailableError(f"timed out: {exc}")
        except urllib.error.URLError as exc:
            raise ServiceUnavailableError(f"could not reach upstream: {exc.reason}")
        except OSError as exc:
            raise ServiceUnavailableError(f"network error: {exc}")
        try:
            return json.loads(raw)
        except ValueError:
            raise ServiceUnavailableError("upstream sent something that is not JSON")

    def _geocode(self, place: str) -> tuple[float, float, str]:
        query = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
        results = self._get_json(f"{NOMINATIM_URL}?{query}", timeout=5.0)
        if not isinstance(results, list) or not results:
            error = PlaceNotFound(f"nominatim found nothing for {place!r}")
            error.user_message = (
                f"I could not find anywhere called {place}. Try a street, "
                "an area with its town, or a postcode."
            )
            raise error
        top = results[0]
        name = top.get("display_name") or place
        # "Shoreditch, London Borough of Hackney, Greater London, ..." is long;
        # the first three parts are enough to show which place it was.
        short = ", ".join(p.strip() for p in name.split(",")[:3])
        return float(top["lat"]), float(top["lon"]), short

    def _timezone(self, lat: float, lon: float, fallback: str | None) -> ZoneInfo:
        """The shop's own time zone. Falls back to the user's, then UTC."""
        query = urllib.parse.urlencode({"latitude": f"{lat:.4f}",
                                        "longitude": f"{lon:.4f}",
                                        "timezone": "auto"})
        try:
            name = self._get_json(f"{TIMEZONE_URL}?{query}", timeout=3.0).get("timezone")
            return ZoneInfo(name)
        except (AgentAppError, ZoneInfoNotFoundError, AttributeError, TypeError, ValueError):
            pass
        if fallback:
            return ZoneInfo(fallback)
        return ZoneInfo("UTC")

    def _shops(self, lat: float, lon: float, kinds: list[str]) -> list[dict]:
        """Shops of each kind inside a box around the point.

        One Nominatim request per kind, a second apart as its usage policy
        asks. `kinds` only ever comes from the fixed tables above, never from
        the question. If time runs short the kinds already fetched are used;
        only when not one request succeeded is it an error.
        """
        dlat = SEARCH_RADIUS_M / 111_320
        dlon = SEARCH_RADIUS_M / (111_320 * max(math.cos(math.radians(lat)), 0.01))
        viewbox = f"{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f},{lat - dlat:.5f}"

        elements: list[dict] = []
        succeeded = 0
        last_error: AgentAppError | None = None
        for kind in kinds[:MAX_KINDS]:
            if self._deadline - time.monotonic() < NOMINATIM_GAP_S + 2.0:
                break
            self._sleep(NOMINATIM_GAP_S)
            query = urllib.parse.urlencode({
                "q": f"[shop={kind}]", "format": "jsonv2", "limit": 25,
                "extratags": 1, "addressdetails": 1, "bounded": 1,
                "viewbox": viewbox,
            })
            try:
                results = self._get_json(f"{NOMINATIM_URL}?{query}", timeout=4.0)
            except RateLimitedError as exc:
                last_error = exc
                break
            except ServiceUnavailableError as exc:
                last_error = exc
                continue
            succeeded += 1
            for r in results if isinstance(results, list) else []:
                if r.get("category") != "shop" or r.get("type") != kind:
                    continue
                address = r.get("address") or {}
                extra = r.get("extratags") or {}
                elements.append({
                    "lat": r.get("lat"), "lon": r.get("lon"),
                    "tags": {
                        "name": r.get("name") or "",
                        "shop": kind,
                        "opening_hours": extra.get("opening_hours", ""),
                        "addr:street": address.get("road", ""),
                        "addr:housenumber": address.get("house_number", ""),
                    },
                })
        if not succeeded:
            raise last_error or ServiceUnavailableError("no shop lookup finished")
        return elements

    # ── Wording ─────────────────────────────────────────────────────────────

    @staticmethod
    def _status(hours: str, now: datetime, tz: ZoneInfo,
                window: Window) -> tuple[int | None, str]:
        """→ (bucket, words). Bucket 0 open now, 1 open later in the window,
        2 hours unknown, None closed for the whole window."""
        week = parse_opening_hours(hours)
        if week is None:
            return 2, ("opening hours not listed" if not hours.strip()
                       else f"hours listed as \"{hours.strip()}\"")

        parts: list[str] = []
        bucket: int | None = None
        if window.include_rest_of_today:
            today_spans = [s for s in spans_on(week, now.date(), tz) if s[1] > now]
            current = next((s for s in today_spans if s[0] <= now), None)
            if current:
                bucket = 0
                parts.append(f"open now until {_clock(current[1])}")
            elif today_spans:
                bucket = 1
                s = today_spans[0]
                parts.append(f"opens later at {_clock(s[0])} until {_clock(s[1])}")
            else:
                parts.append("closed for the rest of the day")

        if window.day is not None:
            label = WEEKDAY_NAMES[window.day.weekday()].capitalize()
            day_spans = spans_on(week, window.day, tz, with_tail=False)
            if day_spans:
                if bucket is None:
                    bucket = 1
                times = ", ".join(f"{_clock(a)}-{_clock(b)}" for a, b in day_spans)
                parts.append(f"{label} {times}")
            else:
                parts.append(f"closed {label}")
        return bucket, "; ".join(parts)

    @staticmethod
    def _line(name: str, kind: str, dist: int, tags: dict, status: str,
              suits: list[str]) -> str:
        street = tags.get("addr:street", "").strip()
        number = tags.get("addr:housenumber", "").strip()
        where = f", {number} {street}".replace(",  ", ", ") if street else ""
        line = f"{name} ({SHOP_LABELS.get(kind, kind)}, {_distance_text(dist)}{where}): {status}"
        if suits:
            line += f" (suits: {', '.join(suits)})"
        return line

    @staticmethod
    def _sentence(place: str, window: Window, open_count: int,
                  unknown_count: int, lines: list[str]) -> str:
        if not lines:
            return (f"Gift shops near {place}: none of the kinds looked for are "
                    f"listed as open for {window.label}.")
        return (f"Gift shops near {place}: {open_count} open for {window.label}"
                + (f", plus {unknown_count} with no hours listed" if unknown_count else "")
                + f". Top match: {lines[0]}.")
