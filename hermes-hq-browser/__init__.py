"""Hermes HQ Browser: watch a bot's browser live from Hermes HQ, and take it over.

Every process that runs agents loads this and publishes its bots' browsers (agent.py) through owner-only
files under ``<hermes root>/hermes-hq-browser`` (spool.py). ``hermes serve`` also mounts
``dashboard/plugin_api.py``, which lists those browsers, relays Chrome's own screencast to the phone and
carries the person's taps and typing while they hold a browser. Bots keep every browser tool Hermes gives
them: nothing in Hermes is overridden or patched. If the browser internals agent.py reads change shape,
the plugin stays off. See README.md.
"""
import importlib.util
import logging
import sys
from pathlib import Path

log = logging.getLogger("hermes-hq-browser")


def load(name: str):
    """This plugin's sibling modules, one instance per process (shared with dashboard/plugin_api.py). Hermes'
    plugin reload evicts only ``hermes_plugins.<slug>``, so a sibling whose file changed since it was loaded (an
    update, then ``hermes plugins disable``/``enable``) is loaded again, and the instance it replaces is marked
    ``_retired`` (agent.py's tick loop then ends its listings and stops)."""
    key = f"hermes_hq_browser_{name}"
    path = Path(__file__).with_name(f"{name}.py")
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    current = sys.modules.get(key)
    if current is not None and (stamp is None or getattr(current, "_loaded_stamp", None) == stamp):
        return current
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    module._loaded_stamp = stamp
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if current is not None:
            sys.modules[key] = current  # a broken update keeps the working instance
        else:
            sys.modules.pop(key, None)
        raise
    if current is not None:
        current._retired = True
    return module


def register(ctx):
    try:
        agent = load("agent")
        if agent.install(load("spool"), load("fleet")):
            return
        ctx.register_hook("pre_tool_call", agent.before_tool)
        ctx.register_hook("post_tool_call", agent.after_tool)
        ctx.register_tool(name=agent.ASK_TOOL, toolset="browser", schema=agent.ASK_SCHEMA, handler=agent.ask_user,
                          check_fn=agent.ask_available, description=agent.ASK_SCHEMA["description"], emoji="🙋")
        # "Pull up a website" means the bot's own browser, which Hermes HQ shows; not desktop_preview, which opens
        # the page on the person's phone. Optional surfaces: an older Hermes without them still gets the rest.
        if callable(getattr(ctx, "register_system_prompt_section", None)):
            ctx.register_system_prompt_section("hermes-hq-browser.web", agent.system_section, max_chars=1200)
        ctx.register_hook("pre_llm_call", agent.before_llm)
    except Exception:  # noqa: BLE001 - a broken plugin must never break the agent
        log.warning("hermes-hq-browser: could not start", exc_info=True)
