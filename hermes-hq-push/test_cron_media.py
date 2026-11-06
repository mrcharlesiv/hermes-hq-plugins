"""cron_media against the real Hermes: run with Hermes's own python from the plugin folder, e.g.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_cron_media.py`` (HERMES_AGENT names another checkout)."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

from cron.scheduler_delivery import bot_chat_message  # noqa: E402

_spec = importlib.util.spec_from_file_location("cron_media_under_test", Path(__file__).with_name("cron_media.py"))
cron_media = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cron_media)


def _admission(job_output):
    return bot_chat_message({"id": "abc123", "name": "Ship handoff"}, job_output)


@pytest.fixture
def images(tmp_path):
    a, b = tmp_path / "01-update.png", tmp_path / "02-verified.png"
    a.write_bytes(b"\x89PNG one")
    b.write_bytes(b"\x89PNG two")
    return str(a), str(b)


@pytest.fixture(autouse=True)
def fresh():
    cron_media._pending.clear()
    yield
    cron_media._pending.clear()


def test_marker_matches_hermes_bot_chat_wrapper():
    assert cron_media.MARKER in _admission("hello")


def test_summary_that_turned_pictures_into_paths_gets_them_back(images):
    a, b = images
    cron_media.note_turn("s1", "t1", _admission(f"Build 858 is up.\nMEDIA:{a}\nMEDIA:{b}"))
    summary = f"858 is up. Screenshots:\n- {a}\n- {b}"
    out = cron_media.carry(summary, "s1", "t1")
    assert out.startswith(summary) and out.endswith(f"\n\nMEDIA:{a}\nMEDIA:{b}")


def test_a_kept_picture_is_not_repeated(images):
    a, b = images
    cron_media.note_turn("s1", "t1", _admission(f"MEDIA:{a}\nMEDIA:{b}"))
    out = cron_media.carry(f"Here: MEDIA:{a}", "s1", "t1")
    assert out.count(f"MEDIA:{a}") == 1 and out.endswith(f"MEDIA:{b}")


def test_all_kept_leaves_the_reply_alone(images):
    a, _ = images
    cron_media.note_turn("s1", "t1", _admission(f"MEDIA:{a}"))
    assert cron_media.carry(f"MEDIA:{a}", "s1", "t1") is None


def test_missing_files_are_never_invented(tmp_path):
    cron_media.note_turn("s1", "t1", _admission(f"MEDIA:{tmp_path / 'gone.png'}"))
    assert cron_media.carry("Summary.", "s1", "t1") is None


def test_ordinary_turns_are_untouched(images):
    a, _ = images
    cron_media.note_turn("s1", "t1", f"look at MEDIA:{a}")
    assert cron_media.carry("ok", "s1", "t1") is None


def test_silent_and_empty_stay_as_they_are(images):
    a, _ = images
    cron_media.note_turn("s1", "t1", _admission(f"MEDIA:{a}"))
    assert cron_media.carry("[SILENT]", "s1", "t1") is None
    cron_media.note_turn("s1", "t2", _admission(f"MEDIA:{a}"))
    assert cron_media.carry("", "s1", "t2") is None


def test_only_that_turn_and_only_once(images):
    a, _ = images
    cron_media.note_turn("s1", "t1", _admission(f"MEDIA:{a}"))
    assert cron_media.carry("next turn", "s1", "t2") is None
    assert cron_media.carry("this turn", "s1", "t1").endswith(f"MEDIA:{a}")
    assert cron_media.carry("this turn again", "s1", "t1") is None


def test_a_compression_that_renames_the_session_mid_turn_still_carries(images):
    a, _ = images
    cron_media.note_turn("s1", "s1:task:ab12", _admission(f"MEDIA:{a}"))
    assert cron_media.carry("summary", "s1-compressed", "s1:task:ab12").endswith(f"MEDIA:{a}")


def test_a_later_ordinary_turn_clears_nothing_it_should_not(images):
    a, _ = images
    cron_media.note_turn("s1", "t1", _admission(f"MEDIA:{a}"))
    cron_media.note_turn("s1", "t1", "a retry of the same turn that is not a job")
    assert cron_media.carry("reply", "s1", "t1") is None


def test_prose_mention_is_not_an_attachment():
    assert cron_media.admission_media(cron_media.MARKER + " use MEDIA: tags for files") == []


def test_memory_stays_bounded(images):
    a, _ = images
    for i in range(cron_media._KEEP + 10):
        cron_media.note_turn("s", f"t{i}", _admission(f"MEDIA:{a}"))
    assert len(cron_media._pending) == cron_media._KEEP


def test_hooks_are_wired(images, monkeypatch):
    """The add-on's own pre_llm_call + transform_llm_output carry a job's pictures end to end."""
    plugin = sys.modules.get("hermes_hq_push_under_test")
    if plugin is None:
        spec = importlib.util.spec_from_file_location(
            "hermes_hq_push_under_test", Path(__file__).with_name("__init__.py"),
            submodule_search_locations=[str(Path(__file__).parent)])
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
    plugin.cron_media._pending.clear()
    a, b = images
    plugin.pre_llm_call(platform="tui", user_message=_admission(f"MEDIA:{a}\nMEDIA:{b}"), session_id="s9", turn_id="t9")
    out = plugin.transform_llm_output(response_text=f"Two shots: {a}, {b}", session_id="s9", turn_id="t9",
                                      model="m", platform="tui")
    assert out.endswith(f"MEDIA:{a}\nMEDIA:{b}")
    import inspect  # register() also installs the desktop theme etc., so read it rather than run it here
    assert 'register_hook("transform_llm_output", transform_llm_output)' in inspect.getsource(plugin.register)
