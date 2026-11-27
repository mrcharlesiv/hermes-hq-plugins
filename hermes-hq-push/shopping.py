"""Shopping cards (0.9.60, ``shoppingCards`` in /status): a bot shows the products it found as product cards in the
Hermes HQ app chat (photo, name, price, store; a tap opens the product's page with Buy and Notify Me), and an order it
has taken to the last step of checkout as an order card with Cancel and Pay. Nothing is paid until the person taps Pay.

Two tools, as ``draft_message`` is one (drafts.py): ``show_products`` and ``order_card`` answer at once and tell the bot
to end its reply. The app draws the tool call's arguments as the cards and never reads the result, so all this file
does with a call is check it (a refused call is drawn as nothing, and the bot is told in words what to fix) and say
what comes next. The person's taps come back as ordinary messages in the chat, written by the app, whose first line
names the product or the order:

    [Hermes HQ shopping p-1a2b3c4d: buy]            then the product, its link, and "take it to the last step"
    [Hermes HQ shopping p-1a2b3c4d: watch stock]    a scheduled job that says when it is back
    [Hermes HQ shopping p-1a2b3c4d: watch price]    a scheduled job that says when the price drops
    [Hermes HQ order o-1a2b3c4d: pay]               place this order, at exactly this total
    [Hermes HQ order o-1a2b3c4d: cancel]            don't

Both ids are made from the call's own words (``product_id``, ``order_id``) so the app makes the same ones.

The tools show only once an app that draws the cards has said so on this computer (PUT /shopping/cards writes
``shopping-cards.on`` in the add-on's data folder), so a bot never leaves a card where nobody can see it. Off for every
bot while ``shopping.off`` exists there. Only in turns ``hermes serve`` runs: a turn of the messaging gateway has no app
chat to show a card in.

The pay block (``blocked_message``, the add-on's pre_tool_call): from a shown order card until the person taps Pay or
Cancel on it, every tool that acts in a browser (click, type, navigate, run a script, fill a card) is refused in that
chat; reading the page stays allowed. The orders are kept in ``shopping-orders.json`` in the add-on's data folder, so
a restart doesn't lift a block. Nothing about a product is logged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

PRODUCTS_TOOL = "show_products"
ORDER_TOOL = "order_card"
TOOLS = (PRODUCTS_TOOL, ORDER_TOOL)
SWITCH = "shopping.off"
CARDS = "shopping-cards.on"
TOOLSET = "hq_shopping"

MAX_PRODUCTS = 12
MAX_IMAGES = 8
MAX_WHY = 3
MAX_CAUTION = 2
MAX_OPTIONS = 4
MAX_SPECS = 8
MAX_OFFERS = 5
MAX_FOLLOW_UPS = 3
MAX_ITEMS = 20
MAX_CHARGES = 6
MAX_QTY = 999
MAX_URL = 2048
MAX_AMOUNT = 1e9
STOCK = ("in", "out", "preorder", "backorder")
# What the card shows of each text; longer text is cut there by the app (the call is not refused for it).
LENGTHS = {"note": 200, "title": 160, "store": 60, "brand": 60, "badge": 24, "delivery": 80, "why": 120,
           "caution": 120, "option": 80, "spec": 80, "follow_up": 60, "detail": 120, "label": 40, "pay_with": 60,
           "arrives": 60, "order_note": 160, "ship_name": 80, "ship_address": 200}
KEEP = 200
TTL = 7 * 86400.0

PRODUCT_MARK = re.compile(r"^\s*\[Hermes HQ shopping (p-[0-9a-f]{8}): (buy|watch stock|watch price)\]")
ORDER_MARK = re.compile(r"^\s*\[Hermes HQ order (o-[0-9a-f]{8}): (pay|cancel)\]")
_PLAIN_NUMBER = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_CARD_NUMBER = re.compile(r"\d(?:[ -]?\d){11,}")

_PRICE_WORDS = ("Plain numbers in `currency`, as the store's page shows them: 129.99, never \"$129.99\" or "
                "\"129.99 USD\".")

PRODUCTS_SCHEMA = {
    "name": PRODUCTS_TOOL,
    "description": (
        "Show the user products to buy as product cards in the Hermes HQ app: photo, name, price, store and rating; "
        "a tap opens the product's page with your reasons, other stores' prices and the Buy and Notify Me buttons. "
        "Use it whenever the user asks you to find, compare or recommend something to buy and you have real "
        "products with real prices from store pages you have actually looked at (search, page extract or the "
        "browser). One call holds every product you recommend, best first (up to 12); a comparison of two products "
        "is one call with both. Once the cards are in the chat, answer questions about them (which is better, what "
        "the difference is) in words; call it again only for new products or changed prices. Take every value from the store's own page and leave out any field the page doesn't "
        "show: never guess or invent a rating, a review count, a price, an old price, a photo or the stock. `url` is "
        "the product's own page at that store (not a search or category page); `image` and `images` are the "
        "product's own photos from that page. Your reasons go in `why` and downsides in `caution`, not in your "
        "reply. Don't use it for products you haven't looked up, or when the question isn't about buying something. "
        "This tool buys nothing: it returns {products: \"shown\", ids} at once. Then don't repeat the products' "
        "names, prices or links in your reply: end it with one short line (your pick and why, or a question). The "
        "user's taps arrive as messages: \"[Hermes HQ shopping <product id>: buy]\" (open that product's page in "
        "the browser, check the price and that it is in stock, choose the options it names, take it to the last "
        "step of checkout, then call order_card and stop; never place the order yourself: once the card is shown "
        "your browser is locked in this chat until they tap Pay or Cancel), \"[Hermes HQ shopping "
        "<product id>: watch stock]\" or \"[... : watch price]\" (set up a scheduled job, with the "
        "product-price-monitor skill or your price watch job if you have one, that checks the page and messages "
        "them only when it is back in stock or below the price; then say in one line that the watch is set)."),
    "parameters": {
        "type": "object",
        "properties": {
            "note": {"type": "string",
                     "description": "Optional. One short line shown above the cards: which one you'd pick and why."},
            "products": {
                "type": "array",
                "description": f"The products, best first: 1 to {MAX_PRODUCTS}. Each one once.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "The product's name as the store's page gives it."},
                        "store": {"type": "string", "description": "Who sells it at this price: Example Outfitters."},
                        "url": {"type": "string",
                                "description": "https link to this product's own page at that store."},
                        "price": {"type": "number",
                                  "description": "What it costs there now. " + _PRICE_WORDS + " Leave it out only "
                                                 "when it is out of stock and the page shows no price."},
                        "currency": {"type": "string", "description": "ISO 4217 code: USD (the default), EUR, GBP…"},
                        "was": {"type": "number",
                                "description": "The struck-through old price, only when the page shows one higher "
                                               "than `price`; otherwise leave it out."},
                        "image": {"type": "string",
                                  "description": "https link to the product's main photo, from its store page. Leave "
                                                 "it out rather than use a logo, a placeholder or a guessed link."},
                        "images": {"type": "array", "items": {"type": "string"},
                                   "description": f"Optional further photos of it from the page, up to {MAX_IMAGES}."},
                        "brand": {"type": "string", "description": "Optional. Who makes it."},
                        "rating": {"type": "number",
                                   "description": "The page's star rating out of 5. Leave it out when the page shows "
                                                  "none."},
                        "reviews": {"type": "integer", "description": "How many reviews that rating is from."},
                        "badge": {"type": "string",
                                  "description": "Optional, one or two words on the card: Best value, Lightest."},
                        "stock": {"type": "string", "enum": list(STOCK),
                                  "description": "As the page says: in (the default), out, preorder or backorder."},
                        "delivery": {"type": "string",
                                     "description": "Optional, as the page says: Free shipping, arrives Fri."},
                        "why": {"type": "array", "items": {"type": "string"},
                                "description": f"Up to {MAX_WHY} short reasons it fits what the user asked."},
                        "caution": {"type": "array", "items": {"type": "string"},
                                    "description": f"Up to {MAX_CAUTION} short downsides worth knowing."},
                        "options": {
                            "type": "array",
                            "description": f"Up to {MAX_OPTIONS}: the options you chose for this price, one value "
                                           "per name (Colour: Navy, Size: M). Buy orders exactly these. Other "
                                           "colours or sizes on offer go in `specs`, not here.",
                            "items": {"type": "object",
                                      "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                                      "required": ["name", "value"]}},
                        "specs": {
                            "type": "array",
                            "description": f"Up to {MAX_SPECS} details from the page: Weight 1.2 kg.",
                            "items": {"type": "object",
                                      "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                                      "required": ["label", "value"]}},
                        "offers": {
                            "type": "array",
                            "description": f"Up to {MAX_OFFERS}: the same product at other stores, from their pages, "
                                           "in the same currency.",
                            "items": {"type": "object",
                                      "properties": {"store": {"type": "string"}, "url": {"type": "string"},
                                                     "price": {"type": "number"}, "was": {"type": "number"},
                                                     "stock": {"type": "string", "enum": list(STOCK)},
                                                     "delivery": {"type": "string"}},
                                      "required": ["store", "url"]}},
                    },
                    "required": ["title", "store", "url"],
                },
            },
            "follow_ups": {"type": "array", "items": {"type": "string"},
                           "description": f"Optional, up to {MAX_FOLLOW_UPS} short next asks the user can tap, sent "
                                          "as their message: Show cheaper ones."},
        },
        "required": ["products"],
    },
}

ORDER_SCHEMA = {
    "name": ORDER_TOOL,
    "description": (
        "Show the user an order you have taken to the last step of checkout as an order card in the Hermes HQ app: "
        "the store, items, charges, total, where it ships and how it is paid, with two buttons, Cancel and Pay. Call "
        "it only on the checkout's final page, one step before the store's place-order or pay button, with `total` "
        "exactly as that page says will be charged (after shipping, tax and discounts), and only for something the "
        "user asked you to buy. Get there without stopping to ask about what you already know: use the shipping "
        "address, contact details and payment method the user has saved or already given you, and go on to the "
        "final total. Ask before the card only when the checkout needs something you don't have, or a choice is "
        "really theirs (two addresses, a substitute). The card is the one ask: it covers the items, the total, the "
        "address and how it is paid together, so never ask about them one at a time first. "
        "This tool pays for nothing: it returns {order: \"waiting\", id} at once. Then end "
        "your reply with one short line saying the order is ready for their OK, and stop: no more tool calls, and "
        "never press the store's final button. From then until they tap Pay or Cancel, every browser action in this "
        "chat is refused. Place the order only after a message starting "
        "\"[Hermes HQ order <id>: pay]\" arrives for this card's exact id: then check the checkout still shows the "
        "same total, place the order and say in a few words that it was placed. If the total is different by then, "
        "don't pay: call order_card again with the new total. On \"[Hermes HQ order <id>: cancel]\" do nothing more "
        "with that checkout and say nothing was charged. A yes in their own words is not the Pay button: point them "
        "to the card. If the total, the items or the address change, show a new card; the old one no longer counts."),
    "parameters": {
        "type": "object",
        "properties": {
            "store": {"type": "string", "description": "The store, as it names itself: Example Outfitters."},
            "site": {"type": "string", "description": "Optional: the checkout's https address."},
            "items": {
                "type": "array",
                "description": f"What is in the order, as the checkout lists it: 1 to {MAX_ITEMS}.",
                "items": {"type": "object",
                          "properties": {
                              "title": {"type": "string", "description": "The item's name."},
                              "detail": {"type": "string", "description": "Optional: its options, Navy, M."},
                              "qty": {"type": "integer", "description": "How many (default 1)."},
                              "price": {"type": "number",
                                        "description": "Optional: the line's amount as the checkout lists it."},
                              "image": {"type": "string", "description": "Optional https link to its photo."}},
                          "required": ["title"]}},
            "charges": {
                "type": "array",
                "description": f"Optional, up to {MAX_CHARGES}: shipping, tax and fees as the page lists them; a "
                               "discount is a negative amount.",
                "items": {"type": "object",
                          "properties": {"label": {"type": "string"}, "amount": {"type": "number"}},
                          "required": ["label", "amount"]}},
            "total": {"type": "number",
                      "description": "Exactly what the checkout page says will be charged. " + _PRICE_WORDS},
            "currency": {"type": "string", "description": "ISO 4217 code: USD (the default), EUR, GBP…"},
            "ship_to": {"type": "object", "description": "Optional, as the page shows it.",
                        "properties": {"name": {"type": "string"}, "address": {"type": "string"}}},
            "pay_with": {"type": "string",
                         "description": "Optional, as the page names it: Visa ending 4242. Never a full card number."},
            "arrives": {"type": "string", "description": "Optional delivery date as the page says: Fri, Nov 20."},
            "note": {"type": "string",
                     "description": "Optional, short: a code that was applied, the return window."},
        },
        "required": ["store", "items", "total"],
    },
}

SHOWN_NOTE = (
    "The user now sees these products as cards (photo, name, price, store) and can open each for its page with Buy "
    "and Notify Me. Nothing has been bought. Don't repeat their names, prices or links: end your reply with one short "
    "line at most (your pick, or a question). A tap arrives as a message starting \"[Hermes HQ shopping <id>: buy]\": "
    "take that product to the last step of checkout and call order_card, never paying first. \"watch stock\" or "
    "\"watch price\": set up a scheduled job that tells them only when it happens.")

WAITING_NOTE = (
    "The user now sees this order as a card with Cancel and Pay. Nothing has been paid, and you must not pay yet. Say "
    "in one short line that the order is ready for their OK and end your reply now: no more tool calls, and don't "
    "press the store's final button. Your browser is locked in this chat until they tap Pay or Cancel on the card. "
    "Their answer arrives as \"[Hermes HQ order <id>: pay]\" (check the checkout "
    "still shows this total, place the order, say it was placed; if the total differs, don't pay: call order_card "
    "again) or \"[Hermes HQ order <id>: cancel]\" (don't place it; say nothing was charged).")

_lock = threading.Lock()
_asks: dict[str, dict] = {}       # product id -> {session, ask: buy|watch stock|watch price, at}


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
    """PUT /shopping/cards: an app that draws product and order cards uses this computer."""
    (Path(data_dir) / CARDS).touch(exist_ok=True)


def status(data_dir: Path) -> dict:
    """`shoppingCards` (0.9.60): show_products and order_card exist here (false while shopping.off turns them off).
    `shoppingCardsShown`: an app has said it draws the cards, so bots have the tools; an app that draws them and reads
    false answers with PUT /shopping/cards. `shoppingPayBlock`: browser actions are refused in a chat while its order
    card waits for Pay or Cancel (blocked_message)."""
    return {"shoppingCards": switched_on(data_dir), "shoppingCardsShown": cards_shown(data_dir), "shoppingPayBlock": True}


# MARK: Ids (the app's ShoppingCards.productId / orderId make the same ones)

def _hex8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def product_id(title: str, store: str, url: str) -> str:
    """"p-" and the first 8 hex digits of SHA-256 over `title\\nstore\\nurl`, each trimmed."""
    return "p-" + _hex8("\n".join(str(v).strip() for v in (title, store, url)))


def order_id(store: str, total: float, currency: str, items: list) -> str:
    """"o-" and the first 8 hex digits of SHA-256 over `store\\ntotal\\ncurrency\\n` and each item's `qty x title`
    joined by newlines (the total with two decimals: 140.39). `items` are (qty, title) pairs."""
    head = "\n".join([str(store).strip(), f"{float(total):.2f}", str(currency).strip()])
    lines = "\n".join(f"{int(qty)} x {str(title).strip()}" for qty, title in items)
    return "o-" + _hex8(head + "\n" + lines)


# MARK: Checking a call

def _reply(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _failed(problems: list[str]) -> str:
    shown = problems[:6]
    more = f" (and {len(problems) - len(shown)} more like these)" if len(problems) > len(shown) else ""
    return _reply({"status": "failed",
                   "error": "Nothing was shown. Fix this and call again: " + " ".join(shown) + more})


class _Check:
    """Collects what is wrong (refused) and what is longer than the card shows (cut by the app, said back)."""

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
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = str(value)
            else:
                self.problems.append(f"{where}`{key}` is text.")
                return ""
        value = value.strip()
        if not value and required:
            self.problems.append(f"{where}`{key}` is empty.")
        limit = LENGTHS[limit_key]
        if len(value) > limit:
            self.long.append(f"{where}`{key}` ({limit})")
        return value

    def texts(self, obj: dict, key: str, where: str, limit_key: str, most: int) -> list[str]:
        value = obj.get(key)
        if value is None or value == "" or value == []:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.problems.append(f"{where}`{key}` is a list of short texts.")
            return []
        value = [v.strip() for v in value if v.strip()]
        if len(value) > most:
            self.problems.append(f"{where}`{key}` has {len(value)}; the card holds {most}. Keep the best {most}.")
        limit = LENGTHS[limit_key]
        if any(len(v) > limit for v in value):
            self.long.append(f"{where}`{key}` ({limit} each)")
        return value

    def number(self, obj: dict, key: str, where: str, low: float | None = 0.0) -> float | None:
        value = obj.get(key)
        if value is None:
            return None
        if isinstance(value, bool):
            self.problems.append(f"{where}`{key}` is a number.")
            return None
        if isinstance(value, str):
            if not value.strip():
                return None
            if _PLAIN_NUMBER.match(value):
                value = float(value)
            else:
                self.problems.append(f"{where}`{key}` is {json.dumps(value, ensure_ascii=False)}: write it as a plain "
                                     "number, 129.99, with no currency sign, code or commas (the currency goes in "
                                     "`currency`).")
                return None
        if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) >= MAX_AMOUNT:
            self.problems.append(f"{where}`{key}` is a number.")
            return None
        if low is not None and value < low:
            self.problems.append(f"{where}`{key}` can't be below {low:g}.")
            return None
        return float(value)

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
        if not re.match(r"^https://[^/\s?#]+\.[^/\s?#]+", value, re.IGNORECASE) or re.search(r"\s", value):
            self.problems.append(f"{named} is {json.dumps(value[:80], ensure_ascii=False)}: only a full https:// link "
                                 "to a real page works (no http, no relative path, no spaces).")
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

    def stock(self, obj: dict, where: str) -> str:
        value = obj.get("stock")
        if value is None or value == "":
            return "in"
        if not isinstance(value, str) or value.strip().lower() not in STOCK:
            self.problems.append(f"{where}`stock` is {json.dumps(value, ensure_ascii=False)}: use in, out, preorder or "
                                 "backorder (or leave it out when the page doesn't say).")
            return "in"
        return value.strip().lower()

    def was(self, obj: dict, where: str, price: float | None) -> None:
        was = self.number(obj, "was", where)
        if was is None or was == 0:
            return
        if price is None:
            if obj.get("price") in (None, ""):
                self.problems.append(f"{where}`was` needs a `price` it is above.")
        elif was < price:   # the same as the price is no old price at all (the card shows none): not refused
            self.problems.append(f"{where}`was` ({was:g}) is the struck-through old price and must be above `price` "
                                 f"({price:g}). Leave it out when the page shows no higher old price.")

    def pairs(self, obj: dict, key: str, where: str, keys: tuple[str, str], most: int) -> None:
        value = obj.get(key)
        if value is None or value == []:
            return
        shape = f"{{\"{keys[0]}\": …, \"{keys[1]}\": …}}"
        if not isinstance(value, list):
            self.problems.append(f"{where}`{key}` is a list of {shape}.")
            return
        if len(value) > most:
            self.problems.append(f"{where}`{key}` has {len(value)}; the page holds {most}.")
        for item in value:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), (str, int, float)) and
                                                     str(item.get(k)).strip() for k in keys):
                self.problems.append(f"{where}each of `{key}` is {shape} with both filled in.")
                return
            if any(len(str(item.get(k)).strip()) > LENGTHS["option"] for k in keys):
                self.long.append(f"{where}`{key}` ({LENGTHS['option']} each)")
                return
        if key == "options":
            names = [str(item.get("name")).strip().casefold() for item in value]
            if len(set(names)) < len(names):
                self.problems.append(f"{where}`options` names the same option twice: they are the one choice this price "
                                     "is for (Colour: Navy), which Buy orders. Put the other choices in `specs` "
                                     "(Colours: Navy, Graphite).")

    def count(self, value, named: str, most: int, least: int = 0) -> list:
        if value is None:
            value = []
        if not isinstance(value, list):
            self.problems.append(f"`{named}` is a list.")
            return []
        if len(value) < least:
            self.problems.append(f"`{named}` is empty: pass at least {least}.")
        if len(value) > most:
            self.problems.append(f"`{named}` has {len(value)}; the card holds {most}. Keep the best {most}.")
        return value


def _where(index: int, title: str, kind: str = "product") -> str:
    return f"{kind.capitalize()} {index + 1}" + (f" ({title[:40]})" if title else "") + ": "


def check_products(args) -> tuple[list[str], list[str], list[str]]:
    """(problems, ids, long text) for a show_products call."""
    c = _Check()
    if not isinstance(args, dict):
        return ["The arguments are an object: {products: [...]}."], [], []
    c.text(args, "note", "", "note")
    products = c.count(args.get("products"), "products", MAX_PRODUCTS, least=1)
    c.texts(args, "follow_ups", "", "follow_up", MAX_FOLLOW_UPS)
    ids: list[str] = []
    for index, product in enumerate(products[:MAX_PRODUCTS + 1]):
        if not isinstance(product, dict):
            c.problems.append(f"Product {index + 1} is an object with `title`, `store`, `url` and `price`.")
            continue
        title = product.get("title") if isinstance(product.get("title"), str) else ""
        where = _where(index, title.strip())
        title = c.text(product, "title", where, "title", required=True)
        store = c.text(product, "store", where, "store", required=True)
        url = c.url(product, "url", where, required=True)
        stock = c.stock(product, where)
        currency = c.currency(product, where)
        price = c.number(product, "price", where)
        if price is None and product.get("price") in (None, "") and stock != "out":
            c.problems.append(f"{where}`price` is missing: give the price the store's page shows (leave it out only "
                              "for an out-of-stock product whose page shows none).")
        c.was(product, where, price)
        if product.get("image") not in (None, ""):
            c.url(product, "image", where)
        images = product.get("images")
        if images not in (None, []):
            if not isinstance(images, list):
                c.problems.append(f"{where}`images` is a list of https links.")
            else:
                if len(images) > MAX_IMAGES:
                    c.problems.append(f"{where}`images` has {len(images)}; the page shows {MAX_IMAGES}.")
                for i, image in enumerate(images[:MAX_IMAGES + 1]):
                    c.link(image, f"{where}`images[{i}]`")
        c.text(product, "brand", where, "brand")
        if isinstance(product.get("badge"), list):
            c.problems.append(f"{where}`badge` is one short text (one badge per card).")
        else:
            c.text(product, "badge", where, "badge")
        rating = c.number(product, "rating", where)
        if rating is not None and rating > 5:
            c.problems.append(f"{where}`rating` ({rating:g}) is out of 5: convert it, or leave it out.")
        reviews = c.number(product, "reviews", where)
        if reviews is not None and reviews != int(reviews):
            c.problems.append(f"{where}`reviews` is a whole number of reviews.")
        if reviews is not None and rating is None:
            c.problems.append(f"{where}`reviews` needs the `rating` it counts; leave both out when the page shows none.")
        c.text(product, "delivery", where, "delivery")
        c.texts(product, "why", where, "why", MAX_WHY)
        c.texts(product, "caution", where, "caution", MAX_CAUTION)
        c.pairs(product, "options", where, ("name", "value"), MAX_OPTIONS)
        c.pairs(product, "specs", where, ("label", "value"), MAX_SPECS)
        offers = product.get("offers")
        if offers not in (None, []):
            if not isinstance(offers, list):
                c.problems.append(f"{where}`offers` is a list of {{store, url, price}}.")
            else:
                if len(offers) > MAX_OFFERS:
                    c.problems.append(f"{where}`offers` has {len(offers)}; the page shows {MAX_OFFERS}.")
                for i, offer in enumerate(offers[:MAX_OFFERS + 1]):
                    at = f"{where}offer {i + 1}: "
                    if not isinstance(offer, dict):
                        c.problems.append(f"{at}is an object {{store, url, price}}.")
                        continue
                    c.text(offer, "store", at, "store", required=True)
                    c.url(offer, "url", at, required=True)
                    offer_price = c.number(offer, "price", at)
                    c.was(offer, at, offer_price)
                    c.stock(offer, at)
                    c.text(offer, "delivery", at, "delivery")
                    if offer.get("currency") not in (None, "") and str(offer.get("currency")).strip() != currency:
                        c.problems.append(f"{at}is in {offer.get('currency')} but the product is in {currency}: one "
                                          "currency per product. Leave out offers in another currency.")
        if title and store and url:
            new_id = product_id(title, store, url)
            if new_id in ids:
                c.problems.append(f"{where}is the same product, store and link as product {ids.index(new_id) + 1}: "
                                  "list each once.")
            ids.append(new_id)
    return c.problems, ids, c.long


def check_order(args) -> tuple[list[str], str, list[str], dict]:
    """(problems, id, long text, the order's total and currency) for an order_card call."""
    c = _Check()
    if not isinstance(args, dict):
        return ["The arguments are an object: {store, items, total}."], "", [], {}
    store = c.text(args, "store", "", "store", required=True)
    if args.get("site") not in (None, ""):
        c.url(args, "site", "")
    items = c.count(args.get("items"), "items", MAX_ITEMS, least=1)
    lines: list[tuple[int, str]] = []
    priced: list[tuple[int, float]] = []
    for index, item in enumerate(items[:MAX_ITEMS + 1]):
        if not isinstance(item, dict):
            c.problems.append(f"Item {index + 1} is an object {{title, qty, price}}.")
            continue
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        where = _where(index, title.strip(), "item")
        title = c.text(item, "title", where, "title", required=True)
        c.text(item, "detail", where, "detail")
        qty = item.get("qty", 1)
        if qty is None:
            qty = 1
        if isinstance(qty, str) and qty.strip().isdigit():
            qty = int(qty.strip())
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty != int(qty) or not 1 <= qty <= MAX_QTY:
            c.problems.append(f"{where}`qty` is a whole number from 1 to {MAX_QTY}.")
            qty = 1
        price = c.number(item, "price", where)
        if item.get("image") not in (None, ""):
            c.url(item, "image", where)
        if title:
            lines.append((int(qty), title))
        if price is not None:
            priced.append((int(qty), price))
    charges = c.count(args.get("charges"), "charges", MAX_CHARGES)
    amounts: list[float] = []
    for index, charge in enumerate(charges[:MAX_CHARGES + 1]):
        where = f"Charge {index + 1}: "
        if not isinstance(charge, dict):
            c.problems.append(f"{where}is an object {{label, amount}} (a discount is a negative amount).")
            continue
        c.text(charge, "label", where, "label", required=True)
        amount = c.number(charge, "amount", where, low=None)
        if amount is None and "amount" not in charge:
            c.problems.append(f"{where}`amount` is missing.")
        if amount is not None:
            amounts.append(amount)
    total = c.number(args, "total", "")
    if total is None and args.get("total") in (None, ""):
        c.problems.append("`total` is missing: exactly what the checkout page says will be charged.")
    currency = c.currency(args, "")
    ship = args.get("ship_to")
    if ship not in (None, "", {}):
        if not isinstance(ship, dict):
            c.problems.append("`ship_to` is {\"name\": …, \"address\": …}.")
        else:
            c.text(ship, "name", "ship_to: ", "ship_name")
            c.text(ship, "address", "ship_to: ", "ship_address")
    pay_with = c.text(args, "pay_with", "", "pay_with")
    if pay_with and _CARD_NUMBER.search(pay_with):
        c.problems.append("`pay_with` looks like a full card number: name it as the page does, \"Visa ending 4242\", "
                          "and never write the number.")
    c.text(args, "arrives", "", "arrives")
    c.text(args, "note", "", "order_note")
    # When the card can add it all up (every item priced, the charges listed), it must come to the total, or the card
    # would show sums that don't agree. An item's price may be the line's amount or the price of one: either adds up.
    if total is not None and amounts and items and len(priced) == len(items):
        charged = sum(amounts)
        sums = (sum(p for _, p in priced) + charged, sum(q * p for q, p in priced) + charged)
        if all(abs(s - total) > 0.011 for s in sums):
            c.problems.append(f"The items and charges come to {sums[0]:.2f}, not the `total` {total:.2f}. Use the total "
                              "the checkout page shows, and list every charge it adds (shipping, tax, fees; a discount "
                              "as a negative amount), or leave `charges` out.")
    new_id = order_id(store, total, currency, lines) if (not c.problems and store and total is not None) else ""
    return c.problems, new_id, c.long, {"store": store, "total": total, "currency": currency}


def _long_words(long: list[str]) -> str:
    if not long:
        return ""
    return (" Some text is longer than the card shows and will be cut: " + ", ".join(long[:6]) +
            ". Keep it shorter next time.")


# MARK: The pay block: no browser action in a chat while its order card waits for Pay or Cancel
#
# The owner's rule: from the moment the bot shows an order card until the person taps Pay or Cancel on it, the bot
# can't act in its browser in that chat (no click, typing, navigating or filling a card); it can still read the page.
# After Pay it may place that order; after Cancel it is free again. A yes typed in words lifts nothing.
#
# Kept in a small JSON file in the add-on's data folder (STATE), so a restart of the gateway or of `hermes serve`
# doesn't lift a block, and every process of this computer (serve, the messaging gateway) sees the same orders.

STATE = "shopping-orders.json"
LAPSE = 24 * 3600.0          # a card nobody answered for a day lapses: the block lifts and it can't be paid any more
CLOSED = ("pay", "cancel", "replaced", "lapsed")

# The browser tools that only read. Every other tool whose name has a `browser_` part (at the start, or after a
# server's prefix: mcp_playwright_browser_click) is an action and is refused while a card waits: a browser tool this
# list doesn't know is refused too (fail closed), since a new acting tool must never slip through unnoticed.
# Hermes's own browser today is browser_exec (Python over CDP), browser_cdp (raw CDP), browser_dialog and
# browser_vault (fill, enter_code, save_login, unlock): each can act, so none is here; the names below are the
# reading tools of Hermes's earlier browser toolset and of browser plugins (scrolling only moves the view).
BROWSER_READERS = frozenset({
    "browser_snapshot", "browser_screenshot", "browser_vision", "browser_get_images", "browser_get_text",
    "browser_get_content", "browser_list_tabs", "browser_tabs", "browser_scroll",
})
# Tools without a `browser_` part that drive a page or pay all the same: the desktop app's in-app browser pane
# (drive_preview; desktop_preview opens pages, its `read` only reads), the whole screen (computer_use, which can click
# in any browser window), and the link-card-fill plugin's card staging (it makes a card the vault can type in).
OTHER_ACTORS = frozenset({"drive_preview", "computer_use", "link_card_stage"})
_BROWSER_PART = re.compile(r"(?:^|_)(browser_[a-z0-9_]+)$")

BLOCKED = (
    "Nothing was done. An order card for {store}, total {total}, is waiting for the user's answer, and nothing can "
    "be done in the browser in this chat until they tap Pay or Cancel on that card. Don't try another way (no "
    "terminal, no other browser, no script). Tell them in one short line that the order card is waiting for their "
    "tap, and end your reply. If they asked for something different, ask them to tap Cancel on the card first.")
LAPSED_NOTE = (
    "[Hermes HQ app note, not from the person: the order card {order} expired (nobody answered it for a day), so this "
    "Pay places nothing. Don't place the order. Go back to the checkout, and if they still want it, show a new card "
    "with order_card.]")
REPLACED_NOTE = (
    "[Hermes HQ app note, not from the person: the order card {order} was replaced by a newer card, so this answer "
    "doesn't count. Don't place the order; point them to the newest order card.]")

_log = logging.getLogger("hermes-hq-push")
_warned = False


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / STATE


def _load(data_dir: Path) -> dict:
    """The orders, by id. A missing file is no orders. A corrupt or unreadable one is no orders too, logged once: one
    bad file must not lock the browser in every chat of every bot (the alternative, fail closed, would), and an order
    lost that way only means a bot isn't stopped, which the order card's own teaching still covers."""
    global _warned
    try:
        text = _path(data_dir).read_text()
    except FileNotFoundError:
        return {}
    except OSError:
        text = None
    try:
        state = json.loads(text) if text is not None else None
        orders = state.get("orders") if isinstance(state, dict) else None
        if isinstance(orders, dict):
            return {k: v for k, v in orders.items() if isinstance(v, dict) and isinstance(k, str)}
    except ValueError:
        pass
    if not _warned:
        _warned = True
        _log.warning("hermes-hq-push: %s can't be read; taken as no waiting orders", STATE)
    return {}


def _save(data_dir: Path, orders: dict) -> None:
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"version": 1, "orders": orders}, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _prune(orders: dict, now: float) -> bool:
    """Waiting orders older than LAPSE lapse; anything older than TTL goes; at most KEEP. True when it changed."""
    changed = False
    for order in orders.values():
        if order.get("state") == "waiting" and now - float(order.get("at") or 0) > LAPSE:
            order["state"], order["settled"] = "lapsed", now
            changed = True
    for key in [k for k, v in orders.items() if now - float(v.get("settled") or v.get("at") or 0) > TTL]:
        del orders[key]
        changed = True
    while len(orders) > KEEP:
        del orders[min(orders, key=lambda k: float(orders[k].get("at") or 0))]
        changed = True
    return changed


def remember_order(data_dir: Path, session_id: str, new_id: str, store: str, total: float, currency: str,
                   now: float | None = None) -> None:
    """A shown order card waits (and blocks). A newer card in the same chat replaces the one still waiting there."""
    now = time.time() if now is None else now
    session = str(session_id or "")
    with _lock:
        orders = _load(data_dir)
        for key, order in orders.items():
            if order.get("session") == session and order.get("state") == "waiting" and key != new_id:
                order["state"], order["settled"] = "replaced", now
        orders[new_id] = {"session": session, "store": store, "total": total, "currency": currency,
                          "state": "waiting", "at": now}
        _prune(orders, now)
        _save(data_dir, orders)


def decision(user_message) -> tuple[str, str] | None:
    """(id, answer) when a message is the app's answer to a product card (buy, watch stock, watch price) or an order
    card (pay, cancel)."""
    if not isinstance(user_message, str):
        return None
    found = ORDER_MARK.match(user_message) or PRODUCT_MARK.match(user_message)
    return (found.group(1), found.group(2)) if found else None


def note_decision(data_dir: Path, user_message, session_id: str = "", now: float | None = None) -> str | None:
    """pre_llm_call: the person tapped a card. Pay or Cancel on the chat's waiting order settles it (and lifts the
    block); an answer to an order that lapsed or was replaced settles nothing and returns a note for the bot. Any
    other message, a typed "yes go ahead" included, changes nothing."""
    found = decision(user_message)
    if not found:
        return None
    now = time.time() if now is None else now
    key, answer = found
    if not key.startswith("o-"):
        with _lock:
            _asks[key] = {"session": str(session_id or ""), "ask": answer, "at": now}
            for old in [k for k, v in _asks.items() if now - v["at"] > TTL]:
                del _asks[old]
        return None
    with _lock:
        orders = _load(data_dir)
        changed = _prune(orders, now)
        order = orders.get(key)
        note = None
        if order is not None and session_id and order.get("session") and order["session"] != str(session_id):
            order = None    # another chat's order: an answer here doesn't settle it
        if order is not None:
            if order.get("state") == "waiting":
                order["state"], order["settled"] = answer, now
                changed = True
            elif answer == "pay" and order.get("state") == "lapsed":
                note = LAPSED_NOTE.format(order=key)
            elif answer == "pay" and order.get("state") == "replaced":
                note = REPLACED_NOTE.format(order=key)
        if changed:
            _save(data_dir, orders)
    return note


def order_state(data_dir: Path, order: str, now: float | None = None) -> str | None:
    """waiting, pay, cancel, replaced or lapsed; None for an order this computer never saw."""
    now = time.time() if now is None else now
    with _lock:
        orders = _load(data_dir)
        _prune(orders, now)
        found = orders.get(order)
        return found.get("state") if found else None


def waiting_order(data_dir: Path, session_id: str, now: float | None = None) -> tuple[str, dict] | None:
    """(id, order) of the chat's order card that waits for Pay or Cancel, or None."""
    now = time.time() if now is None else now
    session = str(session_id or "")
    if not session:
        return None
    with _lock:
        orders = _load(data_dir)
    _prune(orders, now)   # a lapse here only shapes this answer; the next write keeps it
    found = [(k, v) for k, v in orders.items() if v.get("session") == session and v.get("state") == "waiting"]
    return max(found, key=lambda kv: float(kv[1].get("at") or 0)) if found else None


def acts_in_browser(tool_name, args=None) -> bool:
    """Whether a tool acts in a browser (or pays): every `browser_` tool but the readers, the actors without the name,
    and desktop_preview except its `read`."""
    name = str(tool_name or "").strip().lower()
    if name in OTHER_ACTORS:
        return True
    if name == "desktop_preview":
        return not (isinstance(args, dict) and str(args.get("action") or "").strip().lower() == "read")
    found = _BROWSER_PART.search(name)
    return bool(found) and found.group(1) not in BROWSER_READERS


def _money(total, currency: str) -> str:
    try:
        amount = f"{float(total):,.2f}"
    except (TypeError, ValueError):
        return "as shown on the card"
    sign = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency or "USD")
    return f"{sign}{amount}" if sign else f"{amount} {currency}"


def blocked_message(data_dir: Path, tool_name, args, session_id="", now: float | None = None) -> str | None:
    """pre_tool_call: the refusal for a browser action in a chat whose order card waits, else None. Off with the tools
    (shopping.off). Only the chat with the waiting card: another chat of the same bot is free."""
    if not acts_in_browser(tool_name, args):
        return None
    if not switched_on(data_dir):
        return None
    found = waiting_order(data_dir, str(session_id or ""), now)
    if not found:
        return None
    _, order = found
    return BLOCKED.format(store=order.get("store") or "the store", total=_money(order.get("total"), order.get("currency")))


# MARK: The tools

def show(args, session_id: str = "") -> str:
    problems, ids, long = check_products(args)
    if problems:
        return _failed(problems)
    return _reply({"products": "shown", "id": ids[0], "ids": ids, "note": SHOWN_NOTE + _long_words(long)})


def order(args, session_id: str, data_dir: Path) -> str:
    problems, new_id, long, money = check_order(args)
    if problems:
        return _failed(problems)
    remember_order(data_dir, session_id, new_id, money["store"], money["total"], money["currency"])
    return _reply({"order": "waiting", "id": new_id, "note": WAITING_NOTE + _long_words(long)})


def register_tool(ctx, data_dir: Callable[[], Path], serves: Callable[[], bool]) -> None:
    def outside() -> str:
        return _reply({"status": "failed",
                       "error": "Product and order cards show only in a Hermes HQ app chat, and this turn isn't one. "
                                "Put the products in your reply instead, and never pay for an order without the "
                                "user's clear yes."})

    def handle_products(args, session_id="", **kwargs):
        return show(args, session_id) if serves() else outside()

    def handle_order(args, session_id="", **kwargs):
        return order(args, session_id, data_dir()) if serves() else outside()

    def on() -> bool:
        try:
            root = data_dir()
            return switched_on(root) and cards_shown(root)
        except Exception:
            return False

    ctx.register_tool(name=PRODUCTS_TOOL, toolset=TOOLSET, check_fn=on, schema=PRODUCTS_SCHEMA,
                      handler=handle_products, description=PRODUCTS_SCHEMA["description"], emoji="🛍️")
    ctx.register_tool(name=ORDER_TOOL, toolset=TOOLSET, check_fn=on, schema=ORDER_SCHEMA, handler=handle_order,
                      description=ORDER_SCHEMA["description"], emoji="🧾")
