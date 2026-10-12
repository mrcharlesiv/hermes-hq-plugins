"""Each bot's browser, as the person chose it on the phone (Bot Info › Browser).

Every bot keeps a Chrome of its own (fleet.py), on its own port, in ``<bot home>/chrome-debug``. Two choices change it:

- **Profile** — ``own`` (the default: the bot's own profile, signed in to whatever the bot signed in to) or ``copy``:
  a copy of one of the person's Chrome profiles (cookies, saved sign-ins, history, settings) put in the bot's folder,
  so sites open already signed in and the browser looks lived-in. Saved passwords, cards, addresses and extensions
  are not copied. The person's own Chrome is never opened or changed: the copy is taken while it runs (databases
  through SQLite's online backup, or a raw copy SQLite recovers when Chrome holds them locked). The bot's own
  profile is set aside (``chrome-debug-own``) and comes back when the person switches back; the copy is then deleted.
  "Refresh sign-ins" copies the person's profile again. On macOS the copy stays readable because the bot's Chrome is
  the same Chrome app, run by the same macOS user, so it opens the same "Chrome Safe Storage" Keychain item.
  Hermes' own ``browser.use_real_profile`` does the same kind of copy, but only of the default browser, into one folder
  per Hermes home, headless, for one session at a time (the browser closes at the end of each turn, so there is
  nothing to watch between turns), and a ``browser.cdp_url`` (which every bot has with this plugin) wins over it.
- **Window** — ``hidden`` (the default: headless, as before) or ``normal``: an ordinary Chrome window (the Chrome
  binary started directly with the bot's own user-data-dir; macOS may bring it forward once as it starts). Headless Chrome announces itself (a ``HeadlessChrome`` brand in its client hints,
  missing window and GPU traits), which is what most "are you human?" checks look for; a normal window doesn't. It is
  still the same Chrome; nothing is faked, and a check that still appears is the person's to solve (the bot asks
  through ``browser_ask_user``, the phone takes over).

State lives in the bot's fleet state file (``source``, ``copied_at``, ``window``); fleet.start passes it to the launch.

A bot whose browser this plugin doesn't run reads ``blocked`` (why) and ``can_take_over``; ``take_over`` hands it to
the plugin when the person asks.

Routes (dashboard/plugin_api.py): ``GET /chrome-profiles``, ``GET /choice?profile=<bot>``, ``POST /choice``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import sqlite3
import time
from pathlib import Path

PROFILES_SCHEMA = "hermes-hq-browser.chrome-profiles.v1"
CHOICE_SCHEMA = "hermes-hq-browser.choice.v1"
NO_ACCESS = ("Your Mac is blocking Hermes from reading Chrome's profiles. On the Mac, open System Settings › Privacy & "
             "Security › Full Disk Access, add Hermes's Python, then quit and reopen Hermes.")
PROFILE_MODES = ("own", "copy")
WINDOWS = ("hidden", "normal")
# A Chrome profile folder name: "Default", "Profile 2"… never a path.
PIN = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
# Where each Chromium browser keeps its profiles (macOS, Linux), by the binary's app name. The copy must be opened by
# the same browser that wrote it (each has its own Keychain item), so only the bots' own browser's profiles are listed.
SUPPORT = {
    "Google Chrome": ("Library/Application Support/Google/Chrome", ".config/google-chrome"),
    "Chromium": ("Library/Application Support/Chromium", ".config/chromium"),
    "Brave Browser": ("Library/Application Support/BraveSoftware/Brave-Browser", ".config/BraveSoftware/Brave-Browser"),
    "Microsoft Edge": ("Library/Application Support/Microsoft Edge", ".config/microsoft-edge"),
}
# Never copied: caches (rebuilt), live-instance locks, crash dumps, extensions and their data (the bot's Chrome runs
# with --disable-extensions; some would act on the person's accounts), and SQLite sidecars (the databases come through
# _backup_database instead).
SKIP_DIRS = {"Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache",
             "ShaderCache", "GraphiteDawnCache", "CacheStorage", "ScriptCache", "Crashpad", "blob_storage",
             "Download Service", "optimization_guide_model_store", "component_crx_cache",
             "Extensions", "Local Extension Settings", "Sync Extension Settings", "Managed Extension Settings",
             "Extension State", "Extension Rules", "Extension Scripts"}
SKIP_FILES = re.compile(r"^(Singleton.*|.*-journal|.*-wal|.*-shm|LOCK|lockfile)$")
# Never copied at all: saved passwords, payment cards and addresses (the bot's password manager is off; bots sign in
# through Hermes' vault), and extension cookies.
PRIVATE = re.compile(r"^(Login Data|Login Data For Account|Web Data|Extension Cookies|Affiliation Database)(-journal|-wal|-shm)?$")
DATABASES = ("Cookies", "Network/Cookies", "History", "Favicons", "Top Sites", "Shortcuts")


def support_dir(binary: str | None, home: Path | None = None) -> Path | None:
    """The person's profiles folder for the browser the bots run (``fleet.chrome_binary()``), or None."""
    if not binary:
        return None
    home = home or Path.home()
    name = next((key for key in SUPPORT if f"/{key}.app/" in binary or binary.endswith(f"/{key}")), None)
    if name is None:
        lowered = binary.lower()
        name = ("Google Chrome" if "google-chrome" in lowered or lowered.endswith("/chrome") else
                "Chromium" if "chromium" in lowered else None)
    if name is None:
        return None
    mac, linux = SUPPORT[name]
    for candidate in (home / mac, home / linux):
        if candidate.is_dir():
            return candidate
    return None


def read_profiles(source: Path) -> dict | None:
    """``{profiles: [{dir, name, account}], last_used}`` from a profiles folder's ``Local State`` (Chrome's own list,
    in its own order); only folders that exist. None when there is no readable ``Local State``."""
    try:
        state = json.loads((source / "Local State").read_text(encoding="utf-8-sig", errors="replace"))
    except PermissionError:
        raise                   # macOS privacy protection: chrome_profiles says so (``no_access``)
    except (OSError, ValueError):
        return None
    section = state.get("profile") if isinstance(state, dict) else None
    section = section if isinstance(section, dict) else {}
    cache = section.get("info_cache") if isinstance(section.get("info_cache"), dict) else {}
    order = [name for name in section.get("profiles_order") or [] if isinstance(name, str)]
    names = order + sorted(name for name in cache if name not in order)
    profiles = []
    for name in names:
        info = cache.get(name)
        if not isinstance(info, dict) or not PIN.match(name) or not (source / name).is_dir():
            continue
        profiles.append({"dir": name, "name": str(info.get("name") or name)[:80],
                         "account": str(info.get("user_name") or "")[:120] or None})
    last = section.get("last_used")
    return {"profiles": profiles[:32],
            "last_used": last if isinstance(last, str) and any(p["dir"] == last for p in profiles) else None}


def chrome_profiles(binary: str | None, home: Path | None = None) -> dict:
    """The person's profiles in the browser the bots run. ``supported`` false (with a ``reason``): no Chrome
    (``no_chrome``), the computer won't let Hermes read them (``no_access``: on a Mac, Hermes's Python needs Full Disk
    Access; ``python`` names it), or none of its profiles found (``no_profiles``)."""
    base = {"schema": PROFILES_SCHEMA, "supported": False, "browser": None, "profiles": [], "last_used": None}
    if not binary:
        return {**base, "reason": "no_chrome"}
    browser = next((key for key in SUPPORT if f"/{key}.app/" in binary), Path(binary).name)
    folder = support_dir(binary, home)
    try:
        found = read_profiles(folder) if folder else None
    except PermissionError:
        return {**base, "browser": browser, "reason": "no_access", "python": sys.executable}
    if not found or not found["profiles"]:
        return {**base, "browser": browser, "reason": "no_profiles"}
    return {**base, "supported": True, "browser": browser, **found}


# ---------------------------------------------------------------------------------------------- copying a profile

def _backup_database(source: Path, target: Path) -> str | None:
    """A consistent copy of a SQLite database the person's Chrome may hold: SQLite's online backup through a read-only
    open first; Chrome keeps some databases locked while it runs (the backup then times out), so next a raw copy of
    the file and its journal / WAL, which SQLite rolls forward or back when the copy is first opened, checked with
    ``quick_check``. Returns ``"backup"``, ``"raw"`` or None (not copied)."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        # VACUUM INTO, not Connection.backup: backup retries a locked database forever; this gives up after the
        # busy timeout with "database is locked".
        reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=2)
        try:
            reader.execute("VACUUM INTO ?", (str(target),))
        finally:
            reader.close()
        os.chmod(target, 0o600)
        return "backup"
    except sqlite3.Error:
        _unlink_database(target)
    try:
        for suffix in ("", "-journal", "-wal"):
            if Path(f"{source}{suffix}").is_file():
                shutil.copyfile(f"{source}{suffix}", f"{target}{suffix}")
                os.chmod(f"{target}{suffix}", 0o600)
        checker = sqlite3.connect(str(target), timeout=2)
        try:
            ok = checker.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            checker.execute("PRAGMA wal_checkpoint(TRUNCATE)") if ok else None
        finally:
            checker.close()
        for suffix in ("-journal", "-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
        if ok:
            return "raw"
    except (OSError, sqlite3.Error):
        pass
    _unlink_database(target)
    return None


def _unlink_database(target: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)


def copy_profile(source_root: Path, name: str, target: Path, seeded: dict) -> dict:
    """Copies the person's profile ``name`` into ``target`` (a staging folder, not a running Chrome's) as its single
    ``Default`` profile: no passwords, card or address data, extensions, caches or symlinks. Returns
    ``{databases, skipped}``. Raises ``ValueError`` (plain words) when the profile is missing or its sign-ins (Cookies)
    couldn't be copied; ``target`` is then removed."""
    if not PIN.match(name) or not (source_root / name).is_dir() or (source_root / name).is_symlink():
        raise ValueError("That Chrome profile isn't on this computer.")
    source = source_root / name
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(mode=0o700, parents=True)
    try:
        report = _copy_into(source_root, name, source, target, seeded)
    except ValueError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    except (OSError, shutil.Error, sqlite3.Error):
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError("Couldn't copy your Chrome profile. Try again.") from None
    return report


def _copy_into(source_root: Path, name: str, source: Path, target: Path, seeded: dict) -> dict:
    databases = {Path(rel) for rel in DATABASES}
    skipped = 0

    def ignore(folder, entries):
        relative = Path(folder).relative_to(source)
        return [e for e in entries
                if e in SKIP_DIRS or SKIP_FILES.match(e) or PRIVATE.match(e) or e.startswith("chrome-extension_")
                or (relative / e) in databases or os.path.islink(os.path.join(folder, e))]
    try:
        shutil.copytree(source, target / "Default", ignore=ignore, symlinks=True)
    except shutil.Error as error:  # files that vanished mid-copy (Chrome rotates some): the rest is fine
        skipped += len(error.args[0]) if error.args and isinstance(error.args[0], list) else 1
    copied = 0
    cookies_wanted = cookies_copied = False
    for rel in DATABASES:
        path = source / rel
        if path.is_file() and not path.is_symlink():
            how = _backup_database(path, target / "Default" / rel)
            if rel.endswith("Cookies"):
                cookies_wanted = True
                cookies_copied = cookies_copied or how is not None
            if how:
                copied += 1
            else:
                skipped += 1
    if cookies_wanted and not cookies_copied:
        raise ValueError("Couldn’t copy your sign-ins from Chrome. Quit Chrome on your computer and try again.")
    # Local State names one profile, Default, with the chosen profile's name and picture.
    try:
        state = json.loads((source_root / "Local State").read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        state = {}
    state = state if isinstance(state, dict) else {}
    section = state.get("profile") if isinstance(state.get("profile"), dict) else {}
    info = (section.get("info_cache") or {}).get(name) if isinstance(section.get("info_cache"), dict) else None
    state["profile"] = {**section, "last_used": "Default", "profiles_order": ["Default"],
                        "info_cache": {"Default": info} if isinstance(info, dict) else {}}
    (target / "Local State").write_text(json.dumps(state))
    # The bot's Chrome keeps its own settings: no password manager (its "Save password?" bubble has stalled bot
    # Chromes), and no "Restore pages?" after the copy.
    preferences_path = target / "Default" / "Preferences"
    try:
        preferences = json.loads(preferences_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        preferences = {}
    merge(preferences, seeded)
    merge(preferences, {"profile": {"exit_type": "Normal", "exited_cleanly": True}})
    preferences_path.write_text(json.dumps(preferences))
    for path in [target, *target.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
    return {"databases": copied, "skipped": skipped}


def merge(into: dict, values: dict) -> None:
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            merge(into[key], value)
        else:
            into[key] = value


# ---------------------------------------------------------------------------------------------- the choice

# Why a bot's browser isn't one this plugin runs (``blocked``), and whether the person can hand it to the plugin
# (``can_take_over``). fleet.skip_reason's codes (the supervisor's own reasons, so the two stay in step), plus:
# ``external`` (its cdp_url's port is answered by a Chrome the person runs), ``no_browser`` (its browser tools are
# switched off in Hermes: never offered, whatever else is set), ``starting`` (no Chrome of its own yet: the supervisor
# hasn't come round to it).
TAKE_OVER = ("real_profile", "released", "external", "fixed")
REFUSED = {
    "off": "Hermes HQ’s browser add-on is switched off for this bot, so it can’t run its browser.",
    "no_browser": "This bot’s browser tools are turned off in Hermes, so there’s no browser to run.",
    "remote": "This bot uses a browser on another computer, so Hermes HQ leaves it alone.",
    "camofox": "This bot browses through Camofox, so Hermes HQ leaves it alone.",
    "starting": "Hermes HQ is still setting up this bot’s browser. Try again in a minute.",
}
BLOCKED = {
    "real_profile": "This bot uses your real Chrome profile through Hermes’s own setting, so Hermes HQ doesn’t run its "
                    "browser.",
    "released": "This bot’s browser was turned off in its Hermes settings.",
    **REFUSED,
}
TAKE_OVER_WAIT = 25.0  # seconds take_over waits for the supervisor to bring the bot's Chrome up


def blocked_reason(fleet, config: dict, state: dict) -> str | None:
    """None when this plugin runs the bot's Chrome (its choice can be changed), else a short reason code."""
    reason = fleet.skip_reason(config, state)
    if reason is None:
        if state.get("status") in ("external", "released"):
            reason = state["status"]
        elif not state.get("folder"):
            reason = "starting"
    if reason is not None and reason != "off" and fleet.browser_turned_off(config):
        reason = "no_browser"
    return reason


def _home_of(fleet, root: Path, profile: str) -> Path | None:
    try:
        return next((home for name, home in fleet.profiles(root) if name == profile), None)
    except Exception:  # noqa: BLE001
        return None


def read_choice(fleet, root: Path, profile: str, home: Path | None = None) -> dict:
    state = fleet.read_state(root, profile)
    home = home or _home_of(fleet, root, profile)
    config = fleet.read_config(home) if home is not None else {}
    source = state.get("source") if isinstance(state.get("source"), dict) else None
    blocked = blocked_reason(fleet, config, state)
    return {"schema": CHOICE_SCHEMA, "profile": profile, "mode": "copy" if source else "own",
            "source": source, "copied_at": state.get("copied_at") if source else None,
            "window": "normal" if state.get("window") == "normal" else "hidden",
            # Only a Chrome this plugin runs for the bot can be changed (not a remote, real-profile or hand-made one).
            "changeable": blocked is None, "blocked": blocked, "can_take_over": blocked in TAKE_OVER,
            "status": state.get("status")}


STOP_WAIT = 10.0  # seconds a bot's Chrome gets to stop before a change is refused


def stop_chrome(fleet, folder: Path, port, name: str, wait: float | None = None) -> None:
    """Ends the bot's Chrome and waits until nothing runs on its folder and its port is quiet. Without a way to see
    processes (no psutil) a Chrome still answering can't be stopped: raises rather than swap folders under it."""
    for pid, data, _ in fleet.chrome_processes():
        if data == str(folder):
            fleet.end(pid, str(folder))
    deadline = time.time() + (STOP_WAIT if wait is None else wait)
    while True:
        running = [pid for pid, data, _ in fleet.chrome_processes() if data == str(folder)]
        answering = isinstance(port, int) and fleet.answers(port, 1.0)
        if not running and not answering:
            return
        if time.time() >= deadline:
            raise ValueError(f"Couldn’t stop {name}’s browser to change it. Try again in a minute.")
        time.sleep(0.25)


def set_choice(fleet, root: Path, profile: str, home: Path, *, mode: str | None = None, source: str | None = None,
               window: str | None = None, refresh: bool = False, binary: str | None = None,
               user_home: Path | None = None, now: float | None = None) -> dict:
    """Changes a bot's browser and restarts its Chrome. ``mode`` own/copy (copy needs ``source``, a profile folder
    from ``chrome_profiles``), ``window`` hidden/normal, ``refresh`` copies the chosen profile again. Raises
    ``ValueError`` (plain words) for a request that can't be done; nothing is changed then.

    A copy is made into a staging folder first, outside the bot's lock (the bot's own browser calls go on meanwhile);
    only stopping its Chrome and swapping folders happen under the lock. The bot's own profile is moved aside to
    ``chrome-debug-own`` (recorded in the state file before the move) and only ever moved back, never deleted; any
    failure during the swap puts the folders back as they were."""
    now = time.time() if now is None else now
    if mode is not None and mode not in PROFILE_MODES:
        raise ValueError("Unknown browser choice.")
    if window is not None and window not in WINDOWS:
        raise ValueError("Unknown window choice.")
    if mode == "copy" and not source:
        raise ValueError("Choose one of your Chrome profiles.")
    folder, kept = home / "chrome-debug", home / "chrome-debug-own"
    lock = fleet.state_folder(root) / f"{profile}.lock"
    with fleet.Locked(lock):
        state = fleet.read_state(root, profile)
    blocked = blocked_reason(fleet, fleet.read_config(home), state)
    if blocked is not None:
        raise ValueError(BLOCKED.get(blocked) or "This bot's browser was set up by hand, so Hermes HQ leaves it alone.")
    current = state.get("source") if isinstance(state.get("source"), dict) else None
    wanted = current if mode is None else (None if mode == "own" else {"dir": source})
    copy_now = wanted is not None and (refresh or not current or current.get("dir") != wanted["dir"])
    window_now = window is not None and window != ("normal" if state.get("window") == "normal" else "hidden")
    if not (copy_now or (current is not None) != (wanted is not None) or window_now):
        return read_choice(fleet, root, profile, home)
    staging, report = None, None
    if copy_now:
        binary = binary or fleet.chrome_binary()
        listing = chrome_profiles(binary, user_home)
        match = next((p for p in listing.get("profiles") or [] if p["dir"] == wanted["dir"]), None)
        if listing.get("reason") == "no_access":
            raise ValueError(NO_ACCESS)
        if not listing.get("supported") or match is None:
            raise ValueError("That Chrome profile isn't on this computer.")
        wanted = {"dir": match["dir"], "name": match["name"], "account": match["account"]}
        staging = home / f"chrome-debug.copying-{os.getpid()}"
        report = copy_profile(support_dir(binary, user_home), wanted["dir"], staging, fleet.SEEDED_PREFERENCES)
    name = profile
    try:
        with fleet.Locked(lock):
            state = fleet.read_state(root, profile)       # fresh: the supervisor may have written meanwhile
            active = state.get("source") if isinstance(state.get("source"), dict) else None
            stop_chrome(fleet, folder, state.get("port"), name)
            if copy_now:
                _swap_in(fleet, root, profile, state, folder, kept, staging, active is not None, now)
                staging = None
                state = fleet.read_state(root, profile)
                state["source"], state["copied_at"] = wanted, now
            elif wanted is None and active is not None:
                _swap_out(folder, kept, home, now)
                state.pop("source", None)
                state.pop("copied_at", None)
                state.pop("own_kept", None)
            if window is not None:
                state["window"] = window
            state.update(status="starting", pid=None)
            fleet.write_state(root, profile, state)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    result = read_choice(fleet, root, profile, home)
    if report is not None:
        result["copy"] = report
    return result


def take_over(fleet, root: Path, profile: str, home: Path, *, wait: float | None = None,
              now: float | None = None) -> dict:
    """The person hands a bot's browser to this plugin (Bot Info › Browser on the phone): a bot on Hermes' own
    ``browser.use_real_profile``, one whose ``cdp_url`` the person removed (``released``), or one pointed at a Chrome the
    person runs (``external``, ``fixed``). Only that profile's ``config.yaml`` changes, through Hermes' own writer
    (fleet.write_config), backed up first to ``<hermes root>/plugin-backups/hermes-hq-browser-takeover/``:
    ``browser.use_real_profile`` off (where it was on) and ``browser.cdp_url`` set to this plugin's address for the
    bot, or emptied for the supervisor to give it a port of its own. Then waits (``TAKE_OVER_WAIT``) for the
    supervisor to bring the bot's Chrome up and returns ``read_choice``. Raises ``ValueError`` (plain words) for a bot
    whose browser is deliberately off or elsewhere (``can_take_over`` false); nothing is changed then."""
    now = time.time() if now is None else now
    lock = fleet.state_folder(root) / f"{profile}.lock"
    with fleet.Locked(lock):
        config = fleet.read_config(home)
        state = fleet.read_state(root, profile)
        blocked = blocked_reason(fleet, config, state)
        if blocked is None:
            return read_choice(fleet, root, profile, home)
        if blocked not in TAKE_OVER:
            raise ValueError(REFUSED.get(blocked) or "Hermes HQ can’t run this bot’s browser.")
        current = str(fleet.browser_settings(config).get("cdp_url") or "").strip()
        if blocked == "real_profile" and fleet.port_of(current):
            url = current                       # already pointed at a Chrome of its own; only the real profile goes
        elif blocked in ("real_profile", "released") and fleet.port_of(state.get("url")):
            url = state["url"]                  # the address this plugin gave it before: its Chrome folder is there
        else:
            url = ""                            # the supervisor gives it a port of its own and writes it
        _back_up_config(root, profile, home, now)
        # The state first: a supervisor pass reading the new config must not take a leftover url for "released".
        fresh = {k: v for k, v in state.items() if k not in ("status", "pid", "error")}
        if not url:                             # read as "starting" until the supervisor has given it a port
            for key in ("url", "port", "folder"):
                fresh.pop(key, None)
        fleet.write_state(root, profile, {**fresh, "status": "starting"} if url else fresh)

        def change(config):
            settings = config.get("browser")
            if not isinstance(settings, dict):
                settings = config["browser"] = {}
            if settings.get("use_real_profile"):
                settings["use_real_profile"] = False
            settings["cdp_url"] = url
        try:
            fleet.write_config(home, change)
        except Exception:  # noqa: BLE001 - the config is as it was: so is the state
            fleet.write_state(root, profile, state)
            raise ValueError("Couldn’t change this bot’s Hermes settings. Try again.") from None
    deadline = time.time() + (TAKE_OVER_WAIT if wait is None else wait)
    while True:
        state = fleet.read_state(root, profile)
        port = state.get("port")
        if (blocked_reason(fleet, fleet.read_config(home), state) is None and isinstance(port, int)
                and fleet.answers(port, 1.0)):
            break
        if time.time() >= deadline:
            break                               # it comes up on a later pass; the phone shows where it is
        time.sleep(0.5)
    return read_choice(fleet, root, profile, home)


def _back_up_config(root: Path, profile: str, home: Path, now: float) -> Path | None:
    """A copy of the profile's config.yaml (owner-only) before take_over changes it."""
    source = home / "config.yaml"
    if not source.is_file():
        return None
    folder = root / "plugin-backups" / "hermes-hq-browser-takeover"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / f"{profile}-config-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{os.getpid()}.yaml"
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)
    return target


def _swap_in(fleet, root, profile, state, folder: Path, kept: Path, staging: Path, copy_active: bool, now: float) -> None:
    """Under the lock, with the bot's Chrome stopped: the staged copy becomes ``chrome-debug``. A first copy moves the
    bot's own profile to ``chrome-debug-own`` (an older leftover there is renamed, never deleted); a copy replacing a
    copy deletes the old copy only after the new one is in place. Any failure restores the folders."""
    old_copy = None
    moved_own = False
    try:
        if copy_active:
            if folder.exists():
                old_copy = folder.with_name(f"chrome-debug.old-{os.getpid()}")
                os.replace(folder, old_copy)
        else:
            fleet.write_state(root, profile, {**state, "own_kept": True})   # recorded before the move
            if kept.exists():
                os.replace(kept, kept.with_name(f"chrome-debug-own.{int(now)}"))
            if folder.exists():
                os.replace(folder, kept)
                moved_own = True
        os.replace(staging, folder)
    except Exception:
        if not folder.exists():
            if moved_own and kept.exists():
                os.replace(kept, folder)
            elif old_copy is not None and old_copy.exists():
                os.replace(old_copy, folder)
        if not copy_active:
            fleet.write_state(root, profile, {**state, "own_kept": False})
        raise
    if old_copy is not None:
        shutil.rmtree(old_copy, ignore_errors=True)


def _swap_out(folder: Path, kept: Path, home: Path, now: float) -> None:
    """Back to the bot's own profile: the copy (the person's sign-ins) is deleted, ``chrome-debug-own`` comes back."""
    if folder.exists():
        trash = home / f"chrome-debug.copy-removed-{os.getpid()}"
        os.replace(folder, trash)
        try:
            if kept.exists():
                os.replace(kept, folder)
        except Exception:
            os.replace(trash, folder)
            raise
        shutil.rmtree(trash, ignore_errors=True)
    elif kept.exists():
        os.replace(kept, folder)
