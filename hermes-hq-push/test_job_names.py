"""The job names note (0.9.81) against the real Hermes: run with Hermes's own python from the plugin folder, e.g.
``env -u HERMES_AGENT PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_job_names.py``."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

HERE = Path(__file__).resolve().parent


def _helper_scope():
    source = (HERE / "__init__.py").read_text()
    helper = source[source.index("JOB_NAMES_NOTE ="):source.index("def _short_titles")]
    scope = {"log": __import__("logging").getLogger("t")}
    exec(helper, scope)
    return scope


def test_every_prompt_asks_for_a_sentence_and_a_comment_naming_a_background_job():
    from hermes_cli.plugins import PluginContext, get_plugin_manager
    from hermes_cli.plugins_manifest import PluginManifest

    manager = get_plugin_manager()
    manager._system_prompt_sections.pop("hermes-hq-push.jobs", None)
    ctx = PluginContext(PluginManifest(name="hermes-hq-push", path=str(HERE), source="user"), manager)
    _helper_scope()["_job_names_note"](ctx)
    section = manager._system_prompt_sections["hermes-hq-push.jobs"]
    assert len(section.content) <= section.max_chars
    assert "in the background" in section.content and "sentence" in section.content
    assert "first line a short comment" in section.content and "# " in section.content
    manager._system_prompt_sections.pop("hermes-hq-push.jobs", None)


def test_it_is_registered_at_load():
    source = (HERE / "__init__.py").read_text()
    register = source[source.index("def register("):]
    assert "_job_names_note(ctx)" in register


def test_an_old_hermes_without_prompt_sections_is_left_alone():
    _helper_scope()["_job_names_note"](object())   # no register_system_prompt_section: nothing raised
