#!/usr/bin/env python3
"""Check an HTML page for the Hermes HQ app: draw it the way the app shows it and say what looks wrong.

    python3 check_page.py <page.html> [--shots DIR] [--offline] [--json]

The page is drawn in a headless Chrome at a phone's width in dark and in light, at a small phone's width and at an
iPad's, with the app's theme (`--foreground`, `--muted-foreground`, `--accent`, `--border`, `--card`, `--hq-radius`)
and the app's default styles set first, as the app sets them. Then it is measured:

  PROBLEM  must be fixed: the page runs off the side, a checkbox sits on another line than its words, words are hard
           to read in dark or light, a script fails.
  WARNING  fix it unless you mean it: a title sharing a row with controls, a group of controls broken apart by
           wrapping, left edges that don't line up, small tap targets or text, "click"/"hover" words, something
           shown only on hover, square-cornered maps and pictures.

It writes a picture of each view (PICTURES:). Look at them: you are the one who can tell whether it reads well.
Exit status 1 when there is a PROBLEM, 2 when the page couldn't be drawn.

The page may load what it needs from the network (map tiles, a library), as it can in the app; --offline draws it
with no network at all.
"""

import argparse
import concurrent.futures
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# The views: (name, width, height, scheme, take a picture).
VIEWS = [("phone-dark", 390, 844, "dark", True), ("phone-light", 390, 844, "light", True),
         ("small-phone", 320, 640, "dark", False), ("ipad", 820, 1100, "dark", True)]

# Close to the app's default theme (App/Chat/ChatPalette.swift through HTMLPageSheet.prelude); the ground is what
# shows through a page that paints no background of its own (PageLegibility's GROUND in dark).
THEMES = {
    "dark": {"--foreground": "rgba(242,242,247,1)", "--muted-foreground": "rgba(235,235,245,0.62)",
             "--accent": "rgba(10,132,255,1)", "--border": "rgba(84,84,88,0.65)", "--card": "rgba(30,32,38,1)",
             "ground": "#101218"},
    "light": {"--foreground": "rgba(28,28,30,1)", "--muted-foreground": "rgba(60,60,67,0.7)",
              "--accent": "rgba(0,122,255,1)", "--border": "rgba(60,60,67,0.29)", "--card": "rgba(255,255,255,1)",
              "ground": "#f2f2f7"},
}

# The app's own default styles, in :where() so the page's rules win (HTMLPageSheet.prelude: keep the two alike).
DEFAULTS = (":where(html,body){margin:0;padding:0;background:transparent}:where(body){padding:12px 16px;box-sizing:border-box;"
            "overflow-wrap:break-word}:where(img,svg,canvas,video){max-width:100%}:where(svg[viewBox]){height:auto}"
            ":where(input,select,textarea,button){font:inherit;color:inherit}"
            ":where(label:has(>input[type=checkbox],>input[type=radio])){display:inline-flex;align-items:baseline;gap:.4em}"
            ":where(a){color:var(--accent)}:where(pre){white-space:pre-wrap}"
            ":where(hr){border:0;border-top:1px solid var(--border)}:where(iframe){max-width:100%;border:0}")


def prelude(scheme):
    t = THEMES[scheme]
    root = ";".join(f"{k}:{v}" for k, v in t.items() if k.startswith("--"))
    style = (f":root{{{root};--hq-radius:16px;color-scheme:{scheme};color:var(--foreground);accent-color:var(--accent);"
             f"font-family:-apple-system,system-ui,sans-serif;-webkit-text-size-adjust:100%}}" + DEFAULTS +
             f":where(html){{background:{t['ground']}}}")
    return (f'<meta name="viewport" content="width=device-width,initial-scale=1"><style id="hq-check-prelude">{style}</style>'
            '<script>window.__hqErrors=[];addEventListener("error",function(e){if(e.message)__hqErrors.push(e.message'
            '+(e.lineno?" (line "+e.lineno+")":""))});</script>')


PROBE = r"""
<script>
(function(){
function run(){
  var d=document.documentElement, vw=d.clientWidth, out=[], seen={};
  var GROUND=__GROUND__;
  function add(k,sev,first,extra){ if(seen[k]){seen[k].n++;return;} seen[k]={k:k,sev:sev,first:first,n:1,extra:extra||""}; out.push(seen[k]); }
  function name(e){ if(!e||!e.tagName) return "?"; var c=typeof e.className==="string"&&e.className.trim()?"."+e.className.trim().split(/\s+/)[0]:"";
    return e.tagName.toLowerCase()+(e.id?"#"+e.id:"")+c; }
  function says(e,n){ var t=(e.innerText||e.textContent||"").trim().replace(/\s+/g," "); n=n||40; return t.length>n?t.slice(0,n-1)+"…":t; }
  function shown(e){ var r=e.getBoundingClientRect(), s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.visibility!=="hidden"&&s.display!=="none"&&+s.opacity>0.05; }
  function rgb(c){ var m=c&&c.match(/-?[\d.]+(?:e-?\d+)?/g); if(!m||m.length<3) return null; var k=/^color\(/.test(c)?255:1;
    return [m[0]*k,m[1]*k,m[2]*k,m.length>3?+m[3]:1]; }
  function lum(c){ return [c[0],c[1],c[2]].map(function(v){v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);})
    .reduce(function(a,v,i){return a+v*[0.2126,0.7152,0.0722][i];},0); }
  function over(top,bot){ var a=top[3]; return [top[0]*a+bot[0]*(1-a),top[1]*a+bot[1]*(1-a),top[2]*a+bot[2]*(1-a),1]; }
  function hex(c){ return "#"+[0,1,2].map(function(i){return ("0"+Math.round(c[i]).toString(16)).slice(-2);}).join(""); }
  var dangerous=[];  // text over a picture: its ground is unknown, so it isn't judged
  function ground(e){ var layers=[];
    for(var a=e;a&&a.nodeType===1;a=a.parentElement){ var s=getComputedStyle(a);
      if(s.backgroundImage&&s.backgroundImage!=="none") return null;
      var c=rgb(s.backgroundColor); if(c&&c[3]>0){ layers.push(c); if(c[3]>=0.99) break; } }
    var g=GROUND.slice(); for(var i=layers.length-1;i>=0;i--) g=over(layers[i],g); return g; }

  // Script errors.
  (window.__hqErrors||[]).slice(0,3).forEach(function(m){ add("script:"+m,"P","a script fails: "+m); });

  var all=[].slice.call(document.body?document.body.querySelectorAll("*"):[]);

  // 1. Runs off the side.
  if(d.scrollWidth>vw+1){ var worst=null;
    all.forEach(function(e){ var r=e.getBoundingClientRect(); if(r.width&&r.right>vw+1&&getComputedStyle(e).position!=="fixed"){
      if(!worst||r.right>worst.r||(r.right===worst.r)) worst={e:e,r:r.right}; } });
    add("sideways","P","the page is "+d.scrollWidth+" wide in a "+vw+" window"+(worst?" ("+name(worst.e)+" reaches "+Math.round(worst.r)+")":"")+
        ": it scrolls sideways. Let it wrap (flex-wrap, max-width:100%, min-width:0) or scroll only that part (overflow-x:auto)"); }

  // 2. A checkbox or radio on another line than its words.
  [].slice.call(document.querySelectorAll("input[type=checkbox],input[type=radio]")).forEach(function(box){
    if(!shown(box)) return; var lab=box.closest("label")||(box.id&&document.querySelector('label[for="'+box.id+'"]'));
    if(!lab) return; var w=document.createTreeWalker(lab,NodeFilter.SHOW_TEXT,null), t;
    while((t=w.nextNode())&&!t.nodeValue.trim()){}
    if(!t) return; var rg=document.createRange(); rg.selectNodeContents(t); var rs=rg.getClientRects(); if(!rs.length) return;
    var a=box.getBoundingClientRect(), b=rs[0], ca=(a.top+a.bottom)/2, cb=(b.top+b.bottom)/2;
    if(Math.abs(ca-cb)>Math.max(a.height,b.height)*0.75||b.left<a.left-2&&Math.abs(ca-cb)>4)
      add("boxsplit","P","the "+box.type+" for “"+says(lab)+"” is on another line than its words: put the box and its words in one label "+
          "with display:inline-flex; align-items:center; gap:6px"); });

  // 3. Rows that wrap: a title sharing a row with controls, a group of like controls broken apart.
  function kind(e){ var tag=e.tagName.toLowerCase();
    if(/^h[1-6]$/.test(tag)) return "title";
    if(e.querySelector&&e.matches("label")&&e.querySelector("input[type=checkbox],input[type=radio]")) return "toggle";
    if(e.matches("input[type=checkbox],input[type=radio]")) return "toggle";
    if(e.matches("select,input,textarea")) return "field";
    if(e.matches("button,[role=button],a.button,a.btn,.btn")) return "button";
    if(e.matches("strong,b")||parseFloat(getComputedStyle(e).fontWeight)>=600&&!e.querySelector("input,select,button")) return "title";
    return "other"; }
  all.forEach(function(row){ var s=getComputedStyle(row);
    if(!(s.display.indexOf("flex")>=0&&s.flexWrap!=="nowrap"&&s.flexDirection.indexOf("row")===0)) return;
    var kids=[].slice.call(row.children).filter(shown); if(kids.length<2) return;
    var lines=[]; kids.forEach(function(k){ var r=k.getBoundingClientRect(), L=null;
      for(var i=0;i<lines.length;i++) if(r.top<lines[i].bottom-2&&r.bottom>lines[i].top+2){L=lines[i];break;}
      if(!L){L={top:r.top,bottom:r.bottom,kids:[]};lines.push(L);} L.kids.push(k); L.bottom=Math.max(L.bottom,r.bottom); });
    if(lines.length<2) return;
    var kinds=kids.map(kind), titled=lines.some(function(L){ var ks=L.kids.map(kind);
      return ks.indexOf("title")>=0&&ks.some(function(k){return k==="toggle"||k==="field"||k==="button";}); });
    if(titled) add("titlerow","W","the title “"+says(kids[kinds.indexOf("title")],30)+"” shares a row with controls in "+name(row)+
        ", and the row wraps at this width, so the controls fall onto lines wherever they land. Put the title on its own line and "+
        "the controls in a row of their own (one row of chips for the checkboxes, one for the menu and button)");
    ["toggle","button"].forEach(function(k){ var members=kids.filter(function(e){return kind(e)===k;}); if(members.length<2) return;
      var on=lines.filter(function(L){return L.kids.some(function(e){return members.indexOf(e)>=0;});});
      if(on.length>1&&on.some(function(L){return L.kids.some(function(e){return kind(e)!==k;});}))
        add("groupsplit:"+k,"W","the "+(k==="toggle"?"checkboxes":"buttons")+" "+members.map(function(e){return "“"+says(e,18)+"”";}).join(", ")+
            " land on different lines, mixed in with other things: give them a row (a flex-wrap group) of their own"); }); });

  // 4. Small tap targets, 5. small text, 6. hard-to-read words.
  all.forEach(function(e){ if(!shown(e)) return; var r=e.getBoundingClientRect();
    if(e.matches("button,select,input:not([type=checkbox]):not([type=radio]):not([type=hidden]):not([type=range]),[role=button],label:has(input[type=checkbox],input[type=radio])")
       &&!e.closest(".leaflet-control,.mapboxgl-ctrl,.maplibregl-ctrl")&&r.height<30)
      add("tap","W",name(e)+(says(e)?" “"+says(e,24)+"”":"")+" is "+Math.round(r.height)+" tall: make controls at least 44 tall (min-height:44px) so a finger hits them"); });
  var texts=[], tw=document.createTreeWalker(document.body||d,NodeFilter.SHOW_TEXT,null), n;
  while((n=tw.nextNode())){ if(!n.nodeValue.trim()) continue; var p=n.parentElement; if(!p||p.closest("script,style,noscript,svg")) continue; texts.push(n); }
  var judged=new Set(), probeInk=document.createElement("span"); document.body.appendChild(probeInk);
  probeInk.style.color="var(--foreground)"; var THEME_INK=getComputedStyle(probeInk).color;
  probeInk.style.color="var(--muted-foreground)"; var THEME_INK2=getComputedStyle(probeInk).color; probeInk.remove();
  texts.forEach(function(t){ var e=t.parentElement; if(judged.has(e)||!shown(e)) return; judged.add(e);
    var s=getComputedStyle(e), size=parseFloat(s.fontSize);
    if(size<12&&!e.closest("[class*=attribution],.leaflet-control,sup,sub")) add("small","W","“"+says(e,30)+"” is "+size+"px: keep words at 12px or more (13px for anything worth reading)");
    if(s.color===THEME_INK||s.color===THEME_INK2) return;   // the app's own inks: the app answers for them
    var g=ground(e); if(!g) return; var c=rgb(s.color); if(!c) return; c=over(c,g);
    var L1=lum(c),L2=lum(g), ratio=(Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    var big=size>=24||size>=18.66&&parseFloat(s.fontWeight)>=600, want=big?3:4.5;
    if(ratio<want) add("contrast:"+hex(c)+hex(g),"P","“"+says(e,36)+"” is hard to read: "+hex(c)+" on "+hex(g)+" ("+ratio.toFixed(1)+":1, "+want+" needed). "+
        "Use var(--foreground) or var(--muted-foreground) for words, or give that part its own background"); });

  // 7. Left edges that don't line up.
  function blocks(){ var res=[]; [].slice.call(document.body.children).forEach(function(c){
      if(!shown(c)||c.matches("script,style")||getComputedStyle(c).position==="fixed"||getComputedStyle(c).position==="absolute") return;
      var r=c.getBoundingClientRect(); if(r.width<vw*0.5) return; res.push(c); }); return res; }
  function leftOf(c){ var s=getComputedStyle(c), r=c.getBoundingClientRect();
    if(c.matches("img,canvas,video,iframe,svg,table,.leaflet-container")||(s.backgroundColor&&rgb(s.backgroundColor)&&rgb(s.backgroundColor)[3]>0.05)||parseFloat(s.borderLeftWidth)>0)
      return {x:r.left,what:"box"};
    var w=document.createTreeWalker(c,NodeFilter.SHOW_TEXT|NodeFilter.SHOW_ELEMENT,null), x;
    while((x=w.nextNode())){ if(x.nodeType===1){ if(!shown(x)) continue; var xs=getComputedStyle(x), xb=rgb(xs.backgroundColor);
        if(x.matches("input,select,button,img,canvas,svg,video,iframe")||(xb&&xb[3]>0.05)||parseFloat(xs.borderLeftWidth)>0)
          return {x:x.getBoundingClientRect().left,what:x.matches("input,select,button")?"controls":"box"}; continue; }
      if(!x.nodeValue.trim()) continue; var rg=document.createRange(); rg.selectNodeContents(x); var rs=rg.getClientRects(); if(rs.length) return {x:rs[0].left,what:"words"}; }
    return {x:r.left,what:"box"}; }
  var edges=blocks().map(function(c){var l=leftOf(c); return {c:c,x:Math.round(l.x),what:l.what};}).filter(function(e){return e.x>0||e.what==="box";});
  if(edges.length>1){ var xs=edges.map(function(e){return e.x;}), lo=Math.min.apply(null,xs), hi=Math.max.apply(null,xs);
    if(hi-lo>=3&&hi-lo<=48){ var a=edges.filter(function(e){return e.x===lo;})[0], b=edges.filter(function(e){return e.x===hi;})[0];
      add("edges","W","left edges don't line up: "+name(b.c)+"’s "+b.what+" start at "+hi+", "+name(a.c)+"’s "+a.what+" at "+lo+
          ". Keep one left edge: let the app's 16px sides do it and give sections no side padding of their own (or the same padding everywhere)"); } }

  // 8. Desktop words.
  var DESK=/\b(click(?:ing|ed|s)?|hover(?:ing)?|mouse ?over|right-click|double-click)\b/i;
  texts.forEach(function(t){ var e=t.parentElement; if(e.closest("code,pre,kbd")||!shown(e)||!DESK.test(t.nodeValue)) return;
    add("click","W","says “"+says(e,60)+"”: on a phone it's “tap” (and there is no hover)"); });

  // 9. Shown only on hover.
  try{ [].slice.call(document.styleSheets).forEach(function(sh){ var rules; try{rules=sh.cssRules;}catch(_){return;}
    [].slice.call(rules||[]).forEach(function(r){ if(!r.selectorText||r.selectorText.indexOf(":hover")<0) return;
      var st=r.style; if((st.display&&st.display!=="none")||(st.visibility==="visible")||(st.opacity&&+st.opacity>0.5&&/opacity/.test(r.cssText)))
        add("hover","W","“"+r.selectorText.slice(0,60)+"” shows something only on hover: there is no hover on a phone. Show it always, or on a tap"); }); }); }catch(_){}

  // 10. Square-cornered pictures and maps that don't run edge to edge.
  all.forEach(function(e){ if(!e.matches("img,canvas,video,iframe,.leaflet-container,.mapboxgl-map,.maplibregl-map")||!shown(e)) return;
    if(e.parentElement&&e.parentElement.closest(".leaflet-container,.mapboxgl-map,.maplibregl-map")) return;
    var r=e.getBoundingClientRect(); if(r.width<vw*0.6||r.height<120||r.left<=1&&r.right>=vw-1) return;
    var rad=0; for(var a=e;a&&a!==document.body;a=a.parentElement){ var s=getComputedStyle(a); rad=Math.max(rad,parseFloat(s.borderTopLeftRadius)||0);
      if(rad||a.getBoundingClientRect().width>r.width+4) break; }
    if(!rad) add("square","W",name(e)+" has square corners inside the page's margins: round it (border-radius:var(--hq-radius); overflow:hidden), or run it edge to edge"); });

  return {vw:vw, height:Math.max(d.scrollHeight,document.body?document.body.scrollHeight:0), found:out};
}
function report(){ var res; try{res=run();}catch(e){res={vw:0,height:0,found:[{k:"probe",sev:"W",first:"the check itself failed: "+e.message,n:1}]};}
  window.__hqCheckResult=res; }
if(document.readyState==="complete") setTimeout(report,1200); else addEventListener("load",function(){setTimeout(report,1200);});
})();
</script>
"""


def find_chromes():
    """The dashboard-tile check's browser search (one list of places to look for both checks)."""
    sys.path.insert(0, os.path.join(HERE, "..", "dashboard-tile"))
    try:
        from check_tile import find_chromes as tile_find
        return tile_find()
    except Exception:
        found = []
        for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "chrome-headless-shell"):
            if shutil.which(name):
                found.append(shutil.which(name))
        mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(mac):
            found.append(mac)
        return found
    finally:
        sys.path.pop(0)


def dressed(html, scheme):
    """The page as the app shows it: the theme and defaults first in <head>, the probe last."""
    ground = THEMES[scheme]["ground"].lstrip("#")
    probe = PROBE.replace("__GROUND__", json.dumps([int(ground[i:i + 2], 16) for i in (0, 2, 4)] + [1]))
    first = prelude(scheme)
    m = re.search(r"<head\b[^>]*>", html, re.I) or re.search(r"<html\b[^>]*>", html, re.I) or re.search(r"<!doctype[^>]*>", html, re.I)
    html = html[:m.end()] + first + html[m.end():] if m else first + html
    m = None
    for m in re.finditer(r"</body\s*>", html, re.I):
        pass
    return html[:m.start()] + probe + html[m.start():] if m else html + probe


class Browser:
    """One headless Chrome driven over --remote-debugging-pipe (DevTools on fds 3 and 4): a phone's true viewport
    (Chrome draws no window narrower than 500, and a page's vh must stay the phone's while the whole page is pictured)."""

    def __init__(self, chrome, profile, sandbox, offline):
        import threading
        to_chrome_r, self._w = os.pipe()
        self._r, from_chrome_w = os.pipe()

        def fds():
            os.dup2(to_chrome_r, 3)
            os.dup2(from_chrome_w, 4)
        args = [chrome, "--headless=new", "--remote-debugging-pipe", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                "--no-default-browser-check", "--mute-audio", f"--user-data-dir={profile}"]
        if not sandbox:
            args.append("--no-sandbox")
        if offline:
            args += ["--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=127.0.0.1:9", "--proxy-bypass-list=<-loopback>"]
        self.proc = subprocess.Popen(args + ["about:blank"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, preexec_fn=fds, pass_fds=(3, 4))
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._id, self._buf, self._events, self._lock = 0, b"", [], threading.Lock()

    def _read(self, deadline):
        import select
        import time
        while b"\0" not in self._buf:
            left = deadline - time.time()
            if left <= 0 or not select.select([self._r], [], [], left)[0]:
                raise TimeoutError("Chrome didn't answer")
            chunk = os.read(self._r, 1 << 20)
            if not chunk:
                raise EOFError("Chrome closed")
            self._buf += chunk
        msg, self._buf = self._buf.split(b"\0", 1)
        return json.loads(msg)

    def call(self, method, params=None, session=None, timeout=30):
        import time
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self._w, json.dumps(msg).encode() + b"\0")
        deadline = time.time() + timeout
        while True:
            m = self._read(deadline)
            if m.get("id") == self._id:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error'].get('message')}")
                return m.get("result", {})
            self._events.append(m)

    def wait_event(self, method, timeout):
        import time
        deadline = time.time() + timeout
        while True:
            for i, m in enumerate(self._events):
                if m.get("method") == method:
                    return self._events.pop(i)
            self._events.append(self._read(deadline))

    def close(self):
        try:
            self.call("Browser.close", timeout=5)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass


def draw(path, chrome, view, shots, offline, sandbox):
    import base64
    import time
    name, width, height, scheme, picture = view
    html = open(path, encoding="utf-8", errors="replace").read()
    copy = os.path.join(os.path.dirname(os.path.abspath(path)), f".hq-page-check-{os.getpid()}-{name}.html")
    with open(copy, "w", encoding="utf-8") as f:
        f.write(dressed(html, scheme))
    browser, tmp = None, ""
    try:
        tmp = tempfile.mkdtemp(prefix="hq-page-check-")
        browser = Browser(chrome, f"{tmp}/p", sandbox, offline)
        try:
            page = next(t for t in browser.call("Target.getTargets")["targetInfos"] if t["type"] == "page")
        except (TimeoutError, EOFError) as exc:
            if sandbox:
                browser.close()
                browser = Browser(chrome, f"{tmp}/p2", False, offline)
                page = next(t for t in browser.call("Target.getTargets")["targetInfos"] if t["type"] == "page")
            else:
                raise exc
        s = browser.call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})["sessionId"]
        browser.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 2,
                                                            "mobile": width < 600}, s)
        browser.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": scheme}]}, s)
        browser.call("Page.enable", {}, s)
        browser.call("Page.navigate", {"url": "file://" + copy}, s)
        try:
            browser.wait_event("Page.loadEventFired", 25)
        except TimeoutError:
            pass   # still loading something from the network: measure what is there
        found, deadline = None, time.time() + 12
        while found is None and time.time() < deadline:
            time.sleep(0.3)
            r = browser.call("Runtime.evaluate", {"expression": "JSON.stringify(window.__hqCheckResult||null)",
                                                  "returnByValue": True}, s)
            value = r.get("result", {}).get("value")
            found = json.loads(value) if value and value != "null" else None
        if found is None:
            return name, None, None, ["the page never finished (or its scripts stopped the check)"]
        shot = None
        if picture and shots:
            tall = max(height, min(int(found.get("height") or height), 2400))
            r = browser.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True,
                                                        "clip": {"x": 0, "y": 0, "width": width, "height": tall, "scale": 1}},
                             s, timeout=45)
            shot = os.path.join(shots, f"{name}.png")
            with open(shot, "wb") as f:
                f.write(base64.b64decode(r["data"]))
        return name, found, shot, None
    except (TimeoutError, EOFError, RuntimeError, OSError, StopIteration) as exc:
        return name, None, None, [f"{os.path.basename(chrome)}: {exc or type(exc).__name__}"]
    finally:
        if browser:
            browser.close()
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.remove(copy)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("page")
    ap.add_argument("--shots", help="where to put the pictures (default: a folder beside the page)")
    ap.add_argument("--offline", action="store_true", help="draw it with no network")
    ap.add_argument("--json", action="store_true", help="print the findings as JSON")
    a = ap.parse_args()
    path = os.path.abspath(os.path.expanduser(a.page))
    if not os.path.isfile(path):
        print(f"PROBLEM: no page at {path}")
        return 2
    base = os.path.splitext(os.path.basename(path))[0]
    shots = os.path.abspath(os.path.expanduser(a.shots)) if a.shots else os.path.join(os.path.dirname(path), f".{base}-check")
    os.makedirs(shots, exist_ok=True)
    chromes = find_chromes()
    if not chromes:
        print("NOT DRAWN: no Chrome or Chromium here. Install one (`npx playwright install chromium`) or set HQ_TILE_CHROME; "
              "until then, say the page wasn't checked.")
        return 2
    sandbox = not (hasattr(os, "geteuid") and os.geteuid() == 0)
    results, failed = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(draw, path, chromes[0], v, shots, a.offline, sandbox) for v in VIEWS]
        for job in jobs:
            name, found, shot, err = job.result()
            if found is None:
                failed.append(f"{name}: {err[0] if err else 'no answer'}")
            else:
                results[name] = (found, shot)
    if not results:
        print("NOT DRAWN: " + "; ".join(failed))
        return 2
    merged = {}
    for name, _, _, _, _ in VIEWS:
        if name not in results:
            continue
        for f in results[name][0]["found"]:
            key = f["k"]
            m = merged.setdefault(key, {"sev": f["sev"], "first": f["first"], "n": f["n"], "views": []})
            m["views"].append(name)
            m["n"] = max(m["n"], f["n"])
    if a.json:
        print(json.dumps({"findings": merged, "pictures": [s for _, s in results.values() if s], "failed": failed}, indent=1))
    problems = [m for m in merged.values() if m["sev"] == "P"]
    warnings = [m for m in merged.values() if m["sev"] != "P"]
    if not a.json:
        for label, group in (("PROBLEM", problems), ("WARNING", warnings)):
            for m in group:
                more = f" (+{m['n'] - 1} more like it)" if m["n"] > 1 else ""
                print(f"{label}: {m['first']}{more}  [{', '.join(m['views'])}]")
        if not merged:
            print("OK: nothing found at any size.")
        for f in failed:
            print(f"NOT DRAWN: {f}")
        pics = [s for _, s in results.values() if s]
        if pics:
            print("PICTURES: look at each before you hand the page over:")
            for p in pics:
                print("  " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
