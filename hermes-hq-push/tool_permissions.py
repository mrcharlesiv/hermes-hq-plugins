"""Connector tool permissions (0.9.86, ``toolPermissions`` in /status): the app's Connectors page sets, per tool of an
MCP server, how far a bot may go with it, and this file makes the two asking levels real. Hermes reads only
``tools.include`` / ``tools.exclude`` (a blocked tool is written there and never registers); nothing in Hermes reads
the levels, which the app keeps beside them in the bot's config.yaml:

    mcp_servers:
      linear:
        tools:
          permissions: {create_issue: needs_approval, list_issues: always_allow, update_issue: auto_review}

    no entry          the tool runs without asking, as every tool did before levels
    always_allow      the same: never asked by this file
    needs_approval    every call asks the person first
    auto_review       a call the connector marks read-only runs; one it marks destructive, or not read-only, asks;
                      with neither mark, one whose name says it changes something (CHANGES) asks; anything else runs

"Asks" is Hermes's own approval gate: the add-on's pre_tool_call answers ``{"action": "approve", "message"}`` and Hermes
shows the approval as it shows a risky command's (the app's card, a push, Once / Session / Always / Deny), and runs
the tool only on a yes. A Hermes without that answer (before v0.21.6, the July 2026 releases) would run the tool
anyway, so there the call is refused instead (fail closed): the bot is told the tool is set to ask and this computer's
Hermes must be updated for that.

The hook runs on every tool call of every bot, so this costs nothing for a tool that isn't an MCP tool (a prefix
test) and one dict lookup for an MCP tool whose server has no levels. The profile's config.yaml is read again only
when its modification time or size changes. Nothing here raises; a malformed map is ignored. No argument value that
looks like a secret reaches the approval's summary, and nothing is logged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path

LEVELS = ("always_allow", "auto_review", "needs_approval")
ASKING = ("auto_review", "needs_approval")
PREFIXES = ("mcp__", "mcp_")     # mcp__<server>__<tool> since v2026.7.7; mcp_<server>_<tool> before
RULE = "hq-connector"
TITLES = {"auto_review": "Auto-review", "needs_approval": "Needs approval"}
# auto_review with no marks from the connector: a tool whose name holds one of these words changes something. Kept
# short on purpose, the verbs that write, send, spend or remove; a word in the middle of a name counts (batch_update,
# createIssue, send-message), a word inside another word doesn't (settings, address, sendgrid_list are none of them).
CHANGES = frozenset({
    "create", "add", "insert", "update", "edit", "modify", "set", "write", "upload", "move", "rename",
    "delete", "remove", "archive", "close", "cancel", "send", "post", "reply", "publish", "submit", "share", "invite",
    "pay", "buy", "purchase", "book", "transfer", "merge", "deploy", "run", "execute",
})
# The arguments the approval sums up: never one whose name says it is a secret, and never long.
# A name is a secret's when one of its words is one of these (so `author` and `shipping` are not).
_SECRET_WORDS = frozenset({"password", "passwd", "pass", "passphrase", "secret", "token", "key", "apikey", "auth",
                           "authorization", "bearer", "cookie", "credential", "credentials", "otp", "pin", "cvv", "cvc",
                           "ssn"})
_SECRET_VALUE = re.compile(r"^(?:sk-|sk_|pk_|rk_|xox[abpr]-|gh[pousr]_|github_pat_|eyJ)[-_A-Za-z0-9.]{12,}|"
                           r"^[A-Za-z0-9+/=_-]{40,}$")
_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
MAX_SUMMARY = 300
MAX_VALUE = 60
MAX_ARGS = 6

_log = logging.getLogger("hermes-hq-push")
_lock = threading.Lock()
# config.yaml path -> (mtime_ns, size, {registered tool name: (server, tool, level)})
_cache: dict[str, tuple[int, int, dict]] = {}


def status() -> dict:
    """`toolPermissions` (0.9.86): the add-on enforces a connector tool's `tools.permissions` level. `toolPermissionsAsk`:
    this Hermes can ask the person (the `approve` answer); false means a tool set to ask is refused instead."""
    return {"toolPermissions": True, "toolPermissionsAsk": supports_approve()}


def supports_approve() -> bool:
    """Whether the running Hermes takes a pre_tool_call's {"action": "approve"}: resolve_pre_tool_block came with it
    (v2026.7.7, v0.21.6) and is in every release since. hermes_cli.plugins is loaded before any plugin is."""
    try:
        plugins = sys.modules.get("hermes_cli.plugins")
        if plugins is None:
            import hermes_cli.plugins as plugins
        return callable(getattr(plugins, "resolve_pre_tool_block", None))
    except Exception:
        return False


# MARK: Names (Hermes's tools/mcp_tool_schema.mcp_prefixed_tool_name)

def _sanitize(value) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


def _hermes_namer():
    """Hermes's own naming function when this Hermes has it, so a change there is followed here; else None."""
    for module in ("tools.mcp_tool_schema", "tools.mcp_tool"):
        try:
            found = sys.modules.get(module)
            if found is None:
                found = __import__(module, fromlist=["mcp_prefixed_tool_name"])
            namer = getattr(found, "mcp_prefixed_tool_name", None)
            if callable(namer):
                return namer
        except Exception:
            continue
    return None


def registered_name(server: str, tool: str) -> str:
    """The name a call to `tool` on `server` reaches pre_tool_call with: ``mcp__<server>__<tool>``, every character
    outside [A-Za-z0-9_] made `_`, case kept, and a name longer than 64 cut to 55 with `_` and 8 hex of its SHA-256
    (what Hermes does since v2026.7.7)."""
    full = f"mcp__{_sanitize(server)}__{_sanitize(tool)}"
    if len(full) <= 64:
        return full
    suffix = "_" + hashlib.sha256(full.encode("utf-8")).hexdigest()[:8]
    return full[:64 - len(suffix)] + suffix


def names_for(server: str, tool: str, namer=None) -> set[str]:
    """Every registered name the pair may carry here: Hermes's own when it has the function (else this file's copy of
    it), and the name Hermes gave before v2026.7.7 (``mcp_<server>_<tool>``) when it doesn't."""
    if namer is not None:
        try:
            return {str(namer(server, tool))}
        except Exception:
            pass
    return {registered_name(server, tool), f"mcp_{_sanitize(server)}_{_sanitize(tool)}"}


# MARK: The levels, from the profile's config.yaml

def _config_path() -> Path | None:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "config.yaml"
    except Exception:
        return None


def index_of(config) -> dict:
    """{registered tool name: (server, tool, level)} for every asking level in `config` (config.yaml, read). Anything
    the wrong shape is skipped: a map that isn't one, a name or level that isn't text, a level the app doesn't write.
    `always_allow` asks nothing, so it isn't kept. When two tools of a server come out under one name (read-file and
    read_file; Hermes registers neither then), the stricter level stands."""
    out: dict = {}
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        return out
    namer = None
    for server, entry in servers.items():
        tools = entry.get("tools") if isinstance(entry, dict) else None
        levels = tools.get("permissions") if isinstance(tools, dict) else None
        if not isinstance(levels, dict) or not isinstance(server, str) or not server:
            continue
        for tool, level in levels.items():
            if not isinstance(tool, str) or not tool or not isinstance(level, str) or level.strip() not in ASKING:
                continue
            level = level.strip()
            if namer is None:
                namer = _hermes_namer() or False
            for name in names_for(server, tool, namer or None):
                kept = out.get(name)
                if kept is None or (kept[2] == "auto_review" and level == "needs_approval"):
                    out[name] = (server, tool, level)
    return out


def _load(path: Path) -> dict:
    try:
        import yaml
        with open(path, encoding="utf-8") as handle:
            return index_of(yaml.safe_load(handle))
    except Exception:
        _log.debug("hermes-hq-push: connector levels not read from config.yaml")
        return {}


def levels(path: Path | None = None) -> dict:
    """The index for the profile this call runs under (its HERMES_HOME), read again only when config.yaml changed."""
    path = path if path is not None else _config_path()
    if path is None:
        return {}
    key = str(path)
    try:
        info = os.stat(key)
    except OSError:
        _cache.pop(key, None)
        return {}
    found = _cache.get(key)
    if found is not None and found[0] == info.st_mtime_ns and found[1] == info.st_size:
        return found[2]
    index = _load(path)
    with _lock:
        _cache[key] = (info.st_mtime_ns, info.st_size, index)
    return index


# MARK: auto_review: what the connector says, else the tool's name

def hints(server: str, tool: str) -> tuple[bool | None, bool | None]:
    """(read-only, destructive) as the MCP server marked the tool, None where unknown. Hermes keeps readOnlyHint for
    every discovered tool (``tools.mcp_tool._tool_read_only_hints``; True only when marked exactly so) and the full
    marks only on a live connection's tool list (``_servers[key]._tools``; empty for a server not connected yet, and
    in a separate plugin host process). Read from what is already loaded, never imported; any surprise is unknown."""
    core = sys.modules.get("tools.mcp_tool")
    if core is None:
        return None, None
    try:
        scope = sys.modules.get("tools.mcp_tool_scope")
        key = scope._resolve_server_key(server) if scope is not None else server
    except Exception:
        key = server
    read_only = destructive = None
    try:
        if (getattr(core, "_tool_read_only_hints", {}).get(key) or {}).get(tool) is True:
            read_only = True
    except Exception:
        pass
    try:
        live = getattr(core, "_servers", {}).get(key)
        for item in list(getattr(live, "_tools", None) or []):
            if getattr(item, "name", None) != tool:
                continue
            marks = getattr(item, "annotations", None)
            if marks is None:
                break
            if isinstance(marks, dict):
                ro, de = marks.get("readOnlyHint", marks.get("read_only_hint")), \
                    marks.get("destructiveHint", marks.get("destructive_hint"))
            else:
                ro = getattr(marks, "read_only_hint", getattr(marks, "readOnlyHint", None))
                de = getattr(marks, "destructive_hint", getattr(marks, "destructiveHint", None))
            if isinstance(ro, bool):
                read_only = ro
            if isinstance(de, bool):
                destructive = de
            break
    except Exception:
        pass
    return read_only, destructive


def changes_by_name(tool: str) -> bool:
    """Whether the tool's name holds one of CHANGES as a word of its own (snake, kebab, dotted or camel case)."""
    return any(word.lower() in CHANGES for word in _WORDS.findall(str(tool or "")))


def asks(level: str, server: str, tool: str) -> bool:
    if level == "needs_approval":
        return True
    if level != "auto_review":
        return False
    read_only, destructive = hints(server, tool)
    if read_only is True:
        return False
    # Marked as not read-only: it changes something, which is what this level asks about, whether or not the
    # connector also calls the change harmless (MCP itself reads a missing destructiveHint as destructive).
    if destructive is True or read_only is False:
        return True
    return changes_by_name(tool)


# MARK: The answer

def _secret_name(name: str) -> bool:
    return any(word.lower() in _SECRET_WORDS for word in _WORDS.findall(name))


def summary(args) -> str:
    """A few of the call's arguments, `name: value`, for the person to judge it by: secrets left out, values cut."""
    if not isinstance(args, dict) or not args:
        return ""
    parts = []
    for name, value in list(args.items())[:MAX_ARGS]:
        name = str(name)
        if _secret_name(name):
            continue
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
        text = " ".join(text.split())
        if _SECRET_VALUE.search(text):
            text = "(hidden)"
        if len(text) > MAX_VALUE:
            text = text[:MAX_VALUE - 1] + "…"
        parts.append(f"{name}: {text}")
    if len(args) > MAX_ARGS:
        parts.append(f"and {len(args) - MAX_ARGS} more")
    out = "; ".join(parts)
    return out if len(out) <= MAX_SUMMARY else out[:MAX_SUMMARY - 1] + "…"


def _rule_key(server: str, tool: str, level: str, args) -> str:
    """The grain of the person's Session or Always: a whole tool for auto_review (they judged its kind of call), the
    exact call for needs_approval (it was set to ask every time, so a yes covers only the same call again)."""
    base = f"{RULE}:{server}/{tool}"
    if level != "needs_approval":
        return base
    try:
        text = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        text = str(args)
    return base + ":" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def check(tool_name, args=None, path: Path | None = None) -> dict | None:
    """pre_tool_call: None (run as before), Hermes's approval for this call, or a refusal where Hermes can't ask."""
    try:
        name = tool_name if isinstance(tool_name, str) else ""
        if not name.startswith(PREFIXES[1]):
            return None
        found = levels(path).get(name)
        if found is None:
            return None
        server, tool, level = found
        if not asks(level, server, tool):
            return None
        try:
            said = summary(args)
        except Exception:
            said = ""
        about = f"{server} › {tool} (set to {TITLES[level]} in Connectors)" + (f": {said}" if said else "")
        if supports_approve():
            return {"action": "approve", "message": about, "rule_key": _rule_key(server, tool, level, args)}
        return {"action": "block", "message": (
            f"Nothing was done. The {server} connector's {tool} is set to {TITLES[level]} in the Hermes HQ app's "
            "Connectors, so it asks the user before it runs, and the Hermes on this computer is too old to ask (it "
            "must be updated for that). Don't try another way. Tell the user in one short line: this tool needs "
            "their OK, which this computer's Hermes can't ask for until it is updated; or they can set it to Always "
            "allow in Connectors.")}
    except Exception:
        _log.debug("hermes-hq-push: connector level check skipped")
        return None
