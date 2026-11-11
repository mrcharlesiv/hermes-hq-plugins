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


def test_only_the_update_time_needs_a_zone_and_it_is_said_once():
    """A forecast's hours ("time": "…T14:00") are the page's own business: twelve of them failed a weather tile."""
    head = '<meta name="viewport" content="width=device-width"><style>html,body{height:100%;width:100%;overflow:hidden}</style>'
    hours = "".join(f'{{"time": "2026-11-11T{h:02d}:00"}},' for h in range(12))
    problems, _ = check_tile.static_problems(head + f'<script type="application/json">[{hours}{{"updated": "2026-11-11T14:00"}}]</script>')
    assert len([p for p in problems if "time zone" in p]) == 1, problems
    problems, _ = check_tile.static_problems(head + f'<script type="application/json">[{hours}{{"updated": "2026-11-11T14:00Z"}}]</script>')
    assert not [p for p in problems if "time zone" in p], problems


def test_an_update_time_that_has_not_happened_yet_is_a_problem():
    """A bot typed the time by hand: "Updated 3:08 PM" at 3:05."""
    head = '<meta name="viewport" content="width=device-width"><style>html,body{height:100%;width:100%;overflow:hidden}</style>'
    problems, _ = check_tile.static_problems(head + '<script type="application/json">{"updated": "2999-01-01T00:00:00Z"}</script>')
    assert any("hasn't happened yet" in p for p in problems), problems
    problems, _ = check_tile.static_problems(head + '<script type="application/json">{"updated": "2026-11-03T15:30:00-05:00"}</script>')
    assert not any("hasn't happened yet" in p for p in problems), problems


def _tile(tmp_path, name, data=None, edit=None):
    """The template with this data block (and an edit to the page), saved as a tile."""
    page = (SKILL / "template.html").read_text()
    if data is not None:
        start = page.index('<script type="application/json" id="data">') + len('<script type="application/json" id="data">')
        page = page[:start] + "\n" + json.dumps(data) + "\n" + page[page.index("</script>", start):]
    if edit:
        page = edit(page)
    tile = tmp_path / name
    tile.write_text(page)
    return tile


BOARD = {"title": "HomeLab", "badge": "● LIVE", "value": "ONLINE", "state": "good", "note": "Core path and compute verified",
         "items_as": "cards", "updated": "2026-11-11T20:26:00Z", "error": None,
         "items": [{"name": f"host-{n:02d}", "value": "20 cores", "state": "good"} for n in range(14)]}

needs_chrome = pytest.mark.skipif(check_tile.find_chrome() is None, reason="no Chrome or Chromium here")


def test_a_browser_named_in_the_environment_comes_first(tmp_path, monkeypatch):
    mine = tmp_path / "my-chrome"
    mine.write_text("#!/bin/sh\n")
    mine.chmod(0o755)
    monkeypatch.setenv("HQ_TILE_CHROME", str(mine))
    assert check_tile.find_chromes()[0] == str(mine.resolve())
    assert check_tile.find_chrome() == str(mine.resolve())


def test_with_no_browser_the_template_is_ok_by_the_file_rules_and_says_it_was_not_drawn(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check_tile, "find_chromes", lambda: [])
    monkeypatch.setattr(sys, "argv", ["check_tile.py", str(_tile(tmp_path, "board.html", BOARD))])
    assert check_tile.main() == 0
    out = capsys.readouterr().out
    assert "NOT DRAWN" in out and "OK by the file rules, but not drawn" in out and "PICTURES" not in out


def test_with_no_browser_a_page_without_the_fit_script_fails(tmp_path, monkeypatch, capsys):
    """Nothing drew it and nothing in it keeps it from being cut off: that is how a crowded board reached a phone."""
    monkeypatch.setattr(check_tile, "find_chromes", lambda: [])
    bare = _tile(tmp_path, "bare.html", BOARD, edit=lambda page: page[:page.index('<script id="hq-tile-fit">')] + "</body></html>")
    monkeypatch.setattr(sys, "argv", ["check_tile.py", str(bare)])
    assert check_tile.main() == 1
    assert "fit script" in capsys.readouterr().out


def test_a_browser_that_never_finishes_gives_way_to_the_next(tmp_path, monkeypatch):
    tried = []

    def one(path, chrome, shots):
        tried.append(chrome)
        return ([], [], "NOT DRAWN: hung") if chrome == "hung" else (["p"], ["w"], None)

    monkeypatch.setattr(check_tile, "render_with", one)
    assert check_tile.render_problems("tile.html", ["hung", "good", "spare"], None) == (["p"], ["w"], None)
    assert tried == ["hung", "good"]
    assert check_tile.render_problems("tile.html", ["hung"], None) == ([], [], "NOT DRAWN: hung")


def test_findings_are_said_once_per_kind_with_their_sizes():
    shapes = [("small", 175, 175, "light"), ("small", 175, 175, "dark"), ("medium", 362, 175, "light")]
    found = {"0": [{"k": "past", "n": 10, "first": "div.nodes"}, {"k": "small", "n": 3, "first": "div.badge 9.1 px"}],
             "1": [{"k": "past", "n": 10, "first": "div.nodes"}], "2": [{"k": "past", "n": 1, "first": "div.foot"}]}
    problems, warnings = check_tile.worded(found, shapes)
    assert len(problems) == 1 and len(warnings) == 1
    assert "small 175×175 (10 parts, first: div.nodes); medium 362×175 (1 part)" in problems[0]
    assert "show less" in problems[0] and "under 11 pt" in warnings[0]


@needs_chrome
def test_drawn_at_every_size_the_template_fits_and_a_phone_page_does_not(tmp_path):
    tile = tmp_path / "revenue.html"
    tile.write_text((SKILL / "template.html").read_text())
    problems, warnings, note = check_tile.render_problems(str(tile), check_tile.find_chromes(), str(tmp_path / "shots"))
    assert note is None and problems == [] and warnings == []
    assert (tmp_path / "shots" / "tiles.png").stat().st_size > 10_000
    phone = tmp_path / "phone.html"
    phone.write_text('<meta name="viewport" content="width=device-width"><body style="margin:0">'
                     '<h1 style="font-size:40px">Revenue</h1><p>' + "Lots of words. " * 80 + "</p></body>")
    problems, _, note = check_tile.render_problems(str(phone), check_tile.find_chromes(), None)
    assert note is None
    assert any("scrolls" in p and "small 175×175" in p for p in problems), problems


@needs_chrome
@pytest.mark.parametrize("data", [
    BOARD,
    dict(BOARD, value="NO-GO", state="bad", items=[{"name": "Servers", "value": "4/4 nodes", "state": "bad"}] + BOARD["items"][:5]),
    {"title": "Open Deals", "value": "12", "note": "Worth $84k", "updated": "2026-11-11T20:26:00Z", "error": None,
     "items": [{"name": f"Deal number {n}", "value": f"${n * 3}k"} for n in range(1, 30)]},
    {"title": "Calls Today", "value": "48", "updated": "2026-11-11T20:26:00Z", "error": None},
    {"title": "Revenue", "value": None, "updated": None, "error": None},
    {"title": "Tokens", "value": "64k", "note": "3090 tokens, 24 h", "series": [3, 1, 4, 1, 5, 9, 2, 6], "items_as": "cards",
     "updated": "2026-11-11T20:26:00Z", "error": None,
     "items": [{"name": "RTX 3090", "value": "Up 39°C", "state": "good"}, {"name": "RTX 2070", "value": "Up 28°C", "state": "good"}]},
], ids=["status board", "no-go board", "long list", "one number", "no data yet", "number, chart and cards"])
def test_the_template_fits_itself_whatever_it_is_given(tmp_path, data):
    """Owner 2026-11-11, a tester's Tiles: status boards with their last row and their "Updated" line cut off, words
    under the boxes, a third of the tile blank. Fourteen items in a Small tile must come out whole, as "+N more"."""
    problems, warnings, note = check_tile.render_problems(str(_tile(tmp_path, "tile.html", data)), check_tile.find_chromes(), None)
    assert note is None and problems == [], problems
    assert not [w for w in warnings if "blank block" in w or "under 11 pt" in w or "too many words" in w], warnings


@needs_chrome
def test_a_board_squeezed_into_a_fixed_grid_is_caught(tmp_path):
    """What the old template was bent into: nine boxes of small text in the chart's place, cut off at the tile's edge."""
    def bend(page):
        boxes = "".join(f'<div style="font-size:9.5px;border:1px solid gray;padding:4px"><b>node-{n}</b><br>10.10.10.{n} up</div>' for n in range(9))
        grid = f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px">{boxes}</div>'
        page = page.replace('<div class="chart" id="chart" data-drop="4" data-least="48"></div>', f'<div class="chart">{grid}</div>')
        return page.replace('el("chart").innerHTML =', 'el("title").title =').replace('} else { el("chart").remove(); }', '}')
    problems, warnings, note = check_tile.render_problems(str(_tile(tmp_path, "bent.html", edit=bend)), check_tile.find_chromes(), None)
    assert note is None
    assert any("cut off" in p and "small 175×175" in p for p in problems), problems
    assert any("under 11 pt" in w for w in warnings), warnings


@needs_chrome
def test_a_script_that_fails_is_a_problem_not_a_blank_tile_that_passes(tmp_path):
    broken = _tile(tmp_path, "broken.html", edit=lambda page: page.replace('text("title", d.title);', 'text("title", d.title); el("nope").textContent = 1;'))
    problems, _, note = check_tile.render_problems(str(broken), check_tile.find_chromes(), None)
    assert note is None and any("a script failed" in p for p in problems), problems


@needs_chrome
def test_words_under_a_box_are_caught(tmp_path):
    def cover(page):
        return page.replace('<div class="foot"', '<div style="position:absolute;left:0;top:0;width:100%;height:60%;background:#345"></div><div class="foot"')
    problems, _, note = check_tile.render_problems(str(_tile(tmp_path, "covered.html", edit=cover)), check_tile.find_chromes(), None)
    assert note is None and any("drawn under something else" in p for p in problems), problems
