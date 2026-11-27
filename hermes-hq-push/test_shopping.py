"""Shopping cards (0.9.60, shopping.py): the two tools, their checks, the ids, the card switch, the person's answers,
the route and the status."""
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent

if "hermes_hq_shopping" in sys.modules:
    shopping = sys.modules["hermes_hq_shopping"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_shopping", HERE / "shopping.py")
    shopping = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_shopping"] = shopping
    _spec.loader.exec_module(shopping)

# The vectors the app's Tests/Shopping hold too: both sides must make these ids.
PRODUCT_ID = "p-d6dc20a2"
ORDER_ID = "o-4a928c2e"

SHOE = {"title": "Trail Runner 2", "store": "Example Outfitters", "url": "https://example.com/p/trail-runner-2",
        "price": 129.99, "currency": "USD", "was": 159.99, "image": "https://example.com/img/trail-runner-2.jpg",
        "rating": 4.6, "reviews": 1234, "badge": "Best value", "stock": "in", "delivery": "Free shipping, arrives Fri",
        "why": ["Light and grippy on wet rock"], "caution": ["Runs half a size small"],
        "options": [{"name": "Colour", "value": "Navy"}], "specs": [{"label": "Weight", "value": "280 g"}],
        "offers": [{"store": "Example Sports", "url": "https://example.net/trail-runner-2", "price": 134.0, "was": 0,
                    "stock": "in"}]}
ORDER = {"store": "Example Outfitters", "site": "https://example.com/checkout",
         "items": [{"title": "Trail Runner 2", "detail": "Navy, 10", "qty": 1, "price": 129.99},
                   {"title": "Wool Socks", "qty": 2, "price": 0}],
         "charges": [{"label": "Shipping", "amount": 0}, {"label": "Tax", "amount": 10.4}], "total": 140.39,
         "currency": "USD", "ship_to": {"name": "Sam Example", "address": "1 Example Way"},
         "pay_with": "Visa ending 4242", "arrives": "Fri, Nov 20"}


ROOT = {}


@pytest.fixture(autouse=True)
def fresh(tmp_path):
    """Each test's orders file lives in its own folder (the add-on's data folder in Hermes)."""
    ROOT["dir"] = tmp_path / "plugin-data"
    ROOT["dir"].mkdir()
    shopping._asks.clear()
    yield
    shopping._asks.clear()


def root():
    return ROOT["dir"]


def orders():
    return shopping._load(root())


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def registered(tmp_path, serves=True):
    ctx = Ctx()
    shopping.register_tool(ctx, lambda: tmp_path, lambda: serves)
    return ctx.tools


def shown(*products, **extra):
    return json.loads(shopping.show({"products": list(products) or [SHOE], **extra}, "s1"))


def ordered(session="s1", **changes):
    return json.loads(shopping.order({**ORDER, **changes}, session, root()))


def refusal(reply: dict) -> str:
    assert reply["status"] == "failed", reply
    assert reply["error"].startswith("Nothing was shown.")
    return reply["error"]


# MARK: The ids (the app's ShoppingCards.productId / orderId)

def test_the_ids_match_the_apps_vectors():
    assert shopping.product_id("Trail Runner 2", "Example Outfitters", "https://example.com/p/trail-runner-2") == PRODUCT_ID
    assert PRODUCT_ID == "p-" + hashlib.sha256(
        b"Trail Runner 2\nExample Outfitters\nhttps://example.com/p/trail-runner-2").hexdigest()[:8]
    assert shopping.order_id("Example Outfitters", 140.39, "USD", [(1, "Trail Runner 2"), (2, "Wool Socks")]) == ORDER_ID
    assert ORDER_ID == "o-" + hashlib.sha256(
        b"Example Outfitters\n140.39\nUSD\n1 x Trail Runner 2\n2 x Wool Socks").hexdigest()[:8]


def test_the_tools_answer_with_those_ids():
    assert shown()["id"] == PRODUCT_ID and shown()["ids"] == [PRODUCT_ID]
    assert ordered()["id"] == ORDER_ID
    # Trimmed on both sides; a missing qty is 1, a missing currency USD; the total with two decimals.
    padded = {**SHOE, "title": "  Trail Runner 2\n", "store": " Example Outfitters "}
    assert shown(padded)["id"] == PRODUCT_ID
    items = [{"title": " Trail Runner 2 "}, {"title": "Wool Socks", "qty": 2}]
    assert ordered(items=items, currency=None, charges=None, total=140.390)["id"] == ORDER_ID
    assert ordered(total=140.4)["id"] != ORDER_ID


# MARK: show_products

def test_a_good_call_is_shown_and_says_what_comes_next():
    two = {**SHOE, "title": "Trail Runner 3", "url": "https://example.com/p/trail-runner-3"}
    reply = shown(SHOE, two, note="The 2 is the better buy.", follow_ups=["Show cheaper ones"])
    assert reply["products"] == "shown" and reply["ids"][0] == PRODUCT_ID and len(reply["ids"]) == 2
    assert reply["note"] == shopping.SHOWN_NOTE
    assert "Don't repeat" in reply["note"] and "order_card" in reply["note"]


def test_only_title_store_and_url_and_price_are_needed():
    bare = {"title": "Trail Runner 2", "store": "Example Outfitters", "url": "https://example.com/p/trail-runner-2",
            "price": 129.99}
    assert shown(bare)["id"] == PRODUCT_ID
    # Out of stock with no price on the page: shown without one.
    assert shown({**bare, "price": None, "stock": "out"})["products"] == "shown"


def test_harmless_extras_pass():
    reply = shown({**SHOE, "sku": "TR2-NVY", "was": 0, "images": [], "offers": [], "why": "Light"}, extra="x")
    assert reply["products"] == "shown"
    # An old price equal to the price is no old price: the card shows none either way.
    assert shown({**SHOE, "was": 129.99})["products"] == "shown"
    # A plain numeric string is a number.
    assert shown({**SHOE, "price": "129.99", "was": "159.99"})["products"] == "shown"


def test_long_text_is_shown_and_cut_and_said_back():
    reply = shown({**SHOE, "title": "Trail Runner 2 " + "x" * 200, "why": ["y" * 300]})
    assert reply["products"] == "shown"
    assert "will be cut" in reply["note"] and "`title` (160)" in reply["note"] and "`why` (120 each)" in reply["note"]


@pytest.mark.parametrize("change, words", [
    ({"url": "http://example.com/p/trail-runner-2"}, "only a full https:// link"),
    ({"url": "/p/trail-runner-2"}, "only a full https:// link"),
    ({"url": ""}, "`url` is missing"),
    ({"image": "https://"}, "`image`"),
    ({"images": ["https://example.com/a.jpg", "ftp://example.com/b.jpg"]}, "`images[1]`"),
    ({"images": ["https://example.com/a.jpg"] * 9}, "`images` has 9"),
    ({"price": "$129.99"}, "no currency sign"),
    ({"price": "129.99 USD"}, "plain number"),
    ({"price": "1,299.00"}, "commas"),
    ({"price": None}, "`price` is missing"),
    ({"price": -1}, "can't be below 0"),
    ({"price": True}, "`price` is a number"),
    ({"was": 99.0}, "must be above `price`"),
    ({"rating": 9}, "out of 5"),
    ({"rating": "4.6 stars"}, "`rating`"),
    ({"rating": None, "reviews": 1234}, "`reviews` needs the `rating`"),
    ({"reviews": 12.5}, "whole number"),
    ({"stock": "sold out"}, "in, out, preorder or backorder"),
    ({"currency": "$"}, "3-letter ISO code"),
    ({"currency": "usd"}, "capitals"),
    ({"badge": ["Best value", "Lightest"]}, "one badge per card"),
    ({"why": ["a", "b", "c", "d"]}, "`why` has 4"),
    ({"caution": ["a", "b", "c"]}, "`caution` has 3"),
    ({"options": [{"name": "Colour", "value": "Navy"}, {"name": "Colour", "value": "Red"}]}, "same option twice"),
    ({"options": [{"name": "Colour"}]}, "both filled in"),
    ({"options": {"Colour": "Navy"}}, "is a list of"),
    ({"specs": [{"label": "Weight", "value": "1 kg"}] * 9}, "`specs` has 9"),
    ({"offers": [{"store": "Example Sports", "url": "https://example.net/x", "price": 1}] * 6}, "`offers` has 6"),
    ({"offers": [{"store": "Example Sports", "price": 134.0}]}, "offer 1: `url` is missing"),
    ({"offers": [{"store": "Example Sports", "url": "https://example.net/x", "price": 134.0, "currency": "EUR"}]},
     "one currency per product"),
    ({"offers": [{"store": "Example Sports", "url": "https://example.net/x", "price": 134.0, "was": 100}]},
     "must be above"),
    ({"title": "  "}, "`title` is empty"),
    ({"store": None}, "`store` is missing"),
])
def test_each_wrong_product_is_refused_in_words(change, words):
    error = refusal(shown({**SHOE, **change}))
    assert words in error, error
    assert "Product 1" in error


@pytest.mark.parametrize("args, words", [
    ({"products": []}, "`products` is empty"),
    ({}, "`products` is empty"),
    ({"products": "Trail Runner 2"}, "`products` is a list"),
    ({"products": [SHOE] * 13}, "`products` has 13"),
    ({"products": [SHOE, SHOE]}, "list each once"),
    ({"products": ["Trail Runner 2"]}, "is an object"),
    ({"products": [SHOE], "follow_ups": ["a", "b", "c", "d"]}, "`follow_ups` has 4"),
    ("not a dict", "The arguments are an object"),
])
def test_each_wrong_call_is_refused_in_words(args, words):
    error = refusal(json.loads(shopping.show(args, "s1")))
    assert words in error, error


def test_a_refusal_names_every_problem_at_once():
    error = refusal(shown({**SHOE, "price": "$5", "stock": "gone", "rating": 7}))
    assert "no currency sign" in error and "backorder" in error and "out of 5" in error


# MARK: order_card

def test_an_order_waits_and_is_kept():
    reply = ordered()
    assert reply == {"order": "waiting", "id": ORDER_ID, "note": shopping.WAITING_NOTE}
    assert "must not pay" in reply["note"]
    assert shopping.order_state(root(), ORDER_ID) == "waiting"


@pytest.mark.parametrize("change, words", [
    ({"store": ""}, "`store` is missing"),
    ({"items": []}, "`items` is empty"),
    ({"items": [{"title": "Trail Runner 2"}] * 21}, "`items` has 21"),
    ({"items": [{"qty": 1}]}, "`title` is missing"),
    ({"items": [{"title": "Trail Runner 2", "qty": 0}]}, "whole number from 1"),
    ({"items": [{"title": "Trail Runner 2", "qty": 1.5}]}, "whole number from 1"),
    ({"items": [{"title": "Trail Runner 2", "price": "$129.99"}]}, "no currency sign"),
    ({"total": None}, "`total` is missing"),
    ({"total": "$140.39"}, "no currency sign"),
    ({"total": -3}, "can't be below 0"),
    ({"total": 150.0}, "come to 140.39, not the `total` 150.00"),
    ({"charges": [{"label": "Tax", "amount": 1}] * 7}, "`charges` has 7"),
    ({"charges": [{"label": "Tax"}]}, "`amount` is missing"),
    ({"charges": [{"amount": 3}]}, "`label` is missing"),
    ({"site": "http://example.com/checkout"}, "only a full https:// link"),
    ({"currency": "dollars"}, "3-letter ISO code"),
    ({"ship_to": "1 Example Way"}, "`ship_to` is"),
    ({"pay_with": "4242 4242 4242 4242"}, "full card number"),
    ({"pay_with": "Visa 4242-4242-4242-4242"}, "full card number"),
])
def test_each_wrong_order_is_refused_in_words(change, words):
    error = refusal(ordered(**change))
    assert words in error, error
    assert orders() == {}


def test_an_order_item_can_name_its_own_page_and_options():
    # 0.9.86: the card opens an item's product page on a tap (the app's ShoppingCards.product(for:in:)).
    items = [{"title": "Trail Runner 2", "detail": "Navy, 10", "qty": 1, "price": 129.99,
              "url": "https://example.com/p/trail-runner-2",
              "options": [{"name": "Colour", "value": "Navy"}, {"name": "Size", "value": "10"}]},
             {"title": "Wool Socks", "qty": 2, "price": 0}]
    reply = ordered(items=items)
    assert reply["order"] == "waiting"
    # The order id hashes the store, total, currency and each item's `qty x title` only, on both sides.
    assert reply["id"] == ORDER_ID
    props = shopping.ORDER_SCHEMA["parameters"]["properties"]["items"]["items"]["properties"]
    assert {"url", "options"} <= set(props) and props["options"]["items"]["required"] == ["name", "value"]


@pytest.mark.parametrize("item, words", [
    ({"url": "http://example.com/p/1"}, "only a full https:// link"),
    ({"url": "/p/trail-runner-2"}, "only a full https:// link"),
    ({"url": "https://example.com/" + "a" * 2100}, "longer than 2048"),
    ({"options": {"Colour": "Navy"}}, "`options` is a list of"),
    ({"options": [{"name": "Colour"}]}, "with both filled in"),
    ({"options": [{"name": f"N{i}", "value": "v"} for i in range(5)]}, "`options` has 5"),
    ({"options": [{"name": "Colour", "value": "Navy"}, {"name": "colour", "value": "Red"}]}, "the same option twice"),
])
def test_a_wrong_items_page_or_options_are_refused(item, words):
    error = refusal(ordered(items=[{"title": "Trail Runner 2", **item}], charges=None))
    assert words in error, error
    assert orders() == {}


def test_the_sum_check_takes_a_line_amount_or_a_price_each_and_a_discount():
    items = [{"title": "Trail Runner 2", "qty": 1, "price": 129.99}, {"title": "Wool Socks", "qty": 2, "price": 6.0}]
    each = [{"label": "Tax", "amount": 10.4}, {"label": "Code SAVE12", "amount": -12.0}]
    assert ordered(items=items, charges=each, total=140.39)["order"] == "waiting"           # 2 x 6.00 each
    line = [{"title": "Trail Runner 2", "qty": 1, "price": 129.99}, {"title": "Wool Socks", "qty": 2, "price": 12.0}]
    assert ordered(items=line, charges=each, total=140.39)["order"] == "waiting"            # 12.00 for the line
    # Without charges, or with an unpriced item, the card can't add it up: no check.
    assert ordered(charges=None, total=150.0)["order"] == "waiting"
    assert ordered(items=[{"title": "Trail Runner 2"}], total=150.0)["order"] == "waiting"


def test_a_newer_order_in_the_chat_replaces_the_waiting_one():
    first = ordered()["id"]
    second = ordered(total=150.0, charges=None)["id"]
    other_chat = ordered("s2", total=160.0, charges=None)["id"]
    assert shopping.order_state(root(), first) == "replaced"
    assert shopping.order_state(root(), second) == "waiting" and shopping.order_state(root(), other_chat) == "waiting"


# MARK: The person's answers

def test_only_the_apps_own_first_line_is_an_answer():
    assert shopping.decision(f"[Hermes HQ order {ORDER_ID}: pay]\nPlace this order now") == (ORDER_ID, "pay")
    assert shopping.decision(f"  [Hermes HQ order {ORDER_ID}: cancel]") == (ORDER_ID, "cancel")
    assert shopping.decision(f"[Hermes HQ shopping {PRODUCT_ID}: buy]\nBuy this") == (PRODUCT_ID, "buy")
    assert shopping.decision(f"[Hermes HQ shopping {PRODUCT_ID}: watch stock]") == (PRODUCT_ID, "watch stock")
    assert shopping.decision(f"[Hermes HQ shopping {PRODUCT_ID}: watch price]") == (PRODUCT_ID, "watch price")
    assert shopping.decision(f"please [Hermes HQ order {ORDER_ID}: pay]") is None
    assert shopping.decision(f"[Hermes HQ order {PRODUCT_ID}: pay]") is None
    assert shopping.decision(f"[Hermes HQ shopping {PRODUCT_ID}: pay]") is None
    assert shopping.decision("[Hermes HQ order o-XYZ: pay]") is None
    assert shopping.decision(None) is None
    assert shopping.decision([{"type": "text", "text": f"[Hermes HQ order {ORDER_ID}: pay]"}]) is None


def test_pay_and_cancel_settle_the_order_and_buy_is_kept():
    ordered()
    shopping.note_decision(root(), f"[Hermes HQ order {ORDER_ID}: pay]\nPlace this order now.", "s1")
    assert shopping.order_state(root(), ORDER_ID) == "pay"
    # An id this computer never showed settles nothing.
    shopping.note_decision(root(), "[Hermes HQ order o-0000000a: cancel]\nDon't place this order.", "s1")
    assert shopping.order_state(root(), "o-0000000a") is None
    shopping.note_decision(root(), f"[Hermes HQ shopping {PRODUCT_ID}: buy]\nBuy this for me", "s1")
    assert shopping._asks[PRODUCT_ID]["ask"] == "buy"
    shopping.note_decision(root(), "pay it", "s1")
    assert shopping.order_state(root(), ORDER_ID) == "pay"


def test_old_answers_are_forgotten():
    shopping.remember_order(root(), "s1", "o-00000001", "Example Outfitters", 1.0, "USD", now=0.0)
    shopping.remember_order(root(), "s2", "o-00000002", "Example Outfitters", 1.0, "USD", now=shopping.TTL + 1)
    # The first lapsed then (a late Pay still gets told it expired); a week after that it is gone.
    assert orders()["o-00000001"]["state"] == "lapsed"
    shopping.remember_order(root(), "s3", "o-00000003", "Example Outfitters", 1.0, "USD", now=2 * shopping.TTL + 2)
    assert list(orders()) == ["o-00000002", "o-00000003"]
    for i in range(shopping.KEEP + 5):
        shopping.remember_order(root(), f"s{i}", f"o-{i:08x}", "Example Outfitters", 1.0, "USD",
                                now=2 * shopping.TTL + 3 + i)
    assert len(orders()) == shopping.KEEP


# MARK: Offered only when

def test_the_tools_show_only_once_an_app_draws_the_cards(tmp_path):
    tools = registered(tmp_path)
    assert set(tools) == {"show_products", "order_card"}
    assert all(t["toolset"] == "hq_shopping" for t in tools.values())
    assert [t["check_fn"]() for t in tools.values()] == [False, False]
    shopping.note_cards(tmp_path)
    assert [t["check_fn"]() for t in tools.values()] == [True, True]
    (tmp_path / shopping.SWITCH).touch()
    assert [t["check_fn"]() for t in tools.values()] == [False, False]
    assert shopping.status(tmp_path) == {"shoppingCards": False, "shoppingCardsShown": True, "shoppingPayBlock": True}


def test_status_offers_cards_before_any_app_asked(tmp_path):
    assert shopping.status(tmp_path) == {"shoppingCards": True, "shoppingCardsShown": False, "shoppingPayBlock": True}


def test_a_switch_that_cannot_be_read_keeps_the_tools_away():
    ctx = Ctx()
    shopping.register_tool(ctx, lambda: (_ for _ in ()).throw(OSError("gone")), lambda: True)
    assert ctx.tools["show_products"]["check_fn"]() is False


def test_a_turn_outside_the_app_is_told_to_answer_in_its_reply(tmp_path):
    tools = registered(tmp_path, serves=False)
    for name, args in (("show_products", {"products": [SHOE]}), ("order_card", ORDER)):
        reply = json.loads(tools[name]["handler"](args, session_id="s1", task_id="t"))
        assert reply["status"] == "failed" and "reply" in reply["error"]
    assert orders() == {}


def test_the_handlers_take_hermes_arguments(tmp_path):
    tools = registered(tmp_path)
    assert json.loads(tools["show_products"]["handler"]({"products": [SHOE]}, session_id="s1", task_id="t"))["id"] == PRODUCT_ID
    assert json.loads(tools["order_card"]["handler"](ORDER, session_id="s1", task_id="t"))["id"] == ORDER_ID
    assert shopping._load(tmp_path)[ORDER_ID]["session"] == "s1"


def test_the_teaching_text_says_what_matters():
    products = shopping.PRODUCTS_SCHEMA["description"]
    for words in ("find, compare or recommend", "One call", "never guess or invent", "don't repeat",
                  "[Hermes HQ shopping <product id>: buy]", "watch stock", "watch price", "order_card",
                  "never place the order yourself", "scheduled job"):
        assert words in products, words
    order = shopping.ORDER_SCHEMA["description"]
    for words in ("last step of checkout", "exactly as that page says", "only for something the user asked",
                  "never press the store's final button", "[Hermes HQ order <id>: pay]", "exact id",
                  "[Hermes HQ order <id>: cancel]", "call order_card again", "new card",
                  "without stopping to ask about what you already know", "The card is the one ask"):   # 0.9.82
        assert words in order, words
    for schema in (shopping.PRODUCTS_SCHEMA, shopping.ORDER_SCHEMA):
        assert set(schema["parameters"]["required"]) <= set(schema["parameters"]["properties"])


# MARK: The route and the add-on

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "off")
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_shopping_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    api.push._watcher_started = True  # no reply watcher in a test
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    return TestClient(app), api


def test_the_route_turns_the_tools_on_and_the_status_says_so(client, tmp_path):
    test, api = client
    assert api.shopping is shopping
    base = "/api/plugins/hermes-hq-push"
    before = test.get(f"{base}/status").json()
    assert before["shoppingCards"] is True and before["shoppingCardsShown"] is False
    assert "draftCards" in before
    assert test.put(f"{base}/shopping/cards").json() == {"ok": True}
    assert (tmp_path / "data" / shopping.CARDS).exists()
    after = test.get(f"{base}/status").json()
    assert after["shoppingCards"] is True and after["shoppingCardsShown"] is True and after["shoppingPayBlock"] is True


def test_through_the_addon_the_way_hermes_calls_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hermes_hq_push_addon_shopping", HERE / "__init__.py")
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    assert addon.shopping is shopping
    ctx = Ctx()
    addon._shopping_tools(ctx)
    assert {"show_products", "order_card"} <= set(ctx.tools)
    tool = ctx.tools["order_card"]
    assert tool["check_fn"]() is False
    shopping.note_cards(tmp_path)
    assert tool["check_fn"]() is True
    # A turn outside `hermes serve`: the handler says to answer in the reply.
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    assert json.loads(tool["handler"](ORDER, session_id="chat-1"))["status"] == "failed"
    # One `hermes serve` runs: the order waits in that chat.
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setitem(sys.modules, "tui_gateway.server", types.ModuleType("tui_gateway.server"))
    assert json.loads(tool["handler"](ORDER, session_id="chat-1"))["id"] == ORDER_ID
    # The pay block, through the add-on's own hooks: a click waits for the card, a typed yes lifts nothing, Cancel does.
    held = addon.pre_tool_call(tool_name="browser_exec", args={"code": "click('#place-order')"}, session_id="chat-1")
    assert held["action"] == "block" and "Example Outfitters" in held["message"] and "$140.39" in held["message"]
    assert addon.pre_tool_call(tool_name="browser_exec", args={"code": "x"}, session_id="chat-2") is None
    assert addon.pre_tool_call(tool_name="web_search", args={"query": "x"}, session_id="chat-1") is None
    addon.pre_llm_call(user_message="yes go ahead", session_id="chat-1")
    assert addon.pre_tool_call(tool_name="browser_exec", args={"code": "x"}, session_id="chat-1")["action"] == "block"
    addon.pre_llm_call(user_message=f"[Hermes HQ order {ORDER_ID}: cancel]\nDon't place this order.", session_id="chat-1")
    assert shopping.order_state(tmp_path, ORDER_ID) == "cancel"
    assert addon.pre_tool_call(tool_name="browser_exec", args={"code": "x"}, session_id="chat-1") is None


# MARK: The pay block

ACTORS = ["browser_exec", "browser_cdp", "browser_dialog", "browser_vault", "browser_ask_user",
          "browser_navigate", "browser_click", "browser_type", "browser_press", "browser_back", "browser_console",
          "browser_vault_fill", "browser_vault_save_login", "browser_select_option", "browser_upload_file",
          "mcp_playwright_browser_click", "browser_x", "BROWSER_CLICK",
          "drive_preview", "computer_use", "link_card_stage"]
READERS = ["browser_snapshot", "browser_screenshot", "browser_vision", "browser_get_images", "browser_get_text",
           "browser_get_content", "browser_list_tabs", "browser_tabs", "browser_scroll", "mcp_playwright_browser_snapshot"]
OTHERS = ["web_search", "web_extract", "cronjob", "clarify", "terminal", "read_file", "link_card_cleanup",
          "order_card", "show_products", "vision_analyze", "annotate_preview", "send_message"]


def waits(session="s1"):
    ordered(session)
    return root()


@pytest.mark.parametrize("tool", ACTORS)
def test_each_acting_browser_tool_is_refused_while_the_card_waits(tool):
    message = shopping.blocked_message(waits(), tool, {}, "s1")
    assert message == shopping.BLOCKED.format(store="Example Outfitters", total="$140.39")
    assert message.startswith("Nothing was done.") and "tap Pay or Cancel" in message


@pytest.mark.parametrize("tool", READERS + OTHERS)
def test_reading_and_every_other_tool_stay_allowed(tool):
    assert shopping.blocked_message(waits(), tool, {}, "s1") is None


def test_the_preview_pane_reads_but_doesnt_open_pages():
    data = waits()
    assert shopping.blocked_message(data, "desktop_preview", {"action": "read"}, "s1") is None
    assert shopping.blocked_message(data, "desktop_preview", {"action": "open", "target": "https://example.com"}, "s1")
    assert shopping.blocked_message(data, "desktop_preview", None, "s1")


def test_nothing_is_blocked_before_a_card_or_in_another_chat():
    assert shopping.blocked_message(root(), "browser_exec", {}, "s1") is None
    waits("s1")
    assert shopping.blocked_message(root(), "browser_exec", {}, "s2") is None
    assert shopping.blocked_message(root(), "browser_exec", {}, "") is None


def test_a_typed_yes_lifts_nothing():
    data = waits()
    for words in ("yes go ahead and buy it", "pay", "[Hermes HQ order: pay]", f"please [Hermes HQ order {ORDER_ID}: pay]"):
        assert shopping.note_decision(data, words, "s1") is None
        assert shopping.blocked_message(data, "browser_click", {}, "s1")


def test_pay_on_the_card_lifts_it():
    data = waits()
    shopping.note_decision(data, f"[Hermes HQ order {ORDER_ID}: pay]\nPlace this order now.", "s1")
    assert shopping.blocked_message(data, "browser_click", {}, "s1") is None


def test_cancel_on_the_card_lifts_it():
    data = waits()
    shopping.note_decision(data, f"[Hermes HQ order {ORDER_ID}: cancel]\nDon't place this order.", "s1")
    assert shopping.blocked_message(data, "browser_click", {}, "s1") is None


def test_an_answer_for_another_id_or_chat_lifts_nothing():
    data = waits()
    shopping.note_decision(data, "[Hermes HQ order o-0123abcd: pay]", "s1")
    shopping.note_decision(data, f"[Hermes HQ order {ORDER_ID}: pay]", "s2")
    assert shopping.blocked_message(data, "browser_click", {}, "s1")
    assert shopping.order_state(data, ORDER_ID) == "waiting"


def test_a_newer_card_carries_the_block_and_the_old_ones_pay_doesnt_lift_it():
    data = waits()
    newer = ordered(total=150.0, charges=None)["id"]
    assert shopping.blocked_message(data, "browser_click", {}, "s1") == shopping.BLOCKED.format(
        store="Example Outfitters", total="$150.00")
    note = shopping.note_decision(data, f"[Hermes HQ order {ORDER_ID}: pay]", "s1")
    assert "replaced" in note and "Don't place the order" in note
    assert shopping.blocked_message(data, "browser_click", {}, "s1")
    shopping.note_decision(data, f"[Hermes HQ order {newer}: pay]", "s1")
    assert shopping.blocked_message(data, "browser_click", {}, "s1") is None


def test_a_restart_keeps_the_block():
    data = waits()
    # A new process: a fresh copy of the module, reading the same file.
    spec = importlib.util.spec_from_file_location("hermes_hq_shopping_after_restart", HERE / "shopping.py")
    again = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(again)
    assert again.blocked_message(data, "browser_click", {}, "s1")
    again.note_decision(data, f"[Hermes HQ order {ORDER_ID}: pay]", "s1")
    assert shopping.blocked_message(data, "browser_click", {}, "s1") is None
    state = json.loads((data / shopping.STATE).read_text())
    assert state["version"] == 1 and state["orders"][ORDER_ID]["state"] == "pay"
    assert state["orders"][ORDER_ID]["total"] == 140.39 and state["orders"][ORDER_ID]["store"] == "Example Outfitters"
    assert oct((data / shopping.STATE).stat().st_mode & 0o777) == "0o600"
    assert not list(data.glob("*.tmp"))


def test_a_card_nobody_answers_for_a_day_lapses():
    data = root()
    shopping.remember_order(data, "s1", ORDER_ID, "Example Outfitters", 140.39, "USD", now=1000.0)
    assert shopping.blocked_message(data, "browser_click", {}, "s1", now=1000.0 + shopping.LAPSE - 1)
    later = 1000.0 + shopping.LAPSE + 1
    assert shopping.blocked_message(data, "browser_click", {}, "s1", now=later) is None
    note = shopping.note_decision(data, f"[Hermes HQ order {ORDER_ID}: pay]", "s1", now=later)
    assert "expired" in note and "new card" in note
    assert shopping.order_state(data, ORDER_ID, now=later) == "lapsed"


@pytest.mark.parametrize("junk", ["{not json", "[]", '{"orders": []}', "\x00\xff"])
def test_a_corrupt_file_locks_nothing(junk):
    data = root()
    (data / shopping.STATE).write_text(junk)
    assert shopping.blocked_message(data, "browser_click", {}, "s1") is None
    assert ordered()["order"] == "waiting"      # and the next card writes a good file
    assert shopping.blocked_message(data, "browser_click", {}, "s1")


def test_shopping_off_switches_the_block_off():
    data = waits()
    (data / shopping.SWITCH).touch()
    assert shopping.blocked_message(data, "browser_click", {}, "s1") is None
    (data / shopping.SWITCH).unlink()
    assert shopping.blocked_message(data, "browser_click", {}, "s1")


def test_the_teaching_text_says_the_browser_is_locked():
    assert "locked" in shopping.PRODUCTS_SCHEMA["description"]
    assert "every browser action in this chat is refused" in shopping.ORDER_SCHEMA["description"]
    assert "locked" in shopping.WAITING_NOTE
