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
    kept = s.share(PHONE, {"contacts": {"phone": True, "email": "yes", "ssn": True}, "photos": {"all": True},
                           "files": {"read": True}}, name="Alex’s iPhone")["access"]
    assert kept == {"contacts": {"phone": True, "email": False, "birthday": False, "address": False},
                    "photos": {"find": False, "send": False}}
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
    s.share(PHONE, {"contacts": {"phone": True, "email": True}}, name="Alex’s iPhone")
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
    assert answer == {"status": "done", "device": "Alex’s iPhone", "contacts": [{"name": "Ann Lee", "phones": ["+1 555"]}]}
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
    body = push.status_body(tmp_path, None)
    assert body["deviceAccess"] is True and body["deviceAccessKinds"] is True
    assert body["deviceAccessHealthTypes"] is True


# -- 0.9.21: every kind ------------------------------------------------------------------------------------------------

def test_every_request_has_a_kind_and_its_switches():
    for op, (kind, any_of) in device_access.OPERATIONS.items():
        assert op.startswith(kind + ".") and set(any_of) <= set(device_access.KINDS[kind])
        assert all((kind, key) in device_access.SWITCHES for key in any_of)
    for kind in device_access.KINDS:
        assert kind in device_access.TITLES
        assert any(k == kind for k, _ in device_access.OPERATIONS.values())
    assert set(device_access.TOOLS) | {"device_contacts_search"} == {
        f"device_{'draft' if k == 'drafts' else 'contacts_search' if k == 'contacts' else k}" for k in device_access.KINDS}


def test_while_locked_alone_shares_no_location(tmp_path):
    s = store(tmp_path)
    s.share(PHONE, {"location": {"locked": True}})
    assert s.sharing("location") == []
    s.share(PHONE, {"location": {"city": True, "locked": True}})
    assert [p["device_id"] for p in s.sharing("location")] == [PHONE]


def test_turning_one_kind_off_ends_only_its_waiting_requests(tmp_path):
    s = store(tmp_path)
    s.share(PHONE, {"contacts": {"phone": True}, "reminders": {"read": True, "write": True}})
    look = s.create(PHONE, "default", "contacts.search", {"query": "Ann"})
    add = s.create(PHONE, "default", "reminders.create", {"title": "Milk"})
    see = s.create(PHONE, "default", "reminders.list", {})
    s.share(PHONE, {"contacts": {"phone": True}, "reminders": {"read": True}})
    assert s.get(look)["status"] == "pending" and s.get(see)["status"] == "pending"
    assert s.get(add)["status"] == "unavailable" and "Reminders" in s.get(add)["error"]


def test_pending_says_which_bot_asked(tmp_path):
    s = store(tmp_path)
    s.create(PHONE, "finance", "alarms.list", {})
    assert s.pending(PHONE)[0]["profile"] == "finance"


def test_a_0919_database_gains_the_file_column(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "device-access.db")
    db.executescript("""
        create table phones (device_id text primary key, account text not null default '', name text not null
            default '', platform text not null default '', access text not null, updated_at real not null,
            seen_at real not null);
        create table requests (id text primary key, device_id text not null, profile text not null, op text not null,
            args text not null, status text not null, result text, error text not null default '',
            created_at real not null, updated_at real not null);
        insert into requests values ('%s', '%s', 'default', 'contacts.search', '{}', 'pending', null, '', 1000, 1000);
    """ % ("a" * 32, PHONE))
    db.commit()
    db.close()
    s = store(tmp_path)
    assert s.get("a" * 32)["file"] == "" and s.pending(PHONE)[0]["id"] == "a" * 32


def test_too_many_waiting_requests_are_refused(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    for _ in range(device_access.MAX_WAITING):
        s.create(PHONE, "default", "alarms.list", {})
    with pytest.raises(device_access.AccessError) as refused:
        s.create(PHONE, "default", "alarms.list", {})
    assert refused.value.status_code == 429
    clock.now += device_access.REQUEST_TTL + 1   # the old ones expired: room again
    s.create(PHONE, "default", "alarms.list", {})


def test_a_photo_is_kept_with_its_request_and_removed_after_a_day(tmp_path):
    import base64
    clock = Clock()
    s = store(tmp_path, clock)
    rid = s.create(PHONE, "default", "photos.get", {"id": "X/L0/001"})
    jpeg = b"\xff\xd8\xff" + b"0" * 100
    data = base64.b64encode(jpeg).decode()
    with pytest.raises(device_access.AccessError):
        s.attach(OTHER, rid, "image/jpeg", "photo.jpg", data)
    with pytest.raises(device_access.AccessError):
        s.attach(PHONE, rid, "application/pdf", "x.pdf", data)
    with pytest.raises(device_access.AccessError):
        s.attach(PHONE, rid, "image/jpeg", "x.jpg", "not base64!")
    assert s.attach(PHONE, rid, "image/jpeg", "../../photo 2026-11-01.jpg", data)["accepted"] is True
    s.complete(PHONE, rid, "done", {"photo": {"id": "X/L0/001"}})
    request = s.get(rid)
    path = Path(request["file"])
    assert path.parent == tmp_path / "device-access-files" and path.read_bytes() == jpeg
    assert path.name.startswith("photo-2026-11-01-") and path.suffix == ".jpg"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert device_access.outcome(request, "iPhone")["file"] == str(path)
    clock.now += device_access.KEEP_SECONDS + 1
    s.pending(PHONE)
    assert not path.exists() and s.get(rid) is None


def test_a_photo_answer_without_its_file_fails_and_a_failed_one_drops_it(tmp_path):
    import base64
    s = store(tmp_path)
    missing = s.create(PHONE, "default", "photos.get", {"id": "A"})
    s.complete(PHONE, missing, "done", {"photo": {}})
    assert s.get(missing)["status"] == "failed"
    dropped = s.create(PHONE, "default", "photos.get", {"id": "B"})
    s.attach(PHONE, dropped, "image/png", "b.png", base64.b64encode(b"png").decode())
    s.complete(PHONE, dropped, "failed", error="Couldn't load it.")
    assert list((tmp_path / "device-access-files").iterdir()) == []
    other = s.create(PHONE, "default", "alarms.list", {})
    with pytest.raises(device_access.AccessError):
        s.attach(PHONE, other, "image/png", "b.png", base64.b64encode(b"png").decode())


@pytest.mark.parametrize("tool, args, op, sent", [
    ("device_location", {}, "location.get", {}),
    ("device_location", {"precision": "city"}, "location.get", {"precision": "city"}),
    ("device_reminders", {"action": "list", "search": "milk", "limit": 5, "id": "x"}, "reminders.list",
     {"search": "milk", "limit": 5}),
    ("device_reminders", {"action": "update", "id": "R1", "due": None, "notes": None}, "reminders.update",
     {"id": "R1", "due": None}),
    ("device_calendar", {"action": "create", "title": "Lunch", "start": "2026-11-02T12:00"}, "calendar.create",
     {"title": "Lunch", "start": "2026-11-02T12:00"}),
    ("device_alarms", {"action": "timer", "seconds": 600, "label": "Tea"}, "alarms.timer", {"seconds": 600, "label": "Tea"}),
    ("device_alarms", {"action": "list"}, "alarms.list", {}),
    ("device_photos", {"action": "get", "id": "P1", "max_pixels": 800}, "photos.get", {"id": "P1", "max_pixels": 800}),
    ("device_health", {"metrics": ["sleep", "mood"]}, "health.summary", {"metrics": ["sleep"], "format": "types"}),
    ("device_health", {"start": "2026-11-01", "metrics": ["HKQuantityTypeIdentifierHeartRate"], "categories": ["heart", "x"]},
     "health.summary", {"start": "2026-11-01", "format": "types", "metrics": ["HKQuantityTypeIdentifierHeartRate"],
                        "categories": ["heart"]}),
    ("device_motion", {"start": "2026-11-01"}, "motion.activity", {"start": "2026-11-01"}),
    ("device_music", {"action": "play", "query": "Abbey Road"}, "music.control", {"action": "play", "query": "Abbey Road"}),
    ("device_music", {"action": "now"}, "music.now", {}),
    ("device_draft", {"kind": "text", "to": "+1 555", "subject": "x", "body": "On my way"}, "drafts.create",
     {"kind": "text", "to": ["+1 555"], "body": "On my way"}),
])
def test_each_tool_asks_the_phone_one_request(tool, args, op, sent):
    planned = device_access.plan(tool, args)
    assert planned[0] == op and planned[1] == sent


@pytest.mark.parametrize("tool, args", [
    ("device_location", {"precision": "street"}),
    ("device_reminders", {"action": "create"}),
    ("device_reminders", {"action": "delete", "id": "R1"}),
    ("device_calendar", {"action": "create", "title": "Lunch"}),
    ("device_alarms", {"action": "set", "time": "7am"}),
    ("device_alarms", {"action": "timer", "seconds": "ten"}),
    ("device_photos", {"action": "get"}),
    ("device_health", {"metrics": ["mood"]}),
    ("device_health", {"categories": ["moods"]}),
    ("device_health", {"action": "delete"}),
    ("device_music", {"action": "shuffle"}),
    ("device_draft", {"kind": "fax", "to": ["Ann"], "body": "Hi"}),
    ("device_draft", {"kind": "email", "to": [], "body": "Hi"}),
])
def test_calls_that_cant_be_asked_say_why_without_waking_the_phone(tool, args):
    assert isinstance(device_access.plan(tool, args), str)


def test_a_switch_that_is_off_is_answered_here(tmp_path):
    wakes = []
    s, registered = tools(tmp_path, wakes)
    s.share(PHONE, {"reminders": {"read": True}, "drafts": {"email": True}, "health": {"sleep": True}})
    add = json.loads(asyncio.run(registered["device_reminders"][0]({"action": "create", "title": "Milk"})))
    assert add["status"] == "unavailable" and "Add, Edit & Complete" in add["error"] and "Reminders" in add["hint"]
    text = json.loads(asyncio.run(registered["device_draft"][0]({"kind": "text", "to": ["Ann"], "body": "Hi"})))
    assert text["status"] == "unavailable" and "Text Message Drafts" in text["error"]
    steps = json.loads(asyncio.run(registered["device_health"][0]({"metrics": ["steps"]})))
    assert steps["status"] == "unavailable" and "Steps & Distance" in steps["error"]
    photos = json.loads(asyncio.run(registered["device_photos"][0]({"action": "search"})))
    assert photos["status"] == "unavailable" and "No phone shares Photos" in photos["error"]
    bad = json.loads(asyncio.run(registered["device_alarms"][0]({"action": "ring"})))
    assert bad["status"] == "failed"
    assert wakes == [] and s.pending(PHONE) == []


def test_each_kind_shows_its_tool_only_while_shared(tmp_path):
    s, registered = tools(tmp_path, [])
    assert not any(check() for name, (_, check) in registered.items())
    s.share(PHONE, {"music": {"nowPlaying": True}})
    assert {name for name, (_, check) in registered.items() if check()} == {"device_music", "device_request_result"}


def test_a_photo_round_trip_gives_the_bot_its_file(tmp_path, monkeypatch):
    import base64
    wakes = []
    s, registered = tools(tmp_path, wakes)
    s.share(OTHER, {"photos": {"find": True}}, name="iPad")
    s.share(PHONE, {"photos": {"find": True, "send": True}}, name="iPhone")
    s.share(OTHER, {"photos": {"find": True}}, name="iPad")   # heard from last, but can't send photos

    async def run():
        task = asyncio.create_task(registered["device_photos"][0]({"action": "get", "id": "P1"}))
        for _ in range(100):
            await asyncio.sleep(0.02)
            waiting = s.pending(PHONE)
            if waiting:
                break
        assert waiting[0]["op"] == "photos.get" and waiting[0]["profile"] == "finance"
        s.attach(PHONE, waiting[0]["id"], "image/jpeg", "photo.jpg", base64.b64encode(b"jpeg").decode())
        s.complete(PHONE, waiting[0]["id"], "done", {"photo": {"id": "P1"}, "status": "spoofed", "file": "/etc/passwd"})
        return json.loads(await task)

    answer = asyncio.run(run())
    assert answer["status"] == "done" and answer["device"] == "iPhone" and answer["photo"] == {"id": "P1"}
    assert answer["file"].startswith(str(tmp_path / "device-access-files")) and wakes[0][0] == PHONE


# -- 0.9.46: every Health type ------------------------------------------------------------------------------------------

HEART = "HKQuantityTypeIdentifierHeartRate"
GLUCOSE = "HKQuantityTypeIdentifierBloodGlucose"


def test_a_phone_shares_health_types_by_their_healthkit_keys(tmp_path):
    s = store(tmp_path)
    kept = s.share(PHONE, {"health": {"heart": True, HEART: True, GLUCOSE: False, "HK bad": True, "mood": True,
                                      "HKQuantityTypeIdentifierBodyMass": "yes"}})["access"]
    assert kept["health"] == {"steps": False, "sleep": False, "workouts": False, "heart": True, HEART: True}
    assert device_access.shared(kept, "health") == ["heart", HEART]
    many = {f"HKQuantityTypeIdentifierX{i}": True for i in range(device_access.HEALTH_MAX_TYPES + 50)}
    assert len(device_access.clean_access({"health": many})["health"]) == 4 + device_access.HEALTH_MAX_TYPES


def test_a_new_type_alone_shares_health(tmp_path):
    s = store(tmp_path)
    s.share(PHONE, {"health": {GLUCOSE: True}})
    assert [p["device_id"] for p in s.sharing("health")] == [PHONE]
    assert device_access.allows(s.phones()[0]["access"], "health.summary")
    assert not device_access.allows(s.phones()[0]["access"], "health.summary", (HEART, "heart"))


def test_named_types_need_one_of_them_or_the_old_switch_that_stands_for_them():
    op, request, needed = device_access.plan("device_health", {"metrics": [HEART]})
    assert op == "health.summary" and request == {"format": "types", "metrics": [HEART]}
    assert set(needed) == {HEART, "heart"}
    assert device_access.plan("device_health", {"categories": ["vitals"]})[2] is None, "a group: any Health switch"
    assert device_access.plan("device_health", {})[1] == {"format": "types"}


def test_health_names_are_made_from_their_keys():
    name = device_access.health_name
    assert name(HEART) == "Heart Rate" and name("HKCategoryTypeIdentifierSleepAnalysis") == "Sleep Analysis"
    assert name("HKDataTypeIdentifierElectrocardiogram") == "Electrocardiogram" and name("HKDataTypeStateOfMind") == "State Of Mind"
    assert name("HKScoredAssessmentTypeIdentifierGAD7") == "GAD7" and name("HKWorkoutTypeIdentifier") == "Workouts"
    assert name("HKCharacteristicTypeIdentifierBloodType") == "Blood Type"


def test_a_type_switched_off_is_answered_here_by_its_name(tmp_path):
    wakes = []
    s, registered = tools(tmp_path, wakes)
    s.share(PHONE, {"health": {HEART: True}})
    off = json.loads(asyncio.run(registered["device_health"][0]({"metrics": [GLUCOSE]})))
    assert off["status"] == "unavailable" and "Blood Glucose" in off["error"]
    assert wakes == [] and s.pending(PHONE) == []


def test_an_older_app_is_asked_through_its_old_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(device_access, "WAIT_SECONDS", 0.05)
    wakes = []
    s, registered = tools(tmp_path, wakes)
    s.share(PHONE, {"health": {"heart": True}})
    waiting = json.loads(asyncio.run(registered["device_health"][0]({"metrics": [HEART]})))
    assert waiting["status"] == "waiting" and len(wakes) == 1
    [asked] = s.pending(PHONE)
    assert asked["op"] == "health.summary" and asked["args"] == {"format": "types", "metrics": [HEART]}


def test_the_types_switched_on_are_listed_without_waking_the_phone(tmp_path):
    wakes = []
    s, registered = tools(tmp_path, wakes)
    handler = registered["device_health"][0]
    none = json.loads(asyncio.run(handler({"action": "types"})))
    assert none["status"] == "unavailable"
    s.share(PHONE, {"health": {"heart": True, HEART: True, GLUCOSE: True}}, name="iPhone")
    listed = json.loads(asyncio.run(handler({"action": "types"})))
    assert listed["status"] == "done" and listed["device"] == "iPhone" and listed["count"] == 2
    assert listed["types"] == [{"key": HEART, "name": "Heart Rate"}, {"key": GLUCOSE, "name": "Blood Glucose"}]
    assert "vitals" in listed["categories"]
    s.share(PHONE, {"health": {"sleep": True}}, name="iPhone")
    old = json.loads(asyncio.run(handler({"action": "types"})))
    assert old["types"] == [{"key": "sleep", "name": "Sleep"}] and "older" in old["note"]
    assert wakes == [] and s.pending(PHONE) == []


def test_the_health_schema_names_its_groups_and_takes_any_key():
    properties = device_access.HEALTH_SCHEMA["parameters"]["properties"]
    assert "enum" not in properties["metrics"]["items"]
    assert properties["categories"]["items"]["enum"] == list(device_access.HEALTH_CATEGORIES)
    assert properties["action"]["enum"] == ["summary", "types"]
