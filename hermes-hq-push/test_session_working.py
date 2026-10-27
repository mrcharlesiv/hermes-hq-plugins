"""session_working against a ledger shaped like Hermes's (state.db's session_admissions / worker_executions):
``python3 -m pytest -q test_session_working.py``."""
import importlib.util
import sqlite3
from pathlib import Path

_spec = importlib.util.spec_from_file_location("session_working_under_test", Path(__file__).with_name("session_working.py"))
session_working = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_working)


def make_db(home: Path, admissions=(), workers=(), sessions=()):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, chat_id TEXT);
        CREATE TABLE session_admissions (seq INTEGER PRIMARY KEY, target_session_id TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE worker_executions (execution_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
                                        status TEXT NOT NULL);
    """)
    conn.executemany("INSERT INTO sessions VALUES (?, ?)", sessions)
    conn.executemany("INSERT INTO session_admissions (target_session_id, status) VALUES (?, ?)", admissions)
    conn.executemany("INSERT INTO worker_executions VALUES (?, ?, ?, ?)",
                     [(f"x{i}", sid, kind, status) for i, (sid, kind, status) in enumerate(workers)])
    conn.commit()
    conn.close()


def test_only_work_that_ends_on_its_own(tmp_path):
    make_db(tmp_path / "default",
            admissions=[("a", "started"), ("b", "queued"), ("c", "terminal"), ("d", "unknown")],
            workers=[("e", "cron", "running"), ("f", "child", "terminal"), ("g", "compute", "registered")])
    rows = session_working.working_in(tmp_path / "default")
    assert [(r["id"], r["status"]) for r in rows] == [("a", "started"), ("b", "queued"), ("e", "running"), ("g", "registered")]


def test_best_status_wins_and_local_chat_ids_map_to_rows(tmp_path):
    make_db(tmp_path / "default",
            admissions=[("a", "queued"), ("a", "started"), ("local-1", "started")],
            sessions=[("a", None), ("20261027_1", "local-1")])
    rows = {r["id"]: r for r in session_working.working_in(tmp_path / "default")}
    assert rows["a"]["status"] == "started"
    assert rows["20261027_1"]["status"] == "started"


def test_profiles_and_filters(tmp_path):
    make_db(tmp_path / "default", admissions=[("a", "started")])
    make_db(tmp_path / "cmo", admissions=[("b", "started")])
    (tmp_path / "empty").mkdir()
    profiles = [("default", tmp_path / "default"), ("cmo", tmp_path / "cmo"), ("empty", tmp_path / "empty")]
    assert [(r["profile"], r["id"]) for r in session_working.working(profiles)] == [("default", "a"), ("cmo", "b")]
    assert session_working.working(profiles, only_profile="cmo") == [{"profile": "cmo", "id": "b", "status": "started",
                                                                      "kind": "admission"}]
    assert session_working.working(profiles, only_session="a")[0]["profile"] == "default"
    assert session_working.working(profiles, only_session="zzz") == []


def test_no_ledger_tables_reads_as_nothing(tmp_path):
    home = tmp_path / "old"
    home.mkdir()
    sqlite3.connect(home / "state.db").close()
    assert session_working.working_in(home) == []
