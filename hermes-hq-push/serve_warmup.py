"""Bots answer fast right after Hermes restarts, and a frozen ``hermes serve`` says what froze it (0.9.15).

Warm-up. A freshly started ``hermes serve`` knows nothing about its other bots: the first chat the app opens in one
pays for that bot's plugin discovery (~60 plugins, imported one after another) and its secret hydration while the
rest of the host waits for the same CPU. 2026-10-30, right after a restart, a finance bot sat on "Waking up…" for
~2 minutes. ``start_warmup`` loads every served bot one at a time on a background thread 20 s after boot (with the
same scope an agent build binds, so the first real build finds the loaded plugins and hydrated secrets), with a
pause between bots so live requests get the CPU back. A single-bot computer has nothing to warm. When the pieces it
relies on are missing (an older or reworked Hermes), or Hermes already warms its bots itself, it does nothing.
``plugin-data/hermes-hq-push/warmup.off`` turns it off.

Freeze recorder. Hermes logs "event loop stalled Ns" but not why. ``watch_loop`` (armed by the first request to this
add-on's routes, which runs on the server's loop) ticks every 2 s and keeps faulthandler's C watchdog — it needs no
GIL — set to dump every thread's stack to ``logs/loop-stall-stacks.log`` when a tick is 3 s late. Stops at 20 MB.
"""
import faulthandler
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("hermes-hq-push")

MARK = "_hermes_hq_serve_warmup"
START_DELAY = 20.0
PAUSE_BETWEEN = 2.0
TICK = 2.0
LATE = 3.0
STACKS_FILE = "loop-stall-stacks.log"
STACKS_MAX_BYTES = 20 * 1024 * 1024

_lock = threading.Lock()


def _launch_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").resolve()


def _claim(name: str) -> bool:
    """Once per process: the add-on loads again in every bot's scope, so the mark lives on a shared module."""
    with _lock:
        holder = sys.modules.get("tui_gateway.server") or sys
        if getattr(holder, MARK + name, False):
            return False
        setattr(holder, MARK + name, True)
        return True


# ── Warm-up ──────────────────────────────────────────────────────────────────────────────────────────────────

def _pieces():
    """``(runtime scope, discover, homes to warm)`` or ``None`` when this Hermes lacks them."""
    try:
        from hermes_cli.plugins import discover_plugins
        from hermes_cli.profiles import profiles_to_serve
        from tui_gateway import server
        scope = server._session_profile_runtime_scope
    except Exception:
        return None
    launch = _launch_home()

    def homes():
        found = []
        for _name, home in profiles_to_serve(multiplex=True):
            resolved = Path(home).resolve()
            if resolved != launch and resolved not in found:
                found.append(resolved)
        return found

    return scope, discover_plugins, homes


def _multi_bot_host() -> bool:
    try:
        from agent.secret_scope import is_multiplex_active
        return bool(is_multiplex_active())
    except Exception:
        return False


def _warm(off_file: Path) -> None:
    time.sleep(START_DELAY)  # after Hermes's last boot step decides whether it hosts several bots
    if off_file.exists() or "tui_gateway.profile_prewarm" in sys.modules or not _multi_bot_host():
        return
    pieces = _pieces()
    if pieces is None:
        return
    scope, discover, homes = pieces
    try:
        targets = homes()
    except Exception:
        log.debug("hermes-hq-push: warm-up could not list bots", exc_info=True)
        return
    started = time.monotonic()
    warmed = 0
    for home in targets:
        one = time.monotonic()
        try:
            with scope({"profile_home": str(home)}):
                discover()
            warmed += 1
            log.info("hermes-hq-push: warm-up: %s ready in %.1fs", home.name, time.monotonic() - one)
        except Exception:
            log.info("hermes-hq-push: warm-up: %s could not be warmed", home.name, exc_info=True)
        time.sleep(PAUSE_BETWEEN)
    log.info("hermes-hq-push: warm-up: %d of %d bots in %.1fs", warmed, len(targets), time.monotonic() - started)


def start_warmup(data_dir: Path) -> str:
    """Start the background warm-up once per ``hermes serve``. Returns "started", "off", "absent" or "already"."""
    off_file = Path(data_dir) / "warmup.off"
    if off_file.exists():
        return "off"
    if _pieces() is None:
        return "absent"
    if not _claim("warmup"):
        return "already"
    threading.Thread(target=_warm, args=(off_file,), name="hermes-hq-warmup", daemon=True).start()
    return "started"


# ── Freeze recorder ──────────────────────────────────────────────────────────────────────────────────────────

def watch_loop(loop) -> bool:
    """Arm the stall recorder on ``loop`` once per process. Returns True when this call armed it."""
    if not _claim("stalls"):
        return False
    path = _launch_home() / "logs" / STACKS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(path, "a", buffering=1, encoding="utf-8")
    except OSError:
        return False

    def arm():
        try:
            faulthandler.cancel_dump_traceback_later()
            if path.stat().st_size < STACKS_MAX_BYTES:
                faulthandler.dump_traceback_later(TICK + LATE, repeat=False, file=stream)
        except Exception:
            pass

    def tick(expected):
        now = loop.time()
        late = now - expected
        if late > LATE:
            try:
                stream.write(f"--- loop back {time.strftime('%Y-%m-%d %H:%M:%S')} after a {late:.1f}s stall ---\n")
            except Exception:
                pass
        arm()
        loop.call_later(TICK, tick, now + TICK)

    def first():
        arm()
        loop.call_later(TICK, tick, loop.time() + TICK)

    loop.call_soon_threadsafe(first)
    log.info("hermes-hq-push: freeze recorder on (%s)", path)
    return True
