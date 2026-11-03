"""short_titles against the real Hermes: run with Hermes's own python from the plugin folder, e.g.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_short_titles.py`` (HERMES_AGENT names another checkout)."""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

from agent import title_generator as titles  # noqa: E402

_spec = importlib.util.spec_from_file_location("short_titles_under_test", Path(__file__).with_name("short_titles.py"))
short_titles = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(short_titles)


def _stock(fn):
    while getattr(fn, short_titles.MARK, False):
        fn = fn.__wrapped__
    return fn


@pytest.fixture(autouse=True)
def stock_hermes():
    """Every test starts from Hermes's own functions and prompt and gets them back afterwards."""
    originals = (_stock(titles.generate_title), _stock(titles.derive_title), titles._TITLE_PROMPT_TEMPLATE)
    titles.generate_title, titles.derive_title, _ = originals
    yield
    titles.generate_title, titles.derive_title, titles._TITLE_PROMPT_TEMPLATE = originals


def _model_says(monkeypatch, title):
    """The title model answers ``title``; returns the prompts it was sent."""
    sent = []

    def call_llm(**kwargs):
        sent.append(kwargs["messages"][0]["content"])
        message = SimpleNamespace(content='{"title": "%s"}' % title, reasoning=None, reasoning_content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(titles, "call_llm", call_llm)
    monkeypatch.setattr(titles, "_auto_title_enabled", lambda: True)
    monkeypatch.setattr(titles, "_title_language", lambda: "")
    return sent


@pytest.mark.parametrize("given, expected", [
    ("Fix login button", "Fix login button"),
    ("Fix login button on mobile", "Fix login button on mobile"),  # 26: fits
    ("Postgres connection pool exhaustion", "Postgres connection pool"),
    ("Plan the quarterly review for the sales team", "Plan the quarterly review"),
    ("Book flights and hotel for Tokyo trip", "Book flights and hotel"),  # no "…for"
    ("Supercalifragilisticexpialidocious-and-more", "Supercalifragilisticexpialid"),
    ("", ""),
])
def test_shorten(given, expected):
    out = short_titles.shorten(given)
    assert out == expected and len(out) <= short_titles.LIMIT


def test_shorten_with_mark_counts_it():
    out = short_titles.shorten("Can you look at why the nightly backup failed again…", mark="…")
    assert out == "Can you look at why…" and len(out) <= short_titles.LIMIT


def test_shorten_leaves_none_alone():
    assert short_titles.shorten(None) is None


def test_install_asks_for_a_phone_line(monkeypatch):
    assert short_titles.install() == "installed"
    assert short_titles.install() == "installed"  # once per process
    sent = _model_says(monkeypatch, "Nightly backup failure")
    assert titles.generate_title("why did the nightly backup fail?") == "Nightly backup failure"
    prompt = sent[0]
    assert short_titles.RULE.strip() in prompt
    assert prompt.count(" words") >= 1 and "3 to 7 words" not in prompt
    assert '{"title": "..."}' in prompt  # the rest of Hermes's prompt is still there


def test_long_model_title_is_shortened(monkeypatch):
    short_titles.install()
    _model_says(monkeypatch, "Postgres connection pool limit reached")
    assert titles.generate_title("the pool keeps running out") == "Postgres connection pool"


def test_answer_instead_of_title_is_still_refused(monkeypatch):
    """Hermes's guard sees the model's whole answer before anything is shortened."""
    short_titles.install()
    answer = " ".join(["word"] * (titles._MAX_TITLE_WORDS + 1))
    _model_says(monkeypatch, answer)
    assert titles.generate_title("hello?") is None


def test_instant_title_is_shortened():
    short_titles.install()
    out = titles.derive_title("Can you look into why the nightly backup job failed again last night")
    assert out and len(out) <= short_titles.LIMIT
    assert titles.derive_title("Weekly sales report") == "Weekly sales report"


def test_auto_title_session_uses_the_short_names(monkeypatch):
    """The namer Hermes actually calls reaches the wrappers (it looks them up on the module each time)."""
    short_titles.install()
    _model_says(monkeypatch, "Investigate flaky auth test on the CI runner")
    stored = {}

    def persist(session_db, session_id, title, *, source, dedupe=True):
        stored[source] = title
        return title

    monkeypatch.setattr(titles, "_persist_session_title", persist)
    monkeypatch.setattr(titles, "_has_upgraded_title", lambda *a: False, raising=False)
    titles.auto_title_session(object(), "s1", "the auth test keeps failing on CI, can you dig in?")
    assert stored and all(len(t) <= short_titles.LIMIT for t in stored.values()), stored


def test_reworked_hermes_is_left_alone(monkeypatch):
    monkeypatch.delattr(titles, "_TITLE_PROMPT_TEMPLATE")
    assert short_titles.install() == "absent"
