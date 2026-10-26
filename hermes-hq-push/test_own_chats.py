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


# ---- Chats the old password login made (0.9.3) ----

FLEET = 'auth:v1:["basic","","fleet"]'           # the old password login, were it turned on again
OWNER = "nous:u1"


@pytest.fixture(autouse=True)
def stock_authorize(monkeypatch):
    monkeypatch.setattr(binding, "authorize_history",
                        getattr(binding.authorize_history, "__wrapped__", binding.authorize_history))
    monkeypatch.delenv("HERMES_HQ_PASSWORD_CHATS_OWNER", raising=False)


def _name(monkeypatch, owner):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"hermes_hq": {"password_chats_owner": owner}})


def _claim(db, subject, row_id):
    actor = Principal(subject, str(db.db_path.parent), frozenset({"session:read", "session:create"}), "native-http:x")
    return db._execute_write(lambda conn: binding.authorize_history(conn, actor, row_id, claim=True))


def test_hermes_test_home_is_not_the_real_one():
    assert os.path.realpath(os.environ["HERMES_HOME"]) != os.path.realpath(os.path.expanduser("~/.hermes"))


def test_stock_hermes_refuses_every_current_login_a_password_chat(db):
    """The bug: only the old password login owns these, and it is turned off."""
    assert own_chats.install() == "installed"
    db.create_session("bot-chat", source="tui", user_id="basic:fleet")
    assert _refused(db, NOUS, "bot-chat")
    assert _auth(db, FLEET, "bot-chat") is True   # Hermes's own rule: the password login, were it on again


def test_nobody_named_changes_nothing(db):
    assert own_chats.install() == "installed"
    assert own_chats.install_password_chats() == "off"
    db.create_session("bot-chat", source="tui", user_id="basic:fleet")
    assert _refused(db, NOUS, "bot-chat")


@pytest.mark.parametrize("value", ["", "  ", "fleet", ":u1", "nous:", "nous:  ", 'auth:v1:["nous","","u1"]', 7])
def test_a_malformed_owner_is_nobody(monkeypatch, value):
    _name(monkeypatch, value)
    assert own_chats.password_chats_owner() is None
    assert own_chats.install_password_chats() == "off"


def test_the_named_login_owns_the_password_chats(monkeypatch, db):
    _name(monkeypatch, OWNER)
    assert own_chats.install() == "installed"
    assert own_chats.install_password_chats() == "installed"
    db.create_session("bot-chat", source="tui", user_id="basic:fleet")
    db.create_session("desktop-chat", source="desktop", user_id="basic:fleet")
    db.create_session("older-name", source="tui", user_id="basic:admin")
    for row in ("bot-chat", "desktop-chat", "older-name"):
        assert _auth(db, NOUS, row) is True
    # Its own chats still answer as before.
    db.create_session("own", source="tui", user_id="nous:u1")
    assert _auth(db, NOUS, "own") is True


def test_the_env_names_the_owner_over_config(monkeypatch, db):
    _name(monkeypatch, "nous:someone-else")
    monkeypatch.setenv("HERMES_HQ_PASSWORD_CHATS_OWNER", " nous:u1 ")
    assert own_chats.password_chats_owner() == "nous:u1"
    assert own_chats.install() == "installed"
    assert own_chats.install_password_chats() == "installed"
    db.create_session("bot-chat", source="tui", user_id="basic:fleet")
    assert _auth(db, NOUS, "bot-chat") is True


@pytest.mark.parametrize("subject", [
    'auth:v1:["nous","https://portal.nousresearch.com","u2"]',   # another Nous user on the same gateway
    'auth:v1:["nous","https://evil.example","u1"]',               # the owner's id from an issuer this computer doesn't trust
    'auth:v1:["basic","","other"]',                               # another password login
    'auth:v1:["self_hosted","","u1"]',                            # the same id from another provider
    "uid:501",
])
def test_only_the_named_login_gets_them(subject, monkeypatch, db):
    _name(monkeypatch, OWNER)
    own_chats.install()
    assert own_chats.install_password_chats() == "installed"
    db.create_session("bot-chat", source="tui", user_id="basic:fleet")
    assert _refused(db, subject, "bot-chat")


def test_the_hand_over_covers_only_password_chats(monkeypatch, db):
    _name(monkeypatch, OWNER)
    own_chats.install()
    own_chats.install_password_chats()
    db.create_session("theirs", source="tui", user_id="nous:u2")
    db.create_session("blank-user", source="tui", user_id="basic:  ")
    db.create_session("cron-run", source="cron", user_id="basic:fleet")       # not owner history: Hermes's False stands
    assert _refused(db, NOUS, "theirs")
    assert _refused(db, NOUS, "blank-user")
    assert _auth(db, NOUS, "cron-run") is False


def test_a_claim_hands_the_chat_over_for_good(monkeypatch, db):
    """The first edit binds the chat to the named login, in Hermes's own format: stock Hermes then agrees."""
    _name(monkeypatch, OWNER)
    own_chats.install()
    own_chats.install_password_chats()
    db.create_session("bot-chat", source="tui", user_id="basic:fleet")
    assert _claim(db, NOUS, "bot-chat") is True
    original = binding.authorize_history.__wrapped__
    actor = Principal(NOUS, str(db.db_path.parent), frozenset({"session:read", "session:create"}), "native-http:x")
    with db._read_ctx() as conn:
        assert original(conn, actor, "bot-chat") is True
    assert _refused(db, FLEET, "bot-chat")      # the password login, turned on again, no longer owns it
    assert _refused(db, 'auth:v1:["nous","https://portal.nousresearch.com","u2"]', "bot-chat")


def test_a_chat_claimed_by_someone_else_stays_theirs(monkeypatch, db):
    _name(monkeypatch, OWNER)
    own_chats.install()
    own_chats.install_password_chats()
    db.create_session("bot-chat", source="tui", user_id="basic:fleet")
    assert _claim(db, FLEET, "bot-chat") is True     # the password login edited it first
    assert _refused(db, NOUS, "bot-chat")


def test_the_named_login_still_needs_create(monkeypatch, db):
    _name(monkeypatch, OWNER)
    own_chats.install()
    own_chats.install_password_chats()
    db.create_session("bot-chat", source="tui", user_id="basic:fleet")
    actor = Principal(NOUS, str(db.db_path.parent), frozenset({"session:read"}), "native-http:x")
    with db._read_ctx() as conn, pytest.raises(Exception) as raised:
        binding.authorize_history(conn, actor, "bot-chat")
    assert getattr(raised.value, "reason", None) == "permission_denied"


def test_installing_the_hand_over_twice_wraps_once(monkeypatch):
    _name(monkeypatch, OWNER)
    assert own_chats.install_password_chats() == "installed"
    first = binding.authorize_history
    assert own_chats.install_password_chats() == "installed"
    assert binding.authorize_history is first


def test_a_reworked_hermes_keeps_its_own_answer(monkeypatch):
    _name(monkeypatch, OWNER)
    monkeypatch.delattr(binding, "authorize_history")
    assert own_chats.install_password_chats() == "absent"
    assert not hasattr(binding, "authorize_history")
