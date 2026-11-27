---
name: goal-page
description: Lead a goal in the Hermes HQ app — its plan, where it stands, the weekly check-in and its Goal Page — with goals_update and goals_offer. Use whenever you lead a goal (goals_list says you_lead), in its own chat or in your Goal check-in run.
---

# Leading a goal in Hermes HQ

A goal is something the person wants to reach over time: a finish line, maybe a date, a one-line why, milestones,
and ordinary tasks under them. **One bot leads it**; other bots may help. If you lead, four things are yours:
**the plan**, **where it stands**, **the weekly check-in**, and **its Goal Page**. **You write the words; the app draws the page.**
You never write HTML.

The page already shows, by itself: the status, the ring, the title, the why, the date, the number (with who reads it
and its history week by week), the milestones (ticked or not, with dates and "2 of 4" tasks), and the goal's
timeline. **Don't repeat any of that.** What only you add:

| Where | What it is | Example |
|---|---|---|
| `stands.text` | Where the goal is **now**, 1–2 sentences: the answer first. The first thing they read. | "Week 5 of 12: long runs are up to 7 km. One missed week, made up since." |
| `stands.next` | What happens next, and who. | "You run 8 km on Saturday; wren finds a race date." |
| check-in `summary` | What **moved since the last check-in**, two sentences. | "You ran 3 of 3 runs and the long one reached 7 km. The race shortlist is ready; no date picked yet." |
| `page.blocks` | What worked, what didn't, what you found: parts with a `title` and any of `text`, `table`, `numbers`. Up to 8. | see below |

## Before the plan

A plan built on guesses is a plan they don't take. So, in this order:

1. **Look first** for what they already have for it: their notes and files, your memory of them, earlier tries, a
   course or method they follow. Build on that; don't start from nothing.
2. **Ask at most three questions, on ONE question card**, each with choices, and skip any your looking already
   answered. In this order:
   1. **What done looks like**: a number or a date ("10 km by May 30").
   2. **What they already have or want to follow** ("my course notes", "a plan from a friend", "nothing yet").
   3. **How much time a week** ("2 hours", "3 hours", "more").
3. **Offer the plan** with `goals_offer` kind `plan`: milestones and the first week's tasks, and with them:
   - `name`: a short name, a few words, when the goal's name is a sentence ("I want to start running so I feel
     stronger" becomes "Run a 10K"; their sentence becomes its why, so their words are kept).
   - `why`: one line in their words, when the goal has none.
   - `measure` (`{unit, start, target}`) and `due` (YYYY-MM-DD), when they gave one.
   - `built_on`: up to 3 short lines on what the plan rests on, their answers and what you found.

Each set-up field is kept only where the goal lacks it (a name only when it differs and no other goal has it); the
answer's `setup_dropped` says which were left out and why. When they tap Add, the name, why, date and number are set
in the same go as the milestones, and Undo takes them back.

```json
goals_offer({
  "kind": "plan", "goal": "<goal id>",
  "name": "Run a 10K",
  "measure": {"unit": "km", "start": 0, "target": 10}, "due": "2027-05-30",
  "built_on": ["Your couch-to-10K notes, 8 weeks", "3 runs a week, 30 minutes each"],
  "milestones": [{"title": "Run 5 km"}, {"title": "Run 8 km"}, {"title": "Race day"}],
  "tasks": [{"title": "First 20-minute run", "who": "me", "milestone": "Run 5 km"},
            {"title": "Find two flat routes", "who": "bot", "bot": "juno"}]
})
```

### Put what a task needs on the task

A task that says "use your course notes" and leaves them to find the notes is half a task. When a task names
something to use (their notes, a lesson, a page, a site, a template, a draft), **find it first and attach it** with
the task's `made`: `file` (its full path), `link` (the URL) or `note` (the lines themselves, in Markdown), up to 6.
If you looked and couldn't find it, the task's `notes` say so plainly ("I couldn't find your Personal Brand notes:
where are they?"). Never "see the notes" with nothing attached.

```json
{"title": "Work through the brand blueprint (40 min)", "who": "me",
 "notes": "One founder you want to reach, one problem you solve, one example you can share.",
 "made": [{"title": "Brand blueprint worksheet", "kind": "file", "ref": "/Users/.../course/brand-blueprint.pdf"},
          {"title": "Lesson 2: Personal Brand", "kind": "link", "ref": "https://course.example.com/lesson-2"}]}
```

Tasks already under a goal that point at something with nothing attached: attach it with `tasks_update` (`made`)
on any task you added that they haven't changed yet.

A goal that has a plan already but no number, or a sentence for a name, can be offered **just the set-up fields**
(a plan with only `name`, `measure` or `due`), when they ask.

## Where it stands

```json
goals_update({
  "goal": "<goal id>",
  "stands": {"text": "Week 5 of 12: long runs are up to 7 km. One missed week, made up since.",
             "next": "You run 8 km on Saturday; wren finds a race date."}
})
```

- **Keep it true.** Write it again each time something moves (after a check-in, when a milestone looks done). A
  line that says "Just starting" in week 6 is worse than none.
- **Say it to them, plainly.** "You ran", "you're two weeks ahead", not "the user has progressed".

## The weekly check-in

Your "Goal check-in" job calls `goals_next_review`, which hands you one goal with what moved: tasks ticked, tasks
past their date, the number's history, and any tasks other bots offered that were **held for you**. Then:

1. **The number**, only if you read it (`measure.source.bot` is you): look where it says (`measure.source.what`)
   and send `measure_now`, or `checked: true` if it hasn't moved. Couldn't read it? Say so in `stands` and ask them.
   **Never guess a number**, and never set one that is theirs ("you").
2. **Where it stands**: `goals_update(stands=…)`, and the page (below) if you learned something.
3. **One check-in card**: `goals_offer` with kind `checkin`: the two-sentence `summary`, then the next three or so
   tasks, each with `who` ("me" for theirs, "bot" for a bot's own, with `bot`). Fold in held tasks worth doing; the
   rest are dropped.
4. Call `goals_next_review` again until it says stop.

```json
goals_offer({
  "kind": "checkin", "goal": "<goal id>",
  "summary": "You ran 3 of 3 runs and the long one reached 7 km. The race shortlist is ready; no date picked yet.",
  "tasks": [
    {"title": "Long run, 8 km", "who": "me", "due": "saturday 08:00", "milestone": "Run 8 km"},
    {"title": "Pick a race date from the shortlist", "who": "me"},
    {"title": "Find two flat 8 km routes", "who": "bot", "bot": "juno"}
  ]
})
```

The check-in is **research and drafts only**: never send, book, buy, pay, post or delete anything for the goal (it is
refused there anyway). A race to sign up for is a task for them, not something you do.

## The Goal Page

```json
goals_update({
  "goal": "<goal id>",
  "page": {
    "summary": "Week 5 of 12: long runs are up to 7 km.",
    "next": "You run 8 km on Saturday; wren finds a race date.",
    "blocks": [
      {"title": "What worked", "text": "- Morning runs: **3 of 3** this week\n- Shorter weekday runs kept the long one fresh"},
      {"title": "What didn't", "text": "- Week 3: no runs (travel). Made up with an extra run in week 4"},
      {"title": "Race shortlist", "table": {"columns": ["Race", "Date", "Distance", "Notes"],
        "rows": [["Spring 10K", "May 17", "10 km", "Flat"],
                 ["Hilltop 10K", "May 30", "10 km", "Two climbs"]]}},
      {"title": "At a glance", "numbers": [{"label": "Longest run", "value": "7 km"},
                                           {"label": "Runs this month", "value": "11"}]}
    ]
  }
})
```

- **Update, don't rebuild.** `summary` and `next` replace theirs when sent; a part replaces the one with the **same
  title**; a new title is added; `{"title": "Old part", "remove": true}` takes one off. Send only what changed.
- **What worked / What didn't** are the heart of it: what you'd tell them over coffee. Short lines, the reason after.
- Good part titles: "What worked", "What didn't", "What I found", "Options", "Open questions". A few words each.
- If the answer has `page_notes`, something was left out: fix it in your next update.

## Looks done

When a milestone's tasks are all done (or every milestone is), say so; **never tick it yourself**:

```json
goals_update({"goal": "<goal id>", "looks_done": "<milestone id>", "why": "All 4 runs are done"})
```

They tap Mark Done or Not Yet. After Not Yet, don't say it again until something under it moves.

## Who does what

The example goal is "Run a 10K" (by May 30), led by **otto**, with **wren** and **juno** helping:

- **otto** (lead) looks for what they have, asks at most three questions on one card, then offers the plan
  (`goals_offer` kind `plan`: milestones, the first week's tasks, and the set-up fields the goal lacks), runs the
  check-in, writes `stands` and the page, and says when something looks done.
- **wren** and **juno** (helpers) take tasks: their own go under the goal with `tasks_add` (`goal`, who "bot");
  tasks for the person they offer with `goals_offer` kind `tasks`. They tell otto what they found
  (`message_agent`) rather than writing on the page.
- **herald** and **ivy** aren't on it: they know its name and why, and offer tasks for it; theirs wait for otto's next
  check-in.

## Don'ts

- Don't paste HTML, scripts or styles: everything you send is shown as words.
- Don't copy the milestones, the number, the date or the timeline onto the page: they're there already.
- Don't tick their task, a milestone or the goal; don't move a number you don't read.
- Don't put passwords, codes or other secrets on a page.
