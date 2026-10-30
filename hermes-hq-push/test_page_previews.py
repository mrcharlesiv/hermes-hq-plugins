"""page_previews against the real Hermes: run with Hermes's own python from the plugin folder, e.g.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_page_previews.py`` (HERMES_AGENT names another checkout)."""
import importlib.util
import inspect
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

import hermes_state_sessions as sessions  # noqa: E402
from hermes_state import SessionDB  # noqa: E402

_spec = importlib.util.spec_from_file_location("page_previews_under_test", Path(__file__).with_name("page_previews.py"))
page_previews = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(page_previews)

FIRST = "first message of "
LISTER = next(c for c in SessionDB.__mro__ if "list_sessions_rich" in vars(c))
READER = next(c for c in SessionDB.__mro__ if "_read_all" in vars(c))
# A Hermes with the fix (it picks the page first) gets "not needed" and runs only the tests marked for it.
FIXED = "page AS (" in inspect.getsource(vars(LISTER)["list_sessions_rich"])
stock_only = pytest.mark.skipif(FIXED, reason="this Hermes picks the page first")


def _stock(fn):
    while getattr(fn, page_previews.MARK, False):
        fn = fn.__wrapped__
    return fn


@pytest.fixture(autouse=True)
def stock_hermes():
    """Every test starts from Hermes's own functions and gets them back afterwards."""
    originals = (_stock(sessions._sql_preview_raw), _stock(vars(LISTER)["list_sessions_rich"]),
                 _stock(vars(READER)["_read_all"]))

    def restore():
        sessions._sql_preview_raw, LISTER.list_sessions_rich, READER._read_all = originals

    restore()
    yield
    restore()


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    for i in range(30):
        sid = f"s{i:02d}"
        db.create_session(sid, "cron" if i % 3 else "telegram", user_id="u")
        db.append_messages_batch(sid, [
            {"role": "user", "content": f"{FIRST}{sid}", "timestamp": 1_000 + i},
            {"role": "assistant", "content": "done", "timestamp": 1_000.5 + i},
        ])
    db.set_session_pinned("s02", True)
    db.set_session_archived("s05", True, notify=False)
    db.set_session_pinned("s05", True)                                  # a pinned archived chat still lists
    db.set_session_archived("s07", True, notify=False)
    db.close()
    return path


def _reads(monkeypatch):
    """The sessions whose first message SQLite reads: the preview's eligibility test calls INSTR on it."""
    read = set()
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)

        def instr(haystack, needle):
            if haystack is None or needle is None:
                return None
            if isinstance(haystack, str) and haystack.startswith(FIRST):
                read.add(haystack[len(FIRST):])
            return haystack.find(needle) + 1

        conn.create_function("instr", 2, instr, deterministic=True)
        return conn

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    return read


SHAPES = {
    "sidebar": dict(limit=3, order_by_last_active=True, min_message_count=1, compact_rows=True, include_pinned=True),
    "full rows": dict(limit=5, order_by_last_active=True, compact_rows=False),
    "offset": dict(limit=4, offset=6, order_by_last_active=True, compact_rows=True),
    "cron": dict(source="cron", limit=50, order_by_last_active=True, compact_rows=True, include_pinned=True),
    "search": dict(limit=10, order_by_last_active=True, search_query="s1"),
    "archived": dict(limit=40, order_by_last_active=True, include_archived=True, include_pinned=True),
    "by start": dict(limit=5),
}


@stock_only
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_list_is_the_same_as_hermes_s_own(path, shape):
    db = SessionDB(db_path=path)
    stock = db.list_sessions_rich(**SHAPES[shape])
    assert page_previews.install() == "installed"
    assert db.list_sessions_rich(**SHAPES[shape]) == stock
    assert stock and all(r["preview"].startswith(FIRST) for r in stock)
    db.close()


@stock_only
def test_only_the_page_s_first_messages_are_read(path, monkeypatch):
    assert page_previews.install() == "installed"
    read = _reads(monkeypatch)
    db = SessionDB(db_path=path)
    rows = db.list_sessions_rich(limit=3, order_by_last_active=True, min_message_count=1, compact_rows=True)
    db.close()
    assert [r["id"] for r in rows] == ["s29", "s28", "s27"]
    assert [r["preview"] for r in rows] == [f"{FIRST}s29", f"{FIRST}s28", f"{FIRST}s27"]
    assert read == {"s29", "s28", "s27"}


@stock_only
def test_stock_hermes_reads_every_chat_s_first_message(path, monkeypatch):
    """The cost the wrapper removes; when this fails, Hermes picks the page first and install says "not needed"."""
    read = _reads(monkeypatch)
    db = SessionDB(db_path=path)
    db.list_sessions_rich(limit=3, order_by_last_active=True, min_message_count=1, compact_rows=True)
    db.close()
    assert len(read) == 28                                              # every chat it admits (two are archived)


@stock_only
def test_a_failed_follow_up_read_runs_hermes_s_own_query(path, monkeypatch):
    db = SessionDB(db_path=path)
    stock = db.list_sessions_rich(**SHAPES["sidebar"])
    assert page_previews.install() == "installed"

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("no such column")

    monkeypatch.setattr(page_previews, "_with_previews", broken)
    assert db.list_sessions_rich(**SHAPES["sidebar"]) == stock
    db.close()


@stock_only
def test_other_previews_and_lists_keep_hermes_s_sql():
    assert page_previews.install() == "installed"
    stock = _stock(sessions._sql_preview_raw)
    assert sessions._sql_preview_raw() == stock()                    # outside a list call
    assert sessions._sql_preview_raw("tip.id") == stock("tip.id")


@stock_only
def test_install_is_once_per_process():
    assert page_previews.install() == "installed"
    wrapped = vars(LISTER)["list_sessions_rich"]
    assert page_previews.install() == "installed"
    assert vars(LISTER)["list_sessions_rich"] is wrapped


def test_a_hermes_that_picks_the_page_first_is_left_alone(monkeypatch):
    def list_sessions_rich(self, **kwargs):
        """WITH RECURSIVE chain ... page AS ( ... ) {_sql_preview_raw()}"""

    monkeypatch.setattr(LISTER, "list_sessions_rich", list_sessions_rich)
    monkeypatch.setattr(page_previews.inspect, "getsource", lambda fn: fn.__doc__)
    assert page_previews.install() == "not needed"
    assert vars(LISTER)["list_sessions_rich"] is list_sessions_rich


@pytest.mark.skipif(not FIXED, reason="this Hermes still reads every chat's first message")
def test_a_fixed_hermes_gets_not_needed(path, monkeypatch):
    listing = vars(LISTER)["list_sessions_rich"]
    assert page_previews.install() == "not needed"
    assert vars(LISTER)["list_sessions_rich"] is listing
    read = _reads(monkeypatch)
    db = SessionDB(db_path=path)
    db.list_sessions_rich(limit=3, order_by_last_active=True, min_message_count=1, compact_rows=True)
    db.close()
    assert read == {"s29", "s28", "s27"}
