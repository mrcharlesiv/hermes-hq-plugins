"""Device Access (device_access.py): ``python3 -m pytest -q test_device_access.py``."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("device_access_under_test", Path(__file__).with_name("device_access.py"))
device_access = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(device_access)

if "hermes_hq_push_core" in sys.modules:
    push = sys.modules["hermes_hq_push_core"]
else:
    _push_spec = importlib.util.spec_from_file_location("hermes_hq_push_core", Path(__file__).with_name("push.py"))
    push = importlib.util.module_from_spec(_push_spec)
    sys.modules["hermes_hq_push_core"] = push
    _push_spec.loader.exec_module(push)

PHONE = "4F1A2B3C-0000-4000-8000-000000000001"
OTHER = "4F1A2B3C-0000-4000-8000-000000000002"


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def store(tmp_path, clock=None):
    return device_access.Store(tmp_path / "device-access.db", clock or Clock())


def test_share_keeps_known_kinds_and_details_only(tmp_path):
    s = store(tmp_path)
    kept = s.share(PHONE, {"contacts": {"phone": True, "email": "yes", "ssn": True}, "photos": {"all": True}},
                   name="Charles’s iPhone")["access"]
    assert kept == {"contacts": {"phone": True, "email": False, "birthday": False, "address": False}}
    assert device_access.shared(kept, "contacts") == ["phone"]
    assert [p["device_id"] for p in s.sharing("contacts")] == [PHONE]


def test_bad_ids_are_refused(tmp_path):
    s = store(tmp_path)
    with pytest.raises(device_access.AccessError):
        s.share("../x", {})
    with pytest.raises(device_access.AccessError):
        s.complete(PHONE, "not-an-id", "done")


def test_all_off_shares_nothing_and_ends_waiting_requests(tmp_path):
    s = store(tmp_path)
    s.share(PHONE, {"contacts": {"phone": True}})
    rid = s.create(PHONE, "default", "contacts.search", {"query": "Ann"})
    s.share(PHONE, {"contacts": {"phone": False}})
    assert s.sharing("contacts") == []
    assert s.get(rid)["status"] == "unavailable"


def test_round_trip_only_the_asked_phone_answers(tmp_path):
    s = store(tmp_path)
    s.share(PHONE, {"contacts": {"phone": True}})
    rid = s.create(PHONE, "default", "contacts.search", {"query": "Ann"})
    assert s.pending(OTHER) == []
    fetched = s.pending(PHONE)
    assert [r["id"] for r in fetched] == [rid] and fetched[0]["args"] == {"query": "Ann"}
    assert s.get(rid)["status"] == "sent"
    assert [r["id"] for r in s.pending(PHONE)] == [rid]   # fetched again after a suspend: still handed out
    with pytest.raises(device_access.AccessError):
        s.complete(OTHER, rid, "done", {"contacts": []})
    assert s.complete(PHONE, rid, "done", {"contacts": [{"name": "Ann"}]})["accepted"] is True
    assert s.complete(PHONE, rid, "failed")["accepted"] is False   # first answer wins
    assert s.get(rid)["result"] == {"contacts": [{"name": "Ann"}]}
    assert s.pending(PHONE) == []


def test_unanswered_requests_expire(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    rid = s.create(PHONE, "default", "contacts.search", {"query": "Ann"})
    clock.now += device_access.REQUEST_TTL + 1
    assert s.get(rid)["status"] == "expired"
    assert s.pending(PHONE) == []
    assert s.complete(PHONE, rid, "done", {})["accepted"] is False


def test_oversized_answer_fails(tmp_path):
    s = store(tmp_path)
    rid = s.create(PHONE, "default", "contacts.search", {"query": "Ann"})
    s.complete(PHONE, rid, "done", {"blob": "x" * (device_access.RESULT_LIMIT + 1)})
    assert s.get(rid)["status"] == "failed"


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None, is_async=False, emoji="", description=""):
        assert toolset == "hq_device" and is_async and schema["name"] == name
        self.tools[name] = (handler, check_fn)


def tools(tmp_path, wakes):
    s = store(tmp_path)
    ctx = Ctx()
    device_access.register_tools(ctx, lambda: s, lambda d, r, p: wakes.append((d, r, p)) or "Asked the phone.",
                                 lambda: "finance")
    return s, ctx.tools


def test_contacts_tool_shows_only_while_a_phone_shares_contacts(tmp_path):
    s, registered = tools(tmp_path, [])
    _, check = registered["device_contacts_search"]
    assert check() is False
    s.share(PHONE, {"contacts": {"email": True}})
    assert check() is True


def test_contacts_tool_asks_the_phone_and_returns_its_answer(tmp_path, monkeypatch):
    wakes = []
    s, registered = tools(tmp_path, wakes)
    s.share(PHONE, {"contacts": {"phone": True, "email": True}}, name="Charles’s iPhone")
    handler, _ = registered["device_contacts_search"]

    async def run():
        task = asyncio.create_task(handler({"query": "Ann", "fields": ["phone", "birthday"]}))
        for _ in range(100):
            await asyncio.sleep(0.02)
            waiting = s.pending(PHONE)
            if waiting:
                break
        assert waiting[0]["args"] == {"query": "Ann", "fields": ["phone"]}   # birthday isn't shared
        s.complete(PHONE, waiting[0]["id"], "done", {"contacts": [{"name": "Ann Lee", "phones": ["+1 555"]}]})
        return json.loads(await task)

    answer = asyncio.run(run())
    assert answer == {"status": "done", "device": "Charles’s iPhone", "contacts": [{"name": "Ann Lee", "phones": ["+1 555"]}]}
    assert wakes and wakes[0][0] == PHONE and wakes[0][2] == "finance"


def test_contacts_tool_without_sharing_says_where_to_turn_it_on(tmp_path):
    _, registered = tools(tmp_path, [])
    answer = json.loads(asyncio.run(registered["device_contacts_search"][0]({"query": "Ann"})))
    assert answer["status"] == "unavailable" and "Device Access" in answer["hint"]


def test_a_late_answer_is_read_with_the_request_id(tmp_path, monkeypatch):
    monkeypatch.setattr(device_access, "WAIT_SECONDS", 0.05)
    s, registered = tools(tmp_path, [])
    s.share(PHONE, {"contacts": {"phone": True}})
    first = json.loads(asyncio.run(registered["device_contacts_search"][0]({"query": "Ann"})))
    assert first["status"] == "waiting" and "Asked the phone." in first["note"]
    s.complete(PHONE, first["request_id"], "unavailable", error="Contacts sharing is off on this phone.")
    later = json.loads(asyncio.run(registered["device_request_result"][0]({"request_id": first["request_id"]})))
    assert later["status"] == "unavailable" and later["error"] == "Contacts sharing is off on this phone."


def test_wake_goes_only_to_the_phone_asked(tmp_path):
    devices = push.Store(tmp_path / "devices.db")
    for device_id, token in ((PHONE, "a" * 64), (OTHER, "b" * 64)):
        devices.upsert(push.Device(device_id, token, "production", push.TOPIC, "*", {}))
    sent = []

    class Sender:
        def send(self, device, item):
            sent.append((device.device_id, item.for_device(device)[0]))

    dispatcher = push.Dispatcher(devices, sender_factory=Sender)
    assert dispatcher.deliver(push.Push(kind="device", request="r1", profile="finance", settled=True, to=PHONE)) == 1
    assert sent[0][0] == PHONE
    assert sent[0][1] == {"aps": {"content-available": 1},
                          "dispatch": {"kind": "device", "request": "r1", "profile": "finance", "settled": True}}


def test_status_says_device_access(tmp_path):
    assert push.status_body(tmp_path, None)["deviceAccess"] is True
