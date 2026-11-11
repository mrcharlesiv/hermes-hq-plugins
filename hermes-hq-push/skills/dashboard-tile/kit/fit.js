// hq-tile-fit: makes a Hermes HQ Dashboard tile fit whatever window it is drawn in. Parts marked data-drop="N" go
// in order (1 first) until the rest fits; a data-items list loses items from its end (data-more stands in for them,
// data-keep is the fewest worth showing); data-least is the least height of a part that stretches, in px or as a
// share of the tile's height ("44 14%": whichever is more); data-whole shows
// whole or not at all; data-shrink is one line that shrinks to its width; data-clamp is words that take the lines
// they have room for; data-fits is a part that goes when its contents run out of it. With nothing left out, the tile
// gets data-roomy. Kept identical in custom.html.
(function () {
  function fitTile() {
    var tile = document.querySelector(".tile"); if (!tile) return;
    function q(sel) { return [].slice.call(tile.querySelectorAll(sel)); }
    function off(e) { e.setAttribute("data-off", ""); }
    function on(e) { e.removeAttribute("data-off"); }
    function shown(e) { return e.getClientRects().length > 0; }
    function kids(c) { return [].slice.call(c.children).filter(function (k) { return !k.hasAttribute("data-more-cell"); }); }
    // "+3 more" at the end of a list that lost items (its data-more, with {n} the number left out).
    function more(c) {
      var n = kids(c).filter(function (k) { return k.hasAttribute("data-off"); }).length, m = null, words = c.getAttribute("data-more");
      [].slice.call(c.children).forEach(function (k) { if (k.hasAttribute("data-more-cell")) m = k; });
      if (!n || !words) { if (m) m.remove(); return; }
      if (!m) { m = document.createElement("div"); m.setAttribute("data-more-cell", ""); c.appendChild(m); }
      // It wears the worst state among what it stands for, so a problem left out still shows.
      var hidden = kids(c).filter(function (k) { return k.hasAttribute("data-off"); });
      var worst = hidden.some(function (k) { return k.classList.contains("bad"); }) ? " bad" : hidden.some(function (k) { return k.classList.contains("warn"); }) ? " warn" : "";
      m.className = "more" + worst;
      m.textContent = words.replace("{n}", n);
    }
    // Too much for the tile: something reaches under the tile's padding, a list's items (or a data-box's parts) run
    // out of its box, or a part that stretches has been squeezed under its data-least.
    function misfit() {
      // A grid one item across is marked "single" (its look can then lay each item out as a row).
      q("[data-items]").forEach(function (c) {
        var across = getComputedStyle(c).gridTemplateColumns.split(" ").filter(function (t) { return parseFloat(t) > 0.5; }).length;
        c.classList.toggle("single", across === 1);
      });
      var floor = tile.getBoundingClientRect().bottom - parseFloat(getComputedStyle(tile).paddingBottom) + 1, all = q("*"), i, r;
      for (i = 0; i < all.length; i++) {
        r = all[i].getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && r.bottom > floor) return true;
      }
      var lists = q("[data-items],[data-box]"), least = q("[data-least]"), box, ks, k;
      for (i = 0; i < lists.length; i++) {
        if (!shown(lists[i])) continue;
        box = lists[i].getBoundingClientRect().bottom + 1; ks = lists[i].children;
        for (k = 0; k < ks.length; k++) if (shown(ks[k]) && ks[k].getBoundingClientRect().bottom > box) return true;
      }
      // data-fits: a part whose own contents must not run out of it (a drawing in a box too small for it).
      var whole = q("[data-fits]");
      for (i = 0; i < whole.length; i++) if (shown(whole[i]) && whole[i].scrollHeight > whole[i].clientHeight + 2) return true;
      // data-least: px, or a share of the tile's height ("44 14%": whichever is more).
      for (i = 0; i < least.length; i++) {
        if (!shown(least[i])) continue;
        var need = 0, high = tile.getBoundingClientRect().height;
        least[i].getAttribute("data-least").split(/\s+/).forEach(function (v) { need = Math.max(need, /%$/.test(v) ? parseFloat(v) / 100 * high : parseFloat(v) || 0); });
        if (least[i].getBoundingClientRect().height < need) return true;
      }
      return false;
    }
    // A list showing its first `count` items.
    function show(c, count) { kids(c).forEach(function (k, i) { if (i < count) on(k); else off(k); }); more(c); }
    // The most of a list's items that fit, `from` at the least: false when even those don't (a long list is
    // halved, not walked: the tile is pictured 0.7 s after it loads).
    function most(c, from) {
      var hi = kids(c).length, lo = Math.min(from, hi), mid;
      show(c, hi); if (!misfit()) return true;
      show(c, lo); if (misfit()) return false;
      while (hi - lo > 1) { mid = (lo + hi) >> 1; show(c, mid); if (misfit()) hi = mid; else lo = mid; }
      show(c, lo); return true;
    }
    // The list a part trims: the part itself, or the one list inside it that has no turn of its own (a table's rows).
    function listOf(part) {
      if (part.hasAttribute("data-items")) return part;
      var inside = [].slice.call(part.querySelectorAll("[data-items]")).filter(function (l) { return !l.hasAttribute("data-drop"); });
      return inside.length === 1 ? inside[0] : null;
    }
    function shrink(e) {
      e.style.fontSize = "";
      for (var size = parseFloat(getComputedStyle(e).fontSize); size > 12 && e.scrollWidth > e.parentNode.clientWidth; ) {
        size -= 1; e.style.fontSize = size + "px";
      }
    }
    // Start again from everything showing (the tile changed size); the page lays out what goes by its width first.
    tile.removeAttribute("data-roomy");
    q("[data-off]").forEach(on);
    if (typeof window.hqTileLayout === "function") window.hqTileLayout();
    q("[data-items]").forEach(more);
    q("[data-shrink]").forEach(shrink);
    q("[data-whole]").forEach(function (e) { if (e.scrollWidth > e.clientWidth + 1) off(e); });
    // Parts go in their order until the rest fits; a list loses items first, down to its data-keep.
    var parts = q("[data-drop]").sort(function (a, b) { return a.getAttribute("data-drop") - b.getAttribute("data-drop"); });
    var gone = [], p;
    for (p = 0; p < parts.length && misfit(); p++) {
      if (!shown(parts[p])) continue;
      if (listOf(parts[p]) && most(listOf(parts[p]), keep(listOf(parts[p])))) break;
      off(parts[p]); gone.push(parts[p]);
    }
    // What went comes back, the most wanted first, when it fits after all: as it is, or with a list giving up
    // items for it (a third of what it shows at the most, and never under its data-keep: a foot is worth a row of
    // boxes, not most of them).
    function keep(c) { return Math.max(0, +c.getAttribute("data-keep") || 0); }
    function count(c) { return kids(c).filter(function (k) { return !k.hasAttribute("data-off"); }).length; }
    var lists = q("[data-items]").filter(shown), least = lists.map(function (c) { return Math.max(keep(c), Math.ceil(count(c) * 2 / 3)); });
    for (p = gone.length - 1; p >= 0; p--) {
      on(gone[p]);
      if (listOf(gone[p]) ? most(listOf(gone[p]), keep(listOf(gone[p]))) : !misfit()) continue;
      var made = lists.some(function (c, i) { return c !== listOf(gone[p]) && shown(c) && most(c, least[i]); });
      if (!made) off(gone[p]);
    }
    q("[data-items]").forEach(function (c) { if (shown(c)) most(c, count(c)); });
    // Tidy: "+1 more" takes the room of the one it stands for, and a "+3 more" alone on a row of its own looks
    // left over: one more item goes, so the words close a full row.
    q("[data-items]").forEach(function (c) {
      var mark = null, seen = kids(c).filter(function (k) { return !k.hasAttribute("data-off"); }), last = seen[seen.length - 1];
      [].slice.call(c.children).forEach(function (k) { if (k.hasAttribute("data-more-cell")) mark = k; });
      if (!shown(c) || !mark || !last || seen.length <= Math.max(1, keep(c))) return;
      var alone = !c.classList.contains("single") && mark.getBoundingClientRect().top > last.getBoundingClientRect().top + 1;
      if (alone || kids(c).length - seen.length === 1) show(c, seen.length - 1);
    });
    q("[data-shrink]").forEach(shrink);
    q("[data-whole]").forEach(function (e) { if (shown(e) && e.scrollWidth > e.clientWidth + 1) off(e); });
    // Room to spare: nothing had to go, so the tile is marked "roomy" (its look can then show more about each
    // thing), unless that is what makes it not fit.
    if (!q("[data-off]").filter(function (e) { return !e.hasAttribute("data-whole"); }).length) {
      tile.setAttribute("data-roomy", ""); if (misfit()) tile.removeAttribute("data-roomy");
    }
    // data-clamp: words that take as many whole lines as their box has room for.
    q("[data-clamp]").forEach(function (e) {
      e.style.webkitLineClamp = "";
      var line = parseFloat(getComputedStyle(e).lineHeight) || parseFloat(getComputedStyle(e).fontSize) * 1.3;
      if (!shown(e)) return;
      // Its room: its box's height less what else is in the box (who said it, under a quote).
      var room = e.parentNode.clientHeight;
      [].slice.call(e.parentNode.children).forEach(function (k) { if (k !== e && shown(k)) room -= k.getBoundingClientRect().height + 6; });
      e.style.webkitLineClamp = Math.max(1, Math.floor((room + 1) / line));
    });
  }
  window.hqTileFit = fitTile;   // call it again after changing what the tile shows (tabs, a button)
  fitTile();
  addEventListener("resize", fitTile);
  addEventListener("load", fitTile);
})();
