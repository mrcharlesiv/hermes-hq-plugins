"""The words a Live Activity's Lock Screen card shows for a bot's steps (0.9.22, ``liveSteps`` in /status).

The app names the running step on its Bots rows and on the card ("Running npm test…", the two before it, how many);
once iOS suspends the app only a push can change the card, so the add-on says the same words: a faithful port of the
app's ``ToolNow.phrase`` (HermesKit/Activity/ToolNow.swift), the bits of ``ToolLines`` it reads (ToolLine.swift), its
silent tools (``ToolPresentation.isSilent``) and its ``LiveStepTrail`` (LiveKit/LiveActivityContent.swift). Keep them in
step: test_live_steps.py pins the same outputs as the app's ToolNowTests.

Pure: no Hermes, no I/O. Nothing here logs. Lengths count characters (code points); Swift counts grapheme clusters,
which differ only for combined characters such as some emoji.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field

# ToolNow.subjectLimit: the longest subject kept whole; longer ones end in "…".
SUBJECT_LIMIT = 36
# LiveStepTrail.recentLimit and .wordsLimit.
RECENT_LIMIT = 2
WORDS_LIMIT = 60

# ToolPresentation.silentTools: draw nothing, so the step before them stays named.
SILENT_TOOLS = frozenset({"react_to_message", "todo", "todo_list"})
# BotDMs.tool
MESSAGE_AGENT = "message_agent"

# ToolNow.plain: tools whose subject reads badly after the verb.
PLAIN = {
    "image_generate": "making an image", "video_generate": "making a video", "text_to_speech": "recording audio",
    "vision_analyze": "looking at an image", "video_analyze": "watching a video", "browser_click": "clicking in the browser",
    "browser_scroll": "scrolling the page", "browser_snapshot": "looking at the page", "browser_back": "going back a page",
    "browser_press": "pressing a key", "browser_vision": "looking at the page", "browser_console": "reading the console",
    "browser_get_images": "looking at the page's images", "cronjob_manage": "updating a scheduled job",
    "execute_code": "running code", "process_manage": "checking a background task", "read_terminal": "reading the terminal",
    "close_terminal": "closing the terminal", "memory": "updating memory", "todo_list": "updating tasks", "todo": "updating tasks",
    "skills_list": "looking through skills", "session_search": "searching past chats", "clarify": "asking you something",
    "x_search": "searching X",
}

# ToolLines.verb / noPreview / forConnector / primary / fallbackKeys / pathTools / urlTools.
VERB = {
    "web_search": "Searching the web", "web_extract": "Reading", "browser_navigate": "Browsing", "browser_click": "Clicking",
    "browser_type": "Typing", "read_file": "Reading", "write_file": "Writing", "patch": "Editing", "search_files": "Searching files",
    "terminal": "Running", "execute_code": "Running code", "image_generate": "Generating image", "video_generate": "Generating video",
    "text_to_speech": "Generating speech", "vision_analyze": "Looking at the image", "session_search": "Searching past sessions",
    "skill_view": "Reading skill", "skills_list": "Listing skills", "skill_manage": "Updating skill", "delegate_task": "Delegating",
    "cronjob_manage": "Scheduling", "clarify": "Asking", "memory": "Updating memory", "todo_list": "Updating tasks",
    "message_agent": "Messaging",
}
NO_PREVIEW = frozenset({"skills_list", "session_search", "memory", "todo_list"})
FOR_CONNECTOR = frozenset({"web_search", "search_files"})
PRIMARY = {
    "terminal": "command", "web_search": "query", "web_extract": "urls", "read_file": "path", "write_file": "path", "patch": "path",
    "search_files": "pattern", "browser_navigate": "url", "browser_click": "ref", "browser_type": "text", "image_generate": "prompt",
    "text_to_speech": "text", "vision_analyze": "question", "skill_view": "name", "skills_list": "category", "cronjob_manage": "action",
    "execute_code": "code", "browser_exec": "code", "delegate_task": "goal", "skill_manage": "name",
    "message_agent": "target",
}
FALLBACK_KEYS = ("query", "text", "command", "path", "name", "prompt", "code", "goal")
PATH_TOOLS = frozenset({"read_file", "write_file", "patch"})
URL_TOOLS = frozenset({"browser_navigate", "web_extract"})

# ToolNow's Composio words.
ACRONYMS = frozenset({"soql", "sql", "url", "urls", "id", "ids", "pdf", "api", "csv", "html", "sms", "crm"})
DOUBLES = frozenset({"run", "get", "set", "put", "stop", "plan", "chat", "tag", "pin", "ship", "scan"})
APP_NAMES = {
    "gmail": "Gmail", "googlecalendar": "Google Calendar", "googledrive": "Google Drive", "googledocs": "Google Docs",
    "googlesheets": "Google Sheets", "github": "GitHub", "gitlab": "GitLab", "hubspot": "HubSpot", "clickup": "ClickUp",
    "youtube": "YouTube", "linkedin": "LinkedIn", "whatsapp": "WhatsApp", "one_drive": "OneDrive", "metaads": "Meta Ads",
    "googleads": "Google Ads", "googlemeet": "Google Meet", "googletasks": "Google Tasks", "googlephotos": "Google Photos",
    "outlook": "Outlook", "onedrive": "OneDrive", "quickbooks": "QuickBooks", "openai": "OpenAI", "exa": "Exa",
}
_WORKBENCH_CALL = re.compile(r"""run_composio_tool\(\s*['"]([A-Z0-9_]+)['"]""")
_BARE_URL = re.compile(r"^https?://(?:www\.)?", re.IGNORECASE)


def is_silent(name) -> bool:
    """ToolPresentation.isSilent: a todo or a reaction leaves the step before it named."""
    return str(name or "") in SILENT_TOOLS


# ---------------------------------------------------------------------------
# JSONValue as Swift reads it
# ---------------------------------------------------------------------------

def _str(value):
    """`.string`: only a string."""
    return value if isinstance(value, str) else None


def _js_number(n) -> str:
    """ActivityJSON.jsNumber: JavaScript's String(n)."""
    n = float(n)
    if math.isfinite(n) and n == round(n) and abs(n) < 1e15:
        return str(int(n))
    text = repr(n)
    return text[:-2] if text.endswith(".0") else text


def _scalar(value):
    """ToolLines.scalar: what a preview shows of a string, number or true/false."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _js_number(value)
    return None


def _swift_split(text: str, sep: str) -> list:
    """Swift's split(separator:), which leaves out empty pieces."""
    return [part for part in text.split(sep) if part]


def _trim_spaces(text: str) -> str:
    """trimmingCharacters(in: .whitespaces): spaces and tabs, not line breaks."""
    def space(ch):
        return ch == "\t" or unicodedata.category(ch) == "Zs"
    start, end = 0, len(text)
    while start < end and space(text[start]):
        start += 1
    while end > start and space(text[end - 1]):
        end -= 1
    return text[start:end]


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


# ---------------------------------------------------------------------------
# ToolLines
# ---------------------------------------------------------------------------

def _one_line(value: str) -> str:
    return " ".join(value.split())


def _basename(path: str) -> str:
    p = path.replace("\\", "/")
    while p.endswith("/"):
        p = p[:-1]
    parts = _swift_split(p, "/")
    return parts[-1] if parts else path


def _bare_url(url: str) -> str:
    u = _BARE_URL.sub("", url, count=1)
    return u[:-1] if u.endswith("/") else u


def preview(tool: str, args: dict) -> str:
    """ToolLines.preview: a call's main argument, as one line (a path's file name, an address without its scheme)."""
    tasks = args.get("tasks")
    if tool == "delegate_task" and isinstance(tasks, list) and tasks:
        if len(tasks) == 1:
            goal = tasks[0].get("goal") if isinstance(tasks[0], dict) else None
            return _one_line(_scalar(goal) or "") if goal is not None else ""
        return f"{len(tasks)} tasks"
    key = PRIMARY.get(tool) or next((k for k in FALLBACK_KEYS if k in args), None)
    if key is None or key not in args:
        return ""
    value = args[key]
    if isinstance(value, list):
        if not value:
            return ""
        value = value[0]
    raw = _scalar(value)
    if raw is None:
        return ""
    text = _one_line(raw)
    if tool in PATH_TOOLS:
        return _basename(text)
    if tool in URL_TOOLS:
        return _bare_url(text)
    return text


# ---------------------------------------------------------------------------
# ToolNow
# ---------------------------------------------------------------------------

def short(text: str) -> str:
    """ToolNow.short: one line, at most SUBJECT_LIMIT characters ("…" ends a longer one)."""
    one = " ".join(str(text).split())
    if len(one) <= SUBJECT_LIMIT:
        return one
    return one[:SUBJECT_LIMIT - 1].strip() + "…"


def command(raw: str) -> str:
    """ToolNow.command: a command's first line without `cd … &&`, `VAR=value`, `sudo` or `env` in front, each path as
    its last part ("python3 /a/b/run.py --fast" → "python3 run.py --fast")."""
    lines = _swift_split(raw, "\n")
    line = _trim_spaces(lines[0] if lines else "")
    steps = [s for s in (_trim_spaces(part) for part in line.split("&&")) if s]
    real = next((s for s in steps if not s.startswith("cd ") and s != "cd"), None)
    if real is not None:
        line = real
    words = _swift_split(line, " ")
    while words and (words[0] in ("sudo", "env") or ("=" in words[0] and not words[0].startswith("-"))):
        words.pop(0)

    def last_part(word: str) -> str:
        if "/" not in word or "://" in word:
            return word
        trimmed = word[:-1] if word.endswith("/") else word
        parts = _swift_split(trimmed, "/")
        return parts[-1] if parts else word

    return " ".join(last_part(word) for word in words)


def unwrap(tool: str, args: dict):
    """ToolNow.unwrap: the one call inside the gateway's `tool_call` wrapper (how bots reach Composio), or None."""
    calls = args.get("calls")
    if tool != "tool_call" or not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return None
    name = _str(calls[0].get("name"))
    if not name:
        return None
    arguments = calls[0].get("arguments")
    inner = arguments if isinstance(arguments, dict) else {}
    if not inner and isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = None
        inner = parsed if isinstance(parsed, dict) else {}
    return name, inner


def human_name(tool: str) -> str:
    """"mcp__linear__search_issues" → "linear search issues"."""
    name = tool
    if name.startswith("mcp__"):
        name = name[5:]
    elif name.startswith("mcp_"):
        name = name[4:]
    return " ".join(_swift_split(name.replace("__", " ").replace("_", " "), " "))


def gerund(verb: str) -> str:
    if verb.endswith("ing"):
        return verb
    if verb in DOUBLES and verb:
        return verb + verb[-1] + "ing"
    if verb.endswith("e") and not verb.endswith("ee") and len(verb) > 2:
        return verb[:-1] + "ing"
    return verb + "ing"


def action_words(slug: str, ing: bool) -> str:
    """"NOTION_SEARCH_NOTION_PAGE" → "searching page" (ing) or "search page"."""
    parts = _swift_split(slug.lower(), "_")
    if not parts:
        return ""
    head = parts[0]
    words = [w for w in parts[1:] if w != head]
    if not words:
        return "using it"
    if ing:
        words[0] = gerund(words[0])
    return " ".join(w.upper() if w in ACRONYMS else w for w in words)


def app_name(slug: str) -> str:
    """"GMAIL_FETCH_EMAILS" → "Gmail", "GOOGLECALENDAR_FIND_EVENT" → "Google Calendar"."""
    parts = _swift_split(slug, "_")
    head = parts[0].lower() if parts else ""
    if not head:
        return ""
    return APP_NAMES.get(head) or _upper_first(head)


def workbench_slugs(code: str) -> list:
    out = []
    for slug in _WORKBENCH_CALL.findall(code):
        if slug not in out:
            out.append(slug)
    return out


def composio(action: str, args: dict) -> str:
    """A Composio tool by what it does in the app, never "Composio"."""
    if "SEARCH_TOOLS" in action:
        queries = args.get("queries")
        if isinstance(queries, list) and len(queries) == 1 and isinstance(queries[0], dict):
            use = _str(queries[0].get("use_case"))
            use = use.strip() if use is not None else ""
            if use:
                return short("looking for a tool to " + _lower_first(use))
        return "looking for the right tool"
    slugs = []
    if isinstance(args.get("tools"), list):
        slugs += [s for s in (_str(t.get("tool_slug")) if isinstance(t, dict) else None for t in args["tools"]) if s is not None]
    if isinstance(args.get("tool_slugs"), list):
        slugs += [s for s in args["tool_slugs"] if isinstance(s, str)]
    if _str(args.get("tool_slug")) is not None:
        slugs.append(args["tool_slug"])
    if "WORKBENCH" in action:
        code = _str(args.get("code_to_execute"))
        if code is None:
            code = _str(args.get("code"))
        if code is not None:
            slugs += workbench_slugs(code)
    if "GET_TOOL_SCHEMAS" in action:
        if not slugs:
            return "reading how to use a tool"
        what = action_words(slugs[0], False) + " in " + app_name(slugs[0]) if len(slugs) == 1 else f"{len(slugs)} tools"
        return short("reading how to use " + what)
    if "MANAGE_CONNECTIONS" in action or "WAIT_FOR_CONNECTION" in action:
        kits = args.get("toolkits")
        apps = [app_name(k) for k in kits if isinstance(k, str)] if isinstance(kits, list) else []
        what = f"the {apps[0]} connection" if len(apps) == 1 else "app connections"
        return short(("waiting for " if "WAIT" in action else "checking ") + what)
    if "BASH" in action:
        return "running a command"
    if not slugs:
        return "running a script" if "WORKBENCH" in action else "using a connected app"
    apps = []
    for slug in slugs:
        app = app_name(slug)
        if app and app not in apps:
            apps.append(app)
    place = apps[0] if len(apps) == 1 else f"{apps[0]} and {apps[1]}" if len(apps) == 2 else f"{len(apps)} apps"
    lead = "running a script that does " if "WORKBENCH" in action else ""
    if len(set(slugs)) == 1:
        return short(lead + action_words(slugs[0], not lead) + " in " + place)
    return short((lead or "running ") + f"{len(slugs)} actions in " + place)


def mcp(tool: str, args: dict):
    """An MCP server's tool: Composio's say the action inside the app; any other server's are its name and the tool's
    words without the server repeated, in lower case ("using linear search issues")."""
    if not tool.startswith("mcp_"):
        return None
    words = human_name(tool).split(" ")
    words = [w for w in words if w]
    if not words:
        return None
    server = words[0].lower()
    rest = words[1:]
    if rest and rest[0].lower() == server:
        rest = rest[1:]
    action = "_".join(rest).upper()
    if server == "composio":
        return composio(action, args)
    plain = " ".join(w.lower() if w == w.upper() else w for w in rest)
    return "using " + short(server + " " + plain if plain else server)


def phrase(name, args=None) -> str:
    """ToolNow.phrase: "running npm test", "reading package.json", "searching the web for swift actors" (no capital,
    no ellipsis). ``args``: the call's arguments as Hermes hands them to pre_tool_call (a dict; anything else is {})."""
    args = args if isinstance(args, dict) else {}
    name = str(name or "")
    tool = name[len("functions."):] if name.startswith("functions.") else name
    inner = unwrap(tool, args)
    if inner is not None:
        return phrase(*inner)
    calls = args.get("calls")
    if tool == "tool_call" and isinstance(calls, list) and len(calls) > 1:
        return f"running {len(calls)} tools"
    if tool in PLAIN:
        return PLAIN[tool]
    if tool == "terminal":
        raw = _str(args.get("command"))
        cmd = command(raw) if raw is not None else ""
        return "running " + short(cmd) if cmd else "running a command"
    if tool == "delegate_task":
        tasks = args.get("tasks")
        if isinstance(tasks, list) and len(tasks) > 1:
            return f"handing off {len(tasks)} tasks"
        return "handing off a task"
    if tool == MESSAGE_AGENT:
        # LiveSteps names the bot as the roster does; without a roster, its handle ("messaging cro").
        target = _str(args.get("target"))
        target = target.strip("@ ") if target is not None else ""
        return "messaging " + short(target) if target else "messaging another bot"
    if tool in ("browser_navigate", "web_extract"):
        url = preview(tool, args)
        hosts = _swift_split(url, "/")
        host = hosts[0] if hosts else ""
        if not host:
            return "reading a page" if tool == "web_extract" else "opening a page"
        return ("reading " if tool == "web_extract" else "opening ") + short(host)
    words = mcp(tool, args)
    if words is not None:
        return words
    verb = VERB.get(tool)
    if verb is None:
        return "using " + short(human_name(tool))
    lead = _lower_first(verb)
    subject = "" if tool in NO_PREVIEW else preview(tool, args)
    if not subject:
        return lead
    return lead + (" for " if tool in FOR_CONNECTOR else " ") + short(subject)


# ---------------------------------------------------------------------------
# The card's trail (LiveStepTrail)
# ---------------------------------------------------------------------------

def clean(words):
    """LiveStepTrail.clean: one line, at most WORDS_LIMIT characters, None when blank."""
    if not isinstance(words, str):
        return None
    line = " ".join(words.splitlines()).strip()
    if not line:
        return None
    return line[:WORDS_LIMIT - 1] + "…" if len(line) > WORDS_LIMIT else line


@dataclass
class Trail:
    """A turn's steps as its card names them: the running one, the ones before it (newest first), how many."""
    step: str | None = None
    recent: list = field(default_factory=list)
    steps: int = 0

    def follow(self, words) -> bool:
        """LiveStepTrail.follow: the bot's step is now ``words``; the one it was on moves to ``recent`` and a new one
        counts. The same words as the running step change nothing (the app's rows don't see a new step either)."""
        words = clean(words)
        if words == self.step:
            return False
        if self.step is not None:
            self.recent = ([self.step] + self.recent)[:RECENT_LIMIT]
        self.step = words
        if words is not None:
            self.steps += 1
        return True

    def settled(self) -> "Trail":
        """The turn is over: the step it was on is a finished one too."""
        if self.step is None:
            return Trail(None, list(self.recent), self.steps)
        return Trail(None, ([self.step] + self.recent)[:RECENT_LIMIT], self.steps)

    def words(self) -> dict:
        """What the card reads (the sealed box's plaintext): {"step", "recent", "steps"}."""
        return {"step": self.step, "recent": list(self.recent), "steps": self.steps}

    @classmethod
    def from_words(cls, value) -> "Trail":
        """A trail from the app (registration) or the store, made safe: one-line words, two recent at most."""
        value = value if isinstance(value, dict) else {}
        recent = value.get("recent")
        recent = [w for w in (clean(item) for item in recent) if w] if isinstance(recent, list) else []
        steps = value.get("steps")
        steps = steps if isinstance(steps, int) and not isinstance(steps, bool) and steps > 0 else 0
        return cls(clean(value.get("step")), recent[:RECENT_LIMIT], steps)


# ---------------------------------------------------------------------------
# Pacing: at most one push per activity every MIN_GAP_S, the last step always lands
# ---------------------------------------------------------------------------

class Pacer:
    """Per activity: a push goes now when the last went at least ``MIN_GAP_S`` ago; otherwise one is due when the gap
    is up (a trailing push, carrying whatever the trail is by then). Steps always go at ``PRIORITY`` 5: iOS rations
    priority-10 Live Activity pushes per app, and a long turn's steps at 10 (0.9.22: one every 20 s) used up that ration,
    so the turn's end, the one push that must land, could be held back and the card counted on (owner, 2026-11-01)."""

    MIN_GAP_S = 3.0
    PRIORITY = 5

    def __init__(self):
        self.last: dict = {}
        self.due: dict = {}

    def offer(self, key, now: float) -> bool:
        """A new step for ``key``: True to push it now; False when a trailing push is (now) due instead."""
        last = self.last.get(key)
        if last is None or now - last >= self.MIN_GAP_S:
            return True
        self.due.setdefault(key, last + self.MIN_GAP_S)
        return False

    def sent(self, key, now: float) -> int:
        """A push for ``key`` goes now: the priority it goes at."""
        self.last[key] = now
        self.due.pop(key, None)
        return self.PRIORITY

    def next_due(self):
        return min(self.due.values()) if self.due else None

    def take_due(self, now: float) -> list:
        keys = [key for key, at in self.due.items() if at <= now]
        for key in keys:
            del self.due[key]
        return keys

    def forget(self, key) -> None:
        for table in (self.last, self.due):
            table.pop(key, None)
