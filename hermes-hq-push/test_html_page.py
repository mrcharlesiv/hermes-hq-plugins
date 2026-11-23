"""The page skill (skills/html-page) against the real Hermes: run with Hermes's own python from the plugin folder, e.g.
``env -u HERMES_AGENT PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_html_page.py``."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

HERE = Path(__file__).resolve().parent
SKILL = HERE / "skills" / "html-page"

_spec = importlib.util.spec_from_file_location("check_page_under_test", SKILL / "check_page.py")
check_page = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_page)

needs_chrome = pytest.mark.skipif(not check_page.find_chromes(), reason="no Chrome or Chromium here")

# A page made the way bots make them for a desktop window (invented content): a title, two checkboxes, a menu and a
# button in one wrapping row; words in a fixed dark colour; a hint padded in further than the map; "click".
DESKTOP_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Trail huts</title>
<style>*{box-sizing:border-box}body{margin:0;color:#173745;font:16px system-ui}
header{padding:12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}label{white-space:nowrap}
#hint{padding:10px 14px}#map{height:300px;background:#cfe3e3}</style></head><body>
<header><strong>North Ridge · 14 huts</strong><label style="color:#1261ab"><input type=checkbox checked> Staffed · 9</label>
<label style="color:#aa341e"><input type=checkbox checked> Self-serve · 5</label>
<select><option>Find a hut…</option></select><button>Show all</button></header>
<p id=hint>Click a hut to see its beds.</p><div id=map></div></body></html>"""


def _frontmatter(text):
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n")[1])


def _helper_scope():
    source = (HERE / "__init__.py").read_text()
    helper = source[source.index("HTML_PAGE_SKILL ="):source.index("def _short_titles")]
    scope = {"Path": Path, "__file__": str(HERE / "__init__.py"), "log": __import__("logging").getLogger("t")}
    exec(helper, scope)
    return scope


def test_the_skill_names_itself_and_its_files_exist():
    meta = _frontmatter((SKILL / "SKILL.md").read_text())
    assert meta["name"] == "html-page"
    assert "phone" in meta["description"] and "dashboard-tile" in meta["description"]
    body = (SKILL / "SKILL.md").read_text()
    for name in ("template.html", "check_page.py"):
        assert "${HERMES_SKILL_DIR}/" + name in body
        assert (SKILL / name).is_file()


def test_hermes_offers_the_skill_and_a_line_in_every_prompt_pointing_at_it():
    from hermes_cli.plugins import PluginContext, get_plugin_manager
    from hermes_cli.plugins_manifest import PluginManifest
    from tools.skills_tool import skill_view

    manager = get_plugin_manager()
    manager._system_prompt_sections.pop("hermes-hq-push.pages", None)
    ctx = PluginContext(PluginManifest(name="hermes-hq-push", path=str(HERE), source="user"), manager)
    scope = _helper_scope()
    scope["_html_page_skill"](ctx)
    assert manager.find_plugin_skill("hermes-hq-push:html-page") == SKILL / "SKILL.md"
    content = json.loads(skill_view("hermes-hq-push:html-page")).get("content", "")
    assert "Pages for Hermes HQ" in content
    assert str(SKILL) + "/check_page.py" in content, "Hermes fills in the skill's folder"
    section = manager._system_prompt_sections["hermes-hq-push.pages"]
    assert "hermes-hq-push:html-page" in section.content and len(section.content) <= section.max_chars
    manager._system_prompt_sections.pop("hermes-hq-push.pages", None)


def test_an_old_hermes_without_plugin_skills_is_left_alone():
    _helper_scope()["_html_page_skill"](object())   # no register_skill: nothing raised


def test_the_page_gets_the_apps_theme_first_and_the_probe_last():
    for page in (DESKTOP_PAGE, "<p>just words</p>", "<html><body><p>no head</p></body></html>"):
        out = check_page.dressed(page, "dark")
        assert out.index('id="hq-check-prelude"') < out.index("<p")
        assert out.rindex("__hqCheckResult") > out.rindex("<p")
    assert check_page.dressed(DESKTOP_PAGE, "dark").startswith("<!doctype html>"), "the doctype stays first"
    assert "#101218" in check_page.prelude("dark") and "#f2f2f7" in check_page.prelude("light")


@needs_chrome
def test_a_desktop_page_is_told_what_falls_apart_on_a_phone(tmp_path, capsys, monkeypatch):
    page = tmp_path / "huts.html"
    page.write_text(DESKTOP_PAGE)
    monkeypatch.setattr(sys, "argv", ["check_page.py", str(page), "--offline"])
    assert check_page.main() == 1
    out = capsys.readouterr().out
    assert "PROBLEM: “North Ridge · 14 huts” is hard to read" in out
    assert "shares a row with controls" in out
    assert "left edges don't line up" in out
    assert "on a phone it's “tap”" in out
    assert "is 19 tall" in out or "tall: make controls at least 44" in out
    pictures = [line.strip() for line in out.splitlines() if line.strip().endswith(".png")]
    assert len(pictures) == 3 and all(Path(p).stat().st_size > 1000 for p in pictures)
    assert not list(tmp_path.glob(".hq-page-check-*")), "the dressed copies are cleaned up"


@needs_chrome
def test_the_starter_page_passes_its_own_check(tmp_path, capsys, monkeypatch):
    page = tmp_path / "start.html"
    page.write_text((SKILL / "template.html").read_text())
    monkeypatch.setattr(sys, "argv", ["check_page.py", str(page), "--offline"])
    assert check_page.main() == 0
    assert "OK: nothing found at any size." in capsys.readouterr().out


@needs_chrome
def test_a_checkbox_on_another_line_than_its_words_is_a_problem(tmp_path, capsys, monkeypatch):
    page = tmp_path / "box.html"
    page.write_text("<!doctype html><body><label style='display:block;width:60px'><input type=checkbox>"
                    "<span style='display:block'>Breakfast included</span></label></body>")
    monkeypatch.setattr(sys, "argv", ["check_page.py", str(page), "--offline"])
    assert check_page.main() == 1
    assert "is on another line than its words" in capsys.readouterr().out


@needs_chrome
def test_a_page_wider_than_the_phone_is_a_problem(tmp_path, capsys, monkeypatch):
    page = tmp_path / "wide.html"
    page.write_text("<!doctype html><body><div style='width:900px'>A very wide strip</div></body>")
    monkeypatch.setattr(sys, "argv", ["check_page.py", str(page), "--offline"])
    assert check_page.main() == 1
    assert "it scrolls sideways" in capsys.readouterr().out
