"""perf_logs.short keeps a log line whole and drops only queries: ``python3 -m pytest -q test_perf_logs.py``."""
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location("perf_logs_under_test", Path(__file__).with_name("perf_logs.py"))
perf_logs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(perf_logs)
short = perf_logs.short


def test_lines_with_tags_and_slashes_stay_whole():
    # Real app lines that 0.9.6 cut to their first few words (log study 2026-10-28).
    line = "complete on #d484ac rt #bb3875: status complete, final row 5339, user row 5335, stream #f7aeec, scope end 62/63"
    assert short(line) == line[:perf_logs.MAX_STRING]
    assert short("GET /api/sessions/sidebar → 200, 412 ms #c1bfee") == "GET /api/sessions/sidebar → 200, 412 ms #c1bfee"
    assert short("rename 4064 \"Profile 'dance-mania' does not exist\" on #ab12cd/#ef34ab") \
        == "rename 4064 \"Profile 'dance-mania' does not exist\" on #ab12cd/#ef34ab"


def test_queries_and_value_fragments_go():
    assert short("GET /api/sessions?limit=24&token=abc → 200, 31 ms") == "GET /api/sessions → 200, 31 ms"
    assert short("open https://x.test/cb#access_token=abc&state=1 now") == "open https://x.test/cb now"
    assert short("see https://x.test/docs#install and more") == "see https://x.test/docs#install and more"
    assert short("is it done? yes #abc123") == "is it done? yes #abc123"


def test_control_characters_and_length():
    assert short("a\nb\tc\x7f") == "a b c "
    assert len(short("x" * 500)) == perf_logs.MAX_STRING


def test_clean_writes_the_whole_field():
    out = perf_logs.clean({"t": 1, "k": "trace", "m": "page #5200a2 (answer #5200a2): asked limit 24 offset 0; 24 rows 1/2"})
    assert out["m"] == "page #5200a2 (answer #5200a2): asked limit 24 offset 0; 24 rows 1/2"


def _lines(folder):
    return [json.loads(line) for path in sorted(folder.glob("*.jsonl")) for line in path.read_text().splitlines()]


def test_a_looping_line_stops_after_the_repeat_limit(tmp_path, monkeypatch):
    # 2026-10-30: a Sessions re-read loop wrote 14 MB of three lines by 10 AM; the rest of the day was lost.
    monkeypatch.setattr(perf_logs, "REPEAT_LIMIT", 5)
    now = 1793372000.0
    loop = [json.dumps({"t": int(now * 1000) + i, "k": "note.net", "s": "n", "m": "read held: gateway known down"}) for i in range(8)]
    other = json.dumps({"t": int(now * 1000), "k": "note.app", "s": "n", "m": "active"})
    perf_logs.write(tmp_path, "iPhone19_2-5c530371", loop + [other], now=now)
    perf_logs.write(tmp_path, "iPhone19_2-5c530371", loop[:3], now=now)
    lines = _lines(tmp_path / "iPhone19_2-5c530371")
    assert sum(1 for line in lines if line["k"] == "note.net") == 5
    assert [line["left_out"] for line in lines if line["k"] == "log.repeat"] == [3, 3]
    assert any(line["k"] == "note.app" for line in lines)


def test_lines_that_differ_only_in_numbers_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(perf_logs, "REPEAT_LIMIT", 2)
    now = 1793372000.0
    timings = [json.dumps({"t": int(now * 1000), "k": "note.http", "s": "n", "m": f"POST /api/auth/ws-ticket → 200, {i} ms"}) for i in range(5)]
    perf_logs.write(tmp_path, "iPhone19_2-5c530371", timings, now=now)
    assert sum(1 for line in _lines(tmp_path / "iPhone19_2-5c530371") if line["k"] == "note.http") == 5


def test_repeat_counts_keep_only_recent_days(tmp_path):
    now = 1793372000.0
    folder = tmp_path / "iPhone19_2-5c530371"
    folder.mkdir()
    (folder / perf_logs.REPEATS_FILE).write_text(json.dumps({"2026-01-01": {"note.net x": 9}}))
    perf_logs.write(tmp_path, "iPhone19_2-5c530371", [json.dumps({"t": int(now * 1000), "k": "note.app", "s": "n", "m": "active"})], now=now)
    assert list(json.loads((folder / perf_logs.REPEATS_FILE).read_text())) == ["2026-10-30"]
