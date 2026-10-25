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
"""
import logging
import os

log = logging.getLogger("hermes-hq-push")

DEFAULT_PORTAL = "https://portal.nousresearch.com"
MARK = "_hermes_hq_own_chats"


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
