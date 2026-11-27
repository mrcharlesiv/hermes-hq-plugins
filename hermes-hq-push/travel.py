"""Travel cards (0.9.86, ``travelCards`` in /status): a bot shows the flights or hotels it found as travel cards in the
Hermes HQ app chat (a flight's airline, times, stops, fare and bag rules; a hotel's photo, area, rating, nightly rate
and cancellation), each with Book and Watch Price. Nothing is booked until the person says so, and the last step of a
booking is an order card (shopping.py) they tap Pay on.

Two tools, as ``show_products`` is one (shopping.py): ``show_flights`` and ``show_hotels`` answer at once and tell the
bot to end its reply. The app draws the tool call's arguments as the cards and never reads the result, so all this file
does with a call is check it (a refused call is drawn as nothing, and the bot is told in words what to fix) and say
what comes next. The checks follow the app's parser (HermesKit/Travel/TravelCards.swift): what it would drop is
refused here, what it would cut is said back. The person's taps come back as ordinary messages in the chat, written by
the app, whose first line names the option:

    [Hermes HQ travel f-1a2b3c4d: book]           then the flight, its link, and "stop before final payment"
    [Hermes HQ travel h-1a2b3c4d: book]           the same for a hotel
    [Hermes HQ travel f-1a2b3c4d: watch price]    a scheduled job that says when the fare drops

Both ids are made from the call's own words (``flight_id``, ``hotel_id``) so the app makes the same ones.

The tools show only once an app that draws the cards has said so on this computer (PUT /travel/cards writes
``travel-cards.on`` in the add-on's data folder), so a bot never leaves a card where nobody can see it. Off for every
bot while ``travel.off`` exists there. Only in turns ``hermes serve`` runs: a turn of the messaging gateway has no app
chat to show a card in. Nothing about a trip is logged or kept.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Callable

FLIGHTS_TOOL = "show_flights"
HOTELS_TOOL = "show_hotels"
TOOLS = (FLIGHTS_TOOL, HOTELS_TOOL)
SWITCH = "travel.off"
CARDS = "travel-cards.on"
TOOLSET = "hq_travel"

# The app's limits (TravelCards.swift): more than these is refused, so the bot picks its best rather than the app
# dropping the rest unseen.
MAX_FLIGHTS = 12
MAX_HOTELS = 12
MAX_SEGMENTS = 6
MAX_IMAGES = 12
MAX_AMENITIES = 10
MAX_WHY = 3
MAX_CAUTION = 2
MAX_FOLLOW_UPS = 3
MAX_RELAXED = 4
MAX_STOPS = 9
MAX_URL = 2048
MAX_FARE = 10_000_000      # the app drops a fare at or above this as a mistake
# What the card shows of each text; longer text is cut there by the app (the call is not refused for it).
LENGTHS = {"note": 200, "step": 60, "relaxed": 60, "follow_up": 60, "airline": 60, "flight_number": 24, "time": 40,
           "duration": 30, "stop_summary": 60, "cabin": 40, "fare_tier": 40, "badge": 28, "bag": 80, "rule": 120,
           "layover": 60, "why": 120, "caution": 120, "location": 80, "date": 40, "guests": 40, "name": 140,
           "neighborhood": 80, "address": 200, "cancellation": 100, "breakfast": 60, "distance": 60, "amenity": 40}

MARK = re.compile(r"^\s*\[Hermes HQ travel ([fh]-[0-9a-f]{8}): (book|watch price)\]")
_PLAIN_NUMBER = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_AIRPORT = re.compile(r"^[A-Za-z]{3}$")
# A link the app keeps exactly as written: https, a host with a dot, and only the characters a URL may carry as they
# are. Anything else (a space, a non-ASCII letter) Foundation would percent-encode first, and the app's id would no
# longer be the one made here.
_LINK = re.compile(r"^https://[^/\s?#]+\.[^/\s?#]+", re.IGNORECASE)
_URL_CHARS = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")

_PRICE_WORDS = ("Plain numbers in `currency`, as the booking page shows them: 412.50, never \"$412.50\" or "
                "\"412.50 USD\".")
_TAPS = (
    "The user's taps arrive as messages starting \"[Hermes HQ travel <id>: book]\" or \"[Hermes HQ travel <id>: "
    "watch price]\" (the id is one this tool returned). On book: open that option's own page in the browser, check "
    "the price, the rules and that it is still available, fill in what the booking needs up to the payment step, then "
    "call order_card with the total that page will charge and stop (if you have no order_card, stop there and ask for "
    "their OK in words with the total); never pay or confirm the booking yourself. On watch price: set up a "
    "scheduled job that checks that page and messages them only when the price drops below the one on the card, then "
    "say in one line that the watch is set.")

FLIGHTS_SCHEMA = {
    "name": FLIGHTS_TOOL,
    "description": (
        "Show the user flights as flight cards in the Hermes HQ app: airline, route, times, stops, cabin, fare, bag and "
        "change rules, with Book and Watch Price on each. Use it only after you have really looked fares up on an "
        "airline's or a booking site's pages (the browser, search or page extract) for the trip the user asked about. "
        "One call holds every option you recommend, best first (up to 12). Take every value from the page and leave out "
        "any field it doesn't show: never guess or invent a fare, a time, a flight number, a bag rule or a link. "
        "`price` is the fare as the page shows it now; `url` is one https deep link to that option on the page you "
        "found it (not a search form you haven't filled). When nothing matches exactly and you widened the search, "
        "say what you relaxed in `relaxed_filters`. Your reasons go in `why` and downsides in `caution`, not in your "
        "reply. This tool books nothing: it returns {flights: \"shown\", ids} at once. Then don't repeat the flights' "
        "times, fares or links in your reply: end it with one short line (your pick and why, or a question). " + _TAPS),
    "parameters": {
        "type": "object",
        "properties": {
            "note": {"type": "string",
                     "description": "Optional. One short line shown above the cards: which one you'd pick and why."},
            "step": {"type": "string",
                     "description": "Optional, a few words on where you are: Checking the fare."},
            "relaxed_filters": {"type": "array", "items": {"type": "string"},
                                "description": f"Optional, up to {MAX_RELAXED}: the wishes you had to drop to find "
                                               "these, when none matched exactly: Nonstop only, Morning departure."},
            "flights": {
                "type": "array",
                "description": f"The flights, best first: 1 to {MAX_FLIGHTS}. Each one once.",
                "items": {
                    "type": "object",
                    "properties": {
                        "airline": {"type": "string", "description": "The airline as the page names it."},
                        "flight_number": {"type": "string",
                                          "description": "Optional: the first leg's flight number, XY 123."},
                        "origin": {"type": "string", "description": "The 3-letter IATA airport code it leaves from: SFO."},
                        "destination": {"type": "string",
                                        "description": "The 3-letter IATA airport code it ends at: JFK."},
                        "depart_time": {"type": "string",
                                        "description": "When it leaves, as the page says: Fri Mar 6, 8:05 AM."},
                        "arrive_time": {"type": "string",
                                        "description": "When it lands (with +1 when it is the next day): 4:40 PM."},
                        "duration": {"type": "string", "description": "Optional, door to door: 5h 35m."},
                        "stops": {"type": "integer", "description": f"0 for nonstop, up to {MAX_STOPS}."},
                        "stop_summary": {"type": "string",
                                         "description": "Optional, as the page says: 1 stop in DEN (1h 10m)."},
                        "cabin": {"type": "string", "description": "Optional: Economy, Premium economy, Business."},
                        "fare_tier": {"type": "string", "description": "Optional, the fare's name: Basic, Main, Flex."},
                        "price": {"type": "number",
                                  "description": "The fare for everyone the user asked for, as the page shows it "
                                                 "now. " + _PRICE_WORDS},
                        "was": {"type": "number",
                                "description": "Only when the page shows a struck-through higher fare."},
                        "currency": {"type": "string", "description": "ISO 4217 code: USD (the default), EUR, GBP…"},
                        "url": {"type": "string",
                                "description": "https deep link to this option on the airline's or booking site's "
                                               "page."},
                        "badge": {"type": "string", "description": "Optional, one or two words: Cheapest, Fastest."},
                        "carry_on": {"type": "string", "description": "Optional, as the page says: Carry-on included."},
                        "checked_bag": {"type": "string",
                                        "description": "Optional, as the page says: 1st bag 35 USD."},
                        "change_rule": {"type": "string",
                                        "description": "Optional, as the page says: Changes free, fare difference "
                                                       "applies."},
                        "refund_rule": {"type": "string", "description": "Optional, as the page says: Non-refundable."},
                        "segments": {
                            "type": "array",
                            "description": f"Optional, for a trip with stops: each leg in order, up to {MAX_SEGMENTS}.",
                            "items": {"type": "object",
                                      "properties": {"airline": {"type": "string"},
                                                     "flight_number": {"type": "string"},
                                                     "origin": {"type": "string", "description": "IATA code."},
                                                     "destination": {"type": "string", "description": "IATA code."},
                                                     "depart_time": {"type": "string"},
                                                     "arrive_time": {"type": "string"},
                                                     "duration": {"type": "string"},
                                                     "layover": {"type": "string",
                                                                 "description": "The wait after this leg: 1h 10m in "
                                                                                "DEN."}},
                                      "required": ["origin", "destination"]}},
                        "why": {"type": "array", "items": {"type": "string"},
                                "description": f"Up to {MAX_WHY} short reasons it fits what the user asked."},
                        "caution": {"type": "array", "items": {"type": "string"},
                                    "description": f"Up to {MAX_CAUTION} short downsides worth knowing."},
                    },
                    "required": ["airline", "origin", "destination", "price", "url"],
                },
            },
            "follow_ups": {"type": "array", "items": {"type": "string"},
                           "description": f"Optional, up to {MAX_FOLLOW_UPS} short next asks the user can tap, sent "
                                          "as their message: Show later flights."},
        },
        "required": ["flights"],
    },
}

HOTELS_SCHEMA = {
    "name": HOTELS_TOOL,
    "description": (
        "Show the user hotels as hotel cards in the Hermes HQ app: photo, name, area, stars, guest rating, nightly rate "
        "and total, cancellation and breakfast, on a map when you give the coordinates, with Book and Watch Price on "
        "each. Use it only after you have really looked rooms up on a hotel's or a booking site's pages (the browser, "
        "search or page extract) for the dates the user asked about. One call holds every hotel you recommend, best "
        "first (up to 12). Take every value from the page and leave out any field it doesn't show: never guess or "
        "invent a rate, a rating, a review count, a photo, the coordinates or a link. `price_per_night` and "
        "`total_price` are the prices the page shows for those dates; `url` is one https deep link to that hotel's "
        "page for those dates. Your reasons go in `why` and downsides in `caution`, not in your reply. This tool books "
        "nothing: it returns {hotels: \"shown\", ids} at once. Then don't repeat the hotels' names, rates or links in "
        "your reply: end it with one short line (your pick and why, or a question). " + _TAPS),
    "parameters": {
        "type": "object",
        "properties": {
            "note": {"type": "string",
                     "description": "Optional. One short line shown above the cards: which one you'd pick and why."},
            "step": {"type": "string", "description": "Optional, a few words on where you are: Checking rooms."},
            "location": {"type": "string", "description": "Optional: where the stay is, Lisbon, Alfama."},
            "check_in": {"type": "string", "description": "Optional, as you'd say it: Fri Mar 6."},
            "check_out": {"type": "string", "description": "Optional, as you'd say it: Mon Mar 9."},
            "guests": {"type": "string", "description": "Optional, as text: 2 adults, 1 room."},
            "hotels": {
                "type": "array",
                "description": f"The hotels, best first: 1 to {MAX_HOTELS}. Each one once.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The hotel's name as the page gives it."},
                        "url": {"type": "string",
                                "description": "https deep link to this hotel's page for these dates."},
                        "neighborhood": {"type": "string", "description": "Optional: the area, Alfama."},
                        "address": {"type": "string", "description": "Optional: the street address the page shows."},
                        "latitude": {"type": "number", "description": "Optional, with `longitude`, from the page."},
                        "longitude": {"type": "number", "description": "Optional, with `latitude`, from the page."},
                        "stars": {"type": "integer", "description": "Optional: the hotel's class, 1 to 5."},
                        "rating": {"type": "number",
                                   "description": "The guests' rating as the page gives it: out of 5, or out of 10 "
                                                  "as it is (any rating above 5 is read as out of 10)."},
                        "reviews": {"type": "integer", "description": "How many reviews that rating is from."},
                        "price_per_night": {"type": "number",
                                            "description": "The nightly rate for these dates. " + _PRICE_WORDS},
                        "total_price": {"type": "number",
                                        "description": "The total for the stay as the page shows it, taxes as it "
                                                       "says."},
                        "currency": {"type": "string", "description": "ISO 4217 code: USD (the default), EUR, GBP…"},
                        "was": {"type": "number",
                                "description": "Only when the page shows a struck-through higher nightly rate."},
                        "image": {"type": "string", "description": "https link to the hotel's main photo, from its page."},
                        "images": {"type": "array", "items": {"type": "string"},
                                   "description": "Optional further photos from the page (with `image`, up to "
                                                  f"{MAX_IMAGES})."},
                        "badge": {"type": "string", "description": "Optional, one or two words: Best location."},
                        "cancellation": {"type": "string",
                                         "description": "Optional, as the page says: Free cancellation until Mar 4."},
                        "breakfast": {"type": "string", "description": "Optional, as the page says: Breakfast included."},
                        "distance": {"type": "string", "description": "Optional: 0.4 km from the centre."},
                        "amenities": {"type": "array", "items": {"type": "string"},
                                      "description": f"Optional, up to {MAX_AMENITIES} short ones: Pool, Gym."},
                        "why": {"type": "array", "items": {"type": "string"},
                                "description": f"Up to {MAX_WHY} short reasons it fits what the user asked."},
                        "caution": {"type": "array", "items": {"type": "string"},
                                    "description": f"Up to {MAX_CAUTION} short downsides worth knowing."},
                    },
                    "required": ["name", "url"],
                },
            },
            "follow_ups": {"type": "array", "items": {"type": "string"},
                           "description": f"Optional, up to {MAX_FOLLOW_UPS} short next asks the user can tap, sent "
                                          "as their message: Show cheaper ones."},
        },
        "required": ["hotels"],
    },
}

SHOWN_NOTE = (
    "The user now sees these {kind} as cards, each with Book and Watch Price. Nothing has been booked. Don't repeat "
    "their details, prices or links: end your reply with one short line at most (your pick, or a question). A tap "
    "arrives as a message starting \"[Hermes HQ travel <id>: book]\": open that option's page, check the price and "
    "rules, take it up to the payment step and call order_card with the total, never paying or confirming first. "
    "\"watch price\": set up a scheduled job that tells them only when the price drops.")


# MARK: The switches

def switched_on(data_dir: Path) -> bool:
    try:
        return not (Path(data_dir) / SWITCH).exists()
    except OSError:
        return True


def cards_shown(data_dir: Path) -> bool:
    """Whether an app that draws the cards has said so on this computer."""
    try:
        return (Path(data_dir) / CARDS).exists()
    except OSError:
        return False


def note_cards(data_dir: Path) -> None:
    """PUT /travel/cards: an app that draws flight and hotel cards uses this computer."""
    (Path(data_dir) / CARDS).touch(exist_ok=True)


def status(data_dir: Path) -> dict:
    """`travelCards` (0.9.86): show_flights and show_hotels exist here (false while travel.off turns them off).
    `travelCardsShown`: an app has said it draws the cards, so bots have the tools; an app that draws them and reads
    false answers with PUT /travel/cards."""
    return {"travelCards": switched_on(data_dir), "travelCardsShown": cards_shown(data_dir)}


# MARK: Ids (the app's TravelCards.flightId / hotelId make the same ones)

def _hex8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _one_line(value: str, limit: int) -> str:
    """The app's text(_:_:): trimmed, its lines joined by single spaces, and cut to `limit` with an ellipsis."""
    text = " ".join(line for line in value.strip().splitlines() if line)
    if len(text) <= limit:
        return text
    return text[:limit - 1].strip(" \t") + "…"


def flight_id(airline: str, origin: str, destination: str, depart: str, url: str) -> str:
    """"f-" and the first 8 hex digits of SHA-256 over `airline\\norigin\\ndestination\\ndepart\\nurl`, each trimmed,
    the airport codes in capitals, and the departure as the card shows it (one line, at most 40 characters)."""
    parts = (str(airline).strip(), str(origin).strip().upper(), str(destination).strip().upper(),
             _one_line(str(depart), LENGTHS["time"]), str(url).strip())
    return "f-" + _hex8("\n".join(v.strip() for v in parts))


def hotel_id(name: str, address: str, url: str) -> str:
    """"h-" and the first 8 hex digits of SHA-256 over `name\\naddress\\nurl`, each trimmed (an empty address when
    the call gives none)."""
    return "h-" + _hex8("\n".join(str(v).strip() for v in (name, address, url)))


# MARK: Checking a call

def _reply(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _failed(problems: list[str]) -> str:
    shown = problems[:6]
    more = f" (and {len(problems) - len(shown)} more like these)" if len(problems) > len(shown) else ""
    return _reply({"status": "failed",
                   "error": "Nothing was shown. Fix this and call again: " + " ".join(shown) + more})


class _Check:
    """Collects what is wrong (refused) and what is longer than the card shows (cut by the app, said back). Text
    must be text: the app shows a number given for a text field as nothing."""

    def __init__(self):
        self.problems: list[str] = []
        self.long: list[str] = []

    def text(self, obj: dict, key: str, where: str, limit_key: str, required: bool = False) -> str:
        value = obj.get(key)
        if value is None or value == "":
            if required:
                self.problems.append(f"{where}`{key}` is missing.")
            return ""
        if not isinstance(value, str):
            self.problems.append(f"{where}`{key}` is text, in quotes: {json.dumps(str(value), ensure_ascii=False)}.")
            return ""
        value = value.strip()
        if not value and required:
            self.problems.append(f"{where}`{key}` is empty.")
        limit = LENGTHS[limit_key]
        if len(_one_line(value, 10 * limit)) > limit:
            self.long.append(f"{where}`{key}` ({limit})")
        return value

    def texts(self, obj: dict, key: str, where: str, limit_key: str, most: int) -> list[str]:
        value = obj.get(key)
        if value is None or value == []:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.problems.append(f"{where}`{key}` is a list of short texts: [\"…\", \"…\"].")
            return []
        value = [v.strip() for v in value if v.strip()]
        if len(value) > most:
            self.problems.append(f"{where}`{key}` has {len(value)}; the card holds {most}. Keep the best {most}.")
        limit = LENGTHS[limit_key]
        if any(len(v) > limit for v in value):
            self.long.append(f"{where}`{key}` ({limit} each)")
        return value

    def number(self, obj: dict, key: str, where: str) -> float | None:
        value = obj.get(key)
        if value is None:
            return None
        if isinstance(value, bool):
            self.problems.append(f"{where}`{key}` is a number.")
            return None
        if isinstance(value, str):
            if not value.strip():
                return None
            if not _PLAIN_NUMBER.match(value):
                self.problems.append(f"{where}`{key}` is {json.dumps(value, ensure_ascii=False)}: write it as a plain "
                                     "number, 412.50, with no currency sign, code or commas (the currency goes in "
                                     "`currency`).")
                return None
            value = float(value)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            self.problems.append(f"{where}`{key}` is a number.")
            return None
        return float(value)

    def fare(self, obj: dict, key: str, where: str, required: bool = False) -> float | None:
        value = self.number(obj, key, where)
        if value is None:
            if required and obj.get(key) in (None, ""):
                self.problems.append(f"{where}`{key}` is missing: the price the booking page shows now.")
            return None
        if not 0 < value < MAX_FARE:
            self.problems.append(f"{where}`{key}` ({value:g}) is above 0 and below {MAX_FARE:,}: the price as the "
                                 "page shows it.")
            return None
        return value

    def whole(self, obj: dict, key: str, where: str, low: int, high: int) -> int | None:
        value = self.number(obj, key, where)
        if value is None:
            return None
        if value != int(value) or not low <= value <= high:
            self.problems.append(f"{where}`{key}` is a whole number from {low} to {high}.")
            return None
        return int(value)

    def url(self, obj: dict, key: str, where: str, required: bool = False) -> str:
        value = obj.get(key)
        if value is None or value == "":
            if required:
                self.problems.append(f"{where}`{key}` is missing: the https link to its page.")
            return ""
        return self.link(value, f"{where}`{key}`")

    def link(self, value, named: str) -> str:
        if not isinstance(value, str) or not value.strip():
            self.problems.append(f"{named} is an https link.")
            return ""
        value = value.strip()
        if not _LINK.match(value) or re.search(r"\s", value):
            self.problems.append(f"{named} is {json.dumps(value[:80], ensure_ascii=False)}: only a full https:// link "
                                 "to a real page works (no http, no relative path, no spaces).")
            return ""
        if not _URL_CHARS.match(value):
            self.problems.append(f"{named} has characters a link carries percent-encoded (an accented letter, a "
                                 "quote, a brace): pass it as the page's address bar copies it, %C3%A9 for é.")
            return ""
        if len(value) > MAX_URL:
            self.problems.append(f"{named} is longer than {MAX_URL} characters: use the page's plain link.")
            return ""
        return value

    def currency(self, obj: dict, where: str) -> str:
        value = obj.get("currency")
        if value is None or value == "":
            return "USD"
        if not isinstance(value, str) or not _CURRENCY.match(value.strip()):
            self.problems.append(f"{where}`currency` is {json.dumps(value, ensure_ascii=False)}: use the 3-letter ISO "
                                 "code in capitals, USD, EUR, GBP (not a symbol).")
            return "USD"
        return value.strip()

    def airport(self, obj: dict, key: str, where: str) -> str:
        value = self.text(obj, key, where, "time", required=True)
        if value and not _AIRPORT.match(value):
            self.problems.append(f"{where}`{key}` is {json.dumps(value[:40], ensure_ascii=False)}: use the 3-letter "
                                 "IATA airport code, SFO, LHR (not the city or airport's name).")
            return ""
        return value.upper()

    def was(self, obj: dict, where: str, price: float | None, priced: str) -> None:
        was = self.number(obj, "was", where)
        if was is None or was == 0:
            return
        if price is None:
            self.problems.append(f"{where}`was` needs a `{priced}` it is above.")
        elif was < price:
            self.problems.append(f"{where}`was` ({was:g}) is the struck-through old price and must be above "
                                 f"`{priced}` ({price:g}). Leave it out when the page shows no higher old price.")

    def count(self, value, named: str, most: int, least: int = 0, card: str = "card") -> list:
        if value is None:
            value = []
        if not isinstance(value, list):
            self.problems.append(f"`{named}` is a list.")
            return []
        if len(value) < least:
            self.problems.append(f"`{named}` is empty: pass at least {least}.")
        if len(value) > most:
            self.problems.append(f"`{named}` has {len(value)}; the {card} holds {most}. Keep the best {most}.")
        return value

    def badge(self, obj: dict, where: str) -> None:
        if isinstance(obj.get("badge"), list):
            self.problems.append(f"{where}`badge` is one short text (one badge per card).")
        else:
            self.text(obj, "badge", where, "badge")


def _where(index: int, label: str, kind: str) -> str:
    return f"{kind.capitalize()} {index + 1}" + (f" ({label[:40]})" if label else "") + ": "


def _label(obj: dict, *keys: str) -> str:
    return " ".join(obj.get(k).strip() for k in keys if isinstance(obj.get(k), str) and obj.get(k).strip())


def check_flights(args) -> tuple[list[str], list[str], list[str]]:
    """(problems, ids, long text) for a show_flights call."""
    c = _Check()
    if not isinstance(args, dict):
        return ["The arguments are an object: {flights: [...]}."], [], []
    c.text(args, "note", "", "note")
    c.text(args, "step", "", "step")
    c.texts(args, "relaxed_filters", "", "relaxed", MAX_RELAXED)
    c.texts(args, "follow_ups", "", "follow_up", MAX_FOLLOW_UPS)
    flights = c.count(args.get("flights"), "flights", MAX_FLIGHTS, least=1)
    ids: list[str] = []
    for index, flight in enumerate(flights[:MAX_FLIGHTS + 1]):
        if not isinstance(flight, dict):
            c.problems.append(f"Flight {index + 1} is an object with `airline`, `origin`, `destination`, `price` "
                              "and `url`.")
            continue
        where = _where(index, _label(flight, "airline", "flight_number"), "flight")
        airline = c.text(flight, "airline", where, "airline", required=True)
        c.text(flight, "flight_number", where, "flight_number")
        origin = c.airport(flight, "origin", where)
        destination = c.airport(flight, "destination", where)
        depart = c.text(flight, "depart_time", where, "time")
        c.text(flight, "arrive_time", where, "time")
        c.text(flight, "duration", where, "duration")
        c.whole(flight, "stops", where, 0, MAX_STOPS)
        c.text(flight, "stop_summary", where, "stop_summary")
        c.text(flight, "cabin", where, "cabin")
        c.text(flight, "fare_tier", where, "fare_tier")
        price = c.fare(flight, "price", where, required=True)
        c.was(flight, where, price, "price")
        c.currency(flight, where)
        url = c.url(flight, "url", where, required=True)
        c.badge(flight, where)
        c.text(flight, "carry_on", where, "bag")
        c.text(flight, "checked_bag", where, "bag")
        c.text(flight, "change_rule", where, "rule")
        c.text(flight, "refund_rule", where, "rule")
        segments = c.count(flight.get("segments"), f"{where.rstrip(': ')} segments", MAX_SEGMENTS)
        for i, segment in enumerate(segments[:MAX_SEGMENTS + 1]):
            at = f"{where}leg {i + 1}: "
            if not isinstance(segment, dict):
                c.problems.append(f"{at}is an object {{origin, destination, depart_time, arrive_time}}.")
                continue
            c.text(segment, "airline", at, "airline")
            c.text(segment, "flight_number", at, "flight_number")
            c.airport(segment, "origin", at)
            c.airport(segment, "destination", at)
            c.text(segment, "depart_time", at, "time")
            c.text(segment, "arrive_time", at, "time")
            c.text(segment, "duration", at, "duration")
            c.text(segment, "layover", at, "layover")
        c.texts(flight, "why", where, "why", MAX_WHY)
        c.texts(flight, "caution", where, "caution", MAX_CAUTION)
        if airline and origin and destination and url:
            new_id = flight_id(airline, origin, destination, depart, url)
            if new_id in ids:
                c.problems.append(f"{where}is the same airline, route, departure and link as flight "
                                  f"{ids.index(new_id) + 1}: list each once.")
            ids.append(new_id)
    return c.problems, ids, c.long


def check_hotels(args) -> tuple[list[str], list[str], list[str]]:
    """(problems, ids, long text) for a show_hotels call."""
    c = _Check()
    if not isinstance(args, dict):
        return ["The arguments are an object: {hotels: [...]}."], [], []
    c.text(args, "note", "", "note")
    c.text(args, "step", "", "step")
    c.text(args, "location", "", "location")
    c.text(args, "check_in", "", "date")
    c.text(args, "check_out", "", "date")
    c.text(args, "guests", "", "guests")
    c.texts(args, "follow_ups", "", "follow_up", MAX_FOLLOW_UPS)
    hotels = c.count(args.get("hotels"), "hotels", MAX_HOTELS, least=1)
    ids: list[str] = []
    for index, hotel in enumerate(hotels[:MAX_HOTELS + 1]):
        if not isinstance(hotel, dict):
            c.problems.append(f"Hotel {index + 1} is an object with `name`, `url` and its price.")
            continue
        where = _where(index, _label(hotel, "name"), "hotel")
        name = c.text(hotel, "name", where, "name", required=True)
        url = c.url(hotel, "url", where, required=True)
        c.text(hotel, "neighborhood", where, "neighborhood")
        address = c.text(hotel, "address", where, "address")
        latitude = c.number(hotel, "latitude", where)
        longitude = c.number(hotel, "longitude", where)
        if (latitude is None) != (longitude is None):
            c.problems.append(f"{where}`latitude` and `longitude` go together: give both from the page, or neither.")
        if latitude is not None and abs(latitude) > 90:
            c.problems.append(f"{where}`latitude` ({latitude:g}) is between -90 and 90.")
        if longitude is not None and abs(longitude) > 180:
            c.problems.append(f"{where}`longitude` ({longitude:g}) is between -180 and 180.")
        c.whole(hotel, "stars", where, 1, 5)
        rating = c.number(hotel, "rating", where)
        if rating is not None and not 0 <= rating <= 10:
            c.problems.append(f"{where}`rating` ({rating:g}) is out of 5, or out of 10 as the page gives it.")
        reviews = c.number(hotel, "reviews", where)
        if reviews is not None and (reviews != int(reviews) or reviews < 0):
            c.problems.append(f"{where}`reviews` is a whole number of reviews.")
        nightly = c.fare(hotel, "price_per_night", where)
        total = c.fare(hotel, "total_price", where)
        if nightly is None and total is None and hotel.get("price_per_night") in (None, "") \
                and hotel.get("total_price") in (None, ""):
            c.problems.append(f"{where}give `price_per_night` or `total_price` (or both), as the page shows them for "
                              "these dates.")
        c.was(hotel, where, nightly, "price_per_night")
        c.currency(hotel, where)
        if hotel.get("image") not in (None, ""):
            c.url(hotel, "image", where)
        images = hotel.get("images")
        if images not in (None, []):
            if not isinstance(images, list):
                c.problems.append(f"{where}`images` is a list of https links.")
            else:
                most = MAX_IMAGES - (1 if hotel.get("image") not in (None, "") else 0)
                if len(images) > most:
                    c.problems.append(f"{where}`images` has {len(images)}; with `image` the card shows {MAX_IMAGES}.")
                for i, image in enumerate(images[:MAX_IMAGES + 1]):
                    c.link(image, f"{where}`images[{i}]`")
        c.badge(hotel, where)
        c.text(hotel, "cancellation", where, "cancellation")
        c.text(hotel, "breakfast", where, "breakfast")
        c.text(hotel, "distance", where, "distance")
        c.texts(hotel, "amenities", where, "amenity", MAX_AMENITIES)
        c.texts(hotel, "why", where, "why", MAX_WHY)
        c.texts(hotel, "caution", where, "caution", MAX_CAUTION)
        if name and url:
            new_id = hotel_id(name, address, url)
            if new_id in ids:
                c.problems.append(f"{where}is the same hotel, address and link as hotel {ids.index(new_id) + 1}: list "
                                  "each once.")
            ids.append(new_id)
    return c.problems, ids, c.long


def _long_words(long: list[str]) -> str:
    if not long:
        return ""
    return (" Some text is longer than the card shows and will be cut: " + ", ".join(long[:6]) +
            ". Keep it shorter next time.")


def decision(user_message) -> tuple[str, str] | None:
    """(id, "book" | "watch price") when a message is the app's answer to a flight or hotel card."""
    if not isinstance(user_message, str):
        return None
    found = MARK.match(user_message)
    return (found.group(1), found.group(2)) if found else None


# MARK: The tools

def show_flights(args) -> str:
    problems, ids, long = check_flights(args)
    if problems:
        return _failed(problems)
    return _reply({"flights": "shown", "id": ids[0], "ids": ids,
                   "note": SHOWN_NOTE.format(kind="flights") + _long_words(long)})


def show_hotels(args) -> str:
    problems, ids, long = check_hotels(args)
    if problems:
        return _failed(problems)
    return _reply({"hotels": "shown", "id": ids[0], "ids": ids,
                   "note": SHOWN_NOTE.format(kind="hotels") + _long_words(long)})


def register_tool(ctx, data_dir: Callable[[], Path], serves: Callable[[], bool]) -> None:
    def outside() -> str:
        return _reply({"status": "failed",
                       "error": "Flight and hotel cards show only in a Hermes HQ app chat, and this turn isn't one. "
                                "Put the options in your reply instead, and never book or pay without the user's "
                                "clear yes."})

    def handle_flights(args, session_id="", **kwargs):
        return show_flights(args) if serves() else outside()

    def handle_hotels(args, session_id="", **kwargs):
        return show_hotels(args) if serves() else outside()

    def on() -> bool:
        try:
            root = data_dir()
            return switched_on(root) and cards_shown(root)
        except Exception:
            return False

    ctx.register_tool(name=FLIGHTS_TOOL, toolset=TOOLSET, check_fn=on, schema=FLIGHTS_SCHEMA,
                      handler=handle_flights, description=FLIGHTS_SCHEMA["description"], emoji="✈️")
    ctx.register_tool(name=HOTELS_TOOL, toolset=TOOLSET, check_fn=on, schema=HOTELS_SCHEMA, handler=handle_hotels,
                      description=HOTELS_SCHEMA["description"], emoji="🏨")
