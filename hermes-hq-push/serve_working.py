"""Which chats ``hermes serve`` itself is running a turn in right now (0.9.20, ``serveWorking`` in /status).

``/sessions/working`` (session_working.py) reads Hermes's durable ledger, which holds the messaging gateway's work: a
message from another bot, Telegram, a job's hand-off. A turn you start by typing in Hermes HQ or the desktop app runs
in this server instead, and Hermes keeps that only in memory (``tui_gateway.server._sessions``, ``running`` set while
the turn runs). So a host restart that waited for ``/sessions/working`` to be empty still cut off a reply typed in the
app (owner, 2026-10-31: the app bot stopped mid-step while it judged 20 icons; the restart script saw nothing working).
This read is that in-memory flag. The route runs in the same process, so it only looks: it never imports the gateway
into a process that hasn't loaded it.
"""
import sys


def working_in(sessions: dict, profile_for_home=None) -> list:
    """[{id, runtime, profile}] for each running entry of a ``_sessions``-shaped dict (runtime id -> session dict)."""
    out = []
    for runtime, sess in sessions.items():
        if not isinstance(sess, dict) or not sess.get("running"):
            continue
        profile = ""
        if profile_for_home is not None:
            try:
                profile = profile_for_home(sess.get("profile_home")) or ""
            except Exception:
                profile = ""
        out.append({"id": str(sess.get("session_key") or ""), "runtime": str(runtime), "profile": profile})
    return sorted(out, key=lambda r: (r["profile"], r["id"], r["runtime"]))


def hermes_working():
    """The running chats of this server, or None when this process isn't serving chats (no gateway loaded)."""
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return None
    lock = getattr(server, "_sessions_lock", None)
    sessions = getattr(server, "_sessions", None)
    if lock is None or not isinstance(sessions, dict):
        return None
    with lock:
        snapshot = dict(sessions)
    profile_for_home = getattr(server, "profile_name_for_home", None)
    return working_in(snapshot, profile_for_home)
