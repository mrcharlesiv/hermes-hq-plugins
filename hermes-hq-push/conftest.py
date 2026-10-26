"""Every test here imports the real Hermes, so pin HERMES_HOME to a throwaway folder before any test module does:
nothing a test reads or writes can reach the computer's own ~/.hermes (its config.yaml, state.db or plugins)."""
import os
import tempfile
from pathlib import Path

os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes-hq-push-tests-")
assert Path(os.environ["HERMES_HOME"]).resolve() != (Path.home() / ".hermes").resolve()
