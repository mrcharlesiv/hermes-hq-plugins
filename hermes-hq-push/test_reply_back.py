"""reply_back against the real Hermes: run with Hermes's own python from the plugin folder, e.g.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_reply_back.py`` (HERMES_AGENT names another checkout).
conftest.py pins HERMES_HOME to a throwaway folder first; every chat here lives in that folder's state.db, and the
platform send is caught where Hermes would call Telegram or Discord, so nothing is ever posted."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

import hermes_state_registry  # noqa: E402
import tools.send_message_tool as smt  # noqa: E402
from hermes_constants import get_default_hermes_root  # noqa: E402

HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("reply_back_under_test", HERE / "reply_back.py")
reply_back = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reply_back)

ROOT = get_default_hermes_root()
assert ROOT.resolve() != (Path.home() / ".hermes").resolve()


def db_for(home: Path):
    return hermes_state_registry.acquire(Path(home) / "state.db")


def chat(home, sid, source="telegram", chat_id="-100500", chat_type="group", name="Garden club", thread=None,
         prompt_platform_id=None, answer="Water the beds at six.", **more):
    db = db_for(home)
    try:
        db.create_session(sid, source, session_key=f"agent:main:{source}:{chat_type}:{chat_id}" if chat_id else None,
                          chat_id=chat_id, chat_type=chat_type, thread_id=thread, display_name=name, **more)
        db.append_message(sid, "user", "When should I water?", platform_message_id=prompt_platform_id)
        db.append_message(sid, "assistant", "", tool_calls=[{"id": "c1", "type": "function",
                                                             "function": {"name": "weather", "arguments": "{}"}}])
        db.append_message(sid, "tool", "sunny", tool_call_id="c1", tool_name="weather")
        if answer is not None:
            db.append_message(sid, "assistant", answer)
    finally:
        hermes_state_registry.release_or_close(db)


def reads(home):
    def read_session(sid):
        db = db_for(home)
        try:
            return db.get_session(sid)
        finally:
            hermes_state_registry.release_or_close(db)

    def read_messages(sid):
        db = db_for(home)
        try:
            return db.get_messages(sid, latest=True, limit=200)
        finally:
            hermes_state_registry.release_or_close(db)
    return read_session, read_messages


@pytest.fixture
def home(tmp_path):
    return tmp_path


def run(home, sid, deliver, sent=None, profile="default"):
    read_session, read_messages = reads(home)
    return reply_back.answer(profile, sid, read_session, read_messages, deliver,
                             sent or reply_back.Sent(home / "reply-back.json"),
                             reply_back._is_silent, reply_back._is_machinery)


def catcher():
    calls = []

    def deliver(chat, text):
        calls.append((chat, text))
        return {"success": True, "message_id": "42"}
    return calls, deliver


def refused(home, sid, reason, deliver=None):
    calls, catch = catcher()
    with pytest.raises(reply_back.ReplyError) as error:
        run(home, sid, deliver or catch)
    assert error.value.reason == reason, error.value.detail
    assert calls == []
    return error.value


def test_posts_the_newest_answer_to_the_chat_the_session_belongs_to(home):
    chat(home, "s-tg", thread="17")
    calls, deliver = catcher()
    result = run(home, "s-tg", deliver)
    assert result["sent"] is True and result["label"] == "Telegram"
    [(where, text)] = calls
    assert (where["platform"], where["chatId"], where["threadId"]) == ("telegram", "-100500", "17")
    assert where["group"] is True and where["name"] == "Garden club"
    assert text == "Water the beds at six."


def test_discord_too(home):
    chat(home, "s-dc", source="discord", chat_id="998877", chat_type="channel", name="reading-club")
    calls, deliver = catcher()
    assert run(home, "s-dc", deliver)["label"] == "Discord"
    assert calls[0][0]["platform"] == "discord" and calls[0][0]["chatId"] == "998877"


def test_the_same_answer_is_never_posted_twice(home):
    chat(home, "s-tg")
    calls, deliver = catcher()
    run(home, "s-tg", deliver)
    again = run(home, "s-tg", deliver)
    assert again["sent"] is False and again["already"] is True
    assert len(calls) == 1


def test_a_failed_send_can_be_tried_again(home):
    chat(home, "s-tg")
    with pytest.raises(reply_back.ReplyError) as error:
        run(home, "s-tg", lambda chat, text: {"error": "Telegram send failed: Forbidden"})
    assert error.value.status_code == 502 and "Forbidden" in error.value.detail
    calls, deliver = catcher()
    assert run(home, "s-tg", deliver)["sent"] is True and len(calls) == 1


def test_an_answer_to_a_message_from_the_platform_is_not_posted_again(home):
    chat(home, "s-tg", prompt_platform_id="9001")   # the gateway already delivered this answer in Telegram
    refused(home, "s-tg", "not-from-app")


def test_only_telegram_and_discord_chats(home):
    chat(home, "s-app", source="tui", chat_id=None, chat_type=None)
    refused(home, "s-app", "not-outside")
    chat(home, "s-slack", source="slack", chat_id="C1")
    refused(home, "s-slack", "not-outside")


def test_a_chat_without_its_platform_chat_is_refused(home):
    chat(home, "s-lost", chat_id=None)
    refused(home, "s-lost", "no-chat")


def test_unknown_chat(home):
    assert refused(home, "s-none", "missing").status_code == 404


def test_no_answer_yet(home):
    chat(home, "s-wait", answer=None)
    refused(home, "s-wait", "no-answer")


def test_a_silent_answer_is_not_posted(home):
    chat(home, "s-quiet", answer="[SILENT]")
    refused(home, "s-quiet", "silent")


def test_files_are_not_sent(home):
    chat(home, "s-file", answer="MEDIA:/tmp/chart.png")
    refused(home, "s-file", "only-media")
    chat(home, "s-both", answer="Here is the chart.\nMEDIA:/tmp/chart.png")
    calls, deliver = catcher()
    run(home, "s-both", deliver)
    assert calls[0][1] == "Here is the chart."


def test_bad_ids(home):
    for profile, sid in [("../x", "s"), ("default", "../../etc"), ("default", ""), ("", "s")]:
        with pytest.raises(reply_back.ReplyError) as error:
            reply_back.check_ids(profile, sid)
        assert error.value.status_code == 400


def test_describe_names_the_group_and_says_when_the_bot_cant_post(home):
    chat(home, "s-tg")
    read_session, _ = reads(home)
    ok = reply_back.describe("default", "s-tg", read_session, lambda chat: None)
    assert ok == {"ok": True, "platform": "telegram", "label": "Telegram", "kind": "group", "group": True,
                  "name": "Garden club", "ready": True}
    chat(home, "s-dm", chat_id="123", chat_type="dm", name="Charles")
    not_ready = reply_back.describe("default", "s-dm", read_session, lambda chat: "Telegram isn't set up.")
    assert not_ready["group"] is False and not_ready["ready"] is False and not_ready["reason"] == "Telegram isn't set up."


# ---- Through Hermes: the bot's own profile, its own token, Hermes's sender ----

def profile_home(name):
    home = ROOT if name == "default" else ROOT / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def bots(monkeypatch):
    """Two bots on this Hermes, each with its own Telegram token; Hermes's send caught before the network."""
    for name, token in (("default", "111:default-bot"), ("cro", "222:cro-bot")):
        home = profile_home(name)
        (home / ".env").write_text(f"TELEGRAM_BOT_TOKEN={token}\n")
        (home / "config.yaml").write_text("platforms:\n  telegram:\n    enabled: true\n")
    sent = []

    async def caught(platform, pconfig, chat_id, message, thread_id=None, **more):
        sent.append({"platform": getattr(platform, "value", platform), "token": pconfig.token, "chat": chat_id,
                     "thread": thread_id, "text": message, "more": more})
        return {"success": True, "message_id": "77"}
    monkeypatch.setattr(smt, "_send_to_platform", caught)
    return sent


def test_through_hermes_posts_as_that_bot(bots, tmp_path):
    chat(profile_home("cro"), "s-cro", chat_id="-100777", name="Ops room")
    info = reply_back.hermes_describe("cro", "s-cro")
    assert info["ready"] is True and info["name"] == "Ops room"
    result = reply_back.hermes_answer("cro", "s-cro", tmp_path)
    assert result["sent"] is True
    assert bots == [{"platform": "telegram", "token": "222:cro-bot", "chat": "-100777", "thread": None,
                     "text": "Water the beds at six.", "more": {}}]
    assert reply_back.hermes_answer("cro", "s-cro", tmp_path)["already"] is True
    assert len(bots) == 1


def test_through_hermes_a_chat_is_only_found_in_its_own_bot(bots, tmp_path):
    chat(profile_home("cro"), "s-only-cro", chat_id="-100778")
    with pytest.raises(reply_back.ReplyError) as error:
        reply_back.hermes_answer("default", "s-only-cro", tmp_path)
    assert error.value.reason == "missing"
    assert bots == []


def test_through_hermes_a_bot_without_telegram_cant(bots, tmp_path):
    home = profile_home("quiet")
    (home / "config.yaml").write_text("model: x\n")
    chat(home, "s-quiet-bot", chat_id="-1")
    info = reply_back.hermes_describe("quiet", "s-quiet-bot")
    assert info["ready"] is False and "isn't set up" in info["reason"]
    with pytest.raises(reply_back.ReplyError) as error:
        reply_back.hermes_answer("quiet", "s-quiet-bot", tmp_path)
    assert error.value.reason == "not-set-up"
    assert bots == []


# ---- The routes ----

@pytest.fixture
def client(bots, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HERMES_HQ_PUSH_DATA_DIR", str(tmp_path / "data"))
    core = sys.modules.get("hermes_hq_push_core")
    if core is None:
        spec = importlib.util.spec_from_file_location("hermes_hq_push_core", HERE / "push.py")
        core = importlib.util.module_from_spec(spec)
        sys.modules["hermes_hq_push_core"] = core
        spec.loader.exec_module(core)
    core._watcher_started = True   # no reply watcher in a test
    spec = importlib.util.spec_from_file_location("hermes_hq_push_api_under_test", HERE / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-hq-push")
    return TestClient(app)


def test_routes(client, bots):
    chat(profile_home("cro"), "s-route", chat_id="-100900", name="Route club")
    status = client.get("/api/plugins/hermes-hq-push/status").json()
    assert status["replyBack"] is True and status["pluginVersion"] == "0.9.4"
    info = client.get("/api/plugins/hermes-hq-push/outside/cro/s-route")
    assert info.status_code == 200 and info.json()["name"] == "Route club"
    sent = client.post("/api/plugins/hermes-hq-push/outside/cro/s-route/answer")
    assert sent.status_code == 200 and sent.json()["sent"] is True
    assert bots[0]["chat"] == "-100900" and bots[0]["token"] == "222:cro-bot"
    assert client.post("/api/plugins/hermes-hq-push/outside/cro/s-route/answer").json()["already"] is True
    assert client.get("/api/plugins/hermes-hq-push/outside/cro/s-none").status_code == 404
    chat(profile_home("cro"), "s-route-tg", chat_id="-1", prompt_platform_id="5")
    refused = client.post("/api/plugins/hermes-hq-push/outside/cro/s-route-tg/answer")
    assert refused.status_code == 409 and "from the app" in refused.json()["detail"]["message"]
    assert refused.json()["detail"]["reason"] == "not-from-app"
    # Nothing in the request can name another chat: the body and query are ignored.
    chat(profile_home("cro"), "s-route2", chat_id="-100901")
    other = client.post("/api/plugins/hermes-hq-push/outside/cro/s-route2/answer?chat_id=-1&text=hi",
                        json={"chat_id": "-1", "thread_id": "9", "text": "hello"})
    assert other.status_code == 200
    assert bots[-1]["chat"] == "-100901" and bots[-1]["text"] == "Water the beds at six." and bots[-1]["thread"] is None
    assert len(bots) == 2
