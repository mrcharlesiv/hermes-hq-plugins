"""cron_working against the real Hermes: run with Hermes's own python from the plugin folder, e.g.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_cron_working.py`` (HERMES_AGENT names another checkout)."""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

import tools.bot_live_delivery as live  # noqa: E402
from cron import scheduler_delivery  # noqa: E402
from hermes_constants import reset_hermes_home_override, set_hermes_home_override  # noqa: E402
from hermes_cli.profiles import get_profile_dir  # noqa: E402

_spec = importlib.util.spec_from_file_location("cron_working_under_test", Path(__file__).with_name("cron_working.py"))
cron_working = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cron_working)


@pytest.fixture
def homes(tmp_path):
    root = (tmp_path / "hermes").resolve()
    (root / "profiles" / "cro").mkdir(parents=True)
    return root, root / "profiles" / "cro"


def hermes_key(monkeypatch, source, job, profile):
    """The delivery id Hermes's own bot-chat delivery uses, caught where it first reads the receipt."""
    seen = []

    def stop(home, key):
        seen.append((Path(home), key))
        raise RuntimeError("caught")

    monkeypatch.setattr(live, "read_delivery_result", stop)
    token = set_hermes_home_override(str(source))
    try:
        scheduler_delivery._deliver_to_bot_chat(job, "output", profile)
    finally:
        reset_hermes_home_override(token)
    assert len(seen) == 1
    return seen[0]


def test_the_key_is_hermes_own(monkeypatch, homes):
    root, cro = homes
    job = {"id": "abc123", "name": "Brief", "execution_id": "e" * 32}
    home, key = hermes_key(monkeypatch, root, job, "")
    assert home == root
    assert key == cron_working.delivery_key(root, "abc123", "e" * 32, root)
    home, key = hermes_key(monkeypatch, root, job, "cro")  # a named profile: the one get_profile_dir gives (route's too)
    assert home.resolve() == get_profile_dir("cro").resolve()
    assert key == cron_working.delivery_key(root, "abc123", "e" * 32, home.resolve())


def receipt(home, key, status, age=0.0):
    path = Path(home).joinpath(*cron_working.DELIVERY_DIR, f"{key}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"delivery_id": key, "status": status, "message": "[Cronjob ...]"}))
    if age:
        then = time.time() - age
        os.utime(path, (then, then))


def job(job_id, run_id="r1", deliver="bot-chat", **more):
    return {"id": job_id, "deliver": deliver, "latest_execution": {"id": run_id, "status": "completed"}, **more}


def run(root, cro, jobs):
    profiles = [("default", root), ("cro", cro)]
    return cron_working.working(profiles, lambda name, home: jobs.get(name, []), lambda name: root / "profiles" / name)


def test_pending_handoffs_are_working(homes):
    root, cro = homes
    jobs = {"default": [job("queued"), job("claimed"), job("old-spelling"), job("settled"), job("failed"), job("none")],
            "cro": [job("own-cro", deliver="telegram,bot-chat:cro")]}
    receipt(root, cron_working.delivery_key(root, "queued", "r1", root), "queued")
    receipt(root, cron_working.delivery_key(root, "claimed", "r1", root), "claimed")
    receipt(root, cron_working.delivery_key(root, "old-spelling", "r1", root), "canonical")
    receipt(root, cron_working.delivery_key(root, "settled", "r1", root), "settled")
    receipt(root, cron_working.delivery_key(root, "failed", "r1", root), "failed")
    receipt(cro, cron_working.delivery_key(cro, "own-cro", "r1", cro), "claimed")
    found = run(root, cro, jobs)
    assert [(f["profile"], f["id"], f["status"]) for f in found] == [
        ("default", "queued", "queued"), ("default", "claimed", "claimed"), ("default", "old-spelling", "queued"),
        ("cro", "own-cro", "claimed")]
    assert found[0]["run"] == "r1" and found[0]["since"].endswith("Z")


def test_output_handed_to_another_bot(homes):
    root, cro = homes
    jobs = {"default": [job("to-cro", deliver="bot-chat:cro"), job("failure-to-cro", deliver="local",
                                                                    failure_deliver="Bot-Chat:cro"),
                        job("listed", deliver=["telegram", "bot-chat:cro"])]}
    for job_id in ("to-cro", "failure-to-cro", "listed"):
        receipt(cro, cron_working.delivery_key(root, job_id, "r1", cro), "claimed")
    assert [f["id"] for f in run(root, cro, jobs)] == ["to-cro", "failure-to-cro", "listed"]


def test_only_the_latest_run_fresh_and_readable(homes):
    root, cro = homes
    jobs = {"default": [job("newer-run", run_id="r2"), job("stale"), job("no-run", latest_execution=None),
                        job("garbled")]}
    receipt(root, cron_working.delivery_key(root, "newer-run", "r1", root), "claimed")  # the run before
    receipt(root, cron_working.delivery_key(root, "stale", "r1", root), "queued", age=cron_working.STALE_AFTER + 60)
    garbled = Path(root).joinpath(*cron_working.DELIVERY_DIR, cron_working.delivery_key(root, "garbled", "r1", root) + ".json")
    garbled.write_text("{not json")
    assert run(root, cro, jobs) == []


def test_a_profile_that_cannot_list_is_skipped(homes):
    root, cro = homes
    receipt(cro, cron_working.delivery_key(cro, "j", "r1", cro), "claimed")

    def jobs_for(name, home):
        if name == "default":
            raise OSError("locked")
        return [job("j")]

    found = cron_working.working([("default", root), ("cro", cro)], jobs_for, lambda name: root / "profiles" / name)
    assert [(f["profile"], f["id"]) for f in found] == [("cro", "j")]
