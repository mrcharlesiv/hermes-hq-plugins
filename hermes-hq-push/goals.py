"""Bots on a goal (0.9.66, Goals steps 3 to 5): a lead and helpers, the plan as an offer, the weekly check-in, a
number a bot reads, where the goal stands, and its Goal Page. Owner 2026-11-18: "whatever bots I add to it can work
to help me achieve that goal." The risk is five bots each helping their own way, so these rules hold in code here
(and in tasks.py's ``bot_goal_place`` for tasks_add / tasks_update), not only in the tools' words:

1. One lead per goal: it plans, runs the check-in, writes where the goal stands and keeps its page.
2. Helpers take tasks, not the plan: only the lead offers a plan or a check-in (``offer``). A plan may also set the
   goal's name, why, date and number, each only where the goal lacks it (``_setup_kept``).
3. Every bot knows the active goals by name and why (``known_to_all``, on by default); only the lead and helpers read
   a goal's tasks, milestones, number and page (``goals_list``, ``tasks.goal_seen``).
4. Offers, not edits: a bot puts its OWN task in a goal it is on; anything for the person, any milestone, any date,
   a new goal is an offer card. A goal's ``adds: "auto"`` lets its lead and helpers add the person's tasks directly.
5. A bot never ticks the person's task, a milestone or the goal: the lead says it looks done (``goal.offer``).
6. A bot never moves the number by guessing: only the number's source bot sets ``now``, and each look stamps
   ``checked_at``.
7. Research and drafts only: in the Goal check-in run a tool that sends, books, buys, pays, posts or deletes is
   refused (``guard``; best effort, by the tool's name).
8. Off is off: Tasks off, or Tasks settings ``goals: false``, hides the tools, drops the note and removes every
   check-in job. The goals are kept.
9. A refusal says once what to do instead (0.9.55: a bot was refused eight times in a row with the same words).

tasks.py's Store owns the database: goals live in its ``goals`` table, goal offers in its ``offers`` list (beside the
list offers, with a ``kind``), a goal's page in ``task_pages`` under ``goal:<id>``. This module takes a store, as
ideas.py does. Hermes imports (cron jobs, the run's session) are lazy and function-local, so it imports cleanly with
no Hermes checkout. Nothing here logs a goal's or a task's words.
"""
from __future__ import annotations

import html
import importlib.util
import json
import logging
import re
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable


def _load(name: str, file: str):
    if name in sys.modules:   # one copy for this process: the tools, the note and the routes share it
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tasks = _load("hermes_hq_tasks", "tasks.py")
ideas = _load("hermes_hq_ideas", "ideas.py")
task_page = _load("hermes_hq_task_page", "task_page.py")

log = logging.getLogger("hermes-hq-push")

KINDS = ("goal", "plan", "checkin", "tasks")
SUMMARY_MAX = 300
STANDS_TEXT_MAX = 400
STANDS_NEXT_MAX = 200
LOOKS_WHY_MAX = 160
BRIEF_TASKS = 30
NOTE_GOALS = 5
NOTE_WHY = 80
HISTORY_WEEKS = 12
PAGE_KEY = "goal:"            # a goal's page in task_pages: "goal:<id>" (a task's is its bare id)
FOLDED = "folded"             # a held offer its lead was handed at a check-in: folded in, or dropped
PERSON_TTL = 6 * 3600         # how long a typed turn says "the person is in this chat"
SETUP_KEYS = ("name", "why", "due", "measure")   # a plan offer's set-up fields: what it may set on the goal
BUILT_ON_CAP = 3              # a plan offer's built_on: what the plan rests on, a line each
BUILT_ON_MAX = 90
OFFER_TASK_MADE_CAP = 6       # what an offered task carries (owner 2026-11-23: "it would be great if it was just there")

CHECKIN_JOB_NAME = "Goal check-in"
CHECKIN_JOB_MARK = "goals_next_review"   # in every version's instruction: how an older, unrecorded one is told
CHECKIN_JOB_PROMPT = (
    "Run your goals' weekly check-in. Call goals_next_review. If it returns {\"stop\": ...}, end the turn there: "
    "don't retry or explain. Otherwise it gives you one goal you lead, with what moved since its last check-in. "
    "Look at what moved. If you are the source of its number (measure.source.bot is you), check the number where it "
    "says (measure.source.what) and call goals_update with measure_now (or checked=true when it hasn't moved); if "
    "you couldn't read it, say so plainly in stands and ask them. Then write where it stands with goals_update "
    "(stands: text and next, and page for what worked and what didn't). Then offer ONE check-in with goals_offer "
    "(kind 'checkin'): a two-sentence summary of what moved, and the next three or so tasks, each with who. Fold in "
    "the held offers it lists if they're worth doing; the rest are dropped. Research and drafts only: never send, "
    "book, buy, pay, post or delete anything for the goal. Then call goals_next_review again, until it says stop."
)

# What every bot hears (pre_llm_call), next to Tasks' own note.
MARK = "[Hermes HQ Goals note, not from the person:"
GOALS_RULE = ("When something they say serves one of these, point it there with goals_offer (kind 'tasks'; kind 'goal' "
              "for a new aim they state, \"I want to…\"): a task for them waits for their tap, so ask (\"Want … on …?\"), "
              "never say you added it. Your own step on a goal you lead or help goes in with tasks_add (who 'bot'); "
              "say so in a line, and never add to a goal silently.")
# What a bot says beside its goals_offer card (owner 2026-11-22: "I put … on a card" read as already done).
OFFER_LINE = ("The card is on their screen and nothing is added until they tap: that is the asking. End your reply with "
              "one short line that asks, as a question (\"Want “<task>” on “<goal>”?\"). Never say you added, put or "
              "made it, and don't list it again.")
LEAD_RULE = (
    "You lead this goal: you plan it (goals_offer kind 'plan': milestones and the first week's tasks, each with who). "
    "Before a plan, look first for what they already have for it (their notes and files, memory, earlier tries, a "
    "course or method they follow) and build on that. Then ask at most three questions on ONE question card, each "
    "with choices, skipping any your looking answered, in this order: what done looks like (a number or a date), what "
    "they already have or want to follow, how much time a week. Then offer the plan with name (when the goal's name "
    "is a sentence), why in their words, measure or due when they gave one, and built_on (what the plan rests on). A "
    "goal with a plan but no number, or a sentence for a name, can be offered just those, when they ask. You also "
    "keep where it stands current (goals_update stands; page for what worked and what didn't) and run its weekly "
    "check-in. Your own tasks go under it with tasks_add (goal, who 'bot'); anything for them, any milestone, any date "
    "is an offer they tap. Never tick their task, a milestone or the goal: say it looks done (goals_update "
    "looks_done). Set the number only if you are its source. Research and drafts only: never send, book, buy, pay, "
    "post or delete for this goal. When they ask for a plan, offer it; then one short line, and stop.")
HELPER_RULE = (
    "You help on this goal: you take tasks, not the plan. Your own tasks go under it with tasks_add (goal, who "
    "'bot'); tasks for them you offer with goals_offer (kind 'tasks'). Its lead plans it and runs its check-in. "
    "Never tick their task, a milestone or the goal. Research and drafts only.")


def status() -> dict:
    """``/status`` (0.9.66): bots on a goal, and the Goal Page (GET /goals/{id}/page). ``goal_setup``: a plan offer
    may carry ``setup`` and ``built_on``, and POST /goals takes lead, helpers, adds and review in the same call (an
    add-on without it drops them, so the app sends them in a PATCH after)."""
    return {"goal_bots": True, "goal_pages": True, "goal_setup": True}


def _now_iso(store) -> str:
    return tasks._iso(store.clock)


def _read_goals(db) -> list[dict]:
    return [tasks._row_to_goal(r) for r in db.execute("select id, rev, position, body from goals order by position, id")]


def _under(db, goal_id: str) -> list[dict]:
    """The tasks under a goal, in their own order (``position``)."""
    rows = db.execute(f"select {','.join(tasks.COLUMNS)} from tasks where goal=? order by position, created_at",
                      (goal_id,)).fetchall()
    return [tasks._row_to_task(r) for r in rows]


def _save(store, db, goal: dict, now: str) -> dict:
    goal = dict(goal, rev=goal["rev"] + 1, updated_at=now, log=goal["log"][-tasks.LOG_CAP:])
    store._save_goal(db, goal)
    return goal


# ---------------------------------------------------------------------------
# Progress, pace and the weekly slot: numbers, never words (the app says them)
# ---------------------------------------------------------------------------

def progress(goal: dict, under: list[dict]) -> tuple[float | None, str | None]:
    """Goals step 1's ring: the number when the goal has one (a target below the start counts down), else
    milestones done, else tasks done; ``(None, None)`` for "No plan yet"."""
    measure = goal.get("measure")
    if measure and measure.get("target") != measure.get("start"):
        share = (measure["now"] - measure["start"]) / (measure["target"] - measure["start"])
        return min(max(share, 0.0), 1.0), "measure"
    if goal.get("milestones"):
        return sum(1 for m in goal["milestones"] if m["done"]) / len(goal["milestones"]), "milestones"
    if under:
        return sum(1 for t in under if t["status"] == "done") / len(under), "tasks"
    return None, None


def _day(iso) -> date | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().date()
    except ValueError:
        try:
            return date.fromisoformat(str(iso)[:10])
        except ValueError:
            return None


def pace(goal: dict, under: list[dict], now: float) -> dict:
    """``{progress, basis, days_total, days_gone, days_left, even}``: ``even`` is where an even pace would be today
    (0…1), only with a date."""
    share, basis = progress(goal, under)
    out = {"progress": None if share is None else round(share, 3), "basis": basis}
    due, made, today = _day(goal.get("due")) if goal.get("due") else None, _day(goal.get("created_at")), \
        datetime.fromtimestamp(now).date()
    if due and made:
        total, gone = (due - made).days, (today - made).days
        out.update(days_total=total, days_gone=gone, days_left=(due - today).days,
                   even=round(min(max(gone / total, 0.0), 1.0), 3) if total > 0 else 1.0)
    return out


def _slot(review: dict, now: float, weeks: int = 0) -> float:
    """This week's (Monday-based) check-in moment in the gateway's own time, ``weeks`` on from it."""
    today = datetime.fromtimestamp(now).date()
    day = today - timedelta(days=today.weekday()) + timedelta(days=review["day"] + 7 * weeks)
    hour, minute = (int(p) for p in review["time"].split(":"))
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


def last_slot(goal: dict, now: float) -> float | None:
    """The latest check-in moment at or before ``now``."""
    if not goal.get("review"):
        return None
    slot = _slot(goal["review"], now)
    return slot if slot <= now else _slot(goal["review"], now, -1)


def _looked_at(goal: dict) -> float:
    """Since when the goal needs no check-in: its last one, or (a new goal) when it was made."""
    return max(tasks._seconds((goal.get("checkin") or {}).get("at") or ""), tasks._seconds(goal.get("created_at") or ""))


def due_for_review(goal: dict, now: float) -> bool:
    """Its slot has passed this week and its last check-in is older than that slot (a goal made after the slot waits
    for next week's)."""
    slot = last_slot(goal, now)
    return (goal.get("status") == "active" and bool(goal.get("lead")) and slot is not None
            and _looked_at(goal) < slot)


def next_slot(goal: dict, now: float) -> float | None:
    """When the goal is next due its check-in: ``now`` when it is due already."""
    if goal.get("status") != "active" or not goal.get("lead") or not goal.get("review"):
        return None
    if due_for_review(goal, now):
        return now
    slot = _slot(goal["review"], now)
    return slot if slot > now else _slot(goal["review"], now, 1)


# ---------------------------------------------------------------------------
# What a bot reads of a goal (goals_list, goals_next_review, the goal chat's brief)
# ---------------------------------------------------------------------------

def _held(offers: list[dict], goal_id: str) -> list[dict]:
    return [{"id": o["id"], "bot": o.get("bot"), "at": o.get("at"), "tasks": o.get("tasks") or []}
            for o in offers if o.get("goal") == goal_id and o.get("held") and o.get("state") == "new"]


def brief(goal: dict, bot: str, under: list[dict], offers: list[dict], now: float) -> dict:
    """The whole goal, compact, for its lead or a helper: milestones, the number and its source, pace facts as
    numbers, the open tasks under it with ids, where it stands, and (for the lead) the offers held for it."""
    role = tasks.goal_role(goal, bot)
    out = {"id": goal["id"], "title": goal["title"], "why": goal["why"], "status": goal["status"],
           "you_lead": role == "lead", "you_help": role == "helper", "lead": goal["lead"], "helpers": goal["helpers"],
           "adds": goal["adds"], "review": goal["review"]}
    if goal.get("due"):
        out["due"] = goal["due"]
    if goal["milestones"]:
        out["milestones"] = [{"id": m["id"], "title": m["title"], "done": m["done"], **({"due": m["due"]} if m.get("due") else {})}
                             for m in goal["milestones"]]
    if goal.get("measure"):
        m = goal["measure"]
        out["measure"] = {k: m.get(k) for k in ("unit", "start", "target", "now", "at", "source", "checked_at")}
    out["pace"] = pace(goal, under, now)
    open_tasks = [t for t in under if t["status"] != "done"]
    out["open_tasks"] = [{k: v for k, v in (("id", t["id"]), ("title", t["title"]), ("who", t["who"]), ("bot", t["bot"]),
                                             ("status", t["status"]), ("due", t.get("due")),
                                             ("milestone", t.get("milestone"))) if v}
                         for t in open_tasks[:BRIEF_TASKS]]
    out["tasks_done"] = len(under) - len(open_tasks)
    if goal.get("stands"):
        out["stands"] = goal["stands"]
    if goal.get("offer"):
        out["looks_done"] = goal["offer"]
    if role == "lead":
        held = _held(offers, goal["id"])
        if held:
            out["held_offers"] = held
    return out


def goals_list(store, bot: str) -> dict:
    """``goals_list``: the goals this bot leads or helps, whole, and the other active goals it may know of by name and
    why only."""
    now = store.clock()
    with closing(store._db()) as db:
        goals, offers = _read_goals(db), store._read_offers(db)
        mine = [brief(g, bot, _under(db, g["id"]), offers, now) for g in goals if tasks.goal_role(g, bot)]
    others = [{"id": g["id"], "title": g["title"], "why": g["why"]} for g in goals
              if not tasks.goal_role(g, bot) and tasks.goal_seen(g, bot)]
    out = {"goals": mine, "others": others}
    if not mine and not others:
        out["note"] = "They have no goals you can see. When they say they want to reach something, offer it as a goal."
    return out


# ---------------------------------------------------------------------------
# Offers: the one way a bot proposes (rules 2 and 4). Answered by the person at POST /tasks/offers/{id}.
# ---------------------------------------------------------------------------

def _clean_offer_milestones(value) -> list[dict]:
    out = []
    for item in (value if isinstance(value, list) else [])[:tasks.MILESTONES_CAP]:
        title = tasks._text(item.get("title") if isinstance(item, dict) else item, tasks.MILESTONE_TITLE_MAX)
        if title and title.casefold() not in {m["title"].casefold() for m in out}:
            due = tasks._clean_start(item.get("due")) if isinstance(item, dict) else None
            out.append({"title": title, **({"due": due} if due else {})})
    return out


def _clean_offer_tasks(items, goal: dict | None, bot: str, offer_milestones: list[dict], under: list[dict]):
    """(the tasks, the ones already under the goal). A bot's task goes to its lead or a helper (else to the lead);
    a milestone that names none of the goal's (or the offer's own) is dropped."""
    team = ([goal["lead"]] + goal["helpers"]) if goal else [bot]
    team = [b for b in team if b]
    fresh, already = [], []
    open_under = [t for t in under if t["status"] != "done"]
    for item in (items if isinstance(items, list) else [])[:tasks.OFFER_TASKS_CAP]:
        if not isinstance(item, dict):
            continue
        title = tasks._text(item.get("title"), tasks.TITLE_MAX)
        if not title:
            continue
        twin = next((t for t in open_under if tasks.same_task(t["title"], title)), None)
        if twin is not None:
            already.append({"id": twin["id"], "title": twin["title"], "status": twin["status"]})
            continue
        if any(tasks.same_task(f["title"], title) for f in fresh):
            continue
        one = {"title": title, "who": "bot" if item.get("who") == "bot" else "me"}
        if one["who"] == "bot":
            wanted = tasks._text(item.get("bot"), 64) or bot
            one["bot"] = wanted if wanted in team or not team else team[0]
        if tasks._text(item.get("due"), tasks.DUE_MAX):
            one["due"] = tasks._text(item.get("due"), tasks.DUE_MAX)
        notes = tasks._lines(item.get("notes"), tasks.NOTES_MAX)
        if notes:
            one["notes"] = notes
        attached = tasks._clean_made(item.get("made"))[:OFFER_TASK_MADE_CAP]
        if attached:   # what the task points at (their notes, a page, a site), there on the task once they accept
            one["made"] = attached
        wanted = tasks._text(item.get("milestone"), tasks.MILESTONE_TITLE_MAX)
        if wanted and goal:
            found = next((m["id"] for m in goal["milestones"] if m["id"] == wanted), None) or next(
                (m["id"] for m in goal["milestones"] if m["title"].casefold() == wanted.casefold()), None)
            if found:
                one["milestone"] = found
        if wanted and "milestone" not in one and any(m["title"].casefold() == wanted.casefold() for m in offer_milestones):
            one["milestone"] = next(m["title"] for m in offer_milestones if m["title"].casefold() == wanted.casefold())
        fresh.append(one)
    return fresh, already


def _clean_offer_measure(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    start, target = tasks._number(value.get("start", 0)), tasks._number(value.get("target"))
    if start is None or target is None or start == target:
        raise tasks.TaskError(400, "A goal's number needs a target that differs from where it starts: send "
                                   "measure {unit, start, target}, or leave measure out.")
    return {"unit": tasks._text(value.get("unit"), tasks.UNIT_MAX), "start": start, "target": target}


def _setup_kept(setup: dict, goal: dict, goals: list[dict]) -> tuple[dict, list[str]]:
    """(the set-up fields a plan may set on ``goal``, why each other one was dropped). ``name`` only when it differs
    from the goal's name and no other active or paused goal has it; ``why``, ``due`` and ``measure`` only when the
    goal has none. The same rules hold when the person taps Add (the goal may have changed since)."""
    kept, dropped = {}, []
    name = setup.get("name")
    if name:
        if name.casefold() == goal["title"].casefold():
            dropped.append("name: it is the goal's name already")
        elif any(g["id"] != goal["id"] and g["status"] in ("active", "paused") and g["title"].casefold() == name.casefold()
                 for g in goals):
            dropped.append(f"name: another goal is called “{name}”")
        else:
            kept["name"] = name
    for key, words in (("why", "why"), ("due", "date"), ("measure", "number")):
        if setup.get(key):
            if goal.get(key):
                dropped.append(f"{key}: the goal has a {words} already")
            else:
                kept[key] = setup[key]
    return kept, dropped


def _clean_offer_setup(args: dict, goal: dict, goals: list[dict]) -> tuple[dict, list[str]]:
    """A plan offer's set-up fields, cleaned (``name`` as a goal's title, ``why`` one line, ``due`` a day,
    ``measure`` as a goal offer's), then kept by ``_setup_kept``'s rules. A number is checked only when it would be
    kept."""
    given = {"name": tasks._text(args.get("name"), tasks.TITLE_MAX),
             "why": tasks._text(args.get("why"), tasks.OFFER_WHY_MAX),
             "due": tasks._clean_start(args.get("due"))}
    if isinstance(args.get("measure"), dict) and not goal.get("measure"):
        given["measure"] = _clean_offer_measure(args["measure"])
    elif isinstance(args.get("measure"), dict):
        given["measure"] = True   # dropped below: the goal has a number already
    return _setup_kept({k: v for k, v in given.items() if v}, goal, goals)


def _clean_built_on(value) -> list[str]:
    """What a plan rests on: up to three short lines (their answers, what the bot found), each one line of 90
    characters at most, each once."""
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    out = []
    for item in items:
        line = tasks._text(item, BUILT_ON_MAX)
        if line and line.casefold() not in {o.casefold() for o in out}:
            out.append(line)
        if len(out) == BUILT_ON_CAP:
            break
    return out


def _slot_words(at: float) -> str:
    moment = datetime.fromtimestamp(at)
    return f"{moment.strftime('%a %b')} {moment.day}"


def offer(store, bot: str, session_id: str, kind: str, args: dict, with_person: bool = True) -> dict:
    """``goals_offer``: a goal to make, a plan or a check-in for one the bot leads, or tasks for a goal. Nothing
    changes on the goal until the person taps, except a check-in's stamp (``goal.checkin``). A ``tasks`` offer from a
    run with no person in the chat (a scheduled job) is held for the lead's next check-in; in a chat with the
    person it is a card at once, whichever bot makes it. A plan may also carry set-up fields for the goal (``setup``:
    name, why, due, measure, each kept by ``_setup_kept``'s rules) and ``built_on``, the lines it rests on; a plan of
    set-up fields alone is a plan. Returns the offer as the app sees it, with ``already`` for tasks under the goal
    already and ``setup_dropped`` for the set-up fields it could not keep, and why (neither is stored)."""
    if not store.goals_on():
        raise tasks.TaskError(403, "Goals is turned off, so there is nothing to offer: help with it in the chat instead.")
    if kind not in KINDS:
        raise tasks.TaskError(400, "kind is 'goal' (a new goal), 'plan' or 'checkin' (a goal you lead) or 'tasks'.")
    args = args if isinstance(args, dict) else {}
    now = store.clock()
    stamp = _now_iso(store)
    with closing(store._write_db()) as db, db:
        goals, offers = _read_goals(db), store._read_offers(db)
        goal, role = None, None
        if kind == "goal":
            title = tasks._text(args.get("title"), tasks.TITLE_MAX)
            if not title:
                raise tasks.TaskError(400, "A goal offer needs the goal's name in title, a few words.")
            if any(g["title"].casefold() == title.casefold() and g["status"] in ("active", "paused") for g in goals):
                raise tasks.TaskError(400, f"“{title}” is one of their goals already: offer tasks for it "
                                           "(kind 'tasks', goal) instead.")
            if any(o.get("kind") == "goal" and o.get("state") == "declined" and (o.get("title") or "").casefold()
                   == title.casefold() for o in offers):
                raise tasks.TaskError(403, f"They said not now to “{title}” as a goal. Don't offer it again "
                                           "unless they ask; help with it in the chat instead.")
        else:
            goal = tasks.find_goal(goals, args.get("goal"), bot)
            if goal is None:
                raise tasks.TaskError(404, "No goal by that id or name that you can see: goals_list names them.")
            title, role = goal["title"], tasks.goal_role(goal, bot)
            if kind in ("plan", "checkin") and role != "lead":
                raise tasks.TaskError(403, f"Only the lead of “{title}” offers its {kind}, and that holds for "
                                           "every goal you don't lead: offer tasks for it instead (kind 'tasks'), and "
                                           "its lead folds them in.")
            if goal["status"] != "active":
                raise tasks.TaskError(400, f"“{title}” isn't active (it is {goal['status'].replace('_', ' ')}): "
                                           "ask them in the chat whether to pick it back up instead.")
            if kind == "plan" and not args.get("asked") and any(
                    o.get("kind") == "plan" and o.get("goal") == goal["id"] and o.get("state") == "declined"
                    for o in offers):
                raise tasks.TaskError(403, f"They kept “{title}” without a plan. Offer one again only when "
                                           "they ask you to (then with asked=true); help in the chat until then.")
            if kind == "checkin":
                slot, last = last_slot(goal, now), goal.get("checkin") or {}
                if slot is not None and last.get("state") == "declined" and tasks._seconds(last.get("at") or "") >= slot:
                    upcoming = _slot(goal["review"], now, 1) if _slot(goal["review"], now) <= now else _slot(goal["review"], now)
                    raise tasks.TaskError(403, f"They said not now to this week's check-in for “{title}”. The "
                                               f"next one is {_slot_words(upcoming)}; until then, say anything new in "
                                               "the chat instead.")
        milestones = _clean_offer_milestones(args.get("milestones")) if kind in ("goal", "plan") else []
        under = _under(db, goal["id"]) if goal else []
        fresh, already = _clean_offer_tasks(args.get("tasks"), goal, bot, milestones, under)
        summary = tasks._lines(args.get("summary"), SUMMARY_MAX) if kind == "checkin" else ""
        if kind == "checkin" and not summary:
            raise tasks.TaskError(400, "A check-in needs its summary: two sentences on what moved since the last one.")
        setup, dropped = _clean_offer_setup(args, goal, goals) if kind == "plan" else ({}, [])
        built_on = _clean_built_on(args.get("built_on")) if kind == "plan" else []
        if kind == "plan" and not milestones and not fresh and not setup:
            raise tasks.TaskError(400, "A plan needs milestones, tasks or set-up fields (name, why, due, measure): any "
                                       "one is enough. Send the steps to the goal and the first week's tasks, each "
                                       "with who, or only the set-up fields the goal lacks."
                                  + (f" Dropped: {'; '.join(dropped)}." if dropped else ""))
        if kind == "tasks" and not fresh:
            raise tasks.TaskError(400, "Nothing to offer: every task is under the goal already, or none was given."
                                  + (f" Already there: {json.dumps(already, ensure_ascii=False)}" if already else ""))
        # Owner-approved board 5: a bot that isn't on the goal offers tasks for it as a card, in a chat with the
        # person. Only an offer nobody is there to see (a scheduled run) waits for the lead.
        held = kind == "tasks" and not with_person
        offers = [o for o in offers if not (
            o.get("state") == "new" and o.get("bot") == bot and o.get("kind") == kind
            and (o.get("goal") == goal["id"] if goal else (o.get("title") or "").casefold() == title.casefold()))]
        made = {"id": tasks.new_id(), "kind": kind, "bot": bot, "session_id": str(session_id or ""), "at": stamp,
                "goal": goal["id"] if goal else None, "title": title, "tasks": fresh, "state": "new"}
        if kind == "goal":
            made.update(why=tasks._text(args.get("why"), tasks.OFFER_WHY_MAX), due=tasks._clean_start(args.get("due")),
                        measure=_clean_offer_measure(args.get("measure")))
        if kind in ("goal", "plan"):
            made["milestones"] = milestones
        if setup:
            made["setup"] = setup
        if built_on:
            made["built_on"] = built_on
        if kind == "checkin":
            made["summary"] = summary
            for o in offers:   # the lead was handed these at this check-in: folded in, or dropped
                if o.get("goal") == goal["id"] and o.get("held") and o.get("state") == "new":
                    o["state"], o["answered_at"] = FOLDED, stamp
            fresh_goal = _save(store, db, dict(goal, checkin={"at": stamp, "offer": made["id"], "state": "offered"}), stamp)
            goal = fresh_goal
        if held:
            made["held"] = True
        offers.append(made)
        store._save_offers(db, offers)
        store._bump_rev(db)
        shown = store._show_offer(db, made)
    if already:
        shown["already"] = already
    if dropped:
        shown["setup_dropped"] = dropped
    return shown


def _goal_name_taken(db, title: str) -> bool:
    return any(g["title"].casefold() == title.casefold() and g["status"] in ("active", "paused") for g in _read_goals(db))


def _setup_mark(key: str, value):
    """What undo compares to tell "changed since": a number by its unit, start, target and where it is."""
    if key == "measure" and isinstance(value, dict):
        return {k: value.get(k) for k in ("unit", "start", "target", "now")}
    return value


def _apply_setup(db, goal: dict, setup: dict, stamp: str) -> tuple[dict, dict, dict]:
    """Accepting a plan's set-up fields: (the goal's changed keys and their new values, what each had before, the
    marks undo compares). ``name`` becomes the title; when the goal has no why and the plan brings none, the old
    title (their own words) becomes the why. A number starts where it starts, as a new goal's does."""
    kept, _ = _setup_kept(setup, goal, _read_goals(db))
    changes = {}
    if kept.get("name"):
        changes["title"] = tasks._text(kept["name"], tasks.TITLE_MAX)
        if not goal["why"] and not kept.get("why"):
            changes["why"] = tasks._text(goal["title"], tasks.GOAL_WHY_MAX)
    if kept.get("why"):
        changes["why"] = tasks._text(kept["why"], tasks.GOAL_WHY_MAX)
    if kept.get("due"):
        changes["due"] = tasks._clean_start(kept["due"])
    if kept.get("measure"):
        changes["measure"] = tasks._clean_measure(kept["measure"], None, stamp)
    changes = {k: v for k, v in changes.items() if v}
    return (changes, {k: goal.get(k) for k in changes}, {k: _setup_mark(k, v) for k, v in changes.items()})


def _undo_setup(db, goal: dict, made: dict) -> dict:
    """Undo's half for set-up fields: each key back to what it was, unless the person changed it since (it no
    longer reads as accepting left it) or, for the name, another goal has taken the old one."""
    changes, set_to = {}, made.get("setup_to") or {}
    for key, old in (made.get("setup") or {}).items():
        if key not in set_to or _setup_mark(key, goal.get(key)) != set_to[key]:
            continue   # theirs now
        if key == "title" and _goal_name_taken(db, old):
            continue
        changes[key] = old
    return changes


def answer_offer(store, offer_id: str, action: str, plan: bool = True, in_goal: bool = True,
                 make_list: bool = True) -> dict:
    """``POST /tasks/offers/{id}`` for every offer: a list offer (no ``kind``) goes to the store as before. On a goal
    offer, ``accept`` makes the goal (led by the bot that offered it; ``plan: false`` is "Keep the Goal, No Plan"),
    adds a plan's milestones and sets its set-up fields in the same write (``made.setup``: what each changed key
    had before), or adds the tasks under the goal (``in_goal: false``: "Just Tasks", with no goal); ``decline``
    changes nothing; ``undo`` takes back what accepting made, except what the person has changed since.
    ``{rev, offer, sections, goals}``."""
    with closing(store._db()) as db:
        found = next((o for o in store._read_offers(db) if o["id"] == offer_id), None)
    if found is None:
        raise tasks.TaskError(404, "No such offer.")
    if found.get("kind", "list") == "list":
        return {**store.answer_offer(offer_id, action, make_list), "goals": store.list_goals()}
    if action not in ("accept", "decline", "undo"):
        raise tasks.TaskError(400, "A goal's offer takes accept, decline or undo.")
    stamp = _now_iso(store)
    with closing(store._write_db()) as db, db:
        offers = store._read_offers(db)
        offer = next((o for o in offers if o["id"] == offer_id), None)
        if offer is None:
            raise tasks.TaskError(404, "No such offer.")
        kind, state = offer["kind"], offer.get("state")
        if kind == "plan" and action == "accept" and not plan:
            action = "decline"   # "Keep the Goal, No Plan" on a plan declines the plan
        goal = store._read_goal(db, offer.get("goal")) if offer.get("goal") else None

        def checkin_state(value: str) -> None:
            nonlocal goal
            if kind == "checkin" and goal and (goal.get("checkin") or {}).get("offer") == offer["id"]:
                goal = _save(store, db, dict(goal, checkin=dict(goal["checkin"], state=value)), stamp)

        if action == "decline":
            if state != "new":
                raise tasks.TaskError(409, "That offer was already answered.")
            offer["state"], offer["answered_at"] = "declined", stamp
            checkin_state("declined")
        elif action == "accept":
            if state != "new":
                raise tasks.TaskError(409, "That offer was already answered.")
            made: dict = {"added": [], "at": stamp}
            if kind == "goal":
                if _goal_name_taken(db, offer["title"]):
                    raise tasks.TaskError(409, "They have a goal by that name already.")
                goal = store._new_goal(db, {"title": offer["title"], "why": offer.get("why"), "due": offer.get("due"),
                                            "measure": offer.get("measure"), "lead": offer["bot"],
                                            "milestones": offer.get("milestones") if plan else []}, by="you")
                made["goal"] = goal["id"]
                made["at"] = goal["updated_at"]   # the goal's own stamp: undo's "changed since" starts after it
                made["milestones"] = [m["id"] for m in goal["milestones"]]
                offer["plan"] = bool(plan)
            elif kind == "plan":
                if goal is None:
                    raise tasks.TaskError(409, "That goal is gone.")
                have = {m["title"].casefold() for m in goal["milestones"]}
                adding = [m for m in offer.get("milestones") or [] if m["title"].casefold() not in have]
                room = tasks.MILESTONES_CAP - len(goal["milestones"])
                new = tasks._clean_milestones(adding[:max(room, 0)], now=stamp)
                changes, before, after = _apply_setup(db, goal, offer.get("setup") or {}, stamp)
                if new or changes:   # one write, one line, also when only set-up fields changed
                    goal = _save(store, db, dict(goal, milestones=goal["milestones"] + new, **changes,
                                                 log=goal["log"] + [{"at": stamp, "by": "you", "text": "Plan added"}]), stamp)
                made["milestones"] = [m["id"] for m in new]
                if before:
                    made["setup"], made["setup_to"] = before, after
            in_it = goal is not None and (in_goal or kind in ("goal", "plan"))
            offer["in_goal"] = bool(in_it)
            if kind != "goal" or plan:
                titles = {m["title"].casefold(): m["id"] for m in (goal or {}).get("milestones") or []}
                ids = {m["id"] for m in (goal or {}).get("milestones") or []}
                source = {"kind": "chat", "profile": offer["bot"], "session_id": offer.get("session_id") or "",
                          "offer": offer["id"]}
                at = store.clock()
                for index, item in enumerate(offer.get("tasks") or []):
                    milestone = item.get("milestone")
                    milestone = milestone if milestone in ids else titles.get(str(milestone or "").casefold())
                    fields = {"title": item["title"], "who": item.get("who", "me"), "bot": item.get("bot"),
                              "due": item.get("due"), "notes": item.get("notes"), "position": at + index / 1000,
                              "goal": goal["id"] if in_it else None, "milestone": milestone if in_it else None}
                    made["added"].append(store.create(fields, created_by=offer["bot"], source=source, db=db,
                                                      made=item.get("made"))["id"])
            offer["made"], offer["state"], offer["answered_at"] = made, "accepted", stamp
            checkin_state("accepted")
        else:   # undo
            if state != "accepted":
                raise tasks.TaskError(409, "There is nothing to undo.")
            made, kept = offer.get("made") or {}, []
            for task_id in made.get("added") or []:
                row = db.execute(f"select {','.join(tasks.COLUMNS)} from tasks where id=?", (task_id,)).fetchone()
                if row is None:
                    continue
                task = tasks._row_to_task(row)
                if task["edited"] or task["status"] == "done":
                    kept.append(task_id)   # theirs now: a task they edited or ticked stays
                    continue
                db.execute("delete from tasks where id=?", (task_id,))
                db.execute("delete from task_pages where id=?", (task_id,))
            target = store._read_goal(db, made.get("goal") or offer.get("goal"))
            # A goal it made is theirs once anyone changed it after the accept (an edit, a tick, a lead's words).
            touched = target is not None and (target.get("updated_at") or "") > (made.get("at") or "")
            changes = {}
            if target is not None and made.get("milestones"):
                busy = {r[0] for r in db.execute("select milestone from tasks where goal=? and milestone is not null",
                                                 (target["id"],))}
                gone = {m for m in made["milestones"]}
                keep = [m for m in target["milestones"] if m["id"] not in gone or m["done"] or m["id"] in busy]
                if len(keep) != len(target["milestones"]):
                    changes["milestones"] = keep
            if target is not None and made.get("setup"):
                changes.update(_undo_setup(db, target, made))
            if changes:   # the milestones and the set-up fields back in one write
                target = _save(store, db, dict(target, **changes), stamp)
            if made.get("goal") and target is not None:
                if not touched and not _under(db, target["id"]) and not any(m["done"] for m in target["milestones"]):
                    db.execute("delete from goals where id=?", (target["id"],))
                    db.execute("delete from task_pages where id=?", (PAGE_KEY + target["id"],))
                else:
                    kept.append(target["id"])
            offer["state"], offer["answered_at"], offer["made"] = "undone", stamp, {}
            if kept:
                offer["kept"] = kept
            checkin_state("none")
        store._save_offers(db, offers)
        rev = store._bump_rev(db)
        shown = store._show_offer(db, offer)
    return {"rev": rev, "offer": shown, "sections": store.get_sections(), "goals": store.list_goals()}


def settle(store, answer: dict, home_for: Callable | None = None) -> None:
    """After an answer: each bot whose own task was just added starts it the way a bot's own task does (its Task prep
    job), and the offering bot's check-in job follows a goal it now leads (or no longer does). Best effort."""
    home_for = home_for or tasks.bot_home
    shown = answer.get("offer") or {}
    if shown.get("kind", "list") == "list":
        return
    # Added just now, or taken back by an undo: either way each bot's own-task jobs follow its tasks.
    bots = {t["bot"] for t in shown.get("tasks") or [] if t.get("who") == "bot" and t.get("bot")}
    for bot in sorted(bots):
        tasks.reconcile_prep_job(store, bot, home_for(bot))
    if shown.get("kind") == "goal" and shown.get("bot"):
        reconcile_checkin_job(store, shown["bot"], home_for(shown["bot"]))


# ---------------------------------------------------------------------------
# goals_update: where it stands, the page and looks-done (the lead); the number (its source bot only)
# ---------------------------------------------------------------------------

def _ticked_since(db, goal: dict, what: str, since: str) -> bool:
    """Something under ``what`` (a milestone id, or "goal") was ticked after ``since``: a Not Yet no longer holds."""
    for task in _under(db, goal["id"]):
        if task["status"] == "done" and (task.get("done_at") or "") > since and (what == "goal" or task.get("milestone") == what):
            return True
    return what == "goal" and any((m.get("done_at") or "") > since for m in goal["milestones"])


def update(store, bot: str, goal_value, *, stands=None, measure_now=None, page=None, looks_done=None, why=None,
           checked=None) -> dict:
    """``goals_update``. Lead only, except ``measure_now`` and ``checked`` (the number's source bot only). Returns
    ``{goal: <brief>}`` and, when part of the page was left out, ``page_notes``."""
    if not store.goals_on():
        raise tasks.TaskError(403, "Goals is turned off: there is nothing to update; help in the chat instead.")
    leading = stands is not None or page is not None or looks_done is not None
    number = measure_now is not None or checked is True
    if not leading and not number:
        raise tasks.TaskError(400, "Nothing to update: send stands, page, looks_done, measure_now or checked.")
    stamp, page_notes = _now_iso(store), []
    with closing(store._write_db()) as db, db:
        every = _read_goals(db)
        goal = tasks.find_goal(every, goal_value, bot)
        if goal is None and number and not leading:   # the number's source reads it even on a goal hidden from it
            reads = [g for g in every if isinstance((g.get("measure") or {}).get("source"), dict)
                     and g["measure"]["source"].get("bot") == bot]
            goal = tasks.find_goal([dict(g, known_to_all=True, status="active") for g in reads], goal_value, bot)
            goal = next((g for g in reads if goal and g["id"] == goal["id"]), None)
        if goal is None:
            raise tasks.TaskError(404, "No goal by that id or name that you can see: goals_list names them.")
        role, title = tasks.goal_role(goal, bot), goal["title"]
        if leading and role != "lead":
            raise tasks.TaskError(403, f"Only the lead of “{title}” writes where it stands, its page or a "
                                       "looks-done, and that holds for every goal you don't lead: tell its lead what "
                                       "you found instead (message_agent), or offer tasks (goals_offer kind 'tasks').")
        updated = dict(goal, log=list(goal["log"]))
        if number:
            measure = goal.get("measure")
            if not measure:
                raise tasks.TaskError(400, f"“{title}” has no number to check: leave it; they add one on the "
                                           "goal's page if they want one.")
            source = measure.get("source") or "you"
            if not isinstance(source, dict) or source.get("bot") != bot:
                whose = "theirs to update" if source == "you" else f"read by {source.get('bot')}"
                raise tasks.TaskError(403, f"The number on “{title}” is {whose}, so you never set it: tell "
                                           "them the value you saw in the chat instead, and they tap Update.")
            if measure_now is not None:
                value = tasks._number(measure_now)
                if value is None:
                    raise tasks.TaskError(400, "measure_now is the number you read, as a number (340, not \"340 testers\").")
                measure = tasks._clean_measure({"now": value}, measure, stamp)
                if measure["now"] != goal["measure"]["now"]:
                    unit = f' {measure["unit"]}' if measure["unit"] else ""
                    updated["log"].append({"at": stamp, "by": bot, "text": f'Now {measure["now"]:g}{unit}'})
            updated["measure"] = dict(measure, checked_at=stamp)
        if stands is not None:
            stands = stands if isinstance(stands, dict) else {"text": stands}
            text = tasks._lines(stands.get("text"), STANDS_TEXT_MAX)
            if not text:
                raise tasks.TaskError(400, "stands needs text: where the goal stands, in a sentence or two.")
            updated["stands"] = {"by": bot, "at": stamp, "text": text, "next": tasks._text(stands.get("next"), STANDS_NEXT_MAX)}
        if looks_done is not None:
            what = "goal" if looks_done == "goal" else next(
                (m["id"] for m in goal["milestones"] if looks_done in (m["id"],) or
                 (isinstance(looks_done, str) and m["title"].casefold() == looks_done.casefold())), None)
            if what is None:
                raise tasks.TaskError(400, "looks_done is one of the goal's milestone ids (goals_list), or \"goal\".")
            if what != "goal" and next(m for m in goal["milestones"] if m["id"] == what)["done"]:
                raise tasks.TaskError(400, "That milestone is ticked already: nothing to offer.")
            before = goal.get("offer") or {}
            if before.get("what") == what and before.get("kept") and not _ticked_since(db, goal, what, before.get("kept_at") or ""):
                raise tasks.TaskError(403, "They said not yet to that. Don't offer it again until something under it "
                                           "moves; say what changed in the chat instead.")
            default = "Every milestone looks done." if what == "goal" else "Its tasks look done."
            updated["offer"] = {"kind": "done", "what": what, "by": bot,
                                "text": tasks._text(why, LOOKS_WHY_MAX) or default, "at": stamp}
        if page is not None:
            row = db.execute("select body from task_pages where id=?", (PAGE_KEY + goal["id"],)).fetchone()
            try:
                old = json.loads(row[0]) if row else {}
            except ValueError:
                old = {}
            merged, page_notes = tasks.merge_page(old, page)
            db.execute("insert into task_pages (id, body) values (?, ?) on conflict(id) do update set body=excluded.body",
                       (PAGE_KEY + goal["id"], json.dumps(merged, ensure_ascii=False)))
            updated["page_at"] = stamp
        saved = _save(store, db, updated, stamp)
        store._bump_rev(db)
        if role is None:   # the number's source alone (rule 3): its number back, nothing else of the goal
            shown = {"id": saved["id"], "title": saved["title"], "measure": {
                k: saved["measure"].get(k) for k in ("unit", "start", "target", "now", "at", "source", "checked_at")}}
        else:
            shown = brief(saved, bot, _under(db, saved["id"]), store._read_offers(db), store.clock())
    out = {"goal": shown}
    if page_notes:
        out["page_notes"] = page_notes
    return out


def get_page(store, goal_id: str) -> dict:
    """What the lead wrote on the Goal Page (``merge_page``'s shape), or ``{}``."""
    return store.get_page(PAGE_KEY + str(goal_id or ""))


# ---------------------------------------------------------------------------
# goals_next_review: the check-in job's next goal (quiet hours and the daily limit are Tasks' own)
# ---------------------------------------------------------------------------

def next_review(store, bot: str) -> dict:
    """The next goal ``bot`` leads that is due its weekly look, with what moved since the last check-in, or
    ``{"stop": reason}``. Handing one out counts against Tasks' ``prep_per_day`` and stamps the goal's ``checkin``
    (state "none" until the lead offers one), so the same call never hands it out twice."""
    if not store.goals_on():
        return {"stop": "Goals is turned off."}
    settings, now, stamp = store.get_settings(), store.clock(), _now_iso(store)
    with closing(store._write_db()) as db, db:
        due = [g for g in _read_goals(db) if g["lead"] == bot and due_for_review(g, now)]
        if not due:
            return {"stop": f"no goal {bot} leads is due its check-in"}
        if store._quiet_now(settings):
            return {"stop": f"it's quiet hours ({settings['quiet_start']}–{settings['quiet_end']})"}
        row = db.execute("select count from prep_runs where day=?", (store._today(),)).fetchone()
        if (row[0] if row else 0) >= settings["prep_per_day"]:
            return {"stop": f"today's limit ({settings['prep_per_day']} runs) is reached"}
        goal = min(due, key=lambda g: last_slot(g, now))
        since = max((goal.get("checkin") or {}).get("at") or "", goal["created_at"])
        db.execute("insert into prep_runs (day, count) values (?, 1) on conflict(day) do update set count = count + 1",
                   (store._today(),))
        under, offers = _under(db, goal["id"]), store._read_offers(db)
        goal = _save(store, db, dict(goal, checkin={"at": stamp, "offer": None, "state": "none"}), stamp)
        store._bump_rev(db)
    out = brief(goal, bot, under, offers, now)
    ticked = [{"id": t["id"], "title": t["title"]} for t in under if t["status"] == "done" and (t.get("done_at") or "") > since]
    slipped = [{"id": t["id"], "title": t["title"], "due": t["due"]} for t in under
               if t["status"] != "done" and (tasks._when(t.get("due")) or now + 1) < now]
    moved = [h for h in ((goal.get("measure") or {}).get("history") or []) if (h.get("at") or "") > since]
    out["since"] = since
    out["changed"] = {"ticked": ticked, "slipped": slipped, "number": moved}
    out["how"] = CHECKIN_JOB_PROMPT
    return out


# ---------------------------------------------------------------------------
# What every bot is told (pre_llm_call)
# ---------------------------------------------------------------------------

_people: dict[str, float] = {}


def note_turn(session_id, platform: str = "", user_message=None, now: float | None = None) -> None:
    """pre_llm_call, every process: a turn the person typed (in any chat but a job's) says the person is in this
    chat, so a bot's tasks offer there is a card at once; a turn nobody typed says they aren't."""
    if not session_id:
        return
    typed = (isinstance(user_message, str) and bool(user_message.strip()) and not tasks._not_typed(user_message)
             and platform not in ("cron", ""))
    if typed:
        _people[str(session_id)] = time.time() if now is None else now
    else:
        _people.pop(str(session_id), None)
    if len(_people) > 500:
        for key in sorted(_people, key=_people.get)[:250]:
            _people.pop(key, None)


def person_in(session_id, now: float | None = None) -> bool:
    """The person typed this chat's turn (``note_turn``), and it isn't a scheduled job's run."""
    at = _people.get(str(session_id or ""))
    if at is None or ((time.time() if now is None else now) - at) > PERSON_TTL:
        return False
    return not ideas.run_job_id(session_id)


def goal_for_chat(goals: list[dict], session_id: str) -> dict | None:
    return next((g for g in goals if session_id and (g.get("chat") or {}).get("session_id") == session_id), None)


def goal_chat_line(goal: dict, bot: str, under: list[dict], offers: list[dict], now: float) -> str:
    """A goal's own chat: the goal's whole brief and the lead's (or a helper's) duties, every turn."""
    role = tasks.goal_role(goal, bot)
    duty = LEAD_RULE if role == "lead" else HELPER_RULE if role == "helper" else (
        "You aren't on this goal: point what you'd do to its lead, or offer tasks with goals_offer (kind 'tasks').")
    whole = json.dumps(brief(goal, bot, under, offers, now), ensure_ascii=False)
    return f"This chat is the Goal “{goal['title'][:80]}” (id {goal['id']}). {whole} {duty}"


def context_for(*, store, profile: str, platform: str = "", parent_session_id: str = "", user_message=None,
                conversation_history=None, session_id: str = "") -> str | None:
    """The goals note for this turn, or ``None``. In a goal's own chat: its brief and the lead's duties, every turn
    (the app's plan request is a hidden turn). Elsewhere, only in a turn the person typed in an app chat, at most
    once while it is still in the last ``RESEND_AFTER`` messages: the active goals this bot may know, by name and
    why, five at most, with the one rule."""
    if platform not in ("desktop", "tui") or parent_session_id or not isinstance(user_message, str):
        return None
    if not store.goals_on():
        return None
    now = store.clock()
    with closing(store._db()) as db:
        goals = _read_goals(db)
        chat = goal_for_chat(goals, str(session_id or ""))
        if chat is not None:
            line = goal_chat_line(chat, profile, _under(db, chat["id"]), store._read_offers(db), now)
            return f"{MARK} {tasks.today_line(now)} {line}]"
    if tasks._not_typed(user_message):
        return None
    if any(MARK in tasks._message_text(m) for m in list(conversation_history or [])[-tasks.RESEND_AFTER:]):
        return None
    known = [g for g in goals if g["status"] == "active" and tasks.goal_seen(g, profile)]
    known.sort(key=lambda g: tasks.goal_role(g, profile) is None)   # its own first
    if not known:
        return None
    said = "; ".join(f"{g['title'][:60]}" + (f" ({tasks._clip(g['why'], NOTE_WHY)})" if g["why"] else "")
                     for g in known[:NOTE_GOALS])
    lead = [g["title"][:40] for g in known if g["lead"] == profile]
    extra = f" You lead: {', '.join(lead[:NOTE_GOALS])} (goals_list has them whole)." if lead else ""
    return f"{MARK} Their goals: {said}. {GOALS_RULE}{extra}]"


# ---------------------------------------------------------------------------
# The Goal check-in job: one per lead bot, a one-shot run armed for its next due slot (tasks.own_job's rules)
# ---------------------------------------------------------------------------

def _quiet_at(settings: dict, moment: float) -> bool:
    local = time.localtime(moment)
    minutes = local.tm_hour * 60 + local.tm_min
    start, end = tasks._hhmm_minutes(settings["quiet_start"]), tasks._hhmm_minutes(settings["quiet_end"])
    if start == end:
        return False
    return start <= minutes < end if start < end else (minutes >= start or minutes < end)


def _quiet_end_after(settings: dict, moment: float) -> float:
    """The first quiet-hours end after ``moment`` (the same day's, or the next day's)."""
    hour, minute = (int(p) for p in settings["quiet_end"].split(":"))
    day = datetime.fromtimestamp(moment).date()
    for add in (0, 1):
        when = datetime(day.year, day.month, day.day, hour, minute) + timedelta(days=add)
        if when.timestamp() > moment:
            return when.timestamp()
    return moment + 86400


def run_at(store, profile: str) -> float | None:
    """When ``profile``'s check-in job should run next: its earliest due slot (a minute and a half on, when one is due
    already), moved out of quiet hours, and to tomorrow when today's limit is used up. ``None``: it leads nothing
    with a check-in, or Tasks or Goals is off."""
    if not store.goals_on():
        return None
    now, settings = store.clock(), store.get_settings()
    slots = [s for s in (next_slot(g, now) for g in store.list_goals() if g["lead"] == profile) if s is not None]
    if not slots:
        return None
    at = max(min(slots), now + 90)
    today = datetime.fromtimestamp(now).date()
    if datetime.fromtimestamp(at).date() == today and store.prep_runs_left() <= 0:
        at = datetime.combine(today + timedelta(days=1), datetime.min.time()).timestamp()   # the limit is per day
    if _quiet_at(settings, at):
        at = _quiet_end_after(settings, at)
    return at


def ensure_checkin_job(at: float, store, profile: str) -> str | None:
    """"Goal check-in": one run at ``at`` for ``profile``, in the active schedule (made, moved, or turned back on).
    Its id. Known by the id recorded when it was made, never by its name alone."""
    from cron import jobs as cron_jobs
    when = time.strftime("%Y-%m-%dT%H:%M", time.localtime(at))
    job = tasks.own_job(cron_jobs, store, "checkin", profile, CHECKIN_JOB_NAME, CHECKIN_JOB_MARK)
    if job is None:
        job = tasks.create_own_job(cron_jobs, store, "checkin", profile, CHECKIN_JOB_NAME, prompt=CHECKIN_JOB_PROMPT,
                                   schedule=when)
        log.info("hermes-hq-push: goal check-in job set for %s", when)
        return job["id"]
    updates = {}
    if (job.get("schedule") or {}).get("display") != f"once at {when.replace('T', ' ')}" or not job.get("enabled", True):
        updates.update(schedule=when, enabled=True, state="scheduled")
    if job.get("prompt") != CHECKIN_JOB_PROMPT:
        updates["prompt"] = CHECKIN_JOB_PROMPT
    if updates:
        cron_jobs.update_job(job["id"], updates)
    return job["id"]


def remove_checkin_job(store, profile: str) -> None:
    tasks.remove_own_job(store, "checkin", profile, CHECKIN_JOB_NAME, CHECKIN_JOB_MARK)


def reconcile_checkin_job(store, profile: str, home=None) -> None:
    """``profile``'s check-in job: armed for its next due slot while it leads an active goal with a check-in (and
    Tasks and Goals are on), removed otherwise. In ``home``'s schedule (``None`` = the active one). Never raises."""
    try:
        with tasks._cron_home(home):
            at = run_at(store, profile)
            if at is not None:
                ensure_checkin_job(at, store, profile)
            else:
                remove_checkin_job(store, profile)
    except Exception:
        log.debug("hermes-hq-push: goal check-in job not reconciled", exc_info=True)


def job_bots(store) -> set[str]:
    """Every bot that leads a goal, or has a check-in job of ours recorded: the ones a settings change reconciles."""
    bots = {g["lead"] for g in store.list_goals() if g["lead"]}
    with closing(store._db()) as db:
        bots |= {key[len("job:checkin:"):] for (key,) in db.execute("select key from meta where key like 'job:checkin:%'")}
    return bots


def reconcile_all(store, home_for: Callable | None = None) -> None:
    """Tasks or Goals turned on or off: every lead's check-in job follows, each in its own bot's schedule."""
    home_for = home_for or tasks.bot_home
    for bot in sorted(job_bots(store)):
        home = home_for(bot)
        if home is not None:
            reconcile_checkin_job(store, bot, home)


# ---------------------------------------------------------------------------
# The guard (pre_tool_call), rule 7: inside the Goal check-in run, nothing is sent, booked, bought, paid, posted or
# deleted. Best effort, by the tool's name (a shell command can still hide one).
# ---------------------------------------------------------------------------

OUTWARD = re.compile(r"send|post|reply|publish|tweet|book|buy|purchase|pay|checkout|order|delete|remove|cancel", re.I)
NOT_OUTWARD = frozenset({"draft_message", "message_agent", "delegate_task", "device_draft", "clarify"})


def in_checkin_run(session_id, store_for: Callable, profile: str) -> bool:
    job_id = ideas.run_job_id(session_id)
    if not job_id:
        return False
    try:
        return job_id == tasks.recorded_job_id(store_for(), "checkin", profile)
    except Exception:
        return False


def guard(tool_name: str, session_id, store_for: Callable, profile: str) -> str | None:
    """The block message for a tool that acts outside, called in this bot's Goal check-in run, else ``None``. Cheap
    for every other call: only an outward-named tool in a job's session gets as far as a lookup."""
    name = str(tool_name or "")
    if name in NOT_OUTWARD or name.startswith(("tasks_", "goals_")) or not OUTWARD.search(name):
        return None
    if not in_checkin_run(session_id, store_for, profile):
        return None
    return (f"Refused: the Goal check-in is research and drafts only, so {name} isn't used in it. Put what needs doing "
            "in your check-in's tasks (who 'me') instead, or draft it with draft_message.")


# ---------------------------------------------------------------------------
# The Goal Page (GET /goals/{id}/page): the add-on draws it, in the Task Page's look; the lead writes words only
# ---------------------------------------------------------------------------

TEMPLATE = Path(__file__).resolve().parent / "skills" / "goal-page" / "template.html"
GOAL_STATUS = {"active": ("Active", "t-working"), "paused": ("Someday", "t-plain"), "achieved": ("Achieved", "t-done"),
               "let_go": ("Let Go", "t-plain")}
_e = task_page._e


def _num(value) -> str:
    return f"{value:,g}" if isinstance(value, (int, float)) else _e(value)


def _ring(share: float | None) -> str:
    value = 0 if share is None else round(share * 100)
    return (f'<span class="ring" style="--p:{value}" role="img" aria-label="{value}%"><b>'
            f'{"" if share is None else f"{value}%"}</b></span>')


def _weekly(history: list[dict]) -> list[tuple[str, object]]:
    """The number's history, the last value of each week (Monday-based), newest first."""
    weeks: dict[str, object] = {}
    for entry in history or []:
        day = _day(entry.get("at"))
        if day is None:
            continue
        monday = (day - timedelta(days=day.weekday())).isoformat()
        weeks[monday] = entry.get("value")
    return sorted(weeks.items(), reverse=True)[:HISTORY_WEEKS]


def render_page(goal: dict, under: list[dict], page: dict | None, names: dict | None = None) -> str:
    """The whole Goal Page for ``goal`` (a tasks.Store goal), the tasks under it and what its lead wrote (``page``,
    merge_page's shape). ``names``: the app's display names by profile; a profile without one is title-cased."""
    page, names = page or {}, names or {}

    def name(profile) -> str:
        profile = profile or "default"
        return names.get(profile) or ("Hermes" if profile == "default" else profile.replace("-", " ").replace("_", " ").title())

    word, tone = GOAL_STATUS.get(goal.get("status"), GOAL_STATUS["active"])
    share, basis = progress(goal, under)
    facts = []
    if goal.get("due"):
        facts.append(f"<span>{task_page._time(goal['due'], 'By ', goal['due'])}</span>")
    if goal.get("lead"):
        team = ", ".join(name(h) for h in goal.get("helpers") or [])
        facts.append(f"<span>Led by {_e(name(goal['lead']))}{' with ' + _e(team) if team else ''}</span>")
    if goal.get("review") and goal.get("lead"):
        day = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[goal["review"]["day"]]
        facts.append(f"<span>Checks in {day}s {_e(goal['review']['time'])}</span>")
    body = [f'<header><div class="top"><span class="status {tone}">{_e(word)}</span>{_ring(share)}</div>'
            f'<h1>{_e(goal.get("title"))}</h1>'
            + (f'<p class="why">{_e(goal["why"])}</p>' if goal.get("why") else "")
            + f'<div class="facts">{"".join(facts)}</div></header>']

    measure = goal.get("measure")
    if measure:
        unit = f" {_e(measure.get('unit'))}" if measure.get("unit") else ""
        source = measure.get("source") or "you"
        said = "You update it" if source == "you" else f"{_e(name(source.get('bot')))} reads it" + (
            f": {_e(source.get('what'))}" if source.get("what") else "")
        checked = f' · {task_page._time(measure["checked_at"], "checked ")}' if measure.get("checked_at") else ""
        rows = "".join(f"<tr><td>{task_page._time(week, 'Week of ', week)}</td><td>{_num(value)}</td></tr>"
                       for week, value in _weekly(measure.get("history")))
        table = (f'<div class="scroll"><table><thead><tr><th>Week</th><th>Number</th></tr></thead><tbody>{rows}'
                 "</tbody></table></div>") if rows else ""
        body.append(f'<section class="card"><h2>The number</h2><p class="summary"><b>{_num(measure.get("now"))}</b> of '
                    f'{_num(measure.get("target"))}{unit}</p><p class="small muted">From {_num(measure.get("start"))}'
                    f' · {said}{checked}</p>{table}</section>')

    stands = goal.get("stands") or {}
    text, nxt = stands.get("text") or page.get("summary") or "", stands.get("next") or page.get("next") or ""
    stand = [f'<h2>Where it stands</h2><p class="summary">{task_page._inline(text) if text else "No word from a lead yet."}</p>']
    if share is not None:
        done_words = {"measure": "of the way", "milestones": "of the milestones", "tasks": "of the tasks"}[basis]
        stand.append(f'<div class="bar{" full" if share >= 1 else ""}"><i style="width:{round(share * 100)}%"></i></div>'
                     f'<div class="small muted">{round(share * 100)}% {done_words}</div>')
    if nxt and goal.get("status") == "active":
        stand.append(f'<div class="next"><span class="label">Next</span><span>{task_page._inline(nxt)}</span></div>')
    if stands.get("by"):
        stand.append(f'<p class="small muted" style="margin-top:.5rem">{_e(name(stands["by"]))} · '
                     f'{task_page._time(stands.get("at") or "")}</p>')
    body.append(f'<section class="card">{"".join(stand)}</section>')

    if goal.get("milestones"):
        items = []
        for m in goal["milestones"]:
            mine = [t for t in under if t.get("milestone") == m["id"]]
            count = f'{sum(1 for t in mine if t["status"] == "done")} of {len(mine)}' if mine else ""
            when = task_page._time(m["due"], "", m["due"]) if m.get("due") else ""
            items.append(f'<li class="{"done" if m.get("done") else ""}"><span class="check box"></span>'
                         f'<span class="grow">{_e(m["title"])}</span>'
                         f'<span class="sub">{" · ".join(p for p in (when, count) if p)}</span></li>')
        done = sum(1 for m in goal["milestones"] if m.get("done"))
        body.append(f'<h2>Milestones {done} of {len(goal["milestones"])}</h2><ul class="card rows">{"".join(items)}</ul>')

    body.extend(task_page._block(b) for b in page.get("blocks") or [])

    entries = list(reversed(goal.get("log") or []))

    def row(entry):
        by = entry.get("by") or ""
        who = "You" if by == "you" else name(by)
        return (f'<li><span class="grow">{_e(entry.get("text"))}</span>'
                f'<span class="sub">{_e(who)} · {task_page._time(entry.get("at") or "")}</span></li>')
    shown = "".join(row(e) for e in entries[:task_page.TIMELINE_SHOWN])
    rest = entries[task_page.TIMELINE_SHOWN:]
    more = (f'<details><summary>Show {len(rest)} More</summary><ul class="rows timeline">'
            + "".join(row(e) for e in rest) + "</ul></details>") if rest else ""
    body.append(f'<h2>Timeline</h2><div class="card" style="padding:0"><ul class="rows timeline">{shown}</ul>{more}</div>')
    keeper = name(goal["lead"]) if goal.get("lead") else "you"
    body.append(f'<p class="foot">Goal Page · kept by {_e(keeper)}</p>')
    if goal.get("lead"):
        body.append('<button class="ask" data-hermes-send="Bring this goal\'s Goal Page up to date: look at what moved, '
                    'then write where it stands with goals_update (stands and page).">Ask for an Update</button>')
    template = TEMPLATE.read_text(encoding="utf-8")
    return (template.replace("{{TITLE}}", _e(goal.get("title")))
                    .replace("{{GOAL_ID}}", _e(goal.get("id")))
                    .replace("{{BODY}}", "\n".join(part for part in body if part)))


def page_for(store, goal_id: str, names: dict | None = None) -> str | None:
    """The Goal Page's HTML for the route, or ``None`` for no such goal."""
    goal = store.get_goal(goal_id)
    if goal is None:
        return None
    with closing(store._db()) as db:
        under = _under(db, goal["id"])
    return render_page(goal, under, get_page(store, goal_id), names)


# ---------------------------------------------------------------------------
# The tools (toolset hq_goals): hidden while Tasks or Goals is off
# ---------------------------------------------------------------------------

def _schema(name: str, description: str, properties: dict, required=()) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}


GOAL_REF = {"type": "string", "description": "The goal: its id, or its exact name (goals_list)."}
OFFER_TASK = {"type": "object", "properties": {
    "title": {"type": "string"},
    "who": {"type": "string", "enum": ["me", "bot"], "description": "me: theirs. bot: a bot's own (bot, default you)."},
    "bot": {"type": "string", "description": "With who 'bot': the goal's lead or a helper (default you)."},
    "due": {"type": "string", "description": "Only if it has a date: " + tasks.DUE_HELP},
    "notes": {"type": "string", "description": "A sentence they can read at a glance."},
    "made": {"type": "array", "description": "What they need to do it, right on the task, up to "
             f"{OFFER_TASK_MADE_CAP}: when the task names something (\"use your course notes\", a page, a site, a "
             "template), attach that thing, not a pointer to it. file: its full path; link: the URL; note: the "
             "lines themselves in Markdown. If you couldn't find it, say so in notes (\"I couldn't find your Personal "
             "Brand notes: where are they?\") instead of sending them looking.",
             "items": {"type": "object", "properties": {
                 "title": {"type": "string", "description": "A few words, like \"Personal Brand notes\"."},
                 "kind": {"type": "string", "enum": ["file", "link", "note"]},
                 "ref": {"type": "string", "description": "file: its full path. link: the URL. note: the text."}},
                 "required": ["title"]}},
    "milestone": {"type": "string", "description": "One of the goal's milestones (or this offer's own), by name or id."}},
    "required": ["title"]}

LIST_SCHEMA = _schema(
    "goals_list",
    "The user's goals. goals: the ones you lead or help, whole: milestones, the number and who reads it, pace as "
    "numbers, the open tasks under it with ids, where it stands, and for a lead the offers held for it. others: the "
    "other active goals by name and why only. Each says you_lead / you_help.", {})

OFFER_SCHEMA = _schema(
    "goals_offer",
    "The one way to propose anything for a goal: it shows as ONE card the user taps (nothing changes until they do; "
    "Undo takes it back). kind 'goal': they said they want to reach something (\"I want to…\"): offer it as a goal, "
    "with a first plan if you have one (milestones, this week's tasks); if they say yes you lead it. kind 'plan': "
    "(the lead only) milestones and the first week of tasks for a goal that exists, and any set-up fields it lacks "
    "(name, why, due, measure) with built_on. Before a plan, look first for what they already have for it (their "
    "notes and files, memory, earlier tries, a course or method they follow) and build on it; then ask at most three "
    "questions on ONE question card, each with choices, skipping any the looking answered, in this order: what done "
    "looks like (a number or a date), what they already have or want to follow, how much time a week. A plan of set-up "
    "fields alone is fine when they ask (a goal with a plan but no number). kind 'checkin': (the lead only, "
    "in the weekly check-in) summary, then the next three or so tasks. kind 'tasks': tasks for a goal. Each task "
    "says who: 'me' (theirs) or 'bot' (a bot's own). A task that names something to use (their notes, a lesson, a "
    "page, a site, a template) carries it in made, found first, so they never go looking; if you can't find it, the "
    "notes say so. Tasks already under the goal come back as 'already'. After it, "
    "end your reply with one short line that asks (\"Want … on …?\"): nothing is added until they tap, so never say \"I "
    "added\" or \"I put\". Your own steps on a goal you lead or help go in with tasks_add instead. Research and drafts "
    "only, always.",
    {"kind": {"type": "string", "enum": list(KINDS)},
     "goal": GOAL_REF,
     "title": {"type": "string", "description": "kind 'goal': the goal's name, a few words (\"1,000 public testers\")."},
     "name": {"type": "string", "description": "kind 'plan': a short name for the goal, a few words (\"Run a 10K\"), "
              "when its name is a sentence. Their sentence becomes its why when it has none."},
     "why": {"type": "string", "description": "kind 'goal', or 'plan' when the goal has none: why it matters to "
             "them, one line in their words."},
     "due": {"type": "string", "description": "kind 'goal', or 'plan' when the goal has no date: the day they want "
             "it by, YYYY-MM-DD, only if they said."},
     "measure": {"type": "object", "description": "kind 'goal', or 'plan' when the goal has no number: a number that "
                 "says how far it is, {unit, start, target}, only when there is one."},
     "built_on": {"type": "array", "items": {"type": "string"}, "description": "kind 'plan': up to 3 short lines on "
                  "what the plan rests on, their answers and what you found (\"Your course notes, 8 units\", "
                  "\"3 hours a week\")."},
     "summary": {"type": "string", "description": "kind 'checkin': what moved since the last one, two sentences."},
     "milestones": {"type": "array", "description": "kind 'goal' or 'plan': the steps to the goal, in order, a few "
                    "words each.", "items": {"type": "object", "properties": {
                        "title": {"type": "string"}, "due": {"type": "string", "description": "YYYY-MM-DD"}},
                        "required": ["title"]}},
     "tasks": {"type": "array", "description": "Up to 20.", "items": OFFER_TASK},
     "asked": {"type": "boolean", "description": "kind 'plan': true only when they just asked you for a plan again."}},
    ("kind",))

UPDATE_SCHEMA = _schema(
    "goals_update",
    "Keep a goal you lead current: stands (where it stands, and what's next), page (its Goal Page: summary, next, "
    "blocks for what worked and what didn't; the goal-page skill has the guide), looks_done (a milestone id, or "
    "'goal', when it looks finished: they tap Mark Done or Not Yet; you never tick it yourself). The number: only "
    "when you are its source (measure.source.bot is you): measure_now with what you read, or checked=true when it "
    "hasn't moved. Never guess a number.",
    {"goal": GOAL_REF,
     "stands": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Where it stands now, a sentence or two."},
         "next": {"type": "string", "description": "What happens next, and who."}}, "required": ["text"]},
     "measure_now": {"type": "number", "description": "The number you read, where it says (measure.source.what)."},
     "checked": {"type": "boolean", "description": "You looked and it hasn't moved."},
     "page": tasks.PAGE,
     "looks_done": {"type": "string", "description": "A milestone's id, or 'goal'."},
     "why": {"type": "string", "description": "With looks_done: why, in a few words (\"All 4 tasks are done\")."}},
    ("goal",))

NEXT_REVIEW_SCHEMA = _schema(
    "goals_next_review",
    "Only for your Goal check-in job: the next goal you lead that is due its weekly look, with what moved since the "
    "last one (tasks ticked, tasks past their date, the number's history) and the offers held for it; or "
    "{stop: reason} (none due, quiet hours, today's limit). Call it again after each check-in, until it says stop.", {})


def register_tools(ctx, store_for: Callable, profile: Callable[[], str], notify: Callable[[dict, dict, str], None],
                   person: Callable[[str], bool] | None = None) -> None:
    """``notify(goal, offer, profile)`` sends the one push for a check-in (injected, as tasks.py's is). ``person(
    session_id)``: whether the person is in that chat (default ``person_in``)."""
    person = person or person_in

    def failed(error) -> str:
        return json.dumps({"error": str(error.detail)}, ensure_ascii=False)

    def handle_list(args, **kwargs):
        store = store_for()
        if not store.goals_on():
            return json.dumps({"goals": [], "note": "Goals is turned off."})
        return json.dumps(goals_list(store, profile()), ensure_ascii=False)

    def handle_offer(args, session_id="", **kwargs):
        store, bot = store_for(), profile()
        args = args if isinstance(args, dict) else {}
        items = []
        for item in args.get("tasks") if isinstance(args.get("tasks"), list) else []:
            if isinstance(item, dict):
                try:
                    items.append({**item, "due": tasks.resolve_due(item.get("due"), store.clock())})
                except tasks.TaskError as error:
                    return failed(error)
        try:
            made = offer(store, bot, str(session_id or ""), args.get("kind"), {**args, "tasks": items},
                         with_person=person(str(session_id or "")))
        except tasks.TaskError as error:
            return failed(error)
        out = {"offer": "waiting", "id": made["id"], "kind": made["kind"]}
        if made.get("already"):
            out["already"] = made["already"]
        if made.get("setup_dropped"):
            out["setup_dropped"] = made["setup_dropped"]
        if made.get("held"):
            out = {"held": True, "id": made["id"], "note": "Held for the goal's lead: it isn't a card now; its lead "
                   "folds it in at the next check-in, or drops it. Don't tell them it's waiting for them."}
        else:
            out["note"] = OFFER_LINE
        if made["kind"] == "checkin":
            goal = store.get_goal(made["goal"])
            if goal is not None and not store._quiet_now(store.get_settings()):
                try:   # one push per check-in, never at night
                    notify(goal, made, bot)
                except Exception:
                    log.debug("hermes-hq-push: goal check-in push not sent", exc_info=True)
            reconcile_checkin_job(store, bot, tasks.bot_home(bot))
        return json.dumps(out, ensure_ascii=False)

    def handle_update(args, **kwargs):
        store, bot = store_for(), profile()
        args = args if isinstance(args, dict) else {}
        try:
            out = update(store, bot, args.get("goal"), stands=args.get("stands"), measure_now=args.get("measure_now"),
                         page=args.get("page"), looks_done=args.get("looks_done"), why=args.get("why"),
                         checked=args.get("checked") is True or None)
        except tasks.TaskError as error:
            return failed(error)
        return json.dumps(out, ensure_ascii=False)

    def handle_next_review(args, **kwargs):
        store, bot = store_for(), profile()
        out = next_review(store, bot)
        if "stop" in out:   # the run ends here: its job is armed for the next due slot (or removed)
            reconcile_checkin_job(store, bot, tasks.bot_home(bot))
        return json.dumps(out, ensure_ascii=False)

    def on() -> bool:
        """The tools exist only while Tasks and its Goals are on."""
        try:
            return store_for().goals_on()
        except Exception:
            return False

    log.info("hermes-hq-push: goal offers ask before adding")
    for schema, handler, emoji in ((LIST_SCHEMA, handle_list, "🎯"), (OFFER_SCHEMA, handle_offer, "🗳️"),
                                   (UPDATE_SCHEMA, handle_update, "📍"), (NEXT_REVIEW_SCHEMA, handle_next_review, "🗓️")):
        ctx.register_tool(name=schema["name"], toolset="hq_goals", check_fn=on, schema=schema, handler=handler,
                          description=schema["description"], emoji=emoji)
