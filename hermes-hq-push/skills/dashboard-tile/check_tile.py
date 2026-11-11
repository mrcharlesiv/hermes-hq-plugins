#!/usr/bin/env python3
"""Checks a Hermes HQ Dashboard tile before it is handed over (hermes-hq-push:dashboard-tile).

    python3 check_tile.py path/to/tile.html [--shots DIR]

Two passes:
  1. Rules read from the file: one self-contained page, nothing loaded from the network, fills its window,
     no fixed pixel widths wider than a small tile, light and dark, the template's fit script still there.
  2. When Chrome or Chromium is on this computer: the page drawn in each window the app draws it in (a Grid tile at
     its own size, like a Home Screen widget; a card 390 wide), checked for scripts that fail, scrolling, parts cut
     off or drawn over each other, text too small or too faint, tiles left half empty or packed with words, with
     every size side by side in one picture for you to look at (--shots, default: a folder beside the file).

Prints one line per problem and exits 1 if any; "OK" and exit 0 otherwise. Warnings don't fail the check.
With no browser to draw the page (NOT DRAWN) only the file rules are checked: set HQ_TILE_CHROME to a Chrome or
Chromium binary, or install one (`npx playwright install chromium`, `apt install chromium`).
"""
import argparse
from datetime import datetime, timedelta, timezone
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# The windows the app draws a tile's page in (name, width, height, look). A Grid tile is drawn at its real size in
# points, as a Home Screen widget is: a phone has two ~175 pt columns; an iPad or a Mac has more, so Extra Large is
# wide there. A card (the Cards view of Tiles, the Pinned shelf on Artifacts) is drawn 390 wide in the card's shape
# and shown at the card's width: about 200-235 high on a phone, 140-290 on an iPad or a Mac.
SHAPES = [("small", 175, 175, "light"), ("small", 175, 175, "dark"), ("small on a small phone", 161, 161, "light"),
          ("medium", 362, 175, "light"), ("medium", 362, 175, "dark"), ("large", 362, 382, "light"),
          ("extra-large", 362, 589, "dark"), ("card on Tiles", 390, 223, "dark"), ("card on a big phone", 390, 200, "light"),
          ("widest card on an iPad or Mac", 390, 140, "light"),
          ("extra-large on an iPad or Mac", 760, 570, "light"), ("opened full screen on a phone", 402, 780, "dark")]
MAX_BYTES = 1_500_000
SHEET_WIDTH = 800
# The template's script that makes a tile fit itself (parts marked data-drop go, lists lose items, the number shrinks).
FIT_MARK = "hq-tile-fit"


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
    m = re.search(r'"(?:updated|updated_at|as_of)"\s*:\s*"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?)"', html)
    if m:
        problems.append(f'"{m.group(1)}" has no time zone: write it with one ("…Z" or "+01:00", e.g. Python '
                        "datetime.now().astimezone().isoformat(timespec='seconds')) so the phone shows its own time")
    # A time typed by hand is often minutes ahead of the clock ("Updated 3:08 PM" at 3:05).
    ahead = re.search(r'"updated"\s*:\s*"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d))"', html)
    if ahead:
        try:
            stamp = datetime.fromisoformat(ahead.group(1).replace("Z", "+00:00").replace(" ", "T"))
            if stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
                problems.append(f'"updated" is {ahead.group(1)}, which hasn\'t happened yet: take the time from the clock '
                                "(`date -Iseconds`, or datetime.now().astimezone().isoformat(timespec='seconds')), never by hand")
        except ValueError:
            pass
    if "prefers-color-scheme" not in html:
        warnings.append("no dark look (@media (prefers-color-scheme: dark)): the tile is drawn in the app's light or dark look")
    if has_fit(html) and not re.search(r"\bdata-drop\s*=", html):
        warnings.append("no part is marked data-drop: the fit script has nothing it may leave out when the tile is too small "
                        "(mark the parts that can go, 1 first)")
    return problems, warnings


def has_fit(html):
    """The template's fit script is in the page (the script itself, not a comment that names it)."""
    return bool(re.search(r'<script[^>]*\bid\s*=\s*["\']?' + FIT_MARK + r'\b', html, re.I)) and "data-off" in html


def find_chrome():
    """The first Chrome or Chromium found (None when there is none)."""
    found = find_chromes()
    return found[0] if found else None


def find_chromes():
    """Every Chrome or Chromium to draw the page with, the likeliest first: the one named in HQ_TILE_CHROME (or
    CHROME_PATH), those on the PATH, installed apps, and the ones Playwright or Puppeteer downloaded (a bot's own
    browser tool often did). The check tries the next when one won't start."""
    found = []

    def add(path):
        path = os.path.realpath(os.path.expanduser(path))
        if path not in found and os.path.isfile(path) and os.access(path, os.X_OK):
            found.append(path)

    for var in ("HQ_TILE_CHROME", "CHROME_PATH", "CHROME_BIN"):
        if os.environ.get(var):
            add(os.environ[var])
    names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "chrome-headless-shell",
             "headless_shell", "microsoft-edge", "microsoft-edge-stable", "brave-browser", "brave"]
    for name in names:
        if shutil.which(name):
            add(shutil.which(name))
    home = os.path.expanduser("~")
    candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                  "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                  home + "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  home + "/Applications/Chromium.app/Contents/MacOS/Chromium",
                  "/snap/bin/chromium", "/usr/lib/chromium/chromium", "/usr/lib/chromium-browser/chromium-browser",
                  "/usr/lib64/chromium-browser/chromium-browser", "/opt/google/chrome/chrome", "/opt/pw-browsers/chromium"]
    # Playwright's and Puppeteer's downloads, the newest first: a whole Chromium, else the headless shell.
    stores = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "", home + "/.cache/ms-playwright", home + "/Library/Caches/ms-playwright",
              "/ms-playwright", "/opt/pw-browsers", home + "/.cache/puppeteer", home + "/.agent-browser/browsers"]
    patterns = ["chromium-*/chrome-linux*/chrome", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
                "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                "chrome/*/chrome-linux*/chrome", "chrome/*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                "chrome-*/chrome", "chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                "chromium_headless_shell-*/chrome-*/headless_shell", "chromium_headless_shell-*/chrome-*/chrome-headless-shell",
                "chrome-headless-shell/*/chrome-headless-shell-*/chrome-headless-shell"]
    for pattern in patterns:
        for store in stores:
            if store:
                candidates += sorted(glob.glob(os.path.join(store, pattern)), reverse=True)
    for path in candidates:
        add(path)
    return found


# Put first in a copy of the page: a script that fails leaves a tile half drawn (or blank), so each failure is kept.
ERRORS = ('<script>window.__hqErrors=[];addEventListener("error",function(e){__hqErrors.push((e.message||"a script failed")'
          '+(e.lineno?" (line "+e.lineno+")":""))});</script>')

# Appended to a copy of the page: after the page's own scripts settle (the app waits 0.7 s), measure the window and
# tell the sheet holding it what it found: each finding a kind (see FINDINGS), a count and the first place it was seen.
PROBE = r"""
<script>
setTimeout(function(){
  // innerWidth/innerHeight: a page with no <!doctype> (quirks mode) reports its whole height as clientHeight.
  var d=document.documentElement, w=Math.min(d.clientWidth, innerWidth), h=Math.min(d.clientHeight, innerHeight), out=[];
  (window.__hqErrors||[]).slice(0,2).forEach(function(m){ out.push({k:"script",n:1,first:m}); });
  if (d.scrollHeight > h + 1) out.push({k:"scrolls",n:1,first:d.scrollHeight + " tall in a " + h + " window"});
  if (d.scrollWidth > w + 1) out.push({k:"sideways",n:1,first:d.scrollWidth + " wide in a " + w + " window"});
  var all=document.body ? [].slice.call(document.body.querySelectorAll("*")) : [];
  function label(e){ var c=typeof e.className==="string"&&e.className ? "."+e.className.split(" ")[0] : ""; return e.tagName.toLowerCase()+(e.id?"#"+e.id:"")+c; }
  function says(e){ var t=e.textContent.trim().replace(/\s+/g," "); return " “"+(t.length>48 ? t.slice(0,46)+"…" : t)+"”"; }
  function ownText(e){ for (var c=e.firstChild;c;c=c.nextSibling) if(c.nodeType===3&&c.nodeValue.trim()) return c; return null; }
  function rgb(c){ var m=c&&c.match(/[\d.]+/g); return m&&m.length>=3 ? [+m[0],+m[1],+m[2],m.length>3?+m[3]:1] : null; }
  function lum(c){ return [c[0],c[1],c[2]].map(function(v){ v/=255; return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055,2.4); })
    .reduce(function(a,v,i){ return a+v*[0.2126,0.7152,0.0722][i]; },0); }
  function back(e){ for (var n=e;n&&n.nodeType===1;n=n.parentElement){ var b=rgb(getComputedStyle(n).backgroundColor); if (b&&b[3]>0.5) return b; }
    return matchMedia("(prefers-color-scheme: dark)").matches ? [0,0,0,1] : [255,255,255,1]; }
  var past=0, pastFirst="", tiny=0, small=0, smallFirst="", faint=0, faintFirst="", clipped=0, clippedFirst="",
      boxes=0, boxFirst="", under=0, underFirst="", ink=[];
  var page=getComputedStyle(document.body).backgroundColor;
  all.forEach(function(e){
    var st=getComputedStyle(e), r=e.getBoundingClientRect();
    if (r.width<1||r.height<1||st.visibility==="hidden"||+st.opacity===0) return;
    if (r.right>w+1||r.bottom>h+1||r.left<-1||r.top<-1){ past++; if(!pastFirst) pastFirst=label(e); }
    // A box whose contents are cut off at its foot (a grid squeezed into a row too short for it).
    var text=ownText(e);
    if (st.overflowY!=="visible" && e.scrollHeight>e.clientHeight+2 && !/-webkit-box/.test(st.display)){
      if (text) { clipped++; if(!clippedFirst) clippedFirst=label(e)+says(e); } else { boxes++; if(!boxFirst) boxFirst=label(e); } }
    // What draws something: pictures, and boxes with a colour or a rule of their own (not the page itself).
    var filled=st.backgroundImage!=="none" || (rgb(st.backgroundColor)||[0,0,0,0])[3]>0.02 && st.backgroundColor!==page
      || ["Top","Right","Bottom","Left"].some(function(side){ return parseFloat(st["border"+side+"Width"])>0 && st["border"+side+"Style"]!=="none"; });
    if (/^(svg|canvas|img|video)$/i.test(e.tagName) || filled && (r.width<w*0.9||r.height<h*0.9)) ink.push(r);
    if (!text) return;
    var size=parseFloat(st.fontSize);
    if (size<9) tiny++; else if (size<10.5) { small++; if(!smallFirst) smallFirst=label(e)+" "+(Math.round(size*10)/10)+" px"; }
    var fg=rgb(st.color), bg=back(e);
    if (fg&&bg){ var a=lum(fg), b=lum(bg), ratio=(Math.max(a,b)+0.05)/(Math.min(a,b)+0.05); if (ratio<3){ faint++; if(!faintFirst) faintFirst=label(e)+" "+ratio.toFixed(1)+":1"; } }
    if (st.overflow!=="visible" && e.scrollWidth>e.clientWidth+1){ clipped++; if(!clippedFirst) clippedFirst=label(e)+says(e); }
    // The words themselves: where they are drawn, and whether something else is drawn over them.
    var range=document.createRange(); range.selectNodeContents(text);
    var rects=range.getClientRects(); for (var i=0;i<rects.length;i++) ink.push(rects[i]);
    if (!rects.length) return;
    var t=rects[0], x=(Math.max(t.left,r.left)+Math.min(t.right,r.right))/2, y=(Math.max(t.top,r.top)+Math.min(t.bottom,r.bottom))/2;
    if (x<0||y<0||x>=w||y>=h) return;
    var top=document.elementFromPoint(x,y);
    if (top && top!==e && !e.contains(top) && !top.contains(e)){ under++; if(!underFirst) underFirst=label(e)+says(e)+" under "+label(top); }
  });
  function say(kind,n,first){ if (n) out.push({k:kind,n:n,first:first||""}); }
  say("past",past,pastFirst); say("boxes",boxes,boxFirst); say("under",under,underFirst); say("faint",faint,faintFirst);
  say("tiny",tiny); say("small",small,smallFirst); say("clipped",clipped,clippedFirst);
  var words=document.body ? document.body.innerText.trim().split(/\s+/).filter(function(t){ return /[A-Za-z0-9À-￿]/.test(t); }).length : 0;
  if (!words && !document.querySelector("svg,canvas,img")) out.push({k:"nothing",n:1,first:""});
  var glance=Math.round(w*h/1500);
  if (words>glance && h<700) out.push({k:"crowded",n:words,first:"a glance here is about " + glance});
  // The largest blank block (on a 24-across grid): a tile with a third of it empty has its parts in the wrong places.
  // (A tile that is one number and a few words is sparse by design; this is for a tile with things to place.)
  if (w*h>40000 && ink.length && (words>=10 || document.querySelector("svg,canvas,img"))){
    var N=24, M=Math.max(4,Math.round(N*h/w)), cw=w/N, ch=h/M, full=[], best=0, heights=[];
    for (var gy=0;gy<M;gy++){ full.push([]); for (var gx=0;gx<N;gx++) full[gy].push(false); }
    ink.forEach(function(r){ for (var gy=Math.max(0,Math.floor(r.top/ch)); gy<M && gy*ch<r.bottom; gy++)
      for (var gx=Math.max(0,Math.floor(r.left/cw)); gx<N && gx*cw<r.right; gx++) full[gy][gx]=true; });
    for (var c=0;c<N;c++) heights.push(0);
    for (var row=0;row<M;row++){
      for (var col=0;col<N;col++) heights[col]=full[row][col] ? 0 : heights[col]+1;
      for (var s=0;s<N;s++){ var low=heights[s]; for (var e2=s;e2<N&&low>0;e2++){ low=Math.min(low,heights[e2]); best=Math.max(best,low*(e2-s+1)); } }
    }
    var share=best/(N*M);
    if (share>0.3) out.push({k:"empty",n:Math.round(share*100),first:""});
  }
  if (document.compatMode === "BackCompat") out.push({k:"quirks",n:1,first:""});
  parent.postMessage({hqTile: location.hash.slice(1), problems: out}, "*");
}, 700);
</script>
"""


# What the drawn page can be found doing: kind -> (fails the check, what it is, the unit counted, what to do about it).
FINDINGS = {
    "script": (True, "a script failed, so the tile is left half drawn or blank", None, "fix the script"),
    "scrolls": (True, "the page scrolls (only its top is pictured)", None, "make it fill the window: html, body { height: 100%; overflow: hidden }"),
    "sideways": (True, "the page scrolls sideways", None, "no fixed widths: %, fr, cqw"),
    "past": (True, "parts run past the tile's edge and are cut off there", "part", "show less at these sizes (the template's fit script: "
             "data-drop on parts that can go, data-items on lists); don't shrink the text to squeeze it in"),
    "boxes": (True, "a box's contents are cut off at its foot", "box", "too much for these sizes: show fewer things there"),
    "under": (True, "text is drawn under something else", "piece", "give each part its own room (no fixed heights, no absolute positions)"),
    "faint": (True, "text is too faint to read on its background", "piece", "aim for 4.5:1"),
    "tiny": (True, "text is smaller than 9 pt, unreadable in a tile", "piece", "show fewer things, larger"),
    "nothing": (True, "the tile draws nothing (no text or picture)", None, "check the data block and the scripts"),
    "quirks": (True, "no <!doctype html> on the first line (the page is laid out in quirks mode)", None, "add it"),
    "small": (False, "text is under 11 pt, hard to read on a phone", "piece", "show fewer things, larger (the template keeps to .85em of its base size)"),
    "clipped": (False, "text is cut off with \u2026", "piece", "shorten the words (a long name may be a choice; a headline, a status or a note cut short is not)"),
    "crowded": (False, "too many words for a glance", "words", "show fewer things at these sizes: a tile is read in a second, the details are a tap away"),
    "empty": (False, "a blank block covers a third of the tile or more", "% blank", "let the chart or the items stretch into it, or lay that shape out differently"),
}


def worded(found, shapes):
    """One line per kind of finding, with each size it was seen at (light and dark of a size said once)."""
    kinds = {}
    for i, (shape, w, h, _) in enumerate(shapes):
        for f in found.get(str(i), []):
            where = kinds.setdefault(f["k"], {})
            where.setdefault(f"{shape} {w}×{h}", f)
    problems, warnings = [], []
    for kind, where in kinds.items():
        fails, what, unit, fix = FINDINGS.get(kind, (True, kind, None, ""))
        said = {f.get("first") for f in where.values()}
        if not unit and len(said) == 1 and len(where) > 1:   # the same thing at every size: said once
            (problems if fails else warnings).append(f"{what}{': ' + said.pop() if any(said) else ''} (at {len(where)} sizes, "
                                                     f"the first: {next(iter(where))}). Fix: {fix}")
            continue
        places = []
        for n, (size, f) in enumerate(where.items()):
            detail = [f"{f['n']} {unit}{'s' if unit in ('part', 'piece', 'box') and f['n'] != 1 else ''}".replace("boxs", "boxes")] if unit else []
            if f.get("first") and (n == 0 or not unit or kind == "crowded"):
                detail.append(("first: " if unit and kind != "crowded" else "") + f["first"])
            places.append(size + (f" ({', '.join(detail)})" if detail else ""))
        (problems if fails else warnings).append(f"{what}: {'; '.join(places)}. Fix: {fix}")
    return problems, warnings


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


def sheet_height(shapes):
    """How tall the sheet comes out: the frames wrap across SHEET_WIDTH, 16 apart, each with a line of caption."""
    x, row, total = 0, 0, 16
    for _, w, h, _ in shapes:
        if x and x + w > SHEET_WIDTH:
            total += row + 16
            x, row = 0, 0
        x += w + 16
        row = max(row, h + 24)
    return total + row + 16


def with_probe(html):
    """A copy of the page that keeps its scripts' failures and reports what it measured."""
    m = re.search(r"<head[^>]*>", html, re.I) or re.search(r"<!doctype[^>]*>", html, re.I)
    html = html[:m.end()] + ERRORS + html[m.end():] if m else ERRORS + html
    if re.search(r"</body>", html, re.I):
        return re.sub(r"</body>", lambda _: PROBE + "</body>", html, count=1, flags=re.I)
    return html + PROBE


def render_problems(path, chromes, shots):
    """What drawing the page found: (problems, warnings, note). A browser that won't start or never finishes gives
    way to the next one found (three at most)."""
    note = None
    for chrome in chromes[:3]:
        problems, warnings, note = render_with(path, chrome, shots)
        if note is None:
            return problems, warnings, None
    return [], [], note


def render_with(path, chrome, shots):
    """The page drawn with one browser: (problems, warnings, note). Each line names the sizes it was seen at."""
    html = open(path, encoding="utf-8", errors="replace").read()
    name = os.path.basename(chrome)
    with tempfile.TemporaryDirectory() as tmp:
        # Beside the original, so pictures and files it names by relative path still load.
        tile = os.path.join(os.path.dirname(path), f".hq-tile-check-{os.getpid()}.html")
        with open(tile, "w", encoding="utf-8") as f:
            f.write(with_probe(html))
        host = os.path.join(os.path.dirname(path), f".hq-tile-sheet-{os.getpid()}.html")
        with open(host, "w", encoding="utf-8") as f:
            f.write(sheet(SHAPES).replace('src="tile.html', f'src="{os.path.basename(tile)}'))
        try:
            base = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox", "--no-first-run",
                    "--allow-file-access-from-files", "--force-device-scale-factor=2",
                    f"--window-size={SHEET_WIDTH + 32},{sheet_height(SHAPES)}",
                    "--virtual-time-budget=3000", f"--user-data-dir={tmp}/profile"]
            url = "file://" + host
            try:
                dom = subprocess.run(base + ["--dump-dom", url], capture_output=True, text=True, timeout=45).stdout
            except OSError as exc:
                return [], [], f"NOT DRAWN: {name} wouldn't start ({exc}); only the file rules were checked"
            except subprocess.TimeoutExpired:
                return [], [], f"NOT DRAWN: {name} didn't finish drawing it in 45 s; only the file rules were checked"
            m = re.search(r'<meta name="hq-tile-check" content="([^"]*)"', dom)
            if not m:
                return [], [], f"NOT DRAWN: couldn't draw it with {name} (the page never finished); only the file rules were checked"
            found = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
            problems, warnings = worded(found, SHAPES)
            if shots:
                os.makedirs(shots, exist_ok=True)
                try:
                    subprocess.run(base + [f"--screenshot={os.path.join(shots, 'tiles.png')}", url], capture_output=True, timeout=45)
                except subprocess.TimeoutExpired:
                    pass
            return problems, warnings, None
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
    chromes = [] if args.no_render else find_chromes()
    if chromes:
        shots = args.shots or os.path.splitext(path)[0] + "-tiles"
        found, looks, note = render_problems(path, chromes, shots)
        problems += found
        warnings += looks
    elif not args.no_render:
        note = ("NOT DRAWN: no Chrome or Chromium on this computer, so the page wasn't drawn at any size and nothing above "
                "says how it looks. Draw it: set HQ_TILE_CHROME to a browser's binary, or install one (`npx playwright "
                "install chromium`, or `apt install chromium`) and run this again. If you can't, keep the template's layout "
                "and fit script as they are, and tell the person the tile wasn't checked by eye")
    if note and not has_fit(html):
        problems.append("the page wasn't drawn and it doesn't have the template's fit script (<script id=\"hq-tile-fit\">), so "
                        "nothing keeps it from being cut off in a small tile: start from template.html and keep that script")
    for p in problems:
        print("PROBLEM:", p)
    for w in warnings:
        print("WARNING:", w)
    if note:
        print(note)
    if shots and not note and os.path.isfile(os.path.join(shots, "tiles.png")):
        print(f"PICTURES: {os.path.join(shots, 'tiles.png')} has every tile size side by side: look at it before you hand "
              "the tile over (the folder is yours to delete afterwards)")
    if problems:
        return 1
    print(("OK by the file rules, but not drawn" if note else "OK: ready to pin") + ". Show it with its own line:  MEDIA:" + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
