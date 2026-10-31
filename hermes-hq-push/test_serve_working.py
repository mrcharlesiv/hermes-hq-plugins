"""serve_working against a ``tui_gateway.server``-shaped module: ``python3 -m pytest -q test_serve_working.py``."""
import importlib.util
import sys
import threading
import types
from pathlib import Path

_spec = importlib.util.spec_from_file_location("serve_working_under_test", Path(__file__).with_name("serve_working.py"))
serve_working = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serve_working)


def test_only_running_sessions_count():
    sessions = {
        "rt-a": {"running": True, "session_key": "20261006_162543_89b56f", "profile_home": "/h/profiles/app"},
        "rt-b": {"running": False, "session_key": "idle"},
        "rt-c": {"session_key": "never-ran"},
    }
    rows = serve_working.working_in(sessions, lambda home: Path(home).name if home else "default")
    assert rows == [{"id": "20261006_162543_89b56f", "runtime": "rt-a", "profile": "app"}]


def test_a_failing_profile_lookup_still_reports_the_chat():
    def boom(_):
        raise RuntimeError("no profiles")
    rows = serve_working.working_in({"rt": {"running": True, "session_key": "s"}}, boom)
    assert rows == [{"id": "s", "runtime": "rt", "profile": ""}]


def test_not_a_chat_server_is_unknown(monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    assert serve_working.hermes_working() is None


def test_reads_the_loaded_gateway(monkeypatch):
    fake = types.ModuleType("tui_gateway.server")
    fake._sessions = {"rt": {"running": True, "session_key": "s1", "profile_home": None}}
    fake._sessions_lock = threading.RLock()
    fake.profile_name_for_home = lambda home: "default"
    monkeypatch.setitem(sys.modules, "tui_gateway.server", fake)
    assert serve_working.hermes_working() == [{"id": "s1", "runtime": "rt", "profile": "default"}]
    fake._sessions["rt"]["running"] = False
    assert serve_working.hermes_working() == []
