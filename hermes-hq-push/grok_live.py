"""Grok Live voice tokens (0.9.26, ``voice.grok_live`` in config.yaml): the app's voice session (a WebSocket) talks
to xAI's Realtime API directly, so this only mints the short-lived ephemeral token (``POST
.../realtime/client_secrets``) xAI's own guide asks for — the bearer that authorizes it never reaches the phone.

Two routes, both behind the dashboard's normal login like everything else in ``dashboard/plugin_api.py``:

- ``GET /voice/grok-live/status`` is a cheap, network-free probe (a Grok sign-in in ``auth.json``, the bot's own or
  the main bot's) so the app can show whether Grok Live can start at all.
- ``POST /voice/grok-live/session`` resolves real credentials (``tools.xai_http.resolve_xai_http_credentials``)
  and asks xAI for a token good for a few minutes. Grok Live runs on the owner's Grok subscription only (0.9.35,
  owner 2026-11-04: "It should be using my subscription"): an ``XAI_API_KEY`` is never used for it, not even as a
  fallback. A subscription bearer that's gone stale gets one forced refresh; still refused, the phone is told so.

Both routes run under the bot's own profile (``?profile=``, the same scope every other route here uses,
``hermes_cli.web_server_profiles._config_profile_scope``) so a second bot's own xAI credentials and
``voice.grok_live`` settings are the ones read, not the dashboard's own. A bot with no xAI login or key of its own
borrows the main bot's Grok sign-in (0.9.27; Hermes's credential pool already does for minting, the status
probe now says so too): the person signs in to Grok once (``hermes auth add xai-oauth``) and every bot on the
computer can talk with it, each still with its own name, voice and instructions. Status says ``"shared": true`` then.

Hermes imports are wrapped in ``try/except``: an older Hermes without ``tools.xai_http`` (or without xAI OAuth at
all, e.g. a Windows build that predates it) answers ``available: false, reason: "unsupported_hermes"`` rather than
500ing — the add-on has to load on every Hermes version. The session route's one HTTP call runs behind a plain
``def`` route (FastAPI/Starlette hands a sync path function to its thread pool automatically), so a slow or stuck
xAI request never blocks this server's event loop for every other bot.
"""
import logging

log = logging.getLogger("hermes-hq-push")

DEFAULT_MODEL = "grok-voice-latest"
DEFAULT_VOICE = "eve"
DEFAULT_BASE_URL = "https://api.x.ai/v1"
EXPIRES_AFTER_SECONDS = 300
SESSION_TIMEOUT = 20.0
BODY_LIMIT = 300  # how much of xAI's error body ever reaches the app

# Appended to the voice persona (tools/voice_live.py's live_persona/live_instructions), after any config.yaml
# voice.grok_live.instructions: the one thing Grok Live itself can't do is run Hermes's tools.
ASK_AGENT_NOTE = (
    "For anything beyond small talk, call the ask_agent tool with the person's full request in their words, then "
    "say the result it returns in your own words, briefly. While waiting, say at most one short line. Never "
    "invent results. No backchannels."
)


class GrokLiveError(Exception):
    """A refusal the route answers with ``status_code`` and a plain ``detail``."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


# ---------------------------------------------------------------------------
# voice.grok_live config.yaml section — pure, no Hermes imports
# ---------------------------------------------------------------------------

def section_of(config: dict) -> dict:
    voice = config.get("voice") if isinstance(config, dict) else None
    section = (voice or {}).get("grok_live") if isinstance(voice, dict) else None
    return section if isinstance(section, dict) else {}


def model_of(section: dict) -> str:
    return str(section.get("model") or "").strip() or DEFAULT_MODEL


def voice_of(section: dict) -> str:
    return str(section.get("voice") or "").strip() or DEFAULT_VOICE


def instructions_of(section: dict, persona: str) -> str:
    extra = str(section.get("instructions") or "").strip()
    parts = [persona, ASK_AGENT_NOTE]
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


def ws_url(base_url: str, model: str) -> str:
    """``wss://api.x.ai/v1/realtime?model=...`` from whatever base URL the credentials carry (an
    ``HERMES_XAI_BASE_URL`` override stays honored)."""
    base = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/realtime?model={model}"


def body_snippet(text) -> str:
    return str(text or "")[:BODY_LIMIT]


# ---------------------------------------------------------------------------
# Hermes's own pieces, under the bot's profile
# ---------------------------------------------------------------------------

def _scope(profile: str):
    from hermes_cli.web_server_profiles import _config_profile_scope
    return _config_profile_scope(profile)


def _config_section() -> dict:
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except Exception:
        config = {}
    return section_of(config if isinstance(config, dict) else {})


def _own_xai_login() -> bool:
    """This bot's own Grok sign-in: an xai-oauth token in its auth.json (``providers.xai-oauth.tokens.access_token``,
    or any ``credential_pool.xai-oauth`` entry). An ``XAI_API_KEY`` doesn't count: Grok Live is subscription only."""
    try:
        from hermes_constants import get_hermes_home
        return _oauth_in(get_hermes_home() / "auth.json")
    except Exception:
        log.debug("hermes-hq-push: grok-live oauth probe failed", exc_info=True)
        return False


def _persona() -> str:
    """Hermes's own voice persona for this bot (``tools/voice_live.py``'s ``live_persona``/``live_bot_name``), for
    an older Hermes that still answers Grok Live status but doesn't carry that module yet."""
    try:
        from tools.voice_live import live_bot_name, live_persona
        return live_persona(live_bot_name())
    except Exception:
        log.debug("hermes-hq-push: grok-live persona fallback", exc_info=True)
        return (f"You are {_fallback_bot_name()}, a calm and friendly voice assistant. Speak naturally and "
                "briefly, and never claim an action succeeded before it actually did.")


def _fallback_bot_name() -> str:
    try:
        from hermes_constants import get_hermes_home
        from tools.bot_mode_probe import _display_name, _profile_name
        home = get_hermes_home()
        name = _profile_name(home)
        return _display_name(name, home) or name
    except Exception:
        return "Assistant"  # never "Hermes" — this is the bot speaking, not the product


def status(profile: str = "") -> dict:
    """``{"ok": True, "available", "auth", "model", "voice", "reason"?}``. Never touches the network."""
    with _scope(profile):
        section = _config_section()
        model, voice = model_of(section), voice_of(section)
        try:
            from tools.xai_http import resolve_xai_http_credentials  # noqa: F401 — an older Hermes has no xAI calls
        except Exception:
            return {"ok": True, "available": False, "auth": None, "model": model, "voice": voice,
                    "reason": "unsupported_hermes"}
        if _own_xai_login():
            return {"ok": True, "available": True, "auth": "subscription", "model": model, "voice": voice}
    # Not ``if profile``: the dashboard may take ``?profile=`` itself and hand the route an empty one while the
    # request already runs under that bot's home. The main bot never gets here (its own probe said yes).
    if _main_bot_has_xai_login():
        return {"ok": True, "available": True, "auth": "subscription", "model": model, "voice": voice, "shared": True}
    return {"ok": True, "available": False, "auth": None, "model": model, "voice": voice, "reason": "no_subscription"}


def _main_bot_has_xai_login() -> bool:
    """The main bot's Grok sign-in (``auth.json`` at the Hermes root). Hermes's own credential pool already falls
    back to it for a bot without one (``agent/credential_pool.py``, the global-root fallback), so the session route
    mints with it. Read by path, not by profile
    scope: inside a ``?profile=B`` request an empty or ``default`` scope stays on B."""
    try:
        from hermes_constants import get_default_hermes_root
        return _oauth_in(get_default_hermes_root() / "auth.json")
    except Exception:
        log.debug("hermes-hq-push: grok-live main bot probe failed", exc_info=True)
        return False


def _oauth_in(auth_path) -> bool:
    import json
    if not auth_path.exists():
        return False
    store = json.loads(auth_path.read_text(encoding="utf-8-sig"))
    providers = store.get("providers") if isinstance(store, dict) else None
    tokens = (providers or {}).get("xai-oauth") if isinstance(providers, dict) else None
    tokens = (tokens or {}).get("tokens") if isinstance(tokens, dict) else None
    if isinstance(tokens, dict) and str(tokens.get("access_token") or "").strip():
        return True
    pool = store.get("credential_pool") if isinstance(store, dict) else None
    entries = (pool or {}).get("xai-oauth") if isinstance(pool, dict) else None
    return isinstance(entries, list) and any(
        isinstance(e, dict) and str(e.get("access_token") or "").strip() for e in entries)


def _post_client_secret(creds: dict, headers_fn) -> "object":
    import httpx
    base_url = str(creds.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    api_key = str(creds.get("api_key") or "").strip()
    headers = {**headers_fn(), "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    return httpx.post(f"{base_url}/realtime/client_secrets", headers=headers,
                      json={"expires_after": {"seconds": EXPIRES_AFTER_SECONDS}}, timeout=SESSION_TIMEOUT)


def _body_text(response) -> str:
    try:
        return body_snippet(response.text)
    except Exception:
        return ""


def mint_session(profile: str = "") -> dict:
    """Mint an xAI Realtime ephemeral token for this bot's profile, or raise ``GrokLiveError``.

    ``{"ok": True, "client_secret", "expires_at", "auth", "url", "model", "voice", "instructions"}``. Runs the
    blocking HTTP itself — callers (the route) must not run this on the event loop."""
    with _scope(profile):
        try:
            from tools.xai_http import hermes_xai_default_headers, resolve_xai_http_credentials
        except Exception:
            raise GrokLiveError(503, "no_credentials") from None

        section = _config_section()
        model, voice = model_of(section), voice_of(section)
        instructions = instructions_of(section, _persona())

        try:
            creds = resolve_xai_http_credentials()
        except Exception:
            log.warning("hermes-hq-push: grok-live credential resolve failed", exc_info=True)
            raise GrokLiveError(503, "no_subscription") from None
        # Subscription only: a bot whose chain lands on XAI_API_KEY has no Grok sign-in to use.
        if creds.get("provider") != "xai-oauth" or not str(creds.get("api_key") or "").strip():
            raise GrokLiveError(503, "no_subscription")

        response = _post_client_secret(creds, hermes_xai_default_headers)
        if response.status_code == 401 and creds.get("provider") == "xai-oauth":
            rejected = str(creds.get("api_key") or "")
            try:
                refreshed = resolve_xai_http_credentials(force_refresh=True, api_key_hint=rejected)
            except Exception:
                refreshed = None
            if (refreshed and refreshed.get("provider") == "xai-oauth"
                    and str(refreshed.get("api_key") or "").strip()):
                creds = refreshed
                response = _post_client_secret(creds, hermes_xai_default_headers)

        if response.status_code >= 400:
            raise GrokLiveError(502, f"xAI said {response.status_code}: {_body_text(response)}")
        try:
            payload = response.json()
        except Exception:
            payload = None
        value = payload.get("value") if isinstance(payload, dict) else None
        if not value:
            raise GrokLiveError(502, f"xAI said {response.status_code}: {_body_text(response)}")
        expires_at = payload.get("expires_at") if isinstance(payload, dict) else None
        auth = "subscription"
        base_url = str(creds.get("base_url") or DEFAULT_BASE_URL)
        return {"ok": True, "client_secret": value, "expires_at": expires_at, "auth": auth,
                "url": ws_url(base_url, model), "model": model, "voice": voice, "instructions": instructions}
