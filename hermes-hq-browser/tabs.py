"""Which tab a bot worked in last, so Watch Browser opens on it.

A bot's Chrome collects tabs: every ``new_tab`` stays open, across chats and scheduled jobs. Chrome can't say which
one the bot is using (its own tab order follows activation, which moving within a tab doesn't change and which the
viewer itself changes), so the dashboard keeps one quiet DevTools connection per standing Chrome and notes when each
tab opened or went to a new address. ``recent(url)`` lists tab ids, most recently used first.

When a bot's Chrome is restarted (it froze or quit), its tabs are gone: headless Chrome keeps no session to restore.
The tracker remembers each tab's address, and when it finds a new Chrome on that port holding only empty tabs, it
opens the bot's pages again (the most recently used last, so the view lands on it). Sign-ins come back with the
profile; what was typed into a page doesn't.

The same connection listens on every tab for the boxes Chrome draws outside the page (an alert box, a "Sign in with…"
account chooser). Chrome tells only a connection that was listening when one opened, and a page with an alert box open
answers nothing else, so a phone that starts watching afterwards learns of it (``box``) and answers it (``answer``) here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger("hermes-hq-browser")
RETRY = 5.0
REOPEN = 8  # at most this many of a restarted Chrome's pages come back


class Tracker:
    def __init__(self, cdp_module):
        self.cdp_module = cdp_module
        self.used: dict[str, dict[str, float]] = {}  # Chrome (discovery root) -> tab id -> when it was last used
        self.watched: set[str] = set()
        self.pages: dict[str, dict[str, str]] = {}   # Chrome -> tab id -> address, as last seen
        self.browser: dict[str, str] = {}            # Chrome -> the browser id last connected to
        self.touched: dict[str, float] = {}          # Chrome -> when a tab last opened or moved (seen, not guessed)
        self.boxes: dict[str, dict[str, tuple]] = {}  # Chrome -> tab -> (event, params, session) of the box it has open
        self.connections: dict[str, object] = {}     # Chrome -> this tracker's DevTools connection to it
        self.lock = threading.Lock()
        self.loop = None

    def watch(self, url: str) -> None:
        """Start following a bot's Chrome (its ``browser.cdp_url`` discovery root). Cheap to call again."""
        key = root(url)
        with self.lock:
            if key in self.watched:
                return
            self.watched.add(key)
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                threading.Thread(target=self.loop.run_forever, name="hermes-hq-browser-tabs", daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._follow(key), self.loop)

    def recent(self, url: str) -> list[str]:
        with self.lock:
            used = dict(self.used.get(root(url), {}))
        return sorted(used, key=lambda tab: -used[tab])

    def box(self, url: str, tab) -> tuple | None:
        """``(event, params)`` of the box ``tab`` has open (Chrome's ``Page.javascriptDialogOpening`` or
        ``FedCm.dialogShown``), or None."""
        with self.lock:
            found = self.boxes.get(root(url), {}).get(str(tab))
        return (found[0], found[1]) if found else None

    def answer(self, url: str, tab, method: str, params: dict):
        """Answer ``tab``'s open box through this connection (the one Chrome told). A concurrent future."""
        key = root(url)
        with self.lock:
            found, cdp = self.boxes.get(key, {}).get(str(tab)), self.connections.get(key)

        async def call():
            if not found or cdp is None:
                raise self.cdp_module.CdpError("no box open")
            return await cdp.call(method, params, found[2], timeout=5)
        return asyncio.run_coroutine_threadsafe(call(), self.loop)

    def last_used(self, url: str) -> float:
        with self.lock:
            return self.touched.get(root(url), 0.0)

    def _mark(self, key: str, tab, when: float) -> None:
        if tab:
            with self.lock:
                self.used.setdefault(key, {})[str(tab)] = when
                self.touched[key] = max(self.touched.get(key, 0.0), when)

    async def _follow(self, key: str) -> None:
        while True:
            try:
                await self._session(key)
            except Exception:  # noqa: BLE001 - Chrome restarting or not up yet
                log.debug("hermes-hq-browser: tab tracking for %s paused", key, exc_info=True)
            await asyncio.sleep(RETRY)

    async def _session(self, key: str) -> None:
        listing = await asyncio.to_thread(_get, f"{key}/json/list")
        endpoint = (await asyncio.to_thread(_get, f"{key}/json/version")).get("webSocketDebuggerUrl")
        if not endpoint:
            return
        # Before this connection, Chrome's own order (newest activation first) is the best guess; anything seen from
        # now on is newer than it.
        start = time.time()
        pages = [t for t in listing if isinstance(t, dict) and t.get("type") == "page"]
        browser = endpoint.rsplit("/", 1)[-1]
        lost = self._lost(key, browser, pages)
        with self.lock:
            known = self.used.setdefault(key, {})
            for rank, tab in enumerate(pages):
                known.setdefault(str(tab.get("id")), start - 3600 - rank)
        urls: dict[str, str] = {str(t.get("id")): str(t.get("url") or "") for t in pages}
        with self.lock:
            self.pages[key] = urls
            self.browser[key] = browser
        cdp = await self.cdp_module.Cdp().connect(endpoint)
        sessions: dict[str, str] = {}  # this connection's session on a tab -> the tab

        def on_box(method, params, session):
            tab = sessions.get(session)
            if not tab:
                return
            with self.lock:
                boxes = self.boxes.setdefault(key, {})
                if method in ("Page.javascriptDialogOpening", "FedCm.dialogShown"):
                    boxes[tab] = (method, params, session)
                elif tab in boxes and boxes[tab][0].split(".")[0] == method.split(".")[0]:
                    boxes.pop(tab)  # Page.javascriptDialogClosed, FedCm.dialogClosed

        def on_event(method, params, session):
            info = params.get("targetInfo") or {}
            tab = str(info.get("targetId") or params.get("targetId") or "")
            if method == "Target.attachedToTarget" and info.get("type") == "page":
                sessions[params.get("sessionId")] = tab
                for enable in ("Page.enable", "FedCm.enable"):  # a tab with an alert box open answers these later
                    cdp.send(enable, {}, params.get("sessionId"))
            elif method == "Target.detachedFromTarget":
                gone = sessions.pop(params.get("sessionId"), None)
                with self.lock:
                    self.boxes.get(key, {}).pop(gone, None)
            elif method.startswith(("Page.javascriptDialog", "FedCm.dialog")):
                on_box(method, params, session)
            elif method == "Target.targetCreated" and info.get("type") == "page":
                if tab in urls:  # discovery replays every tab that was already open: not a use
                    return
                urls[tab] = str(info.get("url") or "")
                self._mark(key, tab, time.time())
            elif method == "Target.targetInfoChanged" and info.get("type") == "page":
                url = str(info.get("url") or "")
                if urls.get(tab) != url:
                    urls[tab] = url
                    self._mark(key, tab, time.time())
            elif method == "Target.targetDestroyed":
                urls.pop(tab, None)
                with self.lock:
                    self.used.get(key, {}).pop(tab, None)
                    self.boxes.get(key, {}).pop(tab, None)

        cdp.listeners.append(on_event)
        with self.lock:
            self.connections[key] = cdp
            self.boxes[key] = {}
        try:
            await cdp.call("Target.setDiscoverTargets", {"discover": True})
            try:  # every tab, now and later, quietly (pages only: no workers, no frames)
                await cdp.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True,
                                                        "filter": [{"type": "page", "exclude": False}, {"exclude": True}]})
            except self.cdp_module.CdpError:
                log.info("hermes-hq-browser: this Chrome at %s can't report alert boxes to a late viewer", key)
            for url in lost:
                await cdp.call("Target.createTarget", {"url": url, "background": True})
                await asyncio.sleep(0.2)
            if lost:
                log.info("hermes-hq-browser: reopened %d page(s) in the restarted Chrome at %s", len(lost), key)
            await cdp.closed.wait()
        finally:
            with self.lock:
                if self.connections.get(key) is cdp:
                    self.connections.pop(key, None)
                    self.boxes.pop(key, None)
            await cdp.close()


    def _lost(self, key: str, browser: str, pages: list) -> list[str]:
        """The pages to open again: this port now runs a different Chrome with nothing in it, and the last one had
        pages. Oldest first, at most REOPEN."""
        with self.lock:
            before, previous, used = dict(self.pages.get(key) or {}), self.browser.get(key), dict(self.used.get(key) or {})
        if not previous or previous == browser or any(not _blank(t.get("url")) for t in pages):
            return []
        kept = [tab for tab, url in before.items() if not _blank(url)]
        kept.sort(key=lambda tab: used.get(tab, 0))
        return [before[tab] for tab in kept[-REOPEN:]]


def _blank(url) -> bool:
    url = str(url or "")
    return url in ("", "about:blank") or url.startswith(("chrome://", "chrome-error://", "devtools://"))


def root(url: str) -> str:
    return str(url).rstrip("/").replace("ws://", "http://").replace("localhost", "127.0.0.1")


def _get(url: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=3) as response:  # noqa: S310 - loopback only
        return json.loads(response.read(4 * 1024 * 1024))
