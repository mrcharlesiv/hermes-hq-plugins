"""[0.9.52] plugin_updates: the owner's approved update runs Hermes's own command and answers only what was approved."""
import importlib.util
import sys
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("hq_plugin_updates_test", Path(__file__).parent / "plugin_updates.py")
updates = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(updates)


def wait(key):
    for _ in range(200):
        found = updates.job(key)
        if found["state"] != "running":
            return found
        time.sleep(0.02)
    raise AssertionError("the update never finished")


def test_the_command_is_hermes_own_update_for_the_bot():
    assert updates.command("default", "homeassistant", False)[-3:] == ["plugins", "update", "homeassistant"]
    assert updates.command("finance", "homeassistant", True)[-6:] == ["-p", "finance", "plugins", "update", "homeassistant", "--force"]
    assert updates.command("default", "x", False)[0] == sys.executable


def test_only_the_approved_questions_are_answered_yes():
    packages = "homeassistant declares Python dependencies:\n  - tzdata\n  Prepare these with Hermes through PM now? [y/N]: "
    assert updates.answer_for(packages, False) == "y"
    assert updates.answer_for("installing…\n", False) is None, "not asking: nothing typed"
    wider = "+ tool: clip_page\n  Apply this update? [y/N]: "
    assert updates.answer_for(wider, False) == "n", "a wider plugin the owner didn't accept"
    assert updates.answer_for(wider, True) == "y"
    assert updates.answer_for("  Update anyway? Only continue if you trust the source. [y/N]: ", True) == "n"
    assert updates.answer_for("  Enable 'x' now? [y/N]: ", True) == "n", "any other question is no"
    wrapped = "Prepare these with Hermes\nthrough PM now? \x1b[1m[y/N]\x1b[0m: "
    assert updates.answer_for(wrapped, False) == "y", "wrapped or coloured, still the same question"


def test_bad_names_are_refused():
    for profile, name in (("default", "../etc"), ("a b", "x"), ("default", ""), ("default", "x; rm -rf /"), ("-p", "x")):
        with pytest.raises(updates.UpdateError) as caught:
            updates.start(profile, name, runner=lambda *a: (0, "", []))
        assert caught.value.status_code == 400


def test_a_job_says_how_the_update_went():
    calls = []

    def ok(cmd, accept):
        calls.append((cmd, accept))
        return 0, "homeassistant declares Python dependencies:\n - tzdata\nPrepare these with Hermes through PM now? [y/N]: y\nUpdated homeassistant.\n", ["y"]

    key = updates.start("finance", "homeassistant", accept_capabilities=True, runner=ok)["job"]
    done = wait(key)
    assert done["state"] == "done" and done["code"] == 0 and done["packages"] is True and done["unchanged"] is False
    assert "Updated homeassistant." in done["output"]
    assert calls[0][0][-5:] == ["-p", "finance", "plugins", "update", "homeassistant"] and calls[0][1] is True

    key = updates.start("default", "honcho", runner=lambda *a: (1, "Error: the security scan blocked it\n", []))["job"]
    failed = wait(key)
    assert failed["state"] == "failed" and "security scan" in failed["output"]

    key = updates.start("default", "slow", runner=lambda *a: (-1, "…", []))["job"]
    assert wait(key)["output"] == "The update took too long and was stopped."

    key = updates.start("default", "same", runner=lambda *a: (0, "Already up to date.\n", []))["job"]
    assert wait(key)["unchanged"] is True

    with pytest.raises(updates.UpdateError) as caught:
        updates.job("nope")
    assert caught.value.status_code == 404


def test_one_update_of_a_plugin_at_a_time():
    import threading
    gate = threading.Event()

    def slow(cmd, accept):
        gate.wait(2)
        return 0, "done\n", []

    first = updates.start("default", "busy", runner=slow)["job"]
    assert updates.start("default", "busy", runner=slow)["job"] == first, "the running one, not a second"
    assert updates.start("other", "busy", runner=slow)["job"] != first, "another bot's copy is its own update"
    gate.set()
    assert wait(first)["state"] == "done"


@pytest.mark.skipif(not updates.supported(), reason="needs a pseudo-terminal")
def test_a_real_terminal_gets_the_answer():
    script = ("import sys\n"
              "print('x declares Python dependencies:')\n"
              "a = input('  Prepare these with Hermes through PM now? [y/N]: ')\n"
              "b = input('  Enable it now? [y/N]: ')\n"
              "print('answers', a, b, sys.stdin.isatty())\n"
              "sys.exit(0 if (a, b) == ('y', 'n') else 3)\n")
    code, output, answers = updates.run([sys.executable, "-c", script], timeout=20)
    assert code == 0 and answers == ["y", "n"]
    assert "answers y n True" in output
    code, _, _ = updates.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert code == -1, "stopped when it runs out of time"
