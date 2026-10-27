"""Lets a Nous sign-in delete, archive and rename the chats it made (Hermes 0.22.5, and main as of 2026-10-24).

``hermes serve`` stamps each chat it creates with the login that made it, as ``nous:<user>``: the issuer is dropped
(``tui_gateway/session_transports.py``). An edit from that same login reaches the gateway as
``auth:v1:["nous", "<portal>", "<user>"]``, and ``hermes_state_mutation_binding._login_label`` maps only an
issuer-less login to its stamp, so every chat the Hermes HQ app made answered ``permission_denied`` (2026-10-24).

The Nous dashboard provider accepts only access tokens whose ``iss`` is its configured portal, so on this computer a
``nous:<user>`` stamp can only have come from that issuer. ``install`` wraps ``_login_label`` to map exactly that
issuer as well. Every other login (another issuer, another provider, another user) gets Hermes's own answer,
unchanged. When Hermes already maps it (fixed upstream), or the function is gone (a reworked Hermes), ``install``
leaves Hermes alone.

Chats made under the old dashboard password login are stamped ``basic:<username>`` (0.9.3). Only that password login
owns them, so once the password login is turned off (``dashboard.basic_auth.username`` empty), nothing can delete,
archive or rename them. A Nous login can't simply be mapped to them: the label says nothing about which person typed
the password, and on a shared gateway every Nous user Portal lets in would then own them. So nothing changes unless
the computer's owner names the one login that takes them over, in config.yaml (``hermes_hq.password_chats_owner``)
or ``HERMES_HQ_PASSWORD_CHATS_OWNER``, e.g. ``nous:<user id>``. Whoever can write that file can already rewrite
state.db, so naming a login hands over nothing they couldn't take. ``install_password_chats`` then lets exactly that
login past Hermes's owner check on unclaimed ``basic:`` chats, and Hermes's own rules (routes, sources, an earlier
claim, ``session:create``) still apply. The first edit claims the chat for that login, as Hermes does for every
cold chat, so afterwards Hermes alone decides.
"""
import logging
import os

log = logging.getLogger("hermes-hq-push")

DEFAULT_PORTAL = "https://portal.nousresearch.com"
MARK = "_hermes_hq_own_chats"
PASSWORD_MARK = "_hermes_hq_password_chats"
PASSWORD_PREFIX = "basic:"


def portal() -> str:
    """The issuer Hermes verifies Nous logins against: ``HERMES_DASHBOARD_PORTAL_URL``, else
    ``dashboard.oauth.portal_url``, else the Nous portal (``plugins/dashboard_auth/nous``). A different
    answer here than Hermes's only means no login maps; it can never let a login Hermes didn't verify in."""
    value = os.environ.get("HERMES_DASHBOARD_PORTAL_URL", "").strip()
    if not value:
        try:
            from hermes_cli.config import load_config
            dashboard = (load_config() or {}).get("dashboard") or {}
            oauth = dashboard.get("oauth") if isinstance(dashboard, dict) else None
            value = str((oauth or {}).get("portal_url") or "").strip() if isinstance(oauth, dict) else ""
        except Exception:
            value = ""
    return (value or DEFAULT_PORTAL).rstrip("/")


def install() -> str:
    """Wrap ``_login_label`` once per process. Returns "installed", "not needed" or "absent"."""
    try:
        import hermes_state_mutation_binding as binding
        from gateway.session_identity import authenticated_subject, parse_authenticated_subject
    except Exception:
        return "absent"
    original = getattr(binding, "_login_label", None)
    if not callable(original):
        return "absent"
    if getattr(original, MARK, False):
        return "installed"
    pinned = portal()
    try:
        if original(authenticated_subject({"provider": "nous", "issuer": pinned, "user_id": "probe"})) == "nous:probe":
            return "not needed"
    except Exception:
        return "absent"

    def _login_label(subject):
        label = original(subject)
        if label is not None:
            return label
        parts = parse_authenticated_subject(subject)
        if parts is None:
            return None
        provider, issuer, user = parts
        if provider != "nous" or issuer != pinned or not user.strip():
            return None
        return f"nous:{user.strip()}"

    _login_label.__doc__ = original.__doc__
    _login_label.__wrapped__ = original
    setattr(_login_label, MARK, True)
    binding._login_label = _login_label
    return "installed"


def password_chats_owner() -> str | None:
    """The login named to take over the old password login's chats: ``HERMES_HQ_PASSWORD_CHATS_OWNER``, else
    ``hermes_hq.password_chats_owner`` in config.yaml; a ``<provider>:<user>`` label as Hermes stamps chats
    (``nous:<user id>``), else None. Unset, empty or malformed means nobody: Hermes's answer stands."""
    value = os.environ.get("HERMES_HQ_PASSWORD_CHATS_OWNER", "").strip()
    if not value:
        try:
            from hermes_cli.config import load_config
            section = (load_config() or {}).get("hermes_hq") or {}
            value = str(section.get("password_chats_owner") or "").strip() if isinstance(section, dict) else ""
        except Exception:
            value = ""
    provider, colon, user = value.partition(":")
    if not colon or not provider.strip() or not user.strip() or value.startswith("auth:"):
        return None
    return f"{provider.strip()}:{user.strip()}"


def install_password_chats() -> str:
    """Wrap ``authorize_history`` once per process so the named login owns unclaimed ``basic:`` chats.
    Returns "installed", "off" (no login named) or "absent" (a reworked Hermes)."""
    owner = password_chats_owner()
    if owner is None:
        return "off"
    try:
        import json
        import hermes_state_mutation_binding as binding
        from hermes_state_runtime import RuntimeStoreError
    except Exception:
        return "absent"
    original = getattr(binding, "authorize_history", None)
    sources = getattr(binding, "_history_sources", None)
    prefix = getattr(binding, "BINDING_PREFIX", None)
    if not callable(original) or not callable(sources) or not callable(getattr(binding, "_login_label", None)) \
            or not isinstance(prefix, str):
        return "absent"
    if getattr(original, PASSWORD_MARK, False):
        return "installed"

    def handed_over(conn, actor, session_id) -> bool:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return False
        stamp = row["user_id"]
        if not isinstance(stamp, str) or not stamp.startswith(PASSWORD_PREFIX) or not stamp[len(PASSWORD_PREFIX):].strip():
            return False
        if row["session_key"] or row["chat_id"] or row["origin_json"] or row["source"] not in sources():
            return False
        if conn.execute("SELECT 1 FROM state_meta WHERE key=?", (prefix + session_id,)).fetchone() is not None:
            return False   # claimed already: Hermes's binding decides
        if "session:create" not in actor.capabilities:
            return False
        # Through the module, so a Nous login maps as install() taught it.
        return binding._login_label(actor.subject) == owner

    def authorize_history(conn, actor, session_id, *, claim=False):
        try:
            return original(conn, actor, session_id, claim=claim)
        except RuntimeStoreError as refused:
            if refused.reason != "permission_denied" or not handed_over(conn, actor, session_id):
                raise
        if claim:
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (
                prefix + session_id, json.dumps([actor.profile_id, actor.subject], separators=(",", ":"))))
        return True

    authorize_history.__doc__ = original.__doc__
    authorize_history.__wrapped__ = original
    setattr(authorize_history, PASSWORD_MARK, True)
    binding.authorize_history = authorize_history
    return "installed"
