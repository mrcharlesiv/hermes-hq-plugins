---
name: task-page
description: Keep a task's Task Page current in the Hermes HQ app — where it stands, what's next and what you found — with tasks_update(page=…). Use every time you work on a task from the Tasks list (prep runs, a task's own chat, a hand-off).
---

# Task Pages for Hermes HQ

Every task on the person's Tasks list has a **Task Page** in the Hermes HQ app: one page they open to see
everything about the task and where it stands, at a glance. **You write the words; the app draws the page.** Every
task's page has the same look, so the person always knows where to look. You never write HTML for it.

The page already shows, by itself: the status, the title, who it's with, when it's due, the steps (ticked or not, and
whose), a progress bar, what you made (`made`), the task's notes and its history. **Don't repeat any of that.** What
only you can add:

| Field | What it is | Example |
|---|---|---|
| `summary` | Where it stands **now**, 1–3 short sentences. The first thing they read. | "3 venues shortlisted. Elm & Oak fits best and holds the 14th until Friday." |
| `next` | What happens next, and who does it. | "You pick one by Thursday; I'll book it." |
| `blocks` | Parts of the page, each with a `title` and any of `text`, `table`, `numbers`. Up to 8. | see below |

## Writing it

```json
tasks_update({
  "id": "<task id>",
  "page": {
    "summary": "3 venues shortlisted. Elm & Oak fits best and holds the 14th until Friday.",
    "next": "You pick one by Thursday; I'll book it.",
    "blocks": [
      {"title": "Options", "table": {"columns": ["Venue", "Price", "Fits 40?", "Notes"],
        "rows": [["Elm & Oak", "$1,800", "Yes", "Holds the 14th until Fri"],
                 ["The Loft", "$2,400", "Yes", "Parking is tight"],
                 ["Harbor Room", "$1,500", "35 max", "Cheapest"]]}},
      {"title": "At a glance", "numbers": [{"label": "Venues checked", "value": "9"},
                                           {"label": "Best price", "value": "$1,500", "note": "Harbor Room"}]},
      {"title": "What I found", "text": "- Most places want a deposit within **48 hours**\n- Elm & Oak includes AV\n- [Elm & Oak menu](https://example.com/menu)"}
    ]
  }
})
```

- **Update, don't rebuild.** `summary` and `next` replace theirs when you send them; leave them out to keep them.
  A part (`blocks` item) replaces the one with the **same title**; a new title is added after the others;
  `{"title": "Old part", "remove": true}` takes one off. So send only what changed.
- **Keep it true.** Each time you work on the task, update `summary` (and `next`) to match. A page that says
  "Searching…" after you finished is worse than no page.
- **Plain words, short lines.** Write for someone glancing at their phone: the answer first, then the why. No
  "I have completed the research phase" — say what you found.
- **Pick the right part:**
  - `table` (`columns`, `rows`) to **compare options** (up to 6 columns, 25 rows). Short cells.
  - `numbers` (`label`, `value`, optional `note`, up to 6) for **the few numbers that matter** ("$1,500", "3 of 9").
  - `text` (Markdown: short lines, `- ` lists, `- [ ]` / `- [x]` checklists, `**bold**`, `[links](https://…)`) for
    **findings, reasons and drafts**. A long draft (an email, a script) goes in `made` as a note instead.
  - Good part titles: "Options", "What I found", "Draft", "Open questions", "Prices", "Decisions". A few words each.
- **Waiting on them?** Set `status: "needs_you"` with the question in `status_note`: the page shows it in its own
  banner at the top. Say what's waiting in `next` too ("Waiting for your pick").
- **Done?** Leave a `summary` that tells the outcome ("Booked Elm & Oak for the 14th; confirmation is in Made.").

## Do it with the rest of the update

Write the page in the **same** `tasks_update` as your other changes (steps ticked, `made`, `status`), so the page,
the card and the notification all say the same thing. If the answer has `page_notes`, something was left out: fix
it in your next update.

## Don'ts

- Don't paste HTML, scripts or styles: everything you send is shown as words.
- Don't copy the steps, `made` items, status or history onto the page: they're there already.
- Don't put passwords, codes or other secrets on a page.
- Don't make more than 8 parts: fold small ones together.
