"""Travel cards (0.9.86, travel.py): the two tools, their checks against the app's parser, the ids, the card switch,
the route and the status."""
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent

if "hermes_hq_travel" in sys.modules:
    travel = sys.modules["hermes_hq_travel"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_travel", HERE / "travel.py")
    travel = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_travel"] = travel
    _spec.loader.exec_module(travel)

# The vectors the app's Tests/Travel hold too: both sides must make these ids.
FLIGHT_URL = "https://example.com/flights/ex123?fare=main"
HOTEL_URL = "https://example.com/hotels/example?in=2027-03-06&out=2027-03-09"
FLIGHT_ID = "f-34c7f1d1"        # Example Air, SFO, JFK, "Fri Mar 6, 8:05 AM", FLIGHT_URL
HOTEL_ID = "h-83efb011"         # Hotel Example, "1 Example Way", HOTEL_URL
HOTEL_NO_ADDRESS_ID = "h-18de903b"

FLIGHT = {"airline": "Example Air", "flight_number": "EX 123", "origin": "SFO", "destination": "JFK",
          "depart_time": "Fri Mar 6, 8:05 AM", "arrive_time": "4:40 PM", "duration": "5h 35m", "stops": 0,
          "cabin": "Economy", "fare_tier": "Main", "price": 412.5, "was": 460, "currency": "USD", "url": FLIGHT_URL,
          "badge": "Cheapest", "carry_on": "Carry-on included", "checked_bag": "1st bag 35 USD",
          "change_rule": "Changes free", "refund_rule": "Non-refundable", "why": ["Nonstop, lands before dinner"],
          "caution": ["No seat choice on this fare"]}
STOPPING = {**FLIGHT, "flight_number": "EX 77", "stops": 1, "stop_summary": "1 stop in DEN (1h 10m)", "price": 318,
            "was": None, "url": "https://example.com/flights/ex77",
            "segments": [{"airline": "Example Air", "flight_number": "EX 77", "origin": "SFO", "destination": "DEN",
                          "depart_time": "6:00 AM", "arrive_time": "9:30 AM", "duration": "2h 30m",
                          "layover": "1h 10m in DEN"},
                         {"airline": "Example Air", "flight_number": "EX 78", "origin": "DEN", "destination": "JFK",
                          "depart_time": "10:40 AM", "arrive_time": "4:20 PM", "duration": "3h 40m"}]}
HOTEL = {"name": "Hotel Example", "url": HOTEL_URL, "neighborhood": "Old Town", "address": "1 Example Way",
         "latitude": 38.71, "longitude": -9.13, "stars": 4, "rating": 8.9, "reviews": 1234, "price_per_night": 189,
         "total_price": 612.4, "currency": "EUR", "was": 220, "image": "https://example.com/img/hotel-example.jpg",
         "images": ["https://example.com/img/hotel-example-2.jpg"], "badge": "Best location",
         "cancellation": "Free cancellation until Mar 4", "breakfast": "Breakfast included",
         "distance": "0.4 km from the centre", "amenities": ["Pool", "Gym"], "why": ["Quiet street, near the tram"],
         "caution": ["Small rooms"]}


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def registered(tmp_path, serves=True):
    ctx = Ctx()
    travel.register_tool(ctx, lambda: tmp_path, lambda: serves)
    return ctx.tools


def flights(*options, **extra):
    return json.loads(travel.show_flights({"flights": list(options) or [FLIGHT], **extra}))


def hotels(*options, **extra):
    return json.loads(travel.show_hotels({"hotels": list(options) or [HOTEL], **extra}))


def refusal(reply: dict) -> str:
    assert reply["status"] == "failed", reply
    assert reply["error"].startswith("Nothing was shown.")
    return reply["error"]


# MARK: The ids (the app's TravelCards.flightId / hotelId)

def test_the_ids_match_the_apps_vectors():
    assert travel.flight_id("Example Air", "SFO", "JFK", "Fri Mar 6, 8:05 AM", FLIGHT_URL) == FLIGHT_ID
    assert FLIGHT_ID == "f-" + hashlib.sha256(
        b"Example Air\nSFO\nJFK\nFri Mar 6, 8:05 AM\nhttps://example.com/flights/ex123?fare=main").hexdigest()[:8]
    assert travel.hotel_id("Hotel Example", "1 Example Way", HOTEL_URL) == HOTEL_ID
    assert HOTEL_ID == "h-" + hashlib.sha256(
        b"Hotel Example\n1 Example Way\nhttps://example.com/hotels/example?in=2027-03-06&out=2027-03-09").hexdigest()[:8]
    assert travel.hotel_id("Hotel Example", "", HOTEL_URL) == HOTEL_NO_ADDRESS_ID


def test_the_ids_trim_and_capitalise_as_the_app_does():
    # Each field trimmed; the airport codes in capitals; the departure as the card shows it (one line, 40 at most).
    assert travel.flight_id(" Example Air\n", "sfo", " jfk ", "  Fri Mar 6, 8:05 AM ", FLIGHT_URL + " ") == FLIGHT_ID
    assert travel.flight_id("Example Air", "SFO", "JFK", "Fri Mar 6,\n8:05 AM", FLIGHT_URL) == travel.flight_id(
        "Example Air", "SFO", "JFK", "Fri Mar 6, 8:05 AM", FLIGHT_URL)
    long = "Friday the sixth of March, at five past eight in the morning"
    assert travel.flight_id("Example Air", "SFO", "JFK", long, FLIGHT_URL) == "f-" + hashlib.sha256(
        ("Example Air\nSFO\nJFK\n" + long[:39].strip() + "…\n" + FLIGHT_URL).encode()).hexdigest()[:8]
    assert travel.hotel_id("  Hotel Example ", " 1 Example Way\n", HOTEL_URL) == HOTEL_ID


def test_the_tools_answer_with_those_ids():
    assert flights()["id"] == FLIGHT_ID and flights()["ids"] == [FLIGHT_ID]
    assert flights({**FLIGHT, "origin": "sfo", "destination": "jfk"})["id"] == FLIGHT_ID
    assert hotels()["id"] == HOTEL_ID
    assert hotels({**HOTEL, "address": None})["id"] == HOTEL_NO_ADDRESS_ID


# MARK: show_flights

def test_a_good_flights_call_is_shown_and_says_what_comes_next():
    reply = flights(FLIGHT, STOPPING, note="The nonstop is worth it.", step="Checking the fare",
                    relaxed_filters=["Morning departure"], follow_ups=["Show later flights"])
    assert reply["flights"] == "shown" and reply["ids"][0] == FLIGHT_ID and len(reply["ids"]) == 2
    assert reply["note"] == travel.SHOWN_NOTE.format(kind="flights")
    assert "order_card" in reply["note"] and "[Hermes HQ travel <id>: book]" in reply["note"]


def test_only_what_a_flight_card_needs_is_required():
    reply = flights({"airline": "Example Air", "origin": "SFO", "destination": "JFK", "price": "199",
                     "url": FLIGHT_URL})
    assert reply["flights"] == "shown"


@pytest.mark.parametrize("change, words", [
    ({"airline": None}, "`airline` is missing"),
    ({"origin": "San Francisco"}, "3-letter IATA"),
    ({"destination": ""}, "`destination` is missing"),
    ({"price": None}, "`price` is missing"),
    ({"price": 0}, "above 0"),
    ({"price": "$412.50"}, "plain number"),
    ({"price": 10_000_000}, "below 10,000,000"),
    ({"url": None}, "`url` is missing"),
    ({"url": "http://example.com/f"}, "only a full https:// link"),
    ({"url": "https://example.com/vols/ré"}, "percent-encoded"),
    ({"was": 300}, "must be above `price`"),
    ({"stops": 1.5}, "whole number from 0 to 9"),
    ({"stops": 12}, "whole number from 0 to 9"),
    ({"currency": "$"}, "3-letter ISO"),
    ({"flight_number": 123}, "is text, in quotes: \"123\""),
    ({"why": "Nonstop"}, "list of short texts"),
    ({"why": ["a", "b", "c", "d"]}, "the card holds 3"),
    ({"badge": ["Cheapest", "Fastest"]}, "one badge per card"),
    ({"segments": [{"origin": "SFO"}] * 7}, "the card holds 6"),
    ({"segments": [{"origin": "SFO", "destination": "Denver"}]}, "leg 1: `destination`"),
    ({"segments": ["SFO-DEN"]}, "leg 1: is an object"),
])
def test_each_wrong_flight_is_refused_in_words(change, words):
    assert words in refusal(flights({**FLIGHT, **change})), words


def test_the_flights_list_itself_is_checked():
    assert "`flights` is empty" in refusal(json.loads(travel.show_flights({"flights": []})))
    assert "the card holds 12" in refusal(json.loads(travel.show_flights(
        {"flights": [{**FLIGHT, "url": f"{FLIGHT_URL}&n={i}"} for i in range(13)]})))
    assert "list each once" in refusal(flights(FLIGHT, dict(FLIGHT)))
    assert "is an object" in refusal(json.loads(travel.show_flights({"flights": ["EX 123"]})))
    assert "the card holds 4" in refusal(flights(relaxed_filters=["a", "b", "c", "d", "e"]))
    assert "arguments are an object" in refusal(json.loads(travel.show_flights("SFO to JFK")))


def test_long_flight_text_is_said_back_not_refused():
    reply = flights({**FLIGHT, "change_rule": "x" * 200, "badge": "A very long badge for one card"})
    assert reply["flights"] == "shown"
    assert "change_rule` (120)" in reply["note"] and "badge` (28)" in reply["note"]


# MARK: show_hotels

def test_a_good_hotels_call_is_shown():
    second = {"name": "Example Inn", "url": "https://example.net/inn", "total_price": 480}
    reply = hotels(HOTEL, second, location="Old Town", check_in="Fri Mar 6", check_out="Mon Mar 9",
                   guests="2 adults, 1 room", follow_ups=["Show cheaper ones"])
    assert reply["hotels"] == "shown" and reply["ids"][0] == HOTEL_ID and len(reply["ids"]) == 2
    assert reply["note"] == travel.SHOWN_NOTE.format(kind="hotels")


@pytest.mark.parametrize("change, words", [
    ({"name": " "}, "`name` is empty"),
    ({"url": "example.com/hotel"}, "only a full https:// link"),
    ({"price_per_night": None, "total_price": None}, "give `price_per_night` or `total_price`"),
    ({"price_per_night": -5}, "above 0"),
    ({"latitude": None}, "go together"),
    ({"latitude": 120}, "between -90 and 90"),
    ({"longitude": -190}, "between -180 and 180"),
    ({"stars": 6}, "whole number from 1 to 5"),
    ({"stars": 3.5}, "whole number from 1 to 5"),
    ({"rating": 11}, "out of 10"),
    ({"reviews": 10.5}, "whole number of reviews"),
    ({"was": 150}, "must be above `price_per_night`"),
    ({"was": 300, "price_per_night": None}, "needs a `price_per_night`"),
    ({"image": "http://example.com/a.jpg"}, "only a full https:// link"),
    ({"images": "https://example.com/a.jpg"}, "list of https links"),
    ({"images": [f"https://example.com/{i}.jpg" for i in range(12)]}, "the card shows 12"),
    ({"images": ["/img/1.jpg"]}, "`images[0]`"),
    ({"amenities": [f"A{i}" for i in range(11)]}, "the card holds 10"),
])
def test_each_wrong_hotel_is_refused_in_words(change, words):
    assert words in refusal(hotels({**HOTEL, **change})), words


def test_the_stay_itself_is_checked():
    assert "`guests` is text" in refusal(hotels(guests=2))
    assert "list each once" in refusal(hotels(HOTEL, dict(HOTEL)))
    assert "`hotels` is empty" in refusal(json.loads(travel.show_hotels({"hotels": []})))
    # A rating out of 10 is taken as it is (the app reads anything above 5 as out of 10).
    assert hotels({**HOTEL, "rating": 4.5})["hotels"] == "shown"


# MARK: The person's taps

def test_only_the_apps_own_first_line_is_an_answer():
    assert travel.decision(f"[Hermes HQ travel {FLIGHT_ID}: book]\nBook this flight for me") == (FLIGHT_ID, "book")
    assert travel.decision(f"  [Hermes HQ travel {HOTEL_ID}: watch price]\nWatch the nightly rate") == (
        HOTEL_ID, "watch price")
    for text in (f"please [Hermes HQ travel {FLIGHT_ID}: book]", f"[Hermes HQ travel {FLIGHT_ID}: pay]",
                 "[Hermes HQ travel p-1a2b3c4d: book]", None, 5):
        assert travel.decision(text) is None, text


# MARK: Offered only when

def test_the_tools_show_only_once_an_app_draws_the_cards(tmp_path):
    tools = registered(tmp_path)
    assert set(tools) == {"show_flights", "show_hotels"}
    assert all(t["toolset"] == "hq_travel" for t in tools.values())
    assert [t["check_fn"]() for t in tools.values()] == [False, False]
    travel.note_cards(tmp_path)
    assert [t["check_fn"]() for t in tools.values()] == [True, True]
    (tmp_path / travel.SWITCH).touch()
    assert [t["check_fn"]() for t in tools.values()] == [False, False]
    assert travel.status(tmp_path) == {"travelCards": False, "travelCardsShown": True}


def test_status_offers_cards_before_any_app_asked(tmp_path):
    assert travel.status(tmp_path) == {"travelCards": True, "travelCardsShown": False}


def test_a_switch_that_cannot_be_read_keeps_the_tools_away():
    ctx = Ctx()
    travel.register_tool(ctx, lambda: (_ for _ in ()).throw(OSError("gone")), lambda: True)
    assert ctx.tools["show_flights"]["check_fn"]() is False


def test_a_turn_outside_the_app_is_told_to_answer_in_its_reply(tmp_path):
    tools = registered(tmp_path, serves=False)
    for name, args in (("show_flights", {"flights": [FLIGHT]}), ("show_hotels", {"hotels": [HOTEL]})):
        reply = json.loads(tools[name]["handler"](args, session_id="s1", task_id="t"))
        assert reply["status"] == "failed" and "reply" in reply["error"]


def test_the_handlers_take_hermes_arguments(tmp_path):
    tools = registered(tmp_path)
    assert json.loads(tools["show_flights"]["handler"]({"flights": [FLIGHT]}, session_id="s1", task_id="t"))["id"] == FLIGHT_ID
    assert json.loads(tools["show_hotels"]["handler"]({"hotels": [HOTEL]}, session_id="s1"))["id"] == HOTEL_ID


def test_the_teaching_text_says_what_matters():
    for schema in (travel.FLIGHTS_SCHEMA, travel.HOTELS_SCHEMA):
        text = schema["description"]
        for words in ("only after you have really looked", "One call", "never guess or invent", "https deep link",
                      "don't repeat", "[Hermes HQ travel <id>: book]", "[Hermes HQ travel <id>: watch price]",
                      "order_card", "never pay or confirm the booking yourself", "scheduled job"):
            assert words in text, words
        assert set(schema["parameters"]["required"]) <= set(schema["parameters"]["properties"])
    flight = travel.FLIGHTS_SCHEMA["parameters"]["properties"]["flights"]["items"]
    assert set(flight["required"]) == {"airline", "origin", "destination", "price", "url"}
    assert {"segments", "relaxed_filters"} <= set(flight["properties"]) | set(
        travel.FLIGHTS_SCHEMA["parameters"]["properties"])
    hotel = travel.HOTELS_SCHEMA["parameters"]["properties"]["hotels"]["items"]
    assert set(hotel["required"]) == {"name", "url"}


# MARK: The route and the add-on

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "off")
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_travel_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    api.push._watcher_started = True  # no reply watcher in a test
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    return TestClient(app), api


def test_the_route_turns_the_tools_on_and_the_status_says_so(client, tmp_path):
    test, api = client
    assert api.travel is travel
    base = "/api/plugins/hermes-hq-push"
    before = test.get(f"{base}/status").json()
    assert before["travelCards"] is True and before["travelCardsShown"] is False
    assert "shoppingCards" in before
    assert test.put(f"{base}/travel/cards").json() == {"ok": True}
    assert (tmp_path / "data" / travel.CARDS).exists()
    after = test.get(f"{base}/status").json()
    assert after["travelCards"] is True and after["travelCardsShown"] is True


def test_through_the_addon_the_way_hermes_calls_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hermes_hq_push_addon_travel", HERE / "__init__.py")
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    assert addon.travel is travel
    ctx = Ctx()
    addon._travel_tools(ctx)
    assert {"show_flights", "show_hotels"} <= set(ctx.tools)
    tool = ctx.tools["show_flights"]
    assert tool["check_fn"]() is False
    travel.note_cards(tmp_path)
    assert tool["check_fn"]() is True
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    assert json.loads(tool["handler"]({"flights": [FLIGHT]}, session_id="chat-1"))["status"] == "failed"
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setitem(sys.modules, "tui_gateway.server", types.ModuleType("tui_gateway.server"))
    assert json.loads(tool["handler"]({"flights": [FLIGHT]}, session_id="chat-1"))["id"] == FLIGHT_ID
