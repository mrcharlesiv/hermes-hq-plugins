---
name: dashboard-tile
description: Make a live tile for the person's Dashboard in the Hermes HQ app (a small HTML page that fills its tile at any size, kept fresh by a scheduled job). Use when asked for a dashboard, tile, widget, status board, or something to pin in Hermes HQ.
---

# Dashboard tiles for Hermes HQ

The Hermes HQ app has a **Dashboard**: pages, pictures and files the person pinned, laid out in a grid of tiles like
Home Screen widgets. A tile is an `.html` file you make. The app draws the page **in the tile's own window** and shows
that picture; a tap opens the page full screen, scripts running.

The person decides what the tile shows and how it looks. This skill is about making it **fit and read at a glance**.

## A tile is a glance, not a report

A tile is read in a second, like a Home Screen widget: **one headline** (a number or a word such as `ONLINE`), a few
words, then the few things that matter most. Everything else is a tap away, in the opened page.

- **Show less in small windows; never shrink it to squeeze it in.** A grid of nine boxes of 9 px text is not a tile.
  The template shows as many items as fit and says `+6 more`.
- **Nothing cut off at the tile's edge, nothing over anything else, no half of the tile left blank.** Those are the
  three ways a tile goes wrong; the check looks for each.
- **What is wrong comes first.** A status board's failed parts lead, so they are what stays in a Small tile.
- **Short words.** A heading of one or two words (the app already shows the tile's name under it); an item's name
  and value about 12 characters each (`Storage` · `6/6 pools`); a note of one short line (about 30 characters).
- **An item's value says something.** `20 cores`, `39°C`, `6/6 pools`, not nine boxes that each say `Up`: the colour
  already says up. With many things alike, group them (`Hosts` · `4/4`, `GPUs` · `2/2`) and name the one that is
  down; list each thing only when there are a few. More about one item goes in its `detail`.

About how much fits (the template works this out by itself; plan the content with it in mind):

| Window | What fits |
|---|---|
| Small tile | the heading, the headline, then a small chart **or** 2–3 items |
| Medium tile, a card | the same beside a chart, or 4–6 items |
| Large tile | a chart and 3–4 rows, or 6–8 items |
| Extra Large, opened | everything |

## Where a tile is seen (points; px in the page = pt on screen)

| Where | Phone | iPad / Mac |
|---|---|---|
| Small tile | ~160–175 square | ~190–200 square |
| Medium tile | ~335–362 × 160–175 (about 2:1) | same shape, larger |
| Large tile | ~335–362 × 380 | same shape, larger |
| Extra Large tile | ~335–362 × 590 (tall) | the whole width × 3 rows (wide, ~760 × 570) |
| Card (the Cards view of Tiles, the shelf on Artifacts) | drawn 390 wide × 200–235 | drawn 390 wide × 140–290 |
| Opened (a tap) | the whole screen, ~400 × 780 | a large sheet |

A Grid tile is drawn at its real size. A card is drawn 390 wide in the card's shape and shown at the card's width, so
it is wide and short. The person picks the view and the size and can change them any time, so one page must work in
**all** of these.

## The template

`template.html` is one page with six parts, each optional but the heading and the headline:

| Part | In the data block | |
|---|---|---|
| heading | `title`, `badge`, `badge_state` | a word or two; the badge is a short fact that stays true in a picture (`2/2 up`, never `LIVE` or `now`), coloured by `badge_state`; it shows whole or not at all |
| headline | `value`, `state`, `delta`, `trend` | the number, or the word; `state` colours it: `good`, `warn`, `bad` |
| note | `note` | one short line under the headline |
| chart | `series` | a line that stretches into the room left |
| items | `items`, `items_as` | `rows` (a name and its number) or `cards` (a grid of small boxes); each item a `name`, a `value`, a `state`, and a `detail` line that shows only in a big tile and the opened page |
| foot | `updated`, `error`, `source` | when it was updated, or why the last update failed; the `source` (`Grafana`) shows in a big tile and the opened page |

Leave out what the tile doesn't have and the layout closes up around the rest: with no chart the items take its
room; with nothing but the headline, the number grows to fill the tile. Three tiles it makes as it is:

- **A number over time** (revenue, calls, tokens): `value`, `delta`, `series`, and a few `rows` for the breakdown.
- **A status board** (servers, services, go / no-go): `value` is the one word (`ONLINE`, `NO-GO`) with its `state`,
  no `series`, `"items_as": "cards"` with a `state` on each item. Give `items` the highest `data-drop` (below).
- **A list** (open deals, what needs a reply): `value` is the count, `items` as `rows`, the most pressing first.

A tile that is both a number and a status (tokens today, and whether the boxes are up): the number is the headline;
its `state` and the badge carry the status, so the number turns red and the badge says `1/2 up` when something is
down, in the smallest tile too. Have the job that updates the tile set both.

### How it fits itself (keep this)

The last script in the template (`<script id="hq-tile-fit">`) makes the page fit whatever window it is drawn in.
When there is too much for the window it leaves parts out, **in the order you give**, until the rest fits. Mark the parts:

- `data-drop="1"`, `"2"`, … on a part that may go: `1` goes first. A part with no `data-drop` always stays.
- `data-items` on a list or a grid: at its turn it loses items from its end, down to `data-keep`; with room for
  fewer than that it goes altogether. `data-more="+{n} more"` is what stands in for the items left out. (A part that
  went comes back when that costs a list no more than a third of its items: the foot is worth a row of boxes.)
- `data-least="48"` on a part that stretches (the chart): under that many px high it counts as not fitting.
- `data-whole` on a part that shows whole or not at all; `data-shrink` on the one line that shrinks to its width.

Set the order to suit the tile. For a number over time the breakdown goes first and the chart last (the template's
order). For a status board the note and the foot go first and the items last:

```html
<div class="note" id="note" data-drop="1"></div>
<div class="chart" id="chart" data-drop="3" data-least="48"></div>
<div class="items" id="items" data-items data-drop="4" data-keep="1" data-more="+{n} more"></div>
<div class="foot" id="foot" data-drop="2"></div>
```

Keep the script whole and last in the page, and keep the layout rules marked `KEEP`; change the look and the parts
freely. If you add a part of your own, give it a `data-drop` and a place in the layout's rows. For something that
should show only where there is room (a line inside each item), hide it and show it with a container query, as the
template does for `.detail`. After a script of yours changes what shows (tabs, a button), call `hqTileFit()`.

## Rules that make a tile work

1. **One self-contained `.html` file.** Styles, scripts, data and pictures inline (SVG, or small `data:` images). The
   page is drawn with **no network**: no CDN scripts, web fonts, `fetch`, XHR or iframes.
2. **Fill the window exactly, never scroll.** `html, body { width:100%; height:100%; margin:0; overflow:hidden }`,
   and the layout sized from the window (`100vh`/`100vw`, `%`, `fr`, or container units `cqw`/`cqh`/`cqmin` with
   `body { container-type: size }`). No fixed widths or heights over ~150 px, no absolute positions for content.
3. **Scale text with the tile.** One base size such as `font-size: clamp(13px, 4.5cqmin + 5px, 20px)` on the tile, and
   everything else in `em`. **Nothing under 11 px** (`.85em` of the base): smaller is unreadable on a phone.
4. **Numbers always fit.** Abbreviate big numbers (`$1.2M`, `48.3k`), keep the headline on one line with
   `data-shrink`. A number cut off with "…" is a broken tile.
5. **Charts stretch.** SVG with a `viewBox`, `width:100%; height:100%`, `preserveAspectRatio="none"` for lines and
   areas plus `vector-effect: non-scaling-stroke`, or plain HTML bars in `%`. No fixed-size `<canvas>`.
6. **Shape-aware layout.** The template lays out by the window's shape with container queries: wide
   (`min-aspect-ratio: 8/5`) puts the words beside the chart or the items; big and wide (iPad/Mac Extra Large) puts
   the chart and the items side by side. A grid of items uses
   `repeat(auto-fit, minmax(min(100%, 8.5em), 1fr))`, never a fixed number of columns, and stretches to fill.
7. **The person's own colours.** The app gives every page the person's theme as CSS variables: `--card` (background),
   `--foreground`, `--muted-foreground`, `--accent`, `--border`. Use them, with your own colours as the fallback
   (`var(--card, #f6f6f3)`), so the tile matches the app. Only pick fixed colours when the person asks for them.
   Good / warn / bad colours are yours.
8. **Light and dark.** Fallback colours as CSS variables with `@media (prefers-color-scheme: dark)`; the app draws the
   tile in the look it's in. `<meta name="color-scheme" content="light dark">`. Text at least 4.5:1 against its
   background (3:1 for big numbers); faint grey on grey is unreadable on a phone.
9. **Clear of the corners.** Tiles have rounded corners: keep words and numbers inside the padding
   (`clamp(10px, 7cqmin, 28px)`); only a chart's fill or a background may run to the edge.
10. **No motion.** The tile is a picture taken about 0.7 s after loading: no entrance animations, transitions or
    counters that count up (they get pictured half-way). Scripts must finish drawing within half a second.
11. **A tile is a picture.** Links and buttons only work once the tile is opened; on Tiles nothing is tappable but the
    whole tile. Never put the only copy of something important behind a tap. (Buttons that message the bot,
    `window.hermes.send`, work only when the page is opened from its chat.)
12. `<meta name="viewport" content="width=device-width, initial-scale=1">`, and a `<title>`: **the tile's name in the
    app**, shown under the tile, in plain words and Title Case, such as `Revenue This Week` or `Calls Today`. Never a
    file name, no `.html`, no dashes or underscores, at most 40 characters (shorter is better: ~20 fit under a Small
    tile). The heading inside the tile can say the same or less.

## Time, empty and failed

- **When it was updated.** Put the time in the data block as `"updated"` with a time zone
  (`2026-11-03T15:30:00Z` or `…-05:00`), read from the clock (`date -Iseconds`), never typed by hand, and show it as
  an absolute time in the person's zone ("Updated Tue 3:30 PM"), never "5 minutes ago" or "live" (the picture keeps
  saying it long after).
- **No data yet.** Show a calm empty state ("No data yet", "No calls today") in place of the number, never a blank tile,
  `NaN`, `undefined` or `$0` that isn't true.
- **A refresh that fails.** Keep the last good data and its `updated` time, and set `"error"` to a few words
  ("Stripe timed out"); the template then says "Couldn’t update · last Tue 3:30 PM · Stripe timed out" and never
  drops that line. Never replace a working tile with an error page or an empty one.

## Steps

1. **Start from the template:** copy `${HERMES_SKILL_DIR}/template.html` to where the tile will live, e.g.
   `~/dashboards/<short-name>.html` (keep the same path for the life of the tile: the pin points at it). Keep the
   parts marked `KEEP`; change the look, the data block and the parts freely to match what the person asked for.
2. **Fill in real data** in the `<script type="application/json" id="data">` block, with `updated` (time zone
   included) and `error: null`. Set the `data-drop` order for this tile.
3. **Check it:** `python3 ${HERMES_SKILL_DIR}/check_tile.py <file>`. It draws the page in every window above and
   says what is wrong and at which sizes. Fix every `PROBLEM`, and every `WARNING` you can, by **showing less at
   that size** or shortening the words, and run it again. (Text ending in "…" is a warning, not a problem: fine for
   one long name, not for a headline, a status or a note.) When it says `PICTURES:`, look at that `tiles.png` (every
   size side by side, light and dark; it is in a `<name>-tiles` folder beside the tile, yours to delete afterwards)
   before handing it over: a crowded small tile, a line wrapped in an ugly place or a half-empty large one is yours
   to fix even when the check passes.
   - `NOT DRAWN` means there was no browser to draw it with, so nothing was looked at: install one
     (`npx playwright install chromium`, or `apt install chromium`) or point `HQ_TILE_CHROME` at one, and run it
     again. If you can't, keep the template's layout and fit script exactly, and tell the person the tile wasn't
     checked by eye.
4. **Hand it over** with the page alone on its own line, so the app shows it as a page card the person can pin:

   ```
   MEDIA:/absolute/path/to/tile.html
   ```

   Tell them: hold the card and choose **Pin as Tile**, then pick its size. (If they asked from **Tiles › New Tile**,
   the app pins it for them.)

## Keeping it live

A tile is a picture of the file, redrawn when the file changes. To keep it fresh, **rewrite the same file on a
schedule** with a scheduled job (cron), never by fetching from the page:

- When the data comes from something a script can read (an API, a CSV, a database), write a small script beside the
  tile (e.g. `~/dashboards/<name>.refresh.py`) that fetches the data, replaces the JSON block and the `updated`
  time (or, when the fetch fails, keeps the old data and only sets `error`), and writes the file (write to a temporary file, then rename over the tile, so it is never half-written).
  Schedule a job that runs it. Run it once yourself first and run the check again. The script changes **only the
  data block**: the page's look, its marks and its fit script stay as they are.
- When the data needs you (reading mail, judging something), schedule a job whose prompt is: "Update the Dashboard
  tile `<path>` with the dashboard-tile skill: <what to show>. Keep the path and the look. Reply [SILENT]."
  On such an update change the data only: run the check, fix a `PROBLEM` the new data caused (a longer name, more
  items), and leave the look alone.
- Data grows: today's 4 items are 12 next month. Send them all, the most important first; the tile shows what fits.
- Choose a sensible interval (every 15 minutes for live numbers, hourly or daily for most things).
- **Write the job's id into the page** (and keep it there each time the page is rewritten), so the app's tile menu
  can change how often it runs, update it now, or pause it:
  `<meta name="hq-tile-job" content="<job id>">` in the `<head>`. Make one job per tile.
- Tell the person how often it updates, and that they can change it from the tile: hold it (or open it and tap the
  clock) and choose **How Often**.
- If you can't schedule a job here, say so plainly: the tile shows what was true when it was made and won't change
  until a job exists.

The app redraws pinned tiles when you rewrite the file during a chat with it, when the person pulls to refresh,
and when it next opens the Dashboard after the file changed.

## Changing a tile made before

A tile made from an older template has no fit script. Leave it as it is for a small change (a colour, a word). When
the person says it is cut off, cramped or half empty, or the check finds a `PROBLEM` of that kind, move it onto the
current template: keep its path, `<title>`, `<meta name="hq-tile-job">`, colours and data, carry its parts over into
the template's parts, and change its refresh script to write the new data block.

## Don'ts

- Don't load anything from the internet in the page, and don't make the page fetch its own data.
- Don't make one tall scrolling report: that's a page, not a tile (it is pictured from the top only).
- Don't fix "it doesn't fit" with smaller text, a fixed grid or `overflow: hidden` on a box: show less at that size.
- Don't pick the tile's look against what the person asked for; the template's look is only a neutral start.
- Don't move or rename a pinned tile's file; make a new tile instead.
