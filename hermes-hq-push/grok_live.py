"""Grok Live voice tokens (0.9.26, ``voice.grok_live`` in config.yaml): the app's voice session (a WebSocket) talks
to xAI's Realtime API directly, so this only mints the short-lived ephemeral token (``POST
.../realtime/client_secrets``) xAI's own guide asks for — the bearer that authorizes it never reaches the phone.

Two routes, both behind the dashboard's normal login like everything else in ``dashboard/plugin_api.py``:

- ``GET /voice/grok-live/status`` is a cheap, network-free probe (``tools.xai_http.has_xai_credentials()``) so the
  app can show whether Grok Live can start at all, and which kind of credential would be used: the owner's xAI
  subscription (OAuth, ``hermes auth xai``) or a plain ``XAI_API_KEY``.
- ``POST /voice/grok-live/session`` resolves real credentials (``tools.xai_http.resolve_xai_http_credentials``,
  the same chain the rest of Hermes's direct xAI calls use: subscription OAuth first, ``XAI_API_KEY`` as the
  fallback) and asks xAI for a token good for a few minutes. A subscription bearer that's gone stale gets one forced
  refresh; if xAI still refuses it and an API key exists, that's tried too, so a phone doesn't see a stale-token
  error the next request would have avoided.

Both routes run under the bot's own profile (``?profile=``, the same scope every other route here uses,
``hermes_cli.web_server_profiles._config_profile_scope``) so a second bot's own xAI credentials and
``voice.grok_live`` settings are the ones read, not the dashboard's own. A bot with no xAI login or key of its own
borrows the main bot's (0.9.27): the person signs in to Grok once (``hermes auth add xai-oauth``) and every bot on the
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


def _xai_auth_kind() -> str | None:
    """``"subscription"`` if an xai-oauth token exists in auth.json (``providers.xai-oauth.tokens.access_token``,
    or any ``credential_pool.xai-oauth`` entry), else ``"api_key"`` if ``XAI_API_KEY`` resolves, else ``None``.
    Mirrors ``tools.xai_http.has_xai_credentials``'s own checks, split by which one matched; never raises."""
    try:
        import json
        from hermes_constants import get_hermes_home
        auth_path = get_hermes_home() / "auth.json"
        if auth_path.exists():
            store = json.loads(auth_path.read_text(encoding="utf-8-sig"))
            providers = store.get("providers") if isinstance(store, dict) else None
            tokens = (providers or {}).get("xai-oauth") if isinstance(providers, dict) else None
            tokens = (tokens or {}).get("tokens") if isinstance(tokens, dict) else None
            if isinstance(tokens, dict) and str(tokens.get("access_token") or "").strip():
                return "subscription"
            pool = store.get("credential_pool") if isinstance(store, dict) else None
            entries = (pool or {}).get("xai-oauth") if isinstance(pool, dict) else None
            if isinstance(entries, list) and any(
                    isinstance(e, dict) and str(e.get("access_token") or "").strip() for e in entries):
                return "subscription"
    except Exception:
        log.debug("hermes-hq-push: grok-live oauth probe failed", exc_info=True)
    try:
        from agent.secret_scope import get_secret
        if str(get_secret("XAI_API_KEY", "") or "").strip():
            return "api_key"
    except Exception:
        log.debug("hermes-hq-push: grok-live api key probe failed", exc_info=True)
    return None


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
            from tools.xai_http import has_xai_credentials
        except Exception:
            return {"ok": True, "available": False, "auth": None, "model": model, "voice": voice,
                    "reason": "unsupported_hermes"}
        if _has_credentials(has_xai_credentials):
            return {"ok": True, "available": True, "auth": _xai_auth_kind(), "model": model, "voice": voice}
    if profile:
        with _scope(""):
            if _has_credentials(has_xai_credentials):
                return {"ok": True, "available": True, "auth": _xai_auth_kind(), "model": model, "voice": voice,
                        "shared": True}
    return {"ok": True, "available": False, "auth": None, "model": model, "voice": voice, "reason": "no_credentials"}


def _has_credentials(probe) -> bool:
    try:
        return bool(probe())
    except Exception:
        log.warning("hermes-hq-push: grok-live status probe failed", exc_info=True)
        return False


def _credentials_profile(profile: str) -> str:
    """The profile whose xAI login mints the token: the bot's own, else the main bot's (``""``) when the bot has
    none. Network-free, like ``status``."""
    if not profile:
        return profile
    try:
        from tools.xai_http import has_xai_credentials
    except Exception:
        return profile
    with _scope(profile):
        if _has_credentials(has_xai_credentials):
            return profile
    with _scope(""):
        if _has_credentials(has_xai_credentials):
            return ""
    return profile


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

    with _scope(_credentials_profile(profile)):
        try:
            creds = resolve_xai_http_credentials()
        except Exception:
            log.warning("hermes-hq-push: grok-live credential resolve failed", exc_info=True)
            raise GrokLiveError(503, "no_credentials") from None
        if not str(creds.get("api_key") or "").strip():
            raise GrokLiveError(503, "no_credentials")

        response = _post_client_secret(creds, hermes_xai_default_headers)
        if response.status_code == 401 and creds.get("provider") == "xai-oauth":
            rejected = str(creds.get("api_key") or "")
            try:
                refreshed = resolve_xai_http_credentials(force_refresh=True, api_key_hint=rejected)
            except Exception:
                refreshed = None
            if refreshed and str(refreshed.get("api_key") or "").strip():
                creds = refreshed
                response = _post_client_secret(creds, hermes_xai_default_headers)
        if response.status_code in (401, 403):
            try:
                fallback = resolve_xai_http_credentials(prefer_api_key=True)
            except Exception:
                fallback = None
            fallback_key = str((fallback or {}).get("api_key") or "").strip()
            if fallback_key and fallback_key != str(creds.get("api_key") or "").strip():
                creds = fallback
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
        auth = "subscription" if creds.get("provider") == "xai-oauth" else "api_key"
        base_url = str(creds.get("base_url") or DEFAULT_BASE_URL)
        return {"ok": True, "client_secret": value, "expires_at": expires_at, "auth": auth,
                "url": ws_url(base_url, model), "model": model, "voice": voice, "instructions": instructions}
