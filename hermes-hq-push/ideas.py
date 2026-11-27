"""Task ideas (0.9.39+, multi-bot & source controls in 0.9.86): tasks bots suggest you could start, from the exact
sources you choose.

- Off until the user turns it on (Tasks settings ``ideas``).
- One or more bots (``ideas_bots``, falling back to ``[ideas_bot]``) each run a daily scheduled job, "Task ideas", at
  ``ideas_at`` (``reconcile_all``). In a multi-bot sweep, the primary bot scans recent chats (filtered by ``chat_bots``)
  plus its own enabled sources, while secondary bots scan their own memory/files/enabled connected apps — merging and
  deduplicating into one Suggested list.
- Built-in source toggles (``chats``, ``memory``, ``files``, ``web``) and auto-discovered connected apps
  (``connectors``: direct MCP servers and Composio connected apps, opt-in by default) are BOTH included explicitly in
  the run's instructions AND hard-enforced in ``pre_tool_call`` (``guard``). Mutating/external-write actions and task
  writes are always refused during the run.
- Plain-English Focus & Skip rules (``ideas_note``) steer what to look for or ignore.
- An idea is never a task until the user adds it (``add``, ``POST /tasks/ideas/{id}/add``). Added and dismissed ideas
  are kept ``KEEP_DAYS`` so the same idea isn't offered again; untouched ones fade after ``tasks.IDEAS_FRESH_DAYS``.
- No push, no Lock Screen card, and nothing here logs an idea's words, API keys, or the chats it read.

Shares tasks.db with tasks.py (table ``task_ideas``). Hermes imports are lazy and function-local, so this file imports
cleanly outside a Hermes checkout.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable

if "hermes_hq_tasks" in sys.modules:
    tasks = sys.modules["hermes_hq_tasks"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_tasks", Path(__file__).with_name("tasks.py"))
    tasks = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_tasks"] = tasks
    _spec.loader.exec_module(tasks)

log = logging.getLogger("hermes-hq-push")

JOB_NAME = "Task ideas"
JOB_MARK = "tasks_ideas"   # in every version's Task ideas instruction: how an older, unrecorded one is told
# In the Task ideas run these are always refused: the run suggests, and the user adds.
TASK_WRITES = frozenset({"tasks_add", "tasks_update", "tasks_next_prep", "tasks_offer", "goals_offer", "goals_update"})
ALWAYS_ALLOWED = frozenset({"tasks_ideas", "tasks_list", "goals_list"})
CHAT_TOOLS = frozenset({"session_search"})
MEMORY_TOOLS = frozenset({"memory"})
MEMORY_WRITES = frozenset({"add", "replace", "remove", "delete", "clear", "update", "write", "edit"})
FILE_READ_TOOLS = frozenset({"read_file", "search_files", "terminal", "execute_code"})
FILE_WRITE_TOOLS = frozenset({"write_file", "patch"})
WEB_TOOLS = frozenset({"web_search", "web_extract"})
COMPOSIO_DISCOVERY_TOOLS = frozenset({"COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS"})
COMPOSIO_BLOCKED_TOOLS = frozenset({"COMPOSIO_MANAGE_CONNECTIONS", "COMPOSIO_WAIT_FOR_CONNECTION"})
_WORKBENCH_CALL = re.compile(r"""run_composio_tool\(\s*['"]([A-Za-z0-9_]+)['"]""")
_MUTATING_ACTION = re.compile(
    r"(?:^|_|-)(send|post|reply|publish|tweet|book|buy|purchase|pay|checkout|order|delete|remove|cancel|"
    r"create|update|write|edit|patch|insert|append|modify|archive|trash|move|put|set|add|assign|invite|"
    r"share|grant|revoke|upload|submit|approve|reject|dismiss|close|reopen|merge|push)(?:$|_|-)",
    re.I,
)
_OUTWARD_TOOL = re.compile(
    r"send|post|reply|publish|tweet|book|buy|purchase|pay|checkout|order|delete|remove|cancel|"
    r"write|edit|patch|create|update",
    re.I,
)
_MCP_DOUBLE = re.compile(r"^mcp__([^_][^_]*?)__(.+)$")
_MCP_SINGLE = re.compile(r"^mcp_([A-Za-z0-9-]+)_(.+)$")
_COMPOSIO_SESSION = re.compile(r"/tool_router/(?:v\d+/)?([^/?#]+)/mcp")

WHO = ("me", "together", "bot")
KEEP_DAYS = 60
NEW_CAP = 12             # at most this many new ideas wait on the page; the oldest fade first
RUN_GAP = 15 * 60        # Look Now: once every 15 minutes
LOOKING_FOR = 10 * 60    # the page says "Looking…" this long after Look Now, unless the run answers sooner
CHAT_DAYS = 3
CHATS_PER_BOT = 8
LINES_PER_CHAT = 6
LINE_MAX = 280
CONTEXT_BUDGET = 9000    # characters of chat lines handed to the run
TASKS_SHOWN = 40
OFFERED_SHOWN = 60
TITLE_MAX = 120
WHY_MAX = 240
SOURCE_MAX = 60
HELP_MAX = 160
COLS = ("id", "created_at", "bot", "title", "why", "source", "who", "help", "state", "task_id", "decided_at")

IDEA_RULE = (
    "An idea is a task the user would plausibly want on their list now: a follow-up they promised, something "
    "time-bound coming up, a loose end left in a chat, email, calendar or doc, or a chore they mentioned. Fewer is "
    "better; none is fine when nothing is worth their time. Each idea: title (a to-do in a few words, like \"Renew "
    "the car registration\"), why (one line: what you saw and why now, with no passwords, codes or account numbers), "
    "source (where, in a few words: \"Chat with Hermes, Tue\", \"Gmail · Yesterday\", \"Notion · Inbox\"), who "
    "('me' if it's theirs to do, 'together' if a bot could do part, 'bot' if a bot could do it all), help (when a "
    "bot can help: what it would do, in one short line) and bot (the bot best placed to help, from bots). Skip "
    "anything already in open_tasks or offered_before, in any words.")

JOB_PROMPT = (
    "Find task ideas for the user. First call tasks_ideas with action 'context'. Read it carefully: actively check "
    "every source listed in context.sources_to_check (including any enabled connected apps like email, calendar, or "
    "Notion) before deciding, skip everything in context.sources_to_skip, and follow context.instructions if given. "
    "You're finding ideas, not doing them: don't start on one, and don't send, book, buy, edit, create or delete "
    "anything; the user picks what happens. Then call tasks_ideas with action 'suggest' and at most the number "
    "context gives as max (none is fine). End the turn with one short line.")

_RUN_ID = re.compile(r"^cron_(.+)_\d{8}_\d{6}$")
_known_runs: dict[str, bool] = {}
_composio_cache: dict[str, tuple[float, list[dict]]] = {}
_COMPOSIO_CACHE_TTL = 180.0
_COMPOSIO_BASE = "https://backend.composio.dev"

_PRETTY_NAMES = {
    "gmail": "Gmail",
    "googlecalendar": "Google Calendar",
    "google_calendar": "Google Calendar",
    "google-calendar": "Google Calendar",
    "googledrive": "Google Drive",
    "google_drive": "Google Drive",
    "googledocs": "Google Docs",
    "googlesheets": "Google Sheets",
    "notion": "Notion",
    "slack": "Slack",
    "github": "GitHub",
    "linear": "Linear",
    "jira": "Jira",
    "asana": "Asana",
    "trello": "Trello",
    "airtable": "Airtable",
    "hubspot": "HubSpot",
    "salesforce": "Salesforce",
    "outlook": "Outlook",
    "microsoftteams": "Microsoft Teams",
    "todoist": "Todoist",
    "dropbox": "Dropbox",
    "stripe": "Stripe",
    "figma": "Figma",
    "discord": "Discord",
    "telegram": "Telegram",
}


class IdeaError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# Settings helpers: active bots (multi-bot) and normalized sources (opt-in connectors)
# ---------------------------------------------------------------------------

def active_bots(settings: dict) -> list[str]:
    """The bots that run Task ideas: ``ideas_bots`` when set, else ``[ideas_bot]`` (default: ['default'])."""
    bots = settings.get("ideas_bots")
    if isinstance(bots, list) and bots:
        out, seen = [], set()
        for b in bots:
            if isinstance(b, str) and b and b not in seen:
                seen.add(b)
                out.append(b)
        if out:
            return out
    one = settings.get("ideas_bot") or "default"
    return [one]


def sources_of(settings: dict) -> dict:
    """Normalized ``ideas_sources`` dict with safe defaults (built-in chats+memory on; files, web, connectors off)."""
    return tasks.Store._clean_ideas_sources(settings.get("ideas_sources"))


def _pretty_connector_name(slug: str) -> str:
    low = slug.strip().lower()
    if low in _PRETTY_NAMES:
        return _PRETTY_NAMES[low]
    compact = low.replace("-", "").replace("_", "")
    if compact in _PRETTY_NAMES:
        return _PRETTY_NAMES[compact]
    parts = re.split(r"[-_]+", slug.strip())
    return " ".join(p.capitalize() for p in parts if p) or slug


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


# ---------------------------------------------------------------------------
# Connector discovery across bot profiles (direct MCP servers + Composio apps)
# ---------------------------------------------------------------------------

def _load_yaml_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        import yaml
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    # Minimal indentation-based fallback for mcp_servers when PyYAML isn't installed in test envs.
    servers: dict[str, dict] = {}
    in_mcp = False
    current = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            in_mcp = line == "mcp_servers:"
            current = None
            continue
        if not in_mcp:
            continue
        if indent == 2 and line.endswith(":"):
            current = line[:-1].strip().strip("\"'")
            servers[current] = {}
        elif indent >= 4 and current and ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().strip("\"'")
            v = v.strip().strip("\"'")
            if v.lower() == "false":
                servers[current][k] = False
            elif v.lower() == "true":
                servers[current][k] = True
            else:
                servers[current][k] = v
    return {"mcp_servers": servers} if servers else {}


def _read_env_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("export "):
                s = s[7:].strip()
            if s.startswith(key + "="):
                val = s[len(key) + 1:].strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                return val
    except OSError:
        pass
    return ""


def _composio_api_key(entry: dict, home: Path) -> str:
    headers = entry.get("headers") if isinstance(entry, dict) else None
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() == "x-api-key" and isinstance(v, str) and v.strip():
                return v.strip()
    for env_file in (home / ".env", Path.home() / ".hermes" / ".env"):
        val = _read_env_value(env_file, "COMPOSIO_API_KEY")
        if val:
            return val
    return ""


def _fetch_composio_toolkits(session_id: str, api_key: str) -> list[dict]:
    """Connected Composio toolkits for a tool_router session, cached briefly in memory. Never logs keys."""
    if not session_id or not api_key:
        return []
    now = time.monotonic()
    cached = _composio_cache.get(session_id)
    if cached and now - cached[0] < _COMPOSIO_CACHE_TTL:
        return list(cached[1])
    url = f"{_COMPOSIO_BASE}/api/v3.1/tool_router/session/{urllib.parse.quote(session_id, safe='')}/toolkits?is_connected=true&limit=100"
    req = urllib.request.Request(url, headers={"x-api-key": api_key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return list(cached[1]) if cached else []
    items = payload.get("items") if isinstance(payload, dict) else None
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip().lower()
        if not slug:
            continue
        name = str(item.get("name") or "").strip() or _pretty_connector_name(slug)
        out.append({"slug": slug, "name": name})
    _composio_cache[session_id] = (now, out)
    return list(out)


def discover_connectors(profiles=None, home_for: Callable | None = None,
                        fetch_composio: Callable | None = None) -> list[dict]:
    """Auto-discovers enabled direct MCP servers and connected Composio apps across ``profiles``."""
    names = list(profiles if profiles is not None else _bot_names())
    resolve_home = home_for or tasks.bot_home
    fetch = fetch_composio or _fetch_composio_toolkits
    by_id: dict[str, dict] = {}

    def record(cid: str, name: str, kind: str, bot: str) -> None:
        entry = by_id.get(cid)
        if entry is None:
            by_id[cid] = {"id": cid, "name": name, "kind": kind, "bots": [bot]}
        elif bot not in entry["bots"]:
            entry["bots"].append(bot)

    for profile in names:
        try:
            home = resolve_home(profile)
        except Exception:
            home = None
        if home is None:
            continue
        home_path = Path(home)
        cfg = _load_yaml_dict(home_path / "config.yaml")
        mcps = cfg.get("mcp_servers")
        if not isinstance(mcps, dict):
            continue
        for server, conf in mcps.items():
            if not isinstance(server, str):
                continue
            low = server.strip().lower()
            if not low or low.startswith("hermes-hq-"):
                continue
            if isinstance(conf, dict) and conf.get("enabled") is False:
                continue
            if low == "composio":
                url = str(conf.get("url") or "") if isinstance(conf, dict) else ""
                match = _COMPOSIO_SESSION.search(url)
                if match:
                    key = _composio_api_key(conf, home_path)
                    for tk in fetch(match.group(1), key):
                        slug = str(tk.get("slug") or "").strip().lower()
                        if slug:
                            cid = f"composio:{slug}"
                            if tasks._CONNECTOR_ID.match(cid):
                                record(cid, str(tk.get("name") or _pretty_connector_name(slug)), "composio", profile)
                continue
            cid = f"mcp:{low}"
            if tasks._CONNECTOR_ID.match(cid):
                record(cid, _pretty_connector_name(server), "mcp", profile)

    return sorted(by_id.values(), key=lambda item: (item["name"].lower(), item["id"]))


def available_sources(store=None, profiles=None, home_for: Callable | None = None,
                      fetch_composio: Callable | None = None) -> dict:
    """Returns auto-discovered connectors and bot profiles for the app's Where to Look settings."""
    names = list(profiles if profiles is not None else _bot_names())
    connectors = discover_connectors(profiles=names, home_for=home_for, fetch_composio=fetch_composio)
    return {"connectors": connectors, "bots": names}


# ---------------------------------------------------------------------------
# Storage: rows in tasks.db's task_ideas table; meta keys ideas_ran, ideas_asked, ideas_job.
# ---------------------------------------------------------------------------

def _ago(store, seconds: float = 0.0) -> str:
    return tasks._iso(lambda: store.clock() - seconds)


def _row(row) -> dict:
    return dict(zip(COLS, row))


def _meta(store, key: str) -> dict:
    with closing(store._db()) as db:
        row = db.execute("select value from meta where key=?", (key,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _put_meta(db, key: str, value: dict) -> None:
    db.execute("insert into meta (key, value) values (?, ?) on conflict(key) do update set value=excluded.value",
               (key, json.dumps(value)))


def _tidy(db, store) -> None:
    """New ideas past their week fade, anything past KEEP_DAYS goes, and at most NEW_CAP stay new."""
    now = _ago(store)
    db.execute("update task_ideas set state='expired', decided_at=? where state='new' and created_at<?",
               (now, _ago(store, tasks.IDEAS_FRESH_DAYS * 86400)))
    db.execute("delete from task_ideas where created_at<?", (_ago(store, KEEP_DAYS * 86400),))
    extra = db.execute("select id from task_ideas where state='new' order by created_at desc limit -1 offset ?",
                       (NEW_CAP,)).fetchall()
    for (idea_id,) in extra:
        db.execute("update task_ideas set state='expired', decided_at=? where id=?", (now, idea_id))


def get(store, idea_id: str) -> dict | None:
    with closing(store._db()) as db:
        row = db.execute(f"select {','.join(COLS)} from task_ideas where id=?", (idea_id,)).fetchone()
    return _row(row) if row else None


def page(store, *, include_sources: bool = True) -> dict:
    """``GET /tasks/ideas``: the new ideas (newest first), whether Ideas is on, active bots, sources, when it last
    answered, and whether a Look Now is still under way."""
    settings = store.get_settings()
    with closing(store._write_db()) as db, db:
        _tidy(db, store)
        rows = db.execute(f"select {','.join(COLS)} from task_ideas where state='new' order by created_at desc")
        ideas_list = [_row(r) for r in rows.fetchall()]
    ran, asked = _meta(store, "ideas_ran"), _meta(store, "ideas_asked")
    asked_at = asked.get("at") or ""
    looking = bool(asked_at) and asked_at > (ran.get("at") or "") and asked_at >= _ago(store, LOOKING_FOR)
    bots = active_bots(settings)
    out = {
        "on": bool(settings.get("ideas")) and store.enabled(),
        "bot": bots[0],
        "bots": bots,
        "at": settings["ideas_at"],
        "max": settings["ideas_max"],
        "sources": sources_of(settings),
        "note": settings.get("ideas_note", ""),
        "ideas": ideas_list,
        "ran_at": ran.get("at"),
        "looking": looking,
    }
    if include_sources:
        out["available_sources"] = available_sources(store)
    return out


def _decide(store, idea_id: str, state: str, task_id: str | None = None, *, only_from=None) -> dict:
    with closing(store._write_db()) as db, db:
        row = db.execute("select state from task_ideas where id=?", (idea_id,)).fetchone()
        if row is None:
            raise IdeaError(404, "No such idea.")
        if only_from and row[0] not in only_from:
            raise IdeaError(409, "That idea was already decided.")
        db.execute("update task_ideas set state=?, task_id=coalesce(?, task_id), decided_at=? where id=?",
                   (state, task_id, None if state == "new" else _ago(store), idea_id))
        store._bump_rev(db)
    return get(store, idea_id)


def dismiss(store, idea_id: str) -> dict:
    return _decide(store, idea_id, "dismissed", only_from=("new", "expired"))


def restore(store, idea_id: str) -> dict:
    """Undo a dismiss: the idea is new again (it still fades a week after it was first offered)."""
    return _decide(store, idea_id, "new", only_from=("dismissed",))


def add(store, idea_id: str, start: bool = False) -> dict:
    """The idea becomes a task: its title, the why (and how a bot can help) as notes, and for a bot's part ``prep``
    when ``start`` (the route then starts that bot's Task prep). An open task that's the same thing in other words
    is used instead of a second one. ``{idea, task, already}``."""
    idea = get(store, idea_id)
    if idea is None:
        raise IdeaError(404, "No such idea.")
    if idea["state"] == "added" and idea["task_id"] and (task := store.get(idea["task_id"])):
        return {"idea": idea, "task": task, "already": True}
    twin = store.find_open_twin(idea["title"])
    if twin is not None:
        task = twin
    else:
        who = idea["who"] if idea["who"] in WHO else "me"
        notes = "\n\n".join(part for part in (idea["why"], idea["help"]) if part)
        fields = {"title": idea["title"], "notes": notes, "who": who, "bot": idea["bot"] if who != "me" else None,
                  "prep": bool(start) and who in ("bot", "together")}
        task = store.create(fields, created_by="you", source={"kind": "idea", "idea": idea_id, "profile": idea["bot"]})
    return {"idea": _decide(store, idea_id, "added", task["id"]), "task": task, "already": twin is not None}


def suggest(store, items, by_bot: str, bots=None) -> dict:
    """The run's ideas: at most ``ideas_max``, none that's already a task (open, or done lately) or was offered
    before in other words. Stamps the run's answer (``ran_at``) even when it brings none."""
    if not isinstance(items, list):
        return {"error": "ideas must be a list (it can be empty)."}
    cap = store.get_settings().get("ideas_max", 3)
    names = set(bots if bots is not None else _bot_names()) | {by_bot}
    saved, skipped = [], []
    with closing(store._write_db()) as db, db:
        _tidy(db, store)
        offered = [r[0] for r in db.execute("select title from task_ideas")]
        listed = [r[0] for r in db.execute("select title from tasks where status!='done' or updated_at>=?",
                                           (_ago(store, KEEP_DAYS * 86400),))]
        for item in items:
            if not isinstance(item, dict):
                continue
            title = _mask(tasks._text(item.get("title"), TITLE_MAX))
            if not title:
                continue
            if len(saved) >= cap:
                skipped.append({"title": title, "why": f"at most {cap} a day"})
            elif any(tasks.same_task(title, other) for other in listed):
                skipped.append({"title": title, "why": "already on their list"})
            elif any(tasks.same_task(title, other) for other in offered):
                skipped.append({"title": title, "why": "offered before"})
            else:
                bot = tasks._text(item.get("bot"), 64)
                row = {"id": uuid.uuid4().hex[:12], "created_at": _ago(store), "bot": bot if bot in names else by_bot,
                       "title": title, "why": _mask(tasks._text(item.get("why"), WHY_MAX)),
                       "source": _mask(tasks._text(item.get("source"), SOURCE_MAX)),
                       "who": item.get("who") if item.get("who") in WHO else "me",
                       "help": _mask(tasks._text(item.get("help"), HELP_MAX)), "state": "new"}
                db.execute(f"insert into task_ideas ({','.join(row)}) values ({','.join('?' * len(row))})",
                           list(row.values()))
                offered.append(title)
                saved.append(row)
        _put_meta(db, "ideas_ran", {"at": _ago(store), "bot": by_bot, "saved": len(saved)})
        store._bump_rev(db)
    return {"saved": len(saved), "skipped": skipped,
            "note": "Saved on their Ideas page. Nothing happens until they add one. End the turn with one short line."}


def _source_directives(settings: dict, profile: str, connectors: list[dict]) -> tuple[list[str], list[str]]:
    """Builds explicit check / skip source lists for ``profile``'s Task ideas run."""
    src = sources_of(settings)
    participants = active_bots(settings)
    is_primary = (profile == participants[0]) if participants else True
    check, skip = [], []

    if src["chats"]:
        if is_primary:
            if src["chat_bots"]:
                check.append(f"Recent chats (from {', '.join(src['chat_bots'])}, provided in recent_chats)")
            else:
                check.append("Recent chats (provided in recent_chats)")
        else:
            skip.append("Recent chats (already covered by the primary bot in this sweep)")
    else:
        skip.append("Recent chats and session_search")

    if src["memory"]:
        check.append("Memory (ongoing projects and commitments)")
    else:
        skip.append("Memory")

    if src["files"]:
        check.append("Local files and notes (read-only)")
    else:
        skip.append("Local files, notes, and terminal")

    if src["web"]:
        check.append("Web search")
    else:
        skip.append("Web search and browsing")

    enabled = set(src["connectors"])
    mine = [c for c in connectors if profile in (c.get("bots") or [])]
    for conn in mine:
        label = f"{conn['name']} ({conn['kind'].upper() if conn['kind'] == 'mcp' else 'Connected app'})"
        if conn["id"] in enabled:
            check.append(label)
        else:
            skip.append(label)
    if not any(c["id"] in enabled for c in mine):
        for cid in sorted(enabled):
            if not any(c["id"] == cid for c in connectors):
                kind, _, slug = cid.partition(":")
                check.append(f"{_pretty_connector_name(slug)} ({kind.upper() if kind == 'mcp' else 'Connected app'})")

    return check, skip


def context(store, profile: str, bots=None, chats=None, connectors=None) -> dict:
    """What the run reads first: today, open tasks, previously offered ideas, source rules, Focus & Skip instructions,
    and (when enabled for this bot) recent chats."""
    settings, now = store.get_settings(), store.clock()
    names = list(bots if bots is not None else _bot_names())
    src = sources_of(settings)
    participants = active_bots(settings)
    is_primary = (profile == participants[0]) if participants else True
    keep = _ago(store, KEEP_DAYS * 86400)
    open_tasks = [{k: t[k] for k in ("title", "who", "bot", "due", "status") if t.get(k)}
                  for t in store.list_all() if t["status"] != "done"][-TASKS_SHOWN:]
    with closing(store._db()) as db:
        offered = [{"title": r[0], "state": r[1]} for r in db.execute(
            "select title, state from task_ideas where created_at>=? order by created_at desc limit ?",
            (keep, OFFERED_SHOWN))]

    if not src["chats"] or (len(participants) > 1 and not is_primary):
        chosen_chats = []
    elif chats is not None:
        if src["chat_bots"]:
            allowed = set(src["chat_bots"])
            chosen_chats = [c for c in chats if c.get("bot") in allowed]
        else:
            chosen_chats = chats
    else:
        chat_profiles = [b for b in names if b in set(src["chat_bots"])] if src["chat_bots"] else names
        chosen_chats = recent_chats(now, chat_profiles)

    discovered = connectors if connectors is not None else discover_connectors(profiles=names)
    check, skip = _source_directives(settings, profile, discovered)
    out = {
        "today": tasks.today_line(now),
        "max": settings.get("ideas_max", 3),
        "you_are": profile,
        "primary_bot": is_primary,
        "bots": names,
        "open_tasks": open_tasks,
        "offered_before": offered,
        "recent_chats": chosen_chats,
        "sources_to_check": check,
        "sources_to_skip": skip,
        "how": IDEA_RULE,
    }
    note = (settings.get("ideas_note") or "").strip()
    if note:
        out["instructions"] = note
    return out


# ---------------------------------------------------------------------------
# Recent chats: what the user typed lately, in every bot's own sessions (Hermes's SessionDB), newest first, clipped.
# Job runs, hidden chats (a task's own chat) and helpers' sessions are left out; so is anything nobody typed.
# ---------------------------------------------------------------------------

_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{32,}")
_SAID = r"(\s*(?:is\s*[:=]?|[:=])\s*)"   # "is", ":" or "=" after the word, then the value
_SAID_SECRET = re.compile(r"\b(password|passcode|passwd|passphrase)" + _SAID + r"\S+", re.IGNORECASE)
_SAID_CODE = re.compile(r"\b(pin|otp|code)" + _SAID + r"(?=\S*\d)\S+", re.IGNORECASE)   # only with a digit in it


def _mask(text: str) -> str:
    """Keys and tokens (long runs of letters and digits), a said password, and a said PIN or code with digits in it
    become [hidden]."""
    if not text:
        return text

    def token(match):
        value = match.group(0)
        looks_secret = re.search(r"\d", value) and re.search(r"[A-Za-z]", value) and value.count("-") < 4
        return "[hidden]" if looks_secret else value

    text = _LONG_TOKEN.sub(token, text)
    for said in (_SAID_SECRET, _SAID_CODE):
        text = said.sub(lambda m: m.group(1) + m.group(2) + "[hidden]", text)
    return text


def _bot_names() -> list[str]:
    try:
        from hermes_cli.profiles import list_profile_names
        return list_profile_names() or ["default"]
    except Exception:
        return ["default"]


@contextmanager
def _session_db(profile: str):
    home = tasks.bot_home(profile)
    path = home / "state.db" if home is not None else None
    if path is None or not path.exists():
        yield None
        return
    from hermes_state_registry import acquire, release_or_close
    db = acquire(path)
    try:
        yield db
    finally:
        release_or_close(db)


def _said(message) -> str:
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    text = message.get("content")
    if isinstance(text, list):
        text = "\n".join(str(part.get("text") or "") for part in text if isinstance(part, dict))
    if not isinstance(text, str) or not text.strip() or tasks._not_typed(text):
        return ""
    return tasks._clip(_mask(" ".join(text.split())), LINE_MAX)


def bot_chats(db, profile: str, now: float) -> list[dict]:
    """One bot's chats active in the last CHAT_DAYS, each with the user's last few lines."""
    found = []
    for row in db.list_sessions_rich(limit=CHATS_PER_BOT, order_by_last_active=True, exclude_sources=["cron"]):
        last = float(row.get("last_active") or row.get("started_at") or 0)
        if last < now - CHAT_DAYS * 86400:
            continue
        lines = [line for line in map(_said, db.get_messages(row["id"], latest=True, limit=40)) if line]
        if lines:
            found.append({"bot": profile, "chat": tasks._clip(str(row.get("title") or ""), 80),
                          "when": tasks._iso(lambda: last), "you_said": lines[-LINES_PER_CHAT:], "_last": last})
    return found


def recent_chats(now: float, profiles, open_db: Callable | None = None) -> list[dict]:
    """Every bot's recent chats, newest first, until CONTEXT_BUDGET characters. ``open_db(profile)`` is for tests: a
    context manager giving that bot's SessionDB (or ``None``)."""
    found = []
    for profile in profiles:
        try:
            with (open_db or _session_db)(profile) as db:
                if db is not None:
                    found.extend(bot_chats(db, profile, now))
        except Exception:
            log.debug("hermes-hq-push: ideas left one bot's chats out")
    found.sort(key=lambda chat: chat["_last"], reverse=True)
    out, used = [], 0
    for chat in found:
        size = len(chat["chat"]) + sum(len(line) for line in chat["you_said"]) + 40
        if used + size > CONTEXT_BUDGET:
            break
        used += size
        out.append({k: v for k, v in chat.items() if k != "_last"})
    return out


# ---------------------------------------------------------------------------
# The "Task ideas" jobs: daily in each participating bot's schedule while Ideas is on; nowhere else.
# ---------------------------------------------------------------------------

def _cron_expr(at: str) -> str:
    hours, minutes = at.split(":")
    return f"{int(minutes)} {int(hours)} * * *"


def ensure_job(at: str, store, profile: str) -> str | None:
    """Create ``profile``'s "Task ideas" in the active schedule (or bring its time, words and tool list up to
    date). Its id. Known by the id recorded when it was made, like the Task prep job (tasks.own_job)."""
    from cron import jobs as cron_jobs
    expr = _cron_expr(at)
    job = tasks.own_job(cron_jobs, store, "ideas", profile, JOB_NAME, JOB_MARK)
    if job is None:
        job = tasks.create_own_job(cron_jobs, store, "ideas", profile, JOB_NAME, prompt=JOB_PROMPT, schedule=expr)
        log.info("hermes-hq-push: task ideas job created (%s)", expr)
        return job["id"]
    updates = {}
    if (job.get("schedule") or {}).get("expr") != expr:
        updates["schedule"] = expr
    if job.get("prompt") != JOB_PROMPT:
        updates["prompt"] = JOB_PROMPT
    if job.get("enabled_toolsets") is not None:   # a trial build's short list: the bot's usual tools now
        updates["enabled_toolsets"] = None
    if not job.get("enabled", True):
        updates.update(enabled=True, state="scheduled")
    if updates:
        cron_jobs.update_job(job["id"], updates)
    return job["id"]


def remove_job(store, profile: str) -> None:
    """Ideas is off here: ``profile``'s own Task ideas job goes; a job of the person's with that name stays."""
    tasks.remove_own_job(store, "ideas", profile, JOB_NAME, JOB_MARK)


def _record_job(store, job_id: str) -> None:
    ids = [i for i in (_meta(store, "ideas_job").get("ids") or []) if i != job_id][-24:] + [job_id]
    with closing(store._write_db()) as db, db:
        _put_meta(db, "ideas_job", {"ids": ids})


def recorded_job_bots(store) -> set[str]:
    """Every bot that currently has an ideas job id recorded in tasks.db meta."""
    with closing(store._db()) as db:
        return {key[len("job:ideas:"):] for (key,) in db.execute("select key from meta where key like 'job:ideas:%'")}


def reconcile_job(store, profile: str, home=None) -> None:
    """``profile``'s "Task ideas" job: there, at ideas_at, while Tasks and Ideas are on and it's in active_bots;
    removed otherwise. In ``home``'s schedule (``None`` = the active one). Best-effort, never raises."""
    try:
        with tasks._cron_home(home):
            settings = store.get_settings()
            if store.enabled() and settings.get("ideas") and profile in active_bots(settings):
                job_id = ensure_job(settings["ideas_at"], store, profile)
                if job_id:
                    _record_job(store, job_id)
            else:
                remove_job(store, profile)
    except Exception:
        log.debug("hermes-hq-push: task ideas job not reconciled", exc_info=True)


def reconcile_all(store, before_bots=None, home_for: Callable | None = None) -> None:
    """Reconciles Task ideas jobs across every previous, recorded, and currently active bot."""
    resolve_home = home_for or tasks.bot_home
    settings = store.get_settings()
    targets = set(active_bots(settings)) | set(before_bots or ()) | recorded_job_bots(store)
    for bot in sorted(targets):
        home = resolve_home(bot)
        if home is not None:
            reconcile_job(store, bot, home)


def run_now(store, home_for: Callable | None = None) -> dict:
    """Look Now: triggers the "Task ideas" job on every active bot at its scheduler's next tick. Once every RUN_GAP."""
    settings = store.get_settings()
    if not (store.enabled() and settings.get("ideas")):
        raise IdeaError(409, "Ideas is off.")
    asked = _meta(store, "ideas_asked").get("at") or ""
    if asked and asked > _ago(store, RUN_GAP):
        return {"started": False, "looking": True}
    resolve_home = home_for or tasks.bot_home
    bots = active_bots(settings)
    triggered = 0
    missing_all = True
    for bot in bots:
        home = resolve_home(bot)
        if home is None:
            continue
        missing_all = False
        try:
            with tasks._cron_home(home):
                job_id = ensure_job(settings["ideas_at"], store, bot)
                from cron import jobs as cron_jobs
                if job_id and cron_jobs.trigger_job(job_id) is not None:
                    _record_job(store, job_id)
                    triggered += 1
        except Exception:
            log.debug("hermes-hq-push: task ideas not started now for %s", bot, exc_info=True)
    if missing_all:
        raise IdeaError(404, "No such bot.")
    if triggered == 0:
        raise IdeaError(503, "Couldn't start it now; it runs at its time.")
    with closing(store._write_db()) as db, db:
        _put_meta(db, "ideas_asked", {"at": _ago(store)})
    return {"started": True, "looking": True}


# ---------------------------------------------------------------------------
# The guard (pre_tool_call): enforces source toggles + strict read-only safety inside any Task ideas run.
# ---------------------------------------------------------------------------

def run_job_id(session_id) -> str:
    """The job a cron run's session belongs to, or ""."""
    match = _RUN_ID.match(str(session_id or ""))
    if match:
        return match.group(1)
    try:
        from gateway.session_cron import current_execution
        execution = current_execution()
        if execution is not None and (not session_id or str(execution[1]) == str(session_id)):
            job_id = str(execution[2] or "").strip()
            if job_id:
                return job_id
    except Exception:
        pass
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_CRON_JOB_ID") or ""
    except Exception:
        return ""


def in_ideas_run(session_id, store_for: Callable) -> bool:
    job_id = run_job_id(session_id)
    if not job_id:
        return False
    if job_id in _known_runs:
        return _known_runs[job_id]
    answer = None
    try:   # by the ids we recorded, not the name: a job of the person's called "Task ideas" may add tasks
        answer = job_id in (_meta(store_for(), "ideas_job").get("ids") or [])
    except Exception:
        pass
    if answer is not None:   # a lookup that failed isn't remembered: the next call asks again
        _known_runs[job_id] = answer
    return bool(answer)


def _composio_toolkit(slug: str) -> str:
    s = str(slug or "").strip()
    if not s or "_" not in s:
        return ""
    return s.split("_", 1)[0].lower()


def _composio_action(slug: str) -> str:
    s = str(slug or "").strip()
    if not s or "_" not in s:
        return s
    return s.split("_", 1)[1]


def _is_mutating_action(name: str) -> bool:
    return bool(_MUTATING_ACTION.search(str(name or "")))


def _connector_enabled(kind: str, raw_name: str, enabled_connectors: set[str]) -> bool:
    cid = f"{kind}:{raw_name.lower()}"
    if cid in enabled_connectors:
        return True
    target = _norm_key(raw_name)
    prefix = kind + ":"
    for item in enabled_connectors:
        if item.startswith(prefix) and _norm_key(item[len(prefix):]) == target:
            return True
    return False


def _extract_mcp(tool_name: str) -> tuple[str, str] | None:
    m = _MCP_DOUBLE.match(tool_name)
    if m:
        return m.group(1).lower(), m.group(2)
    m = _MCP_SINGLE.match(tool_name)
    if m:
        return m.group(1).lower(), m.group(2)
    return None


def _guard_composio(action: str, args: dict | None, enabled_connectors: set[str]) -> str | None:
    has_any = any(c.startswith("composio:") for c in enabled_connectors)
    act = action.upper()
    if act in COMPOSIO_BLOCKED_TOOLS:
        return f"Refused: {action} isn't used in a Task ideas run."
    if not has_any:
        return "Refused: no Connected Apps are enabled for Suggested Tasks. Enable them in Tasks Settings › Where to Look."
    if act in COMPOSIO_DISCOVERY_TOOLS:
        return None
    if act == "COMPOSIO_MULTI_EXECUTE_TOOL":
        tools = (args or {}).get("tools")
        if not isinstance(tools, list) or not tools:
            return None
        for call in tools:
            if not isinstance(call, dict):
                continue
            slug = str(call.get("tool_slug") or "")
            tk = _composio_toolkit(slug)
            if not tk or not _connector_enabled("composio", tk, enabled_connectors):
                return f"Refused: {_pretty_connector_name(tk or slug)} is not enabled in Suggested Tasks › Where to Look."
            if _is_mutating_action(_composio_action(slug)):
                return f"Refused: Task ideas is read-only, so {slug} isn't allowed."
        return None
    if act == "COMPOSIO_REMOTE_WORKBENCH":
        code = str((args or {}).get("code_to_execute") or "")
        slugs = _WORKBENCH_CALL.findall(code)
        if not slugs:
            return "Refused: arbitrary workbench scripts aren't allowed in a Task ideas run."
        for slug in slugs:
            tk = _composio_toolkit(slug)
            if not tk or not _connector_enabled("composio", tk, enabled_connectors):
                return f"Refused: {_pretty_connector_name(tk or slug)} is not enabled in Suggested Tasks › Where to Look."
            if _is_mutating_action(_composio_action(slug)):
                return f"Refused: Task ideas is read-only, so {slug} isn't allowed."
        return None
    if _is_mutating_action(action):
        return f"Refused: Task ideas is read-only, so {action} isn't allowed."
    return None


def guard(tool_name: str, session_id, store_for: Callable, args=None, profile: str | None = None) -> str | None:
    """Enforces source toggles and read-only safety during any Task ideas run, else ``None``.
    Cheap for every non-ideas session: returns immediately when not in a Task ideas run."""
    name = str(tool_name or "")
    if not name or name in ALWAYS_ALLOWED:
        return None
    if not in_ideas_run(session_id, store_for):
        return None
    if name in TASK_WRITES:
        return (f"Refused: in the Task ideas run, {name} isn't used. Put each idea in tasks_ideas with action "
                "'suggest'; the user adds the ones they want.")

    try:
        src = sources_of(store_for().get_settings())
    except Exception:
        src = dict(tasks.DEFAULT_IDEAS_SOURCES)
    enabled_connectors = set(src["connectors"])

    # Built-in source gates
    if name in CHAT_TOOLS:
        if not src["chats"]:
            return "Refused: Recent Chats is off in Suggested Tasks › Where to Look."
        return None
    if name in MEMORY_TOOLS:
        if not src["memory"]:
            return "Refused: Memory is off in Suggested Tasks › Where to Look."
        action = str((args or {}).get("action") or "").lower()
        if action in MEMORY_WRITES:
            return f"Refused: Task ideas is read-only, so memory {action} isn't allowed."
        return None
    if name in FILE_WRITE_TOOLS:
        return f"Refused: Task ideas is read-only, so {name} isn't allowed."
    if name in FILE_READ_TOOLS:
        if not src["files"]:
            return "Refused: Local Files & Notes is off in Suggested Tasks › Where to Look."
        return None
    if name in WEB_TOOLS or name.startswith("browser_"):
        if not src["web"]:
            return "Refused: Web Search is off in Suggested Tasks › Where to Look."
        if name.startswith("browser_") and _is_mutating_action(name):
            return f"Refused: Task ideas is read-only, so {name} isn't allowed."
        return None

    # Composio & direct MCP connector gates
    mcp = _extract_mcp(name)
    if mcp is not None:
        server, action = mcp
        if server == "composio" or action.upper().startswith("COMPOSIO_"):
            return _guard_composio(action, args if isinstance(args, dict) else None, enabled_connectors)
        if not _connector_enabled("mcp", server, enabled_connectors):
            return f"Refused: {_pretty_connector_name(server)} is not enabled in Suggested Tasks › Where to Look."
        if _is_mutating_action(action):
            return f"Refused: Task ideas is read-only, so {name} isn't allowed."
        return None
    if name.upper().startswith("COMPOSIO_"):
        return _guard_composio(name, args if isinstance(args, dict) else None, enabled_connectors)

    # General read-only outward safety net
    if _OUTWARD_TOOL.search(name):
        return f"Refused: Task ideas is read-only, so {name} isn't allowed."
    return None


# ---------------------------------------------------------------------------
# The tool: tasks_ideas, for the Task ideas run only.
# ---------------------------------------------------------------------------

IDEA_ITEM = {"type": "object", "properties": {
    "title": {"type": "string", "description": "The task, as a to-do in a few words (at most 120 characters)."},
    "why": {"type": "string", "description": "One line: what you saw and why now (at most 240 characters)."},
    "source": {"type": "string", "description": "Where it came from, in a few words (\"Chat with Hermes, Tue\", \"Gmail · Yesterday\", \"Notion · Inbox\")."},
    "who": {"type": "string", "enum": list(WHO), "description": "Who'd do it: me (the user), together, or bot."},
    "help": {"type": "string", "description": "What a bot would do to help, in one short line (empty if none)."},
    "bot": {"type": "string", "description": "The bot best placed to help, from context's bots."},
}, "required": ["title", "why"]}

SCHEMA = {
    "name": "tasks_ideas",
    "description": ("Only for your Task ideas job: action 'context' gives you today, the user's open tasks, ideas "
                    "offered before, which sources to check and skip, any focus/skip instructions, and recent chats "
                    "when enabled; action 'suggest' saves your ideas (at most context's max; none is fine) on their "
                    "Tasks page's Suggested view. Nothing becomes a task until they add it. " + IDEA_RULE),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["context", "suggest"]},
        "ideas": {"type": "array", "items": IDEA_ITEM, "description": "For suggest."},
    }, "required": ["action"]},
}


def register_tool(ctx, store_for: Callable, profile: Callable[[], str]) -> None:
    def handle(args, session_id="", **kwargs):
        args = args if isinstance(args, dict) else {}
        store, bot = store_for(), profile()
        settings = store.get_settings()
        if not (store.enabled() and settings.get("ideas")):
            return json.dumps({"stop": "Ideas is turned off. End the turn."})
        if not in_ideas_run(session_id, store_for):
            return json.dumps({"error": "Only the Task ideas job uses this. To add a task the user asked for, use "
                                        "tasks_add."})
        if args.get("action") == "suggest":
            return json.dumps(suggest(store, args.get("ideas"), bot), ensure_ascii=False)
        return json.dumps(context(store, bot), ensure_ascii=False)

    def on() -> bool:
        """Any participating ideas bot sees it while Tasks and Ideas are on."""
        try:
            store = store_for()
            settings = store.get_settings()
            return store.enabled() and bool(settings.get("ideas")) and profile() in active_bots(settings)
        except Exception:
            return False

    ctx.register_tool(name="tasks_ideas", toolset="hq_tasks", check_fn=on, schema=SCHEMA, handler=handle,
                      description=SCHEMA["description"], emoji="💡")
