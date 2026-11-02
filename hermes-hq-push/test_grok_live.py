"""Grok Live voice tokens (0.9.26): config.yaml's voice.grok_live section, the credential probe, minting a
session against a mocked xAI, and the /voice/grok-live routes. ``~/.hermes/hermes-agent/.venv/bin/python
-m pytest -q test_grok_live.py`` from the plugin folder. conftest.py pins HERMES_HOME to a throwaway folder
first; nothing here ever reaches the real xAI (httpx.post is always mocked) or the computer's own ~/.hermes."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

import httpx  # noqa: E402
import tools.xai_http as xai_http  # noqa: E402
from hermes_constants import get_default_hermes_root  # noqa: E402

HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("grok_live_under_test", HERE / "grok_live.py")
grok_live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grok_live)

ROOT = get_default_hermes_root()
assert ROOT.resolve() != (Path.home() / ".hermes").resolve()


def profile_home(name):
    home = ROOT if name == "default" else ROOT / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    return home


class FakeResponse:
    def __init__(self, status_code, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


# ---------------------------------------------------------------------------
# Pure config/words helpers — no Hermes involved
# ---------------------------------------------------------------------------

def test_section_of_reads_voice_grok_live_and_tolerates_junk():
    assert grok_live.section_of({"voice": {"grok_live": {"model": "m"}}}) == {"model": "m"}
    assert grok_live.section_of({}) == {}
    assert grok_live.section_of({"voice": "nope"}) == {}
    assert grok_live.section_of({"voice": {"grok_live": "nope"}}) == {}
    assert grok_live.section_of("nope") == {}


def test_model_and_voice_defaults_and_overrides():
    assert grok_live.model_of({}) == grok_live.DEFAULT_MODEL == "grok-voice-latest"
    assert grok_live.voice_of({}) == grok_live.DEFAULT_VOICE == "eve"
    assert grok_live.model_of({"model": "grok-x"}) == "grok-x"
    assert grok_live.voice_of({"voice": "nova"}) == "nova"
    assert grok_live.model_of({"model": "  "}) == grok_live.DEFAULT_MODEL  # blank falls back


def test_instructions_append_the_ask_agent_note_then_any_extra():
    only_persona = grok_live.instructions_of({}, "Persona.")
    assert only_persona == f"Persona.\n\n{grok_live.ASK_AGENT_NOTE}"
    with_extra = grok_live.instructions_of({"instructions": "Keep it upbeat."}, "Persona.")
    assert with_extra == f"Persona.\n\n{grok_live.ASK_AGENT_NOTE}\n\nKeep it upbeat."


def test_ws_url_flips_the_scheme_and_keeps_the_host():
    assert grok_live.ws_url("https://api.x.ai/v1", "grok-voice-latest") == \
        "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
    assert grok_live.ws_url("http://localhost:8080/v1", "m") == "ws://localhost:8080/v1/realtime?model=m"
    assert grok_live.ws_url("", "m") == "wss://api.x.ai/v1/realtime?model=m"  # falls back to the default base


def test_body_snippet_truncates_and_is_never_none():
    assert grok_live.body_snippet("x" * 400) == "x" * grok_live.BODY_LIMIT
    assert grok_live.body_snippet(None) == ""


# ---------------------------------------------------------------------------
# GET status — never touches the network
# ---------------------------------------------------------------------------

def test_status_reports_subscription_when_an_oauth_token_exists(monkeypatch):
    home = profile_home("grok-status-sub")
    (home / "config.yaml").write_text("voice:\n  grok_live:\n    model: grok-test\n    voice: nova\n")
    (home / "auth.json").write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {"access_token": "tok"}}}}))
    monkeypatch.setattr(xai_http, "has_xai_credentials", lambda: True)
    assert grok_live.status("grok-status-sub") == {
        "ok": True, "available": True, "auth": "subscription", "model": "grok-test", "voice": "nova"}


def test_status_reports_subscription_from_a_pool_entry_too(monkeypatch):
    home = profile_home("grok-status-sub-pool")
    (home / "config.yaml").write_text("voice:\n  grok_live: {}\n")
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"xai-oauth": [{"access_token": "p1"}]}}))
    monkeypatch.setattr(xai_http, "has_xai_credentials", lambda: True)
    result = grok_live.status("grok-status-sub-pool")
    assert result["available"] is True and result["auth"] == "subscription"


def test_status_reports_api_key_when_only_an_env_key_is_set(monkeypatch):
    home = profile_home("grok-status-key")
    (home / "config.yaml").write_text("voice:\n  grok_live: {}\n")
    (home / ".env").write_text("XAI_API_KEY=sk-test\n")
    monkeypatch.setattr(xai_http, "has_xai_credentials", lambda: True)
    result = grok_live.status("grok-status-key")
    assert result == {"ok": True, "available": True, "auth": "api_key",
                      "model": grok_live.DEFAULT_MODEL, "voice": grok_live.DEFAULT_VOICE}


def test_status_no_credentials(monkeypatch):
    home = profile_home("grok-status-none")
    (home / "config.yaml").write_text("voice:\n  grok_live: {}\n")
    monkeypatch.setattr(xai_http, "has_xai_credentials", lambda: False)
    assert grok_live.status("grok-status-none") == {
        "ok": True, "available": False, "auth": None, "model": grok_live.DEFAULT_MODEL,
        "voice": grok_live.DEFAULT_VOICE, "reason": "no_credentials"}


def test_status_probe_failure_is_treated_as_no_credentials(monkeypatch):
    home = profile_home("grok-status-raises")
    (home / "config.yaml").write_text("voice:\n  grok_live: {}\n")

    def boom():
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(xai_http, "has_xai_credentials", boom)
    result = grok_live.status("grok-status-raises")
    assert result["available"] is False and result["reason"] == "no_credentials"


def test_status_unsupported_hermes_when_xai_http_is_missing(monkeypatch):
    home = profile_home("grok-status-old")
    (home / "config.yaml").write_text("voice:\n  grok_live:\n    model: grok-old\n")
    monkeypatch.setitem(sys.modules, "tools.xai_http", None)
    assert grok_live.status("grok-status-old") == {
        "ok": True, "available": False, "auth": None, "model": "grok-old",
        "voice": grok_live.DEFAULT_VOICE, "reason": "unsupported_hermes"}


# ---------------------------------------------------------------------------
# Persona fallback — never names "Hermes"
# ---------------------------------------------------------------------------

def test_persona_falls_back_without_naming_hermes_when_voice_live_is_missing(monkeypatch):
    (profile_home("grok-persona-fallback") / "config.yaml").write_text("model: x\n")
    monkeypatch.setitem(sys.modules, "tools.voice_live", None)
    with grok_live._scope("grok-persona-fallback"):
        persona = grok_live._persona()
    assert "grok-persona-fallback" in persona
    assert "Hermes" not in persona


# ---------------------------------------------------------------------------
# POST session — the HTTP call and the credential resolvers are mocked
# ---------------------------------------------------------------------------

def test_mint_session_success_with_a_subscription_token(monkeypatch):
    profile_home("grok-session-ok")
    (profile_home("grok-session-ok") / "config.yaml").write_text(
        "voice:\n  grok_live:\n    model: grok-voice-latest\n    voice: eve\n    instructions: Be brief.\n")
    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials",
                        lambda **kw: {"provider": "xai-oauth", "api_key": "tok-1", "base_url": "https://api.x.ai/v1"})
    monkeypatch.setattr(xai_http, "hermes_xai_default_headers", lambda: {"User-Agent": "Hermes-Agent/test"})
    posted = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posted.append((url, headers, json, timeout))
        return FakeResponse(200, {"value": "secret-123", "expires_at": 1793623956})
    monkeypatch.setattr(httpx, "post", fake_post)

    result = grok_live.mint_session("grok-session-ok")
    assert result["ok"] is True
    assert result["client_secret"] == "secret-123" and result["expires_at"] == 1793623956
    assert result["auth"] == "subscription"
    assert result["url"] == "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
    assert result["model"] == "grok-voice-latest" and result["voice"] == "eve"
    assert grok_live.ASK_AGENT_NOTE in result["instructions"] and "Be brief." in result["instructions"]
    assert "tok-1" not in json_module_dump(result)

    assert len(posted) == 1
    url, headers, body, timeout = posted[0]
    assert url == "https://api.x.ai/v1/realtime/client_secrets"
    assert headers["Authorization"] == "Bearer tok-1" and headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == "Hermes-Agent/test"
    assert body == {"expires_after": {"seconds": 300}}
    assert timeout == 20.0


def json_module_dump(value) -> str:
    return json.dumps(value)


def test_mint_session_retries_once_after_401_with_a_refreshed_oauth_token(monkeypatch):
    profile_home("grok-session-401")
    (profile_home("grok-session-401") / "config.yaml").write_text("voice:\n  grok_live: {}\n")
    calls = []

    def resolver(*, force_refresh=False, api_key_hint=None, prefer_api_key=False):
        calls.append((force_refresh, api_key_hint, prefer_api_key))
        if force_refresh:
            return {"provider": "xai-oauth", "api_key": "tok-2", "base_url": "https://api.x.ai/v1"}
        return {"provider": "xai-oauth", "api_key": "tok-1", "base_url": "https://api.x.ai/v1"}
    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials", resolver)
    monkeypatch.setattr(xai_http, "hermes_xai_default_headers", lambda: {})
    responses = [FakeResponse(401, text="unauthorized"), FakeResponse(200, {"value": "secret-2", "expires_at": 111})]
    bearers = []

    def fake_post(url, headers=None, json=None, timeout=None):
        bearers.append(headers["Authorization"])
        return responses.pop(0)
    monkeypatch.setattr(httpx, "post", fake_post)

    result = grok_live.mint_session("grok-session-401")
    assert result["client_secret"] == "secret-2" and result["auth"] == "subscription"
    assert bearers == ["Bearer tok-1", "Bearer tok-2"]
    assert calls[0] == (False, None, False)
    assert calls[1] == (True, "tok-1", False)  # the rejected bearer is the hint, not whatever select() would pick


def test_mint_session_falls_back_to_the_api_key_when_the_refreshed_token_is_still_refused(monkeypatch):
    profile_home("grok-session-fallback")
    (profile_home("grok-session-fallback") / "config.yaml").write_text("voice:\n  grok_live: {}\n")

    def resolver(*, force_refresh=False, api_key_hint=None, prefer_api_key=False):
        if prefer_api_key:
            return {"provider": "xai", "api_key": "sk-fallback", "base_url": "https://api.x.ai/v1"}
        if force_refresh:
            return {"provider": "xai-oauth", "api_key": "tok-2", "base_url": "https://api.x.ai/v1"}
        return {"provider": "xai-oauth", "api_key": "tok-1", "base_url": "https://api.x.ai/v1"}
    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials", resolver)
    monkeypatch.setattr(xai_http, "hermes_xai_default_headers", lambda: {})
    responses = [FakeResponse(401, text="a"), FakeResponse(403, text="b"),
                FakeResponse(200, {"value": "secret-3", "expires_at": 222})]
    bearers = []

    def fake_post(url, headers=None, json=None, timeout=None):
        bearers.append(headers["Authorization"])
        return responses.pop(0)
    monkeypatch.setattr(httpx, "post", fake_post)

    result = grok_live.mint_session("grok-session-fallback")
    assert result["client_secret"] == "secret-3" and result["auth"] == "api_key"
    assert bearers == ["Bearer tok-1", "Bearer tok-2", "Bearer sk-fallback"]


def test_mint_session_gives_up_when_the_api_key_fallback_is_the_same_token(monkeypatch):
    """No XAI_API_KEY at all: prefer_api_key=True resolves back to the empty/oauth token, so the third
    request is never sent and the second refusal (403) is what the app sees."""
    profile_home("grok-session-no-fallback")
    (profile_home("grok-session-no-fallback") / "config.yaml").write_text("voice:\n  grok_live: {}\n")

    def resolver(*, force_refresh=False, api_key_hint=None, prefer_api_key=False):
        if prefer_api_key:
            return {"provider": "xai-oauth", "api_key": "tok-2", "base_url": "https://api.x.ai/v1"}
        if force_refresh:
            return {"provider": "xai-oauth", "api_key": "tok-2", "base_url": "https://api.x.ai/v1"}
        return {"provider": "xai-oauth", "api_key": "tok-1", "base_url": "https://api.x.ai/v1"}
    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials", resolver)
    monkeypatch.setattr(xai_http, "hermes_xai_default_headers", lambda: {})
    responses = [FakeResponse(401, text="a"), FakeResponse(403, text="still refused")]
    bearers = []

    def fake_post(url, headers=None, json=None, timeout=None):
        bearers.append(headers["Authorization"])
        return responses.pop(0)
    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(grok_live.GrokLiveError) as error:
        grok_live.mint_session("grok-session-no-fallback")
    assert error.value.status_code == 502 and "still refused" in error.value.detail
    assert bearers == ["Bearer tok-1", "Bearer tok-2"]


def test_mint_session_502_passes_xais_body_without_the_bearer(monkeypatch):
    profile_home("grok-session-502")
    (profile_home("grok-session-502") / "config.yaml").write_text("voice:\n  grok_live: {}\n")
    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials",
                        lambda **kw: {"provider": "xai", "api_key": "sk-secret", "base_url": "https://api.x.ai/v1"})
    monkeypatch.setattr(xai_http, "hermes_xai_default_headers", lambda: {})
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: FakeResponse(500, text="xAI exploded " + "z" * 400))

    with pytest.raises(grok_live.GrokLiveError) as error:
        grok_live.mint_session("grok-session-502")
    assert error.value.status_code == 502
    assert "sk-secret" not in error.value.detail
    assert "xAI exploded" in error.value.detail
    assert len(error.value.detail) <= len("xAI said 500: ") + grok_live.BODY_LIMIT


def test_mint_session_503_when_there_are_no_credentials_at_all(monkeypatch):
    profile_home("grok-session-none")
    (profile_home("grok-session-none") / "config.yaml").write_text("voice:\n  grok_live: {}\n")
    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials",
                        lambda **kw: {"provider": "xai", "api_key": "", "base_url": "https://api.x.ai/v1"})
    with pytest.raises(grok_live.GrokLiveError) as error:
        grok_live.mint_session("grok-session-none")
    assert error.value.status_code == 503 and error.value.detail == "no_credentials"


def test_mint_session_503_when_resolving_credentials_raises(monkeypatch):
    profile_home("grok-session-raises")
    (profile_home("grok-session-raises") / "config.yaml").write_text("voice:\n  grok_live: {}\n")

    def boom(**kw):
        raise RuntimeError("pool is locked")
    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials", boom)
    with pytest.raises(grok_live.GrokLiveError) as error:
        grok_live.mint_session("grok-session-raises")
    assert error.value.status_code == 503 and error.value.detail == "no_credentials"


def test_mint_session_unsupported_hermes_when_xai_http_is_missing(monkeypatch):
    (profile_home("grok-session-old") / "config.yaml").write_text("model: x\n")
    monkeypatch.setitem(sys.modules, "tools.xai_http", None)
    with pytest.raises(grok_live.GrokLiveError) as error:
        grok_live.mint_session("grok-session-old")
    assert error.value.status_code == 503 and error.value.detail == "no_credentials"


# ---------------------------------------------------------------------------
# The routes, through the real plugin_api router
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_HQ_PUSH_RELAY_URL", "off")
    core = sys.modules.get("hermes_hq_push_core")
    if core is None:
        spec = importlib.util.spec_from_file_location("hermes_hq_push_core", HERE / "push.py")
        core = importlib.util.module_from_spec(spec)
        sys.modules["hermes_hq_push_core"] = core
        spec.loader.exec_module(core)
    core._watcher_started = True  # no reply watcher in a test
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_grok_live_under_test",
                                                   HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    return TestClient(app)


BASE = "/api/plugins/hermes-hq-push"


def test_route_status_no_credentials(client, monkeypatch):
    home = profile_home("grok-route-status")
    (home / "config.yaml").write_text("voice:\n  grok_live:\n    model: grok-route\n")
    monkeypatch.setattr(xai_http, "has_xai_credentials", lambda: False)
    resp = client.get(f"{BASE}/voice/grok-live/status", params={"profile": "grok-route-status"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "available": False, "auth": None, "model": "grok-route",
                           "voice": grok_live.DEFAULT_VOICE, "reason": "no_credentials"}


def test_route_session_503_envelope(client, monkeypatch):
    profile_home("grok-route-503")
    (profile_home("grok-route-503") / "config.yaml").write_text("voice:\n  grok_live: {}\n")
    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials",
                        lambda **kw: {"provider": "xai", "api_key": "", "base_url": "https://api.x.ai/v1"})
    resp = client.post(f"{BASE}/voice/grok-live/session", params={"profile": "grok-route-503"}, json={})
    assert resp.status_code == 503
    assert resp.json() == {"ok": False, "detail": "no_credentials"}


def test_route_session_success_with_an_empty_body(client, monkeypatch):
    profile_home("grok-route-ok")
    (profile_home("grok-route-ok") / "config.yaml").write_text("voice:\n  grok_live:\n    voice: nova\n")
    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials",
                        lambda **kw: {"provider": "xai-oauth", "api_key": "tok", "base_url": "https://api.x.ai/v1"})
    monkeypatch.setattr(xai_http, "hermes_xai_default_headers", lambda: {})
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: FakeResponse(200, {"value": "s", "expires_at": 1}))
    resp = client.post(f"{BASE}/voice/grok-live/session", params={"profile": "grok-route-ok"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["client_secret"] == "s" and body["voice"] == "nova"


# ---------------------------------------------------------------------------
# 0.9.27: a bot with no xAI login of its own borrows the main bot's
# ---------------------------------------------------------------------------

def _only_main_bot_has_xai():
    from hermes_constants import get_hermes_home
    return get_hermes_home().resolve() == ROOT.resolve()


def test_status_borrows_the_main_bots_login(monkeypatch):
    home = profile_home("grok-borrow-status")
    (home / "config.yaml").write_text("voice:\n  grok_live:\n    voice: nova\n")
    root_auth = ROOT / "auth.json"
    had = root_auth.read_text() if root_auth.exists() else None
    root_auth.write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {"access_token": "root-tok"}}}}))
    try:
        monkeypatch.setattr(xai_http, "has_xai_credentials", _only_main_bot_has_xai)
        assert grok_live.status("grok-borrow-status") == {
            "ok": True, "available": True, "auth": "subscription", "model": grok_live.DEFAULT_MODEL,
            "voice": "nova", "shared": True}
    finally:
        root_auth.write_text(had) if had is not None else root_auth.unlink()


def test_status_of_the_main_bot_itself_is_never_shared(monkeypatch):
    monkeypatch.setattr(xai_http, "has_xai_credentials", _only_main_bot_has_xai)
    assert "shared" not in grok_live.status("")


def test_mint_session_uses_the_main_bots_login_and_the_bots_own_settings(monkeypatch):
    home = profile_home("grok-borrow-session")
    (home / "config.yaml").write_text("voice:\n  grok_live:\n    voice: nova\n    instructions: Sound like the CFO.\n")
    monkeypatch.setattr(xai_http, "has_xai_credentials", _only_main_bot_has_xai)

    def resolver(**kw):
        key = "root-tok" if _only_main_bot_has_xai() else ""
        return {"provider": "xai-oauth", "api_key": key, "base_url": "https://api.x.ai/v1"}
    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials", resolver)
    monkeypatch.setattr(xai_http, "hermes_xai_default_headers", lambda: {})
    posted = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posted.append(headers["Authorization"])
        return FakeResponse(200, {"value": "s", "expires_at": 1})
    monkeypatch.setattr(httpx, "post", fake_post)

    result = grok_live.mint_session("grok-borrow-session")
    assert posted == ["Bearer root-tok"]
    assert result["voice"] == "nova" and "Sound like the CFO." in result["instructions"]
    assert result["auth"] == "subscription"


def test_a_bot_with_its_own_login_keeps_it(monkeypatch):
    (profile_home("grok-own-login") / "config.yaml").write_text("voice: {}\n")
    monkeypatch.setattr(xai_http, "has_xai_credentials", lambda: True)
    assert grok_live._credentials_profile("grok-own-login") == "grok-own-login"


def test_nobody_has_a_login_stays_on_the_bot(monkeypatch):
    (profile_home("grok-no-login") / "config.yaml").write_text("voice: {}\n")
    monkeypatch.setattr(xai_http, "has_xai_credentials", lambda: False)
    assert grok_live._credentials_profile("grok-no-login") == "grok-no-login"
