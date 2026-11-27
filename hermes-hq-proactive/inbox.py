"""Where a find goes: Tasks > Ideas in the Hermes HQ app (hermes-hq-proactive 0.3.0).

0.2.0 kept everything it found on its own page, where Approve only recorded a tap. The app
already has the right place: the Ideas tab of Tasks (the hermes-hq-push add-on), where Add puts
a find on the task list and Add & Start has the bot do it. So a suggestion is handed there, and
what the person does with it comes back here on the next check:

* added      -> the suggestion counts as accepted and the loop it was about is closed (it is a task now)
* dismissed  -> counts as dismissed, and the loop is dropped so it stops coming back
* aged out   -> closed without counting for or against anything

The hand-off is one in-process call into that add-on (``ideas.offer``, there since its 0.9.90).
When the add-on is missing, older than that, or its Ideas tab is off, nothing is lost: the
suggestion stays on the Proactivity page, as before.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger("hermes-hq-proactive")

SOURCE = "Proactivity"
KEEP_DAYS_IN_IDEAS = 8   # an idea fades after a week; the suggestion behind it waits a day longer
NEEDED_API = 1
PUSH_PLUGIN_DIR = "hermes-hq-push"
_MODULES = (("hermes_hq_push_core", "push.py"), ("hermes_hq_tasks", "tasks.py"), ("hermes_hq_ideas", "ideas.py"))

# Why finds are, or are not, going to Ideas. The page shows the sentence.
STATES = {
    "ok": "What it finds goes to Tasks › Ideas in the app.",
    "setting_off": "You turned off sending finds to Tasks › Ideas, so they stay on the Proactivity page.",
    "missing": "The Hermes HQ add-on is not installed for this bot, so finds stay on the Proactivity page.",
    "old": "The Hermes HQ add-on is older than 0.9.90, so finds stay on the Proactivity page until it is updated.",
    "off": "Ideas is turned off in the app's Tasks page, so finds stay on the Proactivity page.",
}

# Who does it, as Ideas words it, for a find that is not about a tracked item. (For a tracked
# item it follows whose it is: the bot's own promise is the bot's; yours is done together.)
_BOT_CAN_DO = frozenset({"prepared_draft", "stale_check", "next_step", "pattern_automation", "pre_event"})


def _load_push_modules() -> Optional[Dict[str, Any]]:
    """The push add-on's modules: the copies already loaded in this process, else from its folder
    beside this plugin. None when it is not installed here."""
    found: Dict[str, Any] = {}
    for name, filename in _MODULES:
        mod = sys.modules.get(name)
        if mod is None:
            path = Path(__file__).resolve().parent.parent / PUSH_PLUGIN_DIR / filename
            if not path.is_file():
                return None
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[name] = mod
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop(name, None)
                logger.debug("hermes-hq-proactive: could not load %s", path, exc_info=True)
                return None
        found[name] = mod
    return found


class Inbox:
    """The Ideas tab, for one bot. Every method is safe to call when the add-on is not there."""

    def __init__(self, bot: str = "default", *, modules: Optional[Mapping[str, Any]] = None, tasks_store: Any = None):
        self.bot = (bot or "default").strip() or "default"
        self._modules = dict(modules) if modules is not None else None
        self._tasks_store = tasks_store

    def _open(self):
        """``(ideas module, tasks store)``, or a state word saying why not."""
        mods = self._modules if self._modules is not None else _load_push_modules()
        if not mods:
            return "missing"
        ideas = mods["hermes_hq_ideas"]
        if int(getattr(ideas, "OUTSIDE_API", 0) or 0) < NEEDED_API or not callable(getattr(ideas, "offer", None)):
            return "old"
        store = self._tasks_store
        if store is None:
            store = mods["hermes_hq_tasks"].Store(mods["hermes_hq_push_core"].data_dir() / "tasks.db")
        return ideas, store

    def state(self) -> str:
        try:
            opened = self._open()
            if isinstance(opened, str):
                return opened
            _, store = opened
            return "ok" if (store.enabled() and store.get_settings().get("ideas")) else "off"
        except Exception:
            logger.debug("hermes-hq-proactive: Ideas state unknown", exc_info=True)
            return "missing"

    def offer(self, item: Mapping[str, Any]) -> Dict[str, Any]:
        """Hand one find to Ideas. ``{"idea": row}`` or ``{"skipped": why[, "idea": twin]}``."""
        opened = self._open()
        if isinstance(opened, str):
            return {"skipped": opened}
        ideas, store = opened
        return ideas.offer(store, dict(item), self.bot)

    def get(self, idea_id: str) -> Optional[Dict[str, Any]]:
        opened = self._open()
        if isinstance(opened, str):
            raise LookupError(opened)
        ideas, store = opened
        return ideas.get(store, idea_id)

    def dismiss(self, idea_id: str) -> bool:
        opened = self._open()
        if isinstance(opened, str):
            return False
        ideas, store = opened
        try:
            ideas.dismiss(store, idea_id)
            return True
        except Exception:
            return False


def describe(store, inbox: Optional[Inbox]) -> Dict[str, Any]:
    """Where finds are going right now, for the page: ``{"state", "to_ideas", "note"}``."""
    if not store.get_settings().get("ideas_inbox", True):
        state = "setting_off"
    else:
        state = inbox.state() if inbox is not None else "missing"
    return {"state": state, "to_ideas": state == "ok", "note": STATES.get(state, STATES["missing"])}


def idea_title(offer: Mapping[str, Any], loop: Optional[Mapping[str, Any]]) -> str:
    """What the idea is called. The loop's own words when there is one, so the same loop is the
    same idea whichever check found it ("Past due: X" and "Due soon: X" are both just X)."""
    if loop is None:
        return str(offer.get("title") or "").strip()
    title = str(loop.get("title") or "").strip()
    if loop.get("kind") == "waiting_on" and title.lower().startswith("waiting on "):
        return f"Follow up on {title[len('waiting on '):]}"
    return title


def idea_fields(store, offer: Mapping[str, Any]) -> Dict[str, Any]:
    loop = store.get_loop(str(offer["loop_id"])) if offer.get("loop_id") else None
    why = str(offer.get("summary") or "").strip()
    if int(offer.get("tier") or 0) >= 2:
        why = f"Time-sensitive. {why}"
    if loop is not None:
        bot_can = loop.get("owner") == "bot"
    else:
        bot_can = offer.get("category") in _BOT_CAN_DO
    return {
        "title": idea_title(offer, loop),
        "why": why,
        "source": SOURCE,
        "who": "bot" if bot_can else "together",
        "help": str(offer.get("suggested_action") or "").strip(),
    }


def _close_loop(store, offer: Mapping[str, Any], *, status: str, by: str) -> None:
    if offer.get("loop_id"):
        loop = store.get_loop(str(offer["loop_id"]))
        if loop is not None and loop.get("status") in ("open", "snoozed"):
            store.update_loop(str(offer["loop_id"]), status=status, resolved_by=by)


def deliver(store, offer: Mapping[str, Any], inbox: Optional[Inbox]) -> str:
    """Show one new suggestion: in Ideas when it can go there, on the page otherwise.
    Returns ``ideas``, ``page`` or ``closed`` (it turned out to be on the list or answered already)."""
    offer_id = str(offer["id"])
    to_ideas = (
        inbox is not None
        and int(offer.get("tier") or 0) >= 1   # a quiet one is only kept on the page and in the brief
        and bool(store.get_settings().get("ideas_inbox", True))
    )
    if not to_ideas:
        store.mark_offer_delivered(offer_id, delivered_to="page")
        return "page"
    try:
        res = inbox.offer(idea_fields(store, offer))
    except Exception:
        logger.debug("hermes-hq-proactive: Ideas hand-off failed", exc_info=True)
        res = {"skipped": "missing"}

    idea = res.get("idea") if isinstance(res.get("idea"), Mapping) else None
    skipped = res.get("skipped")
    if idea is not None and not skipped:
        store.mark_offer_delivered(offer_id, delivered_to="ideas", idea_id=str(idea["id"]), keep_days=KEEP_DAYS_IN_IDEAS)
        return "ideas"
    if skipped == "offered" and idea is not None:
        state = idea.get("state")
        if state == "new":   # the same idea is already waiting there: ride along with it
            store.mark_offer_delivered(offer_id, delivered_to="ideas", idea_id=str(idea["id"]), keep_days=KEEP_DAYS_IN_IDEAS)
            return "ideas"
        if state == "added":
            store.close_offer_quietly(offer_id, status="accepted", note=f"Already a task: {offer['title']}")
            _close_loop(store, offer, status="resolved", by="task")
        elif state == "dismissed":
            store.close_offer_quietly(offer_id, status="dismissed", note=f"Dismissed in Ideas before: {offer['title']}")
            _close_loop(store, offer, status="dropped", by="idea_dismissed")
        else:
            store.close_offer_quietly(offer_id, status="expired", note=f"Offered in Ideas before: {offer['title']}")
        return "closed"
    if skipped == "listed":
        store.close_offer_quietly(offer_id, status="accepted", note=f"Already on the task list: {offer['title']}")
        _close_loop(store, offer, status="resolved", by="task")
        return "closed"
    store.mark_offer_delivered(offer_id, delivered_to="page")
    return "page"


def sync(store, inbox: Optional[Inbox]) -> Dict[str, int]:
    """Bring back what the person did in Ideas since the last check."""
    out = {"added": 0, "dismissed": 0, "expired": 0}
    if inbox is None:
        return out
    waiting = store.list_offers_in_ideas()
    if not waiting:
        return out
    for offer in waiting:
        try:
            idea = inbox.get(str(offer["idea_id"]))
        except Exception:
            return out   # the add-on is not reachable right now: ask again next check
        state = idea.get("state") if idea else None
        if state == "new":
            continue
        if state == "added":
            store.respond_to_offer(offer["id"], "accept", actor="ideas", loop_outcome="task")
            out["added"] += 1
        elif state == "dismissed":
            store.respond_to_offer(offer["id"], "dismiss", actor="ideas", loop_outcome="idea_dismissed")
            out["dismissed"] += 1
        else:   # aged out, or gone
            store.close_offer_quietly(offer["id"], status="expired", note=f"Aged out in Ideas untouched: {offer['title']}")
            out["expired"] += 1
    return out


def withdraw(store, offer: Mapping[str, Any], inbox: Optional[Inbox]) -> None:
    """The person dismissed it on the Proactivity page: take the idea out of Ideas too."""
    if inbox is not None and offer.get("delivered_to") == "ideas" and offer.get("idea_id"):
        inbox.dismiss(str(offer["idea_id"]))
