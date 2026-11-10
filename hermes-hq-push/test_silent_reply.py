"""silent_reply: "[SILENT]" and a model's own bracketed stand-in for it send no alert. Run from the plugin folder,
e.g. ``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_silent_reply.py``."""
import pytest

from push import silent_reply, without_closing_marker


@pytest.mark.parametrize("text", [
    "[SILENT]", " [ silent ] ",
    "[Notification processed; no additional user-facing response.]",
    "(no user-facing response)",
    "*[No reply needed]*",
    "_(nothing further to report)_",
    "[No further response required.]",
    "(no user‑facing reply)",
    "NO_REPLY", " no_reply\n", "NO REPLY", "NO_REPLY.", "*NO_REPLY*", "SILENT", "[静默]",
])
def test_stand_ins_are_silent(text):
    assert silent_reply(text)


@pytest.mark.parametrize("text", [
    "", "No action needed.", "[No response from the server]",
    "Done. [no additional response]  Also, here is the summary.",
    "[no additional response]\n[second line]",
    "Here is what I found: nothing to report on the inbox.",
    "I'll answer NO_REPLY to the CRO's next ping.", "[NO_REPLY", "NO_REPLY_TOKEN",
])
def test_real_replies_alert(text):
    assert not silent_reply(text)


def test_a_closing_marker_leaves_the_alert():
    assert without_closing_marker("Here is the answer.\n\nNO_REPLY") == "Here is the answer."
    assert without_closing_marker("NO_REPLY") == "NO_REPLY"
    assert without_closing_marker("Line one\nsays NO_REPLY here") == "Line one\nsays NO_REPLY here"
