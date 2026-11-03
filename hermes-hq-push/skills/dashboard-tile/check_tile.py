#!/usr/bin/env python3
"""Checks a Hermes HQ Dashboard tile before it is handed over (hermes-hq-push:dashboard-tile).

    python3 check_tile.py path/to/tile.html [--shots DIR]

Two passes:
  1. Rules read from the file: one self-contained page, nothing loaded from the network, fills its window,
     no fixed pixel widths wider than a small tile, light and dark.
  2. When Chrome or Chromium is on this computer: the page drawn at each tile's real size (the app draws a tile's
     page at the size it shows, like a Home Screen widget), checked for scrolling, overflow and text too small to
     read, with every size side by side in one picture for you to look at (--shots, default: a folder beside the file).

Prints one line per problem and exits 1 if any; "OK" and exit 0 otherwise. Warnings don't fail the check.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# The windows the app draws a tile's page in: the tile's real size in points, as a Home Screen widget is drawn
# (name, width, height, look). A phone has two ~175 pt columns; an iPad or a Mac has more, so Extra Large is wide there.
SHAPES = [("small", 175, 175, "light"), ("small", 175, 175, "dark"), ("small on a small phone", 161, 161, "light"),
          ("medium", 362, 175, "light"), ("medium", 362, 175, "dark"), ("large", 362, 382, "light"),
          ("extra-large", 362, 589, "dark"), ("card on Artifacts", 260, 140, "light"), ("card on Tiles", 370, 210, "dark"),
          ("extra-large on an iPad or Mac", 760, 570, "light"), ("opened full screen on a phone", 402, 780, "dark")]
MAX_BYTES = 1_500_000
SHEET_WIDTH = 760


def static_problems(html):
    problems, warnings = [], []
    if len(html.encode("utf-8")) > MAX_BYTES:
        problems.append(f"the file is {len(html) // 1000} KB; keep a tile under {MAX_BYTES // 1_000_000} MB (pictures small, or drawn in SVG)")
    if not re.search(r'<meta[^>]+name=["\']viewport["\'][^>]*width=device-width', html, re.I):
        problems.append('no <meta name="viewport" content="width=device-width, initial-scale=1">: the page is laid out 980 wide and shrunk')
    # The tile's name in the app is the page's <title> (Hermes HQ: "Revenue This Week", not "revenue.html").
    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
    title = " ".join(m.group(1).split()) if m else ""
    if not title:
        problems.append('no <title>: the app names the tile from it; give it a plain name such as "Revenue This Week"')
    elif title.lower() in ("tile title", "title", "untitled", "document", "tile"):
        problems.append(f'the <title> is still "{title}": name the tile in plain words, such as "Revenue This Week"')
    elif re.search(r"\.html?\b|[/\\]|^[\w-]+_[\w_-]+$|^[a-z0-9]+(-[a-z0-9]+)+$", title, re.I):
        problems.append(f'the <title> "{title}" reads as a file name: the app shows it as the tile\'s name, so use plain words ("Revenue This Week")')
    elif len(title) > 40:
        problems.append(f'the <title> is {len(title)} characters: keep the tile\'s name to 40 or fewer (it sits under a small tile)')
    # Anything from the network: scripts, styles, fonts, pictures, fetch/XHR, iframes.
    for m in re.finditer(r'\b(?:src|href)\s*=\s*["\'](https?:)?//[^"\']+', html, re.I):
        tag = html.rfind("<", 0, m.start())
        if re.match(r"<a\b", html[tag:tag + 3], re.I):
            continue   # a link to tap is fine
        problems.append(f"loads from the network: {m.group(0)[:90]} (the tile is pictured offline: inline it)")
    for m in re.finditer(r'url\(\s*["\']?(https?:)?//[^)"\']+', html, re.I):
        problems.append(f"CSS loads from the network: {m.group(0)[:90]}")
    for m in re.finditer(r'@import\s+(?:url\()?\s*["\']https?:', html, re.I):
        problems.append(f"CSS @import from the network: {m.group(0)[:90]}")
    if re.search(r'\bfetch\s*\(|XMLHttpRequest|new\s+WebSocket|EventSource\s*\(', html):
        problems.append("fetches data at run time (fetch/XHR/WebSocket): put the data in the file; a scheduled job keeps it fresh")
    if re.search(r'<iframe\b', html, re.I):
        problems.append("has an <iframe>: a tile is one page")
    # Sizing.
    fills = re.search(r'100(?:vh|dvh|svh|lvh|cqh|%)', html) and re.search(r'100(?:vw|dvw|cqw|%)', html)
    if not fills:
        problems.append("nothing fills the window (no 100vh/100vw, 100% or 100cqh/100cqw on the page): it will sit small in the tile")
    if not re.search(r'overflow\s*:\s*hidden', html):
        warnings.append("html/body don't say overflow: hidden; a page that scrolls is pictured from its top only")
    for m in re.finditer(r'(?<![-\w(])(?:min-)?width\s*:\s*(\d{3,})px', html):
        if int(m.group(1)) > 150:
            problems.append(f"fixed width {m.group(1)}px: wider than a Small tile (~160-195 pt); use %, fr, cqw or vw")
    for m in re.finditer(r'(?<![-\w(])(?:min-)?height\s*:\s*(\d{3,})px', html):
        if int(m.group(1)) > 150:
            problems.append(f"fixed height {m.group(1)}px: taller than a Small or Medium tile (~160-195 pt); use %, fr, cqh or vh")
    for m in re.finditer(r'<(?:svg|canvas|img)\b[^>]*\bwidth\s*=\s*["\']?(\d{3,})(?!%)', html, re.I):
        if int(m.group(1)) > 160 and "viewBox" not in m.group(0):
            warnings.append(f"a {m.group(0)[:40]}… with a fixed width; give charts a viewBox and width:100%")
    px_fonts = [int(v) for v in re.findall(r'font-size\s*:\s*(\d+)px', html)]
    if px_fonts and not re.search(r'font-size\s*:\s*clamp\(|font-size\s*:[^;]*(?:vmin|vw|vh|cq)', html):
        warnings.append("every font size is in px: text won't grow in a Large tile or shrink in a Medium one (use clamp() with vmin/cqmin, then em)")
    # The picture is taken 0.7 s after the page loads: anything that animates in is caught halfway.
    if re.search(r"@keyframes|\banimation\s*:|\.animate\s*\(|requestAnimationFrame", html):
        warnings.append("animates (an animation or @keyframes): the app pictures the tile 0.7 s after it loads, so a chart that grows in "
                        "is caught halfway; draw the finished state at once")
    # "updated" with no time zone is read as the phone's own time: a bot on a UTC server shows the wrong hour.
    for m in re.finditer(r'"(?:updated|updated_at|as_of|time)"\s*:\s*"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?)"', html):
        problems.append(f'"{m.group(1)}" has no time zone: write it with one ("…Z" or "+01:00", e.g. Python '
                        "datetime.now().astimezone().isoformat(timespec='seconds')) so the phone shows its own time")
    if "prefers-color-scheme" not in html:
        warnings.append("no dark look (@media (prefers-color-scheme: dark)): the tile is drawn in the app's light or dark look")
    return problems, warnings


def find_chrome():
    names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/opt/pw-browsers/chromium"]
    candidates += sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")), reverse=True)
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


# Appended to a copy of the page: after the page's own scripts settle (the app waits 0.7 s), measure the window and
# tell the sheet holding it.
PROBE = r"""
<script>
setTimeout(function(){
  // innerWidth/innerHeight: a page with no <!doctype> (quirks mode) reports its whole height as clientHeight.
  var d=document.documentElement, w=Math.min(d.clientWidth, innerWidth), h=Math.min(d.clientHeight, innerHeight), out=[];
  if (d.scrollHeight > h + 1) out.push("scrolls: content is " + d.scrollHeight + " tall in a " + h + " window");
  if (d.scrollWidth > w + 1) out.push("scrolls sideways: content is " + d.scrollWidth + " wide in a " + w + " window");
  var all=document.body ? document.body.querySelectorAll("*") : [], cut=0, first="", tiny=0;
  function label(e){ var c=typeof e.className==="string"&&e.className ? "."+e.className.split(" ")[0] : ""; return e.tagName.toLowerCase()+(e.id?"#"+e.id:"")+c; }
  for (var i=0;i<all.length;i++){ var e=all[i], r=e.getBoundingClientRect();
    if (r.width<1||r.height<1) continue;
    if (r.right>w+1||r.bottom>h+1||r.left<-1||r.top<-1){ cut++; if(!first) first=label(e); }
    var own=false; for (var c=e.firstChild;c;c=c.nextSibling) if(c.nodeType===3&&c.nodeValue.trim()){own=true;break}
    if (own && parseFloat(getComputedStyle(e).fontSize) < 9) tiny++; }
  if (cut) out.push(cut + " element(s) run past the window's edge (first: " + first + ")");
  // Text hard to read on its background (under 3:1), and text cut off inside its box.
  function rgb(c){ var m=c&&c.match(/[\d.]+/g); return m&&m.length>=3 ? [+m[0],+m[1],+m[2],m.length>3?+m[3]:1] : null; }
  function lum(c){ return [c[0],c[1],c[2]].map(function(v){ v/=255; return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055,2.4); })
    .reduce(function(a,v,i){ return a+v*[0.2126,0.7152,0.0722][i]; },0); }
  function back(e){ for (var n=e;n&&n.nodeType===1;n=n.parentElement){ var b=rgb(getComputedStyle(n).backgroundColor); if (b&&b[3]>0.5) return b; }
    return matchMedia("(prefers-color-scheme: dark)").matches ? [0,0,0,1] : [255,255,255,1]; }
  var faint=0, faintFirst="", clipped=0, clippedFirst="";
  for (var j=0;j<all.length;j++){ var el=all[j], st=getComputedStyle(el), rr=el.getBoundingClientRect();
    if (rr.width<1||rr.height<1||st.visibility==="hidden"||+st.opacity===0) continue;
    var mine=false; for (var k=el.firstChild;k;k=k.nextSibling) if(k.nodeType===3&&k.nodeValue.trim()){mine=true;break}
    if (!mine) continue;
    var fg=rgb(st.color), bg=back(el);
    if (fg&&bg){ var a=lum(fg), b=lum(bg), ratio=(Math.max(a,b)+0.05)/(Math.min(a,b)+0.05); if (ratio<3){ faint++; if(!faintFirst) faintFirst=label(el)+" "+ratio.toFixed(1)+":1"; } }
    if (st.overflow!=="visible" && (el.scrollWidth>el.clientWidth+1)){ clipped++; if(!clippedFirst) clippedFirst=label(el)+" \u201c"+el.textContent.trim().slice(0,30)+"\u201d"; } }
  if (faint) out.push(faint + " piece(s) of text too faint to read on their background (first: " + faintFirst + "; aim for 4.5:1)");
  if (clipped) out.push("cut: " + clipped + " piece(s) of text cut off with \u2026 (first: " + clippedFirst + ")");
  if (tiny) out.push(tiny + " piece(s) of text smaller than 9 pt: unreadable in this tile");
  var text=document.body ? document.body.innerText.trim().length : 0;
  if (!text && !document.querySelector("svg,canvas,img")) out.push("draws nothing (no text or picture)");
  if (document.compatMode === "BackCompat") out.push("no <!doctype html> on the first line: the page is laid out in quirks mode");
  parent.postMessage({hqTile: location.hash.slice(1), problems: out}, "*");
}, 700);
</script>
"""


def sheet(shapes):
    """The page holding one frame per tile window, each exactly the tile's size, in its look."""
    frames = "".join(
        f'<figure><iframe src="tile.html#{i}" width="{w}" height="{h}" style="color-scheme:{look}"></iframe>'
        f'<figcaption>{name} · {w}×{h} · {look}</figcaption></figure>'
        for i, (name, w, h, look) in enumerate(shapes))
    return f"""<!doctype html><meta charset="utf-8"><style>
body{{margin:0;padding:16px;background:#8a8f98;font:13px -apple-system,system-ui,sans-serif;display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start;width:{SHEET_WIDTH}px}}
figure{{margin:0}} iframe{{border:0;display:block;border-radius:18px;background:transparent}} figcaption{{color:#fff;margin-top:4px}}
</style>{frames}<script>
var got={{}},n={len(shapes)};
addEventListener("message",function(e){{ if(!e.data||e.data.hqTile===undefined) return; got[e.data.hqTile]=e.data.problems;
  if(Object.keys(got).length===n){{ var m=document.createElement("meta"); m.name="hq-tile-check"; m.content=JSON.stringify(got); document.head.appendChild(m); }} }});
</script>"""


def render_problems(path, chrome, shots):
    html = open(path, encoding="utf-8", errors="replace").read()
    probed = re.sub(r"</body>", lambda _: PROBE + "</body>", html, count=1, flags=re.I) if re.search(r"</body>", html, re.I) else html + PROBE
    name = os.path.basename(chrome)
    with tempfile.TemporaryDirectory() as tmp:
        # Beside the original, so pictures and files it names by relative path still load.
        tile = os.path.join(os.path.dirname(path), f".hq-tile-check-{os.getpid()}.html")
        with open(tile, "w", encoding="utf-8") as f:
            f.write(probed)
        host = os.path.join(os.path.dirname(path), f".hq-tile-sheet-{os.getpid()}.html")
        with open(host, "w", encoding="utf-8") as f:
            f.write(sheet(SHAPES).replace('src="tile.html', f'src="{os.path.basename(tile)}'))
        try:
            height = 3700   # every size, wrapped two or three across
            base = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox", "--no-first-run",
                    "--allow-file-access-from-files", "--force-device-scale-factor=2", f"--window-size={SHEET_WIDTH + 32},{height}",
                    "--virtual-time-budget=3000", f"--user-data-dir={tmp}/profile"]
            url = "file://" + host
            try:
                dom = subprocess.run(base + ["--dump-dom", url], capture_output=True, text=True, timeout=45).stdout
            except subprocess.TimeoutExpired:
                return [], f"{name} didn't finish drawing it in 45 s; the file checks above still apply"
            m = re.search(r'<meta name="hq-tile-check" content="([^"]*)"', dom)
            if not m:
                return [], f"couldn't draw it with {name} (the page never finished); the file checks above still apply"
            found = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
            problems, cuts = [], []
            for i, (shape, w, h, look) in enumerate(SHAPES):
                for p in found.get(str(i), []):
                    line = f"{shape} ({w}×{h}, {look}): {p.removeprefix('cut: ')}"
                    # Text ending in "…" is a choice (a long name in a Small tile); worth a look, not a failure.
                    (cuts if p.startswith("cut: ") else problems).append(line)
            if cuts:
                problems.append("WARN " + "; ".join(cuts))
            if shots:
                os.makedirs(shots, exist_ok=True)
                try:
                    subprocess.run(base + [f"--screenshot={os.path.join(shots, 'tiles.png')}", url], capture_output=True, timeout=45)
                except subprocess.TimeoutExpired:
                    pass
            return problems, None
        finally:
            for f in (tile, host):
                try:
                    os.remove(f)
                except OSError:
                    pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--shots", help="folder for tiles.png, every tile size side by side (default: <file>-tiles/ beside it)")
    ap.add_argument("--no-render", action="store_true", help="only the file checks")
    args = ap.parse_args()
    path = os.path.abspath(os.path.expanduser(args.file))
    if not path.lower().endswith((".html", ".htm")):
        print(f"PROBLEM: {path} is not an .html file (the app shows .html files as live pages)")
        return 1
    try:
        html = open(path, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print(f"PROBLEM: can't read {path}: {exc}")
        return 1
    problems, warnings = static_problems(html)
    note = None
    shots = None
    if not args.no_render:
        chrome = find_chrome()
        if chrome:
            shots = args.shots or os.path.splitext(path)[0] + "-tiles"
            found, note = render_problems(path, chrome, shots)
            problems += [p for p in found if not p.startswith("WARN ")]
            warnings += [p[5:] for p in found if p.startswith("WARN ")]
        else:
            note = "no Chrome/Chromium here, so the page wasn't drawn; check the sizing rules by reading the file"
    for p in problems:
        print("PROBLEM:", p)
    for w in warnings:
        print("WARNING:", w)
    if note:
        print("NOTE:", note)
    if shots and os.path.isdir(shots):
        print(f"PICTURES: {shots} tiles.png has every tile size side by side: look at it before you hand the tile over")
    if problems:
        return 1
    print("OK: ready to pin. Show it with its own line:  MEDIA:" + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
