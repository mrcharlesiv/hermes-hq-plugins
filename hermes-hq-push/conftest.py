"""Every test here imports the real Hermes, so pin HERMES_HOME to a throwaway folder before any test module does:
nothing a test reads or writes can reach the computer's own ~/.hermes (its config.yaml, state.db or plugins)."""
import os
import tempfile
from pathlib import Path

os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes-hq-push-tests-")
# Some Hermes calls import hermes_bootstrap, which (for a home that only borrows the checkout, as this one does)
# builds a whole Python environment in that home and re-execs the test runner into it, without pytest. Inside Hermes
# that boot has already happened; here it is turned off with Hermes's own switch (hermes_cli/venv_sync.prepare_launch).
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")
assert Path(os.environ["HERMES_HOME"]).resolve() != (Path.home() / ".hermes").resolve()
