"""The Dashboard tile skill against the real Hermes: run with Hermes's own python from the plugin folder, e.g.
``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_dashboard_tile.py`` (HERMES_AGENT names another checkout)."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

HERE = Path(__file__).resolve().parent
SKILL = HERE / "skills" / "dashboard-tile"

_spec = importlib.util.spec_from_file_location("check_tile_under_test", SKILL / "check_tile.py")
check_tile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_tile)


def _frontmatter(text):
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n")[1])


def test_the_skill_names_itself_and_its_files_exist():
    meta = _frontmatter((SKILL / "SKILL.md").read_text())
    assert meta["name"] == "dashboard-tile"
    assert "Dashboard" in meta["description"]
    body = (SKILL / "SKILL.md").read_text()
    for name in ("template.html", "check_tile.py"):
        assert "${HERMES_SKILL_DIR}/" + name in body
        assert (SKILL / name).is_file()


def test_hermes_offers_it_as_a_plugin_skill_with_its_folder_filled_in():
    from hermes_cli.plugins import PluginContext, get_plugin_manager
    from hermes_cli.plugins_manifest import PluginManifest
    from tools.skills_tool import skill_view

    manager = get_plugin_manager()
    ctx = PluginContext(PluginManifest(name="hermes-hq-push", path=str(HERE), source="user"), manager)
    init = importlib.util.spec_from_file_location("hermes_hq_push_init_source", HERE / "__init__.py")
    source = (HERE / "__init__.py").read_text()
    # The helper alone (the package's register() also starts hooks and watchers this test doesn't want).
    helper = source[source.index("DASHBOARD_SKILL ="):source.index("def _short_titles")]
    scope = {"Path": Path, "__file__": str(HERE / "__init__.py"), "log": __import__("logging").getLogger("t")}
    exec(helper, scope)
    scope["_dashboard_skill"](ctx)
    assert manager.find_plugin_skill("hermes-hq-push:dashboard-tile") == SKILL / "SKILL.md"
    seen = json.loads(skill_view("hermes-hq-push:dashboard-tile"))
    content = seen.get("content", "")
    assert "Dashboard tiles for Hermes HQ" in content
    assert str(SKILL) + "/template.html" in content, "Hermes fills in the skill's folder"
    assert init is not None


def test_an_old_hermes_without_plugin_skills_is_left_alone():
    source = (HERE / "__init__.py").read_text()
    helper = source[source.index("DASHBOARD_SKILL ="):source.index("def _short_titles")]
    scope = {"Path": Path, "__file__": str(HERE / "__init__.py"), "log": __import__("logging").getLogger("t")}
    exec(helper, scope)
    scope["_dashboard_skill"](object())   # no register_skill: nothing raised


def test_the_template_passes_its_own_file_checks():
    problems, warnings = check_tile.static_problems((SKILL / "template.html").read_text())
    assert problems == [] and warnings == []


@pytest.mark.parametrize("page, says", [
    ('<script src="https://cdn.example.com/chart.js"></script>', "loads from the network"),
    ('<link rel="stylesheet" href="//fonts.example.com/a.css">', "loads from the network"),
    ("<style>body{background:url(https://x.example/bg.png)}</style>", "CSS loads from the network"),
    ("<script>fetch('/api/data')</script>", "fetches data at run time"),
    ('<iframe src="other.html"></iframe>', "iframe"),
    ("<style>.card{width: 390px}</style>", "fixed width 390px"),
    ("<style>.card{height: 600px}</style>", "fixed height 600px"),
])
def test_what_breaks_a_tile_is_named(page, says):
    good_head = '<meta name="viewport" content="width=device-width"><style>html,body{height:100%;width:100%;overflow:hidden}</style>'
    problems, _ = check_tile.static_problems(good_head + page)
    assert any(says in p for p in problems), problems


@pytest.mark.parametrize("title, ok", [
    ("Revenue This Week", True),
    ("Calls & Meetings · Today", True),
    ("", False),
    ("Tile title", False),
    ("revenue-week.html", False),
    ("revenue-week", False),
    ("calls_this_week", False),
    ("A name far too long to sit under a small tile on the phone", False),
])
def test_the_tile_has_a_plain_name(title, ok):
    head = (f'<title>{title}</title><meta name="viewport" content="width=device-width">'
            '<style>html,body{height:100%;width:100%;overflow:hidden}</style>')
    problems, _ = check_tile.static_problems(head)
    named = [p for p in problems if "<title>" in p]
    assert (named == []) == ok, named


@pytest.mark.parametrize("stamp, ok", [
    ("2026-11-03T15:30:00Z", True),
    ("2026-11-03T10:30:00-05:00", True),
    ("2026-11-03T15:30:00", False),
    ("2026-11-03 15:30", False),
])
def test_the_updated_time_carries_its_zone(stamp, ok):
    page = (f'<meta name="viewport" content="width=device-width"><style>html,body{{height:100%;width:100%;overflow:hidden}}</style>'
            f'<script type="application/json" id="data">{{"updated": "{stamp}"}}</script>')
    problems, _ = check_tile.static_problems(page)
    zoned = [p for p in problems if "time zone" in p]
    assert (zoned == []) == ok, zoned


def test_an_entrance_animation_is_warned_about():
    page = '<meta name="viewport" content="width=device-width"><style>@keyframes grow{from{height:0}}.bar{animation: grow 1s}</style>'
    _, warnings = check_tile.static_problems(page)
    assert any("animates" in w for w in warnings), warnings


def test_a_link_to_tap_is_fine_and_a_page_with_no_viewport_or_fill_is_not():
    problems, _ = check_tile.static_problems('<a href="https://example.com">Open</a>')
    assert not any("network" in p for p in problems)
    assert any("viewport" in p for p in problems)
    assert any("fills the window" in p for p in problems)


def test_the_command_says_ok_for_the_template_without_drawing(tmp_path):
    tile = tmp_path / "revenue.html"
    tile.write_text((SKILL / "template.html").read_text())
    run = subprocess.run([sys.executable, str(SKILL / "check_tile.py"), str(tile), "--no-render"], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout
    assert f"MEDIA:{tile}" in run.stdout


def test_the_command_fails_a_broken_tile(tmp_path):
    tile = tmp_path / "broken.html"
    tile.write_text('<div style="width:600px">x</div><script>fetch("https://api.example.com")</script>')
    run = subprocess.run([sys.executable, str(SKILL / "check_tile.py"), str(tile), "--no-render"], capture_output=True, text=True)
    assert run.returncode == 1
    assert run.stdout.count("PROBLEM:") >= 3


@pytest.mark.skipif(check_tile.find_chrome() is None, reason="no Chrome or Chromium here")
def test_drawn_at_every_size_the_template_fits_and_a_phone_page_does_not(tmp_path):
    tile = tmp_path / "revenue.html"
    tile.write_text((SKILL / "template.html").read_text())
    problems, note = check_tile.render_problems(str(tile), check_tile.find_chrome(), str(tmp_path / "shots"))
    assert note is None and problems == []
    assert (tmp_path / "shots" / "tiles.png").stat().st_size > 10_000
    phone = tmp_path / "phone.html"
    phone.write_text('<meta name="viewport" content="width=device-width"><body style="margin:0">'
                     '<h1 style="font-size:40px">Revenue</h1><p>' + "Lots of words. " * 80 + "</p></body>")
    problems, note = check_tile.render_problems(str(phone), check_tile.find_chrome(), None)
    assert note is None
    assert any(p.startswith("small (") and "scrolls" in p for p in problems), problems
