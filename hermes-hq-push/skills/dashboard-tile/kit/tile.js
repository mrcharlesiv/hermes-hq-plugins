// Hermes HQ Dashboard tile kit: draws the tile from its data block (make_tile.py's spec). The bot gives data, never
// a page: every tile gets the same designed parts, laid out for every size, in the look it names. A part the kit
// has no block for is a "custom" block: the bot's own markup, inside the kit's layout and colours. kit/fit.js then
// makes it all fit.
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
  function nums(list) { return (list || []).filter(function (v) { return typeof v === "number" && isFinite(v); }); }
  // The look and the tile's state are the page's: the looks' colours hang off them.
  var root = document.documentElement;
  root.className = "look-" + (/^(midnight|terminal|vivid)$/.test(d.look) ? d.look : "app") + (state(d.state) ? " mood-" + state(d.state) : "");
  if (/^#[0-9a-f]{3,8}$/i.test(d.accent || "")) root.style.setProperty("--tint", d.accent);
  // A picture behind the whole tile, under a shade dark enough for white words.
  if (d.backdrop && has(d.backdrop.src)) {
    root.className += " has-backdrop";
    var backdrop = h("div", "backdrop");
    backdrop.style.backgroundImage = 'url("' + d.backdrop.src + '")';
    document.body.appendChild(backdrop);
  }
  var tile = h("div", "tile");

  // Heading: a word or two, and a badge that shows whole or not at all.
  var head = h("div", "head");
  if (has(d.title)) head.appendChild(h("div", "title", d.title));
  if (has(d.badge)) { var badge = h("div", "badge " + state(d.badge_state), d.badge); badge.setAttribute("data-whole", ""); head.appendChild(badge); }
  if (head.firstChild) tile.appendChild(head);

  // Headline: the number or the word. A tile with blocks may go without one; a tile with nothing says so.
  var blocks = (d.blocks || []).filter(function (b) { return b && typeof b === "object"; });
  if (has(d.value) || !blocks.length) {
    var headline = h("div", "headline"), big = h("div", "big " + state(d.state), has(d.value) ? d.value : (d.empty || "No data yet"));
    if (!has(d.value)) big.className = "big empty";
    big.setAttribute("data-shrink", "");
    headline.appendChild(big);
    if (has(d.delta)) headline.appendChild(h("div", "delta " + (d.trend === "up" || d.trend === "down" ? d.trend : ""), (d.trend === "up" ? "▲ " : d.trend === "down" ? "▼ " : "") + String(d.delta).replace(/^[+−-]\s*/, "")));
    tile.appendChild(headline);
  }
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
    (items || []).forEach(function (it) {
      var item = h("div", "item " + state(it.state));
      each(item, it);
      if (has(it.detail)) item.appendChild(h("span", "detail", it.detail));
      box.appendChild(item);
    });
    return box;
  }
  // A smooth line through the values that never overshoots them (monotone cubic), in a box W wide and H high.
  function path(s, W, H, pad, lo, hi) {
    var n = s.length, dx = W / (n - 1), range = (hi - lo) || 1, i, m = [], t = [];
    var y = s.map(function (v) { return H - pad - (v - lo) / range * (H - 2 * pad); });
    for (i = 0; i < n - 1; i++) m[i] = (y[i + 1] - y[i]) / dx;
    t[0] = m[0]; t[n - 1] = m[n - 2];
    for (i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : 2 * m[i - 1] * m[i] / (m[i - 1] + m[i]);
    var line = "M0 " + y[0].toFixed(2);
    for (i = 0; i < n - 1; i++) {
      line += " C" + (i * dx + dx / 3).toFixed(2) + " " + (y[i] + t[i] * dx / 3).toFixed(2) + " " +
              ((i + 1) * dx - dx / 3).toFixed(2) + " " + (y[i + 1] - t[i + 1] * dx / 3).toFixed(2) + " " +
              ((i + 1) * dx).toFixed(2) + " " + y[i + 1].toFixed(2);
    }
    return { d: line, last: y[n - 1] };
  }
  function words(label, right, drop) {
    var row = h("div", "words-row");
    row.appendChild(h("span", null, label || ""));
    if (has(right)) row.appendChild(h("span", "high", right));
    row.setAttribute("data-drop", drop);
    return row;
  }
  var count = 0, hooks = [];
  var parts = {
    // A line over its fill, several lines, or bars: stretched to whatever box the layout gives it.
    chart: function (b) {
      var lines = (b.series || []).length && typeof b.series[0] === "object" && b.series[0]
        ? b.series.map(function (l) { return { name: l.name, values: nums(l.values) }; }).filter(function (l) { return l.values.length > 1; }).slice(0, 4)
        : [{ name: null, values: nums(b.series) }];
      if (!lines.length || lines[0].values.length < 2) return null;
      var box = h("div", "chart stretch"), plot = h("div", "plot"), all = [].concat.apply([], lines.map(function (l) { return l.values; }));
      var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
      if (has(b.label) || has(b.high)) box.appendChild(words(b.label, b.high, "10"));
      if (lines.length > 1) {
        var legend = h("div", "legend"); legend.setAttribute("data-drop", "9");
        lines.forEach(function (l, i) { var key = h("span", "key c" + (i + 1)); key.appendChild(h("i", "swatch")); key.appendChild(h("span", null, l.name || "")); legend.appendChild(key); });
        box.appendChild(legend);
      }
      if (b.kind === "bars") {
        plot.className = "plot bars";
        var s = lines[0].values, base = Math.min(0, lo), span = (hi - base) || 1;
        s.forEach(function (v, i) {
          var bar = h("div", "bar" + (i === s.length - 1 ? " last" : ""));
          bar.style.height = Math.max(4, (v - base) / span * 100).toFixed(1) + "%";
          plot.appendChild(bar);
        });
      } else {
        var W = 100, H = 40, svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" aria-hidden="true">', ends = [];
        lines.forEach(function (l, i) {
          var p = path(l.values, W, H, 4, lo, hi), id = "hq-fill-" + (++count), color = "var(--c" + (i + 1) + ")";
          if (i === 0 && b.kind !== "line" && lines.length === 1) {
            svg += '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + color + '" stop-opacity=".34"/>' +
                   '<stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
                   '<path d="' + p.d + ' L' + W + ' ' + H + ' L0 ' + H + ' Z" fill="url(#' + id + ')"/>';
          }
          svg += '<path d="' + p.d + '" fill="none" stroke="' + color + '" stroke-width="' + (i ? 1.8 : 2.2) + '" vector-effect="non-scaling-stroke"' +
                 ' stroke-linejoin="round" stroke-linecap="round"/>';
          ends.push({ y: p.last / H * 100, c: i + 1 });
        });
        plot.innerHTML = svg + "</svg>";
        ends.forEach(function (e) { var dot = h("i", "dot c" + e.c); dot.style.left = "100%"; dot.style.top = e.y.toFixed(1) + "%"; plot.appendChild(dot); });
      }
      plot.setAttribute("data-least", "34 15%");
      box.appendChild(plot);
      // What the chart runs along (days, hours): up to five of its labels, evenly spread.
      var marks = (b.labels || []).filter(has);
      if (marks.length > 1) {
        var axis = h("div", "axis"), want = Math.min(b.kind === "bars" && marks.length <= 8 ? marks.length : 5, marks.length);
        for (var k = 0; k < want; k++) axis.appendChild(h("span", null, marks[Math.round(k * (marks.length - 1) / (want - 1))]));
        axis.setAttribute("data-drop", "8");
        box.appendChild(axis);
      }
      return box;
    },
    // Boxes, one per thing, each with a light for its state.
    status: function (b) {
      return list("status", ordered(b.items), function (item, it) {
        item.appendChild(named(it, true));
        item.appendChild(h("span", "amount", it.value));
      }, true);
    },
    // A ruled list: an optional lead (a time, a rank, a mark), a name, a small line of its trend, its value.
    rows: function (b) {
      return list("rows", ordered(b.items), function (item, it) {
        if (has(it.lead)) item.appendChild(h("span", "lead-in", it.lead));
        item.appendChild(named(it, !!state(it.state)));
        var s = nums(it.series);
        if (s.length > 1) {
          var spark = h("span", "spark"), p = path(s, 100, 24, 3, Math.min.apply(null, s), Math.max.apply(null, s));
          spark.innerHTML = '<svg viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true"><path d="' + p.d +
            '" fill="none" stroke="currentColor" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linecap="round"/></svg>';
          item.appendChild(spark);
        }
        if (has(it.value)) item.appendChild(h("span", "amount", it.value));
      }, true);
    },
    // How full: a bar under each name ("value" of "max", 100 unless given).
    meters: function (b) {
      return list("meters", ordered(b.items), function (item, it) {
        var top = h("div", "words"), track = h("div", "track"), fill = h("div", "fill");
        var part = Math.max(0, Math.min(1, (+it.value || 0) / (+it.max || 100)));
        top.appendChild(h("span", "name", it.name));
        top.appendChild(h("span", "amount", has(it.text) ? it.text : Math.round(part * 100) + "%"));
        fill.style.width = (part * 100).toFixed(1) + "%";
        track.appendChild(fill);
        item.appendChild(top); item.appendChild(track);
      }, true);
    },
    // A few small numbers side by side, each under its label.
    stats: function (b) {
      return list("stats", b.items, function (item, it) {
        item.appendChild(h("span", "name", it.name));
        item.appendChild(h("span", "amount", it.value));
      }, false);
    },
    // A strip of small columns, in order (the hours of a forecast, the days of a week): a label, a glyph, a value.
    strip: function (b) {
      return list("strip", b.items, function (item, it) {
        item.appendChild(h("span", "name", it.name));
        if (has(it.glyph)) item.appendChild(h("span", "glyph", it.glyph));
        if (has(it.value)) item.appendChild(h("span", "amount", it.value));
      }, false);
    },
    // Rings: how far along, one ring each, the number in its middle.
    rings: function (b) {
      var box = list("rings stretch", (b.items || []).slice(0, 4), function (item, it) {
        var part = Math.max(0, Math.min(1, (+it.value || 0) / (+it.max || 100))), wrap = h("div", "ring");
        wrap.innerHTML = '<svg viewBox="0 0 36 36" aria-hidden="true"><circle class="ring-track" cx="18" cy="18" r="15" fill="none" stroke-width="3.6"/>' +
          '<circle class="ring-fill" cx="18" cy="18" r="15" fill="none" stroke-width="3.6" stroke-linecap="round" transform="rotate(-90 18 18)" ' +
          'stroke-dasharray="' + (part * 94.25).toFixed(2) + ' 94.25"/></svg>';
        wrap.appendChild(h("span", "ring-text", has(it.text) ? it.text : Math.round(part * 100) + "%"));
        wrap.setAttribute("data-least", "36");
        item.appendChild(wrap);
        if (has(it.name)) item.appendChild(h("span", "name", it.name));
      }, false);
      return box;
    },
    // A grid of small cells, oldest first: one row is a run of days (up or down), seven rows is a year of weeks.
    heat: function (b) {
      var v = b.values || [], rows = Math.max(1, Math.min(7, Math.round(+b.rows || 1))), cols = Math.ceil(v.length / rows);
      if (!v.length) return null;
      var box = h("div", "heat" + (rows > 1 ? " stretch tall" : " flat")), plot = h("div", "plot"), top = Math.max.apply(null, nums(v).concat([1])), svg = "";
      if (has(b.label) || has(b.note)) box.appendChild(words(b.label, b.note, "10"));
      v.forEach(function (x, i) {
        var col = Math.floor(i / rows), row = i % rows, st = state(x), shade = typeof x === "number" ? Math.max(0, Math.min(1, x / top)) : null;
        svg += '<rect x="' + (col * 10 + 1) + '" y="' + (row * 10 + 1) + '" width="8" height="8" rx="1.5" ' +
          (st ? 'fill="var(--' + st + ')"' : shade ? 'fill="var(--tint)" opacity="' + (0.25 + shade * 0.75).toFixed(2) + '"' : 'fill="var(--ink2)" opacity=".18"') + "/>";
      });
      plot.innerHTML = '<svg viewBox="0 0 ' + cols * 10 + ' ' + rows * 10 + '" preserveAspectRatio="none" aria-hidden="true">' + svg + "</svg>";
      if (rows > 1) plot.setAttribute("data-least", "48 16%");
      box.appendChild(plot);
      return box;
    },
    // Shares of a whole: one bar cut into its parts, each named under it.
    split: function (b) {
      var items = (b.items || []).filter(function (it) { return +it.value > 0; }).slice(0, 6);
      var total = items.reduce(function (a, it) { return a + (+it.value); }, 0);
      if (!total) return null;
      var box = h("div", "split"), bar = h("div", "split-bar");
      items.forEach(function (it, i) { var seg = h("i", "seg c" + (i + 1)); seg.style.flexGrow = (+it.value / total * 1000).toFixed(0); bar.appendChild(seg); });
      box.appendChild(bar);
      var keys = list("split-keys", items.map(function (it, i) { return { it: it, i: i }; }), function (item, e) {
        item.className += " c" + (e.i + 1);
        var name = h("span", "name"); name.appendChild(h("i", "swatch")); name.appendChild(h("span", null, e.it.name));
        item.appendChild(name);
        item.appendChild(h("span", "amount", has(e.it.text) ? e.it.text : Math.round(+e.it.value / total * 100) + "%"));
      }, true);
      box.appendChild(keys);
      box.inner = keys;
      return box;
    },
    // A small table: the first column names the row; the last columns go first when the tile is narrow.
    table: function (b) {
      var columns = (b.columns || []).map(String), n = columns.length;
      if (n < 2) return null;
      var box = list("table", b.rows || [], function (item, row) {
        columns.forEach(function (_, c) { item.appendChild(h("span", "cell" + (c ? " amount" : " name"), row[c] == null ? "" : row[c])); });
      }, true);
      var top = h("div", "table-head");
      columns.forEach(function (name, c) { top.appendChild(h("span", "cell" + (c ? " amount" : ""), name)); });
      var wrap = h("div", "table-box");
      wrap.appendChild(top); wrap.appendChild(box);
      hooks.push(function () {
        var room = wrap.getBoundingClientRect().width, em = parseFloat(getComputedStyle(wrap).fontSize) || 12;
        var show = Math.max(2, Math.min(n, Math.floor((room - 5.5 * em) / (4.2 * em)) + 1));
        [top].concat([].slice.call(box.children)).forEach(function (row) {
          if (row.hasAttribute("data-more-cell")) return;
          row.style.gridTemplateColumns = "minmax(0, 1.7fr) repeat(" + (show - 1) + ", minmax(0, 1fr))";
          [].forEach.call(row.children, function (cell, c) { cell.style.display = c < show ? "" : "none"; });
        });
      });
      // The rows are what is trimmed: the marks the layout puts on a block go on the list inside.
      wrap.inner = box;
      return wrap;
    },
    // Words: a note or a quote, as many lines as the tile has room for.
    text: function (b) {
      if (!has(b.text)) return null;
      var box = h("div", "text stretch" + (b.kind === "quote" ? " quote" : "")), body = h("div", "text-body", b.text);
      body.setAttribute("data-clamp", "");
      box.appendChild(body);
      if (has(b.by)) { var by = h("div", "text-by", b.by); by.setAttribute("data-drop", "8"); box.appendChild(by); }
      box.setAttribute("data-least", "30");
      return box;
    },
    // A picture (a photo, a logo, a drawing): covering its box, or shown whole.
    picture: function (b) {
      var box = h("div", "picture stretch" + (b.fit === "contain" ? " whole" : ""));
      if (has(b.svg)) { box.innerHTML = b.svg; box.className += " drawn"; }
      else if (has(b.src)) { box.style.backgroundImage = 'url("' + b.src + '")'; box.setAttribute("role", "img"); if (has(b.alt)) box.setAttribute("aria-label", b.alt); }
      else return null;
      box.setAttribute("data-least", "44 18%");
      return box;
    },
    // Buttons that ask the tile's bot for something (they work in the app's Grid and when the tile is opened).
    buttons: function (b) {
      return list("buttons", b.items, function (item, it) {
        var button = h("button", "button", it.label);
        button.type = "button";
        button.setAttribute("data-hermes" + "-send", it.send || it.label);
        item.appendChild(button);
      }, false);
    },
    // The bot's own drawing, for what no block here draws: its markup and styles, in the kit's layout and colours.
    custom: function (b) {
      if (!has(b.html)) return null;
      var box = h("div", "custom" + (b.stretch === false ? "" : " stretch"));
      box.innerHTML = b.html;
      if (has(b.css)) { var style = h("style", null, b.css); document.head.appendChild(style); }
      if (has(b.js)) hooks.push(function once() { if (once.done) return; once.done = true; try { new Function("block", "data", b.js)(box, d); } catch (e) { setTimeout(function () { throw e; }); } });
      if (has(b.least)) box.setAttribute("data-least", String(b.least));
      return box;
    }
  };
  // The first block is the main one (it gets the room); the rest sit beside or under it and go first when the tile
  // is small: the last of them first, then the caption, the foot, and the main block's items last of all.
  var built = blocks.map(function (b) { var make = parts[b.type], e = make ? make(b) : null; return e ? { e: e, b: b } : null; }).filter(Boolean);
  var main = h("div", "main"), side = h("div", "side");
  side.setAttribute("data-box", "");
  built.forEach(function (p, i) {
    // The block goes at its turn; the list in it (the block itself, or the rows inside a table) is what loses items.
    var rows = p.e.inner || (p.e.hasAttribute("data-items") ? p.e : null), items = rows ? rows.children.length : 0;
    p.e.classList.add("block");
    if (!/\b(chart|heat|picture)\b/.test(p.e.className)) p.e.setAttribute("data-fits", "");
    p.e.setAttribute("data-drop", has(p.b.drop) ? p.b.drop : (i === 0 ? 40 : built.length - i));
    if (items) rows.setAttribute("data-keep", has(p.b.keep) ? p.b.keep : Math.min(i === 0 && rows.hasAttribute("data-more") ? 1 : 2, items));
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
  } else if (when && d.foot !== false) {
    var foot = h("div", "foot");
    foot.appendChild(h("span", "lead", "Updated "));
    foot.appendChild(document.createTextNode(when));
    if (has(d.source)) foot.appendChild(h("span", "source", " · " + d.source));
    foot.setAttribute("data-drop", "30");
    tile.appendChild(foot);
  }
  document.body.appendChild(tile);
  // What depends on the tile's width (a table's columns, a custom block's own script) runs before each fit.
  window.hqTileLayout = function () { hooks.forEach(function (hook) { hook(); }); };
})();
