// Hermes HQ Dashboard tile kit: draws the tile from its data block (make_tile.py's spec). The bot gives data, never
// markup: every tile gets the same parts, drawn the same way, in the look it names. kit/fit.js then makes it fit.
(function () {
  var d = JSON.parse(document.getElementById("data").textContent);
  var STATES = { good: 1, warn: 1, bad: 1 };
  function state(s) { return STATES[s] ? s : ""; }
  function h(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function has(v) { return v != null && v !== ""; }
  // The look and the tile's state are the page's: the looks' colours hang off them.
  var root = document.documentElement;
  root.className = "look-" + (/^(midnight|terminal|vivid)$/.test(d.look) ? d.look : "app") + (state(d.state) ? " mood-" + state(d.state) : "");
  if (/^#[0-9a-f]{3,8}$/i.test(d.accent || "")) root.style.setProperty("--tint", d.accent);
  var tile = h("div", "tile");

  // Heading: a word or two, and a badge that shows whole or not at all.
  var head = h("div", "head");
  if (has(d.title)) head.appendChild(h("div", "title", d.title));
  if (has(d.badge)) { var badge = h("div", "badge " + state(d.badge_state), d.badge); badge.setAttribute("data-whole", ""); head.appendChild(badge); }
  if (head.firstChild) tile.appendChild(head);

  // Headline: the number or the word. Never a blank tile: no value yet says so.
  var headline = h("div", "headline"), big = h("div", "big " + state(d.state), has(d.value) ? d.value : (d.empty || "No data yet"));
  if (!has(d.value)) big.className = "big empty";
  big.setAttribute("data-shrink", "");
  headline.appendChild(big);
  if (has(d.delta)) headline.appendChild(h("div", "delta " + (d.trend === "up" || d.trend === "down" ? d.trend : ""), (d.trend === "up" ? "▲ " : d.trend === "down" ? "▼ " : "") + String(d.delta).replace(/^[+−-]\s*/, "")));
  tile.appendChild(headline);
  if (has(d.caption)) { var caption = h("div", "caption", d.caption); caption.setAttribute("data-drop", "20"); tile.appendChild(caption); }

  // What is wrong comes first, so it is what stays when only a few fit.
  function ordered(items) {
    var rank = { bad: 0, warn: 1 };
    return (items || []).map(function (it, i) { return { it: it, i: i }; }).sort(function (a, b) {
      var x = rank[a.it.state], y = rank[b.it.state];
      return (x == null ? 2 : x) - (y == null ? 2 : y) || a.i - b.i;
    }).map(function (e) { return e.it; });
  }
  function named(it, pip) {
    var name = h("span", "name");
    if (pip) name.appendChild(h("i", "pip"));
    name.appendChild(h("span", null, it.name));
    return name;
  }
  function list(cls, items, each, more) {
    var box = h("div", cls);
    box.setAttribute("data-items", "");
    if (more) box.setAttribute("data-more", "+{n} more");
    items.forEach(function (it) {
      var item = h("div", "item " + state(it.state));
      each(item, it);
      if (has(it.detail)) item.appendChild(h("span", "detail", it.detail));
      box.appendChild(item);
    });
    return box;
  }
  var gradients = 0;
  var parts = {
    // A line over its fill (or bars), stretched to whatever box the layout gives it.
    chart: function (b) {
      var s = (b.series || []).filter(function (v) { return typeof v === "number" && isFinite(v); });
      if (s.length < 2) return null;
      var box = h("div", "chart"), plot = h("div", "plot");
      if (has(b.label) || has(b.high)) {
        var words = h("div", "chart-words");
        words.setAttribute("data-drop", "10");   // the chart's words go before the chart gets too thin to read
        words.appendChild(h("span", null, b.label || ""));
        if (has(b.high)) words.appendChild(h("span", "high", b.high));
        box.appendChild(words);
      }
      var lo = Math.min.apply(null, s), hi = Math.max.apply(null, s);
      if (b.kind === "bars") {
        plot.className = "plot bars";
        var base = Math.min(0, lo), span = (hi - base) || 1;
        s.forEach(function (v, i) {
          var bar = h("div", "bar" + (i === s.length - 1 ? " last" : ""));
          bar.style.height = Math.max(4, (v - base) / span * 100).toFixed(1) + "%";
          plot.appendChild(bar);
        });
      } else {
        var W = 100, H = 40, range = (hi - lo) || 1, i, n = s.length, dx = W / (n - 1);
        var y = s.map(function (v) { return H - 4 - (v - lo) / range * (H - 8); }), m = [], t = [];
        // A smooth line that never overshoots its points (monotone cubic).
        for (i = 0; i < n - 1; i++) m[i] = (y[i + 1] - y[i]) / dx;
        t[0] = m[0]; t[n - 1] = m[n - 2];
        for (i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : 2 * m[i - 1] * m[i] / (m[i - 1] + m[i]);
        var line = "M0 " + y[0].toFixed(2);
        for (i = 0; i < n - 1; i++) {
          line += " C" + (i * dx + dx / 3).toFixed(2) + " " + (y[i] + t[i] * dx / 3).toFixed(2) + " " +
                  ((i + 1) * dx - dx / 3).toFixed(2) + " " + (y[i + 1] - t[i + 1] * dx / 3).toFixed(2) + " " +
                  ((i + 1) * dx).toFixed(2) + " " + y[i + 1].toFixed(2);
        }
        var id = "hq-fill-" + (++gradients);
        plot.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" aria-hidden="true">' +
          '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--tint)" stop-opacity=".34"/>' +
          '<stop offset="1" stop-color="var(--tint)" stop-opacity="0"/></linearGradient></defs>' +
          (b.kind === "line" ? "" : '<path d="' + line + ' L' + W + ' ' + H + ' L0 ' + H + ' Z" fill="url(#' + id + ')"/>') +
          '<path d="' + line + '" fill="none" stroke="var(--tint)" stroke-width="2.2" vector-effect="non-scaling-stroke"' +
          ' stroke-linejoin="round" stroke-linecap="round"/></svg>';
        var dot = h("i", "dot");
        dot.style.left = "100%"; dot.style.top = (y[n - 1] / H * 100).toFixed(1) + "%";
        plot.appendChild(dot);
      }
      box.appendChild(plot);
      plot.setAttribute("data-least", "34 15%");
      return box;
    },
    // Boxes, one per thing, each with a light for its state.
    status: function (b) {
      return list("status", ordered(b.items), function (item, it) {
        item.appendChild(named(it, true));
        item.appendChild(h("span", "amount", it.value));
      }, true);
    },
    // A name and its number, ruled.
    rows: function (b) {
      return list("rows", ordered(b.items), function (item, it) {
        item.appendChild(named(it, !!state(it.state)));
        item.appendChild(h("span", "amount", it.value));
      }, true);
    },
    // How full: a bar under each name ("value" of "max", 100 unless given).
    meters: function (b) {
      return list("meters", ordered(b.items), function (item, it) {
        var words = h("div", "words"), track = h("div", "track"), fill = h("div", "fill");
        var part = Math.max(0, Math.min(1, (+it.value || 0) / (+it.max || 100)));
        words.appendChild(h("span", "name", it.name));
        words.appendChild(h("span", "amount", has(it.text) ? it.text : Math.round(part * 100) + "%"));
        fill.style.width = (part * 100).toFixed(1) + "%";
        track.appendChild(fill);
        item.appendChild(words); item.appendChild(track);
      }, true);
    },
    // A few small numbers side by side, each under its label.
    stats: function (b) {
      return list("stats", b.items || [], function (item, it) {
        item.appendChild(h("span", "name", it.name));
        item.appendChild(h("span", "amount", it.value));
      }, false);
    }
  };
  // The first part is the main one (it gets the room); the rest sit beside or under it and go first when the tile
  // is small: the last of them first, then the caption, the foot, and the main part's items last of all.
  var built = (d.blocks || []).map(function (b) { var make = parts[b && b.type], e = make ? make(b) : null; return e ? { e: e, b: b } : null; })
    .filter(Boolean);
  var main = h("div", "main"), side = h("div", "side");
  side.setAttribute("data-box", "");
  built.forEach(function (p, i) {
    var items = p.e.hasAttribute("data-items") ? p.e.children.length : 0;
    p.e.setAttribute("data-drop", has(p.b.drop) ? p.b.drop : (i === 0 ? 40 : built.length - i));
    if (items) p.e.setAttribute("data-keep", has(p.b.keep) ? p.b.keep : (p.b.type === "stats" ? Math.min(2, items) : i === 0 ? 1 : Math.min(2, items)));
    (i === 0 ? main : side).appendChild(p.e);
  });
  if (main.firstChild) tile.appendChild(main);
  if (side.firstChild) tile.appendChild(side);

  // Foot: a time, not "5 min ago" (the tile is a picture, redrawn now and then). A failed update always says so:
  // it has no data-drop, so everything else goes before it does.
  var when = "";
  if (d.updated) {
    var at = new Date(d.updated);
    when = isNaN(at) ? d.updated : at.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  }
  if (has(d.error)) {
    tile.appendChild(h("div", "foot failed", "Couldn’t update" + (when ? " · last " + when : "") + " · " + d.error));
  } else if (when) {
    var foot = h("div", "foot");
    foot.appendChild(h("span", "lead", "Updated "));
    foot.appendChild(document.createTextNode(when));
    if (has(d.source)) foot.appendChild(h("span", "source", " · " + d.source));
    foot.setAttribute("data-drop", "30");
    tile.appendChild(foot);
  }
  document.body.appendChild(tile);
})();
