---
name: html-page
description: Make an HTML page to show the person in the Hermes HQ app (a map, a report, a chart, a comparison, a list, a small tool), built from a starter that looks like the app and checked at phone size before it is handed over. Use whenever you are about to write an .html file for the person to open, share with MEDIA:, or show with ::preview. Not for Dashboard tiles (dashboard-tile).
---

# Pages for Hermes HQ

The person opens your page **on a phone**, most of the time: a sheet about 390 points wide, in their dark or light
theme. They tap; nothing hovers. The app puts its theme into the page before your styles (`--foreground`,
`--muted-foreground`, `--accent`, `--border`, `--card`, `--hq-radius`), gives the body 16-point sides, and shows
the page as it is. The page looks as good as you make it at that size, and a page made for a desktop window
falls apart there: rows wrap at random, a checkbox ends up on another line than the one beside it, edges stop
lining up.

## Steps

1. **Start from the starter page.** Copy `${HERMES_SKILL_DIR}/template.html` to where the page will live (a folder of
   its own under your artifacts, never a temporary folder: the person opens it later) and build in it. Keep its
   `<style>` block as it is and use its classes; add your own rules after it. It holds every common part: a title
   with a line under it, a row of chips for filters, a menu with a button, a note, a map or picture, numbers, a
   card with a list, a table. Delete what you don't need.
2. **Lay it out as a column** (see *Layout*). One idea per row, in reading order.
3. **Check it:** `python3 ${HERMES_SKILL_DIR}/check_page.py <page.html>`. It draws the page as the app does (a phone in
   dark, a phone in light, a small phone, an iPad) and says what is wrong and where. A `PROBLEM` must be fixed; fix
   each `WARNING` unless you mean it. Then **look at each picture** it lists (vision_analyze) before you hand the
   page over. You are the one who can tell whether it reads well. Fix and check again. `NOT DRAWN` means
   there was no browser to draw it with: say the page wasn't checked by eye.
4. **Hand it over** with the page alone on its own line (`MEDIA:/absolute/path/page.html`), and say in a line what
   it shows and what to tap.

## Layout

- **A column at 390 wide.** Title → controls → the thing itself → details. Never a desktop toolbar (title, filters,
  menu and buttons in one row that wraps): it breaks wherever the words happen to end.
- **One row per kind of control.** Checkboxes and toggles together in a `.hq-chips` row (each chip holds its box
  and its words, and the row wraps as a whole). A menu or search field and its button in a `.hq-bar`. The title on
  its own line, with a `.hq-sub` line for the count or the date.
- **One left edge.** The body's 16-point sides are the edge for everything. Give sections no side padding of their
  own. A map, a picture or a card spans the same width as the words, rounded (`.hq-media`, `.hq-card`), or runs
  edge to edge on purpose (`.hq-bleed`).
- **Even spacing.** The starter puts 12 points between sections; don't add margins at random.
- **Room for a finger.** Buttons, menus and fields at least 44 points tall; chips 40. Words at 13 points or more
  (16 in fields, or iOS zooms in when they are tapped).
- **Nothing sideways.** Nothing wider than the screen: a wide table scrolls inside its `.hq-table`, never the page;
  long words and links wrap.
- **Tap, not click.** Write "tap". Nothing only on hover: show it, or show it on a tap.
- **Heights from the screen** for a map or a chart (`clamp(320px, 62vh, 620px)`, as `.hq-map`), not a fixed 800.
- **Wider screens:** the starter keeps the column under 760 wide and centred, so an iPad or Mac shows the same page,
  calmer. A grid that adds columns there (`repeat(auto-fit, minmax(160px, 1fr))`) is fine.

## Colour

- Words are `var(--ink)` or `var(--ink2)` (the starter's names for the app's `--foreground` and
  `--muted-foreground`). **Never set a fixed colour on words** (`color:#173745` reads in light and vanishes in
  dark). Links and the main button use `var(--tint)`.
- A brand or category colour goes on a mark, not on words: a `.hq-dot` in a chip, a pin, a bar, a line in a chart.
- Backgrounds: leave the page's own transparent (the app's ground shows through), and use `var(--panel)` for cards
  and fields. If the page must have its own background (a poster, a printout), set it on `body` and set every word's
  colour against it.
- A chart drawn for a white page (dark labels and axes) sits on a white card, or uses `currentColor` for its labels.

## Maps, charts and libraries

- A library from a CDN (Leaflet, Chart.js) is fine: the page can load from the network. Put the map in
  `<div class="hq-media hq-map">` so it is rounded and sized to the screen, and say "tap a pin".
- Give a chart a `viewBox` and `width:100%`, so it fits the screen and scales.
- Keep popups and tooltips short: they show on a tap, over a small screen.

## Don'ts

- Don't write the page at a desktop width and hope it wraps well.
- Don't put a title and controls in one wrapping row; don't scatter checkboxes among other controls.
- Don't pad sections differently, or the left edges stop lining up.
- Don't use hover, "click", or tiny text and targets.
- Don't hand over a page you haven't checked and looked at.
