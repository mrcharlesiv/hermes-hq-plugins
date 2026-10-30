"""serve_warmup: the background warm-up and the freeze recorder. Run with Hermes's own python from the plugin folder,
e.g. ``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_serve_warmup.py`` (HERMES_AGENT names another checkout)."""
import asyncio
import contextlib
import importlib.util
import os
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))


def _fresh():
    spec = importlib.util.spec_from_file_location("serve_warmup_under_test", Path(__file__).with_name("serve_warmup.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clear_marks():
    for holder in (sys, sys.modules.get("tui_gateway.server")):
        for name in ("warmup", "stalls"):
            if holder is not None and hasattr(holder, "_hermes_hq_serve_warmup" + name):
                delattr(holder, "_hermes_hq_serve_warmup" + name)


def test_once_per_process_even_when_loaded_again_for_another_bot():
    _clear_marks()
    first, second = _fresh(), _fresh()  # Hermes loads the add-on again in every bot's scope
    assert first._claim("warmup") is True
    assert second._claim("warmup") is False
    _clear_marks()


def test_off_file_stops_it(tmp_path):
    _clear_marks()
    (tmp_path / "warmup.off").write_text("")
    assert _fresh().start_warmup(tmp_path) == "off"


def test_a_hermes_without_the_pieces_is_left_alone(tmp_path, monkeypatch):
    _clear_marks()
    module = _fresh()
    monkeypatch.setattr(module, "_pieces", lambda: None)
    assert module.start_warmup(tmp_path) == "absent"


def test_warms_every_bot_once_in_its_own_scope_and_survives_one_failing(tmp_path, monkeypatch):
    module = _fresh()
    monkeypatch.setattr(module, "START_DELAY", 0)
    monkeypatch.setattr(module, "PAUSE_BETWEEN", 0)
    monkeypatch.setattr(module, "_multi_bot_host", lambda: True)
    homes = [tmp_path / "finance", tmp_path / "broken", tmp_path / "sam"]
    current, seen = [], []

    @contextlib.contextmanager
    def scope(session):
        current.append(session["profile_home"])
        try:
            yield
        finally:
            current.pop()

    def discover():
        if current[-1].endswith("broken"):
            raise RuntimeError("plugin exploded")
        seen.append(Path(current[-1]).name)

    monkeypatch.setattr(module, "_pieces", lambda: (scope, discover, lambda: homes))
    module._warm(tmp_path / "warmup.off")
    assert seen == ["finance", "sam"]


def test_single_bot_host_warms_nothing(tmp_path, monkeypatch):
    module = _fresh()
    monkeypatch.setattr(module, "START_DELAY", 0)
    monkeypatch.setattr(module, "_multi_bot_host", lambda: False)
    monkeypatch.setattr(module, "_pieces", lambda: (_ for _ in ()).throw(AssertionError("must not look for bots")))
    module._warm(tmp_path / "warmup.off")


def test_frozen_loop_writes_every_threads_stack(tmp_path, monkeypatch):
    _clear_marks()
    module = _fresh()
    monkeypatch.setattr(module, "_launch_home", lambda: tmp_path)
    monkeypatch.setattr(module, "TICK", 0.2)
    monkeypatch.setattr(module, "LATE", 0.4)

    def a_function_that_freezes_the_loop():
        time.sleep(1.5)

    async def main():
        assert module.watch_loop(asyncio.get_running_loop()) is True
        await asyncio.sleep(0.5)
        a_function_that_freezes_the_loop()
        await asyncio.sleep(0.5)

    asyncio.run(main())
    import faulthandler
    faulthandler.cancel_dump_traceback_later()
    text = (tmp_path / "logs" / module.STACKS_FILE).read_text()
    assert "a_function_that_freezes_the_loop" in text
    assert "after a 1." in text
    _clear_marks()


def test_real_hermes_has_the_pieces():
    """Against the real Hermes checkout: the scope, discovery and profile listing it relies on exist."""
    module = _fresh()
    assert module._pieces() is not None
    assert "_session_profile_runtime_scope" in vars(sys.modules["tui_gateway.server"])
    assert isinstance(sys.modules["tui_gateway.server"], types.ModuleType)
