---
name: dashboard-tile
description: Make a live tile for the person's Dashboard in the Hermes HQ app. Anything they want at a glance (a number, a chart, a status board, a forecast, an agenda, a habit, a table, a photo, a quote, buttons), filling its tile at any size and kept fresh by a scheduled job. Use when asked for a dashboard, tile, widget, status board, or something to pin in Hermes HQ.
---

# Dashboard tiles for Hermes HQ

The Hermes HQ app has a **Dashboard**: a grid of tiles like Home Screen widgets. A tile is one `.html` file. The app
draws it in the tile's own window (from a 160 pt square to a whole screen) and shows that picture; a tap opens it.

**You give the data; the kit draws the tile.** You write a small JSON file (the *spec*) saying what the tile shows,
and `make_tile.py` makes the page from it: designed parts (*blocks*), laid out for every tile size, in the look you
name. You do not write the page. That is what makes tiles look good and alike, and what keeps them from being cut
off: the kit shows what fits each size and says `+6 more` for the rest.

There is a block for most things a person asks for, and a `custom` block for the rest: your own drawing, inside the
kit's layout and colours. So whatever they dream up, the answer is a spec.

## Steps

1. **Decide what the tile shows** (see *What to put on a tile* and *Which blocks*). `python3
   ${HERMES_SKILL_DIR}/make_tile.py blocks` lists the blocks; `blocks <type>` gives one block's fields and an
   example. `${HERMES_SKILL_DIR}/examples/` has a whole spec for each kind of tile.
2. **Write the spec** as `~/dashboards/<short-name>.json` (see *The spec*).
3. **Make the tile:** `python3 ${HERMES_SKILL_DIR}/make_tile.py ~/dashboards/<short-name>.json`. It writes
   `<short-name>.html` beside the spec (keep that path for the life of the tile: the pin points at it). Fix every
   `PROBLEM` it prints, and every `WARNING` you can. While you are still getting it right (steps 3 and 4), change
   the `.json` and make it again as often as you like. Once you hand it over, **the tile holds its own data**: the
   `.json` was only the way in (delete it, or keep it as a note), and every later change goes through
   `make_tile.py set`, never by making the tile again from the old `.json`, which would put old numbers back.
4. **Check it:** `python3 ${HERMES_SKILL_DIR}/check_tile.py ~/dashboards/<short-name>.html`. It draws the tile in
   every window the app uses and says what is wrong and where. `LEFT OUT` lists what each size leaves out so that
   it fits: that is by design, but make sure nothing the person asked for is missing from the sizes they will use.
   When it says `PICTURES:`, **look at each picture** (the small sizes, the large ones, the biggest) before handing
   the tile over: you are the one who can tell whether it reads at a glance. Something off is fixed in the tile's
   **data** (fewer or shorter words, a different block, another order) with `set`, then checked again. A `PROBLEM`
   must be fixed; a `WARNING` is yours to fix where you can, and doesn't stop the hand-over. `NOT DRAWN` means there was no browser to draw it with: install one
   (`npx playwright install chromium`, or `apt install chromium`) or point `HQ_TILE_CHROME` at one; if you can't,
   say the tile wasn't checked by eye.
5. **Keep it live** with a scheduled job (see *Keeping it live*), and give the tile the job's id:
   `python3 ${HERMES_SKILL_DIR}/make_tile.py set <tile.html> '{"job": "<job id>"}'`.
6. **Hand it over** with the page alone on its own line, so the app shows it as a card the person can pin:

   ```
   MEDIA:/absolute/path/to/tile.html
   ```

   Tell them: hold the card and choose **Pin as Tile**, then pick its size. (If they asked from **Tiles › New Tile**,
   the app pins it for them.) Say how often it updates.

## Where a tile is seen

The person picks the view and the size and changes them at will, so one tile must work in all of these (the check
draws each):

| | Phone | iPad / Mac |
|---|---|---|
| Grid: Small | ~160–175 pt square | ~190 square |
| Grid: Medium | ~360 × 175 (wide) | wider |
| Grid: Large | ~360 × 380 | larger |
| Grid: Extra Large | ~360 × 590 (tall) | ~760 × 570 (wide) |
| Cards (the other view of Tiles) | ~390 × 200–235 (wide) | down to 390 × 140 |
| Opened (a tap) | the whole screen | a large sheet |

## The spec

```json
{
  "name": "Revenue This Week",
  "title": "Revenue",
  "look": "app",
  "value": "$12,480", "delta": "+8.2%", "trend": "up",
  "caption": "This week, to Sunday",
  "blocks": [
    {"type": "chart", "series": [1.4, 1.9, 1.6, 1.8, 2.1, 1.7, 2.0], "label": "Daily, last 7 days", "high": "$2.1k"},
    {"type": "rows", "items": [{"name": "Stripe", "value": "$9,120"}, {"name": "App Store", "value": "$2,410"}]}
  ],
  "source": "Stripe"
}
```

Only `name` is required.

| Key | What it is |
|---|---|
| `name` | The tile's name in the app, shown under it: plain words, Title Case, at most 40 characters. Never a file name. |
| `title` | The heading inside the tile: a word or two. |
| `value`, `state` | **The headline**: one number (`$12,480`, `72°`, `12 days`) or one word (`ONLINE`, `NO-GO`). `state` colours it: `good`, `warn`, `bad`. A tile whose blocks say it all (a quote, a table, rings) can go without. |
| `delta`, `trend` | How it moved (`+8.2%`, `up` or `down`). |
| `caption` | One short line under the headline (about 40 characters). |
| `badge`, `badge_state` | A short fact beside the heading that stays true in a picture: `9/9 up`, `3 late`. Never `LIVE` or `now`. It gives way to the heading in a narrow tile, so never the only place a problem shows: that is the headline's `state`. |
| `blocks` | Up to four parts, **the most important first** (below). |
| `look` | `app`, the person's own theme: **use this unless they ask for a look** (the examples show the others only so you can see them). `midnight` (deep blue: "dark", "like Grafana"), `terminal` (green on black, monospace), `vivid` (a bold colour that follows the state; best for one number or a quote). `"accent": "#rrggbb"` changes the tint ("make it green", a brand colour). |
| `backdrop` | `{"file": "<path>"}`: a picture behind the whole tile (a place, an album cover), under white words. |
| `source` | Where the data came from (`Grafana`): shown in big tiles. |
| `foot` | `false` leaves out the "Updated ..." line, for a tile with nothing to update (a quote, a photo). |
| `job` | The id of the scheduled job that keeps the tile fresh, so the app's tile menu can change how often it runs. |
| `refresh` | Seconds: the app reads the file again this often while the tile is on screen (30 or more; for a tile a job rewrites every few minutes). |
| `updated`, `error` | Set for you, so leave them out: `updated` is the time the tile was made or last given data; `error` says why an update failed. |

## Which blocks

| The person wants | Block | In short |
|---|---|---|
| a number over time; this against that | `chart` | a line over a soft fill, up to four lines, or bars |
| is each thing all right; a few things each with its own little chart | `status` | a box per thing with a light: `name`, `value`, `state`, a `series` |
| a breakdown, an agenda, to-dos, a leaderboard, prices | `rows` | a ruled list: `lead` (a time, a rank), `name`, `value`, a small `series` line |
| how full: disk, quota, budget | `meters` | a bar under each name |
| where it sits between good and bad: air quality, a risk | `scale` | a bar from green to red, a mark on it |
| how far along a goal; a score | `rings` | up to four rings, the number in the middle |
| the few figures beside the headline | `stats` | two to four small numbers under their labels |
| a forecast, the days of a week, steps in a row | `strip` | small columns in order: a label, a glyph, a value |
| up or down by day; a habit over a year | `heat` | a grid of small cells |
| where it went: money, time, traffic | `split` | one bar cut into shares, each named |
| a few things compared on a few figures | `table` | up to four columns |
| a note, a summary, a quote; a title with a line under it (a song, a book) | `text` | words, as many lines as fit |
| a photo, a logo, a cover, a drawing | `picture` | a file (put inside the tile) or SVG; `"shape": "square"` for a cover |
| to act from the tile | `buttons` | each sends you a message when pressed |
| anything else | `custom` | your own markup and styles, in the tile's colours |

`python3 ${HERMES_SKILL_DIR}/make_tile.py blocks <type>` gives each block's fields and an example: read it for the
blocks you use.

**How blocks are laid out.** The first block is the main one and gets the room. The others sit beside or under it,
and are the first to go when the tile is small: the last one first. So order them by what matters, and give a list
all its items: the tile shows as many as fit, in full rows. In a wide tile (Medium, a card) the heading runs across
the top, the headline and caption sit on the left and the main block on the right; with no headline or caption the
main block has the whole width. In a small tile the caption, the foot and the badge give way too, so what must
always show goes in the headline. Any block can take:

- `"label": "Up next"`: a heading over it;
- `"drop": 1`: it is the first to go;
- `"keep": 3`: the fewest items worth showing (with room for fewer, the block goes instead);
- `"order": "given"`: its items stay in your order (otherwise what is `bad` or `warn` comes first).

A tile bigger than what it has to show stays partly empty, and that is fine: an Extra Large tile of five rows is
five rows. Don't pad it with things nobody asked for; a block that stretches (a chart, a picture, rings) is what
fills a big tile, when there is one worth showing.

A list that runs along a line (`strip`, `stats`, `rings`) keeps to full rows and leaves out the rest **from its
end**, so put first what matters most: the newest result, not the oldest.

**When no block draws it** (a month's calendar, a seating plan, a map of a cluster, a funnel): make that part a
`custom` block and keep the rest of the tile (heading, headline, other blocks, foot) the kit's. Read
`make_tile.py blocks custom` first: it says what your markup and styles can use. In short: the tile's colours
(`var(--ink)`, `var(--tint)`, `var(--surface)` ...) and sizes in `em` (never under `1em`), so it matches every look
and both light and dark; `height: 100%` to fill the room it is given; `@container` or `@media` queries to change
with the tile's size, since one drawing must work from 160 pt to a whole screen. The check judges it like
everything else, and you must look at the pictures: a drawing is where tiles go wrong. Make the drawing itself
change with the size (fewer labels, a simpler shape in a small tile) so that it shows everywhere. Only when it
can't be small at all, give it `"drop": 1` and follow it with a block a small tile can show instead; it then
appears only in the big tiles.

**Things that tick** (a clock, a timer): a tile is a picture except where it runs live, which is in the app's Grid
when the tile has `"refresh"` or buttons, and when it is opened. So show what stays true (when a timer ends, not
the seconds left), and if you draw a clock in a `custom` block's `js`, say that it is only right where it is live.

## What to put on a tile

A tile is read in a second. Whatever the subject, choose for that:

- **One thing leads.** The number they asked about, the word that answers "is it all right?", the next event, the
  quote. It is the headline (or the main block, when there is no headline).
- **What is wrong leads.** Set the headline's `state`, and each item's: `bad` is broken or needs the person now,
  `warn` is degraded or heading that way, `good` is fine. Items that are `bad` or `warn` are shown first, and
  `+4 more` turns red when it stands for one.
- **A value says something:** `20 cores`, `39°C`, `6/6 pools`, `2 days`, never nine boxes that each say `Up` (the
  light already says up). Up to about eight things that each have something to say: one item each. More than that,
  or things that are all alike: group them (`Hosts` · `4/4`) and name the one that is down.
- **Short words.** `make_tile.py` warns when words are too long for a small tile, and `blocks <type>` says how many
  characters show. Abbreviate big numbers (`$1.2M`, `48.3k`). More about one item goes in its `detail` (give every
  item of a block one, or none).
- **Two or three blocks at most** for most tiles. A small tile shows the headline and the first block; the rest
  appear as the tile gets bigger.
- **Their words win.** If they describe how it should look or what goes where, do that, with the blocks that come
  closest and a `custom` block where none does. A colour they name is the `accent`; "one big number in green" or
  "make it pop" is `vivid`; "pretty" for words is a `text` block with `"face": "serif"` and `"align": "center"`,
  over a `backdrop` if they gave a picture.

## Keeping it live

A tile is a picture of its file, redrawn when the file changes. Keep it fresh by giving the same file new data on a
schedule (a cron job), never by fetching from the page:

- **Data a script can read** (an API, a database, Grafana): write `~/dashboards/<short-name>.refresh.py` that
  fetches the data and pipes the keys that changed to the tile:

  ```python
  import json, subprocess
  MAKE = "<the absolute path of make_tile.py>"
  TILE = "<the absolute path of the tile's .html>"
  try:
      data = fetch()                      # yours
      patch = {"value": data["total"], "state": "good", "blocks": [...]}
      subprocess.run(["python3", MAKE, "set", TILE], input=json.dumps(patch), text=True, check=True)
  except Exception:
      subprocess.run(["python3", MAKE, "fail", TILE, "Grafana didn't answer"])   # the last good data stays
  ```

  `set` replaces each key you give (`blocks` whole: send every block, not only the one that changed), stamps
  `updated` from the clock and clears `error`. It checks the data first: a patch that is wrong prints `PROBLEM`,
  exits 1 and leaves the tile as it was. `fail` takes a few plain words (under 30 characters), not the exception's
  text. Run the script once yourself, then run the check again. Schedule a job that runs it.
- **Data that needs you** (reading mail, judging something): schedule a job whose prompt is: "Update the Dashboard
  tile `<path>` with the dashboard-tile skill: <what to show>. Give it new data with `make_tile.py set`; keep its
  look. Reply [SILENT]."
- Choose a sensible interval (every 15 minutes for live numbers, hourly or daily for most things). One job per
  tile, its id given to the tile with `set` (step 5).
- A tile with nothing to update (a quote you were asked for once, a photo) needs no job: set `"foot": false`.
- If you can't schedule a job here, say so plainly: the tile shows what was true when it was made.
- **Buttons** send you their `send` words as a message. Do what was asked, then give the tile its new data with
  `set`; that is the tile's answer.

The app redraws a pinned tile when its file changes during a chat with it, when the person pulls to refresh, and
when it next opens the Dashboard.

## Changing a tile

`python3 ${HERMES_SKILL_DIR}/make_tile.py show <tile>` prints its data. Change what was asked (a look, a block, the
words) and give it back with `set`; if a refresh script writes those keys, change the script too. `null` takes a
key away. A picture is printed as `<a picture ... inside the tile>`: send that back as it is and the picture
stays. Keep the path, the `name` and the `job`. `rebuild <tile>` draws it again with the newest kit after the
add-on is updated.

A tile that `show` refuses was written by hand before the kit. Leave it for a small change. When the person says it
is cut off, cramped or plain, or asks for a new look, make it again as a spec at the same path (its data, its
`<title>` as `name`, its `hq-tile-job` as `job`) and change its refresh script to use `set`.

## A page of your own

Only when the whole tile is something the kit's layout can't hold (a game, a full-bleed animation): start from
`${HERMES_SKILL_DIR}/custom.html`, a hand-written page with the same fit script, and follow the rules in its
comments. Check it the same way. A `custom` block inside a kit tile is almost always the better answer.

## Don'ts

- Don't edit a kit tile's HTML or CSS: change its data. Don't copy the kit's page to restyle it.
- Don't put a report on a tile: fewer things, and the ones that matter.
- Don't fetch from the page or load anything from the internet in it.
- Don't write "live", "now" or "5 minutes ago": the picture keeps saying it long after.
- Don't move or rename a pinned tile's file; make a new tile instead.
