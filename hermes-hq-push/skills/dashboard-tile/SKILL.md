---
name: dashboard-tile
description: Make a live tile for the person's Dashboard in the Hermes HQ app (a small HTML page that fills its tile at any size, kept fresh by a scheduled job). Use when asked for a dashboard, tile, widget, or something to pin in Hermes HQ.
---

# Dashboard tiles for Hermes HQ

The Hermes HQ app has a **Dashboard**: pages, pictures and files the person pinned, laid out in a grid of tiles like
Home Screen widgets. A tile is an `.html` file you make. The app draws the page **at the tile's own size** and shows
that picture; a tap opens the page full screen, scripts running.

The person decides what the tile shows and how it looks. This skill is only about making it **fit and work**.

## Tile sizes (points; px in the page = pt on screen)

| Size | Phone | iPad / Mac |
|---|---|---|
| Small | ~160–195 square | ~185–200 square |
| Medium | ~335–400 × 160–195 (about 2:1) | same shape, larger |
| Large | ~335–400 square | same shape, larger |
| Extra Large | ~335–400 × 520–600 (tall) | the whole width × 3 rows (wide) |

The person picks the size and can change it any time, so one page must work in **all** of them. Design for Small
first (one number, a few words, maybe a tiny chart), then let bigger tiles show more.

## Rules that make a tile work

1. **One self-contained `.html` file.** Styles, scripts, data and pictures inline (SVG, or small `data:` images). The
   page is drawn with **no network**: no CDN scripts, web fonts, `fetch`, XHR or iframes. Links (`<a href>`) are fine.
2. **Fill the window exactly, never scroll.** `html, body { width:100%; height:100%; margin:0; overflow:hidden }`,
   and the layout sized from the window (`100vh`/`100vw`, `%`, `fr`, or container units `cqw`/`cqh`/`cqmin` with
   `body { container-type: size }`). No fixed widths or heights over ~150 px.
3. **Scale text with the tile.** One base size such as `font-size: clamp(13px, 4.5cqmin + 5px, 20px)` on the tile, and
   everything else in `em`. Nothing under 9 px.
4. **Charts stretch.** SVG with a `viewBox`, `width:100%; height:100%`, `preserveAspectRatio="none"` for lines and
   areas plus `vector-effect: non-scaling-stroke`, or plain HTML bars in `%`. No fixed-size `<canvas>`.
5. **Shape-aware layout.** Use container queries on the window's aspect: wide (`min-aspect-ratio: 8/5`) puts words
   beside the chart; tall (`max-aspect-ratio: 4/5`) has room for a list. Hide extras rather than shrinking them.
6. **Light and dark.** Colours as CSS variables with `@media (prefers-color-scheme: dark)`; the app draws the tile
   in the look it's in. `<meta name="color-scheme" content="light dark">`.
7. `<meta name="viewport" content="width=device-width, initial-scale=1">`, and a `<title>`: **the tile's name in the
   app**, in plain words and Title Case, such as `Revenue This Week` or `Calls Today`. Never a file name, no `.html`,
   no dashes or underscores, at most 40 characters.
8. Scripts must finish drawing within half a second of loading; the picture is taken then.

## Steps

1. **Start from the template:** copy `${HERMES_SKILL_DIR}/template.html` to where the tile will live, e.g.
   `~/dashboards/<short-name>.html` (keep the same path for the life of the tile: the pin points at it). Keep the
   parts marked `KEEP`; change the look, the data block and the parts freely to match what the person asked for.
2. **Fill in real data** in the `<script type="application/json" id="data">` block (and an `updated` time).
3. **Check it:** `python3 ${HERMES_SKILL_DIR}/check_tile.py <file>`. Fix every `PROBLEM` and run it again. When it
   says `PICTURES:`, look at `tiles.png` (every size side by side, light and dark) before handing it over: words cut
   off, crowded small tiles or empty large ones are yours to fix even when the check passes.
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
  time, and writes the file (write to a temporary file, then rename over the tile, so it is never half-written).
  Schedule a job that runs it. Run it once yourself first and run the check again.
- When the data needs you (reading mail, judging something), schedule a job whose prompt is: "Update the Dashboard
  tile `<path>` with the dashboard-tile skill: <what to show>. Keep the path and the look. Reply [SILENT]."
- Choose a sensible interval (every 15 minutes for live numbers, hourly or daily for most things).
- **Write the job's id into the page** (and keep it there each time the page is rewritten), so the app's tile menu
  can change how often it runs, update it now, or pause it:
  `<meta name="hq-tile-job" content="<job id>">` in the `<head>`. Make one job per tile.
- Tell the person how often it updates, and that they can change it from the tile: hold it (or open it and tap the
  clock) and choose **How Often**.

The app redraws pinned tiles when you rewrite the file during a chat with it, when the person pulls to refresh,
and when it next opens the Dashboard after the file changed.

## Don'ts

- Don't load anything from the internet in the page, and don't make the page fetch its own data.
- Don't make one tall scrolling report: that's a page, not a tile (it is pictured from the top only).
- Don't pick the tile's look against what the person asked for; the template's look is only a neutral start.
- Don't move or rename a pinned tile's file; make a new tile instead.
