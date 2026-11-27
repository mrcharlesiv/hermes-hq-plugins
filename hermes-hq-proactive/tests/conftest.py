"""Test isolation: a throwaway HERMES_HOME before Hermes or the plugin is imported.

When a Hermes checkout is on this machine it is put on the path, so the tests that turn the
plugin on run against Hermes's real scheduler and settings code, inside the throwaway home.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = Path(tempfile.mkdtemp(prefix="hermes-proactive-test-"))
os.environ["HERMES_HOME"] = str(_TMP_HOME)
assert Path(os.environ["HERMES_HOME"]).resolve() != (Path.home() / ".hermes").resolve()

_HERMES = Path(os.environ.get("HERMES_AGENT") or (Path.home() / ".hermes" / "hermes-agent"))
if _HERMES.is_dir() and str(_HERMES) not in sys.path:
    sys.path.insert(0, str(_HERMES))
