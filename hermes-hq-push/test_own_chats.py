"""own_chats against the real Hermes: run with Hermes's own python from the plugin folder, e.g.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_own_chats.py`` (HERMES_AGENT names another checkout)."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

import hermes_state_mutation_binding as binding  # noqa: E402
from gateway.session_contract import Principal  # noqa: E402
from hermes_state import SessionDB  # noqa: E402

_spec = importlib.util.spec_from_file_location("own_chats_under_test", Path(__file__).with_name("own_chats.py"))
own_chats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(own_chats)

NOUS = 'auth:v1:["nous","https://portal.nousresearch.com","u1"]'


@pytest.fixture(autouse=True)
def stock_hermes(monkeypatch):
    """Every test starts from Hermes's own function and gets it back afterwards."""
    monkeypatch.setattr(binding, "_login_label", getattr(binding._login_label, "__wrapped__", binding._login_label))
    monkeypatch.delenv("HERMES_DASHBOARD_PORTAL_URL", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})


@pytest.fixture
def db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    yield db
    db.close()


def _auth(db, subject, row_id):
    actor = Principal(subject, str(db.db_path.parent), frozenset({"session:read", "session:create"}), "native-http:x")
    with db._read_ctx() as conn:
        return binding.authorize_history(conn, actor, row_id)


def _refused(db, subject, row_id):
    with pytest.raises(Exception) as raised:
        _auth(db, subject, row_id)
    return getattr(raised.value, "reason", None) == "permission_denied"


def test_stock_hermes_refuses_a_nous_login_its_own_chat(db):
    """The bug this fixes, so the test notices the day Hermes changes."""
    db.create_session("tui-row", source="tui", user_id="nous:u1")
    assert _refused(db, NOUS, "tui-row")


def test_installed_the_nous_login_owns_the_chats_serve_stamped_for_it(db):
    assert own_chats.install() == "installed"
    db.create_session("tui-row", source="tui", user_id="nous:u1")
    db.create_session("desktop-row", source="desktop", user_id="nous:u1")
    assert _auth(db, NOUS, "tui-row") is True
    assert _auth(db, NOUS, "desktop-row") is True


@pytest.mark.parametrize("subject", [
    'auth:v1:["nous","https://evil.example","u1"]',                # an issuer this computer doesn't verify against
    'auth:v1:["nous","https://portal.nousresearch.com/x","u1"]',
    'auth:v1:["nous","https://portal.nousresearch.com","u2"]',    # another Nous user
    'auth:v1:["basic","https://portal.nousresearch.com","u1"]',   # another provider
    'auth:v1:["nous","https://portal.nousresearch.com","  "]',
])
def test_installed_every_other_login_is_still_refused(subject, db):
    assert own_chats.install() == "installed"
    db.create_session("tui-row", source="tui", user_id="nous:u1")
    assert _refused(db, subject, "tui-row")


def test_installed_hermes_answers_for_issuer_less_logins_unchanged(db):
    assert own_chats.install() == "installed"
    assert binding._login_label('auth:v1:["basic","","e2e"]') == "basic:e2e"
    assert binding._login_label('auth:v1:["basic","https://idp.example","e2e"]') is None
    assert binding._login_label("uid:501") is None
    db.create_session("mallory-row", source="desktop", user_id="basic:mallory")
    assert _refused(db, 'auth:v1:["basic","","e2e"]', "mallory-row")


def test_a_configured_portal_is_the_one_that_maps(monkeypatch, db):
    monkeypatch.setenv("HERMES_DASHBOARD_PORTAL_URL", "https://portal.staging.example/")
    assert own_chats.install() == "installed"
    assert binding._login_label('auth:v1:["nous","https://portal.staging.example","u1"]') == "nous:u1"
    assert binding._login_label(NOUS) is None


def test_the_config_portal_counts_when_no_env(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"dashboard": {"oauth": {"portal_url": "https://portal.config.example"}}})
    assert own_chats.portal() == "https://portal.config.example"


def test_installing_twice_wraps_once():
    assert own_chats.install() == "installed"
    first = binding._login_label
    assert own_chats.install() == "installed"
    assert binding._login_label is first


def test_a_fixed_hermes_is_left_alone(monkeypatch):
    def fixed(subject):
        return "nous:probe" if "probe" in str(subject) else None
    monkeypatch.setattr(binding, "_login_label", fixed)
    assert own_chats.install() == "not needed"
    assert binding._login_label is fixed


def test_a_reworked_hermes_is_left_alone(monkeypatch):
    monkeypatch.delattr(binding, "_login_label")
    assert own_chats.install() == "absent"
    assert not hasattr(binding, "_login_label")
