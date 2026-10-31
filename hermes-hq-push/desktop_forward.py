"""The app can send into a chat the desktop app has open (0.9.16).

The desktop app runs its chats in the gateway (``hermes gateway ensure`` attaches to it), and a chat it has open holds
Hermes's one-writer lease for as long as its tile stays open (``tui_gateway/session_lifecycle.py``). ``hermes serve``,
the process the app talks to, then refuses the app's message ``4090 SESSION_NOT_OWNED``: "This chat is open in another
Hermes window". Tester Polo, 2026-10-30, with the chat open in the desktop app.

Hermes already has the answer for a chat the gateway is working on: one owner, one queue, and clients that submit
(``tui_gateway/owner_forward.py``, RT-37). ``hermes serve`` hands the message to the gateway's queue, the gateway runs
it in that chat after anything already running, and the reply streams back to the app. Hermes uses that only when the
gateway's ledger holds work for the chat. ``install`` also uses it when the chat's holder IS the gateway's process
(a desktop app tile, or anything else the gateway runs), so the desktop app sees the message arrive in its open chat.
Any other holder (a terminal, another ``hermes serve``, a scheduled job's one-shot run) keeps Hermes's refusal.

Second fix: Hermes's hand-off opened its connection to the gateway with the websockets default 1 MiB frame limit, and
the gateway's ``session.resume`` answer for a long chat is bigger (a 210k-token bot chat). The hand-off closed with
"frame exceeds limit" and the message was refused, every time, on every long bot chat (52 times on 2026-10-30 on the
owner's computer). ``install_frame_limit`` gives it the 8 MiB Hermes's own gateway client uses, and more.
"""
import contextlib
import logging
import os
from pathlib import Path

log = logging.getLogger("hermes-hq-push")

MARK = "_hermes_hq_desktop_forward"
FRAME_MARK = "_hermes_hq_owner_frame_limit"
#: The hand-off's frame limit. A resume answer carries the chat's history; 64 MiB is far past any chat Hermes keeps.
FRAME_LIMIT = 64 * 1024 * 1024
#: The ledger state the hand-off is given for a chat the gateway holds but has no ledger work for: a running owner
#: that ends its own turns (``owner_forward.FORWARDABLE_LEDGER_STATES``).
FORWARD_STATE = "running"


class _GatewayHeld(str):
    """Hermes's own refusal, read as one its hand-off may take: same words, same reason, a ledger state."""

    reason: str
    ledger_status: str

    def __new__(cls, refusal, state: str):
        obj = super().__new__(cls, str(refusal))
        obj.reason = getattr(refusal, "reason", "SESSION_NOT_OWNED")
        obj.ledger_status = state
        return obj


def _holder(key: str, home) -> dict | None:
    """The live lease on ``key`` in ``home``'s registry, or None."""
    from hermes_cli.active_sessions import active_session_registry_snapshot
    for entry in active_session_registry_snapshot(home):
        if str(entry.get("session_id") or "") == key:
            return entry
    return None


def _gateway_pids(home) -> set[int]:
    """The pids of the gateway serving ``home``: its own, and the root's when a multiplexer serves the profile."""
    from gateway.status import live_gateway_pid_for_home
    pids = set()
    homes = [Path(home)]
    with contextlib.suppress(Exception):
        from hermes_constants import get_default_hermes_root
        homes.append(Path(get_default_hermes_root()))
    for candidate in homes:
        with contextlib.suppress(Exception):
            pid = live_gateway_pid_for_home(candidate)
            if pid:
                pids.add(int(pid))
    return pids


def held_by_gateway(session: dict, refusal) -> bool:
    """Whether a ``SESSION_NOT_OWNED`` refusal of this process's turn names a holder in the gateway's process."""
    if getattr(refusal, "reason", None) != "SESSION_NOT_OWNED" or getattr(refusal, "ledger_status", None) is not None:
        return False
    key = str(session.get("session_key") or "")
    home = session.get("profile_home")
    if not key:
        return False
    if home is None:   # the registry Hermes's claim used (``try_acquire_active_session`` without registry_home)
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    holder = _holder(key, home)
    if holder is None:
        return False
    try:
        pid = int(holder.get("pid") or -1)
    except (TypeError, ValueError):
        return False
    return pid != os.getpid() and pid in _gateway_pids(home)


def install() -> str:
    """Wrap Hermes's ``_slot_refusal`` once per process. Returns "installed", "already" or "absent".

    The prompt handlers are written in ``methods_prompt`` but run rebound onto ``tui_gateway.server``'s globals
    (``method_ctx.bind_module``), so the name they call is the server module's."""
    try:
        from tui_gateway import owner_forward, server as methods_prompt
    except Exception:
        return "absent"
    original = getattr(methods_prompt, "_slot_refusal", None)
    if original is None or not hasattr(methods_prompt, "_submit_to_ledger_owner") \
            or FORWARD_STATE not in getattr(owner_forward, "FORWARDABLE_LEDGER_STATES", ()):
        return "absent"
    if getattr(original, MARK, False):
        return "already"

    def _slot_refusal(rid, sid, session, text, params, refusal, internal_hosted_submit):
        if not internal_hosted_submit:
            try:
                gateway_held = held_by_gateway(session, refusal)
            except Exception:
                log.debug("hermes-hq-push: could not read who holds %s", session.get("session_key"), exc_info=True)
                gateway_held = False
            if gateway_held:
                log.info("hermes-hq-push: %s is open in the gateway (desktop app); handing the app's message to it",
                         session.get("session_key"))
                refusal = _GatewayHeld(refusal, FORWARD_STATE)
        return original(rid, sid, session, text, params, refusal, internal_hosted_submit)

    setattr(_slot_refusal, MARK, True)
    _slot_refusal.__wrapped__ = original
    methods_prompt._slot_refusal = _slot_refusal
    return "installed"


def install_frame_limit() -> str:
    """Give ``owner_forward.connect_owner`` a frame limit that fits a long chat's resume answer.
    Returns "installed", "already", "not needed" (Hermes sets one itself), "changed" (Hermes reworked the dial, so it
    is left alone) or "absent"."""
    try:
        import inspect
        from tui_gateway import owner_forward
    except Exception:
        return "absent"
    original = getattr(owner_forward, "connect_owner", None)
    if original is None:
        return "absent"
    if getattr(original, FRAME_MARK, False):
        return "already"
    try:
        source = inspect.getsource(original)
    except (OSError, TypeError):
        return "absent"
    if "max_size" in source:
        return "not needed"
    if "connect(url, subprotocols=protocols, open_timeout=10, proxy=None)" not in source:
        return "changed"   # Hermes reworked the dial: the copy below would drift from it

    @contextlib.asynccontextmanager
    async def connect_owner(home, endpoint):
        from hermes_cli.gateway_client import GATEWAY_WS_PROTOCOL, GatewayClient, _session_ticket, gateway_ws_target
        from websockets.asyncio.client import connect
        import asyncio

        ticket = await asyncio.to_thread(_session_ticket, home, endpoint)
        url, protocols = gateway_ws_target(endpoint, ticket)
        async with connect(url, subprotocols=protocols, open_timeout=10, proxy=None, max_size=FRAME_LIMIT) as ws:
            if ws.subprotocol != GATEWAY_WS_PROTOCOL:
                raise owner_forward.OwnerUnavailable("the gateway answered with another protocol")
            async with GatewayClient(ws) as client:
                yield client

    setattr(connect_owner, FRAME_MARK, True)
    connect_owner.__wrapped__ = original
    owner_forward.connect_owner = connect_owner
    return "installed"
