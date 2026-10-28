"""perf_logs.short keeps a log line whole and drops only queries: ``python3 -m pytest -q test_perf_logs.py``."""
import importlib.util
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
