"""Hermetic test isolation: redirect HERMES_HOME before importing Hermes or plugin modules."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_HOME = Path(tempfile.mkdtemp(prefix="hermes-proactive-test-"))
os.environ["HERMES_HOME"] = str(_TMP_HOME)
assert Path(os.environ["HERMES_HOME"]).resolve() != (Path.home() / ".hermes").resolve()
