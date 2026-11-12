"""[0.9.52] A plugin update the owner approved in the app, applied on this computer.

Hermes's app-facing update (``plugins.manage`` update) refuses an update that adds Python packages: "run `hermes plugins
update <name>` in a terminal to review them". The app shows the owner those packages and asks; on yes it starts the
update here. This runs that same command, ``hermes [-p <profile>] plugins update <name>``, on a terminal of its own and
answers the one question the owner already answered in the app ("Prepare these with Hermes through PM now?"). A widened
catalog pin ("Apply this update?") is answered yes only when the app says the owner accepted it; any other question is
answered no. Nothing but Hermes's own command changes the plugin.

The update runs on a thread; ``start`` answers a job id at once and ``job`` says how it went, so a slow update never
holds a request open and the app can look again after a trip to the background. Not on Windows (no terminal to give
the command): ``status`` says so and the app falls back to asking the main bot.
"""
import os
import re
import select
import subprocess
import sys
import threading
import time
import uuid

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PACKAGES_QUESTION = "Prepare these with Hermes through PM now?"
WIDER_QUESTION = "Apply this update?"
ANY_QUESTION = re.compile(r"\[y/N\]:?\s*$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TIMEOUT = 300.0
KEEP = 30 * 60.0

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


class UpdateError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def supported() -> bool:
    return os.name == "posix"


def status() -> dict:
    """`pluginUpdates` (0.9.52): POST /plugins/update applies an update whose Python packages the owner approved."""
    return {"pluginUpdates": supported()}


def command(profile: str, name: str, force: bool) -> list[str]:
    """Hermes's own CLI in this server's Python (the same code the server runs), never through a shell."""
    args = ["plugins", "update", name] + (["--force"] if force else [])
    if profile and profile != "default":
        args = ["-p", profile] + args
    return [sys.executable, "-c", "import sys; from hermes_cli.main import main; sys.exit(main())"] + args


def answer_for(text: str, accept_capabilities: bool) -> str | None:
    """What to type at the question the output ends on, or None while it isn't asking."""
    flat = " ".join(ANSI.sub("", text).split())
    if not ANY_QUESTION.search(flat):
        return None
    tail = flat[-240:]
    if PACKAGES_QUESTION in tail:
        return "y"
    if WIDER_QUESTION in tail and accept_capabilities:
        return "y"
    return "n"


def run(cmd: list[str], accept_capabilities: bool = False, timeout: float = TIMEOUT) -> tuple[int, str, list[str]]:
    """Runs `cmd` on a pseudo-terminal -> (exit code, its output, the answers typed). -1: it ran out of time."""
    import pty
    master, slave = pty.openpty()
    env = {**os.environ, "TERM": "dumb", "NO_COLOR": "1", "COLUMNS": "200", "LINES": "50"}
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True, env=env)
    os.close(slave)
    out, asked_from, answers = b"", 0, []
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                return -1, _text(out), answers
            ready, _, _ = select.select([master], [], [], 0.25)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            out += chunk
            answer = answer_for(out[asked_from:].decode(errors="replace"), accept_capabilities)
            if answer:
                os.write(master, (answer + "\n").encode())
                answers.append(answer)
                asked_from = len(out)
        return proc.wait(), _text(out), answers
    finally:
        os.close(master)


def _text(out: bytes) -> str:
    return ANSI.sub("", out.decode(errors="replace")).replace("\r", "")


def last_lines(text: str, count: int = 6) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:])[-1200:]


def start(profile: str, name: str, accept_capabilities: bool = False, force: bool = False, runner=run) -> dict:
    """Starts the update on a thread -> {"job": id}. One update of a plugin for a bot at a time."""
    if not supported():
        raise UpdateError(501, "updating from here needs a Mac or Linux computer")
    if not NAME.match(name or "") or not NAME.match(profile or "default"):
        raise UpdateError(400, "bad plugin or profile name")
    profile = profile or "default"
    now = time.time()
    with _lock:
        for key in [k for k, j in _jobs.items() if j["state"] != "running" and now - j["at"] > KEEP]:
            del _jobs[key]
        for key, job in _jobs.items():
            if job["state"] == "running" and (job["profile"], job["name"]) == (profile, name):
                return {"job": key}
        key = uuid.uuid4().hex[:16]
        _jobs[key] = {"state": "running", "profile": profile, "name": name, "at": now}

    def work():
        try:
            code, output, answers = runner(command(profile, name, force), accept_capabilities)
        except Exception as error:  # the command couldn't be started
            code, output, answers = -2, str(error), []
        done = {"at": time.time(), "state": "done" if code == 0 else "failed", "code": code,
                "unchanged": "Already up to date" in output, "output": last_lines(output),
                "packages": "y" in answers and PACKAGES_QUESTION in output}
        if code == -1:
            done["output"] = "The update took too long and was stopped."
        with _lock:
            _jobs[key].update(done)

    threading.Thread(target=work, name="hq-plugin-update", daemon=True).start()
    return {"job": key}


def job(key: str) -> dict:
    with _lock:
        found = _jobs.get(key)
        if found is None:
            raise UpdateError(404, "no such update")
        return {k: v for k, v in found.items() if k != "at"}
