#!/usr/bin/env python3
"""Makes a Hermes HQ Dashboard tile from its data (hermes-hq-push:dashboard-tile).

You give the data (a small JSON file, the "spec"); the kit beside this script draws it: the same parts, the same care
at every tile size, in the look the spec names. You never write the page's HTML or CSS.

    python3 make_tile.py spec.json [-o tile.html]     make the tile (default: spec's name with .html, beside it)
    python3 make_tile.py set tile.html [patch.json]   new data for a tile already made (the patch, or stdin): each key
                                                      given replaces the tile's ("blocks" whole); "updated" becomes
                                                      now and "error" clears. A patch that is wrong changes nothing
    python3 make_tile.py fail tile.html "reason"      the update failed: the last good data stays, the tile says why
    python3 make_tile.py show tile.html               print the tile's data
    python3 make_tile.py rebuild tile.html            draw it again with this kit (after the add-on is updated)

The spec:
    {
      "name": "Revenue This Week",      the tile's name in the app, plain words, Title Case, at most 40 characters
      "title": "Revenue",               the heading inside the tile: a word or two
      "look": "app",                    app (the person's own theme), midnight, terminal or vivid
      "badge": "2/2 up", "badge_state": "good",          a short fact beside the heading (optional)
      "value": "$12,480", "state": null,                 the headline: a number or a word; state good, warn, bad
      "delta": "+8.2%", "trend": "up",                   how it moved (optional)
      "caption": "This week, to Sunday",                 one short line under the headline (optional)
      "blocks": [ ... ],                                 up to four parts, the most important first (see below)
      "source": "Stripe",                                where it came from (optional)
      "job": "<job id>",                                 the scheduled job that keeps it fresh (optional)
      "updated": "2026-11-03T15:30:00-06:00", "error": null      left out, "updated" is now
    }
Blocks:
    {"type": "chart", "series": [1, 2, 3], "kind": "area", "label": "24 h", "high": "5.2k"}    kind: area, line, bars;
                                                      "high" is the series' top value as it should read
    {"type": "status", "items": [{"name": "Storage", "value": "6/6 pools", "state": "good", "detail": "..."}]}
    {"type": "rows",   "items": [{"name": "Stripe", "value": "$9,120"}]}
    {"type": "meters", "items": [{"name": "Disk", "value": 72, "max": 100, "text": "72%", "state": "warn"}]}
    {"type": "stats",  "items": [{"name": "7 days", "value": "914.6k"}]}

Prints PROBLEM lines and exits 1 when the data is wrong; WARNING lines are worth fixing. Then run check_tile.py.
"""
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LOOKS = ("app", "midnight", "terminal", "vivid")
STATES = ("good", "warn", "bad")
BLOCKS = {"chart": ("series",), "status": ("items",), "rows": ("items",), "meters": ("items",), "stats": ("items",)}
KEYS = {"name", "title", "look", "accent", "badge", "badge_state", "value", "state", "delta", "trend", "caption", "empty",
        "blocks", "source", "job", "updated", "error"}
# About how many characters of an item's name and value show whole in a Small tile, by block.
ROOM = {"status": (12, 12), "rows": (20, 10), "meters": (16, 8), "stats": (10, 8)}
# Keys that say how the tile is drawn or kept, not what it shows: setting only these is not an update of its data.
SETTINGS = {"name", "title", "look", "accent", "job", "source"}
DATA = re.compile(r'(<script type="application/json" id="data">)([\s\S]*?)(</script>)')


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def checked(spec):
    """What is wrong with the data (problems, warnings): a tile is made only with no problems."""
    problems, warnings = [], []
    if not isinstance(spec, dict):
        return ["the spec is not a JSON object"], []

    def long(what, text, most):
        if isinstance(text, str) and len(text) > most:
            warnings.append(f'{what} "{text}" is {len(text)} characters: keep it to about {most} (it is cut off in a small tile)')

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
    long('"title"', spec.get("title"), 20)
    long('"badge"', spec.get("badge"), 12)
    long('"value"', spec.get("value"), 12)
    long('"caption"', spec.get("caption"), 40)
    long('"error"', spec.get("error"), 30)
    if isinstance(spec.get("badge"), str) and re.search(r"\b(live|now|online now|real[- ]?time)\b", spec["badge"], re.I):
        warnings.append(f'the badge "{spec["badge"]}" won\'t stay true: a tile is a picture, redrawn now and then. Use a fact (a count)')
    blocks = spec.get("blocks", [])
    if not isinstance(blocks, list):
        problems.append('"blocks" is a list')
        blocks = []
    if len(blocks) > 4:
        warnings.append(f"{len(blocks)} blocks: a tile is a glance, and only the first two or three are ever seen; keep to four at most")
    for n, block in enumerate(blocks, 1):
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in BLOCKS:
            problems.append(f'block {n} has type "{kind}": one of {", ".join(BLOCKS)}')
            continue
        if kind == "chart":
            series = block.get("series")
            if not isinstance(series, list) or len([v for v in series if isinstance(v, (int, float)) and not isinstance(v, bool)]) < 2:
                problems.append(f'block {n} (chart) needs "series": two numbers or more')
            elif any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in series):
                problems.append(f'block {n} (chart): every value in "series" is a number (put 0 where there was none)')
            if block.get("kind") not in (None, "area", "line", "bars"):
                problems.append(f'block {n} (chart): "kind" is area, line or bars')
            continue
        items = block.get("items")
        if not isinstance(items, list) or not items:
            problems.append(f'block {n} ({kind}) needs "items": a list with something in it (leave the block out when there is nothing)')
            continue
        for i, item in enumerate(items, 1):
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                problems.append(f'block {n} ({kind}), item {i}: needs a "name"')
                continue
            if item.get("state") not in (None, "") + STATES:
                problems.append(f'block {n} ({kind}), item "{item["name"]}": "state" is good, warn, bad or null')
            if kind == "meters":
                if not isinstance(item.get("value"), (int, float)) or isinstance(item.get("value"), bool):
                    problems.append(f'block {n} (meters), item "{item["name"]}": "value" is a number ("text" is what is shown beside it)')
            elif item.get("value") in (None, ""):
                problems.append(f'block {n} ({kind}), item "{item["name"]}": needs a "value"')
            most_name, most_value = ROOM[kind]
            long(f'block {n} ({kind}): the name', item.get("name"), most_name)
            long(f'block {n} ({kind}): the value', item.get("text") if kind == "meters" else item.get("value"), most_value)
        if kind == "status":
            said = {str(item.get("value", "")).strip().lower() for item in items if isinstance(item, dict)}
            if len(items) >= 4 and len(said) == 1:
                warnings.append(f'block {n} (status): every box says "{items[0].get("value")}". The light already says that: give each a '
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


def page(spec):
    """The tile: one self-contained page (the kit's styles and scripts inline, the data in its block)."""
    def kit(name):
        with open(os.path.join(HERE, "kit", name), encoding="utf-8") as f:
            return f.read().strip("\n")

    def text(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    data = json.dumps(spec, indent=1, ensure_ascii=False).replace("</", "<\\/")
    job = f'\n<meta name="hq-tile-job" content="{text(spec["job"])}">' if spec.get("job") else ""
    return f"""<!doctype html>
<!-- A Hermes HQ Dashboard tile, made by make_tile.py (hermes-hq-push:dashboard-tile). Its data is the JSON block
     below: change it with `make_tile.py set`, never by editing the styles or scripts. -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="hq-tile-kit" content="2">
<title>{text(spec.get("name", ""))}</title>{job}
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


def write(path, spec):
    """Checks the data and writes the tile whole (a temporary file, then renamed: never half-written)."""
    problems, warnings = checked(spec)
    for p in problems:
        print("PROBLEM:", p)
    for w in warnings:
        print("WARNING:", w)
    if problems:
        return 1
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".hq-tile-", suffix=".html")
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        f.write(page(spec))
    os.chmod(temp, 0o644)
    os.replace(temp, path)
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
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
            done = write(path, spec)
            if done == 0:
                check = os.path.join(HERE, "check_tile.py")
                print(f"MADE: {path}\nNow check it:  python3 {check} {path}")
            return done
        tile = os.path.abspath(os.path.expanduser(args[0]))
        spec = read_spec(tile)
        if command == "show":
            print(json.dumps(spec, indent=1, ensure_ascii=False))
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
