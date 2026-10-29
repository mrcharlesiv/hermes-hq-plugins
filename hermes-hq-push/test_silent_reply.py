"""silent_reply: "[SILENT]" and a model's own bracketed stand-in for it send no alert. Run from the plugin folder,
e.g. ``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_silent_reply.py``."""
import pytest

from push import silent_reply


@pytest.mark.parametrize("text", [
    "[SILENT]", " [ silent ] ",
    "[Notification processed; no additional user-facing response.]",
    "(no user-facing response)",
    "*[No reply needed]*",
    "_(nothing further to report)_",
    "[No further response required.]",
    "(no user‑facing reply)",
])
def test_stand_ins_are_silent(text):
    assert silent_reply(text)


@pytest.mark.parametrize("text", [
    "", "No action needed.", "[No response from the server]",
    "Done. [no additional response]  Also, here is the summary.",
    "[no additional response]\n[second line]",
    "Here is what I found: nothing to report on the inbox.",
])
def test_real_replies_alert(text):
    assert not silent_reply(text)
