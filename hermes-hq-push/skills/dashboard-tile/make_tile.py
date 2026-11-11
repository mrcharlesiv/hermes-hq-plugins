#!/usr/bin/env python3
"""Makes a Hermes HQ Dashboard tile from its data (hermes-hq-push:dashboard-tile).

You give the data (a small JSON file, the "spec"); the kit beside this script draws it: designed parts, laid out
for every tile size, in the look the spec names. You never write the page.

    python3 make_tile.py spec.json [-o tile.html]     make the tile (default: the spec's name with .html, beside it)
    python3 make_tile.py set tile.html [patch.json]   new data for a tile already made (the patch, or stdin): each key
                                                      given replaces the tile's ("blocks" whole); "updated" becomes
                                                      now and "error" clears. A patch that is wrong changes nothing
    python3 make_tile.py fail tile.html "reason"      the update failed: the last good data stays, the tile says why
    python3 make_tile.py show tile.html               print the tile's data (pictures left out)
    python3 make_tile.py rebuild tile.html            draw it again with this kit (after the add-on is updated)
    python3 make_tile.py blocks [type]                the blocks a tile can have; one block's fields and an example

The spec:
    {
      "name": "Revenue This Week",      the tile's name in the app, plain words, Title Case, at most 40 characters
      "title": "Revenue",               the heading inside the tile: a word or two
      "look": "app",                    app (the person's own theme), midnight, terminal or vivid
      "accent": "#rrggbb",              the tint, when the person asks for a colour
      "badge": "2/2 up", "badge_state": "good",          a short fact beside the heading
      "value": "$12,480", "state": null,                 the headline: a number or a word; state good, warn, bad
      "delta": "+8.2%", "trend": "up",                   how it moved
      "caption": "This week, to Sunday",                 one short line under the headline
      "blocks": [ ... ],                                 up to four parts, the most important first
      "backdrop": {"file": "photo.jpg"},                 a picture behind the whole tile
      "source": "Stripe",                                where it came from
      "job": "<job id>",                                 the scheduled job that keeps it fresh
      "refresh": 300,                                    read the file again this often (seconds) while it shows
      "foot": false                                      leave out the "Updated ..." line (a photo, a quote)
    }
Everything but "name" is optional. "updated" and "error" are set for you.
Prints PROBLEM lines and exits 1 when the data is wrong; WARNING lines are worth fixing. Then run check_tile.py.
"""
import base64
import json
import mimetypes
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LOOKS = ("app", "midnight", "terminal", "vivid")
STATES = ("good", "warn", "bad")
KEYS = {"name", "title", "look", "accent", "badge", "badge_state", "value", "state", "delta", "trend", "caption", "empty",
        "blocks", "backdrop", "source", "job", "refresh", "foot", "updated", "error"}
# Keys that say how the tile is drawn or kept, not what it shows: setting only these is not an update of its data.
SETTINGS = {"name", "title", "look", "accent", "job", "source", "refresh", "foot"}
DATA = re.compile(r'(<script type="application/json" id="data">)([\s\S]*?)(</script>)')
PICTURE_BYTES = 700_000
ITEM = "name, value, state (good, warn, bad)"

# The blocks: what each draws, what it is for, its fields, and an example. `make_tile.py blocks` prints this, and the
# data is checked against it. "items" is about how many characters of a name and of a value show whole in a Small tile.
BLOCKS = {
    "chart": {
        "draws": "a line over a soft fill, several lines, or bars",
        "for": "a number over time; this week against last",
        "fields": ['series: numbers, oldest first; or up to four lines as [{"name": "This week", "values": [...]}, ...]',
                   "kind: area (default), line or bars", 'label: what and how long ("Hourly, 24 h")',
                   'high: the top value as it should read ("5.2k")', 'labels: what it runs along (["Mon", ..., "Sun"]), shown under it'],
        "example": {"type": "chart", "series": [3, 5, 4, 7, 6, 9], "label": "Daily, 6 days", "high": "9"},
    },
    "status": {
        "draws": "a box per thing, each with a light for its state",
        "for": "is each service, server, check or goal all right",
        "fields": [f"items: {ITEM}, detail (one more line, shown in big tiles)"],
        "items": (12, 12),
        "example": {"type": "status", "items": [{"name": "Storage", "value": "6/6 pools", "state": "good"},
                                                {"name": "Monitoring", "value": "52/56", "state": "bad"}]},
    },
    "rows": {
        "draws": "a ruled list: a name and its value",
        "for": "a breakdown, an agenda, a to-do list, a leaderboard, prices that moved",
        "fields": [f"items: {ITEM}, detail", 'lead: a few characters before the name (a time "9:30", a rank "1", a mark "✓")',
                   "series: numbers, drawn as a small line beside the value"],
        "items": (20, 10),
        "example": {"type": "rows", "items": [{"lead": "9:30", "name": "Standup", "value": "15 min"},
                                              {"lead": "11:00", "name": "Dana, pricing", "value": "1 h", "state": "warn"}]},
    },
    "meters": {
        "draws": "how full, as a bar under each name",
        "for": "disk, quota, budget spent, progress to a goal",
        "fields": ["items: name, value (a number), max (100 unless given), text (what is shown beside it), state"],
        "items": (16, 8),
        "example": {"type": "meters", "items": [{"name": "Disk", "value": 72, "text": "720 GB", "state": "warn"}, {"name": "Memory", "value": 41}]},
    },
    "rings": {
        "draws": "up to four rings, the number in the middle of each",
        "for": "how far along a goal is; a score; a battery",
        "fields": ["items: name, value (a number), max (100 unless given), text (what is shown in the ring), state"],
        "items": (10, 5),
        "example": {"type": "rings", "items": [{"name": "Move", "value": 420, "max": 600, "text": "70%"},
                                               {"name": "Stand", "value": 9, "max": 12, "text": "9/12", "state": "good"}]},
    },
    "stats": {
        "draws": "two to four small numbers side by side, each under its label",
        "for": "the few figures that go with the headline",
        "fields": ["items: name, value, state"],
        "items": (10, 8),
        "example": {"type": "stats", "items": [{"name": "7 days", "value": "914.6k"}, {"name": "Per hour", "value": "2.7k"}]},
    },
    "strip": {
        "draws": "a strip of small columns in order: a label, a glyph, a value",
        "for": "a forecast by the hour, the days of a week, a few steps in a row",
        "fields": ['items: name (the label: "2 PM", "Tue"), glyph (one emoji or a character or two: "☀️"), value ("72°"), state'],
        "items": (5, 5),
        "example": {"type": "strip", "items": [{"name": "Now", "glyph": "☀️", "value": "72°"}, {"name": "2 PM", "glyph": "⛅", "value": "74°"},
                                               {"name": "3 PM", "glyph": "🌧", "value": "69°"}]},
    },
    "heat": {
        "draws": "a grid of small cells, oldest first",
        "for": "days up or down (one row); a habit or activity over a year (seven rows: a column a week)",
        "fields": ["values: a number each (a stronger colour the higher), or a state each (good, warn, bad), null for none",
                   "rows: 1 (default) to 7", 'label, note: words over it ("Last 30 days", "99.2%")'],
        "example": {"type": "heat", "values": ["good", "good", "bad", "good", "good", "warn", "good"], "label": "Last 7 days", "note": "6 of 7"},
    },
    "split": {
        "draws": "one bar cut into its shares, each named under it",
        "for": "where the money, the time or the traffic went",
        "fields": ["items: name, value (a number: its share is worked out), text (what is shown: default its percent); six at most"],
        "items": (12, 8),
        "example": {"type": "split", "items": [{"name": "Search", "value": 5200}, {"name": "Direct", "value": 2100}, {"name": "Social", "value": 900}]},
    },
    "table": {
        "draws": "a small table: the first column names the row",
        "for": "a few things compared on two or three figures",
        "fields": ['columns: the headings (["Rep", "Calls", "Won"]), four at most; the last go first in a narrow tile',
                   'rows: a list per row ([["Dana", "42", "6"], ...])'],
        "example": {"type": "table", "columns": ["Rep", "Calls", "Won"], "rows": [["Dana", "42", "6"], ["Lee", "37", "9"]]},
    },
    "text": {
        "draws": "words, as many lines as the tile has room for",
        "for": "a note, a summary in a sentence or two, a quote",
        "fields": ["text: the words (a new line is kept)", "kind: note (default) or quote", "by: who said it"],
        "example": {"type": "text", "kind": "quote", "text": "Make it work, make it right, make it fast.", "by": "Kent Beck"},
    },
    "picture": {
        "draws": "a picture, covering its box",
        "for": "a photo, a logo, a map or a chart you have as an image",
        "fields": [f"file: the path of a .png, .jpg, .gif, .webp or .svg (it is put inside the tile; {PICTURE_BYTES // 1000} KB at most)",
                   "svg: or the drawing itself, as SVG markup with a viewBox", "fit: cover (default) or contain (shown whole)", "alt: what it shows"],
        "example": {"type": "picture", "file": "~/Pictures/logo.png", "fit": "contain", "alt": "The company logo"},
    },
    "buttons": {
        "draws": "buttons that ask you (the tile's bot) for something",
        "for": "an action from the tile: refresh now, mark done, snooze",
        "fields": ["items: label, send (what you are told when it is pressed)",
                   "they work in the app's Grid and in the opened tile; answer by giving the tile new data with `set`"],
        "items": (14, 400),
        "example": {"type": "buttons", "items": [{"label": "Refresh now", "send": "Update my Revenue tile now"}]},
    },
    "custom": {
        "draws": "your own drawing, in the tile's layout and colours",
        "for": "what no block here draws: a month's calendar, a seating plan, a diagram",
        "fields": ["html: the markup (inline SVG is welcome)", "css: its styles (start your class names with x-)",
                   "js: a script run once, given `block` (your element) and `data` (the tile's data)",
                   "stretch: false when it has a height of its own (default: it fills the room it is given)",
                   'a drawing that needs room goes altogether in a tile too small for it: give it "drop": 1 and put a block after it '
                   "(rows, stats) that a small tile can show instead",
                   "use the tile's colours: var(--ink), var(--ink2), var(--tint), var(--good), var(--warn), var(--bad), var(--surface), "
                   "var(--line), var(--radius), var(--c1) to var(--c6); sizes in em, nothing in px over 150 or under 11",
                   "classes you can use: box (a soft card), label (small capitals), strong, muted, t-good, t-warn, t-bad, t-tint, pip",
                   'data-drop="1" on a part of yours that may go when the tile is small (1 goes first)'],
        "example": {"type": "custom", "html": '<div class="x-week"><div class="box"><span class="label">Mon</span> <span class="strong">3</span></div></div>',
                    "css": ".x-week { display: grid; grid-template-columns: repeat(7, 1fr); gap: .3em; }"},
    },
}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def checked(spec):
    """What is wrong with the data (problems, warnings): a tile is made only with no problems."""
    problems, warnings = [], []
    if not isinstance(spec, dict):
        return ["the spec is not a JSON object"], []

    def long(what, text, most):
        if isinstance(text, str) and len(text) > most:
            warnings.append(f'{what} "{text[:60]}" is {len(text)} characters: keep it to about {most} (it is cut off in a small tile)')

    for key in sorted(set(spec) - KEYS):
        warnings.append(f'"{key}" is not something a tile has, so it is not drawn (see: python3 make_tile.py --help)')
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append('"name" is missing: the tile\'s name in the app, in plain words, such as "Revenue This Week"')
    elif re.search(r"\.html?\b|\S[/\\]\S|^[\w-]+_[\w_-]+$|^[a-z0-9]+(-[a-z0-9]+)+$", name, re.I):
        problems.append(f'"name" "{name}" reads as a file name: use plain words ("Revenue This Week")')
    elif len(name) > 40:
        problems.append(f'"name" is {len(name)} characters: keep the tile\'s name to 40 or fewer')
    if spec.get("look") not in (None,) + LOOKS:
        problems.append(f'"look" is "{spec.get("look")}": one of {", ".join(LOOKS)}')
    if spec.get("accent") is not None and not re.fullmatch(r"#[0-9a-fA-F]{3,8}", str(spec["accent"])):
        problems.append('"accent" is a colour as "#rrggbb"')
    for key in ("state", "badge_state"):
        if spec.get(key) not in (None, "") + STATES:
            problems.append(f'"{key}" is "{spec[key]}": good, warn, bad or null')
    if spec.get("trend") not in (None, "", "up", "down"):
        problems.append('"trend" is up, down or null')
    for key in ("title", "badge", "value", "delta", "caption", "source", "error"):
        if spec.get(key) is not None and not isinstance(spec[key], (str, int, float)):
            problems.append(f'"{key}" is words or a number, not {type(spec[key]).__name__}')
    if spec.get("refresh") is not None and (not number(spec["refresh"]) or spec["refresh"] < 30):
        problems.append('"refresh" is a number of seconds, 30 or more')
    long('"title"', spec.get("title"), 20)
    long('"badge"', spec.get("badge"), 12)
    long('"value"', spec.get("value"), 12)
    long('"caption"', spec.get("caption"), 40)
    long('"error"', spec.get("error"), 30)
    if isinstance(spec.get("badge"), str) and re.search(r"\b(live|now|online now|real[- ]?time)\b", spec["badge"], re.I):
        warnings.append(f'the badge "{spec["badge"]}" won\'t stay true: a tile is a picture, redrawn now and then. Use a fact (a count)')
    backdrop = spec.get("backdrop")
    if backdrop is not None and not (isinstance(backdrop, dict) and (backdrop.get("file") or backdrop.get("src"))):
        problems.append('"backdrop" is {"file": "<the path of a picture>"}')
    blocks = spec.get("blocks") or []
    if not isinstance(blocks, list):
        problems.append('"blocks" is a list')
        blocks = []
    if len(blocks) > 4:
        warnings.append(f"{len(blocks)} blocks: a tile is a glance, and only the first two or three are ever seen; keep to four at most")
    if not blocks and spec.get("value") in (None, "") and not backdrop:
        warnings.append('no "value" and no blocks: the tile will say "No data yet"')
    for n, block in enumerate(blocks, 1):
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in BLOCKS:
            problems.append(f'block {n} has type "{kind}": one of {", ".join(BLOCKS)} (see: python3 make_tile.py blocks)')
            continue
        where = f"block {n} ({kind})"
        if kind == "chart":
            series = block.get("series")
            lines = series if isinstance(series, list) and series and isinstance(series[0], dict) else [{"values": series}]
            if len(lines) > 4:
                problems.append(f"{where}: four lines at most")
            for line in lines:
                values = line.get("values") if isinstance(line, dict) else None
                if not isinstance(values, list) or len(values) < 2:
                    problems.append(f'{where} needs "series": two numbers or more (or lines as [{{"name": ..., "values": [...]}}])')
                elif any(not number(v) for v in values):
                    problems.append(f'{where}: every value in "series" is a number (put 0 where there was none)')
            if block.get("kind") not in (None, "area", "line", "bars"):
                problems.append(f'{where}: "kind" is area, line or bars')
            if block.get("kind") == "bars" and len(lines) > 1:
                problems.append(f"{where}: bars draw one series; use lines for several")
        elif kind == "heat":
            values = block.get("values")
            if not isinstance(values, list) or not values:
                problems.append(f'{where} needs "values": a number or a state for each cell, oldest first')
            elif any(not (v is None or number(v) or v in STATES) for v in values):
                problems.append(f'{where}: each of "values" is a number, good, warn, bad or null')
            if block.get("rows") is not None and (not number(block["rows"]) or not 1 <= block["rows"] <= 7):
                problems.append(f'{where}: "rows" is 1 to 7')
        elif kind == "table":
            columns, rows = block.get("columns"), block.get("rows")
            if not isinstance(columns, list) or not 2 <= len(columns) <= 4:
                problems.append(f'{where} needs "columns": two to four headings')
            elif not isinstance(rows, list) or not rows or any(not isinstance(r, list) or len(r) != len(columns) for r in rows):
                problems.append(f'{where} needs "rows": a list per row, each with {len(columns)} values (one per column)')
            else:
                for heading in columns:
                    long(f"{where}: the heading", str(heading), 8)
        elif kind == "text":
            if not isinstance(block.get("text"), str) or not block["text"].strip():
                problems.append(f'{where} needs "text": the words')
            elif len(block["text"]) > 600:
                warnings.append(f"{where}: {len(block['text'])} characters of text: a tile shows a few lines; say it in a sentence or two")
            if block.get("kind") not in (None, "note", "quote"):
                problems.append(f'{where}: "kind" is note or quote')
        elif kind == "picture":
            if not (block.get("file") or block.get("src") or block.get("svg")):
                problems.append(f'{where} needs "file" (the path of a picture) or "svg" (the drawing itself)')
            if block.get("svg") and "viewBox" not in str(block["svg"]):
                problems.append(f'{where}: the "svg" needs a viewBox, so it stretches to its box')
            if block.get("fit") not in (None, "cover", "contain"):
                problems.append(f'{where}: "fit" is cover or contain')
        elif kind == "custom":
            html = block.get("html")
            if not isinstance(html, str) or not html.strip():
                problems.append(f'{where} needs "html": your markup')
            else:
                everything = html + str(block.get("css") or "")
                if re.search(r"\b(?:src|href)\s*=\s*[\"'](?:https?:)?//|url\(\s*[\"']?(?:https?:)?//|@import", everything, re.I):
                    problems.append(f"{where} loads from the network: a tile is drawn offline, so put it in the tile (inline SVG, a data: image)")
                if re.search(r"<script\b", html, re.I):
                    problems.append(f'{where}: no <script> in "html": give the script as "js"')
                for size in re.findall(r"font-size\s*:\s*(\d+(?:\.\d+)?)px", everything):
                    if float(size) < 11:
                        problems.append(f"{where}: font-size {size}px is under 11 px, unreadable on a phone; size text in em")
                for side, size in re.findall(r"(?<![-\w])(width|height)\s*:\s*(\d{3,})px", everything):
                    if int(size) > 150:
                        problems.append(f"{where}: {side}: {size}px is a fixed size bigger than a Small tile: use %, fr or em")
                if re.search(r"position\s*:\s*(?:absolute|fixed)", everything) and not re.search(r"position\s*:\s*relative", everything):
                    warnings.append(f"{where}: something is placed with position: absolute and nothing of yours is position: relative, so it is "
                                    "placed against the whole tile and may sit over other parts")
                if not re.search(r"var\(--", everything):
                    warnings.append(f"{where} uses none of the tile's colours (var(--ink), var(--tint), var(--surface) ...): it won't match the "
                                    "look, or the person's light and dark themes")
        else:
            items = block.get("items")
            if not isinstance(items, list) or not items:
                problems.append(f'{where} needs "items": a list with something in it (leave the block out when there is nothing)')
                continue
            if kind == "rings" and len(items) > 4:
                warnings.append(f"{where}: {len(items)} rings: four at most are drawn")
            if kind == "split" and len(items) > 6:
                warnings.append(f"{where}: {len(items)} shares: six at most are drawn; add the small ones together as Other")
            label = "label" if kind == "buttons" else "name"
            for i, item in enumerate(items, 1):
                if not isinstance(item, dict) or not str(item.get(label, "")).strip():
                    problems.append(f'{where}, item {i}: needs a "{label}"')
                    continue
                called = item[label]
                if item.get("state") not in (None, "") + STATES:
                    problems.append(f'{where}, item "{called}": "state" is good, warn, bad or null')
                if kind in ("meters", "rings", "split"):
                    if not number(item.get("value")):
                        problems.append(f'{where}, item "{called}": "value" is a number ("text" is what is shown)')
                elif kind == "buttons":
                    if not str(item.get("send", "")).strip():
                        problems.append(f'{where}, button "{called}": needs "send": what you are told when it is pressed')
                elif kind not in ("strip", "rows") and item.get("value") in (None, ""):
                    problems.append(f'{where}, item "{called}": needs a "value"')
                if item.get("series") is not None and (not isinstance(item["series"], list) or any(not number(v) for v in item["series"])):
                    problems.append(f'{where}, item "{called}": "series" is a list of numbers')
                most_name, most_value = BLOCKS[kind].get("items", (16, 12))
                long(f"{where}: the {label}", called, most_name)
                long(f"{where}: the value", item.get("text") if kind in ("meters", "rings", "split") else item.get("value"), most_value)
                long(f"{where}: the lead", item.get("lead"), 6)
                long(f"{where}: the glyph", item.get("glyph"), 3)
            if kind == "status":
                said = {str(item.get("value", "")).strip().lower() for item in items if isinstance(item, dict)}
                if len(items) >= 4 and len(said) == 1:
                    warnings.append(f'{where}: every box says "{items[0].get("value")}". The light already says that: give each a '
                                    "value that tells something (20 cores, 39°C), or group them (Hosts 4/4)")
    stamp = spec.get("updated")
    if stamp is not None:
        if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:?\d\d)", stamp):
            problems.append(f'"updated" is "{stamp}": a time with its zone, as 2026-11-03T15:30:00-06:00 (or leave it out: it becomes now)')
        else:
            try:
                if datetime.fromisoformat(stamp.replace("Z", "+00:00")) > datetime.now(timezone.utc) + timedelta(minutes=5):
                    problems.append(f'"updated" is {stamp}, which hasn\'t happened yet: leave it out and it becomes now')
            except ValueError:
                problems.append(f'"updated" is "{stamp}": not a real time')
    return problems, warnings


def embedded(spec, folder):
    """Puts each picture's file inside the spec ("file" becomes "src", a data: URL). Says which can't be."""
    problems = []

    def inside(holder, what):
        path = holder.get("file")
        if not path:
            return
        full = os.path.expanduser(str(path))
        full = full if os.path.isabs(full) else os.path.join(folder, full)
        kind = mimetypes.guess_type(full)[0] or ""
        try:
            with open(full, "rb") as f:
                raw = f.read()
        except OSError as exc:
            problems.append(f"{what}: can't read {path}: {exc.strerror or exc}")
            return
        if not kind.startswith("image/"):
            problems.append(f"{what}: {path} is not a picture (.png, .jpg, .gif, .webp, .svg)")
        elif len(raw) > PICTURE_BYTES:
            problems.append(f"{what}: {path} is {len(raw) // 1000} KB: make it smaller than {PICTURE_BYTES // 1000} KB first "
                            "(a tile is small: 800 px across is plenty)")
        else:
            holder["src"] = f"data:{kind};base64," + base64.b64encode(raw).decode("ascii")
            del holder["file"]

    if isinstance(spec.get("backdrop"), dict):
        inside(spec["backdrop"], '"backdrop"')
    for n, block in enumerate(spec.get("blocks") or [], 1):
        if isinstance(block, dict) and block.get("type") == "picture":
            inside(block, f"block {n} (picture)")
    return problems


def page(spec):
    """The tile: one self-contained page (the kit's styles and scripts inline, the data in its block)."""
    def kit(name):
        with open(os.path.join(HERE, "kit", name), encoding="utf-8") as f:
            return f.read().strip("\n")

    def text(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    data = json.dumps(spec, indent=1, ensure_ascii=False).replace("</", "<\\/")
    metas = ""
    if spec.get("job"):
        metas += f'\n<meta name="hq-tile-job" content="{text(spec["job"])}">'
    if spec.get("refresh"):
        metas += f'\n<meta name="hq-tile-refresh" content="{int(spec["refresh"])}">'
    if any(isinstance(b, dict) and b.get("type") == "buttons" for b in spec.get("blocks") or []):
        metas += '\n<meta name="hq-tile" content="live">'   # its buttons work in the app's Grid
    return f"""<!doctype html>
<!-- A Hermes HQ Dashboard tile, made by make_tile.py (hermes-hq-push:dashboard-tile). Its data is the JSON block
     below: change it with `make_tile.py set`, never by editing the styles or scripts. -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="hq-tile-kit" content="3">
<title>{text(spec.get("name", ""))}</title>{metas}
<style>
{kit("tile.css")}
</style>
</head>
<body>
<script type="application/json" id="data">
{data}
</script>
<script>
{kit("tile.js")}
</script>
<script id="hq-tile-fit">
{kit("fit.js")}
</script>
</body>
</html>
"""


def read_spec(tile):
    with open(tile, encoding="utf-8") as f:
        html = f.read()
    m = DATA.search(html)
    if not m or 'name="hq-tile-kit"' not in html:
        raise SystemExit(f"PROBLEM: {tile} wasn't made by make_tile.py (no kit data block): make it again from a spec")
    return json.loads(m.group(2).replace("<\\/", "</"))


def write(path, spec, folder=None):
    """Checks the data and writes the tile whole (a temporary file, then renamed: never half-written)."""
    problems, warnings = checked(spec)
    if not problems:
        problems += embedded(spec, folder or os.path.dirname(os.path.abspath(path)))
    for p in problems:
        print("PROBLEM:", p)
    for w in warnings:
        print("WARNING:", w)
    if problems:
        return 1
    html = page(spec)
    if len(html.encode("utf-8")) > 1_500_000:
        print(f"PROBLEM: the tile comes to {len(html) // 1000} KB: keep it under 1,500 KB (smaller pictures)")
        return 1
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".hq-tile-", suffix=".html")
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        f.write(html)
    os.chmod(temp, 0o644)
    os.replace(temp, path)
    return 0


def about(kind=None):
    """What the blocks are, or one block's fields and an example."""
    if kind is None:
        print('The blocks a tile can have (the first one in "blocks" gets the room; the rest sit beside or under it):\n')
        for name, block in BLOCKS.items():
            print(f"  {name:<8} {block['draws']}\n           for: {block['for']}")
        print("\nOne block's fields and an example:  python3 make_tile.py blocks <type>")
        return 0
    if kind not in BLOCKS:
        print(f'PROBLEM: no block "{kind}": one of {", ".join(BLOCKS)}')
        return 1
    block = BLOCKS[kind]
    print(f"{kind}: {block['draws']}\nFor: {block['for']}\nFields:")
    for field in block["fields"]:
        print("  " + field)
    if "items" in block:
        print(f"  (in a Small tile about {block['items'][0]} characters of a name and {block['items'][1]} of a value show whole)")
    print("  drop, keep: optional; when it goes in a small tile (1 first) and the fewest items worth showing")
    print("Example:\n  " + json.dumps(block["example"], ensure_ascii=False))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] == "blocks":
        return about(argv[1] if len(argv) > 1 else None)
    command = argv[0] if argv[0] in ("set", "fail", "show", "rebuild") else "new"
    args = argv[1:] if command != "new" else argv
    try:
        if command == "new":
            out = None
            if "-o" in args:
                at = args.index("-o")
                out = args[at + 1]
                args = args[:at] + args[at + 2:]
            source = os.path.abspath(os.path.expanduser(args[0]))
            with open(source, encoding="utf-8") as f:
                spec = json.load(f)
            if isinstance(spec, dict):
                spec["updated"] = spec.get("updated") or now()
                spec.setdefault("error", None)
            path = os.path.abspath(os.path.expanduser(out)) if out else os.path.splitext(source)[0] + ".html"
            done = write(path, spec, os.path.dirname(source))
            if done == 0:
                check = os.path.join(HERE, "check_tile.py")
                print(f"MADE: {path}\nNow check it:  python3 {check} {path}")
            return done
        tile = os.path.abspath(os.path.expanduser(args[0]))
        spec = read_spec(tile)
        if command == "show":
            def short(value):   # a picture inside the tile is its bytes: said, not printed
                if isinstance(value, dict):
                    return {k: (f"<a picture, {len(v) // 1000} KB, inside the tile>" if k == "src" and isinstance(v, str) and v.startswith("data:")
                                else short(v)) for k, v in value.items()}
                return [short(v) for v in value] if isinstance(value, list) else value
            print(json.dumps(short(spec), indent=1, ensure_ascii=False))
            return 0
        if command == "fail":
            spec["error"] = " ".join(args[1:]).strip() or "The update failed"
        elif command == "set":
            raw = open(os.path.expanduser(args[1]), encoding="utf-8").read() if len(args) > 1 else sys.stdin.read()
            patch = json.loads(raw)
            if not isinstance(patch, dict):
                print("PROBLEM: the patch is a JSON object of the keys to replace")
                return 1
            spec.update(patch)
            if set(patch) - SETTINGS:   # new data: stamped now, and the last failure is over
                spec["updated"] = patch.get("updated") or now()
                spec["error"] = patch.get("error")
        done = write(tile, spec)
        if done == 0:
            print(f"UPDATED: {tile}" if command != "rebuild" else f"REBUILT: {tile}")
        return done
    except (OSError, IndexError) as exc:
        print(f"PROBLEM: {exc}" if isinstance(exc, OSError) else "PROBLEM: something is missing from the command (python3 make_tile.py --help)")
        return 2
    except json.JSONDecodeError as exc:
        print(f"PROBLEM: that is not JSON: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
