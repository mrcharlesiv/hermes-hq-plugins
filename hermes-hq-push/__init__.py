"""Hermes HQ push: Apple notifications for the Hermes HQ iPhone app (called dispatch-push before 0.5).

Hooks run wherever an agent turn runs; they only enqueue. App sessions (platform desktop/tui) live in
``hermes serve``, which also mounts ``dashboard/plugin_api.py``, so an approval pushed from here can be
answered from the lock screen through that route. A bot waiting for a password, login, code, secret or answer
(``on_human_input_request``) alerts too (kind "input"), and the alert goes when the prompt ends. A messaging-platform session's approval alerts only devices that
turned Outside Chats on, without buttons. Reply alerts don't come from hooks: ``push.ReplyWatcher`` (started by
``dashboard/plugin_api.py`` in the server) reads every profile's replies, so one install covers every bot.
On load it also puts the Hermes HQ theme in the desktop app's plugin folder (desktop_theme.py), and
restart_guard.py keeps a bot's one-time restart from looping and locking the app out. own_chats.py lets a Nous
sign-in delete, archive and rename the chats it made, which Hermes otherwise refuses (``permission_denied``), and
hands the old password login's chats to the one login the computer's owner names (0.9.3). It also lets the desktop app
on this computer open the chats the app made, which Hermes refused as not_found (0.9.11).
reply_back.py (loaded by the dashboard routes) posts a bot's answer back into the Telegram or Discord chat it came
from when the app asks, once per answer, only when the prompt was typed in the app (0.9.4).
ask_cards.py (0.9.12) asks a bot in an app chat to ask a question it needs answered with ``clarify``, so the app shows
a card, instead of ending its reply with the question in text.
page_previews.py (0.9.13) keeps the Sessions list fast on a bot with many scheduled runs: Hermes read every chat's
first message to build previews before keeping one page (2 s per list on a bot with 8,700 runs); now only the page's.
serve_warmup.py (0.9.15) loads every bot in the background after ``hermes serve`` starts, so the first chat opened
right after a restart answers at once (one sat on "Waking up…" ~2 minutes), and records what a frozen server was doing.
desktop_forward.py (0.9.16) lets the app send into a chat the desktop app has open: the message goes to the gateway that
holds it (Hermes refused it "open in another Hermes window"), and Hermes's hand-off to the gateway fits a long chat.
gateway_logins.py (0.9.17) lets a bot ask for a login, a one-time code or a password manager's unlock in a turn the
messaging gateway runs (another bot's message, a hand-off): the card shows in the app's copy of the chat, as it does for
a turn the app runs (Hermes answered "this session cannot ask the user for a login").
0.9.18 sends a login or code asked without naming the site back to the bot to name it: Hermes otherwise asks for the
browser tab in front, which was another site when the bot's own page didn't load (gateway_logins.unnamed_site).
0.9.44 makes Hermes use the named site's tab, or ask nothing when none is open (gateway_logins.named_tab): Hermes's
vault ignored the name and still asked for the first open tab with a password field, on any site.
0.9.57 does the same for a command's approval and a question (clarify) in a scheduled job's Bot Chat turn, which the
gateway runs with nobody watching: the card shows in the app's copy of the chat and the phone is alerted (Hermes
refused the question and the approval waited unseen until it timed out). With the app out of reach the bot is told to
ask in its reply instead (gateway_logins.forward_approval, install_job_asks).
device_access.py (0.9.19, every kind since 0.9.21) gives bots what the owner shares from the app's Settings › Device
Access (device_contacts_search, device_location, device_reminders, …): each use is a request the phone fetches (woken
by a silent push) and answers.
0.9.22 pushes the steps a bot takes to its turn's Lock Screen card once iOS has suspended the app (push.LiveFeed):
``pre_tool_call`` names each step (live_steps.py, the app's own words), ``on_session_end`` ends the card Done.
short_titles.py (0.9.28) keeps the names Hermes gives new chats short enough for one line of an iPhone screen.
cron_media.py (0.9.42) keeps a scheduled job's pictures when the bot it reports to summarizes it in a Bot Chat.
skills/dashboard-tile (0.9.30; plain tile names and the tile's job in 0.9.31) is a skill offered to every bot (hermes-hq-push:dashboard-tile): how to make a page for the
app's Dashboard that fills its tile at any size, a starter template, and check_tile.py, which draws it at every size.
tasks.py (0.9.32) is a shared to-do list between the owner and their bots: tools tasks_list, tasks_add, tasks_update
and tasks_next_prep (toolset hq_tasks), a tiny pre_llm_call note of the open tasks a bot shares with the owner, a
push (kind "task") when a bot sets a task ready or needs_you, and the "Task prep" scheduled job a bot with open
prep tasks gets (and loses once it has none). Never touches Hermes's Kanban. 0.9.33: the note carries the date and
asks a bot to take its part of what the user needs to do (who "together", its step started now with start_now), and
due takes words ("today 17:00") and is said back. 0.9.38: every task has a Task Page (task_page.py, GET
/tasks/{id}/page): one look for all, drawn from the task and what its bot writes with tasks_update(page=…), guided by
skills/task-page (hermes-hq-push:task-page). 0.9.39: the Tasks page's Ideas tab (ideas.py): once the user turns it
on, one bot's daily "Task ideas" job reads their recent chats and suggests a few tasks; pre_tool_call keeps that run
to reading only, and nothing becomes a task until the user adds it. 0.9.66: bots on a goal (goals.py): a goal's lead
and helpers, the plan and the weekly check-in as offer cards, tools goals_list, goals_offer, goals_update and
goals_next_review (toolset hq_goals), a note naming the person's goals, one "Goal check-in" job per lead bot (kept to
research and drafts by pre_tool_call), and the Goal Page (GET /goals/{id}/page; skills/goal-page).
0.9.86: travel.py shows flights and hotels as cards (show_flights, show_hotels; toolset hq_travel), and
tool_permissions.py makes a connector tool's level from the app's Connectors real: pre_tool_call asks the person
through Hermes's own approval before a tool set to ask runs (last, so every refusal above stands).
0.9.87: show_cards.py tells a bot in an app chat to show the flights, hotels and products it finds with those card
tools instead of a table (Hermes's tool search keeps add-on tools out of a busy bot's tool list).
0.9.90: ideas.offer() takes one idea from another add-on (hermes-hq-proactive 0.3.0+) into the Ideas tab, with the
same checks as a run's ideas; ideas.OUTSIDE_API says it is there.
0.9.91: ask_cards.py and show_first.py make sure a bot writes needed context in chat before opening a clarify card,
and replies in plain chat text first whenever the person skips a card or asks to explain.
"""
import importlib.util
import logging
import os
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("hermes_hq_push_core", Path(__file__).with_name("push.py"))
push = importlib.util.module_from_spec(_spec)
sys.modules["hermes_hq_push_core"] = push  # before exec: dataclasses resolve annotations through it
_spec.loader.exec_module(push)
_guard_spec = importlib.util.spec_from_file_location("hermes_hq_push_restart_guard", Path(__file__).with_name("restart_guard.py"))
restart_guard = importlib.util.module_from_spec(_guard_spec)
_guard_spec.loader.exec_module(restart_guard)
_own_spec = importlib.util.spec_from_file_location("hermes_hq_push_own_chats", Path(__file__).with_name("own_chats.py"))
own_chats = importlib.util.module_from_spec(_own_spec)
_own_spec.loader.exec_module(own_chats)
_ask_spec = importlib.util.spec_from_file_location("hermes_hq_push_ask_cards", Path(__file__).with_name("ask_cards.py"))
ask_cards = importlib.util.module_from_spec(_ask_spec)
_ask_spec.loader.exec_module(ask_cards)
_cards_spec = importlib.util.spec_from_file_location("hermes_hq_push_show_cards", Path(__file__).with_name("show_cards.py"))
show_cards = importlib.util.module_from_spec(_cards_spec)
_cards_spec.loader.exec_module(show_cards)
_pages_spec = importlib.util.spec_from_file_location("hermes_hq_push_page_previews", Path(__file__).with_name("page_previews.py"))
page_previews = importlib.util.module_from_spec(_pages_spec)
_pages_spec.loader.exec_module(page_previews)
_fwd_spec = importlib.util.spec_from_file_location("hermes_hq_push_desktop_forward", Path(__file__).with_name("desktop_forward.py"))
desktop_forward = importlib.util.module_from_spec(_fwd_spec)
_fwd_spec.loader.exec_module(desktop_forward)
_warm_spec = importlib.util.spec_from_file_location("hermes_hq_push_serve_warmup", Path(__file__).with_name("serve_warmup.py"))
serve_warmup = importlib.util.module_from_spec(_warm_spec)
_warm_spec.loader.exec_module(serve_warmup)
_logins_spec = importlib.util.spec_from_file_location("hermes_hq_push_gateway_logins", Path(__file__).with_name("gateway_logins.py"))
gateway_logins = importlib.util.module_from_spec(_logins_spec)
_logins_spec.loader.exec_module(gateway_logins)
_titles_spec = importlib.util.spec_from_file_location("hermes_hq_push_short_titles", Path(__file__).with_name("short_titles.py"))
short_titles = importlib.util.module_from_spec(_titles_spec)
_titles_spec.loader.exec_module(short_titles)
_media_spec = importlib.util.spec_from_file_location("hermes_hq_push_cron_media", Path(__file__).with_name("cron_media.py"))
cron_media = importlib.util.module_from_spec(_media_spec)
_media_spec.loader.exec_module(cron_media)
_access_spec = importlib.util.spec_from_file_location("hermes_hq_device_access", Path(__file__).with_name("device_access.py"))
device_access = importlib.util.module_from_spec(_access_spec)
_access_spec.loader.exec_module(device_access)
_show_spec = importlib.util.spec_from_file_location("hermes_hq_push_show_first", Path(__file__).with_name("show_first.py"))
show_first = importlib.util.module_from_spec(_show_spec)
_show_spec.loader.exec_module(show_first)
if "hermes_hq_drafts" in sys.modules:   # one copy for this process: the tool, the guard and the route share it
    drafts = sys.modules["hermes_hq_drafts"]
else:
    _drafts_spec = importlib.util.spec_from_file_location("hermes_hq_drafts", Path(__file__).with_name("drafts.py"))
    drafts = importlib.util.module_from_spec(_drafts_spec)
    sys.modules["hermes_hq_drafts"] = drafts
    _drafts_spec.loader.exec_module(drafts)
if "hermes_hq_asks" in sys.modules:   # one copy for this process: the tool and the route share it
    asks = sys.modules["hermes_hq_asks"]
else:
    _asks_spec = importlib.util.spec_from_file_location("hermes_hq_asks", Path(__file__).with_name("asks.py"))
    asks = importlib.util.module_from_spec(_asks_spec)
    sys.modules["hermes_hq_asks"] = asks
    _asks_spec.loader.exec_module(asks)
if "hermes_hq_shopping" in sys.modules:   # one copy for this process: the tools, the answers and the route share it
    shopping = sys.modules["hermes_hq_shopping"]
else:
    _shopping_spec = importlib.util.spec_from_file_location("hermes_hq_shopping", Path(__file__).with_name("shopping.py"))
    shopping = importlib.util.module_from_spec(_shopping_spec)
    sys.modules["hermes_hq_shopping"] = shopping
    _shopping_spec.loader.exec_module(shopping)
if "hermes_hq_travel" in sys.modules:   # [travel] 0.9.86: one copy for this process: the tools and the route share it
    travel = sys.modules["hermes_hq_travel"]
else:
    _travel_spec = importlib.util.spec_from_file_location("hermes_hq_travel", Path(__file__).with_name("travel.py"))
    travel = importlib.util.module_from_spec(_travel_spec)
    sys.modules["hermes_hq_travel"] = travel
    _travel_spec.loader.exec_module(travel)
if "hermes_hq_tool_permissions" in sys.modules:   # [tool-permissions] 0.9.86: shared with the status route
    tool_permissions = sys.modules["hermes_hq_tool_permissions"]
else:
    _perms_spec = importlib.util.spec_from_file_location("hermes_hq_tool_permissions",
                                                         Path(__file__).with_name("tool_permissions.py"))
    tool_permissions = importlib.util.module_from_spec(_perms_spec)
    sys.modules["hermes_hq_tool_permissions"] = tool_permissions
    _perms_spec.loader.exec_module(tool_permissions)
if "hermes_hq_tasks" in sys.modules:   # one copy for this process: ideas.py and the dashboard routes share it
    tasks = sys.modules["hermes_hq_tasks"]
else:
    _tasks_spec = importlib.util.spec_from_file_location("hermes_hq_tasks", Path(__file__).with_name("tasks.py"))
    tasks = importlib.util.module_from_spec(_tasks_spec)
    sys.modules["hermes_hq_tasks"] = tasks
    _tasks_spec.loader.exec_module(tasks)
if "hermes_hq_ideas" in sys.modules:
    ideas = sys.modules["hermes_hq_ideas"]
else:
    _ideas_spec = importlib.util.spec_from_file_location("hermes_hq_ideas", Path(__file__).with_name("ideas.py"))
    ideas = importlib.util.module_from_spec(_ideas_spec)
    sys.modules["hermes_hq_ideas"] = ideas
    _ideas_spec.loader.exec_module(ideas)
if "hermes_hq_goals" in sys.modules:   # 0.9.66: one copy for this process: the tools, the note and the routes share it
    goals = sys.modules["hermes_hq_goals"]
else:
    _goals_spec = importlib.util.spec_from_file_location("hermes_hq_goals", Path(__file__).with_name("goals.py"))
    goals = importlib.util.module_from_spec(_goals_spec)
    sys.modules["hermes_hq_goals"] = goals
    _goals_spec.loader.exec_module(goals)

log = logging.getLogger(push.NAME)
_dispatcher = None
_live_feed = None
_sent_approvals: dict = {}
_sent_inputs: dict = {}


def _profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


_sender = push.profile_sender


def _dispatcher_for():
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = push.Dispatcher(push.Store(push.data_dir() / "devices.db"))
    return _dispatcher


def _live():
    """[live-steps] This process's LiveFeed; its store opens on its own thread, at the first step."""
    global _live_feed
    if _live_feed is None:
        _live_feed = push.LiveFeed(lambda: push.Store(push.data_dir() / "devices.db"))
    return _live_feed


def _serves_app_sessions() -> bool:
    """App approvals are queued in the process running tui_gateway (``hermes serve``), which marks
    itself interactive; the messaging gateway's approvals belong to its own platforms."""
    return "tui_gateway.server" in sys.modules and os.environ.get("HERMES_INTERACTIVE") == "1"


_lineage = push.Lineage()


def _in_its_chat(kwargs: dict, profile: str) -> dict:
    """A helper's approval or question names the chat that started the helper (where its card is), not the helper's
    own working session. The settle hooks reuse the alert that was sent, so they follow."""
    session = str(kwargs.get("session_id") or "")
    try:
        chat = _lineage.chat(profile, session) if session else session
    except Exception:
        chat = session
    return kwargs if chat == session else {**kwargs, "session_id": chat}


def pre_approval_request(**kwargs):
    try:
        # The messaging gateway's approvals are answered in their own app; they alert (without buttons) only a
        # device that turned Outside Chats on. One in a Bot Chat (another bot's message) has no app of its own and no
        # one watching it: it alerts every device, also without buttons (the approval waits in the messaging gateway).
        serves = _serves_app_sessions()
        key = str(kwargs.get("session_key") or "")
        outside = "" if serves else push.outside_platform(key)
        bot_chat = not serves and not outside and push.bot_chat(key)
        if not serves and not outside and not bot_chat:
            return
        from tools.approval import list_gateway_approvals
        # [job-asks] A job's turn in a Bot Chat (nobody typed it, no viewer can answer): the card goes to the app's
        # copy of the chat (gateway_logins.forward_approval), and the alert goes even for the "cli" surface a Bot
        # Chat Hermes made itself reports (0.9.57).
        job = bot_chat and _job_approval(kwargs, key)
        profile = _profile()
        kwargs = _in_its_chat(kwargs, profile)
        # 0.9.82: a card fill to confirm (browser_vault's "vault-payment") and an MCP server's question or trust
        # prompt ("mcp-elicitation/<server>", "mcp-trust/<server>") wait on the same timer as a command, so they
        # alert too. They used to wait unseen until they timed out.
        surfaces = push.CONFIRM_SURFACES + (("cli",) if job else ())
        item = push.approval_push(kwargs, list_gateway_approvals(key), push.data_dir(), profile, _sender(profile),
                                  outside, answerable=serves, surfaces=surfaces)
        if item is not None:
            push.remember_approval(_sent_approvals, kwargs, item)
            _dispatcher_for().submit(item)
    except Exception:
        log.debug("hermes-hq-push approval hook failed", exc_info=True)


def _job_approval(kwargs: dict, key: str) -> bool:
    """[job-asks] This approval waits in a job's Bot Chat turn nobody can answer here; its card is sent to the app
    when the server can show it. Never raises."""
    try:
        if gateway_logins.job_turn(key, "session:approve") is None:
            return False
    except Exception:
        log.debug("hermes-hq-push: job approval check failed", exc_info=True)
        return False
    try:
        gateway_logins.forward_approval(gateway_logins.socket_path(push.data_dir()), kwargs)
    except Exception:
        log.debug("hermes-hq-push: job approval not sent to the app", exc_info=True)
    return True


def post_approval_response(**kwargs):
    try:
        _dispatcher_for().submit(push.settled_push(kwargs, _sent_approvals))
    except Exception:
        log.debug("hermes-hq-push settle hook failed", exc_info=True)


def on_human_input_request(**kwargs):
    """A bot in an app chat waits for a password, login, code, secret or answer: alert the phone, as an approval does,
    so the prompt isn't missed while the app is in the background (its socket closed, nothing shows the card)."""
    try:
        if not _serves_app_sessions():
            # [gateway-logins] A vault prompt in a gateway turn that goes to the app's copy of the chat (0.9.17).
            # [job-asks] So does a question in a job's Bot Chat turn (0.9.57).
            kind = str(kwargs.get("kind") or "")
            path = gateway_logins.socket_path(push.data_dir())
            if kind.startswith("vault_"):
                if not gateway_logins.would_ask(path):
                    return
            elif kind != "clarify" or not gateway_logins.would_forward_clarify(path, str(kwargs.get("session_key") or "")):
                return
            kwargs = {**kwargs, "platform": "desktop"}
        profile = _profile()
        item = push.input_push(_in_its_chat(kwargs, profile), profile, _sender(profile))
        if item is not None:
            push.remember_input(_sent_inputs, item)
            _dispatcher_for().submit(item)
    except Exception:
        log.debug("hermes-hq-push input hook failed", exc_info=True)


def on_human_input_resolved(**kwargs):
    try:
        _dispatcher_for().submit(push.settled_input(kwargs, _sent_inputs))
    except Exception:
        log.debug("hermes-hq-push input settle hook failed", exc_info=True)


def pre_llm_call(platform="", parent_session_id="", user_message=None, conversation_history=None, session_id="",
                 turn_id="", **kwargs):
    try:  # [show-first] each turn starts clean and notes whether the person asked for context or unseen bot work ran
        show_first.note_turn(session_id, user_message=user_message, conversation_history=conversation_history)
    except Exception:
        pass
    try:  # [cron-media] every process: a Bot Chat job's hand-off can run in the messaging gateway too
        cron_media.note_turn(session_id, turn_id, user_message)
    except Exception:
        log.debug("hermes-hq-push: job attachments not noted", exc_info=True)
    try:  # [drafts] the person's Send or Don't Send on a draft card (drafts.py)
        drafts.note_decision(user_message, str(session_id or ""))
    except Exception:
        log.debug("hermes-hq-push: draft answer not noted")
    shopping_note = None
    try:  # [shopping] the person's Pay or Cancel lifts the chat's pay block; Buy and Notify Me are noted (shopping.py)
        shopping_note = shopping.note_decision(push.data_dir(), user_message, str(session_id or ""))
    except Exception:
        log.debug("hermes-hq-push: shopping answer not noted")
    try:  # [asks] Send on an ask card's ready reply lets that reply out of the drafts guard (asks.py)
        asks.note_answer(user_message)
    except Exception:
        log.debug("hermes-hq-push: ask answer not noted")
    try:  # [goals] every process: whether the person is in this chat, so a bot's tasks offer there is a card at once
        goals.note_turn(session_id, platform, user_message)
    except Exception:
        log.debug("hermes-hq-push: goals turn not noted")
    if not _serves_app_sessions():
        # [asks] A job's report handed to a Bot Chat (a gateway turn): ask with the card, not in text.
        try:
            note = asks.job_note(user_message, data_dir=push.data_dir(), in_app_chat=_in_app_chat())
            if note:
                return {"context": note}
        except Exception:
            log.debug("hermes-hq-push: job ask note skipped", exc_info=True)
    # Only turns ``hermes serve`` runs: a card asked from a turn the messaging gateway runs in an app chat (another
    # bot's message, a job's hand-off) can't reach the app and comes back undelivered. [tasks] The same restriction
    # keeps the Tasks note out of cron runs and hand-offs: only a conversation with the user adds it.
    if not _serves_app_sessions():
        return None
    notes = [shopping_note] if shopping_note else []
    try:
        note = ask_cards.context_for(data_dir=push.data_dir(), platform=platform, parent_session_id=parent_session_id,
                                     user_message=user_message, conversation_history=conversation_history)
        if note:
            notes.append(note)
    except Exception:
        log.debug("hermes-hq-push: ask-cards note skipped", exc_info=True)
    try:  # [show-cards] 0.9.87: flights, hotels and products go in their card tools, not in a table (show_cards.py)
        root = push.data_dir()
        note = show_cards.context_for(
            data_dir=root, travel=travel.switched_on(root) and travel.cards_shown(root),
            shopping=shopping.switched_on(root) and shopping.cards_shown(root), platform=platform,
            parent_session_id=parent_session_id, user_message=user_message, conversation_history=conversation_history)
        if note:
            notes.append(note)
    except Exception:
        log.debug("hermes-hq-push: show-cards note skipped", exc_info=True)
    try:
        note = tasks.context_for(store=_tasks_store(), profile=_profile(), platform=platform,
                                 parent_session_id=parent_session_id, user_message=user_message,
                                 conversation_history=conversation_history, session_id=str(session_id or ""))
        if note:
            notes.append(note)
    except Exception:
        log.debug("hermes-hq-push: tasks note skipped", exc_info=True)
    try:  # [goals] 0.9.66: the person's goals by name and why; in a goal's own chat, its brief and the lead's duties
        note = goals.context_for(store=_tasks_store(), profile=_profile(), platform=platform,
                                 parent_session_id=parent_session_id, user_message=user_message,
                                 conversation_history=conversation_history, session_id=str(session_id or ""))
        if note:
            notes.append(note)
    except Exception:
        log.debug("hermes-hq-push: goals note skipped", exc_info=True)
    try:  # [tasks] 0.9.47: an answer in a task's own chat starts it moving again (after the note read its question)
        tasks.answer_started(_tasks_store(), str(session_id or ""), platform=platform, user_message=user_message)
    except Exception:
        log.debug("hermes-hq-push: tasks answer not noted", exc_info=True)
    return {"context": " ".join(notes)} if notes else None


def post_llm_call(session_id="", **kwargs):
    """[tasks] 0.9.47: the turn that answered a task's question ended; a task its bot didn't touch goes back to open."""
    if not _serves_app_sessions():
        return None
    try:
        tasks.answer_finished(_tasks_store(), str(session_id or ""))
    except Exception:
        log.debug("hermes-hq-push: tasks answer not finished", exc_info=True)
    return None


def transform_llm_output(response_text=None, session_id="", turn_id="", **kwargs):
    """[cron-media] A Bot Chat job's summary keeps the job's pictures (``cron_media``); None leaves the reply alone."""
    try:
        return cron_media.carry(response_text, session_id, turn_id)
    except Exception:
        log.debug("hermes-hq-push: job attachments not carried", exc_info=True)
        return None


def _desktop_theme():
    """The Hermes HQ theme for the desktop app (desktop_theme.py): a nicety, so nothing here ever stops the plugin."""
    try:
        spec = importlib.util.spec_from_file_location("hermes_hq_push_theme", Path(__file__).with_name("desktop_theme.py"))
        theme = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(theme)
        from hermes_constants import get_default_hermes_root
        outcome = theme.install(get_default_hermes_root())
        if outcome in ("added", "updated"):
            log.info("hermes-hq-push: %s the Hermes HQ theme for the desktop app", outcome)
    except Exception:
        log.debug("hermes-hq-push: desktop theme not written", exc_info=True)


def pre_tool_call(tool_name="", args=None, **kwargs):
    verdict = (_veto(tool_name, args) or _code_veto(tool_name, args, kwargs)
               or _ideas_veto(tool_name, args, kwargs.get("session_id"))
               or _goals_veto(tool_name, kwargs.get("session_id"))
               or _draft_veto(tool_name, args, kwargs.get("session_id"))
               or _pay_block(tool_name, args, kwargs.get("session_id")) or _show_first(tool_name, args, kwargs)
               or _job_clarify(tool_name)
               or _connector_level(tool_name, args))
    if verdict is None or (isinstance(verdict, dict) and verdict.get("action") == "approve"):
        _named_site(tool_name, args, kwargs.get("task_id"))
        _live_step(tool_name, args, kwargs)
        if tool_name == "browser_ask_user":
            _browser_ask_alert(args, kwargs)
    return verdict


def _connector_level(tool_name, args):
    """[tool-permissions] 0.9.86: a connector tool set to ask in the app's Connectors (tools.permissions in the bot's
    config.yaml) asks the person through Hermes's own approval, or is refused where this Hermes can't ask
    (tool_permissions.check). Last, so every refusal above stands as it was: Hermes takes any block over an approve.
    One prefix test for a tool that isn't a connector's; never raises, never logs the call's arguments."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp_"):
        return None
    try:
        verdict = tool_permissions.check(tool_name, args)
    except Exception:
        return None
    if verdict:
        log.info("hermes-hq-push: %s %s: its connector level asks first", tool_name,
                 "asked" if verdict["action"] == "approve" else "refused")
    return verdict


def _show_first(tool_name, args, kwargs):
    """[show-first] A clarify while a picture or context it depends on is still unseen is held once (show_first.py)."""
    try:
        verdict = show_first.check(tool_name, args, kwargs.get("session_id"))
    except Exception:
        log.debug("hermes-hq-push: show-first check failed", exc_info=True)
        return None
    if verdict:
        log.info("hermes-hq-push: clarify held: %s",
                 "picture isn't in the chat yet" if "MEDIA:" in str(verdict.get("message") or "") else "context isn't in the chat yet")
    return verdict


def post_api_request(session_id="", assistant_message=None, **kwargs):
    """[show-first] The bot's words with each model call: a MEDIA line shows what it made (show_first.note_text)."""
    try:
        show_first.note_text(session_id, getattr(assistant_message, "content", None))
    except Exception:
        log.debug("hermes-hq-push: show-first text not noted", exc_info=True)
    return None


def on_interim_message(session_id="", text="", **kwargs):
    """[show-first] Words the bot sent mid-turn (show_first.note_text)."""
    try:
        show_first.note_text(session_id, text)
    except Exception:
        log.debug("hermes-hq-push: show-first interim not noted", exc_info=True)
    return None


def _job_clarify(tool_name):
    """[job-asks] clarify in a job's Bot Chat turn, in the gateway: note where the chat is for the question's send
    (gateway_logins.note_clarify); when the app can't be reached to show it, tell the bot at once to ask in its
    reply instead (Hermes would refuse the question). Only in the messaging gateway; never raises."""
    if tool_name != "clarify" or _serves_app_sessions():
        return None
    try:
        from tools import approval_context
        key = approval_context.get_current_session_key(default="")
        if not push.bot_chat(key):
            return None
        path = gateway_logins.socket_path(push.data_dir())
        gateway_logins.note_clarify(path, key)
        message = gateway_logins.clarify_unreachable(path, key)
    except Exception:
        log.debug("hermes-hq-push: job question check failed", exc_info=True)
        return None
    if not message:
        return None
    log.info("hermes-hq-push: clarify in a job's Bot Chat turn with the app out of reach: the bot asks in its reply")
    return {"action": "block", "message": message}


def _pay_block(tool_name, args, session_id):
    """[shopping] A browser action in a chat whose order card waits for Pay or Cancel (shopping.blocked_message). In
    every process: a job's hand-off the messaging gateway runs in that chat is held the same way."""
    try:
        if not shopping.acts_in_browser(tool_name, args):
            return None
        message = shopping.blocked_message(push.data_dir(), tool_name, args, session_id or "")
    except Exception:
        log.debug("hermes-hq-push: pay block check failed", exc_info=True)
        return None
    if not message:
        return None
    log.info("hermes-hq-push: %s held: an order card waits for Pay or Cancel", tool_name)
    return {"action": "block", "message": message}


def _draft_veto(tool_name, args, session_id):
    """[drafts] A sending tool carrying the text of a draft the person hasn't approved (drafts.blocked_message)."""
    try:
        message = drafts.blocked_message(tool_name, args, session_id or "")
    except Exception:
        return None
    if not message:
        return None
    log.info("hermes-hq-push: %s held: it carries an unapproved draft", tool_name)
    return {"action": "block", "message": message}


def transform_tool_result(tool_name="", result=None, task_id="", **kwargs):
    """[0.9.62] A browser_vault call is over: a sign-in frame it used is let go, and "no code field" says the person
    can't type into the page (gateway_logins.vault_result). [0.9.89] Ending browser_ask_user settles its push alert.
    [0.9.91] A skipped or 'explain first' clarify result arms show_first so the bot writes context before asking again."""
    if tool_name == "clarify":
        try:
            return show_first.note_clarify_result(kwargs.get("session_id") or task_id, result)
        except Exception:
            log.debug("hermes-hq-push: clarify result note skipped", exc_info=True)
            return None
    if tool_name == "browser_ask_user":
        _browser_ask_settle(kwargs)
        return None
    if tool_name != "browser_vault":
        return None
    try:
        return gateway_logins.vault_result(tool_name, result, task_id or "default")
    except Exception:
        log.debug("hermes-hq-push: vault result left as it was", exc_info=True)
        return None


_sent_browser_asks: dict = {}


def _browser_ask_alert(args, kwargs):
    """[0.9.89] Alert the phone when a bot hands its browser over with browser_ask_user so it doesn't time out unseen."""
    try:
        if not _serves_app_sessions():
            return
        reason = str((args or {}).get("reason") or "").strip()
        if not reason:
            return
        profile = _profile()
        info = _in_its_chat(kwargs, profile)
        session = str(info.get("session_id") or info.get("task_id") or "")
        if not session:
            return
        raw_id = str(kwargs.get("tool_call_id") or session)
        req_id = "bask-" + "".join(ch for ch in raw_id if ch.isalnum() or ch in "_-")[:54]
        item = push.input_push(
            {"kind": "browser_ask", "platform": "desktop", "session_id": session,
             "request_id": req_id, "prompt": reason},
            profile, _sender(profile),
        )
        if item is not None:
            push.remember_input(_sent_inputs, item)
            _sent_browser_asks[str(kwargs.get("tool_call_id") or session)] = req_id
            _dispatcher_for().submit(item)
    except Exception:
        log.debug("hermes-hq-push: browser_ask alert failed", exc_info=True)


def _browser_ask_settle(kwargs):
    """[0.9.89] Withdraw the browser_ask push alert once browser_ask_user returns (handed back or timed out)."""
    try:
        key = str(kwargs.get("tool_call_id") or kwargs.get("session_id") or kwargs.get("task_id") or "")
        req_id = _sent_browser_asks.pop(key, None)
        if req_id:
            _dispatcher_for().submit(push.settled_input({"request_id": req_id}, _sent_inputs))
    except Exception:
        log.debug("hermes-hq-push: browser_ask settle failed", exc_info=True)


def _named_site(tool_name, args, task_id):
    """[0.9.44] A login or code that names its site is asked for that site's tab only (gateway_logins.named_tab)."""
    try:
        gateway_logins.remember_site(tool_name, args, task_id)
    except Exception:
        log.debug("hermes-hq-push: named site not kept", exc_info=True)


def _live_step(tool_name, args, kwargs):
    """[live-steps] The step this tool is, for the turn's Lock Screen card: only queued here (push.LiveFeed); never
    raises, never waits, never logs the step or its arguments."""
    try:
        if push.live_steps.is_silent(tool_name):
            return
        _live().step(tool_name, args, kwargs.get("session_id"), _profile())
    except Exception:
        log.debug("hermes-hq-push: live step skipped")


def on_session_end(session_id="", interrupted=False, **kwargs):
    """[live-steps] A turn ended (fires once per turn; a teardown repeat carries interrupted=True): its Lock Screen card
    ends Done. An interrupted turn is left to the app: a /stop or a new message comes from someone with the app open,
    and the next turn may carry on the same card."""
    try:
        if interrupted or not session_id:
            return
        _live().ended(session_id, _profile())
    except Exception:
        log.debug("hermes-hq-push: live end skipped")


def _veto(tool_name, args):
    try:
        unnamed = gateway_logins.unnamed_site(tool_name, args)
    except Exception:
        unnamed = None
    if unnamed:
        log.info("hermes-hq-push: %s %s without a site: asked the bot to name it", tool_name, (args or {}).get("action"))
        return {"action": "block", "message": unnamed}
    try:
        message = restart_guard.blocked_message(args)
    except Exception:
        return None
    if message:
        log.warning("hermes-hq-push: blocked a %s call that ran launchctl submit", tool_name)
        return {"action": "block", "message": message}
    return None


def _code_veto(tool_name, args, kwargs):
    """[0.9.59] The first ask for a site's code is sent back once so the bot looks for an emailed or texted code
    itself (gateway_logins.sent_code). Never logs the call's arguments."""
    try:
        message = gateway_logins.sent_code(tool_name, args, kwargs.get("session_id") or kwargs.get("task_id") or "")
    except Exception:
        return None
    if message:
        log.info("hermes-hq-push: a code was asked for; the bot looks for a sent one first")
        return {"action": "block", "message": message}
    return None


def _ideas_veto(tool_name, args, session_id):
    """[ideas] Inside the Task ideas run, enforces source boundaries and read-only safety (ideas.guard). Never logs args."""
    try:
        message = ideas.guard(tool_name, session_id, _tasks_store, args=args, profile=_profile())
    except Exception:
        return None
    if message:
        log.info("hermes-hq-push: task ideas run asked for %s; refused", tool_name)
        return {"action": "block", "message": message}
    return None


def _goals_veto(tool_name, session_id):
    """[goals] 0.9.66: inside this bot's Goal check-in run, nothing is sent, booked, bought, paid, posted or deleted
    (goals.guard, by the tool's name). Never logs the call's arguments."""
    try:
        message = goals.guard(tool_name, session_id, _tasks_store, _profile())
    except Exception:
        return None
    if message:
        log.info("hermes-hq-push: goal check-in run asked for %s; refused (research and drafts only)", tool_name)
        return {"action": "block", "message": message}
    return None


def _restart_guard():
    try:
        from hermes_constants import get_default_hermes_root
        restart_guard.start(get_default_hermes_root(), push.data_dir())
    except Exception:
        log.debug("hermes-hq-push: restart guard not started", exc_info=True)


def _own_chats():
    try:
        log.info("hermes-hq-push: own chats %s", own_chats.install())
    except Exception:
        log.debug("hermes-hq-push: own chats not installed", exc_info=True)
    try:
        log.info("hermes-hq-push: password chats %s", own_chats.install_password_chats())
    except Exception:
        log.debug("hermes-hq-push: password chats not installed", exc_info=True)
    try:
        log.info("hermes-hq-push: desktop opens app chats %s", own_chats.install_desktop_opens())
    except Exception:
        log.debug("hermes-hq-push: desktop opens app chats not installed", exc_info=True)


def _desktop_forward():
    try:
        log.info("hermes-hq-push: app messages into desktop chats %s", desktop_forward.install())
    except Exception:
        log.debug("hermes-hq-push: app messages into desktop chats not installed", exc_info=True)
    try:
        log.info("hermes-hq-push: hand-off frame limit %s", desktop_forward.install_frame_limit())
    except Exception:
        log.debug("hermes-hq-push: hand-off frame limit not installed", exc_info=True)


def _gateway_logins():
    try:
        log.info("hermes-hq-push: logins asked in gateway turns %s", gateway_logins.install_prompts(push.data_dir()))
    except Exception:
        log.debug("hermes-hq-push: logins asked in gateway turns not installed", exc_info=True)
    try:  # [0.9.62] a sign-in that sits in a frame of an open tab (DoorDash's identity.doordash.com), for a saved
        # login's fill too (not only a login or code the bot names, which installed the tab pick on first use)
        log.info("hermes-hq-push: sign-in frames %s, named tabs %s", gateway_logins.install_frames(),
                 gateway_logins.install_named_tab())
    except Exception:
        log.debug("hermes-hq-push: sign-in frames not installed", exc_info=True)
    try:  # [0.9.59] a login the person moved into the bots' vault instead of typing it
        log.info("hermes-hq-push: moved logins %s", gateway_logins.install_moved())
    except Exception:
        log.debug("hermes-hq-push: moved logins not installed", exc_info=True)
    try:  # [job-asks] the gateway's local chats; in the server it steps aside at each call (gateway_logins._serving)
        gateway_logins.set_context_reader(show_first.take_gateway_context)
        log.info("hermes-hq-push: questions in job turns %s", gateway_logins.install_job_asks(push.data_dir()))
    except Exception:
        log.debug("hermes-hq-push: questions in job turns not installed", exc_info=True)
    try:  # [0.9.59] a 1Password login fills on its whole site (www., account., ...), as 1Password does
        spec = importlib.util.spec_from_file_location("hermes_hq_push_site_match", Path(__file__).with_name("site_match.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        log.info("hermes-hq-push: 1Password logins on their whole site %s", module.install())
    except Exception:
        log.debug("hermes-hq-push: 1Password logins on their whole site not installed", exc_info=True)


def _onepassword_allow():
    """[0.9.59] A 1Password key saved from the app works at once, in this process too (onepassword_allow.install)."""
    try:
        spec = importlib.util.spec_from_file_location("hermes_hq_push_onepassword_allow", Path(__file__).with_name("onepassword_allow.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        log.info("hermes-hq-push: allow in 1Password %s", module.install())
    except Exception:
        log.debug("hermes-hq-push: allow in 1Password not installed", exc_info=True)


def _page_previews():
    try:
        log.info("hermes-hq-push: page previews %s", page_previews.install())
    except Exception:
        log.debug("hermes-hq-push: page previews not installed", exc_info=True)


# The Dashboard tile skill (skills/dashboard-tile): how to make a page that fills a tile in the app's Dashboard at any
# size and stays fresh. Offered to every bot as "hermes-hq-push:dashboard-tile"; read-only, never copied into skills/.
DASHBOARD_SKILL = Path(__file__).resolve().parent / "skills" / "dashboard-tile" / "SKILL.md"


def _dashboard_skill(ctx):
    if not hasattr(ctx, "register_skill"):   # a Hermes before plugin skills
        log.info("hermes-hq-push: dashboard tile skill not offered (this Hermes has no plugin skills)")
        return
    try:
        ctx.register_skill("dashboard-tile", DASHBOARD_SKILL,
                           "Make a live tile for the Hermes HQ app's Dashboard: an HTML page that fills its tile at "
                           "any size, checked at every size, kept fresh by a scheduled job.")
        log.info("hermes-hq-push: dashboard tile skill offered")
    except Exception:
        log.warning("hermes-hq-push: dashboard tile skill not offered", exc_info=True)


# The Task Page skill (skills/task-page, 0.9.38): what goes on a task's Task Page and how to write it. The page's look is
# the add-on's (task_page.py fills in skills/task-page/template.html); the skill is about the words.
TASK_PAGE_SKILL = Path(__file__).resolve().parent / "skills" / "task-page" / "SKILL.md"


def _task_page_skill(ctx):
    if not hasattr(ctx, "register_skill"):
        log.info("hermes-hq-push: task page skill not offered (this Hermes has no plugin skills)")
        return
    try:
        ctx.register_skill("task-page", TASK_PAGE_SKILL,
                           "Keep a task's Task Page current in the Hermes HQ app: where it stands, what's next and "
                           "what you found, written with tasks_update(page=…) each time you work on a task.")
        log.info("hermes-hq-push: task page skill offered")
    except Exception:
        log.warning("hermes-hq-push: task page skill not offered", exc_info=True)


# The Goal Page skill (skills/goal-page, 0.9.66): what a goal's lead writes in where it stands, a check-in and its
# Goal Page. The page's look is the add-on's (goals.render_page fills in skills/goal-page/template.html).
GOAL_PAGE_SKILL = Path(__file__).resolve().parent / "skills" / "goal-page" / "SKILL.md"


def _goal_page_skill(ctx):
    if not hasattr(ctx, "register_skill"):
        log.info("hermes-hq-push: goal page skill not offered (this Hermes has no plugin skills)")
        return
    try:
        ctx.register_skill("goal-page", GOAL_PAGE_SKILL,
                           "Lead a goal in the Hermes HQ app: what goes in where it stands, in a weekly check-in and on "
                           "its Goal Page, written with goals_update and goals_offer.")
        log.info("hermes-hq-push: goal page skill offered")
    except Exception:
        log.warning("hermes-hq-push: goal page skill not offered", exc_info=True)


# The page skill (skills/html-page, 0.9.78): how to make an HTML page the person opens in the app (a map, a report, a
# small tool) that holds together on a phone: a starter page in the app's look and check_page.py, which draws it
# at phone size in dark and light and names what's off (owner 2026-11-21: every bot's page made the same mistakes,
# a title and checkboxes in one wrapping row, edges that don't line up). A line in every prompt points at it, since
# a bot about to write a page doesn't think to look for a skill.
HTML_PAGE_SKILL = Path(__file__).resolve().parent / "skills" / "html-page" / "SKILL.md"
HTML_PAGE_NOTE = ("Pages for Hermes HQ: before you write an HTML page for the person to open (a map, a report, a chart, "
                  "a list, a small tool), load the hermes-hq-push:html-page skill, build from its starter page and "
                  "check the page with it at phone size. Dashboard tiles use hermes-hq-push:dashboard-tile instead.")


def _html_page_skill(ctx):
    if not hasattr(ctx, "register_skill"):
        log.info("hermes-hq-push: page skill not offered (this Hermes has no plugin skills)")
        return
    try:
        ctx.register_skill("html-page", HTML_PAGE_SKILL,
                           "Make an HTML page the person opens in the Hermes HQ app (a map, a report, a chart, a "
                           "list, a small tool): built from a starter in the app's look, checked at phone size.")
        if callable(getattr(ctx, "register_system_prompt_section", None)):
            ctx.register_system_prompt_section("hermes-hq-push.pages", HTML_PAGE_NOTE, max_chars=400)
        log.info("hermes-hq-push: page skill offered")
    except Exception:
        log.warning("hermes-hq-push: page skill not offered", exc_info=True)


# Background job names (0.9.81): the app's Background list names a running job by the bot's own sentence before the call
# that started it, else a leading `# comment` in the command (owner 2026-11-22: a row read "claude -p 'Read .goals-bri…"
# over "No output yet" for 1h 22m, "no way to have any idea what it's for"). A line in every prompt asks bots for both.
JOB_NAMES_NOTE = ("Background jobs in Hermes HQ: when you start a terminal command in the background (a build, a test run, "
                  "a coding agent), say in one plain sentence just before the call what it's for, and make the command's "
                  "first line a short comment naming it (# Fixing the four goal set-up issues). The app shows that as "
                  "the job's name while it runs.")


def _job_names_note(ctx):
    if not callable(getattr(ctx, "register_system_prompt_section", None)):
        log.info("hermes-hq-push: job names note not offered (this Hermes has no prompt sections)")
        return
    try:
        ctx.register_system_prompt_section("hermes-hq-push.jobs", JOB_NAMES_NOTE, max_chars=400)
        log.info("hermes-hq-push: job names note offered")
    except Exception:
        log.warning("hermes-hq-push: job names note not offered", exc_info=True)


def _short_titles():
    try:
        log.info("hermes-hq-push: short chat names %s", short_titles.install())
    except Exception:
        log.debug("hermes-hq-push: short chat names not installed", exc_info=True)


def _serve_warmup():
    """Only in ``hermes serve`` (the process the app talks to), which has its web server loaded by now."""
    if "hermes_cli.web_server" not in sys.modules:
        return
    try:
        log.info("hermes-hq-push: warm-up %s", serve_warmup.start_warmup(push.data_dir()))
    except Exception:
        log.debug("hermes-hq-push: warm-up not started", exc_info=True)


def _device_store():
    return device_access.Store(push.data_dir() / "device-access.db")


def _device_wake(device_id: str, request_id: str, profile: str) -> str:
    """A silent push to the one phone a Device Access request is for; what a bot is told about it."""
    try:
        registered = push.Store(push.data_dir() / "devices.db").get(device_id)
        if registered is None or push.sender_from_env() is None:
            return "Notifications are off on that phone, so it gets this when Hermes HQ is next opened."
        _dispatcher_for().submit(push.Push(kind="device", request=request_id, profile=profile, settled=True, to=device_id))
        return "Asked the phone to answer (iOS wakes it when it can)."
    except Exception:
        log.debug("hermes-hq-push: device access wake failed", exc_info=True)
        return "Couldn't wake the phone; it gets this when Hermes HQ is next opened."


def _device_tools(ctx):
    try:
        device_access.register_tools(ctx, _device_store, _device_wake, _profile)
    except Exception:
        log.warning("hermes-hq-push: device access tools not registered", exc_info=True)


def _tasks_store():
    return tasks.Store(push.data_dir() / "tasks.db")


def _tasks_notify(task: dict, profile: str) -> None:
    """A bot set status ``ready``/``needs_you`` on a shared task: push it to the user's devices, sealed like any
    other alert. Never the task's notes, steps or made items — only its title, and never logged or put on a Live
    Activity card (device_access-style best effort: this never raises back into the tool call)."""
    sender = _sender(profile)
    _dispatcher_for().submit(push.Push(kind="task", profile=profile, request=task.get("id") or "",
                                       sender=sender, text=(task.get("title") or "")[:200],
                                       about=task.get("status") or ""))
    log.info("hermes-hq-push: tasks push sent (%s)", task.get("status"))


def _tasks_tools(ctx):
    try:
        tasks.register_tools(ctx, _tasks_store, _profile, _tasks_notify, goals.person_in)
        log.info("hermes-hq-push: tasks finish on their word on")   # 0.9.81: the restart's proof of load
    except Exception:
        log.warning("hermes-hq-push: tasks tools not registered", exc_info=True)
    try:
        ideas.register_tool(ctx, _tasks_store, _profile)
    except Exception:
        log.warning("hermes-hq-push: task ideas tool not registered", exc_info=True)
    try:
        goals.register_tools(ctx, _tasks_store, _profile, _goals_notify)
    except Exception:
        log.warning("hermes-hq-push: goals tools not registered", exc_info=True)


def _goals_notify(goal: dict, offer: dict, profile: str) -> None:
    """[goals] A lead offered its weekly check-in: one push to the person's devices, sealed like any other ("<Bot>:
    check-in for <goal>"). Only the goal's title travels, and it is never logged."""
    _dispatcher_for().submit(push.Push(kind="task", profile=profile, request="goal:" + (goal.get("id") or ""),
                                       sender=_sender(profile), text=(goal.get("title") or "")[:200], about="checkin"))
    log.info("hermes-hq-push: goal check-in push sent")


def _draft_tool(ctx):
    try:
        drafts.register_tool(ctx, push.data_dir, _serves_app_sessions)
    except Exception:
        log.warning("hermes-hq-push: draft_message tool not registered", exc_info=True)


def _in_app_chat() -> bool:
    """[asks] This turn's chat is one the app shows: a `hermes serve` turn, or a turn the messaging gateway runs in
    a local app chat (a job's report handed to a Bot Chat; gateway_logins.turn), whose calls the chat keeps."""
    if _serves_app_sessions():
        return True
    try:
        return gateway_logins.turn() is not None
    except Exception:
        return False


def _ask_tool(ctx):
    try:
        asks.register_tool(ctx, push.data_dir, _in_app_chat)
    except Exception:
        log.warning("hermes-hq-push: ask_to_act tool not registered", exc_info=True)


def _shopping_tools(ctx):
    try:
        shopping.register_tool(ctx, push.data_dir, _serves_app_sessions)
    except Exception:
        log.warning("hermes-hq-push: shopping tools not registered", exc_info=True)


def _travel_tools(ctx):
    try:
        travel.register_tool(ctx, push.data_dir, _serves_app_sessions)
    except Exception:
        log.warning("hermes-hq-push: travel tools not registered", exc_info=True)


def _tasks_prep_job():
    """Startup safety net: reconcile this bot's "Task prep" job against its tasks.db right now, in case it fell
    out of sync (the job was removed by hand while prep tasks are still open, or vice versa)."""
    try:
        tasks.reconcile_prep_job(_tasks_store(), _profile())
    except Exception:
        log.debug("hermes-hq-push: tasks prep job not reconciled at load", exc_info=True)
    try:   # [ideas] the same for its "Task ideas" job: here if this is the ideas bot and Ideas is on, else gone
        ideas.reconcile_job(_tasks_store(), _profile())
    except Exception:
        log.debug("hermes-hq-push: task ideas job not reconciled at load", exc_info=True)
    try:   # [goals] 0.9.66: and its "Goal check-in": armed while it leads a goal with a check-in, else gone
        goals.reconcile_checkin_job(_tasks_store(), _profile())
    except Exception:
        log.debug("hermes-hq-push: goal check-in job not reconciled at load", exc_info=True)


def register(ctx):
    ctx.register_hook("pre_approval_request", pre_approval_request)
    ctx.register_hook("post_approval_response", post_approval_response)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("on_human_input_request", on_human_input_request)
    ctx.register_hook("on_human_input_resolved", on_human_input_resolved)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_api_request", post_api_request)
    ctx.register_hook("on_interim_message", on_interim_message)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("on_session_end", on_session_end)
    _device_tools(ctx)
    _tasks_tools(ctx)
    _draft_tool(ctx)
    _shopping_tools(ctx)
    _travel_tools(ctx)
    _ask_tool(ctx)
    _dashboard_skill(ctx)
    _task_page_skill(ctx)
    _goal_page_skill(ctx)
    _html_page_skill(ctx)
    _job_names_note(ctx)
    _desktop_theme()
    _restart_guard()
    _own_chats()
    _page_previews()
    _short_titles()
    _desktop_forward()
    _gateway_logins()
    _onepassword_allow()
    _serve_warmup()
    _tasks_prep_job()
